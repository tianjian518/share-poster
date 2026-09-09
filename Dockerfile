# 影帖（YingTie）· Web 版多架构镜像
# 支持 linux/amd64 + linux/arm64（由 GitHub Actions buildx 构建）
FROM python:3.11-slim

# 中文字体：长图排版必需（否则中文全变方块）
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY share_poster.py webapp.py ./

ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai

EXPOSE 8321
CMD ["python", "webapp.py", "--host", "0.0.0.0", "--port", "8321", "--no-browser"]
