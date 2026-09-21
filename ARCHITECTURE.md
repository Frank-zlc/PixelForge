# PixelForge 架构设计（v3）

> 通用型 Android 真机可视化自动化 IDE
>
> 产品定位、功能范围与开发周期见 [PRODUCT.md](./PRODUCT.md)。本文只讲技术架构。
>
> **v3 相对 v2 的核心变更**：工具定位由「AlbionHelper 的编辑器」改为「通用 IDE」。因此解除对 AlbionHelper 的依赖，引入四个插件扩展点，Step 模型独立且更富。v1/v2 的勘误见 §11。

---

## 1. 依赖方向：单向，不可反转

v2 建议 PixelForge 依赖 AlbionHelper 的 `cli` 包以共享 `WorkflowProfileStep` schema。**作为通用工具这是错的。**

AlbionHelper 的 `WorkflowProfileStep` 只有 6 个 action、没有 `swipe`、没有断言、只支持 OCR 文本匹配。把它当作 PixelForge 的内部模型，等于让**第二个业务一进来就必须改公共 schema** —— 而那个 schema 正在生产环境被 `WorkflowRunner` 执行。

```
        ┌─────────────────────────────────────────┐
        │  PixelForge  （自有 Step 模型，更富）      │
        └──────────────────┬──────────────────────┘
                           │  Exporter 插件（有损降级 + 明确报错）
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
   AlbionHelper        pytest          其他执行引擎
   WorkflowProfiles    测试文件         Appium / 自研
```

**PixelForge 不 import 任何下游业务的模块。** 转换不了的时候明确报错，而不是悄悄丢信息：

```
⚠ 步骤 4 使用模板匹配定位，AlbionHelper profile 不支持 wait_image。
  可选：① 改用 OCR 文本定位  ② 降级为坐标  ③ 扩展 AlbionHelper 的 action 枚举
```

这条提示本身就是产出 —— 它告诉你下游该往哪扩展。

---

## 2. 分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│  前端 · React + TypeScript + Vite                                │
│                                                                 │
│   StepList     │  DeviceCanvas       │  Inspector               │
│   步骤列表      │  H.264 实时画面       │  控件树 / OCR 文本框       │
│   拖拽重排      │  + 坐标十字 (4 空间)   │  点画面反查控件            │
│   断点标记      │  + 框选取材           │  步骤属性编辑             │
│  ──────────────┴─────────────────────┴────────────────────────  │
│   Timeline   步骤 · 截图 · 匹配分数 · 网络事件 · logcat  同轴对齐   │
└───┬──────────┬───────────┬────────────┬─────────────────────────┘
    │WS video  │WS control │WS run      │REST
┌───┴──────────┴───────────┴────────────┴─────────────────────────┐
│  后端 · FastAPI + asyncio（单进程）                               │
│                                                                 │
│  ┌── 设备层 ─────────────────────────────────────────────────┐   │
│  │ DeviceRegistry   host:track-devices 长连接（非轮询）        │   │
│  │ LeaseManager     独占租约 + fencing token + TTL 心跳        │   │
│  │ DeviceSession    每设备一个，持有下列子会话                  │   │
│  │   ├ ScrcpySession   H.264 流 + 控制通道                    │   │
│  │   ├ ScreencapPort   无损按需截图（独立于视频流）             │   │
│  │   ├ UiAutomatorPort 常驻控件树服务                          │   │
│  │   └ SecureProbe     FLAG_SECURE 主动诊断                    │   │
│  └───────────────────────────────────────────────────────────┘   │
│                                                                 │
│  CoordinateMapper   ⚠ 全系统唯一坐标换算点                        │
│  VisionPool         ProcessPoolExecutor：模板匹配 / OCR / 稳定检测 │
│  ScriptRuntime      Step 解释器 + dev SDK + 调试器                │
│  TimelineBus        所有事件汇流，单调时钟对齐                     │
│  ProjectStore       SQLite 元数据 + 文件系统素材                   │
│                                                                 │
│  ┌── 插件注册表（四个扩展点，见 §3）──────────────────────────┐    │
│  │ DeviceProvider │ Locator │ Exporter │ Listener            │    │
│  └───────────────────────────────────────────────────────────┘    │
└───┬─────────────────────────────────────────────────────────────┘
    │ adb（vendor 二进制，独立 server 端口 5038）
┌───┴─────────────────────────────────────────────────────────────┐
│  Android 真机                                                    │
│  scrcpy-server.jar  │  uiautomator2-server.apk  │  目标 app      │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. 四个插件扩展点

通用化不靠「功能开关」，靠**把会因业务而变的部分收进四个接口**。核心引擎对业务零认知。

### 3.1 `DeviceProvider` —— 设备从哪来

```python
class DeviceProvider(Protocol):
    name: str

    async def discover(self) -> list[DeviceInfo]: ...
    async def watch(self) -> AsyncIterator[DeviceEvent]: ...
    async def acquire(self, serial: str, ttl_s: int) -> ProviderLease: ...
    async def release(self, serial: str) -> None: ...
    async def adb_endpoint(self, serial: str) -> str: ...
```

内置实现：

| 实现 | 说明 |
|------|------|
| `LocalAdbProvider` | 本地 USB / 无线调试，走 `host:track-devices` |
| `DeviceFarmerProvider` | 对接已部署的 DeviceFarmer：`GET /api/v1/devices` 拿清单，`POST /api/v1/user/devices/{serial}` 预约，再 `adb connect` 到它给的端点 |

`adb_endpoint()` 是关键抽象 —— 无论设备来自本地 USB 还是远程设备池，上层只拿到一个 adb 可达地址，后续链路完全一致。**这也是复用 DeviceFarmer 而不被它绑架的那条缝。**

### 3.2 `Locator` —— 元素怎么找

这是通用化最重要的扩展点。四个内置策略组成降级链：

```python
class Locator(Protocol):
    name: str
    def can_handle(self, target: Target) -> bool: ...
    async def locate(self, ctx: LocateContext, target: Target) -> LocateResult:
        """返回命中框（设备像素）+ 置信度 + 证据（截图/匹配图/控件节点）"""
```

| 策略 | 适用 | 跨机型 | 耗时 |
|------|------|:---:|:---:|
| `A11yLocator` | 有无障碍信息的 app（场景 A） | ✅ 稳定 | 100–400ms |
| `TemplateLocator` | 游戏 / Canvas / 自绘（场景 B）。多尺度 + 掩码 + 画面稳定检测 | ⚠️ 需同分辨率 | 50–200ms |
| `OcrLocator` | 有文字但无 a11y | ⚠️ 受字体渲染影响 | 200–800ms |
| `CoordLocator` | 兜底 | ❌ 仅同机型 | 0ms |

**录制时一次性采集全部四种特征**，回放时按 `target.strategy` 顺序尝试，任一命中即停。这是同一份脚本能跨业务、跨机型复用的机制。

### 3.3 `Exporter` —— 产物给谁执行

```python
class Exporter(Protocol):
    name: str
    def export(self, project: Project, script: Script) -> ExportResult:
        """返回产物文件 + 降级警告列表。表达不了的必须报警告，不得静默丢弃。"""
```

内置：`PixelForgeJsonExporter`（原生全量）、`AlbionHelperExporter`（降级到 `WorkflowProfiles`）、`PytestExporter`（生成可进 CI 的测试）。

### 3.4 `Listener` —— 监听什么

```python
class Listener(Protocol):
    name: str
    async def start(self, ctx: DeviceContext, bus: TimelineBus) -> None: ...
    async def stop(self) -> None: ...
```

内置 `LogcatListener`、`MitmproxyListener`（HTTP/HTTPS）、`PcapListener`（原始 UDP/TCP，供 Photon 这类自定义协议接入）。

所有 Listener 把事件推进 `TimelineBus`，由它统一按**单调时钟**对齐到步骤时间线。抓包解析本身不是 PixelForge 的职责 —— 它只负责对齐和展示。

---

## 4. 核心数据模型

```python
class Target(BaseModel):
    """多策略定位目标。录制时全采，回放时按 strategy 顺序降级。"""
    strategy: list[Literal["a11y", "template", "ocr", "coord"]]
    a11y:     A11ySelector | None = None   # resource_id / text / desc / class / xpath
    template: TemplateRef   | None = None   # file / roi / threshold / mask / scales
    ocr:      OcrMatch      | None = None   # pattern / roi / lang
    coord:    NormCoord     | None = None   # x,y ∈ [0,1]
    recorded_on: DeviceProfile              # size / density / rotation / sdk

class Step(BaseModel):
    id: str
    name: str
    action: Literal[
        "tap", "long_press", "swipe", "gesture",     # 触控
        "input_text", "key",                          # 输入
        "launch_app", "stop_app",                     # 应用
        "wait", "wait_for",                           # 等待
        "assert", "screenshot", "script",             # 校验 / 取证 / 逃生舱
    ]
    target:       Target | None = None
    wait_before:  WaitCondition | None = None   # screen_stable / a11y_exists / ocr_text
    assert_after: Assertion | None = None
    on_fail:      Literal["abort", "retry", "continue", "human"] = "abort"
    retry:        int = 0
    timeout_ms:   int = 10_000
    params:       dict = {}                     # action 特有参数
```

对比 AlbionHelper 的 6 个 action：多了 `swipe` / `gesture` / `long_press`、断言、`wait_for` 的多种条件、`on_fail: human`（触发人工接管）、以及 `Target` 的四策略结构。

**`Project` 是业务隔离单元：**

```python
class Project(BaseModel):
    name: str
    device_filter: DeviceFilter      # 这个业务用哪些设备
    app_package: str | None
    templates_dir: Path              # 模板库（项目私有）
    scripts: list[Script]
    variables: dict[str, str]        # 含凭据引用，不存明文
    exporters: list[str]             # 启用哪些导出插件
    listeners: list[ListenerConfig]
```

多业务并存靠 Project 隔离，不靠代码分支。

---

## 5. 坐标系统 —— 全项目最关键

四套坐标空间，任意两套混用就会出偏移：

```
① CSS 像素      浏览器元素内鼠标位置。受 devicePixelRatio / 容器尺寸 / 缩放影响
② 视频帧像素    scrcpy 编码尺寸。因 max_size 缩放，且被向下对齐到 8 的倍数
③ 设备物理像素  wm size，如 1080×2400
④ 归一化 [0,1]  脚本持久化格式
```

再叠加旋转（0/90/180/270，且 app 可能锁定与系统不同的方向）。

**强制约束：**

1. 全工程**只有 `geometry/mapper.py` 一处**做坐标换算。其他任何文件不得出现坐标的乘除运算。这条要在 code review 里当硬规则查。
2. Mapper 状态全部来自设备上报，不靠推算：帧尺寸 ← scrcpy 流头部；设备尺寸 ← `wm size`；旋转 ← scrcpy 的 rotation 事件（实时推送）。
3. **点击利用 scrcpy 控制协议的一个好性质** —— 报文里同时带坐标和"发送方认为的屏幕尺寸"：

```
type(1) action(1) pointerId(8) x(4) y(4) width(2) height(2) pressure(2) ...
                                       └─────┬─────┘
                        设备端用这个尺寸把 x,y 缩放到真实显示器坐标
```

   所以后端**不需要**换算到设备物理像素，只要发出的 `(x, y, width, height)` 内部自洽即可。少一层换算就少一类 bug。

4. `tests/test_mapper.py` 必须覆盖「旋转 × 缩放 × 8 倍对齐」的完整矩阵。**这块单测不能省** —— 坐标偏 3 像素这种 bug，人工点击测不出来。

> ⚠️ 归一化坐标**不解决跨机型**：16:9 和 20:9 上同一个 `[0.5, 0.88]` 落在完全不同的元素上。跨机型必须靠 `A11yLocator` 或 `OcrLocator`。UI 上要对「仅坐标定位」的步骤显式标注风险。

---

## 6. 双路取图：视频流与截图必须分开

这是 v1 就定下、至今不变的一条硬约束。

| 用途 | 通道 | 画质 | 延迟 |
|------|------|------|------|
| 实时观察、点击交互 | scrcpy H.264 流 | **有损**（4:2:0 + 量化） | 50–120ms |
| 框选取材、模板生成、失败取证 | `adb exec-out screencap` | **无损** | 200–800ms |

**为什么不能合并：** 从 H.264 帧裁出的模板图带压缩伪影，回放时与无损 `screencap` 做 `matchTemplate`，分数会**无规律漂移**。这个 bug 的表现是"有时能匹配有时不能"，极难定位。

一个通道同时满足「低延迟」和「无损」在技术上不成立，所以双路是必须，不是优化。

前端框选时，后端**重新取一张无损截图再裁**，不从视频帧裁。

---

## 7. 脚本引擎与调试器

### 7.1 双层：Python 写逻辑，Step 作产物

```
┌─ 编写层（本地，你人在跟前）────────────────────────────┐
│  完整 Python + dev SDK。不需要沙箱 —— 这是你自己的机器。 │
│     await dev.tap(target)                             │
│     await dev.wait_for(ocr="总价", timeout=5)          │
│     shot = await dev.crop(roi)                        │
└──────────────────┬───────────────────────────────────┘
                   │  录制 / 导出
                   ▼
┌─ 产物层（可回放、可导出、可进 CI）──────────────────────┐
│  list[Step]  ──Exporter──▶  各执行引擎                 │
└──────────────────────────────────────────────────────┘
```

Python 保留完整表达力（循环、条件、import 你自己的库），Step 列表保证可视化编辑与可导出。两者不是二选一。

### 7.2 调试器：SDK 调用即步骤边界

**不要实现 `sys.settrace`。** 有个便宜得多的办法 —— 把每次 `dev.*` 调用当成隐式步骤边界：

```python
class Dev:
    async def tap(self, target: Target) -> None:
        async with self._step("tap", target):          # ← 断点/单步/取证都在这里
            box = await self.locate(target)
            await self.scrcpy.inject_tap(box.center)

    @asynccontextmanager
    async def _step(self, action: str, target=None):
        before = await self.screencap()                 # 无损，不是视频帧
        await self.bus.emit("step_start", action=action, shot=before)
        if self.paused or self.cursor in self.breakpoints:
            await self.wait_resume()                    # ← 可在此手动操作真机再继续
        try:
            yield
        finally:
            await self.bus.emit("step_end", shot=await self.screencap())
```

免费拿到：步骤时间线、每步前后截图、断点暂停、**暂停时人工接管真机再继续**。最后这条是这类工具最有用的功能 —— 脚本卡住时你能直接上手拨一格。

要行级断点和变量查看就 `debugpy.listen()` 让 VS Code attach。**不自己写调试器 UI**，IDE 做得更好。

### 7.3 失败必须可区分

每步失败时，时间线要能一眼分清三种情况：

```
✗ step_4  wait_for(template=btn_confirm)
          最高分 0.62 < 阈值 0.90        →  没找到（模板过期？页面没到？）
          [匹配热力图]

✗ step_5  tap(coord=[0.5, 0.88])
          已点击，但 assert_after 失败    →  点偏了 / 点击被拦截

✗ step_6  tap(a11y=btn_login)
          控件树为空 + 画面大块纯黑        →  FLAG_SECURE（见 §9）
```

这三种的修复动作完全不同。区分不了就只能靠猜，这是自动化调试最大的时间黑洞。

---

## 8. 工程约束（违反会在多设备下卡死）

1. **adb 调用一律 `asyncio.create_subprocess_exec`**，绝不在 async 函数里 `subprocess.run`。一次阻塞的截图会卡住所有设备的视频流。
2. **CV / OCR 丢进 `ProcessPoolExecutor`**。`matchTemplate` 和 pytesseract 单次几百毫秒纯 CPU，跑在事件循环里同上。
3. **不要 `uvicorn --workers N`**。scrcpy 连接、租约都是进程内状态，多 worker 会让请求随机落到没有该设备会话的进程上。要扩展就拆独立的 device-manager 进程 + Redis 共享状态。
4. **adb 二进制 vendor 进仓库，用独立 server 端口。** 默认 `127.0.0.1:5037` 是全局单例 —— 同事开一次 Android Studio，或任何人 `adb kill-server`，你所有会话瞬间全断。

```bash
vendor/platform-tools/adb -P 5038 start-server
export ADB_SERVER_SOCKET=tcp:127.0.0.1:5038
```

5. **adb 调用只接受 argv，不接受 shell 字符串。** serial 走正则白名单。这是防止路径穿越和命令注入的最小成本做法。

---

## 9. FLAG_SECURE：不绕过，但要诊断清楚

Android 12 起 SurfaceFlinger 检查 `CAPTURE_BLACKOUT_CONTENT` 权限并把 shell 移出白名单：

```cpp
uid == AID_GRAPHICS || uid == AID_SYSTEM
                    || PermissionCache::checkPermission(sCaptureBlackoutContent, pid, uid)
```

scrcpy、minicap、`screencap` **三者走同一路径，一律黑屏**。换工具无效。作为通用工具，这个风险不能像单业务那样排除。

**`SecureProbe` 的判据（P0 实现）：** 截图大块纯黑 **且** UiAutomator2 能取到非空控件树 → 判定 FLAG_SECURE，而非真黑屏。

```
⚠ 此页面受 FLAG_SECURE 保护，无法获取画面。
  控件树仍可用 → 可用 A11yLocator 定位。
  坐标拾取与模板匹配在此页面不可用。
```

用户 5 秒内知道原因，而不是花两小时怀疑自己配错。**这类"把无解问题快速诊断清楚"的能力，是通用工具与一次性脚本的分水岭。**

---

## 10. 目录结构

```
PixelForge/
├── PRODUCT.md
├── ARCHITECTURE.md
├── backend/
│   ├── pyproject.toml
│   ├── src/pixelforge/
│   │   ├── main.py                 FastAPI 入口
│   │   ├── config.py
│   │   ├── adb/
│   │   │   ├── client.py           异步 AdbClient（argv 白名单）
│   │   │   └── track.py            host:track-devices 协议
│   │   ├── device/
│   │   │   ├── registry.py         设备注册与事件广播
│   │   │   ├── lease.py            独占租约 + fencing
│   │   │   ├── session.py          DeviceSession 生命周期
│   │   │   ├── scrcpy/
│   │   │   │   ├── server.py       推 jar + app_process 拉起
│   │   │   │   ├── video.py        NAL 拆帧（不解码）
│   │   │   │   └── control.py      控制协议编解码
│   │   │   ├── screencap.py        无损取图
│   │   │   ├── uiautomator.py      常驻控件树服务
│   │   │   └── secure_probe.py     FLAG_SECURE 诊断
│   │   ├── geometry/
│   │   │   └── mapper.py           ⚠ 唯一坐标换算点
│   │   ├── vision/                 模板匹配 / OCR / 稳定检测（ProcessPool）
│   │   ├── locators/               A11y / Template / Ocr / Coord
│   │   ├── script/
│   │   │   ├── model.py            Step / Target / Project
│   │   │   ├── sdk.py              dev SDK
│   │   │   ├── executor.py
│   │   │   └── debugger.py
│   │   ├── exporters/              pixelforge_json / albionhelper / pytest
│   │   ├── listeners/              logcat / mitmproxy / pcap
│   │   ├── timeline/bus.py
│   │   ├── store/                  SQLite + 文件系统
│   │   ├── api/                    REST 路由
│   │   └── ws/                     WebSocket 端点
│   ├── vendor/
│   │   ├── scrcpy-server.jar
│   │   ├── platform-tools/         锁版本的 adb
│   │   └── uiautomator2-server*.apk
│   └── tests/
│       ├── test_mapper.py          ⚠ 旋转 × 缩放 × 对齐 完整矩阵
│       ├── test_track_protocol.py
│       └── test_lease.py
└── frontend/
    ├── package.json
    └── src/
        ├── player/                 WebCodecs 解码 → canvas
        ├── overlay/                坐标十字 / 框选 / 控件高亮
        ├── editor/                 步骤列表 + Monaco
        ├── inspector/              控件树 / 属性面板
        └── timeline/               步骤 · 网络 · 日志 同轴
```

---

## 11. v1 / v2 勘误

| 说法 | 修正 |
|------|------|
| v1「DeviceFarmer 用 `adb shell input tap`，太慢」 | **错。** DeviceFarmer 用 minitouch（守护进程直写 `/dev/input/eventX`），< 10ms，支持多点手势。用 `input tap` 的是 AlbionHelper 自己的 `adb.py`。结论仍成立（该换掉），但批评对象错了。 |
| v1「FLAG_SECURE 是 P0 阻塞」 | 对单业务是过度警告（AlbionHelper 的 OCR 已跑通，证明该游戏没设）。但**对通用工具不可排除**，处理方式见 §9。 |
| v2「PixelForge 依赖 AlbionHelper 的 cli 包共享 schema」 | **错。** 通用工具不能被一个业务的 schema 绑死。依赖方向必须单向，AlbionHelper 降级为 Exporter 插件。见 §1。 |
| v2「PixelForge = AlbionHelper WorkflowProfiles 的可视化 IDE」 | 定位过窄。正确定位见 PRODUCT.md §1。 |

---

## 附：参考资料

- [scrcpy #3049 — Android 12+ FLAG_SECURE 权限变更与 root 绕过](https://github.com/Genymobile/scrcpy/issues/3049)
- [scrcpy #2212 — Disable secure screen content on Android Q+](https://github.com/Genymobile/scrcpy/pull/2212)
- [Android 12 对 scrcpy 能力的限制](https://www.androidpolice.com/2021/06/21/screen-recorder-scrcpy-gains-android-12-support-but-google-severely-limited-its-capabilities/)
