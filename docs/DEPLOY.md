# 部署

## 这是个什么级别的东西

**本地优先的 Web 应用。** 不是桌面程序，也不是 SaaS。

准确说：浏览器里的界面 + 一个 Python 服务，但这个服务**必须跑在能物理接触到手机的那台机器上**——因为它最终是在调 adb，而 adb 需要 USB（或同网段的无线调试）。

| | 像 Web 的地方 | 像 PC 软件的地方 |
|---|---|---|
| 界面 | 浏览器，零构建静态文件 | — |
| 访问 | 局域网内任何人可打开 | — |
| 部署 | 一个 HTTP 服务 | 必须绑定在插着手机的那台机器 |
| 并发 | 多人可同时看画面 | **单进程**，一台设备同时只能一人控制 |
| 数据 | — | 落本地文件，无数据库 |

所以它的形态更接近 **Jupyter 或 Vite dev server**：技术上是 Web 服务，实际上是跑在你工作机上的本地工具。你可以把 Mac mini 当作"设备主机"，团队从各自电脑用浏览器连过去——但手机必须插在那台 Mac mini 上。

**不适合的用法**：部署到云服务器（云上没有你的手机）、做成多租户 SaaS（脚本执行就是本机任意代码，且 `--workers > 1` 会直接坏掉）、公网暴露（adb 等于设备完整 shell，目前没有鉴权）。

---

## 三种部署形态

### 1. 直接跑（推荐，也最简单）

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pixelforge doctor        # 先看缺什么
pixelforge serve
```

打开 <http://127.0.0.1:8420/>。这是开发和日常使用的正常方式。

### 2. Docker —— 应用在容器，adb 在宿主机

**macOS 上这是唯一可行的容器化形态，不是选择题。** Docker Desktop 跑在一层 Linux 虚拟机里，**看不到宿主的 USB 设备**，没有 `--device` 可传，也没有变通办法。

```bash
# 1) 宿主机起 adb server，监听容器能到达的地址
adb -a -P 5038 nodaemon server

# 注意：应用默认用 5037，这里宿主机开的是 5038，所以容器侧要显式指定
#   PIXELFORGE_ADB_SERVER_PORT=5038
# （docker-compose.yml 里设置；宿主机上也不要再有别的 adb server 占着这台设备）

# 2) 另开一个终端
export PLATFORM_TOOLS_VERSION=$(adb version | sed -n 's/.*Version \([0-9.]*\).*/\1/p' | head -1)
docker compose up --build
```

能成立靠的是 `PIXELFORGE_ADB_SERVER_HOST`。这里有个容易翻车的细节：**`adb forward` 绑定的端口在跑 adb server 的那台机器上，不在容器里**。所以 scrcpy 的视频 socket、控制 socket 和 uiautomator2 的 HTTP 全都要往宿主机连。代码里这三处都走 `server_host`，不是 `127.0.0.1`——否则容器能列出设备，一投屏就连不上，而且报错看起来毫无头绪。

两个必须注意的点：

**adb 版本必须一致。** 版本不同的 adb 客户端会尝试 kill 并重启对方的 server，而远端 server 既杀不掉也重启不了，表现就是连接失败。`PLATFORM_TOOLS_VERSION` 就是为此存在的。

**`adb -a` 没有认证。** 绑 0.0.0.0 意味着同网段任何人都能完全控制你连着的手机——装应用、读文件、截屏。只在可信网络上用，并用防火墙把 5038 限制到 Docker 网桥。

### 3. Linux + USB 直通（真正的全容器）

只有 Linux 宿主能做到。容器直接拿到 USB：

```yaml
services:
  pixelforge:
    build: .
    privileged: true                     # 或用下面更小的权限
    volumes:
      - /dev/bus/usb:/dev/bus/usb
      - ./data:/data
    environment:
      PIXELFORGE_ADB_SERVER_HOST: 127.0.0.1   # adb server 在容器内
```

比 `privileged` 更收敛的做法是用 `--device-cgroup-rule` 加 udev 规则只放行 Android 设备的 vendor id。另外容器需要能重新枚举设备，所以热插拔场景下 `privileged` 往往更省事。

这个形态适合 CI 机器或专用的设备主机。

---

## 可选依赖

四项各自独立，缺哪项只关掉对应能力，`pixelforge doctor` 会逐项报告：

| 缺失 | 失去 | 仍可用 |
|------|------|--------|
| adb | 全部设备功能 | —（这是唯一致命的） |
| `vendor/scrcpy-server.jar` | 实时画面、低延迟控制 | 截图、控件树、OCR、模板匹配 |
| uiautomator2 两个 APK | 控件选择器 | 模板、OCR、坐标 |
| tesseract | OCR 定位 | 控件、模板、坐标 |

服务不会因为缺其中任何一项而拒绝启动——一个能打开并说清哪里不对的界面，远比一个起不来的进程有用。

---

## 单 worker 是硬约束

设备会话、scrcpy socket、独占租约、运行中的脚本全是**进程内状态**。`--workers N` 会让请求随机落到没有该设备会话的进程上，症状是"能用，但偶尔莫名 409"——非常难查。

要横向扩展，需要把设备层拆成独立进程并把租约搬到 Redis；`Lease.epoch` 已经是为此准备的 fencing 原语。在那之前，一台机器一个进程。

## 多人使用

允许，且设计上考虑过：多人可同时订阅同一台设备的画面，但**控制权是独占的**，靠租约 + fencing 保证。第二个人点"获取设备"会被拒绝，可以选择强制接管——原持有者的操作会被 epoch 挡掉，而不是两个人的点击交错落在同一台手机上。

目前**没有鉴权**。仅限可信内网。
