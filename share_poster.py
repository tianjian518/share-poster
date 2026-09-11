#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
影帖（YingTie）—— 影视分享帖生成器
----------------
功能：
  1. 从一段分享文本里识别常见网盘（百度 / 夸克 / 阿里云 / 迅雷 / 123 / 城通等），
     自动抽出链接和提取码
  2. 通过 TMDB（v3 API）搜索影视，抓取片名、年份、集数、主要演员、简介、海报
  3. 按图示的格式（名称 / 质量 / 集数 / 主要人物 / 简介 / 下载地址）生成帖子文本
  4. 可选：把海报和文字排版合成一张可发帖的长图

用法：
  交互式：
    python3 share_poster.py
  一行命令：
    python3 share_poster.py -t "乡村爱情" -k <TMDB_API_KEY> \\
        "https://pan.baidu.com/s/xxxxx 提取码:abcd"

依赖：
  requests, Pillow
  pip3 install requests pillow
"""

import argparse
import io
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.stderr.write("缺少 requests，请先执行: pip3 install requests\n")
    sys.exit(1)

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.stderr.write("缺少 Pillow，请先执行: pip3 install Pillow\n")
    sys.exit(1)


# --- 常量 ---------------------------------------------------------------
VERSION = "1.1.3"

TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p"

# 目录递归遍历上限（合集可能有几十上百个子目录）
MAX_WALK_DEPTH = 8        # 最大层数
MAX_WALK_NODES = 1200     # 最多展开多少个节点
MAX_WALK_FILES = 600      # 最多收集多少个文件
WALK_WORKERS = 6          # 并发列目录线程数

# 常见网盘识别规则
CLOUD_PATTERNS = [
    ("百度网盘",  r"https?://pan\.baidu\.com/s/[\w-]+",                     r"(?:提取码|密码)[::\s]*([A-Za-z0-9]{4})"),
    ("夸克网盘",  r"https?://pan\.quark\.cn/s/[\w-]+",                       r"(?:提取码|密码)[::\s]*([A-Za-z0-9]{4,})"),
    ("阿里云盘",  r"https?://www\.alipan\.com/s/[\w-]+",                     r"(?:提取码|密码|访问码)[::\s]*([A-Za-z0-9]{4,})"),
    ("迅雷云盘",  r"https?://pan\.xunlei\.com/s/[\w-]+",                    r"(?:提取码|密码|访问码)[::\s]*([A-Za-z0-9]{4,})"),
    ("123 云盘",  r"https?://(?:www\.)?123pan\.com/s/[\w-]+",                r"(?:提取码|密码|访问码)[::\s]*([A-Za-z0-9]{4,})"),
    ("城通网盘",  r"https?://(?:www\.)?ctfile\.com/\w+/[\w-]+",              None),
    ("蓝奏云",    r"https?://[\w-]+\.lanzou[a-z]\.com/[\w-]+",               r"(?:提取码|密码)[::\s]*([A-Za-z0-9]{4,})"),
    ("奶牛快传",  r"https?://cowtransfer\.com/s/[\w-]+",                     r"(?:提取码|密码|口令)[::\s]*([A-Za-z0-9]{4,})"),
    ("光鸭云盘",  r"https?://(?:www\.)?guangyapan\.com/s/[\w-]+",            None),
    ("微云",      r"https?://share\.weiyun\.com/[\w-]+",                    None),
    ("OneDrive",  r"https?://[\w-]+\.sharepoint\.com/[^\s]+",                None),
    ("Google",    r"https?://drive\.google\.com/[\w/=+\-]+",                None),
]

# 中文字体候选（按顺序尝试）
FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/arphic/ukai.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]

# color emoji 字体候选（暂未启用——长图版本用 ASCII 图标，确保跨平台稳定渲染）
EMOJI_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto-emoji/NotoColorEmoji.ttf",
    "/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "C:/Windows/Fonts/seguiemj.ttf",
]

EMOJI_FONT = None
for _p in EMOJI_FONT_CANDIDATES:
    if Path(_p).exists():
        EMOJI_FONT = _p
        break


def emoji_font():
    """加载 color emoji 字体（PIL 加载 CBDT 需 size=109 才能渲染，再缩放）。
    当前 PosterMaker 默认不调用，预留给想要「原样 emoji 长图」的用户扩展。
    """
    if not EMOJI_FONT:
        return None
    try:
        return ImageFont.truetype(EMOJI_FONT, size=109)
    except Exception:
        return None


# --- 网盘解析 -----------------------------------------------------------
class ShareParser:
    @staticmethod
    def parse(text):
        """从一段文本中识别出 (网盘, 链接, 提取码)"""
        if not text:
            return None

        for name, url_re, code_re in CLOUD_PATTERNS:
            url = re.search(url_re, text)
            if not url:
                continue
            code = None
            if code_re:
                m = re.search(code_re, text)
                if m:
                    code = m.group(1)
            return {"cloud": name, "url": url.group(0), "code": code}

        # 兜底：任意 /s/ 形态的链接
        generic = re.search(r"https?://[\w\.\-]+/s/[\w\-]+", text)
        if generic:
            code = re.search(r"(?:提取码|密码|访问码)[::\s]*([A-Za-z0-9]{2,})", text)
            return {
                "cloud": "未知网盘",
                "url": generic.group(0),
                "code": code.group(1) if code else None,
            }
        return None


# --- TMDB 客户端 --------------------------------------------------------
class TMDBClient:
    def __init__(self, api_key, session=None):
        if not api_key:
            raise ValueError("需要提供 TMDB API Key (v3)")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "share-poster/1.0")

    # 搜索 ----------------------------------------------------------------
    def search(self, query, media_type="multi", year=None, language="zh-CN"):
        url = f"{TMDB_API_BASE}/search/{media_type}"
        params = {
            "api_key": self.api_key,
            "query": query,
            "language": language,
            "include_adult": "false",
        }
        if media_type == "movie" and year:
            params["year"] = year
        if media_type == "tv" and year:
            params["first_air_date_year"] = year

        try:
            r = self.session.get(url, params=params, timeout=12)
            r.raise_for_status()
            return r.json().get("results", [])
        except Exception as e:
            sys.stderr.write(f"[TMDB 搜索失败] {e}\n")
            return []

    # 详情（含 credits） ---------------------------------------------------
    def detail(self, media_type, media_id, language="zh-CN"):
        url = f"{TMDB_API_BASE}/{media_type}/{media_id}"
        params = {
            "api_key": self.api_key,
            "language": language,
            "append_to_response": "credits,images",
            "include_image_language": "zh-CN,en,null",
        }
        try:
            r = self.session.get(url, params=params, timeout=12)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            sys.stderr.write(f"[TMDB 详情失败] {e}\n")
            return None

    # 下载图片 ------------------------------------------------------------
    def download_image(self, path, size="w500"):
        if not path:
            return None
        url = f"{TMDB_IMG_BASE}/{size}{path}"
        try:
            r = self.session.get(url, timeout=20)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content))
            img.load()
            return img
        except Exception as e:
            sys.stderr.write(f"[图片下载失败 {url}] {e}\n")
            return None


# --- 分享页抓取（无头请求，拿网盘分享标题 / 文件大小 / TMDB ID） -----
#
# 部分网盘分享页是 SPA（如光鸭云盘），但其后端接口可以直接用普通 HTTP
# 请求调用，无需浏览器。各网盘的适配器都实现同一个协议：
#   fetch(text) -> {cloud, url, code, title, size_bytes, file_count}
# title 通常是分享者起的文件名，常形如：
#   《片名 (年份) {tmdb-123456}》  或  片名.2026.1080P...
#


class CloudShareFetcher:
    """尝试从分享链接页面抓出资源标题/大小。抓不到返回 None。"""

    # 光鸭云盘 API 适配（纯 JSON 接口）
    GUANGYA_API = "https://api.guangyapan.com/userres/v1"

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", self.UA)
        self.session.headers.setdefault("Referer", "https://www.guangyapan.com/")
        self.session.headers.setdefault("Content-Type", "application/json")

    def fetch(self, text):
        """text 为分享文本；内部按域名分发。失败返回 None"""
        share = ShareParser.parse(text)
        if not share:
            return None
        if "guangyapan.com" in share["url"]:
            try:
                return self._fetch_guangya(share)
            except Exception as e:
                sys.stderr.write(f"[光鸭云盘分享页抓取失败] {e}\n")
                return None
        # 未来可在此扩展更多网盘的适配器（百度/夸克等一般需验证码，暂不支持）
        return None

    def _post_json(self, path, payload):
        r = self.session.post(f"{self.GUANGYA_API}/{path}", json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("msg") != "success" or "data" not in data:
            raise RuntimeError(f"接口返回异常: {str(data)[:200]}")
        return data["data"]

    # --- 目录遍历 ------------------------------------------------------
    # 光鸭接口里 dirType 不可靠（视频文件的 dirType 也可能是 1），
    # 真正能区分文件的是：文件节点带 ext / fileSize / mineType，且 resType=1；
    # 目录节点 resType=2 且没有这些字段。
    @staticmethod
    def _is_file(node):
        if node.get("ext") or node.get("fileSize") or node.get("mineType"):
            return True
        return node.get("resType") == 1

    def _list_dir(self, token, parent_id, retries=2):
        """列出某目录的子节点，失败自动重试（分享接口偶发超时/限流）"""
        last = None
        for _ in range(retries + 1):
            try:
                data = self._post_json("get_share_page_files_list", {
                    "pageSize": 100, "accessToken": token,
                    "orderBy": 0, "sortType": 0, "parentId": parent_id or "",
                })
                return data.get("list") or []
            except Exception as e:      # noqa: BLE001
                last = e
                time.sleep(0.3)
        raise last

    def _walk(self, token, roots, max_depth=MAX_WALK_DEPTH,
              max_nodes=MAX_WALK_NODES, max_files=MAX_WALK_FILES,
              workers=WALK_WORKERS):
        """层序（BFS）+ 并发遍历分享目录树。

        返回 (leaves, dirs)：
          leaves —— 文件节点，附加父目录解析出的片名/年份/tmdb
                    （_self_* 自身解析，_dir_* 最近一层「像片名」的父目录解析）
          dirs   —— 目录节点（含顶层），用于文件层拿不到时的兜底识别
        """
        from concurrent.futures import ThreadPoolExecutor

        def with_meta(node, pt=("", None, None)):
            n = dict(node)
            name = node.get("fileName") or ""
            t, y, tm = parse_share_title(name)
            n["_self_title"], n["_self_year"], n["_self_tmdb"] = t, y, tm
            # 父链信息：自身解析不出年份/tmdb 时继承父目录
            n["_dir_title"], n["_dir_year"], n["_dir_tmdb"] = pt
            return n

        level, dirs, leaves = [], [], []
        for f in roots or []:
            n = with_meta(f)
            level.append(n)
            if not self._is_file(n):
                dirs.append(n)
            else:
                leaves.append(n)

        # 注意：seen 只记录「已经展开过」的目录，顶层目录本身还没展开
        seen = set()
        for _depth in range(max_depth):
            pend = [n for n in level
                    if (not self._is_file(n)) and n.get("fileId")
                    and n.get("fileId") not in seen]
            if not pend:
                break
            for n in pend:
                seen.add(n.get("fileId"))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                children = list(ex.map(
                    lambda n: self._list_dir(token, n.get("fileId")), pend))

            nxt = []
            for parent, kids in zip(pend, children):
                pname = parent.get("fileName") or ""
                pt, py, ptm = parse_share_title(pname)
                # 父目录名本身不像一部片（如「速度与激情 合集」「MP4&MKV」）时，
                # 不要把合集名当成子文件的片名，改为继承更上层的有效身份
                if not looks_like_single_title(pname):
                    pt = parent.get("_dir_title")
                    py = parent.get("_dir_year")
                    ptm = parent.get("_dir_tmdb")
                for c in kids:
                    n = with_meta(c, (pt, py, ptm))
                    if self._is_file(n):
                        leaves.append(n)
                    else:
                        dirs.append(n)
                        nxt.append(n)
            level = nxt
            if len(leaves) >= max_files or len(dirs) + len(leaves) >= max_nodes:
                break
        return leaves, dirs

    def _fetch_guangya(self, share):
        """光鸭云盘：shareId 在 /s/<shareId> 里"""
        share_id = share["url"].rsplit("/s/", 1)[-1].strip("/")
        if not share_id:
            raise RuntimeError("无法从 URL 提取 shareId")

        info = self._post_json("get_share_summary", {"shareId": share_id})
        title = (info.get("title") or "").strip()
        if not title:
            raise RuntimeError("分享标题为空")

        result = dict(share)
        result["title"] = title
        result["size_bytes"] = info.get("totalFileSize") or 0
        result["file_count"] = info.get("totalFileNum") or 0
        result["share_id"] = share_id
        result["files"] = []      # 顶层
        result["deep_files"] = []  # 递归展开后的叶子文件（视频/资源文件）
        result["dir_nodes"] = []   # 递归展开后的目录节点

        # 递归列出目录：分享里常是「合集文件夹」套多层子文件夹
        try:
            token = self._post_json("get_share_access_token", {"shareId": share_id})["accessToken"]
            root = self._list_dir(token, "")
            result["files"] = root
            leaves, dirs = self._walk(token, root)
            result["deep_files"] = leaves
            result["dir_nodes"] = dirs
        except Exception as e:      # noqa: BLE001
            sys.stderr.write(f"[目录遍历失败] {e}\n")
            result["files"] = result.get("files") or []
        return result


# --- TMDB 免 Key 网页抓取 ---------------------------------------------
# TMDB 官网公开页面包含结构化数据（og: 标签 + JSON-LD），无需 API Key。
# 注意：TMDB 网页语言由 Accept-Language 头决定（与访问者 IP/geo 无关），
# 必须显式带中文头，否则海外服务器会拿到英文页面。
class TMDBWebClient:
    BASE = "https://www.themoviedb.org"

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        # 关键：强制中文内容（简体优先，其次繁体），避免海外 IP 返回英文
        self.session.headers.setdefault(
            "Accept-Language",
            "zh-CN,zh-TW;q=0.9,zh;q=0.8,en;q=0.3",
        )

    # 工具 --------------------------------------------------------------
    @staticmethod
    def _html_meta(html, prop):
        m = re.search(r'<meta property="og:%s" content="([^"]*)"' % re.escape(prop), html)
        return m.group(1) if m else None

    # TMDB 网页端对无 key 的抓取有频控（高并发会 429），做全局节流 + 退避
    _throttle_lock = None
    _last_req = [0.0]
    MIN_INTERVAL = 0.35                          # 最小请求间隔（秒）

    def _fetch_page(self, url, retry=3):
        import threading as _th
        import time as _t
        if TMDBWebClient._throttle_lock is None:
            TMDBWebClient._throttle_lock = _th.Lock()
        last = None
        for attempt in range(retry + 1):
            with TMDBWebClient._throttle_lock:
                wait = TMDBWebClient.MIN_INTERVAL - (_t.time() - TMDBWebClient._last_req[0])
                if wait > 0:
                    _t.sleep(wait)
                TMDBWebClient._last_req[0] = _t.time()
            try:
                r = self.session.get(url, timeout=20)
                if r.status_code == 429:
                    # 退避后重试（1.5s, 3s, 4.5s…）
                    _t.sleep(1.5 * (attempt + 1))
                    last = RuntimeError("429 Too Many Requests")
                    continue
                r.raise_for_status()
                return r.text
            except Exception as e:               # noqa: BLE001
                last = e
                if "429" in str(e):
                    _t.sleep(1.5 * (attempt + 1))
        raise last

    @staticmethod
    def _clean_title(t):
        """去掉 ' — The Movie Database (TMDB)' 之类的尾巴，拆出 (年份)"""
        t = re.sub(r"\s*[—-]\s*(The Movie Database|TMDB).*$", "", t).strip()
        return t

    @staticmethod
    def _year_from_title(t):
        m = re.search(r"\((\d{4})\)", t)
        return m.group(1) if m else ""

    @staticmethod
    def _has_cjk(s):
        return bool(re.search(r"[\u4e00-\u9fff]", s or ""))

    def _cast_from_html(self, html, limit=10):
        """从 id='cast' 区块抓演员中文名。

        只认 <a href="/person/数字..."> 节点里的头像 alt，避免把
        “奖项 / 导演 / 预告片” 之类的界面文字当演员抓进来。
        """
        seg = html
        idx = html.find('id="cast"')
        if idx >= 0:
            seg = html[idx: idx + 400000]
        names, seen = [], set()
        # 方案A：person 链接卡片内的 alt
        for m in re.finditer(
            r'<a[^>]+href="/person/\d+[^"]*"[^>]*>'
            r'(?:(?!</a>).)*?alt="([^"]{1,50})"',
            seg, re.S,
        ):
            a = m.group(1).strip()
            if a and a not in seen and not a.startswith("${"):
                seen.add(a)
                names.append(a)
            if len(names) >= limit:
                break
        # 方案B（回退）：cast 区块全部 alt，但过滤常见界面词
        if not names:
            noise = {"奖项", "导演", "编剧", "演员", "预告片", "海报", "剧照",
                     "The Movie Database (TMDB)", "Poster", "Backdrop", "Video",
                     "Search", "Profile", "登录", "注册"}
            for a in re.findall(r'alt="([^"]{1,40})"', seg):
                a = a.strip()
                if (a and a not in seen and a not in noise
                        and not a.startswith("${") and len(a) <= 30
                        and not re.match(r"^[a-zA-Z0-9]{1,3}$", a)):
                    seen.add(a)
                    names.append(a)
                if len(names) >= limit:
                    break
        return names[:limit]
        # 上面的 alt 可能混入站点图标名，用人物链接二次校验后取靠前结果
        cast = []
        for n in names:
            if n in {"The Movie Database (TMDB)", "Poster", "Backdrop"}:
                continue
            cast.append(n)
        return cast[:limit]

    def detail_by_id(self, tmdb_id):
        """按 TMDB ID 抓取中文资料（movie/tv 自动探测）。

        语言由会话的 Accept-Language: zh-CN 头保证（与服务器 IP 无关）。
        若简中缺数据落到繁体/英文，标题含 CJK 即视为中文命中。
        """
        if not str(tmdb_id).isdigit():
            return None
        last_info = None
        for media in ("movie", "tv"):
            try:
                html = self._fetch_page(f"{self.BASE}/{media}/{tmdb_id}")
            except Exception:
                continue
            info = self._parse_detail_html(html, tmdb_id)
            if not info:
                continue
            poster_url = self._html_meta(html, "image")
            # 英文兜底结果先记下；中文命中则立刻返回
            if self._has_cjk(info.get("title", "")):
                return {"type": media, "info": info, "poster_url": poster_url}
            last_info = {"type": media, "info": info, "poster_url": poster_url}
        # 完全没有中文资料时，返回最后的英文结果（原题可用）
        return last_info

    def search_by_name(self, query, year=None):
        """无 Key：抓 TMDB 站内搜索页（服务端渲染），解析候选 (title, id, type)。
        返回列表 [{title,id,type,year,date,overview}]

        注意：新版 TMDB 搜索页结果链接为 `/movie/<id>-<english-slug>`，
        片名在 slug 里（不是锚文本，锚文本是空的图卡）。旧版正则
        `href="/movie/(\\d+)"[^>]*>标题<` 已匹配不到任何结果。
        """
        url = f"{self.BASE}/search?query={requests.utils.quote(query)}"
        try:
            html = self._fetch_page(url)
        except Exception as e:
            sys.stderr.write(f"[TMDB 搜索失败] {e}\n")
            return []
        cands = []
        # 结果卡片：<a href="/movie/73-american-history-x" ...>
        for m in re.finditer(r'href="/(movie|tv)/(\d+)-([a-z0-9\-]{0,120})"', html):
            mt, mid, slug = m.group(1), m.group(2), m.group(3)
            t = slug.replace("-", " ").strip()
            if not t:
                continue
            item = {"id": int(mid), "type": mt, "title": t}
            if item not in cands:
                cands.append(item)
            if len(cands) >= 12:
                break
        return cands

    def search_tmdb(self, query, year=None):
        """TMDB 搜索 + 详情补全：拿到 id 后再抓详情页，返回带中文名/年份/海报的候选。
        返回 [{id,type,title,year,poster_url,info}]"""
        out = []
        for c in self.search_by_name(query, year=year):
            got = self.detail_by_id(c["id"])
            if not got:
                continue
            info = got.get("info") or {}
            out.append({
                "id": c["id"], "type": got.get("type", c["type"]),
                "title": info.get("title") or c["title"],
                "year": info.get("year") or "",
                "poster_url": got.get("poster_url") or "",
                "info": info,
                "_query_title": c["title"],
            })
        return out

    def _parse_detail_html(self, html, tmdb_id):
        """从 TMDB 详情页 HTML 解析出 info dict（字段兼容 PostGenerator）"""
        import html as _h
        m_title = re.search(r"<title>(.*?)</title>", html)
        title = _h.unescape(m_title.group(1)) if m_title else ""
        title = self._clean_title(title)
        title = _h.unescape(title)
        if not title:
            return None
        year = self._year_from_title(title)
        title = re.sub(r"\s*\(\d{4}\)\s*$", "", title).strip()

        desc = self._html_meta(html, "description") or ""
        desc = _h.unescape(desc)
        # 简介末尾的“……(来源 TMDB)”之类的提示去掉
        desc = re.sub(r"\s*…?\s*$", "", desc).strip()

        cast = self._cast_from_html(html, limit=12)
        # 去掉混入的片名本身 / 站点名
        cast = [n for n in cast
                if n not in (title, self._clean_title(_h.unescape(
                    self._html_meta(html, "title") or "")) or title)
                and n not in {"The Movie Database (TMDB)", "Poster", "Backdrop",
                              "Video", "Search", "Profile"}]

        info = {
            "title": title,
            "name": title,
            "_tmdb_id": int(tmdb_id),
        }
        if year:
            info["release_date"] = f"{year}-01-01"
            info["first_air_date"] = ""
            info["year"] = year
        if desc:
            info["overview"] = desc
        if cast:
            info["credits"] = {"cast": [{"name": n} for n in cast[:6]]}
        return info


# --- 帖子生成 -----------------------------------------------------------
class PostGenerator:
    EMOJI = {
        "title":   "📖",
        "quality": "🎬",
        "episodes":"📺",
        "actors":  "👥",
        "synopsis":"📝",
        "download":"⬇️",
    }

    @staticmethod
    def title_of(d):
        return d.get("title") or d.get("name") or d.get("original_title") or d.get("original_name") or ""

    @staticmethod
    def year_of(d):
        date = d.get("release_date") or d.get("first_air_date") or ""
        return date[:4] if date else ""

    @staticmethod
    def actors_of(d, limit=10):
        # 优先 credits.cast
        if d.get("credits") and d["credits"].get("cast"):
            names = [c.get("name") for c in d["credits"]["cast"][:limit]]
        elif d.get("actors"):
            return d["actors"]
        else:
            return "（暂无）"
        return "、".join([n for n in names if n])

    @staticmethod
    def episodes_of(d, manual=None):
        if manual:
            return manual
        if d.get("number_of_episodes") and d.get("number_of_seasons"):
            return f"共 {d['number_of_episodes']} 集 / {d['number_of_seasons']} 季"
        return None

    @staticmethod
    def synopsis_of(d, manual=None, limit=180):
        text = manual or d.get("overview") or ""
        text = text.strip().replace("\n", " ")
        if len(text) > limit:
            text = text[:limit] + "……"
        elif text and not text.endswith("。") and not text.endswith("…") and not text.endswith("……"):
            text = text + "……"
        return text

    def build(self, info, share, quality="1080P WEB-DL X264 AAC",
              size="136.2GB", episodes=None, synopsis=None):

        E = self.EMOJI
        title = self.title_of(info)
        year = self.year_of(info)

        lines = []
        lines.append(f"{E['title']} 名称:{title} ({year})" if year else f"{E['title']} 名称:{title}")
        lines.append(f"{E['quality']} 质量:{quality} [{size}]")

        ep_text = self.episodes_of(info, manual=episodes)
        if ep_text:
            lines.append(f"{E['episodes']} 集数:{ep_text}")
        elif info.get("_type") == "movie":
            lines.append(f"{E['episodes']} 类型:电影")

        lines.append(f"{E['actors']} 主要人物:{self.actors_of(info)}")

        syn = self.synopsis_of(info, manual=synopsis)
        if syn:
            lines.append(f"{E['synopsis']} 简介:{syn}")

        lines.append(f"{E['download']} 下载地址:")
        lines.append(share["url"])
        if share.get("code"):
            lines.append(f"\n提取码: {share['code']}")
        if share.get("cloud"):
            lines.append(f"（来源:{share['cloud']}）")

        return "\n".join(lines)


# --- 长图合成 -----------------------------------------------------------
class PosterMaker:
    """长图排版器。

    注意：为了保证渲染结果在所有平台都稳定（避免缺少 color emoji 字体导致方框），
    长图渲染时使用 ASCII/中文图标代替 emoji。emoji 只出现在纯文本输出中。
    """

    ICON = {
        "📖": "[书]",
        "🎬": "[影]",
        "📺": "[集]",
        "👥": "[演]",
        "📝": "[简]",
        "⬇": "[↓]",
    }

    def __init__(self, width=720, font_size=28, line_height=44):
        self.width = width
        self.font_size = font_size
        self.line_height = line_height
        self.font, self.font_bold = self._load_fonts(font_size)
        self.font_small = self._load_fonts(int(font_size * 0.7))[0]
        self.padding = 36

    @staticmethod
    def _load_fonts(size):
        for path in FONT_CANDIDATES:
            if path and Path(path).exists():
                try:
                    return (
                        ImageFont.truetype(path, size=size),
                        ImageFont.truetype(path, size=size),
                    )
                except Exception:
                    continue
        # 退化到默认字体（可能不显示中文，但仍可运行）
        try:
            return (
                ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size=size),
                ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size=size),
            )
        except Exception:
            return ImageFont.load_default(), ImageFont.load_default()

    def wrap(self, text, max_w):
        """按像素宽度换行"""
        out, line = [], ""
        for ch in text:
            test = line + ch
            bbox = self.font.getbbox(test)
            if bbox[2] - bbox[0] > max_w and line:
                out.append(line)
                line = ch
            else:
                line = test
        if line:
            out.append(line)
        return out

    def _to_pic_line(self, raw_line):
        """把 emoji 行转换为长图用的纯文字行"""
        result = raw_line
        for emoji, icon in self.ICON.items():
            result = result.replace(emoji, icon)
        return result

    def render(self, body_text, poster_img=None, title="", accent=(231, 76, 60)):
        # 把 body_text 中可能含的 emoji 全部转为图标字符
        pic_body = self._to_pic_line(body_text)

        # 换行处理（中文 + ASCII 都能处理）
        max_w = self.width - self.padding * 2
        wrapped = []
        for raw in pic_body.split("\n"):
            if raw == "":
                wrapped.append("")
            else:
                wrapped.extend(self.wrap(raw, max_w))

        # 海报
        poster_h = 0
        poster = None
        if poster_img is not None:
            poster = poster_img.convert("RGB").copy()
            target_w = self.width - self.padding * 2
            ratio = target_w / poster.width
            new_size = (target_w, max(1, int(poster.height * ratio)))
            poster = poster.resize(new_size, Image.LANCZOS)
            poster_h = poster.height + 24

        # 标题块
        title_h = 0
        if title:
            title_pic = self._to_pic_line(title)
            tb = self.font_bold.getbbox(title_pic)
            title_h = tb[3] - tb[1] + 20

        text_h = len(wrapped) * self.line_height
        height = self.padding * 2 + title_h + poster_h + text_h + 40

        # 画布
        canvas = Image.new("RGB", (self.width, height), (252, 252, 252))
        draw = ImageDraw.Draw(canvas)

        # 顶/底装饰条
        draw.rectangle([0, 0, self.width, 8], fill=accent)
        draw.rectangle([0, height - 6, self.width, height], fill=accent)

        y = self.padding
        if title:
            draw.text((self.padding, y), self._to_pic_line(title),
                      fill=(40, 40, 40), font=self.font_bold)
            y += title_h

        if poster:
            canvas.paste(poster, (self.padding, y))
            y += poster.height + 24

        for line in wrapped:
            draw.text((self.padding, y), line, fill=(40, 40, 40), font=self.font)
            y += self.line_height

        return canvas


# --- 海报/封面 文件工具 -------------------------------------------------
POSTER_MAX_BYTES = 480 * 1024   # 目标：单张海报 < 500KB
POSTER_MAX_SIDE = 1200          # 最长边上限


def poster_to_jpeg(img, max_bytes=POSTER_MAX_BYTES, max_side=POSTER_MAX_SIDE):
    """把海报图压成 JPEG bytes：等比缩放 + 自适应质量，确保 < max_bytes"""
    if img is None:
        return None
    im = img.convert("RGB")
    w, h = im.size
    if max(w, h) > max_side:
        r = max_side / float(max(w, h))
        im = im.resize((max(1, int(w * r)), max(1, int(h * r))), Image.LANCZOS)

    buf = io.BytesIO()
    for q in (85, 78, 70, 60, 50):
        buf.seek(0)
        buf.truncate()
        im.save(buf, format="JPEG", quality=q, optimize=True)
        if buf.tell() <= max_bytes:
            break
    # 仍超限就再缩边
    while buf.tell() > max_bytes and max(im.size) > 400:
        im = im.resize((int(im.width * 0.8), int(im.height * 0.8)), Image.LANCZOS)
        buf.seek(0)
        buf.truncate()
        im.save(buf, format="JPEG", quality=70, optimize=True)
    buf.seek(0)
    return buf.read()


def poster_to_png(img):
    """转 PNG bytes（剪贴板图片粘贴需要 PNG 格式）"""
    if img is None:
        return None
    buf = io.BytesIO()
    img.convert("RGBA").save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


UA_DEFAULT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def download_picture(url, timeout=20):
    """带 UA 下载图片（部分图床拒绝 python 默认 UA），失败返回 None"""
    if not url:
        return None
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": UA_DEFAULT})
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        img.load()
        return img
    except Exception as e:      # noqa: BLE001
        sys.stderr.write(f"[图片下载失败 {str(url)[:80]}] {e}\n")
        return None


def fit_font(text, max_w, start=64, end=20, step=4):
    """按最大宽度挑一个能放下的字号"""
    fs = start
    font = None
    while fs >= end:
        font = PosterMaker._load_fonts(fs)[0]
        try:
            w = font.getlength(text)
        except AttributeError:      # 老版本 Pillow
            w = font.getbbox(text)[2]
        if w <= max_w:
            return font
        fs -= step
    return font or PosterMaker._load_fonts(end)[0]


def make_text_cover(title, count, subtitle="", size=(900, 1350)):
    """拿不到任何图片时的保底封面：暗色渐变 + 标题 + 部数"""
    W, H = size
    img = Image.new("RGB", (W, H), (22, 24, 30))
    d = ImageDraw.Draw(img)
    # 竖向渐变
    for y in range(H):
        t = y / float(H)
        d.line([(0, y), (W, y)],
               fill=(int(20 + 34 * t), int(22 + 24 * t), int(30 + 40 * t)))
    # 装饰色块
    d.ellipse([W - 300, -160, W + 140, 300], fill=(231, 76, 60))
    d.ellipse([-160, H - 340, 220, H + 20], fill=(52, 60, 84))

    # 标题（自动换行 + 字号自适应）
    t = (title or "影视合集").strip()
    max_w = W - 120
    font = fit_font(t[:12], max_w, start=76, end=28)
    lines = []
    cur = ""
    for ch in t:
        if font.getlength(cur + ch) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    lines = lines[:3]

    lh = int(font.size * 1.5)
    y = (H - lh * (len(lines) + 2)) // 2
    for ln in lines:
        w = font.getlength(ln)
        d.text(((W - w) / 2, y), ln, fill=(255, 255, 255), font=font)
        y += lh

    sub = subtitle or f"共 {count} 部"
    fs = fit_font(sub, W - 160, start=42, end=20)
    d.text(((W - fs.getlength(sub)) / 2, y + 16), sub, fill=(220, 220, 225),
           font=fs)
    return img


def make_thumb_mosaic(thumbs, title, count, size=(900, 1350)):
    """用网盘视频缩略图拼一张 3×3 封面（没有官方海报时的次选）"""
    W, H = size
    cols, rows = 3, 3
    cw, ch = W // cols, H // rows
    canvas = Image.new("RGB", (W, H), (18, 20, 26))
    for idx, im in enumerate([t for t in thumbs if t][:9]):
        im = im.convert("RGB")
        # 居中裁剪成格子比例后填满
        r_src, r_dst = im.width / im.height, cw / ch
        if r_src > r_dst:
            nw = int(im.height * r_dst)
            im = im.crop(((im.width - nw) // 2, 0, (im.width + nw) // 2, im.height))
        else:
            nh = int(im.width / r_dst)
            im = im.crop((0, (im.height - nh) // 2, im.width, (im.height + nh) // 2))
        canvas.paste(im.resize((cw, ch), Image.LANCZOS),
                     ((idx % cols) * cw, (idx // cols) * ch))

    # 底部压一条信息条
    d = ImageDraw.Draw(canvas)
    bar_h = 170
    d.rectangle([0, H - bar_h, W, H], fill=(16, 17, 21))
    label = f"{(title or '影视合集').strip()} · 共 {count} 部"
    font = fit_font(label, W - 80, start=56, end=24)
    d.text(((W - font.getlength(label)) / 2, H - bar_h + 52), label,
           fill=(255, 255, 255), font=font)
    return canvas


def make_collection_cover(base_poster, title, count, accent=(20, 22, 26)):
    """基于第一部电影海报合成一张合集封面：竖版 2:3，底部黑条写合集信息"""
    if base_poster is None:
        return None
    p = base_poster.convert("RGB")
    # 统一按 2:3 裁边
    w, h = p.size
    target_ratio = 2 / 3
    cur = w / h
    if cur > target_ratio:
        nw = int(h * target_ratio)
        x0 = (w - nw) // 2
        p = p.crop((x0, 0, x0 + nw, h))
    elif cur < target_ratio:
        nh = int(w / target_ratio)
        y0 = (h - nh) // 2
        p = p.crop((0, y0, w, y0 + nh))

    W = 900
    H = int(W * 1.5)
    p = p.resize((W, H), Image.LANCZOS)

    bar_h = 150
    canvas = Image.new("RGB", (W, H + bar_h), accent)
    canvas.paste(p, (0, 0))
    d = ImageDraw.Draw(canvas)
    t = (title or "影视合集").strip()
    # 过长时优先在分隔符处断开（保留「XX合集」这类关键后缀），再退化为截断
    if len(t) > 22:
        cut = re.split(r"[·\-—|/]", t)
        if len(cut) > 1:
            t = cut[0].strip()
    if len(t) > 22:
        t = t[:22] + "…"
    label = f"{t} · 共 {count} 部"
    # 字号自适应：从 64 往下找能放下的
    fs = 64
    font = None
    while fs >= 26:
        font = PosterMaker._load_fonts(fs)[0]
        if d.textlength(label, font=font) <= W - 80:
            break
        fs -= 4
    if font is None:
        font = PosterMaker._load_fonts(26)[0]
    tb = font.getbbox(label)
    tx = (W - (tb[2] - tb[0])) // 2
    ty = H + (bar_h - (tb[3] - tb[1])) // 2 - tb[1]
    d.text((tx, ty), label, fill=(255, 255, 255), font=font)
    return canvas


def _tmdb_poster_of(tmdb_id):
    """按 TMDB ID 抓海报图（网页抓取，无需 Key）"""
    got = TMDBWebClient().detail_by_id(tmdb_id)
    if got and got.get("poster_url"):
        return download_picture(got["poster_url"])
    return None


def _poster_for_item(it, web=None, try_search=True):
    """给单个条目找海报：有 {tmdb-id} 直取，否则用片名站内搜索"""
    web = web or TMDBWebClient()
    title = it.get("title") or ""
    if it.get("tmdb"):
        img = _tmdb_poster_of(it["tmdb"])
        if img is not None:
            return img
    if not try_search or not title:
        return None
    # “速度与激情 1” 这种尾部序号可能搜不到，再试去掉序号的名字
    names = [title, re.sub(r"[\s\-_]*\d+\s*$", "", title).strip()]
    for n in dict.fromkeys([x for x in names if x]):
        try:
            cands = web.search_by_name(n, year=it.get("year")) or []
        except Exception:       # noqa: BLE001
            cands = []
        for c in cands[:2]:
            img = _tmdb_poster_of(c["id"])
            if img is not None:
                return img
    return None


def pick_collection_cover(items, series_name, verbose=False):
    """给合集找封面，多级兜底，保证一定出图：

    1. 从「年份最早」那部开始依次尝试：带 {tmdb-id} 直取官方海报，
       没有 ID 就用片名在 TMDB 站内搜索（最多试 3 部）
    2. 都没有 → 用网盘视频缩略图拼 3×3 封面
    3. 还没有 → 纯文字封面（暗色渐变 + 标题 + 部数）

    返回 (PIL.Image, 来源说明)
    """
    if not items:
        return make_text_cover(series_name, 0), "文字封面"

    ordered = sorted(items, key=lambda x: (x.get("year") or 9999, x["title"]))
    web = TMDBWebClient()

    # 1) 官方海报（优先正传第一部）
    for it in ordered[:3]:
        img = _poster_for_item(it, web)
        if img is not None:
            cover = make_collection_cover(img, series_name, len(items))
            if cover is not None:
                how = f"TMDB#{it['tmdb']}" if it.get("tmdb") else "TMDB 搜索"
                return cover, f"{how}（{it['title']}）"

    # 2) 网盘视频缩略图拼图
    thumbs = [download_picture(t) for t in
              [i.get("thumb") for i in ordered if i.get("thumb")][:9]]
    if any(thumbs):
        return make_thumb_mosaic(thumbs, series_name, len(items)), "视频缩略图拼图"

    # 4) 纯文字封面
    return make_text_cover(series_name, len(items)), "文字封面"


def _try_collection_cover(items, series_name, count, size="w780"):
    """向后兼容的封面入口（只返回图，不返回来源）"""
    cover, _src = pick_collection_cover(items, series_name)
    return cover


# --- 配置持久化 ----------------------------------------------------------
def config_path():
    return Path.home() / ".share_poster.json"


def load_config():
    p = config_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def save_config(cfg):
    p = config_path()
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


# --- 光鸭云盘账号（登录 / token / 文件操作）--------------------------------
class GuangyaAccount:
    """光鸭云盘账号客户端。

    能力：
      · 短信登录（init → send → verify → signin 四步）
      · 手动贴 access_token / refresh_token
      · token 自动持久化到 ~/.share_poster.json（含过期时间）
      · 文件操作：列目录 / 重命名 / 取详情（用于给自己的分享内文件改名）

    接口分两个域：
      · account.guangyapan.com —— 登录鉴权
      · api.guangyapan.com/nd.bizuserres.s/v1 —— 文件/分享操作

    协议细节严格对齐 guangyaclient（DDSRem-Dev），关键点：
      · x-device-sign = f"wdi10.{did}" + 32 位随机 hex（少这段会被风控拒绝）
      · user_info 只带基础头 + authorization，不要带 x-device-* 全套
      · 所有业务请求统一带 traceparent（W3C 格式）
    """

    ACCOUNT_BASE = "https://account.guangyapan.com"
    API_BASE = "https://api.guangyapan.com/nd.bizuserres.s/v1"
    CLIENT_ID = "aMe-8VSlkrbQXpUR"
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/147.0.0.0 Safari/537.36")

    def __init__(self, access_token=None, refresh_token=None, device_id=None):
        self.token = access_token or ""
        self.refresh_token_value = refresh_token or ""
        self.expires_at = None
        self.device_id = device_id or self._gen_did()
        self.session = requests.Session()
        self.session.headers.update({
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "did": self.device_id,
            "dt": "4",
            "origin": "https://www.guangyapan.com",
            "referer": "https://www.guangyapan.com/",
            "user-agent": self.UA,
        })
        if self.token:
            self.session.headers["authorization"] = f"Bearer {self.token}"

    # ---- 基础设施 ----
    @staticmethod
    def _gen_did():
        import hashlib as _h
        import os as _os
        return _h.md5(_os.urandom(16)).hexdigest()

    @staticmethod
    def _traceparent():
        import secrets as _s
        return f"00-{_s.token_hex(16)}-{_s.token_hex(8)}-01"

    def _account_headers(self):
        """登录鉴权用头（含 x-device-* 全套；不带 authorization，
        否则空 Bearer 会被服务端判为 bad authorization format）"""
        import secrets as _s
        return {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://www.guangyapan.com",
            "referer": "https://www.guangyapan.com/",
            "user-agent": self.UA,
            "x-client-id": self.CLIENT_ID,
            "x-client-version": "0.0.1",
            "x-device-id": self.device_id,
            "x-device-model": "chrome%2F147.0.0.0",
            "x-device-name": "PC-Chrome",
            # 关键：did 后面必须再拼 32 位随机 hex，否则服务端认为签名无效
            "x-device-sign": f"wdi10.{self.device_id}{_s.token_hex(16)}",
            "x-net-work-type": "NONE",
            "x-os-version": "MacIntel",
            "x-platform-version": "1",
            "x-protocol-version": "301",
            "x-provider-name": "NONE",
            "x-sdk-version": "9.0.2",
        }

    # ---- 登录：短信流程 ----
    def login_sms_init(self, phone, captcha_token=None):
        """第一步：初始化验证码（返回 captcha_token 或人机验证 url）"""
        body = {
            "client_id": self.CLIENT_ID,
            "action": "POST:/v1/auth/verification",
            "device_id": self.device_id,
            "meta": {"phone_number": phone},
        }
        if captcha_token:
            body["captcha_token"] = captcha_token
        r = self.session.post(f"{self.ACCOUNT_BASE}/v1/shield/captcha/init",
                              headers=self._account_headers(), json=body, timeout=20)
        r.raise_for_status()
        return r.json()

    def login_sms_send(self, phone, captcha_token, target="ANY"):
        """第二步：发送短信验证码"""
        h = self._account_headers()
        h["x-captcha-token"] = captcha_token
        r = self.session.post(f"{self.ACCOUNT_BASE}/v1/auth/verification",
                              headers=h,
                              json={"phone_number": phone, "target": target,
                                    "client_id": self.CLIENT_ID}, timeout=20)
        r.raise_for_status()
        return r.json()

    def login_sms_verify(self, verification_id, code):
        """第三步：校验验证码，拿 verification_token"""
        r = self.session.post(
            f"{self.ACCOUNT_BASE}/v1/auth/verification/verify",
            headers=self._account_headers(),
            json={"verification_id": verification_id,
                  "verification_code": code,
                  "client_id": self.CLIENT_ID}, timeout=20)
        r.raise_for_status()
        return r.json()

    def login_sms_signin(self, code, verification_token, phone, captcha_token):
        """第四步：提交登录，拿 access_token / refresh_token"""
        h = self._account_headers()
        h["x-captcha-token"] = captcha_token
        r = self.session.post(
            f"{self.ACCOUNT_BASE}/v1/auth/signin",
            headers=h,
            json={"verification_code": code,
                  "verification_token": verification_token,
                  "username": phone,
                  "client_id": self.CLIENT_ID}, timeout=20)
        r.raise_for_status()
        return self._apply_token(r.json())

    def start_sms_login(self, phone):
        """登录第 1 步（Web 拆步骤用）：发验证码，返回 {
        captcha_token, verification_id, need_captcha, url }"""
        init = self.login_sms_init(phone)
        ct = init.get("captcha_token")
        if not ct:
            return {"ok": False, "need_captcha": True,
                    "url": init.get("url") or init.get("captcha_url") or "",
                    "raw": init}
        send = self.login_sms_send(phone, ct)
        vid = send.get("verification_id")
        if not vid:
            return {"ok": False, "raw": send}
        return {"ok": True, "captcha_token": ct, "verification_id": vid}

    def finish_sms_login(self, phone, code, captcha_token, verification_id):
        """登录第 2 步（Web 拆步骤用）：提交验证码完成登录"""
        ver = self.login_sms_verify(verification_id, code)
        vtok = ver.get("verification_token")
        if not vtok:
            raise RuntimeError(f"验证码校验失败：{str(ver)[:200]}")
        return self.login_sms_signin(code, vtok, phone, captcha_token)

    def _apply_token(self, result):
        """把登录/刷新返回的 token 落进实例"""
        if not isinstance(result, dict):
            return result
        at = result.get("access_token")
        if at:
            self.token = at
            self.session.headers["authorization"] = f"Bearer {at}"
            exp = result.get("expires_in")
            self.expires_at = time.time() + int(exp) if exp else None
            rt = result.get("refresh_token")
            if rt:
                self.refresh_token_value = rt
        return result

    def login_sms(self, phone, get_code=None):
        """短信登录全流程（CLI 用）。get_code 为取验证码的回调。"""
        st = self.start_sms_login(phone)
        if not st.get("ok"):
            raise RuntimeError(f"发送验证码失败：{str(st.get('raw'))[:200]}")
        if get_code is None:
            def get_code():
                return input("请输入短信验证码: ")
        code = get_code()
        return self.finish_sms_login(phone, code,
                                     st["captcha_token"], st["verification_id"])

    def refresh_token(self):
        """用 refresh_token 换新的 access_token"""
        if not self.refresh_token_value:
            raise RuntimeError("没有可用的 refresh_token，请重新登录")
        h = self._account_headers()
        h["x-action"] = "401"
        r = self.session.post(
            f"{self.ACCOUNT_BASE}/v1/auth/token", headers=h,
            json={"client_id": self.CLIENT_ID, "grant_type": "refresh_token",
                  "refresh_token": self.refresh_token_value}, timeout=20)
        r.raise_for_status()
        return self._apply_token(r.json())

    def user_info(self, _retried=False):
        """获取当前登录用户信息（只带基础头 + authorization）。

        access_token 过期时（很常见，默认只有 2 小时）自动用
        refresh_token 续期后重试一次。
        """
        if not self.token:
            raise RuntimeError("未登录：token 为空")
        h = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://www.guangyapan.com",
            "referer": "https://www.guangyapan.com/",
            "user-agent": self.UA,
            "authorization": f"Bearer {self.token}",
        }
        # 注意：这个接口是 GET，用 POST 会返回 501 Method Not Allowed
        r = self.session.get(f"{self.ACCOUNT_BASE}/v1/user/me",
                             headers=h, timeout=20)
        if r.status_code == 401 and self.refresh_token_value and not _retried:
            # token 过期 → 用 refresh_token 换新的再试一次
            self.refresh_token()
            return self.user_info(_retried=True)
        r.raise_for_status()
        return r.json()

    # ---- 业务 API（自动带 token / 401 自动刷新）----
    def _post_api(self, path, payload, _retried=False):
        url = path if path.startswith("http") else f"{self.API_BASE}/{path}"
        h = {"traceparent": self._traceparent()}
        # 先看本地过期时间，过期就主动续期
        if self.refresh_token_value and self.expires_at \
                and time.time() >= self.expires_at and not _retried:
            try:
                self.refresh_token()
            except Exception:                       # noqa: BLE001
                pass
        r = self.session.post(url, json=payload, headers=h, timeout=30)
        if r.status_code == 401 and self.refresh_token_value and not _retried:
            self.refresh_token()
            return self._post_api(path, payload, _retried=True)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("code") not in (0, None) \
                and data.get("msg") not in ("success", None):
            raise RuntimeError(f"接口异常：{str(data)[:200]}")
        return data

    def fs_rename(self, file_id, new_name):
        """重命名文件/文件夹（注意路径是 file/rename）"""
        return self._post_api("file/rename",
                              {"fileId": str(file_id), "newName": new_name})

    def fs_list(self, parent_id="", page=0, page_size=100):
        """列出某目录下的文件（parentId 为空 = 根目录）"""
        return self._post_api("file/get_file_list", {
            "parentId": "" if parent_id is None else str(parent_id),
            "page": page, "pageSize": page_size,
            "orderBy": 0, "sortType": 0})

    def fs_detail(self, file_id):
        """获取文件详情"""
        return self._post_api("file/get_file_detail", {"fileId": str(file_id)})

    # ---- 持久化 ----
    def save_to_config(self, extra=None):
        cfg = load_config()
        cfg["guangya"] = {
            "access_token": self.token,
            "refresh_token": self.refresh_token_value,
            "device_id": self.device_id,
            "expires_at": self.expires_at,
        }
        if extra:
            cfg["guangya"].update(extra)
        save_config(cfg)

    @classmethod
    def from_config(cls):
        """从配置文件恢复登录态（没有则返回未登录实例）"""
        g = (load_config().get("guangya") or {})
        acc = cls(access_token=g.get("access_token"),
                  refresh_token=g.get("refresh_token"),
                  device_id=g.get("device_id"))
        acc.expires_at = g.get("expires_at")
        return acc

    @property
    def logged_in(self):
        return bool(self.token)


def guangya_login_interactive():
    """命令行交互式登录（供 CLI 直接调用）"""
    acc = GuangyaAccount()
    print("── 光鸭云盘登录 ──")
    phone = input("手机号（含区号，如 +86 13800138000）: ").strip()
    if not phone:
        raise RuntimeError("手机号不能为空")
    try:
        acc.login_sms(phone)
    except Exception as e:      # noqa: BLE001
        print(f"❌ 登录失败：{e}")
        return None
    try:
        info = acc.user_info()
        data = info.get("data") or info
        name = data.get("nickname") or data.get("name") or ""
    except Exception:           # noqa: BLE001
        name = ""
    acc.save_to_config()
    print(f"✅ 登录成功{('：' + name) if name else ''}（token 已保存）")
    return acc


# --- 分享内文件重命名 ------------------------------------------------------
# --- 同片多版本：区分标记 -----------------------------------------------
# 一个分享里同一部片常有多个版本（4K / 1080P / 双语 / 导演剪辑…）。
# 规范化后名字会撞在一起，所以撞名时补一个「有信息量」的标记：
#   优先用画质（2160P 还是 1080P，最直观）
#   其次用版本词（EXTENDED / PROPER / 国配 / 未删减…）
#   最后才退回 -v2 / -v3 纯序号
_VERSION_WORDS = [
    ("extended", "EXTENDED"), ("director", "导演剪辑"), ("remastered", "重制"),
    ("proper", "PROPER"), ("repack", "REPACK"), ("remux", None),   # remux 已在画质里
    ("imax", "IMAX"), ("unrated", "未分级"), ("uncut", "未删减"),
    ("hdr", None), ("dolby", None), ("dv", None),
    ("国英双语", "国配双语"), ("国配", "国语"), ("双语", "双语"),
    ("中字", None), ("中英字幕", None), ("内嵌", None), ("外挂", None),
    ("3d", "3D"), ("注释", None),
]


def extract_version_tags(name, limit=None):
    """从原始名字里抽「版本标记」列表（从具体到宽泛），用于同名区分。

    比如 `速度与激情2.2003.国英双语.中英字幕￡CMCT死亡骑士`
      → ['国配双语', 'CMCT死亡骑士']
    """
    if not name:
        return []
    lo = name.lower()
    tags = []
    for kw, label in _VERSION_WORDS:
        if label is None:
            continue
        if kw in lo and label not in tags:
            tags.append(label)
    # 分辨率 / 编码：名字里写着 1080p、HEVC 时也是很好的区分依据
    for pat, fmt in ((r"\b(2160|1080|720|480)\s*p\b", "{}P"),
                     (r"\b(4k|uhd)\b", "4K")):
        m = re.search(pat, lo)
        if m:
            v = fmt.format(m.group(1).upper())
            if v not in tags:
                tags.append(v)
    # 常见的双语/字幕组：￡CMCT无尽 / -FGT / RARBG
    m = re.search(r"[￡￥]([A-Za-z0-9\u4e00-\u9fff]{2,10})", name)
    if m:
        tags.append(m.group(1))
    m = re.search(r"-([A-Z]{2,10})(?:\.|$)", name)
    if m and m.group(1) not in ("MKV", "MP4", "AVI"):
        tags.append(m.group(1))
    # 去冗余：「国配双语」已经含「双语」，就别再挂一个「双语」了
    out = []
    for t in tags:
        if any(t != o and t in o for o in tags):
            continue
        if t not in out:
            out.append(t)
    return out if limit is None else out[:limit]


def extract_version_tag(name):
    """抽一条最合适的版本标记（兼容旧调用）。"""
    tags = extract_version_tags(name, limit=1)
    return tags[0] if tags else ""


def rename_plan_lines(plan, limit=None, status_of=None):
    """把重命名计划渲染成可读文本行（CLI / Web 共用）。"""
    out = []
    rows = plan if limit is None else plan[:limit]
    for i, p in enumerate(rows, 1):
        st = (status_of or {}).get(p.get("fileId"), "")
        mark = {"ok": "✔", "failed": "✘"}.get(st, "·")
        kind = "📁" if p.get("is_dir") else "🎞"
        out.append(f"  {mark} {i:>3}. {kind} {p['old']}\n"
                   f"          → {p['new']}")
    if limit is not None and len(plan) > limit:
        out.append(f"  … 其余 {len(plan) - limit} 项略")
    return "\n".join(out)


def guangya_token_days_left(acc):
    """token 距离过期还剩多少天，未知返回 None"""
    try:
        exp = getattr(acc, "expires_at", None)
        if not exp:
            return None
        if isinstance(exp, str):
            exp = float(exp)
        if exp > 1e11:                     # 毫秒
            exp = exp / 1000.0
        return (exp - time.time()) / 86400.0
    except Exception:                       # noqa: BLE001
        return None


def ensure_guangya_account(verbose=True, auto_refresh=True):
    """取已登录的光鸭账号；token 将过期时自动续期。未登录返回 None。"""
    acc = GuangyaAccount.from_config()
    if not acc.logged_in:
        if verbose:
            print("⚠ 未登录光鸭云盘。先执行：python3 share_poster.py --login-guangya")
        return None
    days = guangya_token_days_left(acc)
    if auto_refresh and acc.refresh_token_value and days is not None and days < 3:
        try:
            acc.refresh_token()
            acc.save_to_config()
            if verbose:
                print("🔄 登录态已自动续期")
        except Exception as e:              # noqa: BLE001
            if verbose:
                print(f"⚠ 自动续期失败（{e}），仍尝试使用旧 token")
    return acc


def build_rename_plan(deep_files, dir_nodes, items=None, name_fmt=None):
    """为一次分享生成「原名 → 规范名」重命名计划（纯计算，不落盘）。

    目标格式（Emby 友好，他人转存后可直接刮削）：
      文件夹: 片名 (年份) [画质] {tmdb-id}
      文件:   片名 (年份) [画质] {tmdb-id}.mkv

    参数：
      deep_files / dir_nodes —— CloudShareFetcher.fetch() 的产物
      items                  —— collect_media_items() 结果（提供中文名/年份/tmdb）
      name_fmt(fmt_kwargs)->str —— 自定义命名函数，不传则用默认格式

    返回 [{"fileId","is_dir","old","new","changed","reason"}, ...]
    """
    if items is None:
        items = collect_media_items(deep_files, dir_nodes)

    def _norm_key(x):
        return norm_title((x or "").split("(")[0])

    # 支持两种 items：
    #   · collect_media_items() 的新格式 —— 自带 fileId/fileIds，精确到文件
    #   · 旧格式 / 测试数据 —— 只带标题，退化为按目录名匹配
    item_by_key, item_by_fid, item_by_year = {}, {}, {}
    for it in items or []:
        k = _norm_key(it.get("title"))
        if k and k not in item_by_key:
            item_by_key[k] = it
        for fid in ([it.get("fileId")] + list(it.get("fileIds") or [])):
            if fid:
                item_by_fid[str(fid)] = it
    # 年份索引：整份分享里该年份只有一部片时才好用（避免同年的不同片张冠李戴）
    _ycnt = {}
    for it in items or []:
        if it.get("year"):
            _ycnt[it["year"]] = _ycnt.get(it["year"], 0) + 1
    for it in items or []:
        y = it.get("year")
        if y and _ycnt.get(y) == 1:
            item_by_year[y] = it

    def _fmt(**kw):
        if name_fmt:
            return name_fmt(**kw)
        title = kw["title"] or ""
        year = kw.get("year")
        quality = kw.get("quality") or ""
        tmdb = kw.get("tmdb")
        ext = kw.get("ext") or ""
        s = title
        if year:
            s += f" ({year})"
        if quality:
            s += f" [{quality}]"
        if tmdb:
            s += f" {{tmdb-{tmdb}}}"
        return s + ext

    plan = []

    def _split_ext(name, is_dir):
        if is_dir:
            return name, ""
        stem, dot, ext = name.rpartition(".")
        return (stem, ext) if dot else (name, "")

    def _register(name, file_id):
        _used_names[name.lower()] = str(file_id)
        return name

    # 已经出现过的「原始名」——用来判断两个目标撞名的文件到底是不是同一个副本
    _seen_src = set()
    # 已经占用的「目标名」——用于撞名时补后缀
    _used_names = {}

    def _dedup(new, file_id, is_dir, quality, src_name, dup=False):
        """同片多版本撞名时，补一个「能看出区别」的标记。

        候选按「信息量从多到少」依次试，第一个没被占用的就用：
          ① 原名就是同一个文件（同名副本）→ 不加，让 Emby 自己加 (2)
          ② 画质（撞名的往往就是不同画质）
          ③ 版本词：PROPER / EXTENDED / 国配双语 / FGT / CMCT死亡骑士…
          ④ 版本词两两组合
          ⑤ 纯序号 v2 / v3（实在没线索才用）
        """
        # ① 同源副本：改用序号区分（内容虽同，但同目录下不能重名）
        if dup:
            key0 = new.lower()
            if key0 not in _used_names:
                return _register(new, file_id)
            stem, ext = _split_ext(new, is_dir)
            suffix = ("." + ext) if ext else ""
            n = 2
            while True:
                cand = f"{stem} - v{n}{suffix}"
                if cand.lower() not in _used_names:
                    return _register(cand, file_id)
                n += 1

        key = new.lower()
        if key not in _used_names:
            return _register(new, file_id)

        stem, ext = _split_ext(new, is_dir)
        suffix = ("." + ext) if ext else ""

        cands = []
        # ② 版本词 / 发布组优先：PROPER、EXTENDED、FGT、CMCT… 这类区分度最高，
        #    而且不会跟已有的 `[1080P H.264 蓝光]` 画质标签重复
        _base = new.lower()
        tags = [t for t in extract_version_tags(src_name) if t.lower() not in _base]
        cands.extend(tags)
        # ③ 画质（同片不同画质撞名时用，且必须是名字里还没写过的那一档）
        if quality and f"[{quality}]" not in new:
            cands.append(quality)
        # ④ 两两组合，给区分度不够的单标签再加一层保险
        for i, a in enumerate(tags):
            for b in tags[i + 1:]:
                cands.append(f"{a} · {b}")
        # 同片不同「分辨率/编码」时，画质标签往往才是唯一区别（如 1080P vs 2160P），
        # 上面被过滤掉的低分辨率标记作为最后的信息性兜底再放回来
        for t in extract_version_tags(src_name):
            if re.match(r"^\d+[Pp]$|^4K$", t) and t not in cands:
                cands.append(t)
        for info in cands:
            cand = f"{stem} - {info}{suffix}"
            if cand.lower() not in _used_names:
                return _register(cand, file_id)
        # ⑤ 实在没线索 → 纯序号兜底
        n = 2
        while True:
            cand = f"{stem} - v{n}{suffix}"
            if cand.lower() not in _used_names:
                return _register(cand, file_id)
            n += 1

    def _push(file_id, is_dir, old, new, reason="", quality="", src_name=""):
        new = (new or "").strip()
        if not old or not new:
            return
        src_key = old.lower()
        dup = src_key in _seen_src
        _seen_src.add(src_key)
        # 名字本来就规范、且没有副本要区分 → 跳过，省一次没必要的改名请求。
        # 但必须先把目标名登记进 _used_names，否则后面同片多版本撞上来时
        # 检测不到冲突，两个版本会重名。
        if new == old and not dup:
            _register(new, file_id)
            return
        new = _dedup(new, file_id, is_dir, quality, src_name or old, dup=dup)
        plan.append({"fileId": str(file_id), "is_dir": is_dir,
                     "old": old, "new": new, "changed": True,
                     "reason": reason})

    # ---- 文件夹 ----
    root_fid = str(dir_nodes[0].get("fileId")) if dir_nodes else ""
    # 哪些目录「确实装着视频」——只有这些才值得改名。
    # 空壳目录（只剩截图/字幕/说明 txt，视频缺失）改名只会帮倒忙：
    # 名字变规范了却没有内容，还得靠 TMDB 猜年份，容易出错。
    dirs_with_media = set()
    for f in (deep_files or []):
        fext = (f.get("ext") or Path(f.get("fileName") or "").suffix or "").lower()
        if fext in MEDIA_EXTS:
            if f.get("parentId"):
                dirs_with_media.add(str(f["parentId"]))
            for pid in str(f.get("fullParentIds") or "").split("/"):
                if pid:
                    dirs_with_media.add(pid)

    for d in (dir_nodes or []):
        old = (d.get("fileName") or "").strip()
        if not old or is_junk_title(old):
            continue
        # 分享根目录（名字通常就是分享标题，如「速度与激情 合集」）不动，
        # 否则会把「XX 合集」也改成单片格式。
        if str(d.get("fileId")) == root_fid or is_collection_title(old):
            continue
        if not looks_like_single_title(old):
            continue
        t, y, tm = parse_share_title(old)
        if not t:
            continue
        it = item_by_fid.get(str(d.get("fileId"))) or item_by_key.get(_norm_key(t))
        # 没装着视频的目录：跳过（除非条目本身带 tmdb，说明识别很确定）
        if str(d.get("fileId")) not in dirs_with_media \
                and not (it or {}).get("tmdb") and not tm:
            continue
        # 文件夹内部如果没视频，就别塞画质了（截图没有分辨率意义）
        has_media = str(d.get("fileId")) in dirs_with_media
        title = (it or {}).get("title") or t
        year = (it or {}).get("year") or (int(y) if (y or "").isdigit() else None)
        tmdb = (it or {}).get("tmdb") or tm
        # 画质取「自己的名字」优先：同一部片子常有 4K/1080P 多个版本，
        # 目录名 `XXX.1080p.BluRay` 就是最可靠的画质证据；
        # 只有目录名里看不出来时，才退回继承条目（文件）的画质。
        quality = ""
        if has_media:
            quality = (parse_quality_from_name(old)
                       or (it or {}).get("quality")
                       or "")
        new = _fmt(title=title, year=year, quality=quality, tmdb=tmdb,
                   ext="", is_dir=True)
        _push(d.get("fileId"), True, old, new, "文件夹规范化",
              quality=quality, src_name=old)

    # ---- 文件 ----
    for f in (deep_files or []):
        old = (f.get("fileName") or "").strip()
        ext = (f.get("ext") or Path(old).suffix or "").lower()
        if ext not in MEDIA_EXTS:
            continue
        if is_junk_title(Path(old).stem):
            continue
        stem = Path(old).stem
        # 身份优先级：文件自身 fileId 命中 > 父目录解析出的身份 > 文件名自身解析
        it = item_by_fid.get(str(f.get("fileId")))
        pt, py = f.get("_dir_title"), f.get("_dir_year")
        if it is None and pt:
            it = item_by_key.get(_norm_key(pt))
        if it is not None:
            title, year, tmdb = it.get("title") or pt, it.get("year"), it.get("tmdb")
            if not year and py and str(py).isdigit():
                year = int(py)
        else:
            title, year, tmdb = parse_share_title(stem)
            year = int(year) if (year or "").isdigit() else None
            # 父目录与 fileId 都没命中 → 用文件名自己再兜两层：
            #   ① 归一化片名直接命中（`Furious Seven` ↔ `速度与激情7` 不行，但同语言可以）
            #   ② 年份唯一时反查（`The Fast and the Furious (2001)` → 2001 只有这部，认领中文名）
            cand = item_by_key.get(_norm_key(title or stem))
            if cand is None and year and str(year).isdigit():
                cand = item_by_year.get(int(year))
            if cand is not None:
                title, year, tmdb = _pick_identity(
                    (title, year, tmdb),
                    (cand.get("title"), cand.get("year"), cand.get("tmdb")))
        if not title:
            continue
        quality = (parse_quality_from_name(old) or parse_quality_from_name(stem or "")
                   or (it or {}).get("quality") or parse_quality_from_name(pt or ""))
        new = _fmt(title=title, year=year, quality=quality, tmdb=tmdb,
                   ext=ext, is_dir=False)
        _push(f.get("fileId"), False, old, new, "文件名规范化",
              quality=quality, src_name=old)
    return plan


def apply_rename_plan(account, plan, dry_run=True, progress=None):
    """执行重命名计划。dry_run=True 只返回结果不实际改。

    返回 {"total","changed","skipped","failed","items":[...]}
    """
    out = {"total": len(plan or []), "changed": 0, "skipped": 0,
           "failed": 0, "items": []}
    for i, p in enumerate(plan or [], 1):
        rec = dict(p)
        if dry_run:
            rec["status"] = "preview"
            out["items"].append(rec)
            out["changed"] += 1
            continue
        try:
            account.fs_rename(p["fileId"], p["new"])
            rec["status"] = "ok"
            out["changed"] += 1
        except Exception as e:      # noqa: BLE001
            rec["status"] = "failed"
            rec["error"] = str(e)[:160]
            out["failed"] += 1
        out["items"].append(rec)
        if progress:
            progress(i, len(plan), rec)
        time.sleep(0.25)            # 轻微限流，避免触发风控
    return out


# --- 主流程 -------------------------------------------------------------
def pick_one(results, media_label):
    if not results:
        return None
    print(f"\n[{media_label}] 共 {len(results)} 条结果（前 5 条）:")
    for i, r in enumerate(results[:5], 1):
        t = r.get("title") or r.get("name") or r.get("original_title") or r.get("original_name") or "（无标题）"
        d = (r.get("release_date") or r.get("first_air_date") or "")[:10]
        ov = (r.get("overview") or "").replace("\n", " ")
        if len(ov) > 50:
            ov = ov[:50] + "…"
        print(f"  {i}. {t} ({d}) — {ov}")

    print(f"\n请选择序号（直接回车 = 选第 1 条，0 = 跳过）:")
    try:
        raw = input("> ").strip()
    except EOFError:
        raw = ""
    if not raw:
        raw = "1"
    try:
        idx = int(raw)
    except ValueError:
        idx = 1
    if idx <= 0 or idx > len(results[:5]):
        return None
    return results[idx - 1]


def human_size(n):
    """字节数 → 人类可读大小"""
    try:
        n = float(n)
    except Exception:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


def guess_quality(size_bytes):
    """按分享体积启发式推断画质（仅供自动模式默认，可 --quality 覆盖）"""
    if not size_bytes:
        return "1080P WEB-DL X264 AAC"
    gb = size_bytes / (1024 ** 3)
    if gb >= 60:
        return "2160P UHD 蓝光原盘"
    if gb >= 30:
        return "2160P UHD REMUX"
    if gb >= 12:
        return "1080P 蓝光原盘"
    if gb >= 4:
        return "1080P WEB-DL X264 AAC"
    if gb >= 1:
        return "720P WEB-DL"
    return "HD 高清"


# 罗马数字序号 → 阿拉伯数字（合集里常见「速度与激情 Ⅰ/Ⅱ/Ⅲ」）
ROMAN_MAP = {
    "Ⅰ": "1", "Ⅱ": "2", "Ⅲ": "3", "Ⅳ": "4", "Ⅴ": "5",
    "Ⅵ": "6", "Ⅶ": "7", "Ⅷ": "8", "Ⅸ": "9", "Ⅹ": "10",
}

# 英文续集关键词 → 第几部（用于把 "The.Matrix.Reloaded" 这类英文片名
# 与中文文件夹 "黑客帝国2" 按序号对上。只作兜底匹配用）
SEQUEL_KEYWORDS = {
    "reloaded": 2, "revolutions": 3, "resurrections": 4, "revolution": 3,
    "renaissance": 4, "resurgence": 2, "retaliation": 3, "redemption": 4,
    "rising": 2, "reign": 2, "reckoning": 5, "revenge": 4, "relativity": 3,
    "returns": 3, "ridley": 0, "the second": 2, "the third": 3,
    "the fourth": 4, "the fifth": 5, "the last": 99, "final": 99,
    "part ii": 2, "part iii": 3, "part iv": 4, "part v": 5,
}


def extract_part(title):
    """从片名里提取续集序号（第几部）。拿不到返回 None。
    优先级：标题里独立的阿拉伯数字(1-30) > 罗马数字 > 英文续集词。
    注意：4 位年份不算序号（如 1999）。"""
    if not title:
        return None
    m = re.search(r"(?<!\d)(\d{1,2})(?!\d)", title)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 30:
            return n
    for k, v in ROMAN_MAP.items():
        if k in title:
            return int(v)
    lo = title.lower()
    for w, n in SEQUEL_KEYWORDS.items():
        if w in lo:
            return n
    return None

# 明显不是「某一部片」的目录/容器关键词
CONTAINER_WORDS = ("合集", "打包", "全集", "全季", "系列", "字幕", "花絮", "预告",
                   "extras", "bonus", "featurettes", "sample", "mp4", "mkv",
                   "sample", "补丁", "原盘", "iso", "seeds")


def parse_share_title(raw_title, is_share_title=False):
    """从分享标题里拆 (片名, 年份, tmdb_id)。示例：
    《星球大战：曼达洛人与古古 (2026) {tmdb-1228710}》
    片名.2026.2160P  ...
    速度与激情2 (2003) {tmdb-584} - 4K REMUX 4K

    is_share_title=True 时按「分享总标题」解析：
      · 不套用「取方括号中文」规则（该规则是给文件名用的，会把
        【詹妮弗·康纳利】绝世美女-电影合集【42部】错切成「詹妮弗·康纳利」）
      · 清掉【N部】【合集】等描述词后仍保留主体语义
    """
    if not raw_title:
        return None, None, None
    t = raw_title
    if is_share_title:
        # 【42部】【36部】这类计数标记直接删掉
        t = re.sub(r"[\[【]\s*\d{1,3}\s*[部集]\s*[\]】]", " ", t)
        # 【演员名】这种方括号标记：保留内容、去掉壳，避免后面被当成片名主体
        t = re.sub(r"[\[【]([^\]】]{1,30})[\]】]", r" \1 ", t)
        t = re.sub(r"\s+", " ", t).strip()
    tmdb_id = None
    # 兼容 {tmdb-123} / {tmdbid-123} / {tmdb_id:123} 等写法
    m = re.search(r"\{?\s*tmdb(?:[-_ ]?id)?[:_\- ]*(\d+)\s*\}?", t, re.I)
    if m:
        tmdb_id = m.group(1)
        t = t.replace(m.group(0), " ")
    # 兜底清掉没被花括号包住的 tmdb 标记
    t = re.sub(r"\s*tmdb(?:[-_ ]?id)?[:_\- ]*\d+\s*", " ", t, flags=re.I)
    # 年份：优先 (2026)，其次点分/空格分隔的 2001 / .2003.
    m = re.search(r"\((\d{4})\)", t)
    if not m:
        m = re.search(r"(?:^|[\s.\[_\-])((?:19|20)\d{2})(?=[\s.\]_\-]|$)", t)
    year = m.group(1) if m else ""
    # 先去掉带网址 / 发布组的水印括号： 【高清影视之家发布 www.HDBTHD.com】
    t = re.sub(r"[\[【（(][^\]】）)]{0,60}(?:www\.|\.com|\.net|\.org|发布|字幕组|"
               r"压制|转载|原创)[^\]】）)]{0,60}[\]】）)]", " ", t)
    t = re.sub(r"(?:https?://|www\.)\S+", " ", t)
    # 无括号的发布组水印： “高清影视之家发布” “XX字幕组压制” 等（分享标题剥壳后会露出来）
    t = re.sub(r"^[^\u4e00-\u9fff]{0,10}[\u4e00-\u9fff]{0,12}"
               r"(?:发布|制作|压制|出品|字幕组|影视之家)\s*", " ", t)
    # 「演员个人作品合集」常见首字母分类前缀： “M-美国往事” “S -死亡中惊醒” → 去掉前缀
    # 仅当短横线前是 1-2 个字母（分类字母）时去掉，避免误伤 “X战警” “T-34” 这类真片名
    t = re.sub(r"^\s*[A-Za-z]{1,2}\s*[-–—]\s*(?=[\u4e00-\u9fff])", "", t)
    # 文件名形如 “[速度与激情2].2.Fast.2.Furious.2003...” → 取方括号里的中文片名
    # （仅文件名场景；分享总标题不能套用，否则会丢掉方括号外的真正片名主体）
    if not is_share_title:
        m0 = re.match(r"^\s*[\[【]([^\]】]{1,40})[\]】]", t)
        if m0 and re.search(r"[\u4e00-\u9fff]", m0.group(1)):
            t = m0.group(1)
    t = re.sub(r"[\(\[]?\s*(?:19|20)\d{2}\s*[\)\]]?", " ", t)
    # 去掉方括号里的音轨/字幕/版本说明： [国英多音轨+特效中文字幕] [60帧率版本][高码版]
    t = re.sub(r"[\[【][^\]】]{0,40}(?:音轨|字幕|双语|国语|粤语|特效|简繁|"
               r"内封|外挂|中字|帧率|高码|版本|修复|重制|Remux|HDR)"
               r"[^\]】]{0,40}[\]】]", " ", t, flags=re.I)
    t = re.sub(r"[\{\}\[\]]", " ", t)
    t = re.sub(r"\s*\d{3,4}[Pp].*$", "", t)          # 去掉 1080P 等画质后缀
    # 尾部序号： “(3)” “（2）” 之类（要在版本尾巴之前去掉，否则挡住匹配）
    t = re.sub(r"[\s（(]\d{1,2}[）)]\s*$", " ", t)
    # 去掉版本/画质尾巴： “- 4K REMUX 4K” “.BluRay.REMUX” 之类
    t = re.sub(r"[\s.\-–—]+(?:4k|uhd|remux|hdr\d*\+?|10bit|8bit|dolby|"
               r"web[\-\s]?dl|webrip|bluray|bdrip|dvd[rip]?|sdr|"
               r"v\d|proper|extended|imax)[\s.\-\w]*$",
               " ", t, flags=re.I)
    for k, v in ROMAN_MAP.items():
        t = t.replace(k, v)                          # Ⅰ → 1
    # 去压制组水印与音轨/字幕标签：￡CMCT死亡骑士、国英双语、中英字幕…
    t = re.sub(r"[￡＄$@].*$", "", t)
    t = re.sub(r"(国英双语|中英双语|中英字幕|国粤双语|双语字幕|内封字幕|外挂字幕|"
               r"国语|粤语|英语|中字|字幕|双语|简繁|特效|纯净|无水印|"
               r"国配|台配|导演剪辑|加长版|导剪版)", "", t, flags=re.I)
    # 英文名里的点号当空格：`The.Fast.and.the.Furious` → `The Fast and the Furious`。
    # 中文名不受影响（「速度与激情：特别行动」里的点是全角，不在替换范围）。
    if not re.search(r"[\u4e00-\u9fff]", t):
        t = t.replace(".", " ")
    t = re.sub(r"\s+", " ", t).strip(" .-_")
    return t or None, year or None, tmdb_id


def norm_title(s):
    """片名归一化：去空格/标点，用于判断两条记录是不是同一部片"""
    return re.sub(r"[\s\-_：:：·・.,，、]+", "", (s or "")).lower()


def looks_like_single_title(name):
    """这个名字看起来像「单独一部片」吗（用于判断目录能否当一部片的身份）"""
    if not name:
        return False
    t, y, tm = parse_share_title(name)
    if not t:
        return False
    if tm or y:
        return True
    lo = name.lower()
    if any(w in lo for w in CONTAINER_WORDS):
        return False
    if is_collection_title(name):
        return False
    return len(t) <= 30


# 蓝光原盘/播放器生成的元数据名，明显不是影片名
JUNK_TITLE_RE = re.compile(
    r"^(?:bdmv|certificate|auxdata|backup|movieobject|index|jar|playlist|"
    r"stream|clipinf|playlist|discinfo|bdjo|meta|00000|0+|mpls|clpi)\b",
    re.I,
)


def is_junk_title(name):
    """明显不是影片名的条目（蓝光原盘结构名 / 纯数字 / 纯符号）"""
    t = (name or "").strip()
    if not t:
        return True
    if re.fullmatch(r"[\d\W_]+", t):          # 纯数字/符号：00000、003
        return True
    if JUNK_TITLE_RE.match(t):
        return True
    if re.search(r"(?:TV Series|S\d{2}E\d{2}|Season\s*\d)", t, re.I):
        return True
    low = t.lower()
    if low in ("index", "movieobject", "auxdata", "bdmv", "backup", "certificate",
               "sound", "movieobject.bdmv", "sound.bdmv"):
        return True
    return False


def _cli_rename(args, page_meta, items, do_rename):
    """CLI：预览 / 执行重命名。返回 (plan, result)"""
    account = None
    need_account = do_rename
    if need_account:
        account = ensure_guangya_account()
        if account is None:
            return []
    plan = build_rename_plan(page_meta.get("deep_files") if page_meta else None,
                             page_meta.get("dir_nodes") if page_meta else None,
                             items=items)
    if args.rename_limit:
        plan = plan[:args.rename_limit]
    if not plan:
        print("\n✏️  没有需要重命名的项目（名称已规范）")
        return []
    total = len(plan)
    dirs = sum(1 for x in plan if x.get("is_dir"))
    print(f"\n✏️  重命名预览：共 {total} 项（📁 文件夹 {dirs} · 🎞 文件 {total - dirs}）")
    print(rename_plan_lines(plan, limit=None if total <= 40 else 25))
    if not do_rename:
        if not args.rename_dry_run:
            print("\n（未执行；加 --rename 实际修改）")
        return plan
    print(f"\n⏳ 开始重命名 {total} 项…")
    def _prog(i, n, rec):
        print(f"  [{i}/{n}] {'✔' if rec.get('status') == 'ok' else '✘'} "
              f"{rec['old']} → {rec['new']}"
              f"{'' if rec.get('status') == 'ok' else '  (' + rec.get('error', '') + ')'}")
    res = apply_rename_plan(account, plan, dry_run=False, progress=_prog)
    print(f"\n✏️  完成：成功 {res['changed']} · 失败 {res['failed']}")
    return plan


def _cli_guangya_account(args):
    """CLI：登录 / 贴 token / 查看状态 / 退出"""
    # 退出登录
    if args.logout_guangya:
        cfg = load_config()
        cfg.pop("guangya", None)
        save_config(cfg)
        print("✅ 已退出光鸭云盘登录（本地 token 已清除）")
        return

    # 贴 token 登录
    if args.login_token or (args.refresh_token and not args.login_guangya):
        acc = GuangyaAccount(access_token=args.login_token,
                             refresh_token=args.refresh_token)
        if not acc.token and acc.refresh_token_value:
            print("⏳ 用 refresh_token 换取 access_token…")
            try:
                acc.refresh_token()
            except Exception as e:              # noqa: BLE001
                print(f"❌ 换取失败：{e}")
                return
        name = ""
        try:
            info = acc.user_info()
            d = info.get("data") or info
            name = d.get("nickname") or d.get("name") or d.get("phone") or ""
        except Exception as e:                  # noqa: BLE001
            print(f"⚠ 校验用户信息失败（token 可能无效）：{e}")
        acc.save_to_config()
        print(f"✅ 登录态已保存{('：' + str(name)) if name else ''}")
        return

    # 短信登录
    if args.login_guangya:
        guangya_login_interactive()
        return

    # 查看状态
    acc = GuangyaAccount.from_config()
    if not acc.logged_in:
        print("❌ 未登录。执行：python3 share_poster.py --login-guangya")
        return
    name, days = "", guangya_token_days_left(acc)
    try:
        info = acc.user_info()
        d = info.get("data") or info
        name = d.get("nickname") or d.get("name") or d.get("phone") or ""
    except Exception as e:                      # noqa: BLE001
        print(f"⚠ 登录态可能已失效：{e}")
    print(f"✅ 已登录{('：' + str(name)) if name else ''}")
    if days is not None:
        print(f"   token 剩余约 {days:.1f} 天"
              f"{'（快过期了，执行时会自动续期）' if days < 3 else ''}")


def main():
    parser = argparse.ArgumentParser(
        description="影帖 v%s —— 给网盘分享链接，自动识别片名并产出发帖内容（支持 TMDB ID 自动抓取）" % VERSION
    )
    parser.add_argument("share_text", nargs="?", help="分享文本（可包含提取码）")
    parser.add_argument("--version", "-V", action="version", version=f"影帖（YingTie）{VERSION}")
    parser.add_argument("--key", "-k", help="TMDB v3 API Key")
    parser.add_argument("--title", "-t", help="片名（用于 TMDB 检索，缺省则尝试从分享页自动识别）")
    parser.add_argument("--year", "-y", help="年份（精确匹配）", type=int)
    parser.add_argument("--type", choices=["movie", "tv"], help="强制类型")
    parser.add_argument("--quality", help="质量字段（自动模式会按体积推断，可用本参数覆盖）")
    parser.add_argument("--size", help="大小字段（自动模式会用分享页真实体积）")
    parser.add_argument("--episodes", help="手动指定集数信息")
    parser.add_argument("--actors", help="手动指定演员")
    parser.add_argument("--synopsis", help="手动指定简介")
    parser.add_argument("--poster-url", help="手动指定海报 URL")
    parser.add_argument("--poster-size", default="w780")
    parser.add_argument("-o", "--output-dir", default=".", help="输出目录")
    parser.add_argument("--image", dest="make_image", action="store_true", default=True,
                        help="生成配图（默认）")
    parser.add_argument("--no-image", dest="make_image", action="store_false",
                        help="只生成文本，不出图")
    parser.add_argument("--image-mode", choices=["long", "poster", "none"], default=None,
                        help="出图方式: long=长图(默认) / poster=仅海报或封面 / none=无图")
    parser.add_argument("--image-width", type=int, default=720, help="长图宽度")
    parser.add_argument("--font-size", type=int, default=28, help="正文字号")
    parser.add_argument("--no-net", action="store_true",
                        help="禁用分享页/TMDB 网页抓取（只用手动/Key 数据）")
    parser.add_argument("--save-config", action="store_true", help="保存当前 key 到 ~/.share_poster.json")

    # ---- 光鸭云盘账号 & 重命名 ----
    parser.add_argument("--login-guangya", action="store_true",
                        help="登录光鸭云盘（短信验证码，登录态存 ~/.share_poster.json）")
    parser.add_argument("--login-token", metavar="ACCESS_TOKEN",
                        help="直接贴 access_token 完成登录（配 --refresh-token 可自动续期）")
    parser.add_argument("--refresh-token", metavar="REFRESH_TOKEN",
                        help="配合 --login-token 使用；只给它则用刷新令牌换新 token")
    parser.add_argument("--guangya-status", action="store_true",
                        help="查看光鸭云盘登录状态")
    parser.add_argument("--logout-guangya", action="store_true",
                        help="退出光鸭云盘登录（清除本地 token）")
    parser.add_argument("--rename", action="store_true",
                        help="按规范格式重命名分享内的文件夹与视频（需先登录光鸭云盘）")
    parser.add_argument("--rename-dry-run", action="store_true",
                        help="只预览重命名结果，不实际修改")
    parser.add_argument("--rename-limit", type=int, default=None,
                        help="只处理前 N 项（分批执行，便于逐步确认）")
    parser.add_argument("--show-rename-plan", action="store_true",
                        help="生成帖子前，先打印重命名预览")
    args = parser.parse_args()

    # 出图模式：显式 --image-mode 优先；--no-image 兼容为 none
    mode = args.image_mode or ("none" if not args.make_image else "long")
    want_pic = mode in ("long", "poster")

    cfg = load_config()
    api_key = args.key or cfg.get("tmdb_api_key") or os.environ.get("TMDB_API_KEY", "")

    # 0. 光鸭云盘账号（登录 / 贴 token / 看状态 / 退出）
    if args.login_guangya or args.login_token or args.refresh_token or \
            args.guangya_status or args.logout_guangya:
        _cli_guangya_account(args)
        return

    # 1. 读分享文本
    if not args.share_text:
        print("请粘贴分享文本（链接 + 可选提取码）：")
        try:
            args.share_text = input().strip()
        except EOFError:
            parser.print_help()
            return

    share = ShareParser.parse(args.share_text)
    if not share:
        sys.stderr.write("未能识别任何网盘链接。\n")
        return
    print(f"✔ 网盘:{share['cloud']}")
    print(f"  链接:{share['url']}")
    if share.get("code"):
        print(f"  提取码:{share['code']}")

    # 2. 自动抓分享页（无 --title 时）
    page_meta = None
    if not args.title and not args.no_net:
        fetcher = CloudShareFetcher()
        page_meta = fetcher.fetch(args.share_text)
        if page_meta and page_meta.get("title"):
            print(f"\n🔎 分享页自动识别")
            print(f"  标题: {page_meta['title']}")
            if page_meta.get("file_count"):
                print(f"  文件: {page_meta['file_count']} 个，"
                      f"共 {human_size(page_meta.get('size_bytes', 0))}")
            if not args.size and page_meta.get("size_bytes"):
                args.size = human_size(page_meta["size_bytes"])
            if not args.quality:
                args.quality = guess_quality(page_meta.get("size_bytes"))
        else:
            print("\n（无法自动读取分享页，改用片名搜索流程）")

    # 3. 解析片名线索：--title / 分享页标题里的 {tmdb-xxx} 或“片名 (年份)”
    raw_name = args.title or ""
    share_year = None
    tmdb_id = None
    if page_meta and page_meta.get("title"):
        raw_name, share_year, tmdb_id = parse_share_title(page_meta["title"],
                                                           is_share_title=True)
        if raw_name:
            print(f"  片名: {raw_name} {('(' + share_year + ')') if share_year else ''}"
                  f"{('  [tmdb-' + tmdb_id + ']') if tmdb_id else ''}")
    if not raw_name:
        raw_name = args.title or ""

    # 3.5 合集分支：分享目录里递归列出多部不同影片 → 直接出合集帖
    if not args.title:
        items = collect_media_items(page_meta.get("deep_files") if page_meta else None,
                                    page_meta.get("dir_nodes") if page_meta else None)
        if len(items) >= 2:
            print(f"\n🧩 检测到合集（{len(items)} 部）")
            if any(not it.get("tmdb") for it in items):
                print("  ⏳ 正在补全 TMDB 信息…")
                enrich_items_with_tmdb(items)

            # ---- 发帖前重命名（需登录光鸭云盘）----
            if args.rename or args.rename_dry_run or args.show_rename_plan:
                _cli_rename(args, page_meta, items, do_rename=args.rename)
            for it in items[:6]:
                print(f"   - {it['title']} {('(' + str(it['year']) + ')') if it.get('year') else ''}"
                      f"{(' [tmdb-' + it['tmdb'] + ']') if it.get('tmdb') else ''}"
                      f"{(' [' + it['quality'] + ']') if it.get('quality') else ''}")
            if len(items) > 6:
                print(f"   … 其余 {len(items) - 6} 部")
            collection_text = build_collection_text(
                share, raw_name or "影视合集", items,
                quality=args.quality,   # 用户显式指定优先，否则从文件名推断
                size=args.size or human_size(page_meta.get("size_bytes")),
                synopsis=args.synopsis,
            )
            # 输出合集结果
            print("\n" + "─" * 56)
            print(collection_text)
            print("─" * 56 + "\n")
            out_dir = Path(args.output_dir).expanduser()
            out_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w\-\u4e00-\u9fa5]", "_", raw_name or "合集")[:50]
            txt_path = out_dir / f"{safe}_share.txt"
            txt_path.write_text(collection_text, encoding="utf-8")
            print(f"📄 文本已保存:{txt_path}")
            cover = None
            if want_pic:
                cover, cover_src = pick_collection_cover(
                    items, raw_name or "影视合集")
                print(f"🖼  合集封面来源:{cover_src}")
            if mode == "long":
                try:
                    maker = PosterMaker(width=args.image_width, font_size=args.font_size)
                    img = maker.render(collection_text, poster_img=cover,
                                       title=(raw_name or "影视合集"))
                    img_path = out_dir / f"{safe}_share.jpg"
                    img.save(img_path, quality=92)
                    print(f"🖼  长图已保存:{img_path}")
                except Exception as e:
                    sys.stderr.write(f"[生成图片失败] {e}\n")
            elif mode == "poster":
                if cover is None:
                    sys.stderr.write("⚠ 合集封面生成失败，仅输出文本\n")
                else:
                    data = poster_to_jpeg(cover)
                    cov_path = out_dir / f"{safe}_cover.jpg"
                    with open(cov_path, "wb") as f:
                        f.write(data)
                    print(f"🖼  合集封面已保存:{cov_path} "
                          f"({len(data) / 1024:.0f}KB)")
            if api_key and (args.save_config or not cfg.get("tmdb_api_key")):
                save_config({**cfg, "tmdb_api_key": api_key})
            print("\n完成 ✅")
            return

    # 4. 采集影视信息
    info = {"_manual": True}
    poster_img = None
    poster_url = args.poster_url or ""
    title_for_filename = raw_name or "share"

    tmdb_web = None if args.no_net else TMDBWebClient()

    if tmdb_id and tmdb_web:
        # 4a. 分享标题自带 TMDB ID → 直接抓详情页（无需 Key）
        print(f"\n⏳ 正在 TMDB 抓取 #{tmdb_id} 的资料…")
        got = tmdb_web.detail_by_id(tmdb_id)
        if got:
            info = got["info"]
            info["_type"] = got["type"]
            poster_url = poster_url or got.get("poster_url") or ""
            title_for_filename = info.get("title") or raw_name
            print(f"  命中: {title_for_filename} ({info.get('year','')}) [{got['type']}]")
        else:
            print("  ⚠ TMDB 未找到该 ID，回退搜索流程")

    if info.get("_manual") and raw_name and not args.no_net:
        # 4b. 没有 TMDB ID：站内搜索拿候选
        if api_key:
            tmdb = TMDBClient(api_key)
            media_order = [args.type] if args.type else ["tv", "movie"]
            for mt in media_order:
                results = tmdb.search(raw_name, media_type=mt, year=args.year or share_year)
                chosen = pick_one(results, f"{'电视剧' if mt == 'tv' else '电影'}")
                if chosen:
                    detail = tmdb.detail(mt, chosen["id"])
                    if detail:
                        detail["_type"] = mt
                        info = detail
                        title_for_filename = detail.get("title") or detail.get("name") or raw_name
                        if not poster_url and want_pic:
                            poster_img = tmdb.download_image(detail.get("poster_path"), args.poster_size)
                        break
        elif info.get("_manual"):
            print(f"\n⏳ 在 TMDB 站内搜索 “{raw_name}”…")
            cands = tmdb_web.search_by_name(raw_name, year=share_year)
            if cands:
                for i, c in enumerate(cands[:8], 1):
                    print(f"  {i}. {c['title']}  ({'电视剧' if c['type']=='tv' else '电影'})")
                print("\n请选择序号（直接回车 = 选第 1 条，0 = 跳过）:")
                try:
                    raw = input("> ").strip()
                except EOFError:
                    raw = ""
                idx = int(raw) if raw.isdigit() else 1
                if 1 <= idx <= len(cands):
                    chosen = cands[idx - 1]
                    got = tmdb_web.detail_by_id(chosen["id"])
                    if got:
                        info = got["info"]
                        info["_type"] = got["type"]
                        poster_url = poster_url or got.get("poster_url") or ""
                        title_for_filename = info.get("title") or raw_name

    if info.get("_manual"):
        # 4c. 全部失败 → 手动补信息
        if not raw_name:
            print("\n请输入片名（无法自动识别时手动填写）：")
            try:
                raw_name = input().strip()
            except EOFError:
                raw_name = ""
        if not raw_name:
            sys.stderr.write("未提供片名且无法自动识别，中止。"
                             "可加 --title 手动指定，或 --no-net 关闭网页抓取。\n")
            return
        info = {"title": raw_name, "name": raw_name,
                "release_date": "", "first_air_date": "", "_manual": True}
        title_for_filename = raw_name or "share"

    if args.actors:
        info["actors"] = args.actors
    if args.synopsis:
        info["_manual_synopsis"] = args.synopsis
    if args.type and "_type" not in info:
        info["_type"] = args.type
    if share_year and not info.get("release_date") and not info.get("first_air_date"):
        info["release_date"] = f"{share_year}-01-01"

    # 下载海报（TMDB 网页模式返回的是完整 URL）
    if poster_img is None and poster_url and want_pic:
        try:
            r = requests.get(poster_url, timeout=20)
            r.raise_for_status()
            poster_img = Image.open(io.BytesIO(r.content))
            poster_img.load()
        except Exception as e:
            sys.stderr.write(f"[海报下载失败] {e}\n")

    # 5. 合成帖子
    gen = PostGenerator()
    text = gen.build(
        info, share,
        quality=args.quality or "1080P WEB-DL X264 AAC",
        size=args.size or "未知大小",
        episodes=args.episodes,
        synopsis=args.synopsis,
    )

    print("\n" + "─" * 56)
    print(text)
    print("─" * 56 + "\n")

    # 6. 输出
    out_dir = Path(args.output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r"[^\w\-\u4e00-\u9fa5]", "_", title_for_filename)[:50]
    txt_path = out_dir / f"{safe_title}_share.txt"
    txt_path.write_text(text, encoding="utf-8")
    print(f"📄 文本已保存:{txt_path}")

    if mode == "long":
        try:
            maker = PosterMaker(width=args.image_width, font_size=args.font_size)
            img = maker.render(text, poster_img=poster_img,
                               title=info.get("title") or info.get("name") or "")
            img_path = out_dir / f"{safe_title}_share.jpg"
            img.save(img_path, quality=92)
            print(f"🖼  长图已保存:{img_path}")
        except Exception as e:
            sys.stderr.write(f"[生成图片失败] {e}\n")
    elif mode == "poster":
        if poster_img is None:
            sys.stderr.write("⚠ 未获取到海报，无法输出海报文件"
                             "（可用 --poster-url 手动指定海报地址）\n")
        else:
            data = poster_to_jpeg(poster_img)
            poster_path = out_dir / f"{safe_title}_poster.jpg"
            with open(poster_path, "wb") as f:
                f.write(data)
            print(f"🖼  海报已保存:{poster_path} ({len(data) / 1024:.0f}KB)")

    # 7. 配置
    if api_key and (args.save_config or not cfg.get("tmdb_api_key")):
        save_config({**cfg, "tmdb_api_key": api_key})
        print(f"🔑 API Key 已保存到 {config_path()}")

    print("\n完成 ✅")


# --- 合集识别与处理 ----------------------------------------------------
# 分享里常见的资源形态：
#   单部：1 个视频文件（可带字幕/封面）→ 现有单部流程
#   合集：多部不同影片（周星驰合集 / 黑客帝国1-4 / 指环王三部曲）→ 本组函数
# 子文件名通常形如：黑客帝国 (1999) {tmdb-603} [1080p H.265].mkv

NON_MEDIA_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".lrc",
                  ".txt", ".nfo", ".jpg", ".jpeg", ".png", ".gif", ".bmp"}
# .bdmv/.mpls/.clpi 是蓝光原盘的索引文件，不是影片本体
MEDIA_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".wmv",
              ".flv", ".rmvb", ".m4v", ".webm", ".iso"}


def parse_quality_from_name(name):
    """从文件名提取画质标签，如 [1080p H.265 DD 2.0] → '1080P HEVC'"""
    lo = name.lower()
    tags = []
    if re.search(r"\b(2160p|4k|uhd|8k)\b", lo):
        tags.append("2160P")
    elif re.search(r"\b1080p\b", lo):
        tags.append("1080P")
    elif re.search(r"\b720p\b", lo):
        tags.append("720P")
    if re.search(r"\b(hevc|h[-_. ]?265|x265)\b", lo):
        tags.append("HEVC")
    elif re.search(r"\b(h[-_. ]?264|avc|x264)\b", lo):
        tags.append("H.264")
    if re.search(r"\bremux\b", lo):
        tags.append("REMUX")
    elif re.search(r"\b(bluray|bdrip|蓝光原盘)\b", lo):
        tags.append("蓝光")
    elif re.search(r"\bweb[-_. ]?dl\b", lo):
        tags.append("WEB-DL")
    elif re.search(r"\bwebrip\b", lo):
        tags.append("WEBRip")
    if re.search(r"\b(dvdr?|dvdrip)\b", lo):
        tags.append("DVD")
    if re.search(r"\bhdr10?\+?\b|\b(dv|dolby.?vision)\b", lo):
        tags.append("HDR")
    return " ".join(tags) if tags else ""


def _identity_score(c):
    """给一组 (片名, 年份, tmdb) 打分：带 TMDB ID / 年份 / 中文片名的更可信"""
    t, y, tm = c
    s = 0
    if tm:
        s += 4
    if y:
        s += 2
    if t:
        if re.search(r"[\u4e00-\u9fff]", t):
            s += 2
        if len(t) <= 40:
            s += 1
        # 纯英文原名（Fast.and.Furious）仍可用，只是优先级低
    return s


def _pick_identity(self_info, dir_info):
    """融合「文件名解析结果」和「父目录名解析结果」：
    片名优先取中文（目录名通常是中文片名，文件名常是英文原名），
    年份 / TMDB ID 取两边任意非空值。
    """
    a = tuple(self_info) if self_info else ("", None, None)
    b = tuple(dir_info) if dir_info else ("", None, None)
    if not a[0] and not b[0]:
        return "", None, None
    if not a[0]:
        return b
    if not b[0]:
        return a

    def cjk(x):
        return bool(re.search(r"[\u4e00-\u9fff]", x or ""))

    if cjk(b[0]) and not cjk(a[0]):
        title = b[0]
    elif cjk(a[0]) and not cjk(b[0]):
        title = a[0]
    else:
        title = (a if _identity_score(a) >= _identity_score(b) else b)[0]
    return title, (a[1] or b[1]), (a[2] or b[2])


def _build_identity_maps(dir_nodes, deep_files):
    """把分享里出现过的「片名身份」全量收集起来，供文件层反查中文名 / TMDB。

    动机：`_pick_identity` 只能从「父目录」继承身份，一旦文件直接挂在分享根目录
    （父目录名是「XX 合集」这种合集名，解析不出单片身份），中文片名和 TMDB 就全丢了，
    重命名后变成 `The.Fast.and.the.Furious (2001)....mkv` 这种半截名字。

    于是这里建两张表：
      map_year       year      → (title, year, tmdb)      年份在整份分享里唯一时最可靠
      map_norm_title 归一化片名 → (title, year, tmdb)      英文原名 ↔ 中文名的桥

    只在「该年份/片名在整份分享里唯一」时才写入，避免把 Reloaded / Revolutions
    这类同年的不同片子张冠李戴。
    """
    def _ident(name):
        t, y, tm = parse_share_title(name or "")
        if not t:
            return None
        return (t, int(y) if (y or "").isdigit() else None, tm)

    pool = []
    for d in (dir_nodes or []):
        nm = (d.get("fileName") or "").strip()
        if is_junk_title(nm) or is_collection_title(nm):
            continue
        if not looks_like_single_title(nm):
            continue
        idt = _ident(nm)
        if idt:
            pool.append(idt)
    for f in (deep_files or []):
        nm = (f.get("fileName") or "").strip()
        if (f.get("ext") or Path(nm).suffix or "").lower() not in MEDIA_EXTS:
            continue
        idt = _ident(Path(nm).stem)
        if idt and idt[2]:          # 文件名自带 tmdb 的才可信
            pool.append(idt)

    def _pick(a, b):
        """两个身份里挑信息更全的：有中文名 > 有 tmdb > 名字里带序号。

        同一个年份有多条记录时优先用带序号的（「速度与激情 1」胜过「速度与激情」，
        否则第一部会被写成没有序号的裸名，和整个系列对不上）。
        """
        def score(x):
            base, year = x[0], x[1]
            has_seq = 1 if re.search(r"\d+\s*$", base or "") else 0
            return (1 if re.search(r"[\u4e00-\u9fff]", base or "") else 0,
                    1 if x[2] else 0, has_seq, 1 if year else 0)
        return a if score(a) >= score(b) else b

    by_year, by_title = {}, {}
    year_cnt, title_cnt = {}, {}
    for t, y, tm in pool:
        k = norm_title(t)
        if y:
            year_cnt[y] = year_cnt.get(y, 0) + 1
        if k:
            title_cnt[k] = title_cnt.get(k, 0) + 1
    for t, y, tm in pool:
        k = norm_title(t)
        if y and year_cnt.get(y) == 1:
            by_year[y] = _pick(by_year[y], (t, y, tm)) if y in by_year else (t, y, tm)
        if k and title_cnt.get(k) == 1:
            by_title[k] = _pick(by_title[k], (t, y, tm)) if k in by_title else (t, y, tm)
    return by_year, by_title


def collect_media_items(deep_files, dir_nodes=None):
    """把递归列出的文件整理成影视条目（去重：同片不同后缀/分卷只算一个）。
    返回 [{file, name, title, year, tmdb, quality, ext, size}]，已按年份排序。
    过滤掉字幕 / 封面 / 文本等附属文件。

    文件大小字段兼容 fileSize / size；片名优先取「父目录名」里的中文片名与
    {tmdb-id}（很多合集是 每部片一个文件夹，里面才是英文原名的 mkv）。
    """
    map_year, map_title = _build_identity_maps(dir_nodes, deep_files)
    raw = []
    for f in deep_files or []:
        name = (f.get("fileName") or "").strip()
        ext = (f.get("ext") or Path(name).suffix or "").lower()
        if ext not in MEDIA_EXTS:
            continue
        base = Path(name).stem
        # 同片分卷：名字主体去掉 part/cd/disc/分卷 数字后再归并
        key = re.sub(r"[-_ .]?(part|cd|disc|pt|vol|分卷|上下)?[-_ .]?\d{1,2}$", "", base, flags=re.I)
        title, year, tmdb = _pick_identity(
            parse_share_title(base),
            (f.get("_dir_title"), f.get("_dir_year"), f.get("_dir_tmdb")),
        )
        # 父目录没给出身份（或给得残缺）时，拿全量映射表兜底，
        # 补上中文片名 / 年份 / tmdb-id
        if not title or not tmdb:
            _cand = None
            if year and str(year).isdigit():
                _cand = map_year.get(int(year))
            if _cand is None:
                _cand = map_title.get(norm_title(title or base))
            if _cand:
                title, year, tmdb = _pick_identity(
                    (title, year, tmdb), _cand)
        # 画质：文件名优先，没有再看父目录名（如 “- 4K REMUX”）
        qual = parse_quality_from_name(name)
        if not qual and f.get("_dir_title"):
            qual = parse_quality_from_name(str(f.get("_dir_title")))
        raw.append({
            "file": name, "ext": ext,
            "size": f.get("fileSize") or f.get("size") or 0,
            "thumb": f.get("thumbnail") or "",
            "quality": qual,
            "key": (key or base).lower(), "title": title or base,
            "year": int(year) if (year or "").isdigit() else None,
            "tmdb": tmdb,
            "fileId": f.get("fileId"),
            "fileIds": [f.get("fileId")] if f.get("fileId") else [],
        })

    # 目录兜底：接口只返回到目录层（或文件全是非标准后缀）时，
    # 用「看起来像一部片」的目录名直接当条目
    if not raw and dir_nodes:
        for d in dir_nodes:
            name = (d.get("fileName") or "").strip()
            if is_junk_title(name):
                continue
            t, y, tm = parse_share_title(name)
            if not t:
                continue
            looks_media = bool(tm or y or parse_quality_from_name(name))
            if not looks_media:
                continue
            raw.append({
                "file": name, "ext": "", "size": 0, "thumb": "",
                "quality": parse_quality_from_name(name),
                "key": t.lower(), "title": t,
                "year": int(y) if (y or "").isdigit() else None,
                "tmdb": tm,
            })

    # 去重：同一 (key, year)；同片多版本按 (标题, 年份) 再并一次
    seen, items = {}, []
    for it in raw:
        if is_junk_title(it.get("title")):
            continue                    # 蓝光原盘结构名 / 纯数字，不是影片
        uid = (it["key"], it["year"])
        if uid in seen:
            old = seen[uid]
            old["ext"] = old["ext"] + "+" + it["ext"] if it["ext"] else old["ext"]
            old["size"] = max(old["size"] or 0, it["size"] or 0)
            old["thumb"] = old["thumb"] or it["thumb"]
            if not old["quality"]:
                old["quality"] = it["quality"]
            if not old["tmdb"]:
                old["tmdb"] = it["tmdb"]
            for _fid in (it.get("fileIds") or []):
                if _fid and _fid not in old.setdefault("fileIds", []):
                    old["fileIds"].append(_fid)
            continue
        seen[uid] = it
        items.append(it)
    # 同片不同版本（目录名重复）再归一：按 (标题, 年份)
    merged, mseen = [], {}
    for it in items:
        uid = ((it["title"] or "").strip().lower(), it["year"])
        if uid in mseen:
            old = mseen[uid]
            old["size"] = max(old["size"] or 0, it["size"] or 0)
            if not old["tmdb"]:
                old["tmdb"] = it["tmdb"]
            continue
        mseen[uid] = it
        merged.append(it)
    # 只拿到「一部片一个文件夹」但没扫到视频文件时，用目录名补齐片单
    # （要求该目录子树里确实有视频文件，避免把只有截图的空壳目录算成一部）
    parents_with_media = set()
    for f in deep_files or []:
        nm = (f.get("fileName") or "")
        if (f.get("ext") or Path(nm).suffix or "").lower() in MEDIA_EXTS:
            if f.get("parentId"):
                parents_with_media.add(f["parentId"])
            for pid in (f.get("fullParentIds") or "").split("/"):
                if pid:
                    parents_with_media.add(pid)
    strict = bool(raw)          # 一个视频都没扫到时放宽，纯靠目录名兜底

    # ===== 中文文件夹 → 视频 反哺匹配 =====
    # 常见坑：上传者把多部片错误塞进同一个「片名(年份){tmdb}」文件夹，
    # 导致所有视频都继承到同一个错误的父目录身份；真正带正确 tmdb 的中文文件夹
    # 反而是平级的空壳目录（只放截图/字幕）。这里用「tmdb-id / 年份+序号 / 年份唯一」
    # 把中文文件夹的权威身份反哺给视频条目，保证片单中文名 + tmdb 都正确。
    cn_folders = []
    for d in dir_nodes or []:
        name = (d.get("fileName") or "").strip()
        if is_junk_title(name):        # 蓝光原盘结构名（BDMV/STREAM/AUXDATA…）
            continue
        if not looks_like_single_title(name):
            continue
        t, y, tm = parse_share_title(name)
        if not t:
            continue
        year = int(y) if (y or "").isdigit() else None
        cn_folders.append({
            "title": t, "year": year, "tmdb": tm,
            "part": extract_part(name),
            "quality": parse_quality_from_name(name),
            "fileId": d.get("fileId"),
        })

    for cf in cn_folders:
        best, best_score = None, 0
        for it in merged:
            if it.get("_cn_matched"):
                continue
            score = 0
            it_part = extract_part(it["title"])
            if cf["tmdb"] and it.get("tmdb") == cf["tmdb"]:
                score = 100
            elif cf["year"] and it.get("year") == cf["year"] and cf["part"] and it_part == cf["part"]:
                score = 80          # 同年 + 同序号（如 Reloaded/Revolutions 同为 2003）
            elif cf["year"] and it.get("year") == cf["year"] and cf["part"] is None and it_part is None:
                score = 70          # 同年且都无序号（如 1999 唯一一部）
            elif cf["part"] and it_part == cf["part"] and (cf["year"] is None or it.get("year") == cf["year"]):
                score = 60          # 仅序号匹配（中文文件夹缺年份时）
            if score >= 60:
                if score > best_score:
                    best_score, best = score, it
        if best is not None:
            best["_cn_matched"] = True
            if cf["tmdb"] and not best["tmdb"]:
                best["tmdb"] = cf["tmdb"]
            if re.search(r"[\u4e00-\u9fff]", cf["title"]) and \
                    not re.search(r"[\u4e00-\u9fff]", best["title"] or ""):
                best["title"] = cf["title"]
            if not best["quality"]:
                best["quality"] = cf["quality"]

    # 真正缺视频的目录才作为新条目补进片单（避免把已匹配的中文文件夹重复计入）
    def _base_series(t):
        return re.sub(r"\d+$", "", norm_title(t) or "")   # 去尾部序号，比系列名
    for cf in cn_folders:
        if any((i.get("tmdb") == cf["tmdb"] and cf["tmdb"])
               or (i["year"] == cf["year"] and extract_part(i["title"]) == cf["part"]
                   and cf["year"] is not None)
               or norm_title(i["title"]) == norm_title(cf["title"])
               or _base_series(i["title"]) == _base_series(cf["title"])
               for i in merged):
            continue
        # 该目录子树里确有视频时才补（strict 模式且有身份标识者放宽）
        has_identity = bool(cf["tmdb"] or cf["year"])
        if strict and not has_identity and cf["fileId"] not in parents_with_media:
            continue
        merged.append({
            "file": cf["title"], "ext": "", "size": 0, "thumb": "",
            "quality": cf["quality"],
            "key": cf["title"].lower(), "title": cf["title"],
            "year": cf["year"], "tmdb": cf["tmdb"],
        })

    # 同片跨语言/多版本再归一。谨慎合并，避免把同年的两部不同片子并成一部：
    #   · 语言不同（中文名 vs 英文原名）→ 视为同一部，合并
    #   · 标题归一化后相同或互为子串 → 合并
    #   · 其余同年条目 → 保留（如 The.Matrix.Reloaded / Revolutions 都是 2003）
    def _cjk(x):
        return bool(re.search(r"[\u4e00-\u9fff]", x or ""))

    final, by_year = [], {}
    for it in merged:
        y = it["year"]
        old = by_year.get(y) if y else None
        if old is not None:
            can_merge = (_cjk(old["title"]) != _cjk(it["title"])
                         or norm_title(old["title"]) == norm_title(it["title"])
                         or norm_title(old["title"]) in norm_title(it["title"])
                         or norm_title(it["title"]) in norm_title(old["title"]))
            if not can_merge:
                final.append(it)
                continue
            old["size"] = max(old["size"] or 0, it["size"] or 0)
            if not old["tmdb"]:
                old["tmdb"] = it["tmdb"]
            if not old["quality"]:
                old["quality"] = it["quality"]
            # 中文片名优先
            if not _cjk(old["title"]) and _cjk(it["title"]):
                old["title"] = it["title"]
            continue
        if y:
            by_year[y] = it
        final.append(it)
    final.sort(key=lambda x: (x["year"] or 9999, x["title"]))
    return final


def enrich_items_with_tmdb(items, max_items=40, workers=4, verbose=False):
    """给没有 tmdb-id 的条目按「片名 + 年份」搜 TMDB 补 id 与中文名。

    演员个人作品合集（如「詹妮弗·康纳利 42 部」）的分享里通常不带 {tmdb-id}，
    会导致封面只能掉到「剧照拼图」兜底、片单也拿不到中文名。这里统一补全。
    命中后：
      · 填 tmdb id（供封面直取官方海报）
      · 片名是英文原名 → 换回 TMDB 的中文名
      · 年份缺失 → 用 TMDB 年份补
    只在「标题疑似外文」或「缺 tmdb」时才查，避免对已完整的合集浪费请求。
    """
    if not items:
        return items
    web = TMDBWebClient()
    todo = [it for it in items if not it.get("tmdb")
            and not is_junk_title(it.get("title"))][:max_items]
    if not todo:
        return items

    def _lookup(it):
        title = it.get("title") or ""
        if not title:
            return None
        # 候选关键词：标题本身 → 去尾部序号 → 父目录里的英文原名（如有）
        names = [title, re.sub(r"[\s\-_]*\d+\s*$", "", title).strip()]
        alt = it.get("file") or ""
        # 从原始文件名里提取英文核心名（The.Hot.Spot.1990... → The Hot Spot）
        m = re.search(r"^([A-Za-z][A-Za-z0-9'&\s]{2,60}?)[\s.]"
                      r"(?:19|20)\d{2}", alt)
        if m:
            names.append(m.group(1).replace(".", " ").strip())
        cands = []
        for n in dict.fromkeys([x for x in names if x]):
            try:
                cands = web.search_by_name(n) or []
            except Exception:      # noqa: BLE001
                cands = []
            if cands:
                break
        if not cands:
            return None
        # 优先挑年份一致的候选（逐个查详情页核对年份）
        yr = it.get("year")
        if yr:
            for c in cands[:4]:
                got = web.detail_by_id(c["id"])
                if got and str((got.get("info") or {}).get("year") or "")[:4] == str(yr):
                    info = got.get("info") or {}
                    return {
                        "tmdb": str(c["id"]),
                        "title": info.get("title") or title,
                        "year": int(yr),
                    }
        # 没有年份线索 / 年份都对不上 → 取第一条（搜索相关性最高）
        got = web.detail_by_id(cands[0]["id"])
        if not got:
            return None
        info = got.get("info") or {}
        return {
            "tmdb": str(cands[0]["id"]),
            "title": info.get("title") or title,
            "year": it.get("year") or (
                int(str(info.get("year"))) if str(info.get("year", "")).isdigit() else None),
        }

    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            hits = list(ex.map(_lookup, todo))
    except Exception:              # noqa: BLE001
        hits = []
    filled = 0
    for it, hit in zip(todo, hits):
        if not hit:
            continue
        if hit.get("tmdb") and not it.get("tmdb"):
            it["tmdb"] = hit["tmdb"]
        # 英文原名 → 换中文名（保持信息更规范）
        ht = hit.get("title") or ""
        if ht and re.search(r"[\u4e00-\u9fff]", ht) and \
                not re.search(r"[\u4e00-\u9fff]", it.get("title") or ""):
            it["title"] = ht
        if not it.get("year") and hit.get("year"):
            it["year"] = hit["year"]
        filled += 1
    if verbose:
        sys.stderr.write(f"[TMDB 补全] {filled}/{len(todo)} 条命中\n")
    return items


def is_collection_title(name):
    """标题里是否带明显的合集信号（含单部标题里的合集描述词）"""
    if not name:
        return False
    return bool(re.search(r"合集|合辑|全集|全季|系列|三部曲|四部曲|五部曲|"
                          r"打包|收藏版|\d\s*[-—~至]\s*\d|一至|1\s*[-—~]\s*4\b", name))


def _quality_mix(items):
    """合集画质推断：全一致用该标签；分辨率混搭显示“1080P/2160P 混合”"""
    qs = [i["quality"] for i in items if i.get("quality")]
    if not qs:
        return ""
    if len(set(qs)) == 1:
        return qs[0]

    def res(q):
        return ("2160P" if "2160" in q else "1080P" if "1080" in q
                else "720P" if "720" in q else "4K" if "4K" in q else "HD")
    reses = sorted({res(q) for q in qs})
    if len(reses) > 1:
        return "/".join(reses) + " 混合"
    return qs[0]


def build_collection_text(share, series_name, items, quality=None, size=None, synopsis=None):
    """生成合集分享帖文本。quality 缺省时优先取子文件里统一的画质标签。"""
    if not quality:
        quality = _quality_mix(items) or "1080P"
    size = size or "未知大小"
    years = sorted({i["year"] for i in items if i["year"]})
    span = ""
    if len(years) >= 2 and years[-1] - years[0] > 0:
        span = f" ({years[0]}-{years[-1]})"
    elif len(years) == 1:
        span = f" ({years[0]})"

    E = PostGenerator.EMOJI
    lines = [f"{E['title']} 名称:{series_name}{span}"]
    lines.append(f"{E['quality']} 质量:{quality} [{size}]")
    lines.append(f"{E['episodes']} 集数:共 {len(items)} 部")

    # 片单
    shown = 0
    for it in items:
        shown += 1
        if shown > 30:
            lines.append(f"    …其余 {len(items) - shown + 1} 部见分享目录")
            break
        tag = it["quality"]
        tm = f" {{tmdb-{it['tmdb']}}}" if it.get("tmdb") else ""
        y = f" ({it['year']})" if it.get("year") else ""
        lines.append(f"   {shown}.{it['title']}{y}{tm}"
                     + (f" [{tag}]" if tag else ""))

    if synopsis:
        syn = synopsis.strip().replace("\n", " ")
        lines.append(f"{E['synopsis']} 简介:{syn}")
    else:
        lines.append(f"{E['synopsis']} 简介:收录 {len(items)} 部完整影视文件，明细见上方片单")

    lines.append(f"{E['download']} 下载地址:")
    lines.append(share["url"])
    if share.get("code"):
        lines.append(f"\n提取码: {share['code']}")
    if share.get("cloud"):
        lines.append(f"（来源:{share['cloud']}）")
    return "\n".join(lines)


# --- 一键生成（供 CLI / Web 复用） --------------------------------------
def generate(share_text, quality=None, size=None, title=None, pick=1,
             include_image=True, width=720, font_size=28,
             cloud=None, verbose=True, image_mode=None,
             rename=False, account=None, page_meta=None):
    """输入分享文本，走完整自动链路，返回：
    {ok, text, title, image_b64 (长图PNG) | cover_b64(合集封面JPG),
     poster_jpg_b64 / poster_png_b64 (仅 image_mode='poster'),
     cloud, manual, error}

    image_mode: None(默认,沿用 include_image) / 'long' 长图 / 'poster' 仅海报 / 'none' 无图
    """
    mode = image_mode or ("long" if include_image else "none")
    result = {"ok": False, "text": "", "title": "", "image_b64": "",
              "cover_b64": "", "poster_jpg_b64": "", "poster_png_b64": "",
              "cloud": "", "manual": False, "error": "", "rename": None}
    want_pic = mode in ("long", "poster")

    def log(*a):
        if verbose:
            print(*a)

    share = cloud or ShareParser.parse(share_text)
    if not share:
        result["error"] = "未能识别任何网盘链接"
        return result
    result["cloud"] = share["cloud"]
    log(f"✔ 网盘:{share['cloud']}")
    log(f"  链接:{share['url']}")
    if share.get("code"):
        log(f"  提取码:{share['code']}")

    # 分享页自动识别
    if not title and page_meta is None:
        page_meta = CloudShareFetcher().fetch(share_text)
        if page_meta and page_meta.get("title"):
            log(f"\n🔎 分享页自动识别")
            log(f"  标题: {page_meta['title']}")
            if page_meta.get("file_count"):
                log(f"  文件: {page_meta['file_count']} 个，"
                    f"共 {human_size(page_meta.get('size_bytes', 0))}")
            if size is None and page_meta.get("size_bytes"):
                size = human_size(page_meta["size_bytes"])
            if quality is None:
                quality = guess_quality(page_meta.get("size_bytes"))
        else:
            log("\n（无法自动读取分享页，改用片名搜索流程）")

    # 解析片名线索
    raw_name = title or ""
    share_year, tmdb_id = None, None
    if page_meta and page_meta.get("title"):
        raw_name, share_year, tmdb_id = parse_share_title(page_meta["title"],
                                                          is_share_title=True)
        if raw_name:
            log(f"  片名: {raw_name} {('(' + share_year + ')') if share_year else ''}"
                f"{('  [tmdb-' + tmdb_id + ']') if tmdb_id else ''}")

    # ===== 合集分支：递归列出的文件里有多部不同影片 =====
    items = collect_media_items(page_meta.get("deep_files") if page_meta else None,
                                page_meta.get("dir_nodes") if page_meta else None)
    if len(items) >= 2 and not title:
        log(f"\n🧩 检测到合集（{len(items)} 部）")
        # 演员/导演个人作品合集常不带 {tmdb-id}，统一按片名+年份补全，
        # 让片单有中文名、封面能直取官方海报（而不是退化成剧照拼图）
        if any(not it.get("tmdb") for it in items):
            log("  ⏳ 正在补全 TMDB 信息…")
            enrich_items_with_tmdb(items, verbose=verbose)
        for it in items[:8]:
            log(f"   - {it['title']} {('(' + str(it['year']) + ')') if it.get('year') else ''}"
                f"{(' [tmdb-' + it['tmdb'] + ']') if it.get('tmdb') else ''}"
                f"{(' [' + it['quality'] + ']') if it.get('quality') else ''}")
        if len(items) > 8:
            log(f"   … 其余 {len(items) - 8} 部")
        # 发帖前重命名实际文件（rename=True 且已登录时）
        if rename:
            _acc = account or ensure_guangya_account(verbose=verbose)
            if _acc is None:
                result["rename"] = {"ok": False,
                                    "error": "未登录光鸭云盘，已跳过重命名"}
                log("  ⚠ 未登录光鸭云盘，跳过重命名")
            else:
                _plan = build_rename_plan(page_meta.get("deep_files"),
                                          page_meta.get("dir_nodes"),
                                          items=items)
                if not _plan:
                    result["rename"] = {"ok": True, "total": 0,
                                        "changed": 0, "failed": 0, "items": []}
                    log("  ✏️ 无需重命名（名称已规范）")
                else:
                    log(f"  ✏️ 重命名中：共 {len(_plan)} 项…")
                    _res = apply_rename_plan(_acc, _plan, dry_run=False)
                    _res["ok"] = _res["failed"] == 0
                    result["rename"] = _res
                    log(f"  ✏️ 重命名完成：成功 {_res['changed']} · "
                        f"失败 {_res['failed']}")

        series = raw_name or "影视合集"
        text = build_collection_text(
            share, series or "影视合集", items,
            quality=None,
            size=size or human_size(page_meta.get("size_bytes")),
        )
        result["text"] = text
        result["title"] = series or "影视合集"
        result["collection"] = True
        log("\n" + "─" * 56)
        log(text)
        log("─" * 56 + "\n")
        import base64 as _b64
        # 封面：四级兜底，保证合集一定有图可用
        cover, cover_src = (None, "")
        if want_pic:
            cover, cover_src = pick_collection_cover(items, result["title"])
            if cover is not None:
                log(f"  🖼 合集封面来源: {cover_src}")
                result["cover_b64"] = _b64.b64encode(
                    poster_to_jpeg(cover)).decode()
        if mode == "long":
            try:
                maker = PosterMaker(width=width, font_size=font_size)
                img = maker.render(text, poster_img=cover,
                                   title=result["title"])
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                result["image_b64"] = _b64.b64encode(buf.getvalue()).decode()
            except Exception as e:      # noqa: BLE001
                log(f"  ⚠ 合集长图生成失败: {e}")
        elif mode == "poster" and cover is None:
            log("  ⚠ 合集封面生成失败")
        result["ok"] = True
        return result

    # 采集影视信息
    info = {"_manual": True}
    poster_img = None
    poster_url = ""
    title_for_filename = raw_name or "share"
    web = TMDBWebClient()

    if tmdb_id:
        log(f"\n⏳ 正在 TMDB 抓取 #{tmdb_id} 的资料…")
        got = web.detail_by_id(tmdb_id)
        if got:
            info = got["info"]
            info["_type"] = got["type"]
            poster_url = got.get("poster_url") or ""
            title_for_filename = info.get("title") or raw_name
            log(f"  命中: {title_for_filename} ({info.get('year','')}) [{got['type']}]")
        else:
            log("  ⚠ TMDB 未找到该 ID，回退站内搜索")

    if info.get("_manual") and raw_name:
        log(f"\n⏳ 在 TMDB 站内搜索 “{raw_name}”…")
        cands = web.search_by_name(raw_name, year=share_year)
        if cands:
            log(f"  找到 {len(cands)} 条，默认取第 {pick} 条")
            idx = pick if 1 <= pick <= len(cands) else 1
            chosen = cands[idx - 1]
            got = web.detail_by_id(chosen["id"])
            if got:
                info = got["info"]
                info["_type"] = got["type"]
                poster_url = got.get("poster_url") or ""
                title_for_filename = info.get("title") or raw_name
                log(f"  采用: {title_for_filename} ({info.get('year','')}) [{got['type']}]")

    if info.get("_manual"):
        if raw_name:
            log("  ⚠ 自动检索无结果，按手动标题出文（简介/主演留空）")
            info = {"title": raw_name, "name": raw_name,
                    "release_date": f"{share_year}-01-01" if share_year else "",
                    "first_air_date": "", "_manual": True}
            title_for_filename = raw_name
        else:
            result["error"] = "未能从分享页/标题识别片名，请用 --title 指定"
            return result
    result["manual"] = bool(info.get("_manual"))

    # 海报下载
    if poster_url and want_pic:
        try:
            r = requests.get(poster_url, timeout=20)
            r.raise_for_status()
            poster_img = Image.open(io.BytesIO(r.content))
            poster_img.load()
        except Exception as e:
            log(f"  ⚠ 海报下载失败: {e}")

    # 合成帖子
    gen = PostGenerator()
    text = gen.build(info, share,
                     quality=quality or "1080P WEB-DL X264 AAC",
                     size=size or "未知大小")
    result["text"] = text
    result["title"] = info.get("title") or info.get("name") or title_for_filename

    log("\n" + "─" * 56)
    log(text)
    log("─" * 56 + "\n")

    import base64 as _b64
    if mode == "long":
        try:
            maker = PosterMaker(width=width, font_size=font_size)
            img = maker.render(text, poster_img=poster_img,
                               title=result["title"])
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            result["image_b64"] = _b64.b64encode(buf.getvalue()).decode()
        except Exception as e:
            log(f"  ⚠ 长图生成失败: {e}")
    elif mode == "poster":
        if poster_img is None:
            log("  ⚠ 未获取到海报（可用 --poster-url 手动提供）")
        else:
            jpg = poster_to_jpeg(poster_img)
            png = poster_to_png(poster_img)
            if jpg:
                result["poster_jpg_b64"] = _b64.b64encode(jpg).decode()
            if png:
                result["poster_png_b64"] = _b64.b64encode(png).decode()

    result["ok"] = True
    return result


if __name__ == "__main__":
    main()
