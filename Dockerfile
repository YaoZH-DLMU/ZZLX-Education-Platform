# ── 构建阶段 ──────────────────────────────────────────────
FROM python:3.13-slim

WORKDIR /app

# 先复制依赖文件，利用 Docker 层缓存（依赖不变时不重装）
COPY requirements.txt .
# Pillow（qrcode[pil] / matplotlib 依赖）和 ffmpeg（视频封面截图）需要这些系统库
# 使用阿里云镜像源加速（境内服务器），兼容 Debian Bookworm（sources.list.d）和旧版（sources.list）
RUN ( sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
          /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
      sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
          /etc/apt/sources.list 2>/dev/null; true ) \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        libjpeg-dev zlib1g-dev libpng-dev libfreetype6-dev ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ \
    && pip install --no-cache-dir --prefer-binary -r requirements.txt

# 复制项目代码
COPY . .

# 确保上传子目录存在
RUN mkdir -p app/static/uploads/forum

# matplotlib 在服务器无显示器时使用 Agg 后端
ENV MPLBACKEND=Agg

EXPOSE 5000

# sync worker 最稳定；SQLite 单文件用 1 worker 避免写锁
CMD ["gunicorn", "--bind", "0.0.0.0:5000", \
     "--workers", "1", \
     "--worker-class", "sync", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--log-level", "debug", \
     "run:app"]
