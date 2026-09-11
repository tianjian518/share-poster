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
| `--login-guangya` | 短信验证码登录光鸭云盘 |
| `--login-token` | 直接贴 access_token 登录 |
| `--refresh-token` | 贴 refresh_token（可单独用，自动换 access_token） |
| `--guangya-status` | 查看光鸭云盘登录状态 |
| `--logout-guangya` | 退出光鸭云盘登录 |
| `--rename` | 发帖前重命名分享内的文件夹与视频 |
| `--rename-dry-run` | 只预览重命名，不改动 |
| `--rename-limit N` | 只处理前 N 项（分批执行） |
| `--show-rename-plan` | 生成帖子前先打印重命名预览 |

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

## ✨ 光鸭云盘账号 & 发帖前重命名（v1.1.0 新增）

发帖之前，可以先把分享里乱糟糟的文件夹名/文件名改成**规范名**，
别人转存后 Emby / Jellyfin 能直接刮削，识别率更高。

**规范格式（文件夹和视频文件都改）：**

```
文件夹: 片名 (年份) [画质] {tmdb-id}
文件:   片名 (年份) [画质] {tmdb-id}.mkv
```

实际效果（詹妮弗·康纳利合集，71 项）：

```
📁 A-爱的秘密(1997)  →  爱的秘密 (1997) [1080P H.264 WEBRip] {tmdb-12723}
🎞 爱的秘密.mkv       →  爱的秘密 (1997) [1080P H.264 WEBRip] {tmdb-12723}.mkv
```

### 一、登录光鸭云盘（两种方式都支持）

**方式 1 · 短信验证码**（网页右上角「登录光鸭」）

```bash
python3 share_poster.py --login-guangya
```

**方式 2 · 直接贴 Token**

```bash
# access_token + refresh_token（推荐，能自动续期）
python3 share_poster.py --login-token "你的access_token" --refresh-token "你的refresh_token"

# 只有 refresh_token 也行，会自动换出 access_token
python3 share_poster.py --refresh-token "你的refresh_token"
```

登录态保存在 `~/.share_poster.json`，**token 快过期时执行任务会自动续期**。

```bash
python3 share_poster.py --guangya-status     # 看登录状态和剩余天数
python3 share_poster.py --logout-guangya     # 退出登录
```

### 二、重命名

```bash
# 只预览，不改任何东西
python3 share_poster.py --rename-dry-run "分享链接"

# 真改
python3 share_poster.py --rename "分享链接"

# 分批改（先改前 20 项，确认没问题再继续）
python3 share_poster.py --rename --rename-limit 20 "分享链接"

# 重命名完顺便出帖子
python3 share_poster.py --rename "分享链接" -o ./out
```

**网页版更方便**：粘贴链接 → 点「👀 预览重命名」→ 弹窗里逐项勾选 → 「✅ 确认重命名」。
或者勾上下方的「发帖前先重命名」，点「生成帖子」时会自动改名再出帖。

> ⚠️ 重命名是**直接改你自己网盘里的文件**，不可撤销。所以默认只预览，
> 必须手动确认（弹窗里勾选 + 二次确认）才会真正执行。

## v1.1.0 (2026-09-11)

- 新增**光鸭云盘账号登录**：短信验证码 + 手动贴 Token，两种都支持
- 登录态本地保存，token 将过期时自动续期
- 新增**发帖前重命名**：文件夹 + 视频文件统一改成
  `片名 (年份) [画质] {tmdb-id}`（Emby 友好，他人转存即可入库）
- 重命名**默认只预览**，弹窗勾选 + 二次确认后才真正修改
- 网页右上角显示登录状态与 token 剩余天数
- CLI 新增 `--login-guangya` / `--login-token` / `--refresh-token` /
  `--guangya-status` / `--logout-guangya` / `--rename` / `--rename-dry-run` /
  `--rename-limit` / `--show-rename-plan`

## v1.0.7 (2026-09-11)

- **修复 TMDB 站内搜索彻底失效**：新版 TMDB 搜索页是服务端渲染，结果链接格式已变为
  `/movie/<id>-<english-slug>`（旧正则 `href="/movie/(\d+)"[^>]*>标题<` 匹配不到任何结果），
  重新按 slug 解析候选。这是「演员个人作品合集」拿不到海报的根因
- 新增 `enrich_items_with_tmdb()`：合集里没有 `{tmdb-id}` 的条目，按「片名 + 年份」自动搜 TMDB
  补 id 与中文名 → 封面从「剧照拼图」升级为**官方海报**（如「詹妮弗·康纳利 42 部」命中 33/35）
  - 搜索关键词三级兜底：中文标题 → 去尾部序号 → 从原始文件名提取的英文原名
  - 按年份核对候选（逐个查详情页比对年份），避免张冠李戴
- TMDB 抓取加**全局限流 + 429 退避**（0.35s 间隔、指数退避重试），修复并发抓取时大量
  `429 Too Many Requests` 导致补全命中率骤降（26/35 → 33/35）
- `parse_share_title(..., is_share_title=True)`：分享总标题不再套用「取方括号中文片名」规则，
  修复 `【詹妮弗·康纳利】绝世美女-电影合集【42部】` 被错切成「詹妮弗·康纳利」、丢失「合集」语义
- 新增 `is_junk_title()` + 收紧 `MEDIA_EXTS`：过滤蓝光原盘结构名（`BDMV`/`STREAM`/`AUXDATA`/
  `BACKUP`/`MovieObject`/`index`/`sound.bdmv`/纯数字 `00000`），修复它们被误当影片去搜 TMDB
  导致片单污染、年份跨度异常（蝙蝠侠合集 1989-2025 被污染成 1974-2026 的 bug）
- 片名清洗增强：去演员合集的字母分类前缀（`M-美国往事` → `美国往事`，仅短横线后接中文时）、
  去方括号版本说明（`[60帧率版本][高码版]`）、去无括号发布组水印（`高清影视之家发布`）
- 合集封面底栏文字：过长时优先在分隔符处断开，避免「XX合集」后缀被截成「XX合…」

## v1.0.6 (2026-09-10)

- 修复「上传者把多部片错误塞进同一个 `片名(年份){tmdb}` 文件夹」导致所有视频都继承错误身份、整组合集退化为英文片名的问题（如黑客帝国合集：4 部片全被塞进"黑客帝国 (1999) {tmdb-603}"文件夹）
- 新增"中文文件夹 → 视频 反哺匹配"：按 `tmdb-id` / `年份+续集序号` / `年份唯一` 把带正确中文名+tmdb 的平级空壳目录反哺给英文视频条目
- 新增 `extract_part()`：从片名里提取续集序号（优先级：1-2 位独立数字 > 罗马数字 Ⅰ-Ⅹ > 英文续集词 reloaded/revolutions/resurrections 等）
- 新增 `SEQUEL_KEYWORDS` 英文续集词 → 序号 的兜底映射
- 合集片单"同系列去重"：标题去掉尾部序号后相同视为同一部，避免把"黑客帝国 (1999) {tmdb-603}"和"黑客帝国1 1080P"重复计入
- **注意**：TMDB 站内搜索已改用 SPA 渲染（HTML 里没有 `/movie/\d+` 锚点也没有 `__NEXT_DATA__`），靠 `search_by_name` 走兜底匹配走不通了；本版本用「中文文件夹的 tmdb-id」做权威反哺，规避搜索

## v1.0.5 (2026-09-10)

- 修复合集无法生成海报：原光鸭云盘接口里「目录 / 文件」共用 `dirType=1`，旧版用 `dirType` 判断目录导致递归提前停止，0 个视频被识别 → 合集分支跑不到 → 退化为单部流程后搜不到 TMDB → 出不了海报
  - 改用 `resType/ext/fileSize/mineType` 区分目录与文件
  - 递归预算 60 → 1200 节点，深度 8 层；6 线程并发拉取（光鸭接口无频控）
  - `_list_dir` 失败自动重试 2 次
- 强化片名解析：识别点分/空格年份 `.2001.`、去掉 4K REMUX / 国英双语 / 中英字幕 / 压制组水印、罗马数字 Ⅰ→1
- 文件大小字段兼容 `fileSize`；子文件继承最近一层目录的 `{tmdb-id}`、中文片名（如 `[速度与激情2].2.Fast.2.Furious...` 目录名 + 英文文件名 → 取中文片名 + TMDB ID）
- 合集片单去重：同一年份跨语言/多版本自动合并，避免「速度与激情2 / 速度与激情2 - 4K REMUX 4K」重复
- 合集封面 4 级兜底，保证一定能出图：
  1. 子文件带 `{tmdb-id}` → TMDB 详情页官方海报
  2. 没 ID → 拿最早几部片名在 TMDB 站内搜索
  3. 还没 → 用网盘视频缩略图拼 3×3 封面
  4. 全没有 → 纯文字封面（暗色渐变 + 标题 + 部数）
- 合集长图（`--image-mode long`）现在也带封面（之前是空长图）

## v1.0.4 (2026-09-09)

- 加入 `--image-mode long/poster/none`（默认 long 不变）：
  - `poster` 模式只单独输出海报/封面文件，已压缩至 500KB 以内（不再是排版长图）
  - 合集没有官方海报，自动取最早年份那部的海报合成 “标题 · 共 N 部” 合集封面（<300KB）
- Web 端顶部加产物模式选择：长图 / 仅海报 / 纯文本

## v1.0.3 (2026-09-09)

- 支持分享合集（周星驰合集 / 指环王三部曲 / 黑客帝国 1-4 等）：自动递归列出目录，识别每部片名/年份/TMDB ID/画质，生成带片单的合集帖

## v1.0.2 (2026-09-09)

- 修复 TMDB 中文资料不稳定：固定发送 Accept-Language: zh-CN，海外服务器/国外 IP 也稳定返回中文（此前受访问者 IP 影响会出英文）
- 修复演员列表混入「奖项」等界面文字：演员提取改为只认人物链接内的名字

## v1.0.1 (2026-09-09)

- 移动端适配：手机 QQ 频道只能相册发图，页面给出“长按保存 → 频道相册选图”的完整引导

## v1.0.0 (2026-09-09)

- 首个正式发布：CLI + Web 双形态，光鸭云盘分享页自动识别，TMDB 免 Key 网页抓取
- Docker 镜像支持 **linux/amd64 与 linux/arm64** 双架构（GitHub Actions 自动构建）
- 部署与使用详见 `deploy/` 目录

## License

MIT## License

MIT
