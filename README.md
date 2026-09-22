# PixelForge

通用型 Android 真机可视化自动化 IDE。实时投屏、精确坐标拾取、无损素材取材、四策略元素定位、流程录制与断点调试。

- 产品定位、功能范围与开发周期 → [PRODUCT.md](./PRODUCT.md)
- 技术架构与设计决策 → [ARCHITECTURE.md](./ARCHITECTURE.md)
- 文档中心与待评审的图形化图片实验本方案 → [docs/README.md](./docs/README.md)

---

## 进度

两列状态，因为它们是两件不同的事。**代码**＝写完且有单元测试；**真机**＝在
实际手机上跑通过。所有单元测试用的都是假 adb server、合成帧、构造出来的协议
字节——它们能证明代码符合我对协议的理解，不能证明真机上能用。

| 阶段 | 内容 | 代码 | 真机 |
|------|------|------|------|
| **P0** | adb 客户端 · 设备注册 · 独占租约 | ✅ | ⬜ |
| **P1** | scrcpy 协议编解码 · 视频流解复用 · 会话管理 | ✅ | ⬜ |
| **P2** | CoordinateMapper 四坐标空间 · 控制通道 | ✅ | ⬜ |
| **P3** | 无损截图双模式 · 框选裁剪 · 模板库 | ✅ | ⬜ |
| **P4** | UiAutomator2 · 模板匹配 · OCR · 四策略降级链 · FLAG_SECURE 诊断 | ✅ | ⬜ |
| **P5** | Step/Target/Project 模型 · 执行器 · 录制 | ✅ | ⬜ |
| **P6** | 断点 · 单步 · 人工接管 · 步骤时间线 | ✅ | ⬜ |
| **P7** | Exporter 插件机制（原生 / pytest / 第三方插件） | ✅ | ⬜ |
| **P8** | Listener 插件 · logcat · 统一时间线 | ✅ | ⬜ |
| **P9** | 前端 IDE · API 装配 · 部署 | ✅ | ⬜ |

**后端测试 421 项全通过（2026-09-22），真机验收 0 项。** 这两个数字之间的差距就是本项目
当前的真实风险：投屏、点击、取材、控件反查这些核心路径，一次都没在真手机上
执行过。`vendor/scrcpy-server.jar` 和 uiautomator2 APK 也从未装入过，所以
P1/P4 的设备侧代码是纯静态的。

**别把"代码 ✅"当成功能可用。** 验收清单见下，第 3、4 条是成败点。

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
# 放在 backend/vendor/scrcpy-server.jar（vendor 在 backend 下，不是仓库根）

# 控件选择器（可选）
# 去 https://github.com/appium/appium-uiautomator2-server/releases 下载 .apk
# 存到 backend/vendor/ 即可

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

> ⚠️ **只能单 worker 运行。** 设备会话、scrcpy 连接、租约、运行中的脚本都是进程内状态。
> `--workers N` 会让请求随机落到没有该设备会话的进程上，症状是"能用，但偶尔莫名 409"。

### 四项可选依赖

缺了不会导致启动失败，只关掉对应能力。`pixelforge doctor` 和 `/api/health` 都会如实报告：

| 依赖 | 缺失时失去 | 仍可用 | 获取方式 |
|------|-----------|--------|---------|
| **adb** | 全部设备功能 | —（唯一致命的） | `brew install android-platform-tools` |
| `backend/vendor/scrcpy-server.jar` | 实时画面、低延迟控制 | 截图、控件树、OCR、模板 | scrcpy releases 里对应版本的 jar |
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

# 换个端口（一台机器上同时挂多台设备时避免撞车）
pixelforge tcpip --device a1b2c3d4 --port 5556
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

# adb server 端口（默认 5037，即全机共用的那个）
# 一台 USB 设备同一时刻只能被一个 adb server 认领。用私有端口（如 5038）意味着
# 只要别的工具（你的终端、Android Studio、scrcpy）先起了 5037 的 server，这边就
# 永远看不到设备。只有在确定机器上没有别的 adb 时（容器、CI）才值得隔离。
# 设备列表为空时，界面会自动调 GET /api/adb/probe 告诉你设备在哪个端口手里。
PIXELFORGE_ADB_SERVER_PORT=5037

# adb 命令超时（秒，默认 15）
PIXELFORGE_ADB_TIMEOUT_S=30

# --- 设备来源 ---------------------------------------------------------------
# local（默认，本机 USB/无线 adb）或 devicefarmer（接 STF 设备池）
PIXELFORGE_DEVICE_PROVIDER=local

# 选 devicefarmer 时才需要这四项
PIXELFORGE_DEVICEFARMER_URL=https://stf.example.com
PIXELFORGE_DEVICEFARMER_ACCESS_TOKEN=<token>
PIXELFORGE_DEVICEFARMER_POLL_INTERVAL_S=3.0
PIXELFORGE_DEVICEFARMER_VERIFY_SSL=true

# --- 存储 -------------------------------------------------------------------
# 数据目录（项目、模板、截图，默认 backend/.pixelforge/）
PIXELFORGE_DATA_DIR=~/.pixelforge

# 框选 PNG 的默认保存目录（默认 PixelForge 项目根目录）
# ⚠️ 默认值落在仓库里，裁出来的图会进工作树，容易被误提交
#    （仓库里那个 statice/crop_*.png 就是这么来的）。建议指到仓库外面。
PIXELFORGE_ASSET_DIR=~/Desktop/PixelForgeAssets

# 租约过期时间（秒，默认 30）
PIXELFORGE_LEASE_TTL_S=60

# 导出插件目录。列表型配置必须写 JSON 数组——pydantic-settings 对 list 字段
# 只认 JSON，冒号或逗号分隔会在启动时抛 SettingsError 直接起不来。
PIXELFORGE_EXPORTER_PLUGINS=["/path/to/plugins1","~/plugins2"]

# Web 服务主机（默认 127.0.0.1）
PIXELFORGE_HOST=0.0.0.0

# Web 服务端口（默认 8420）
PIXELFORGE_PORT=9000

# CORS 源地址（默认 ["http://localhost:5173"]）。同上，必须是 JSON 数组。
PIXELFORGE_CORS_ORIGINS=["http://localhost:3000","http://localhost:8080"]

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
`bash scripts/bootstrap-offline.sh` 从 GitHub 源码装纯 Python 依赖（本项目就是这么开发的）。

## 常见问题与故障排除

### 启动问题

**Q: `pixelforge serve` 报错 "adb not found"**
- A: 需要安装 Android 开发工具包。macOS: `brew install android-platform-tools`；Linux: `apt install adb`

**Q: 访问 http://127.0.0.1:8420/ 显示 404**
- A: 可能是前端文件未找到。运行 `pixelforge doctor` 检查 `frontend` 能力。确保 `frontend/` 目录存在且有 `index.html`。

**Q: 启动时报 "no space left on device"**
- A: 检查数据目录是否满盘。默认存在 `.pixelforge/` 下，可通过 `PIXELFORGE_DATA_DIR` 改到其他位置。

### 设备连接问题

**Q: `pixelforge devices` 显示空列表或设备显示 "unauthorized"**
- A: 检查设备是否开启 USB 调试：设置 → 开发者选项 → USB 调试。第一次连接会弹授权提示，点击"始终允许"。

**Q: USB 中途掉线，无法重新发现设备**
- A: 用无线调试替代。先连一次 USB，运行：
  ```bash
  pixelforge tcpip --device <serial>
  pixelforge connect <IP>:5555
  ```

**Q: 手机接 Mac 热点后扫不到设备**
- A: MIUI 开了"USB 网络共享"会把 adb 从 USB 配置摘掉。关掉"设置 → 更多连接方式 → USB 网络共享"，或直接用无线 adb。

### 功能缺失

**Q: 为什么没有实时投屏？**
- A: 需要 scrcpy-server.jar。去 https://github.com/Genymobile/scrcpy/releases 下载对应版本，放在 `backend/vendor/scrcpy-server.jar`，然后重启。

**Q: 为什么截图功能灰了？**
- A: 通常是 `backend/vendor/scrcpy-server.jar` 缺失。运行 `pixelforge doctor` 看具体是什么缺了。

**Q: OCR 定位不工作**
- A: 需要 Tesseract。macOS: `brew install tesseract tesseract-lang`；Linux: `apt install tesseract-ocr`。装好后重启服务。

### 性能问题

**Q: 操作延迟很高（响应慢）**
- A: 检查：
  1. 网络延迟：`ping <device_ip>` 看是否 >100ms
  2. 视频编码：投屏画质设得太高会占用设备 CPU，试试降低分辨率
  3. 设备性能：老设备可能吃不消实时投屏 + 录制

**Q: 内存占用很大**
- A: 检查 `pixelforge run` 是否卡住（没正常退出）。这会让前面执行器的内存泄漏（进程内状态）。

### 脚本执行

**Q: `pixelforge run` 报 "device not found"**
- A: 确保设备在线（`pixelforge devices` 看得到），且 serial 拼对了。

**Q: 脚本执行到一半停止，没有错误信息**
- A: 设置 `PIXELFORGE_LOG_LEVEL=DEBUG` 看详细日志，或在 IDE 里加断点调试。

**Q: 导出的 pytest 脚本在 CI 里跑失败**
- A: 导出的脚本仍需要运行时指定设备（通过 `--device` 环境变量或 adb serial）。参考生成脚本的注释。

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
