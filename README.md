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

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # 按需修改；.env 不入库

pixelforge doctor           # 先看缺什么，每项都说明缺了会失去什么
pixelforge serve
```

打开 <http://127.0.0.1:8420/> 即是 IDE，<http://127.0.0.1:8420/docs> 是 API 文档。

其他命令：`pixelforge devices` 列设备、`pixelforge run <项目> <脚本> --device <serial>`
无头执行（CI 入口，失败返回非零）、`pixelforge export <项目> <脚本> --exporter <名字>`。

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
