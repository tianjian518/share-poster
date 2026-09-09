#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
影帖（YingTie）· Web 版
--------------------------
用法:
    python3 webapp.py [--port 8321]
然后浏览器打开 http://127.0.0.1:8321

生成后：
  · “复制文本” → 去 QQ 频道 / 贴吧 / 微博 里 Ctrl+V 粘贴
  · “复制图片” → 把长图写进系统剪贴板，QQ 桌面版里 Ctrl+V 直接粘贴为图片
    （需要 Chrome/Edge 桌面版，访问 http://127.0.0.1 属安全上下文，可正常复制）
"""

import argparse
import base64
import io
import json
import threading
import webbrowser

from flask import Flask, jsonify, request, send_file

from share_poster import ShareParser, VERSION, generate

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

RESULT_CACHE = {}          # 存最近几次结果，供 /image/<key> 取图
_CACHE_LOCK = threading.Lock()


@app.route("/")
def index():
    return PAGE


@app.route("/api/generate", methods=["POST"])
def api_generate():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "请先粘贴分享链接/文本"})

    res = generate(
        text,
        quality=(data.get("quality") or "").strip() or None,
        size=(data.get("size") or "").strip() or None,
        include_image=True,
        width=760,
        font_size=28,
        verbose=False,
    )
    if not res["ok"]:
        return jsonify({"ok": False, "error": res["error"]})

    key = None
    if res.get("image_b64"):
        key = res["title"] or "share"
        with _CACHE_LOCK:
            RESULT_CACHE[key] = res["image_b64"]

    return jsonify({
        "ok": True,
        "text": res["text"],
        "title": res["title"],
        "cloud": res.get("cloud", ""),
        "manual": res.get("manual", False),
        "image_key": key,
        "hint": _size_hint(res.get("text")),
    })


@app.route("/image/<key>")
def image(key):
    with _CACHE_LOCK:
        b64 = RESULT_CACHE.get(key, "")
    if not b64:
        return "not found", 404
    return send_file(io.BytesIO(base64.b64decode(b64)),
                     mimetype="image/png")


def _size_hint(text):
    """从文本里挑出可提示用户修改的字段行"""
    lines = text.split("\n")
    for ln in lines:
        if ln.startswith("🎬") or ln.startswith("[影]"):
            return ln
    return ""


# 前端页面（单文件内联）
PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>影帖 · 影视分享帖生成器</title>
<style>
  :root{--bg:#f4f5f7;--card:#fff;--line:#e5e7eb;--ink:#1f2329;--sub:#8a919f;
        --acc:#e74c3c;--acc2:#ff8a65;--ok:#0f9d58;--radius:14px;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
       "Hiragino Sans GB","Microsoft YaHei",sans-serif;background:var(--bg);
       color:var(--ink);min-height:100vh}
  .wrap{max-width:860px;margin:0 auto;padding:28px 16px 60px}
  header{display:flex;align-items:center;gap:12px;margin-bottom:20px}
  header .logo{width:40px;height:40px;border-radius:10px;flex:0 0 40px;
    background:linear-gradient(135deg,var(--acc),var(--acc2));
    display:flex;align-items:center;justify-content:center;font-size:20px}
  h1{font-size:19px;font-weight:700}
  header p{font-size:12px;color:var(--sub);margin-top:2px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
        padding:20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
  label{font-size:13px;color:var(--sub);display:block;margin-bottom:8px}
  textarea{width:100%;min-height:74px;resize:vertical;border:1px solid var(--line);
    border-radius:10px;padding:12px;font-size:14px;line-height:1.6;
    font-family:inherit;background:#fafbfc;outline:none}
  textarea:focus{border-color:var(--acc2)}
  .opts{display:flex;gap:12px;margin-top:12px}
  .opts input{flex:1;border:1px solid var(--line);border-radius:10px;padding:10px 12px;
    font-size:13px;background:#fafbfc;outline:none}
  .row{display:flex;align-items:center;justify-content:space-between;
       gap:12px;margin-top:14px;flex-wrap:wrap}
  .btn{border:none;cursor:pointer;border-radius:10px;padding:11px 22px;
       font-size:14px;font-weight:600;transition:.15s;display:inline-flex;
       align-items:center;gap:6px}
  .btn:disabled{opacity:.55;cursor:not-allowed}
  .btn.primary{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff}
  .btn.ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
  .btn.copy{background:#eef7ef;color:var(--ok);border:1px solid #cbe8d2}
  .btn.blue{background:#eef2ff;color:#3b5bdb;border:1px solid #d5defa}
  .btn:hover:not(:disabled){transform:translateY(-1px)}
  .btn.copy.ok{background:var(--ok);color:#fff;border-color:var(--ok)}
  .btn.copy.ok.blue2{background:#3b5bdb;color:#fff;border-color:#3b5bdb}
  .hidden{display:none}
  #loading{text-align:center;padding:40px 0;color:var(--sub);font-size:14px}
  .spinner{width:26px;height:26px;border:3px solid #eee;border-top-color:var(--acc);
    border-radius:50%;margin:0 auto 12px;animation:spin .8s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  #result{margin-top:4px}
  .preview{width:100%;border-radius:12px;border:1px solid var(--line);display:block}
  .meta{font-size:12px;color:var(--sub);line-height:1.8}
  .badge{display:inline-block;background:#fef3f2;color:var(--acc);font-size:11px;
    border-radius:6px;padding:2px 8px;margin-left:6px}
  pre.out{white-space:pre-wrap;word-break:break-all;background:#fafbfc;
    border:1px solid var(--line);border-radius:10px;padding:14px;font-size:14px;
    line-height:1.9;max-height:300px;overflow:auto;font-family:inherit}
  .tips{font-size:12px;color:var(--sub);line-height:2}
  .tips b{color:var(--ink)}
  footer{text-align:center;font-size:12px;color:var(--sub);margin-top:8px}
  .toast{position:fixed;left:50%;bottom:36px;transform:translateX(-50%) translateY(20px);
    background:rgba(0,0,0,.82);color:#fff;font-size:13px;padding:10px 18px;
    border-radius:8px;opacity:0;transition:.25s;pointer-events:none;z-index:99}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  .cap{font-size:12px;border-radius:10px;padding:8px 12px;line-height:1.7;
       display:none;margin-bottom:16px}
  .cap.bad{background:#fef3f2;color:#c0392b;border:1px solid #f6c8c2}
  .cap.good{background:#eef7ef;color:var(--ok);border:1px solid #cbe8d2}
  .cap b{font-weight:700}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">🎬</div>
    <div>
      <h1>影帖 · 影视分享帖生成器</h1>
      <p>粘贴网盘链接 → 自动生成发帖图文 → 一键复制去 QQ 频道粘贴</p>
    </div>
  </header>

  <div class="cap" id="cap"></div>

  <div class="card">
    <label>分享链接 / 分享文本（自动识别网盘 + 片名，支持 {tmdb-id} 直取资料）</label>
    <textarea id="in" placeholder="例如：https://www.guangyapan.com/s/1944450213920591880_aeWXvQh3mcEVHL5Y"></textarea>
    <div class="opts">
      <input id="quality" placeholder="质量覆盖（选填）如 2160P REMUX HEVC">
      <input id="size" placeholder="大小覆盖（选填）如 40.5GB">
    </div>
    <div class="row">
      <span class="tips">画质/大小留空则按分享体积自动推断</span>
      <button class="btn primary" id="go">✨ 生成帖子</button>
    </div>
  </div>

  <div id="loading" class="hidden">
    <div class="spinner"></div>
    <div>正在识别网盘、读取分享页、抓取 TMDB 资料…</div>
  </div>

  <div id="result" class="hidden">
    <div class="card">
      <div class="row" style="margin-top:0">
        <label id="title_line" style="margin:0;font-size:14px;font-weight:600;color:var(--ink)"></label>
        <button class="btn copy" id="copy_text">📋 复制文本</button>
      </div>
      <pre class="out" id="out_text" style="margin-top:12px"></pre>
      <div class="meta" style="margin-top:10px">
        复制文本后，到 QQ 频道/输入框 <b>Ctrl+V</b> 即可粘贴全部文字内容
        <span class="badge">文本可直接粘贴</span>
      </div>
    </div>

    <div class="card">
      <div class="row" style="margin-top:0">
        <label style="margin:0;font-size:14px;font-weight:600;color:var(--ink)">发帖长图（封面 + 字段 + 下载地址）</label>
        <button class="btn blue" id="copy_img">🖼 复制图片</button>
      </div>
      <img id="preview_img" class="preview" style="margin-top:14px" alt="长图预览">
      <div class="meta" style="margin-top:10px">
        复制图片后，到 QQ 桌面版频道输入框里 <b>Ctrl+V</b> 直接粘贴为图片
        <span class="badge">需 Chrome/Edge + localhost</span>
      </div>
    </div>
  </div>

  <footer>生成结果仅保存在本机。图片粘贴功能依赖浏览器 Clipboard API（localhost 安全上下文）。</footer>
</div>

<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
let curKey = null;

function toast(msg){const t=$('#toast');t.textContent=msg;t.classList.add('show');
  clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),2600);}

$('#go').onclick = async () => {
  const text = $('#in').value.trim();
  if(!text){toast('请先粘贴分享链接');return;}
  $('#go').disabled = true;
  $('#loading').classList.remove('hidden');
  $('#result').classList.add('hidden');
  try{
    const resp = await fetch('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text, quality:$('#quality').value, size:$('#size').value})});
    const d = await resp.json();
    if(!d.ok){toast(d.error||'生成失败');return;}
    curKey = d.image_key;
    $('#out_text').textContent = d.text;
    $('#title_line').textContent = '🎬 ' + d.title + (d.cloud?('　·　来源 ' + d.cloud):'');
    if(d.manual){$('#title_line').textContent += '（自动资料缺失，已按标题生成）';}
    if(curKey){
      $('#preview_img').src = '/image/' + encodeURIComponent(curKey) + '?t=' + Date.now();
    }
    $('#result').classList.remove('hidden');
    $('#result').scrollIntoView({behavior:'smooth'});
  }catch(e){toast('请求失败：'+e);}
  finally{$('#go').disabled=false;$('#loading').classList.add('hidden');}
};

/* 复制文本：Clipboard API → 老 execCommand 双保险
   （公网 http 非安全上下文下 writeText 可能被拒，execCommand 仍可用） */
async function copyText(txt){
  try{ await navigator.clipboard.writeText(txt); return 'api'; }catch(e){}
  try{
    const ta=document.createElement('textarea');
    ta.value=txt; ta.style.position='fixed'; ta.style.top='-999px'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    const ok=document.execCommand('copy'); ta.remove();
    return ok ? 'legacy' : '';
  }catch(e){ return ''; }
}

$('#copy_text').onclick = async () => {
  const txt = $('#out_text').textContent;
  const r = await copyText(txt);
  if(r){
    flash($('#copy_text'),'✅ 已复制文本');
    toast('文本已复制，去 QQ 频道 Ctrl+V 粘贴即可');
  }else{
    const range=document.createRange();range.selectNode($('#out_text'));
    const sel=getSelection();sel.removeAllRanges();sel.addRange(range);
    toast('自动复制被浏览器拦截，请直接 Ctrl+C');
  }
};

/* 图片出口：桌面=复制到剪贴板；Android=系统分享面板(可选QQ直达)；其余=下载 */
$('#copy_img').onclick = async () => {
  const img = $('#preview_img');
  if(!curKey){toast('还没有可分享的图片');return;}
  let blob;
  try{ blob = await (await fetch(img.src)).blob(); }catch(e){ toast('获取图片失败'); return; }

  const canClip = !!navigator.clipboard && !!window.ClipboardItem && !!navigator.clipboard.write;
  const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
  const isAndroid = /Android/.test(navigator.userAgent);

  // 1) 桌面/支持 ClipboardItem：写入剪贴板（QQ 桌面 Ctrl+V 贴图）
  if(canClip && !isAndroid && !isIOS){
    try{
      await navigator.clipboard.write([new ClipboardItem({'image/png': blob})]);
      flash($('#copy_img'),'✅ 图片已复制');
      toast('图片已在剪贴板，去 QQ 桌面版 Ctrl+V 粘贴');
      return;
    }catch(e){ /* 落到下面兜底 */ }
  }

  // 2) 移动端优先走系统分享面板（可直接选 QQ 发送）
  if(navigator.canShare && navigator.share){
    const file = new File([blob], 'yingtie_share.png', {type:'image/png'});
    try{
      await navigator.share({files:[file], title:curKey});
      return; // 分享面板已打开
    }catch(e){ if(e && e.name==='AbortError') return; /* 用户取消不算错 */ }
  }

  // 3) 最终兜底：下载 PNG（电脑可另存/拖入 QQ，手机可保存相册再发）
  try{
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = curKey + '.png';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url), 4000);
    flash($('#copy_img'),'✅ 长图已下载');
    toast((isAndroid||isIOS) ? '已开始下载长图，保存后到 QQ 里发送图片即可'
                              : '已下载长图，可直接拖进 QQ 或右键复制');
  }catch(e){ toast('分享失败：请长按图片保存/分享'); }
};

function flash(btn,msg,err){
  const old=btn.innerHTML;
  btn.innerHTML=msg;
  btn.classList.add(err?'ok':'ok');btn.classList.add(err?'blue2':'copy');
  setTimeout(()=>{btn.innerHTML=old;btn.classList.remove('ok','blue2','copy');},1800);
}

$('#in').value = ''; // 保持清爽

/* 移动端判断：用于能力提示与按钮文案 */
const IS_MOBILE = /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);

/* 浏览器能力自检：决定能不能"复制图片到剪贴板" */
(function capabilityCheck(){
  const cap = $('#cap');
  const secure = (location.protocol === 'https:') ||
                 (location.protocol === 'http:' &&
                  (location.hostname === '127.0.0.1' || location.hostname === 'localhost'));
  const canAPI = !!navigator.clipboard && !!window.ClipboardItem && !!navigator.clipboard.write;
  const ua = navigator.userAgent;
  const m = ua.match(/Chrom(e|ium)\/(\d+)/) || ua.match(/CriOS\/(\d+)/);
  const ver = m ? parseInt(m[2], 10) : 0;
  const legacy = !!document.execCommand;

  // 移动端按钮文案与提示
  if (IS_MOBILE) {
    $('#copy_img').innerHTML = IS_MOBILE && /Android/.test(navigator.userAgent)
      ? '📤 分享长图' : '⬇ 保存长图';
    cap.className = 'cap good';
    cap.innerHTML = '<b>✔ 手机模式</b> —— 点“' + $('#copy_img').textContent.trim() + '”可把长图发到 QQ（Android 走系统分享面板可直接选 QQ）；文本点“复制文本”后到 QQ 输入框粘贴。';
    cap.style.display = 'block';
    return;
  }

  if (canAPI && secure) {
    cap.className = 'cap good';
    cap.innerHTML = '<b>✔ 当前环境支持“复制图片”</b> —— 生成后可点“复制图片”，再到 QQ 桌面版里 Ctrl+V 粘贴。';
  } else if (ver && ver < 97) {
    cap.className = 'cap bad';
    cap.innerHTML = '<b>⚠ 浏览器内核版本偏低（Chromium ' + ver + '）</b> —— “复制图片”需要 Chromium 97+，请改用 <b>Chrome / Edge</b>。<br>' +
      '文本复制' + (legacy ? '仍可用' : '可能受限') + '；图片可改用“右键 → 复制图片/另存为”。';
  } else if (!secure) {
    cap.className = 'cap bad';
    cap.innerHTML = '<b>⚠ 当前是公网 http 访问（非安全上下文），浏览器禁止站点把图片写入剪贴板</b>。<br>' +
      '解决：① 给部署配 <b>HTTPS</b>（推荐，见部署文档），即可一键复制图片；' +
      '② 或复制文本后，在图上 <b>右键 → “复制图片”</b>，再到 QQ 里 Ctrl+V（浏览器原生能力，不依赖本站）。<br>' +
      '（文本复制不受影响，已做兼容处理）';
  } else {
    cap.className = 'cap bad';
    cap.innerHTML = '<b>⚠ 当前浏览器不支持剪贴板图片接口</b> —— 建议换 Chrome / Edge 97+ 打开。';
  }
  cap.style.display = 'block';
})();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=f"影帖（YingTie）v{VERSION} · Web 版")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8321)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--version", "-V", action="version", version=f"影帖（YingTie）{VERSION}")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}"
    print("=" * 50)
    print(f"  影帖（YingTie）v{VERSION} · Web 版")
    print(f"  打开浏览器访问: {url}")
    print("  提示: 复制图片功能需 Chrome/Edge 桌面版")
    print("  Ctrl+C 退出")
    print("=" * 50)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
