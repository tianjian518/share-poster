# 影帖（YingTie）· 影视分享帖生成器

一个把网盘分享链接一键变成 **可发帖文案 + 长图海报** 的小工具。

支持两条链路，**都不需要 TMDB API Key**：

| 链路 | 做了什么 |
| ---- | -------- |
| 分享页自动识别 | 打开分享页后端接口，直接读分享标题 / 体积 / 文件数，从标题解析 `{tmdb-12345}` 或“片名 (年份)” |
| TMDB 网页抓取 | 无需 Key，请求 TMDB 公开页面，拿中文片名 / 简介 / 主演 / 官方海报 |

再叠加可选的手动字段（`--actors` / `--synopsis` / `--quality` …）覆盖自动结果。

效果示意（生成的文本）：
```
📖 名称:乡村爱情 (2006)
🎬 质量:1080P WEB-DL X264 AAC [136.2GB]
📺 集数:共 849 集 / 18 季
👥 主要人物:唐鉴军、王小利、刘小光、蔡维利、贺树峰
📝 简介:《乡村爱情》是一部反映农村青年爱情、婚姻、事业和生活的轻喜剧……
⬇️ 下载地址:
https://pan.baidu.com/s/xxxxx
提取码: abcd
```

并附带一张排好版的同款长图（封面 + 字段 + 下载地址），方便直接贴到贴吧 / 微博 / 小红书。

---

## 安装

```bash
pip3 install requests pillow
```

> Python 3.7+。

## 使用

### 1. 一键自动（丢链接即可，推荐）
```bash
python3 share_poster.py "https://www.guangyapan.com/s/xxxxxxxxxxxxxxxxx"
```
程序会自动：
1. 识别网盘、解析分享链接；
2. 调用光鸭云盘公开接口，读分享标题和总大小（77.9GB → 自动填 `[77.9GB]`，并按体积推断画质）；
3. 从标题里的 `{tmdb-1228710}` 或“片名 (年份)”定位 TMDB，抓中文资料 + 海报；
4. 生成 `片名_share.txt`（发帖文本）和 `片名_share.jpg`（长图）。

### 2. 手动指定 + 自定义海报（任何网盘 / 无需网络抓取）
```bash
python3 share_poster.py \
  --title "我的影视名称" \
  --actors "演员A、演员B" \
  --synopsis "一句话简介" \
  --quality "4K HDR HEVC" \
  --size "60GB" \
  --episodes "共 12 集" \
  --poster-url "https://..." \
  "https://pan.baidu.com/s/xxx 提取码:abcd"
```

### 3. 有 TMDB Key（更精确的数据，含完整季/集数）
```bash
python3 share_poster.py -k <你的v3_api_key> "https://pan.baidu.com/s/xxx 提取码:abcd"
# 保存 Key，下次免输：
python3 share_poster.py -k <你的v3_api_key> --save-config
```

### 4. 纯交互式
```bash
python3 share_poster.py
# 然后粘贴一段分享文本，跟着提示走
```

## 支持范围

| 能力 | 说明 |
| ---- | ---- |
| 网盘链接识别 | 百度 / 夸克 / 阿里云盘 / 迅雷 / 123 / 城通 / 蓝奏 / 奶牛快传 / 光鸭云盘 / 微云 / OneDrive / Google Drive |
| 分享页自动读取 | 目前光鸭云盘（`guangyapan.com`）走公开 JSON 接口免登录读取标题与体积 |
| 片名 → TMDB | `{tmdb-12345}` ID 直取；或分享标题里的“片名 (年份)”站内搜索 |
| 无需 API Key | TMDB 公开网页抓取（中文资料 + 海报） |
| 输出 | `片名_share.txt` 纯文本 + `片名_share.jpg` 长图 |

> 百度 / 夸克等分享页通常需要登录/验证码，无法免登录自动读标题——此时请用 `--title` 或交互输入片名。

## 命令行参数

| 参数 | 说明 |
| ---- | ---- |
| `share_text` | 分享文本（含链接，可含提取码） |
| `-k / --key` | TMDB v3 API Key（可选） |
| `-t / --title` | 片名；缺省时尝试从分享页自动识别 |
| `-y / --year` | 年份 |
| `--type` | `movie` 或 `tv` |
| `--quality` | 质量字段（自动模式按体积推断，可覆盖） |
| `--size` | 大小字段（自动模式用分享页真实体积） |
| `--episodes` | 手动指定集数 |
| `--actors` | 手动指定演员 |
| `--synopsis` | 手动指定简介 |
| `--poster-url` | 自定义海报 URL |
| `-o / --output-dir` | 输出目录 |
| `--no-image` | 只出文本不出图 |
| `--image-width` / `--font-size` | 长图尺寸 / 字号 |
| `--no-net` | 关闭分享页 / TMDB 网页抓取 |
| `--save-config` | 保存 Key 到 `~/.share_poster.json` |

## 输出

- `片名_share.txt` —— 帖子纯文本，直接复制即可
- `片名_share.jpg` —— 排版好的长图（封面 + 字段 + 链接）

## 工作原理

1. 正则匹配网盘 URL + 提取码
2. 若支持免登录读取（如光鸭云盘），请求其公开接口拿分享标题 / 总大小；从标题解析 `{tmdb-<id>}` 或“片名 (年份)”
3. 抓 TMDB 公开页面（`/zh-CN/movie|tv/{id}`）解析 JSON-LD / og: / cast，取中文片名、简介、主演、海报 URL
4. 按固定模板拼装文本，用 Pillow + 中文字体排版输出长图

## 扩展新网盘

`CloudShareFetcher` 是适配器模式：在 `fetch()` 里加一个分支即可接入新网盘的公开接口，
只要最终返回 `{cloud, url, title, size_bytes, file_count}` 结构即可被主流程自动消费。

---

## v1.0.0 (2026-09-09)

- 首个正式发布：CLI + Web 双形态，光鸭云盘分享页自动识别，TMDB 免 Key 网页抓取
- Docker 镜像支持 **linux/amd64 与 linux/arm64** 双架构（GitHub Actions 自动构建）
- 部署与使用详见 `deploy/` 目录

## License

MIT## License

MIT
