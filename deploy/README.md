# 部署到 飞牛 OS / 甲骨文云 Docker

仓库根目录有 `Dockerfile`（支持 **linux/amd64 + linux/arm64** 双架构），本目录是
compose / HTTPS 反代配置。把整个仓库传到服务器后：

```bash
cd deploy
docker compose up -d --build
```

> 也可直接用 Docker Hub 现成镜像（GitHub Actions 自动多平台构建）：
> `docker pull <DOCKERHUB_USERNAME>/share-poster:latest`
> 然后在 compose 里把 `build` 段替换为 `image: <DOCKERHUB_USERNAME>/share-poster:latest`。

## 前提：一个域名（强烈建议）

"一键复制图片"依赖浏览器安全上下文，也就是 **HTTPS**。而免费 HTTPS 证书
（Let's Encrypt）只能签给**域名**，不能签给裸 IP。所以请：

1. 准备一个域名（便宜的 .xyz/.top 即可），把 `A` 记录指向甲骨文服务器公网 IP；
2. 修改 `Caddyfile`，把 `your-domain.com` 换成你的域名；
3. `docker compose up -d --build` 后，Caddy 自动申请证书，**全自动续期**。

然后访问 `https://你的域名` 即可，页面顶部自检条会显示绿色的
“✔ 当前环境支持复制图片”。

## 没有域名 / 只想要裸 IP？

纯公网 `http://IP:端口` 无法用站点 API 复制图片，但以下仍可用：

| 操作 | 可用性 | 方法 |
| ---- | ------ | ---- |
| 复制文本 | ✅ | “复制文本”按钮（已做 execCommand 兼容） |
| 发图片 | ✅ 变通 | 在图预览上**右键 → “复制图片”**（浏览器原生，不依赖站点），再到 QQ Ctrl+V；或右键“另存为”后直接拖进 QQ 频道 |
| 一键“复制图片”按钮 | ❌ | 只能 HTTPS 环境用 |

若暂时裸 IP 部署，把 `docker-compose.yml` 里的端口映射从 `expose` 改成：

```yaml
    ports:
      - "8321:8321"
```

并去掉 caddy 服务，直接访问 `http://服务器IP:8321`。

## 注意事项

- 飞牛 OS 的防火墙/甲骨文安全组需要放行 **80 + 443**（有域名时）或 **8321**（裸 IP）。
- 建议在 Caddy 前面不套飞牛的反代，避免多一层 https 证书混乱。
- 数据都在容器里，升级时重新 `docker compose up -d --build` 即可。
