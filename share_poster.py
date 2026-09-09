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
VERSION = "1.0.1"

TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p"

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

        # 顺带拉第一层文件列表（可能有更多文件名线索）
        try:
            token = self._post_json("get_share_access_token", {"shareId": share_id})["accessToken"]
            fl = self._post_json("get_share_page_files_list", {
                "pageSize": 100, "accessToken": token,
                "orderBy": 0, "sortType": 0, "parentId": "",
            })
            result["files"] = fl.get("list") or []
        except Exception:
            result["files"] = []
        return result


# --- TMDB 免 Key 网页抓取 ---------------------------------------------
# TMDB 官网公开页面包含结构化数据（og: 标签 + JSON-LD），无需 API Key。
# 我们请求 zh-CN 页面拿到中文片名 / 简介 / 演员 / 海报。
class TMDBWebClient:
    BASE = "https://www.themoviedb.org"

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

    # 工具 --------------------------------------------------------------
    @staticmethod
    def _html_meta(html, prop):
        m = re.search(r'<meta property="og:%s" content="([^"]*)"' % re.escape(prop), html)
        return m.group(1) if m else None

    def _fetch_page(self, url):
        r = self.session.get(url, timeout=20)
        r.raise_for_status()
        return r.text

    @staticmethod
    def _clean_title(t):
        """去掉 ' — The Movie Database (TMDB)' 之类的尾巴，拆出 (年份)"""
        t = re.sub(r"\s*[—-]\s*(The Movie Database|TMDB).*$", "", t).strip()
        return t

    @staticmethod
    def _year_from_title(t):
        m = re.search(r"\((\d{4})\)", t)
        return m.group(1) if m else ""

    def _cast_from_html(self, html, limit=10):
        """从 id='cast' 区块抓演员中文名（缩略图 alt / 人物名文本）"""
        seg = html
        idx = html.find('id="cast"')
        if idx >= 0:
            seg = html[idx: idx + 300000]
        names, seen = [], set()
        # 头像 alt 通常就是演员名
        for a in re.findall(r'alt="([^"]{1,40})"', seg):
            a = a.strip()
            if not a or a.startswith("${") or len(a) > 30:
                continue
            # 排除图片 alt 中的非人名噪音
            if a not in seen and not re.match(r"^[a-zA-Z0-9]{1,3}$", a):
                seen.add(a)
                names.append(a)
            if len(names) >= limit:
                break
        # 上面的 alt 可能混入站点图标名，用人物链接二次校验后取靠前结果
        cast = []
        for n in names:
            if n in {"The Movie Database (TMDB)", "Poster", "Backdrop"}:
                continue
            cast.append(n)
        return cast[:limit]

    def detail_by_id(self, tmdb_id):
        """按 TMDB ID 抓取中文资料（movie/tv 自动探测）。
        返回 {type, info, poster_url} 或 None"""
        if not str(tmdb_id).isdigit():
            return None
        for media in ("movie", "tv"):
            try:
                html = self._fetch_page(f"{self.BASE}/zh-CN/{media}/{tmdb_id}")
            except Exception:
                try:
                    html = self._fetch_page(f"{self.BASE}/{media}/{tmdb_id}")
                except Exception:
                    continue
            info = self._parse_detail_html(html, tmdb_id)
            if info:
                return {"type": media, "info": info,
                        "poster_url": self._html_meta(html, "image")}
        return None

    def search_by_name(self, query, year=None):
        """无 Key：抓 TMDB 站内搜索页，解析候选 (title, id, media_type, year)。
        返回列表 [{title,id,type,year,date,overview}]"""
        url = f"{self.BASE}/search?query={requests.utils.quote(query)}"
        if year:
            url += f"&year={year}"
        try:
            html = self._fetch_page(url)
        except Exception as e:
            sys.stderr.write(f"[TMDB 搜索失败] {e}\n")
            return []
        cands = []
        # 搜索页卡片结构：<a href="/movie/123"> 附近有标题
        for m in re.finditer(r'href="/(movie|tv)/(\d+)[^"]*"[^>]*>([^<]{1,120})<', html):
            mt, mid, t = m.group(1), m.group(2), self._clean_title(m.group(3))
            t = re.sub(r"\s*\((\d{4})\)\s*$", "", t).strip() or m.group(3)
            if t.startswith("${"):
                continue
            item = {"id": int(mid), "type": mt, "title": t}
            if item not in cands:
                cands.append(item)
            if len(cands) >= 8:
                break
        return cands

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


def parse_share_title(raw_title):
    """从分享标题里拆 (片名, 年份, tmdb_id)。示例：
    《星球大战：曼达洛人与古古 (2026) {tmdb-1228710}》
    片名.2026.2160P  ...
    """
    if not raw_title:
        return None, None, None
    t = raw_title
    tmdb_id = None
    # 兼容 {tmdb-123} / {tmdbid-123} / {tmdb_id:123} 等写法
    m = re.search(r"\{?\s*tmdb(?:[-_ ]?id)?[:_\- ]*(\d+)\s*\}?", t, re.I)
    if m:
        tmdb_id = m.group(1)
        t = t.replace(m.group(0), " ")
    # 兜底清掉没被花括号包住的 tmdb 标记
    t = re.sub(r"\s*tmdb(?:[-_ ]?id)?[:_\- ]*\d+\s*", " ", t, flags=re.I)
    m = re.search(r"\((\d{4})\)", t)
    year = m.group(1) if m else ""
    t = re.sub(r"\s*\(\d{4}\)\s*", " ", t)
    t = re.sub(r"[\{\}\[\]]", " ", t)
    t = re.sub(r"\s*\d{3,4}[Pp].*$", "", t)          # 去掉 1080P 等画质后缀
    t = re.sub(r"\s+", " ", t).strip(" .-_")
    return t or None, year or None, tmdb_id


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
    parser.add_argument("--image-width", type=int, default=720, help="长图宽度")
    parser.add_argument("--font-size", type=int, default=28, help="正文字号")
    parser.add_argument("--no-net", action="store_true",
                        help="禁用分享页/TMDB 网页抓取（只用手动/Key 数据）")
    parser.add_argument("--save-config", action="store_true", help="保存当前 key 到 ~/.share_poster.json")
    args = parser.parse_args()

    cfg = load_config()
    api_key = args.key or cfg.get("tmdb_api_key") or os.environ.get("TMDB_API_KEY", "")

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
        raw_name, share_year, tmdb_id = parse_share_title(page_meta["title"])
        if raw_name:
            print(f"  片名: {raw_name} {('(' + share_year + ')') if share_year else ''}"
                  f"{('  [tmdb-' + tmdb_id + ']') if tmdb_id else ''}")
    if not raw_name:
        raw_name = args.title or ""

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
                        if not poster_url and args.make_image:
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
    if poster_img is None and poster_url and args.make_image:
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

    img_path = None
    if args.make_image:
        try:
            maker = PosterMaker(width=args.image_width, font_size=args.font_size)
            img = maker.render(text, poster_img=poster_img,
                               title=info.get("title") or info.get("name") or "")
            img_path = out_dir / f"{safe_title}_share.jpg"
            img.save(img_path, quality=92)
            print(f"🖼  长图已保存:{img_path}")
        except Exception as e:
            sys.stderr.write(f"[生成图片失败] {e}\n")

    # 7. 配置
    if api_key and (args.save_config or not cfg.get("tmdb_api_key")):
        save_config({**cfg, "tmdb_api_key": api_key})
        print(f"🔑 API Key 已保存到 {config_path()}")

    print("\n完成 ✅")


# --- 一键生成（供 CLI / Web 复用） --------------------------------------
def generate(share_text, quality=None, size=None, title=None, pick=1,
             include_image=True, width=720, font_size=28,
             cloud=None, verbose=True):
    """输入分享文本，走完整自动链路，返回：
    {ok, text, title, image_b64 (PNG), cloud, size_hint,
     manual(bool: 是否落入手工兜底), error}
    """
    result = {"ok": False, "text": "", "title": "", "image_b64": "",
              "cloud": "", "manual": False, "error": ""}

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
    page_meta = None
    if not title:
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
        raw_name, share_year, tmdb_id = parse_share_title(page_meta["title"])
        if raw_name:
            log(f"  片名: {raw_name} {('(' + share_year + ')') if share_year else ''}"
                f"{('  [tmdb-' + tmdb_id + ']') if tmdb_id else ''}")

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
    if poster_url and include_image:
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

    if include_image:
        try:
            maker = PosterMaker(width=width, font_size=font_size)
            img = maker.render(text, poster_img=poster_img,
                               title=result["title"])
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            import base64
            result["image_b64"] = base64.b64encode(buf.getvalue()).decode()
        except Exception as e:
            log(f"  ⚠ 长图生成失败: {e}")

    result["ok"] = True
    return result


if __name__ == "__main__":
    main()
