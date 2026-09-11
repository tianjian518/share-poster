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

from share_poster import (ShareParser, VERSION, generate,
                          GuangyaAccount, CloudShareFetcher,
                          collect_media_items, enrich_items_with_tmdb,
                          build_rename_plan, apply_rename_plan,
                          ensure_guangya_account, guangya_token_days_left,
                          guangya_login_interactive)

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
    mode = data.get("mode") or "long"
    if mode not in ("long", "poster", "none"):
        mode = "long"
    if not text:
        return jsonify({"ok": False, "error": "请先粘贴分享链接/文本"})

    res = generate(
        text,
        quality=(data.get("quality") or "").strip() or None,
        size=(data.get("size") or "").strip() or None,
        include_image=(mode != "none"),
        image_mode=mode,
        width=760,
        font_size=28,
        verbose=False,
        rename=bool(data.get("rename")),
    )
    if not res["ok"]:
        return jsonify({"ok": False, "error": res["error"]})

    # 选择前端要展示的主图：根据 mode 取对应 base64
    primary_b64 = ""
    primary_mime = "image/png"
    if mode == "long":
        primary_b64 = res.get("image_b64", "")
        primary_mime = "image/png"
    elif mode == "poster":
        primary_b64 = res.get("cover_b64") or res.get("poster_jpg_b64", "")
        primary_mime = "image/jpeg"
    # 复制按钮剪贴板需要 PNG，前端再用 canvas 转

    return jsonify({
        "ok": True,
        "text": res["text"],
        "title": res["title"],
        "cloud": res.get("cloud", ""),
        "manual": res.get("manual", False),
        "collection": res.get("collection", False),
        "mode": mode,
        "primary_b64": primary_b64,
        "primary_mime": primary_mime,
        "hint": _size_hint(res.get("text")),
        "rename": res.get("rename"),
    })


# ---------------- 光鸭云盘账号 ----------------
LOGIN_SESSIONS = {}        # phone -> {account, captcha_token, verification_id}
_SESSION_LOCK = threading.Lock()


@app.route("/api/account/status")
def api_account_status():
    acc = GuangyaAccount.from_config()
    if not acc.logged_in:
        return jsonify({"ok": True, "logged_in": False})
    name, days, err = "", guangya_token_days_left(acc), ""
    try:
        info = acc.user_info()
        d = info.get("data") or info
        name = d.get("nickname") or d.get("name") or d.get("phone") or ""
    except Exception as e:                             # noqa: BLE001
        err = str(e)[:120]
    return jsonify({"ok": True, "logged_in": True, "name": str(name),
                    "days": round(days, 1) if days is not None else None,
                    "warn": ("登录态校验失败：" + err) if err else ""})


@app.route("/api/account/sms/send", methods=["POST"])
def api_account_sms_send():
    """第 1 步：点「发送验证码」→ 真的把短信发出去"""
    data = request.get_json(force=True, silent=True) or {}
    phone = (data.get("phone") or "").strip()
    if not phone:
        return jsonify({"ok": False, "error": "请填手机号（含区号，如 +86 13800138000）"})
    acc = GuangyaAccount()
    try:
        st = acc.start_sms_login(phone)
    except Exception as e:                             # noqa: BLE001
        return jsonify({"ok": False, "error": f"发送失败：{str(e)[:180]}"})
    if not st.get("ok"):
        # 需要人机验证时，把 url 交给前端打开
        return jsonify({"ok": False, "need_captcha": True,
                        "url": st.get("url") or "",
                        "error": ("需要在浏览器完成人机验证后再试"
                                  if st.get("need_captcha")
                                  else f"发送失败：{str(st.get('raw'))[:180]}")})
    with _SESSION_LOCK:
        LOGIN_SESSIONS[phone] = {"account": acc, "sms": st}
    return jsonify({"ok": True, "msg": "验证码已发送，请查收短信"})


@app.route("/api/account/sms/verify", methods=["POST"])
def api_account_sms_verify():
    """第 2 步：填验证码 → 完成登录"""
    data = request.get_json(force=True, silent=True) or {}
    phone = (data.get("phone") or "").strip()
    code = (data.get("code") or "").strip()
    if not phone or not code:
        return jsonify({"ok": False, "error": "手机号和验证码都要填"})
    with _SESSION_LOCK:
        sess = LOGIN_SESSIONS.get(phone)
    if not sess:
        return jsonify({"ok": False,
                        "error": "还没发过验证码（或服务重启了），请先点「发送验证码」"})
    acc, st = sess["account"], sess["sms"]
    try:
        acc.finish_sms_login(phone, code, st["captcha_token"],
                             st["verification_id"])
    except Exception as e:                             # noqa: BLE001
        return jsonify({"ok": False, "error": f"登录失败：{str(e)[:180]}"})
    if not acc.logged_in:
        return jsonify({"ok": False, "error": "登录未返回 token，请重试"})
    acc.save_to_config()
    with _SESSION_LOCK:
        LOGIN_SESSIONS.pop(phone, None)
    name = ""
    try:
        info = acc.user_info()
        d = info.get("data") or info
        name = d.get("nickname") or d.get("name") or d.get("phone") or ""
    except Exception:                                  # noqa: BLE001
        pass
    return jsonify({"ok": True, "logged_in": True, "name": str(name)})


@app.route("/api/account/login/token", methods=["POST"])
def api_account_login_token():
    """手动贴 access_token / refresh_token"""
    data = request.get_json(force=True, silent=True) or {}
    token = (data.get("access_token") or "").strip()
    refresh = (data.get("refresh_token") or "").strip()
    if not token and not refresh:
        return jsonify({"ok": False, "error": "请填 access_token（或 refresh_token）"})
    acc = GuangyaAccount(access_token=token, refresh_token=refresh)
    if not acc.token and acc.refresh_token_value:
        try:
            acc.refresh_token()
        except Exception as e:                         # noqa: BLE001
            return jsonify({"ok": False, "error": f"用 refresh_token 换取失败：{str(e)[:180]}"})
    if not acc.logged_in:
        return jsonify({"ok": False, "error": "没有拿到有效 token"})
    name, warn = "", ""
    try:
        info = acc.user_info()
        d = info.get("data") or info
        name = d.get("nickname") or d.get("name") or d.get("phone") or ""
    except Exception as e:                             # noqa: BLE001
        warn = f"token 可能无效：{str(e)[:140]}"
    if warn:
        return jsonify({"ok": False, "error": warn})
    acc.save_to_config()
    return jsonify({"ok": True, "logged_in": True, "name": str(name)})


@app.route("/api/account/logout", methods=["POST"])
def api_account_logout():
    from share_poster import load_config, save_config
    cfg = load_config()
    cfg.pop("guangya", None)
    save_config(cfg)
    return jsonify({"ok": True, "logged_in": False})


# ---------------- 重命名 ----------------
@app.route("/api/rename/preview", methods=["POST"])
def api_rename_preview():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "请先粘贴分享链接"})
    meta = CloudShareFetcher().fetch(text)
    if not meta or not meta.get("title"):
        return jsonify({"ok": False, "error": "读取分享页失败，无法生成重命名预览"})
    items = collect_media_items(meta.get("deep_files"), meta.get("dir_nodes"))
    if any(not it.get("tmdb") for it in items):
        enrich_items_with_tmdb(items, verbose=False)
    plan = build_rename_plan(meta.get("deep_files"), meta.get("dir_nodes"),
                             items=items)
    return jsonify({"ok": True, "title": meta.get("title", ""),
                    "count": len(plan), "items": plan,
                    "dirs": sum(1 for x in plan if x.get("is_dir"))})


@app.route("/api/rename/apply", methods=["POST"])
def api_rename_apply():
    data = request.get_json(force=True, silent=True) or {}
    plan = data.get("plan") or []
    if not plan:
        return jsonify({"ok": False, "error": "没有待执行的重命名项"})
    acc = ensure_guangya_account(verbose=False)
    if acc is None:
        return jsonify({"ok": False, "error": "未登录光鸭云盘，请先在右上角登录"})
    # 以 fileId 定位，服务端不信前端传来的 new（防篡改）
    safe = [{"fileId": str(p.get("fileId")), "new": str(p.get("new") or "")}
            for p in plan if p.get("fileId") and p.get("new")]
    res = apply_rename_plan(acc, safe, dry_run=False)
    return jsonify({"ok": res["failed"] == 0, "changed": res["changed"],
                    "failed": res["failed"],
                    "items": [{"old": a.get("old"), "new": a.get("new"),
                               "status": a.get("status"),
                               "error": a.get("error", "")}
                              for a in res["items"]]})


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
  .acct{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--sub)}
  .acct b{color:var(--ink);font-size:12px}
  .modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;
    align-items:center;justify-content:center;z-index:200;padding:16px}
  .modal.show{display:flex}
  .modal .box{background:#fff;border-radius:14px;padding:22px;width:100%;
    max-width:420px;box-shadow:0 8px 30px rgba(0,0,0,.2)}
  .modal h3{font-size:16px;margin-bottom:4px}
  .modal .hint{font-size:12px;color:var(--sub);line-height:1.8;margin-bottom:14px}
  .modal input{width:100%;border:1px solid var(--line);border-radius:10px;
    padding:11px 12px;font-size:14px;background:#fafbfc;outline:none;margin-bottom:10px}
  .modal input:focus{border-color:var(--acc2)}
  .tabs{display:flex;gap:6px;margin-bottom:16px;background:#f1f3f5;padding:4px;
    border-radius:10px}
  .tabs button{flex:1;border:none;background:transparent;border-radius:8px;
    padding:8px;font-size:13px;cursor:pointer;color:var(--sub);font-weight:600}
  .tabs button.on{background:#fff;color:var(--ink);box-shadow:0 1px 3px rgba(0,0,0,.08)}
  .modal .row{margin-top:6px;justify-content:flex-end;gap:8px}
  .rlist{max-height:340px;overflow:auto;border:1px solid var(--line);
    border-radius:10px;margin-top:12px}
  .rlist .it{display:flex;gap:10px;padding:9px 12px;border-bottom:1px solid #f1f3f5;
    font-size:12.5px;line-height:1.6;align-items:flex-start}
  .rlist .it:last-child{border-bottom:none}
  .rlist .it input{margin-top:3px;flex:0 0 auto}
  .rlist .k{color:var(--sub)}
  .rlist .arrow{color:var(--acc);margin:0 6px}
  .rlist .n{color:var(--ink);font-weight:600}
  .chk{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ink);
    cursor:pointer;user-select:none}
  .chk input{width:16px;height:16px;accent-color:var(--acc)}
  .badge.ok{background:#eef7ef;color:var(--ok)}
  .badge.no{background:#f1f3f5;color:var(--sub)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">🎬</div>
    <div style="flex:1">
      <h1>影帖 · 影视分享帖生成器</h1>
      <p>粘贴网盘链接 → 自动生成发帖图文 → 一键复制去 QQ 频道粘贴</p>
    </div>
    <div class="acct">
      <span id="acct_txt">光鸭未登录</span>
      <button class="btn ghost" id="acct_btn"
        style="padding:7px 14px;font-size:12.5px">登录光鸭</button>
    </div>
  </header>

  <div class="cap" id="cap"></div>

  <div class="card">
    <label>分享链接 / 分享文本（自动识别网盘 + 片名，支持 {tmdb-id} 直取资料）</label>
    <textarea id="in" placeholder="例如：https://www.guangyapan.com/s/xxxxxxxxxxxxxxxxx_xxxxxxxxxxxxxxxx"></textarea>
    <p style="margin:6px 0 0;font-size:12px;opacity:.6">把链接发给自己就能拿到；别人发的分享也直接粘进来。生成的帖子只包含链接和片单，**不会**上传或泄露你自己的账号信息。</p>
    <div class="opts">
      <input id="quality" placeholder="质量覆盖（选填）如 2160P REMUX HEVC">
      <input id="size" placeholder="大小覆盖（选填）如 40.5GB">
    </div>
    <div class="row" style="margin-top:12px;padding-top:12px;border-top:1px dashed var(--line)">
      <label class="chk" title="把分享里的文件夹和视频改成 Emby 能识别的规范名">
        <input type="checkbox" id="do_rename">
        <span>发帖前先重命名（规范片名 + 画质 + TMDB ID，方便他人转存后入库）</span>
      </label>
      <button class="btn ghost" id="preview_rename"
        style="padding:8px 16px;font-size:13px">👀 预览重命名</button>
    </div>
    <div class="row">
      <span class="tips">画质/大小留空则按分享体积自动推断</span>
      <div style="display:flex;align-items:center;gap:10px">
        <select id="mode" style="border:1px solid var(--line);border-radius:10px;padding:9px 12px;font-size:13px;background:#fff;outline:none">
          <option value="long">长图(默认)</option>
          <option value="poster">仅海报/封面(&lt;500KB)</option>
          <option value="none">纯文本</option>
        </select>
        <button class="btn primary" id="go">✨ 生成帖子</button>
      </div>
    </div>
  </div>

  <div id="loading" class="hidden">
    <div class="spinner"></div>
    <div>正在识别网盘、读取分享页、抓取 TMDB 资料…</div>
  </div>

  <div id="result" class="hidden">
    <div class="card hidden" id="rename_report">
      <div class="row" style="margin-top:0">
        <label id="rename_title" style="margin:0;font-size:14px;font-weight:600;color:var(--ink)"></label>
      </div>
      <div class="rlist" id="rename_list"></div>
    </div>

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

    <div class="card" id="img_section">
      <div class="row" style="margin-top:0">
        <label style="margin:0;font-size:14px;font-weight:600;color:var(--ink)">图片（长图 / 海报·封面，自动 <500KB）</label>
        <button class="btn blue" id="copy_img">🖼 复制图片</button>
      </div>
      <img id="preview_img" class="preview" style="margin-top:14px" alt="预览">
      <div class="meta" style="margin-top:10px" id="img_hint">
        <b>长图模式</b>：复制图片后到 QQ 桌面版 <b>Ctrl+V</b> 粘贴为图<br>
        <b>海报模式</b>：点按钮复制到剪贴板（移动端会保存到下载目录，再长按图存相册）
      </div>
    </div>
  </div>

  <footer>生成结果仅保存在本机。海报/封面压缩到 500KB 以内，复制图片功能依赖浏览器 Clipboard API（localhost / HTTPS 安全上下文）。</footer>
</div>

<div class="modal" id="login_modal">
  <div class="box">
    <h3>登录光鸭云盘</h3>
    <div class="hint">登录后才能对你自己网盘里的文件重命名。登录态保存在本机，不会上传。</div>
    <div class="tabs">
      <button class="on" data-tab="sms">短信验证码</button>
      <button data-tab="token">粘贴 Token</button>
    </div>

    <div id="tab_sms">
      <input id="lg_phone" placeholder="手机号（含区号，如 +86 13800138000）">
      <div style="display:flex;gap:8px;align-items:flex-start">
        <input id="lg_code" placeholder="收到的验证码" style="flex:1;margin-bottom:10px">
        <button class="btn blue" id="lg_send_code"
          style="padding:11px 14px;font-size:13px;white-space:nowrap;flex:0 0 auto">
          发送验证码
        </button>
      </div>
      <div class="cap" id="lg_tip" style="margin-bottom:10px"></div>
      <div class="row">
        <button class="btn ghost" id="lg_cancel">取消</button>
        <button class="btn primary" id="lg_sms_go">登录</button>
      </div>
    </div>

    <div id="tab_token" class="hidden">
      <input id="lg_token" placeholder="access_token">
      <input id="lg_refresh" placeholder="refresh_token（选填，填了能自动续期）">
      <div class="cap" id="lg_tip2" style="margin-bottom:10px"></div>
      <div class="row">
        <button class="btn ghost" id="lg_cancel2">取消</button>
        <button class="btn primary" id="lg_token_go">保存登录态</button>
      </div>
    </div>
  </div>
</div>

<div class="modal" id="rename_modal">
  <div class="box" style="max-width:640px">
    <h3 id="rm_title">重命名预览</h3>
    <div class="hint" id="rm_sub">勾选要改的项目，确认后才会真正修改云端文件。</div>
    <div class="rlist" id="rm_list"></div>
    <div class="row">
      <button class="btn ghost" id="rm_cancel">取消</button>
      <button class="btn primary" id="rm_apply">✅ 确认重命名</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
let curKey = null;
let curMode = "long";

function toast(msg){const t=$('#toast');t.textContent=msg;t.classList.add('show');
  clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),2600);}

$('#go').onclick = async () => {
  const text = $('#in').value.trim();
  if(!text){toast('请先粘贴分享链接');return;}
  curMode = $('#mode').value || "long";
  $('#go').disabled = true;
  $('#loading').classList.remove('hidden');
  $('#result').classList.add('hidden');
  try{
    const resp = await fetch('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text, quality:$('#quality').value, size:$('#size').value,
        mode:curMode, rename:$('#do_rename').checked})});
    const d = await resp.json();
    if(!d.ok){toast(d.error||'生成失败');return;}
    curMode = d.mode;
    curKey = d.title;
    $('#out_text').textContent = d.text;
    let titleTxt = '🎬 ' + d.title + (d.cloud?('　·　来源 ' + d.cloud):'');
    if(d.collection) titleTxt += '　·　合集';
    if(d.manual)    titleTxt += '（自动资料缺失，已按标题生成）';
    $('#title_line').textContent = titleTxt;

    const img = $('#preview_img');
    if(d.primary_b64){
      img.src = 'data:' + d.primary_mime + ';base64,' + d.primary_b64;
    }else{
      img.removeAttribute('src');
    }
    // 显示与按钮提示
    const imgSection = $('#img_section');
    if(curMode === 'none') imgSection.classList.add('hidden'); else imgSection.classList.remove('hidden');
    const copyBtn = $('#copy_img');
    if(curMode === 'long') copyBtn.textContent = '🖼 复制图片';
    else if(curMode === 'poster'){
      copyBtn.textContent = canShareFiles() ? '🖼 复制海报' : '⬇ 保存海报';
    }else copyBtn.textContent = '图片';

    showRenameReport(d.rename);
    $('#result').classList.remove('hidden');
    $('#result').scrollIntoView({behavior:'smooth'});
  }catch(e){toast('请求失败：'+e);}
  finally{$('#go').disabled=false;$('#loading').classList.add('hidden');}
};

function canShareFiles(){return !!(navigator.canShare && navigator.share && /Android/i.test(navigator.userAgent));}

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

/* 复制图片：根据当前 mode 走不同路径
   - long：PNG 直接写剪贴板（QQ 桌面 Ctrl+V 贴图）
   - poster：把已显示的海报/JPG 用 canvas 转 PNG 后写剪贴板
   - none：按钮已隐藏 */
$('#copy_img').onclick = async () => {
  if(curMode === 'none') return;
  const img = $('#preview_img');
  if(!img || !img.src){ toast('还没有可复制的图片'); return; }

  // 桌面 + 能写 PNG 剪贴板 → 走 ClipboardItem
  const canClipPng = !!navigator.clipboard && !!window.ClipboardItem &&
                     !!navigator.clipboard.write &&
                     !/Android|iPhone|iPad|iPod/.test(navigator.userAgent);

  // poster 模式：先把显示图转为 PNG Blob（剪贴板要求 PNG）
  if(curMode === 'poster'){
    try{
      // 等图加载完
      if(!img.complete){ await new Promise(r => img.onload = r); }
      const c = document.createElement('canvas');
      c.width = img.naturalWidth; c.height = img.naturalHeight;
      c.getContext('2d').drawImage(img, 0, 0);
      const pngBlob = await new Promise(r => c.toBlob(r, 'image/png'));
      if(canClipPng){
        await navigator.clipboard.write([new ClipboardItem({'image/png': pngBlob})]);
        flash($('#copy_img'),'✅ 海报已复制');
        toast('海报已复制，去 QQ 桌面版 Ctrl+V 粘贴'); return;
      }
      // 移动或不支持：下载保存
      const url = URL.createObjectURL(pngBlob);
      const a = document.createElement('a');
      a.href = url; a.download = (curKey || 'poster') + '.png';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(()=>URL.revokeObjectURL(url), 4000);
      flash($('#copy_img'),'✅ 已保存');
      toast('海报已保存，可长按图→保存到相册，再到频道输入框 ⊕ 相册发送');
    }catch(e){ toast('复制失败，请长按图片保存'); }
    return;
  }

  // long 模式（与 v1.0.x 相同）
  if(canClipPng){
    try{
      const blob = await (await fetch(img.src)).blob();
      await navigator.clipboard.write([new ClipboardItem({'image/png': blob})]);
      flash($('#copy_img'),'✅ 图片已复制');
      toast('图片已在剪贴板，去 QQ 桌面版 Ctrl+V 粘贴');
      return;
    }catch(e){}
  }
  // 兜底：保存
  try{
    const blob = await (await fetch(img.src)).blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = (curKey || 'long_image') + '.png';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url), 4000);
    flash($('#copy_img'),'✅ 已保存');
    toast('长图已下载，可直接拖进 QQ 或右键复制图片');
  }catch(e){ toast('失败：请右键图片保存'); }
};

function flash(btn,msg,err){
  const old=btn.innerHTML;
  btn.innerHTML=msg;
  btn.classList.add(err?'ok':'ok');btn.classList.add(err?'blue2':'copy');
  setTimeout(()=>{btn.innerHTML=old;btn.classList.remove('ok','blue2','copy');},1800);
}

$('#in').value = ''; // 保持清爽

/* ==================== 光鸭云盘账号 ==================== */
let ACCOUNT = {logged_in:false};

async function refreshAccount(){
  try{
    const d = await (await fetch('/api/account/status')).json();
    ACCOUNT = d;
    if(d.logged_in){
      $('#acct_txt').textContent = '光鸭：' + (d.name || '已登录') +
        (d.days!==null && d.days!==undefined ? ('（token ' + d.days + ' 天）') : '');
      $('#acct_btn').textContent = '退出登录';
    }else{
      $('#acct_txt').textContent = '光鸭未登录';
      $('#acct_btn').textContent = '登录光鸭';
    }
  }catch(e){}
  return ACCOUNT;
}

$('#acct_btn').onclick = async () => {
  if(ACCOUNT.logged_in){
    await fetch('/api/account/logout',{method:'POST'});
    await refreshAccount();
    toast('已退出光鸭登录');
    return;
  }
  lgTip('');
  const t2 = $('#lg_tip2'); if(t2) t2.style.display = 'none';
  $('#login_modal').classList.add('show');
};
$('#lg_cancel').onclick = () => $('#login_modal').classList.remove('show');
$('#lg_cancel2').onclick = () => $('#login_modal').classList.remove('show');
$('#login_modal').onclick = e => { if(e.target.id === 'login_modal') e.currentTarget.classList.remove('show'); };

document.querySelectorAll('.tabs button').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('.tabs button').forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    const t = b.dataset.tab;
    $('#tab_sms').classList.toggle('hidden', t !== 'sms');
    $('#tab_token').classList.toggle('hidden', t !== 'token');
  };
});

let sendTimer = null;

function lgTip(msg, kind){
  const t = $('#lg_tip');
  if(!msg){ t.style.display = 'none'; return; }
  t.className = 'cap ' + (kind === 'bad' ? 'bad' : 'good');
  t.innerHTML = msg;
  t.style.display = 'block';
}

$('#lg_send_code').onclick = async () => {
  const phone = $('#lg_phone').value.trim();
  if(!phone){ toast('请先填手机号'); return; }
  const btn = $('#lg_send_code');
  btn.disabled = true;
  lgTip('正在发送…');
  try{
    const d = await (await fetch('/api/account/sms/send',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({phone})})).json();
    if(!d.ok){
      lgTip(d.error || '发送失败', 'bad');
      if(d.need_captcha && d.url){
        lgTip((d.error||'') + '<br><a href="' + d.url + '" target="_blank">点这里完成人机验证</a>', 'bad');
      }
      btn.disabled = false;
      return;
    }
    lgTip('✅ 验证码已发送，请查收短信');
    let n = 60;
    btn.textContent = n + 's';
    clearInterval(sendTimer);
    sendTimer = setInterval(() => {
      n--;
      if(n <= 0){ clearInterval(sendTimer); btn.disabled = false; btn.textContent = '发送验证码'; }
      else btn.textContent = n + 's';
    }, 1000);
  }catch(e){
    lgTip('请求失败：' + e, 'bad');
    btn.disabled = false;
  }
};

$('#lg_sms_go').onclick = async () => {
  const phone = $('#lg_phone').value.trim();
  const code  = $('#lg_code').value.trim();
  if(!phone){ toast('请填手机号'); return; }
  if(!code){ toast('请填收到的验证码'); return; }
  $('#lg_sms_go').disabled = true;
  lgTip('正在登录…');
  try{
    const d = await (await fetch('/api/account/sms/verify',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({phone, code})})).json();
    if(!d.ok){ lgTip(d.error || '登录失败', 'bad'); return; }
    $('#login_modal').classList.remove('show');
    await refreshAccount();
    toast('✅ 登录成功，可以重命名啦');
  }catch(e){ lgTip('请求失败：' + e, 'bad'); }
  finally{ $('#lg_sms_go').disabled = false; }
};

$('#lg_token_go').onclick = async () => {
  const tk = $('#lg_token').value.trim();
  const rf = $('#lg_refresh').value.trim();
  if(!tk && !rf){ toast('请填 access_token 或 refresh_token'); return; }
  $('#lg_token_go').disabled = true;
  try{
    const d = await (await fetch('/api/account/login/token',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({access_token:tk, refresh_token:rf})})).json();
    if(!d.ok){
      const t = $('#lg_tip2'); t.className = 'cap bad';
      t.innerHTML = d.error || '登录失败'; t.style.display = 'block';
      return;
    }
    $('#login_modal').classList.remove('show');
    await refreshAccount();
    toast('✅ 登录态已保存');
  }catch(e){ toast('请求失败：' + e); }
  finally{ $('#lg_token_go').disabled = false; }
};

/* ==================== 重命名 ==================== */
let RENAME_PLAN = [];

function renderPlan(items, container, selectable){
  container.innerHTML = '';
  items.forEach((p, i) => {
    const d = document.createElement('div');
    d.className = 'it';
    const kind = p.is_dir ? '📁' : '🎞';
    d.innerHTML = (selectable ? '<input type="checkbox" checked data-i="'+i+'">' : '') +
      '<div style="flex:1"><span class="k">' + kind + ' ' + esc(p.old) + '</span>' +
      '<span class="arrow">→</span><span class="n">' + esc(p.new) + '</span></div>';
    container.appendChild(d);
  });
}
function esc(t){ const d = document.createElement('div'); d.textContent = t == null ? '' : t; return d.innerHTML; }

$('#preview_rename').onclick = async () => {
  const text = $('#in').value.trim();
  if(!text){ toast('请先粘贴分享链接'); return; }
  $('#preview_rename').disabled = true;
  $('#loading').classList.remove('hidden');
  try{
    const d = await (await fetch('/api/rename/preview',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({text})})).json();
    if(!d.ok){ toast(d.error || '预览失败'); return; }
    if(!d.count){ toast('没有需要重命名的项目（名称已规范）'); return; }
    RENAME_PLAN = d.items;
    $('#rm_title').textContent = '重命名预览 · 共 ' + d.count + ' 项';
    $('#rm_sub').innerHTML = esc(d.title) + ' —— 共 ' + d.count +
      ' 项（📁 文件夹 ' + d.dirs + ' · 🎞 文件 ' + (d.count - d.dirs) + '）。' +
      '<br><b>确认后才会真正修改你网盘里的文件</b>，取消则不做任何改动。';
    renderPlan(RENAME_PLAN, $('#rm_list'), true);
    $('#rename_modal').classList.add('show');
  }catch(e){ toast('请求失败：' + e); }
  finally{ $('#preview_rename').disabled = false; $('#loading').classList.add('hidden'); }
};

$('#rm_cancel').onclick = () => $('#rename_modal').classList.remove('show');
$('#rename_modal').onclick = e => { if(e.target.id === 'rename_modal') e.currentTarget.classList.remove('show'); };

$('#rm_apply').onclick = async () => {
  const picked = [];
  $('#rm_list').querySelectorAll('input[type=checkbox]').forEach(c => {
    if(c.checked) picked.push(RENAME_PLAN[+c.dataset.i]);
  });
  if(!picked.length){ toast('没有勾选任何项目'); return; }
  if(!ACCOUNT.logged_in){
    const a = await refreshAccount();
    if(!a.logged_in){ toast('请先登录光鸭云盘'); $('#login_modal').classList.add('show'); return; }
  }
  if(!confirm('确定要重命名这 ' + picked.length + ' 项吗？\n此操作会直接修改你网盘里的文件。')) return;
  $('#rm_apply').disabled = true;
  $('#rm_apply').textContent = '⏳ 正在重命名…';
  try{
    const d = await (await fetch('/api/rename/apply',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({plan:picked})})).json();
    if(!d.ok && d.error){ toast(d.error); return; }
    $('#rm_title').textContent = '重命名完成 · 成功 ' + d.changed + ' · 失败 ' + d.failed;
    $('#rm_sub').innerHTML = d.failed ? '<b style="color:#c0392b">部分失败，见下方标注。</b>'
                                      : '全部改好了 ✅ 现在可以直接生成帖子。';
    renderPlan(d.items, $('#rm_list'), false);
    $('#rm_apply').textContent = '✅ 已完成';
    toast('重命名完成：成功 ' + d.changed + '，失败 ' + d.failed);
    $('#do_rename').checked = false;   // 已手动改过，生成时不必再改
  }catch(e){ toast('请求失败：' + e); }
  finally{ $('#rm_apply').disabled = false; }
};

/* 生成结果里的重命名报告 */
function showRenameReport(r){
  const box = $('#rename_report');
  if(!r){ box.classList.add('hidden'); return; }
  if(r.error){ box.classList.add('hidden'); toast(r.error); return; }
  $('#rename_title').textContent = '✏️ 重命名完成 · 成功 ' + r.changed +
    ' · 失败 ' + (r.failed || 0);
  renderPlan(r.items || [], $('#rename_list'), false);
  box.classList.remove('hidden');
}

refreshAccount();

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

  // 移动端：QQ 频道只能从相册发图，给出正确引导
  if (IS_MOBILE) {
    $('#copy_img').innerHTML = '⬇ 保存长图';
    cap.className = 'cap good';
    cap.innerHTML = '<b>📱 手机发 QQ 频道</b><br>① <b>文本</b>：点“复制文本”后，到频道输入框长按 → 粘贴；<br>② <b>长图</b>：<b>长按上图 → “保存图片”</b>（存入相册），再到频道输入框 ⊕ 相册 → 选择该图发送。<br><span style="color:#8a919f">QQ 频道不支持粘贴剪贴板图片，相册是必经一步。</span>';
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
