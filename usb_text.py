"""
USB 文字传输工具 - 手机远程输入到电脑
通过 ADB USB 连接，手机 PWA 打字后自动输入到电脑活跃窗口
"""

import ctypes
import ctypes.wintypes as wintypes
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse

# ============================================================
# 常量配置
# ============================================================
PORT = 8765
MAX_MESSAGES = 500

# Win32 常量
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0xA2
VK_V = 0x56

# ============================================================
# Win32 ctypes 结构体声明
# ============================================================
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# 剪贴板函数
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HANDLE
kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
kernel32.GlobalUnlock.restype = wintypes.BOOL


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", INPUT_UNION),
    ]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT

# ============================================================
# 剪贴板 + 按键模拟
# ============================================================
_clipboard_lock = threading.Lock()


def set_clipboard(text):
    """将文字写入 Windows 剪贴板"""
    with _clipboard_lock:
        data = (text + "\0").encode("utf-16-le")
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h_mem:
            return False
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            return False
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(h_mem)

        if not user32.OpenClipboard(0):
            return False
        user32.EmptyClipboard()
        result = user32.SetClipboardData(CF_UNICODETEXT, h_mem)
        user32.CloseClipboard()
        return bool(result)


def send_ctrl_v():
    """模拟 Ctrl+V 按键"""
    inputs = (INPUT * 4)()

    # Ctrl 按下
    inputs[0].type = INPUT_KEYBOARD
    inputs[0].union.ki.wVk = VK_CONTROL
    inputs[0].union.ki.dwFlags = 0

    # V 按下
    inputs[1].type = INPUT_KEYBOARD
    inputs[1].union.ki.wVk = VK_V
    inputs[1].union.ki.dwFlags = 0

    # V 释放
    inputs[2].type = INPUT_KEYBOARD
    inputs[2].union.ki.wVk = VK_V
    inputs[2].union.ki.dwFlags = KEYEVENTF_KEYUP

    # Ctrl 释放
    inputs[3].type = INPUT_KEYBOARD
    inputs[3].union.ki.wVk = VK_CONTROL
    inputs[3].union.ki.dwFlags = KEYEVENTF_KEYUP

    sent = user32.SendInput(4, inputs, ctypes.sizeof(INPUT))
    return sent == 4


def paste_text(text):
    """复制文字到剪贴板并模拟 Ctrl+V 粘贴到活跃窗口"""
    if not set_clipboard(text):
        print(f"[!] 剪贴板写入失败")
        return False
    time.sleep(0.05)
    if not send_ctrl_v():
        print(f"[!] 模拟按键失败")
        return False
    return True


# ============================================================
# 消息存储
# ============================================================
class MessageStore:
    def __init__(self):
        self._messages = []
        self._lock = threading.Lock()
        self._counter = 0

    def add(self, text, from_who):
        with self._lock:
            self._counter += 1
            msg = {
                "id": self._counter,
                "text": text,
                "from": from_who,
                "time": datetime.now().strftime("%H:%M:%S"),
            }
            self._messages.append(msg)
            if len(self._messages) > MAX_MESSAGES:
                self._messages = self._messages[-MAX_MESSAGES:]
            return msg

    def get_since(self, since_id):
        with self._lock:
            return [m for m in self._messages if m["id"] > since_id]

    def get_all(self):
        with self._lock:
            return list(self._messages)

    def delete(self, msg_id):
        with self._lock:
            before = len(self._messages)
            self._messages = [m for m in self._messages if m["id"] != msg_id]
            return len(self._messages) < before

    def delete_many(self, ids):
        with self._lock:
            id_set = set(ids)
            before = len(self._messages)
            self._messages = [m for m in self._messages if m["id"] not in id_set]
            return before - len(self._messages)

    def clear(self):
        with self._lock:
            count = len(self._messages)
            self._messages.clear()
            return count


store = MessageStore()
auto_paste_enabled = True
start_time = time.time()

# ============================================================
# HTML 模板
# ============================================================
MANIFEST_JSON = json.dumps({
    "name": "USB 文字传输",
    "short_name": "文字传输",
    "start_url": "/mobile",
    "display": "standalone",
    "background_color": "#0f0f23",
    "theme_color": "#16213e",
    "icons": [{
        "src": "data:image/svg+xml," +
               "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 192 192'>"
               "<rect width='192' height='192' rx='40' fill='%230066ff'/>"
               "<text x='96' y='130' font-size='120' text-anchor='middle' fill='white' font-family='Arial'>T</text>"
               "</svg>",
        "sizes": "192x192",
        "type": "image/svg+xml"
    }]
}, ensure_ascii=False)

SW_JS = """
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));
"""

MOBILE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#16213e">
<link rel="manifest" href="/manifest.json">
<title>USB 文字传输</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0f0f23;--panel:#1a1a2e;--send-bubble:#0066ff;--recv-bubble:#2a2a3e;--text:#e0e0e0;--text-dim:#888;--accent:#0066ff;--safe-bottom:env(safe-area-inset-bottom,0px)}
body{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--text);height:100vh;height:100dvh;display:flex;flex-direction:column;overflow:hidden}
.header{background:var(--panel);padding:12px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #2a2a3e;flex-shrink:0}
.header h1{font-size:16px;font-weight:600}
.header-right{display:flex;align-items:center;gap:10px}
.manage-btn{background:none;border:1px solid #555;color:var(--text);padding:4px 12px;border-radius:14px;font-size:12px;cursor:pointer;transition:.2s}
.manage-btn.active{border-color:#f44336;color:#f44336}
.status{font-size:12px;display:flex;align-items:center;gap:4px}
.status-dot{width:8px;height:8px;border-radius:50%;background:#4caf50;display:inline-block}
.status-dot.off{background:#f44336}
.messages{flex:1;overflow-y:auto;padding:12px 16px;display:flex;flex-direction:column;gap:8px;-webkit-overflow-scrolling:touch}
.msg{max-width:80%;padding:10px 14px;border-radius:16px;font-size:15px;line-height:1.5;word-break:break-word;position:relative;transition:opacity .2s}
.msg .time{font-size:11px;color:var(--text-dim);margin-top:4px}
.msg.phone{align-self:flex-end;background:var(--send-bubble);border-bottom-right-radius:4px}
.msg.phone .time{text-align:right}
.msg.pc{align-self:flex-start;background:var(--recv-bubble);border-bottom-left-radius:4px}
.msg.selected{outline:2px solid #ff4444;opacity:.6}
.del-bar{display:none;background:var(--panel);padding:8px 12px;border-bottom:1px solid #2a2a3e;flex-shrink:0;align-items:center;justify-content:space-between}
.del-bar.show{display:flex}
.del-bar button{padding:7px 14px;border-radius:8px;border:none;font-size:13px;cursor:pointer;color:#fff}
.del-bar .cancel-btn{background:#555}
.del-bar .sel-all-btn{background:#2196f3}
.del-bar .del-btn{background:#f44336}
.del-bar .clear-btn{background:#ff9800}
.del-bar .count{color:var(--text);font-size:13px}
.input-area{background:var(--panel);padding:8px 12px;padding-bottom:calc(8px + var(--safe-bottom));display:flex;gap:8px;align-items:flex-end;border-top:1px solid #2a2a3e;flex-shrink:0}
.input-area textarea{flex:1;background:#2a2a3e;border:none;border-radius:20px;padding:10px 16px;color:var(--text);font-size:15px;font-family:inherit;resize:none;outline:none;max-height:100px;line-height:1.4}
.input-area textarea::placeholder{color:var(--text-dim)}
.send-btn{width:44px;height:44px;border-radius:50%;background:var(--accent);border:none;color:#fff;font-size:20px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center;transition:opacity .2s}
.send-btn:active{opacity:.7}
.send-btn:disabled{opacity:.3}
.empty-hint{text-align:center;color:var(--text-dim);font-size:14px;margin-top:40%;line-height:1.8}
</style>
</head>
<body>
<div class="header">
  <h1>USB 文字传输</h1>
  <div class="header-right">
    <button class="manage-btn" id="manageBtn" onclick="toggleManage()">管理</button>
    <div class="status"><span class="status-dot" id="statusDot"></span><span id="statusText">连接中...</span></div>
  </div>
</div>
<div class="del-bar" id="delBar">
  <button class="cancel-btn" onclick="cancelSelect()">取消</button>
  <button class="sel-all-btn" onclick="selectAll()">全选</button>
  <span class="count" id="selCount">已选 0 条</span>
  <button class="del-btn" onclick="deleteSelected()">删除</button>
  <button class="clear-btn" onclick="clearAll()">清空</button>
</div>
<div class="messages" id="msgArea">
  <div class="empty-hint" id="emptyHint">在下方输入文字<br>发送后将自动输入到电脑</div>
</div>
<div class="input-area">
  <textarea id="input" rows="1" placeholder="输入文字..."></textarea>
  <button class="send-btn" id="sendBtn" onclick="sendMsg()">&#9654;</button>
</div>
<script>
let lastId=0,polling=null,connected=false,selectMode=false,selectedIds=new Set(),allMsgIds=[];
const msgArea=document.getElementById('msgArea'),input=document.getElementById('input'),
      emptyHint=document.getElementById('emptyHint'),statusDot=document.getElementById('statusDot'),
      statusText=document.getElementById('statusText'),sendBtn=document.getElementById('sendBtn'),
      delBar=document.getElementById('delBar'),selCount=document.getElementById('selCount'),
      manageBtn=document.getElementById('manageBtn');

input.addEventListener('input',function(){
  this.style.height='auto';
  this.style.height=Math.min(this.scrollHeight,100)+'px';
});
input.addEventListener('keydown',function(e){
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMsg();}
});

function sendMsg(){
  const text=input.value.trim();
  if(!text)return;
  sendBtn.disabled=true;
  fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:text,from:'phone'})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){input.value='';input.style.height='auto';pollNow();}
  }).catch(()=>{}).finally(()=>{sendBtn.disabled=false;input.focus();});
}

function renderMsg(m){
  if(emptyHint&&emptyHint.parentNode)emptyHint.remove();
  allMsgIds.push(m.id);
  const div=document.createElement('div');
  div.className='msg '+(m.from==='phone'?'phone':'pc');
  div.dataset.id=m.id;
  div.innerHTML='<div>'+escHtml(m.text)+'</div><div class="time">'+m.time+'</div>';
  div.addEventListener('click',function(){if(selectMode){toggleSelect(div,m.id);}});
  msgArea.appendChild(div);
  msgArea.scrollTop=msgArea.scrollHeight;
}

function toggleManage(){
  if(selectMode){cancelSelect();}
  else{enterSelectMode();}
}
function enterSelectMode(){
  if(!selectMode){selectMode=true;delBar.classList.add('show');manageBtn.textContent='完成';manageBtn.classList.add('active');}
}
function toggleSelect(div,id){
  if(selectedIds.has(id)){selectedIds.delete(id);div.classList.remove('selected');}
  else{selectedIds.add(id);div.classList.add('selected');}
  selCount.textContent='已选 '+selectedIds.size+' 条';
}
function selectAll(){
  allMsgIds.forEach(id=>{
    selectedIds.add(id);
    const el=msgArea.querySelector('[data-id="'+id+'"]');
    if(el)el.classList.add('selected');
  });
  selCount.textContent='已选 '+selectedIds.size+' 条';
}
function cancelSelect(){
  selectMode=false;selectedIds.clear();delBar.classList.remove('show');
  manageBtn.textContent='管理';manageBtn.classList.remove('active');
  msgArea.querySelectorAll('.msg.selected').forEach(el=>el.classList.remove('selected'));
}
function deleteSelected(){
  if(selectedIds.size===0)return;
  fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ids:Array.from(selectedIds)})})
  .then(r=>r.json()).then(d=>{if(d.ok)reloadMessages();}).catch(()=>{});
}
function clearAll(){
  fetch('/api/clear',{method:'POST'}).then(r=>r.json()).then(d=>{if(d.ok)reloadMessages();}).catch(()=>{});
}
function reloadMessages(){
  cancelSelect();lastId=0;allMsgIds=[];msgArea.innerHTML='';pollNow();
}

function escHtml(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');}

function pollNow(){
  fetch('/api/messages?since='+lastId).then(r=>r.json()).then(d=>{
    if(d.messages&&d.messages.length>0){
      d.messages.forEach(m=>renderMsg(m));
      lastId=d.last_id;
    }
    setConnected(true);
  }).catch(()=>{setConnected(false);});
}

function setConnected(v){
  connected=v;
  statusDot.className='status-dot'+(v?'':' off');
  statusText.textContent=v?'已连接':'未连接';
}

if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js').catch(()=>{});}
pollNow();
polling=setInterval(pollNow,1500);
</script>
</body>
</html>"""

PC_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>USB 文字传输 - 电脑端</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0f0f23;--panel:#1a1a2e;--send-bubble:#2a6e2a;--recv-bubble:#0066ff;--text:#e0e0e0;--text-dim:#888;--accent:#0066ff;--border:#2a2a3e}
body{font-family:'Segoe UI','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column}
.header{background:var(--panel);padding:12px 20px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border)}
.header h1{font-size:18px;font-weight:600}
.header-right{display:flex;align-items:center;gap:16px;font-size:13px}
.toggle{display:flex;align-items:center;gap:6px;cursor:pointer}
.toggle input{display:none}
.toggle .slider{width:36px;height:20px;background:#555;border-radius:10px;position:relative;transition:.3s}
.toggle .slider::after{content:'';width:16px;height:16px;background:#fff;border-radius:50%;position:absolute;top:2px;left:2px;transition:.3s}
.toggle input:checked+.slider{background:var(--accent)}
.toggle input:checked+.slider::after{left:18px}
.status{display:flex;align-items:center;gap:4px}
.status-dot{width:8px;height:8px;border-radius:50%;background:#4caf50}
.status-dot.off{background:#f44336}
.stats{color:var(--text-dim);font-size:12px}
.del-bar{display:none;background:var(--panel);padding:8px 20px;border-bottom:1px solid var(--border);align-items:center;gap:12px}
.del-bar.show{display:flex}
.del-bar button{padding:6px 14px;border-radius:6px;border:none;font-size:13px;cursor:pointer;color:#fff}
.del-bar .cancel-btn{background:#555}
.del-bar .sel-all-btn{background:#2196f3}
.del-bar .del-btn{background:#f44336}
.del-bar .clear-btn{background:#ff9800}
.del-bar .count{color:var(--text);font-size:13px;margin-left:auto}
.main{flex:1;display:flex;overflow:hidden}
.messages{flex:1;overflow-y:auto;padding:16px 20px;display:flex;flex-direction:column;gap:8px}
.msg{max-width:65%;padding:10px 14px;border-radius:16px;font-size:14px;line-height:1.5;word-break:break-word;position:relative;transition:opacity .2s;cursor:pointer}
.msg .meta{font-size:11px;color:var(--text-dim);margin-top:4px;display:flex;align-items:center;gap:8px}
.msg.pc{align-self:flex-end;background:var(--send-bubble);border-bottom-right-radius:4px}
.msg.pc .meta{justify-content:flex-end}
.msg.phone{align-self:flex-start;background:var(--recv-bubble);border-bottom-left-radius:4px}
.msg.selected{outline:2px solid #ff4444;opacity:.6}
.copy-btn,.del-single-btn{background:none;border:none;color:var(--text-dim);cursor:pointer;font-size:11px;padding:2px 6px;border-radius:4px;transition:.2s}
.copy-btn:hover,.del-single-btn:hover{background:rgba(255,255,255,.1);color:#fff}
.input-area{background:var(--panel);padding:12px 20px;display:flex;gap:10px;align-items:flex-end;border-top:1px solid var(--border)}
.input-area textarea{flex:1;background:#2a2a3e;border:none;border-radius:12px;padding:10px 16px;color:var(--text);font-size:14px;font-family:inherit;resize:none;outline:none;max-height:120px;line-height:1.4}
.input-area textarea::placeholder{color:var(--text-dim)}
.send-btn{padding:10px 24px;border-radius:12px;background:var(--accent);border:none;color:#fff;font-size:14px;cursor:pointer;transition:opacity .2s;white-space:nowrap}
.send-btn:hover{opacity:.85}
.send-btn:active{opacity:.7}
.empty-hint{text-align:center;color:var(--text-dim);font-size:14px;margin:auto;line-height:2}
</style>
</head>
<body>
<div class="header">
  <h1>USB 文字传输 - 电脑端</h1>
  <div class="header-right">
    <div class="stats" id="stats">消息: 0</div>
    <label class="toggle" title="手机发送文字时自动粘贴到活跃窗口">
      <span>自动粘贴</span>
      <input type="checkbox" id="autoPaste" checked onchange="toggleAutoPaste()">
      <span class="slider"></span>
    </label>
    <div class="status"><span class="status-dot" id="statusDot"></span><span id="statusText">连接中</span></div>
  </div>
</div>
<div class="del-bar" id="delBar">
  <button class="cancel-btn" onclick="cancelSelect()">取消</button>
  <button class="sel-all-btn" onclick="selectAll()">全选</button>
  <span class="count" id="selCount">已选 0 条</span>
  <button class="del-btn" onclick="deleteSelected()">删除选中</button>
  <button class="clear-btn" onclick="clearAll()">清空全部</button>
</div>
<div class="main">
  <div class="messages" id="msgArea">
    <div class="empty-hint" id="emptyHint">等待消息...<br>手机发送的文字将显示在这里<br>在下方输入框可向手机发送文字</div>
  </div>
</div>
<div class="input-area">
  <textarea id="input" rows="1" placeholder="输入要发送到手机的文字..."></textarea>
  <button class="send-btn" onclick="sendMsg()">发送到手机</button>
</div>
<script>
let lastId=0,polling=null,msgCount=0,selectMode=false,selectedIds=new Set(),allMsgIds=[];
const msgArea=document.getElementById('msgArea'),input=document.getElementById('input'),
      emptyHint=document.getElementById('emptyHint'),statusDot=document.getElementById('statusDot'),
      statusText=document.getElementById('statusText'),stats=document.getElementById('stats'),
      delBar=document.getElementById('delBar'),selCount=document.getElementById('selCount');

input.addEventListener('input',function(){
  this.style.height='auto';
  this.style.height=Math.min(this.scrollHeight,120)+'px';
});
input.addEventListener('keydown',function(e){
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMsg();}
});

function sendMsg(){
  const text=input.value.trim();
  if(!text)return;
  fetch('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:text,from:'pc'})})
  .then(r=>r.json()).then(d=>{
    if(d.ok){input.value='';input.style.height='auto';pollNow();}
  }).catch(()=>{});
}

function toggleAutoPaste(){
  const v=document.getElementById('autoPaste').checked;
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({auto_paste:v})}).catch(()=>{});
}

function copyText(text){
  navigator.clipboard.writeText(text).then(()=>{}).catch(()=>{});
}

function renderMsg(m){
  if(emptyHint&&emptyHint.parentNode)emptyHint.remove();
  msgCount++;allMsgIds.push(m.id);
  stats.textContent='消息: '+msgCount;
  const div=document.createElement('div');
  div.className='msg '+(m.from==='phone'?'phone':'pc');
  div.dataset.id=m.id;
  const label=m.from==='phone'?'手机':'电脑';
  div.innerHTML='<div>'+escHtml(m.text)+'</div>'+
    '<div class="meta"><span>'+label+' '+m.time+'</span>'+
    '<button class="copy-btn" onclick="event.stopPropagation();copyText(\''+escJs(m.text)+'\')">复制</button>'+
    '<button class="del-single-btn" onclick="event.stopPropagation();deleteSingle('+m.id+')">删除</button>'+
    '</div>';
  div.addEventListener('click',function(){if(selectMode){toggleSelect(div,m.id);}});
  div.addEventListener('dblclick',function(){enterSelectMode();toggleSelect(div,m.id);});
  msgArea.appendChild(div);
  msgArea.scrollTop=msgArea.scrollHeight;
}

function enterSelectMode(){if(!selectMode){selectMode=true;delBar.classList.add('show');}}
function toggleSelect(div,id){
  if(selectedIds.has(id)){selectedIds.delete(id);div.classList.remove('selected');}
  else{selectedIds.add(id);div.classList.add('selected');}
  selCount.textContent='已选 '+selectedIds.size+' 条';
  if(selectedIds.size===0)cancelSelect();
}
function selectAll(){
  enterSelectMode();
  allMsgIds.forEach(id=>{
    selectedIds.add(id);
    const el=msgArea.querySelector('[data-id="'+id+'"]');
    if(el)el.classList.add('selected');
  });
  selCount.textContent='已选 '+selectedIds.size+' 条';
}
function cancelSelect(){
  selectMode=false;selectedIds.clear();delBar.classList.remove('show');
  msgArea.querySelectorAll('.msg.selected').forEach(el=>el.classList.remove('selected'));
}
function deleteSingle(id){
  fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ids:[id]})})
  .then(r=>r.json()).then(d=>{if(d.ok)reloadMessages();}).catch(()=>{});
}
function deleteSelected(){
  if(selectedIds.size===0)return;
  fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ids:Array.from(selectedIds)})})
  .then(r=>r.json()).then(d=>{if(d.ok)reloadMessages();}).catch(()=>{});
}
function clearAll(){
  fetch('/api/clear',{method:'POST'}).then(r=>r.json()).then(d=>{if(d.ok)reloadMessages();}).catch(()=>{});
}
function reloadMessages(){
  cancelSelect();lastId=0;msgCount=0;allMsgIds=[];msgArea.innerHTML='';stats.textContent='消息: 0';pollNow();
}

function escHtml(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');}
function escJs(s){return s.replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/\n/g,'\\n');}

function pollNow(){
  fetch('/api/messages?since='+lastId).then(r=>r.json()).then(d=>{
    if(d.messages&&d.messages.length>0){
      d.messages.forEach(m=>renderMsg(m));
      lastId=d.last_id;
    }
    setConnected(true);
  }).catch(()=>{setConnected(false);});
}

function setConnected(v){
  statusDot.className='status-dot'+(v?'':' off');
  statusText.textContent=v?'已连接':'未连接';
}

pollNow();
polling=setInterval(pollNow,1500);
</script>
</body>
</html>"""

# ============================================================
# HTTP 服务器
# ============================================================
class RequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 静默 HTTP 日志

    def _send_response(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, data, code=200):
        self._send_response(code, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False))

    def _send_html(self, html):
        self._send_response(200, "text/html; charset=utf-8", html)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._send_html(PC_HTML)
        elif path == "/mobile":
            self._send_html(MOBILE_HTML)
        elif path == "/manifest.json":
            self._send_response(200, "application/manifest+json; charset=utf-8", MANIFEST_JSON)
        elif path == "/sw.js":
            self._send_response(200, "application/javascript; charset=utf-8", SW_JS)
        elif path == "/api/messages":
            qs = parse_qs(parsed.query)
            since_id = int(qs.get("since", ["0"])[0])
            messages = store.get_since(since_id)
            last_id = messages[-1]["id"] if messages else since_id
            self._send_json({"messages": messages, "last_id": last_id})
        elif path == "/api/status":
            uptime = int(time.time() - start_time)
            self._send_json({"ok": True, "uptime": uptime, "auto_paste": auto_paste_enabled})
        else:
            self._send_response(404, "text/plain", "Not Found")

    def do_POST(self):
        global auto_paste_enabled
        path = urlparse(self.path).path

        if path == "/api/send":
            body = self._read_body()
            text = body.get("text", "").strip()
            from_who = body.get("from", "unknown")
            if not text:
                self._send_json({"ok": False, "error": "empty text"}, 400)
                return
            msg = store.add(text, from_who)

            # 来自手机的消息：显示并自动粘贴
            if from_who == "phone":
                print(f"\n[手机 {msg['time']}] {text}")
                if auto_paste_enabled:
                    threading.Thread(target=paste_text, args=(text,), daemon=True).start()

            # 来自电脑的消息：在终端也显示
            if from_who == "pc":
                print(f"\n[电脑 {msg['time']}] -> 手机: {text}")

            self._send_json({"ok": True, "id": msg["id"]})

        elif path == "/api/delete":
            body = self._read_body()
            ids = body.get("ids", [])
            if ids:
                count = store.delete_many(ids)
                print(f"[系统] 删除了 {count} 条消息")
                self._send_json({"ok": True, "deleted": count})
            else:
                self._send_json({"ok": False, "error": "no ids"}, 400)

        elif path == "/api/clear":
            count = store.clear()
            print(f"[系统] 清空了 {count} 条消息")
            self._send_json({"ok": True, "cleared": count})

        elif path == "/api/config":
            body = self._read_body()
            if "auto_paste" in body:
                auto_paste_enabled = bool(body["auto_paste"])
                state = "开启" if auto_paste_enabled else "关闭"
                print(f"[系统] 自动粘贴已{state}")
            self._send_json({"ok": True, "auto_paste": auto_paste_enabled})

        else:
            self._send_response(404, "text/plain", "Not Found")


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        """覆盖 server_bind 避免 getfqdn 在非 ASCII 主机名上崩溃"""
        import socket
        if self.allow_reuse_address:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.server_address)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


# ============================================================
# ADB 设置
# ============================================================
def find_adb():
    """查找 ADB 可执行文件"""
    # 先检查 PATH
    try:
        result = subprocess.run(["adb", "version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return "adb"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 常见路径
    common_paths = [
        os.path.expanduser(r"~\AppData\Local\Android\Sdk\platform-tools\adb.exe"),
        r"C:\Android\platform-tools\adb.exe",
        r"C:\Program Files (x86)\Android\android-sdk\platform-tools\adb.exe",
    ]
    for p in common_paths:
        if os.path.isfile(p):
            return p

    return None


def setup_adb_reverse(port):
    """设置 ADB 反向端口转发"""
    adb = find_adb()
    if not adb:
        print("[!] 未找到 ADB，请手动执行:")
        print(f"    adb reverse tcp:{port} tcp:{port}")
        return False

    # 检查设备
    try:
        result = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=10)
        lines = result.stdout.strip().split("\n")
        devices = [l for l in lines[1:] if "device" in l and "unauthorized" not in l]
        if not devices:
            print("[!] 未检测到已授权的 Android 设备")
            print("    请确认: 1) USB 已连接  2) USB 调试已开启  3) 已授权此电脑")
            return False
    except Exception as e:
        print(f"[!] ADB 设备检查失败: {e}")
        return False

    # 设置反向端口转发
    try:
        result = subprocess.run(
            [adb, "reverse", f"tcp:{port}", f"tcp:{port}"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            print(f"[OK] ADB 反向端口转发已设置 (tcp:{port})")
            return True
        else:
            print(f"[!] ADB reverse 失败: {result.stderr.strip()}")
            return False
    except Exception as e:
        print(f"[!] ADB reverse 执行失败: {e}")
        return False


# ============================================================
# 主入口
# ============================================================
def main():
    print("=" * 50)
    print("  USB 文字传输工具")
    print("=" * 50)
    print()

    # ADB 设置
    setup_adb_reverse(PORT)
    print()

    # 启动服务器
    server = ThreadingHTTPServer(("127.0.0.1", PORT), RequestHandler)

    print(f"  服务器已启动: http://127.0.0.1:{PORT}")
    print()
    print(f"  手机端: 在手机浏览器打开 http://localhost:{PORT}/mobile")
    print(f"  电脑端: http://localhost:{PORT}")
    print()
    print("  提示: 在手机浏览器菜单中选择「添加到主屏幕」可当 APP 使用")
    print("  提示: 手机发送的文字会自动粘贴到电脑当前活跃窗口")
    print()
    print("  按 Ctrl+C 停止")
    print("=" * 50)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[系统] 正在停止服务器...")
        server.shutdown()
        print("[系统] 已停止")


if __name__ == "__main__":
    main()
