# PixelForge

通用型 Android 真机可视化自动化 IDE。实时投屏、精确坐标拾取、无损素材取材、四策略元素定位、流程录制与断点调试。

- 产品定位、功能范围与开发周期 → [PRODUCT.md](./PRODUCT.md)
- 技术架构与设计决策 → [ARCHITECTURE.md](./ARCHITECTURE.md)

---

## 进度

| 阶段 | 内容 | 状态 |
|------|------|------|
| **P0** | adb 客户端 · 设备注册 · 独占租约 | ✅ |
| **P1** | scrcpy 协议编解码 · 视频流解复用 · 会话管理 | ✅ |
| **P2** | CoordinateMapper 四坐标空间 · 控制通道 | ✅ |
| **P3** | 无损截图双模式 · 框选裁剪 · 模板库 | ✅ |
| **P4** | UiAutomator2 · 模板匹配 · OCR · 四策略降级链 · FLAG_SECURE 诊断 | ✅ |
| **P5** | Step/Target/Project 模型 · 执行器 · 录制 | ✅ |
| **P6** | 断点 · 单步 · 人工接管 · 步骤时间线 | ✅ |
| **P7** | Exporter 插件机制（原生 / pytest / 第三方插件） | ✅ |
| **P8** | Listener 插件 · logcat · 统一时间线 | ✅ |
| **P9** | 前端 IDE · API 装配 · 部署 | ✅ |

**测试：370 项，全部通过。** 未在真机上跑过 —— 需要你接上手机验收，清单见下。

---

## 快速开始

### 0. 前置条件

需要在 Mac mini 或 Linux 上运行，手机通过 USB 连接或无线 adb 连接。

- **macOS**: `brew install android-platform-tools`（adb 必需）
- **Linux**: `apt install adb` 或 `pacman -S android-tools`

### 1. 克隆与环境配置

```bash
git clone https://github.com/Frank-zlc/PixelForge.git
cd PixelForge/backend

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate  # macOS/Linux
# 或 .venv\Scripts\activate  # Windows

# 安装依赖（包括开发依赖）
pip install -e ".[dev]"

# 复制环境配置（按需修改）
cp .env.example .env
```

### 2. 可选依赖安装

以下四项可选，缺了不阻止启动，只关掉对应功能：

```bash
# 实时投屏 + 低延迟控制（推荐装）
# 去 https://github.com/Genymobile/scrcpy/releases 下载对应版本的 scrcpy-server.jar
# 放在 vendor/scrcpy-server.jar

# 控件选择器（可选）
# 去 https://github.com/appium/appium-uiautomator2-server/releases 下载 .apk
# 存到 vendor/ 目录即可

# OCR 定位（可选，macOS 用 brew）
brew install tesseract tesseract-lang
# 或 apt install tesseract-ocr
```

### 3. 诊断 & 启动

```bash
# 检查缺了什么，每项都会说明缺了会失去什么功能
pixelforge doctor

# 启动服务（**必须单 worker**）
pixelforge serve

# 或指定主机和端口
pixelforge serve --host 0.0.0.0 --port 8420

# 开发模式（代码改动自动重载）
pixelforge serve --reload
```

打开浏览器访问：
- **IDE 界面**: http://127.0.0.1:8420/
- **API 文档**: http://127.0.0.1:8420/docs
- **WebSocket 调试**: ws://127.0.0.1:8420/ws/screen

### 4. 设备管理

```bash
# 列出所有连接的设备
pixelforge devices

# 手机连着 Mac 热点时，启用无线调试（参见下文）
pixelforge tcpip --device <serial>
pixelforge connect 192.168.2.5:5555
pixelforge disconnect 192.168.2.5:5555
```

要改用 DeviceFarmer/STF 设备池，在 `backend/.env` 配置：

```dotenv
PIXELFORGE_DEVICE_PROVIDER=devicefarmer
PIXELFORGE_DEVICEFARMER_URL=https://devices.example.com
PIXELFORGE_DEVICEFARMER_ACCESS_TOKEN=replace-with-minimal-api-token
```

PixelForge 使用 DeviceFarmer 的公开 API 获取设备、预约/续租，并通过
`remoteConnect` 返回的地址执行 `adb connect`。Token 只在后端读取。建议锁定
DeviceFarmer/STF `v3.7.9`；边缘节点共享 ADB 时也支持直接使用同一 serial。

项目默认启用 `logcat` 监听。获取设备后监听器随会话启动，日志进入底部统一时间线；
项目 JSON 的 `listeners` 可关闭或传入过滤选项。`mitmproxy` 和 `pcap` 目前仍会如实显示为
`planned`，不会伪装成已经接通的网络协议监听。

> ⚠️ **只能单 worker 运行。** 设备会话、scrcpy 连接、租约、运行中的脚本都是进程内状态。
> `--workers N` 会让请求随机落到没有该设备会话的进程上，症状是"能用，但偶尔莫名 409"。

### 四项可选依赖

缺了不会导致启动失败，只关掉对应能力。`pixelforge doctor` 和 `/api/health` 都会如实报告：

| 依赖 | 缺失时失去 | 仍可用 | 获取方式 |
|------|-----------|--------|---------|
| **adb** | 全部设备功能 | —（唯一致命的） | `brew install android-platform-tools` |
| `vendor/scrcpy-server.jar` | 实时画面、低延迟控制 | 截图、控件树、OCR、模板 | scrcpy releases 里对应版本的 jar |
| `uiautomator2-server*.apk` | 控件选择器 | 模板、OCR、坐标 | appium-uiautomator2-server releases |
| `tesseract` | OCR 定位 | 控件、模板、坐标 | `brew install tesseract tesseract-lang` |

### 无线连接（USB 掉线 / 手机连着热点时用这条）

手机接在 Mac 热点上做抓包时，USB 那条链路经常出问题——最常见的是开了
"USB 网络共享"：MIUI 会把 USB 功能从 `mtp,adb` 换成 `rndis`，adb 直接从
USB 配置里消失。症状是手机照常充电、Mac 多出一个网卡、`adb devices` 空了。

既然手机已经和 Mac 同在一个网段，就让 adb 也走这条链路，USB 口彻底不参与：

```bash
pixelforge tcpip --device <serial>      # 最后一次用到数据线
pixelforge connect 192.168.2.5:5555     # 地址由上一条命令打印出来
pixelforge devices                      # 拔掉线，设备仍在
```

界面上的连接面板做的是同一件事。Android 11+ 也可以跳过第一步，直接用系统
设置里"无线调试"给出的配对地址。断开用 `pixelforge disconnect <地址>`。

## CLI 命令参考

### `serve` —— 启动 Web 服务

```bash
pixelforge serve [OPTIONS]
```

**选项：**
- `--host <HOST>` — 绑定的主机名（默认 127.0.0.1）
- `--port <PORT>` — 监听端口（默认 8420）
- `--reload` — 启用代码热重载（开发用）

**用例：**
```bash
# 本机调试
pixelforge serve

# 监听所有网卡（Docker 或远程访问）
pixelforge serve --host 0.0.0.0

# 自定义端口
pixelforge serve --port 9000

# 开发模式（代码改动自动重启）
pixelforge serve --reload
```

### `doctor` —— 环境诊断

```bash
pixelforge doctor
```

检查并报告：
- adb 是否可用（缺了禁用全部设备功能）
- scrcpy-server.jar 是否存在（缺了禁用实时投屏和低延迟控制）
- tesseract 是否安装（缺了禁用 OCR 定位）
- uiautomator2 APK 是否存在（缺了禁用控件选择器）

每项缺失都会清楚地说明失去什么功能，但不会阻止服务启动。

### `devices` —— 列出设备

```bash
pixelforge devices
```

显示所有已连接的 Android 设备及其状态（device / offline / unauthorized 等）。

### `tcpip` —— USB 转 TCP（启用无线调试）

```bash
pixelforge tcpip --device <SERIAL> [--port 5555]
```

**参数：**
- `--device <SERIAL>` ⭐️（必需）— 目标设备的 adb serial
- `--port <PORT>` — 目标端口（默认 5555）

**用处：** 将 USB 连接的设备切换到 TCP 模式，通常用于启用无线调试。命令会打印设备的 IP 地址供下一步使用。

**例子：**
```bash
# 启用无线调试，显示 IP
pixelforge tcpip --device a1b2c3d4

# 使用非标准端口
pixelforge tcpip --device a1b2c3d4 --port 5037
```

### `connect` —— 连接无线设备

```bash
pixelforge connect <ADDRESS>
```

**参数：**
- `<ADDRESS>` — 无线设备地址，格式 `host:port`（如 `192.168.2.5:5555`）

**用处：** 通过 TCP/IP 连接无线 Android 设备（需要手机和 Mac 在同一网络）。

**例子：**
```bash
pixelforge connect 192.168.2.5:5555

# 连接后可用 adb 命令
adb -s 192.168.2.5:5555 shell getprop ro.product.model
```

### `disconnect` —— 断开无线设备

```bash
pixelforge disconnect [ADDRESS]
```

**参数：**
- `[ADDRESS]` — 可选，指定地址断开；省略则断开所有无线设备

**例子：**
```bash
# 断开特定设备
pixelforge disconnect 192.168.2.5:5555

# 断开所有无线设备
pixelforge disconnect
```

### `run` —— 无头执行脚本（CI 入口）

```bash
pixelforge run <PROJECT> <SCRIPT> --device <SERIAL> [OPTIONS]
```

**参数：**
- `<PROJECT>` — 项目目录名
- `<SCRIPT>` — 脚本文件名（不含 `.json` 扩展）
- `--device <SERIAL>` ⭐️（必需）— 目标设备 serial

**选项：**
- `--var KEY=VALUE` — 传递变量给脚本（可重复）
- `--json` — 以 JSON 格式输出执行结果

**返回：** 成功返回 0，失败返回非零（可用于 CI 流程）

**例子：**
```bash
# 基础执行
pixelforge run my_project test_login --device a1b2c3d4

# 带变量
pixelforge run my_project test_checkout --device a1b2c3d4 \
  --var username=testuser \
  --var password=testpass123

# JSON 输出（用于 CI 解析）
pixelforge run my_project ci_flow --device a1b2c3d4 --json
```

### `export` —— 导出脚本

```bash
pixelforge export <PROJECT> <SCRIPT> [--exporter <NAME>] [--out <FILE>]
```

**参数：**
- `<PROJECT>` — 项目目录名
- `<SCRIPT>` — 脚本文件名（不含 `.json` 扩展）

**选项：**
- `--exporter <NAME>` — 导出格式（默认 `pixelforge`，可选 `pytest`）
- `--out <FILE>` — 输出文件路径（默认输出到 stdout）

**例子：**
```bash
# 导出为 pixelforge 格式（Native）
pixelforge export my_project test_flow --out test_flow.pf.json

# 导出为 pytest 格式（可直接用 pytest 运行）
pixelforge export my_project test_flow --exporter pytest --out test_flow_test.py

# 打印到控制台
pixelforge export my_project test_flow --exporter pytest
```

### `exporters` —— 列出可用导出器

```bash
pixelforge exporters
```

显示所有已注册的导出器及其描述。内置 `pixelforge` 和 `pytest`，可通过插件扩展（见下文）。

## 环境变量

创建 `.env` 文件或设置环境变量来自定义配置：

```bash
# adb 可执行文件路径（默认用 PATH 中的 adb）
PIXELFORGE_ADB_EXECUTABLE=/opt/android-sdk/platform-tools/adb

# adb server 主机（容器化部署时用，默认 127.0.0.1）
PIXELFORGE_ADB_SERVER_HOST=host.docker.internal

# adb server 端口（默认 5037）
PIXELFORGE_ADB_SERVER_PORT=5037

# adb 命令超时（秒，默认 15）
PIXELFORGE_ADB_TIMEOUT_S=30

# 数据目录（项目、模板、截图，默认 .pixelforge/）
PIXELFORGE_DATA_DIR=~/.pixelforge

# 框选 PNG 的默认保存目录（默认 PixelForge 项目根目录）
PIXELFORGE_ASSET_DIR=~/Desktop/PixelForgeAssets

# 租约过期时间（秒，默认 30）
PIXELFORGE_LEASE_TTL_S=60

# 导出插件目录（多个用 : 分隔）
PIXELFORGE_EXPORTER_PLUGINS=/path/to/plugins1:/path/to/plugins2

# Web 服务主机（默认 127.0.0.1）
PIXELFORGE_HOST=0.0.0.0

# Web 服务端口（默认 8420）
PIXELFORGE_PORT=9000

# CORS 源地址（默认 http://localhost:5173）
PIXELFORGE_CORS_ORIGINS=http://localhost:3000,http://localhost:8080

# 前端目录（默认自动搜索 frontend/）
PIXELFORGE_FRONTEND_DIR=~/my-frontend-build

# 日志级别（DEBUG / INFO / WARNING / ERROR）
PIXELFORGE_LOG_LEVEL=DEBUG
```

**例子：**
```bash
# 编写 .env 文件
cat > backend/.env <<EOF
PIXELFORGE_HOST=0.0.0.0
PIXELFORGE_PORT=8080
PIXELFORGE_LOG_LEVEL=DEBUG
PIXELFORGE_ADB_SERVER_HOST=192.168.1.100
EOF

# 或直接设置环境变量
export PIXELFORGE_PORT=8080
export PIXELFORGE_ADB_SERVER_HOST=192.168.1.100
pixelforge serve
```

### 部署形态

**本地优先的 Web 应用**——浏览器界面 + Python 服务，但服务必须跑在能物理接触到
手机的那台机器上。不是桌面程序，也不是 SaaS。三种部署方式（直接跑 / Docker +
宿主 adb / Linux USB 直通）和 macOS 上 Docker 看不到 USB 这个硬约束，见
[docs/DEPLOY.md](./docs/DEPLOY.md)。

### 受限网络环境

如果 `pip install` 报 403 或找不到包，说明出口策略拦了 PyPI。
`scripts/bootstrap-offline.sh` 从 GitHub 源码装纯 Python 依赖（本项目就是这么开发的）。

## 测试

```bash
cd backend && pytest
```

覆盖：ADB 协议帧 · serial 注入防护 · 租约独占/过期/fencing · 假 adb server 上的 track-devices
端到端 · **坐标换算完整矩阵（旋转 × 缩放 × 编码器对齐）** · **前后端坐标实现一致性（跑真实 JS 比对）**
· scrcpy 控制报文线格 · 视频流任意分块解复用 · 模板匹配（ROI/掩码/多尺度/零方差拒绝）·
OCR TSV 解析与真实识别 · FLAG_SECURE 诊断 · 执行器降级/重试/断言/人工接管 · 调试器断点与单步 ·
导出降级警告 · 项目存储路径穿越防护 · API 全链路。

---

## 真机验收清单

代码逻辑已验证，设备交互没有。接上手机后按顺序确认：

1. **设备发现** —— 插拔手机，列表毫秒级更新（非轮询）
2. **投屏延迟** —— 手机开秒表，与画面对拍，应 < 150ms
3. **坐标精度** —— 画面四角 + 中心各点一次，误差应为 0px；旋转 90/180/270 后重测
4. **取材无损** —— 框选一块已知图案，比对导出 PNG 与设备实际像素的哈希
5. **中文输入** —— 输入框里打中文（`adb shell input text` 做不到这件事）
6. **控件反查** —— 点画面应高亮对应控件并读出 `resource-id`
7. **断点接管** —— 脚本在断点暂停时手动操作真机，再继续，状态应正确
8. **多设备并发** —— 3 台同时运行互不干扰

第 3 和第 4 条是成败点。坐标偏 3 像素这种 bug 人工点击测不出来，必须按矩阵测。

---

## 设计要点

几条容易踩、代价又高的约束，细节见 ARCHITECTURE.md：

1. **坐标只在 `geometry/mapper.py` 一处换算。** 唯一的例外是前端 `geometry.js`（十字光标不能每次移动都往返一趟），它由 `test_frontend_parity.py` 跑真实 node 比对锁死。这个测试第一次跑就抓到了一个真 bug。
2. **视频流和截图是两条通道。** H.264 有损，从视频帧裁出的模板与无损 `screencap` 匹配时分数会无规律漂移——比直接失败难查得多。
3. **四策略降级链**（控件 → 模板 → OCR → 坐标）是通用化的核心。录制时一次采全四种特征，回放按序降级，所以同一套工具能同时覆盖普通 app 和游戏，不用让用户选模式。
4. **失败必须可区分。** "没找到"和"找到了但点偏了"需要完全相反的修法，所以每步都记录前后截图和定位分数。
5. **导出降级必须报警。** 静默丢掉一个断言，会产出在 IDE 里通过、在生产里少做事的脚本——最糟的失败形态。
6. **核心不含业务知识。** 面向某个具体执行引擎的导出器编码的是那个引擎的 schema，放进核心就等于让通用工具替某家公司记格式——第二家进来时注册表会变成一张别人格式的清单。所以这类导出器走插件（`PIXELFORGE_EXPORTER_PLUGINS`），`examples/exporters/` 有完整可抄的例子。
7. **能力缺失不阻断启动。** 缺 adb / scrcpy jar / tesseract 各自只关掉一项能力，如实上报。

## 四个扩展点

通用化不靠功能开关，靠把会因业务而变的部分收进接口：

| 扩展点 | 职责 | 内置实现 |
|--------|------|---------|
| `DeviceProvider` | 设备从哪来 | `LocalAdbProvider`（DeviceFarmer 按此接口接入） |
| `Locator` | 元素怎么找 | a11y / template / ocr / coord |
| `Exporter` | 产物给谁执行 | pixelforge / pytest（+ 目录插件） |
| `Listener` | 监听什么 | logcat（mitmproxy、pcap 已声明未实现，理由见 registry） |

## 目录

```
backend/src/pixelforge/
├── adb/          ADB host 协议 · 异步客户端
├── device/       注册 · 租约 · 会话 · 截图 · 控件树 · FLAG_SECURE 诊断
│   └── scrcpy/   控制报文 · 视频解复用 · 服务端生命周期
├── geometry/     ⚠️ 唯一坐标换算点
├── vision/       模板匹配 · OCR · 画面稳定检测
├── locators/     四策略与降级链
├── script/       模型 · 执行器 · 调试器 · 设备接口
├── exporters/    导出插件
├── listeners/    监听插件
├── timeline/     统一事件总线
├── store/        项目与模板持久化
├── api/ ws/      REST 与 WebSocket
frontend/         零构建 ESM：播放器 · 覆盖层 · 编辑器 · 时间线
```
