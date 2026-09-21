# PixelForge —— 容器里跑应用，adb 留在宿主机。
#
# 为什么必须这么拆，而不是把 adb 一起塞进容器：
#
# macOS 上的 Docker Desktop 跑在一层 Linux 虚拟机里，**看不到宿主的 USB 设备**。
# 没有 --device /dev/bus/usb 可以传，也没有变通办法。所以在 Mac 上，容器化
# PixelForge 的唯一可行形态就是「应用在容器里，adb server 在宿主机」。
# Linux 上可以真的把 USB 传进去（见 docs/DEPLOY.md），但这个拆分在两边都能用。
#
# 这个拆分能成立，靠的是代码里的 PIXELFORGE_ADB_SERVER_HOST：adb forward 绑定的
# 端口在**跑 server 的那台机器上**，所以 scrcpy 的视频/控制 socket 和
# uiautomator2 的 HTTP 都要往那台机器连，而不是往 127.0.0.1。

FROM python:3.12-slim AS base

# adb 客户端与服务端版本必须一致 —— 版本不同的 adb 会互相 kill 对方的 server，
# 而远端 server 是 kill 不掉也重启不了的，表现就是连不上。
# 用宿主机 `adb version` 报的版本号覆盖这个值。
ARG PLATFORM_TOOLS_VERSION=latest

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl unzip ca-certificates \
        # OCR 定位用；chi-sim 装上以便识别中文界面
        tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
        # opencv-python-headless 仍需要这个
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Linux 版 adb 客户端。只做客户端用 —— 容器不碰 USB。
RUN curl -fsSL -o /tmp/pt.zip \
        "https://dl.google.com/android/repository/platform-tools_r${PLATFORM_TOOLS_VERSION}-linux.zip" \
     || curl -fsSL -o /tmp/pt.zip \
        "https://dl.google.com/android/repository/platform-tools-latest-linux.zip" \
    && unzip -q /tmp/pt.zip -d /opt && rm /tmp/pt.zip \
    && /opt/platform-tools/adb version

WORKDIR /app
COPY backend/pyproject.toml backend/README.md* ./backend/
COPY backend/src ./backend/src
RUN pip install --no-cache-dir -e ./backend

COPY frontend ./frontend
COPY examples ./examples

# 非 root 运行。容器不需要设备访问权限 —— USB 在宿主机那边。
RUN useradd --create-home --uid 10001 pixelforge \
    && mkdir -p /data && chown -R pixelforge:pixelforge /data /app
USER pixelforge

ENV PIXELFORGE_ADB_EXECUTABLE=/opt/platform-tools/adb \
    PIXELFORGE_DATA_DIR=/data \
    PIXELFORGE_FRONTEND_DIR=/app/frontend \
    PIXELFORGE_HOST=0.0.0.0 \
    PIXELFORGE_PORT=8420

EXPOSE 8420
VOLUME ["/data"]

# 健康检查用 /api/health：它会如实报告 adb 是否可达，所以容器起来但连不上
# 宿主 adb 这种情况能被区分出来，而不是笼统地「容器在跑」。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys,json; \
d=json.load(urllib.request.urlopen('http://127.0.0.1:8420/api/health',timeout=4)); \
sys.exit(0 if d['status']=='ok' else 1)"

# 只能单 worker：设备会话、scrcpy socket、租约、运行中的脚本都是进程内状态。
CMD ["pixelforge", "serve"]
