# A1 · 图像能力库与实验台

> 研究记录：本备选方案的有效机制已整理进 [PF-DES-001](001-2026-09-22-visual-image-notebook/README.md) 及其数据模型、接口、交付文档。该方案已按用户指示开始 N1 开发。本文保留供追溯，不作为后续开发依据；其中的估时和部分 MHXY 算法评价未获验证。

> **后续修正**：本文已与 [PF-DES-001](001-2026-09-22-visual-image-notebook/README.md) 做过对比，见 [A2 对比与合并建议](A2-comparison-and-merge.md)。其中 §8 列出了本文被推翻的 6 处判断（尤其 §2.2 对 `template_similarity_check` 的评价、§D5 的线性链模型、§8 的估时）。阅读本文时请同时看那份清单；建议的处置是本文让位于 001，机制部分并入它缺失的三份子文档。

| 项 | 值 |
| --- | --- |
| 编号 | A1（A = 备选方案，与仓库中已有的 001 号方案并行，供对比评审） |
| 状态 | **已归档** — 备选研究；正式开发方案为 PF-DES-001 |
| 日期 | 2026-09-22 |
| 影响范围 | `backend/src/pixelforge/vision/`、新增 `ops/`、`api/`、`frontend/`、新增一个 SQLite 库 |
| 前置阅读 | [ARCHITECTURE.md](../../ARCHITECTURE.md) §3 四个插件扩展点、§8 工程约束 |
| 作者说明 | 见文末「污染声明」——本文不是在完全隔离的条件下写的 |

---

## 0. 文档规范（本文同时是这条规范的样例）

设计文档放 `docs/design/`，一个方案一个文件，需要配图或附件时才升级成同名目录：

```
docs/design/
  A1-image-capability-library.md          ← 单文件方案
  A2-xxx/                                 ← 需要附件时
    README.md
    assets/
```

**命名**：`<编号>-<英文 slug>.md`。编号一旦发出不再改，别的文档会引用它。

**每份文档开头必须有上面那张元信息表**，其中 `状态` 只有四个值，它决定了读者该怎么对待这份文档：

| 状态 | 含义 |
| --- | --- |
| 待评审 | 还没拍板，代码里不该有它的影子 |
| 已接受 | 拍板了，正在实施 |
| 已实现 | 代码追上了文档；此后改代码必须回头改文档 |
| 已废弃 | 保留原文，在开头写清被哪份取代、为什么 |

**方案被推翻时不要删除文件**，把状态改成「已废弃」并在开头加一段"为什么当时是错的"。PixelForge 已经吃过一次亏：`ARCHITECTURE.md` §8.4 当初论证要用私有 adb 端口 5038，理由成立但漏算了 USB 独占，结果实际用起来经常连不上设备。那条现在被标成勘误保留着——**被推翻的决策连同推翻理由，比一句正确的结论有用得多**，因为它拦住了下一个人再走一遍。

---

## 1. 要解决什么

你的原话是三件事，其实是一件：

1. 我想知道这个工具现在有多少可用功能 → **能力库页**
2. 我想用下拉框选一个工具，对图片做处理 → **参数面板 + 单步执行**
3. 我想要一个图形化的 Jupyter → **算子链**

第三件是前两件的超集。一个 Jupyter notebook 就是"一串有序的、可以反复改了重跑的调用"，每个 cell 选一个函数、给一组参数、拿到输出，输出喂给下一个 cell。把这件事做对，前两件是它的退化情况：能力库是"所有可用 cell 的清单"，单步执行是"只有一个 cell 的 notebook"。

**还有第四件你上一轮提过但这次没重复**：截图处理需要步骤回退。这个不需要单独做——算子链本身就是撤销栈（见 D5）。

### 不做什么

- 不做通用图像编辑器（画笔、图层、滤镜叠加）。这是自动化工具的能力库，不是 Photoshop。
- 不做真正的 Python 代码执行 cell。图形化 Jupyter 的"图形化"就是指参数面板取代代码输入；允许用户输入任意 Python 等于在本机开一个远程执行后门。
- 第一期不做批量处理。它是能力库的下游消费者，等能力库稳定了再说。

---

## 2. 现状盘点

### 2.1 PixelForge 已有的

| 位置 | 能力 | 备注 |
| --- | --- | --- |
| `vision/matching.py` | `match_template` | 多尺度 + ROI + 掩膜，比 MHXY 那版完整 |
| `vision/matching.py` | `diff_ratio` | 画面稳定性检测 |
| `vision/ocr.py` | `TesseractOcr` / `parse_tsv` | 带置信度和词框 |
| `geometry/mapper.py` | `Rect` / `Size` / 坐标换算 | 全系统唯一坐标换算点 |
| `api/capture.py` | 无损截图 | 能力库的图片来源之一 |
| `exporters/registry.py` | 插件注册表 | **能力库应该照着它写**，不要另造一套 |

### 2.2 MHXY 里你自己写的（这批才是能力库的真正内容）

盘完 `mhxy/tools/` 的 2454 行，能进能力库的**纯图像函数**：

| 源函数 | 做什么 | 价值判断 |
| --- | --- | --- |
| `images_tool.template_similarity_check` | 掩膜交集相似度：颜色掩膜或 Canny 轮廓求交集，`交集像素/模板像素` 得分 | **这是这批代码里最有价值的一个。** 它回答的是 matchTemplate 回答不了的问题——"匹配到的那块区域，真的长得像模板吗"。多尺度匹配给的是相关性分数，对纯色块、对渐变背景都会给出虚高的分。你这个二次校验是独立的证据。 |
| `images_tool.get_color_limit_from_hsv` | Y/R/W/G/B 五色 HSV 阈值 | 常量本身就是资产——是在真实画面上调出来的。**移植时必须逐字保留你的数值**，不要"顺手优化"。 |
| `ocr_tool.ocr_synthesis_words_img` | 双档亮度（180/150）按 0.7/0.3 加权合成 | 彩色文字增强，OCR 前处理的关键一步 |
| `dynamic_capture.img_coloer_optimization` | 按文字颜色筛选画面 | |
| `click_function.get_highlight_mask` | 高亮/选中态掩膜 | 判断"这个按钮是否被选中"，游戏和 App 都用得上 |
| `click_function.area_image_similarity` | 轮廓相似度 | 与 `template_similarity_check` 的轮廓分支重复，合并 |
| `images_tool.get_match_template_info` | 模板匹配，返回 box + 重心 | 与 PixelForge 的 `match_template` 重复，**保留 PixelForge 那版**，但要把"重心点"这个输出补上 |
| `dynamic_capture.get_words_xy` | 按颜色找文字位置 | 颜色筛选 + OCR 的组合 |
| `dynamic_capture.accurate_recognition` | 以 `min_pixel` 起步、每轮 `add_pixel` 扩大区域反复识别 | 逐步扩张搜索，很实用的一个模式 |
| `reserve_function.line_detect_possible_demo` | 霍夫直线检测 | |
| `reserve_function.get_box_area_frame` / `cut_img` | 区域裁剪 | 与 PixelForge 的 crop 重复 |
| `map_tool.get_map_red_dot_xy` | 小地图红点定位 | 业务味重，但"找某颜色的小色块"是通用的，抽象成 `color_blob_locate` |

**依赖设备或全局状态、不进能力库的**：`get_current_image`、`fly_map`、`clear_interface_player_booth`、`mobile_waiting`、以及 `map_tool` 里那一串 `jiu_dian` / `da_tang_guo_jing` 之类的地图点位方法。这些是业务脚本，属于 Step 模型那一侧。

### 2.3 移植必须先剥掉的东西

你这批函数**目前不能直接被 Web 服务调用**，原因不是算法，是副作用：

```python
# images_tool.py 里的真实代码
cv2.imwrite(os.path.join(BASE_DIR, r"mhxy\log\test\match_template_imgs\MT_target_img.png"), ...)
print('识别捕获区域: ', (x1, y1), (x2, y2))
logger.info('↓ sum_intersection：{} ...')
```

- **硬编码 Windows 路径**（`r"mhxy\log\..."`）——在 macOS 上就是一个带反斜杠的文件名
- **函数内部写盘**——并发调用会互相覆盖；Web 服务里这是数据竞争
- **`print` 当返回值用**——关键中间结果（相似度分子分母）只存在于 stdout
- **单例 `__new__` + 实例状态**——多请求并发时状态串台

这里有个转化，是本方案的关键手法之一：**那些 `cv2.imwrite` 调试图，恰恰就是实验台要显示的东西。** 它们不该被删掉，而该从"偷偷写盘的副作用"变成"函数正式返回的产物"（见 D2 的 `artifacts`）。你当初写这些 imwrite 是因为没有地方看中间结果——实验台就是那个地方。

### 2.4 仓库里已存在的一版实现

`backend/src/pixelforge/vision/tool_catalog.py`（未跟踪）已经实现了一版：12 条 `TOOLS` 元组、一张 `image_tools` 表、6 个可执行算子、一张 demo 图。**思路对，我沿用它三点**：分类维度、`availability` 三态（可用/仅设备/计划中）、`source` 标注来源。

三点我不同意，理由在 D1/D2/D3，这里先列结论：

1. 目录用 Python 元组手写、启动时 upsert 进 SQLite。等于同一份真相存了两份，而 `function_name` 只是一个**没人校验的字符串**——`"TBD"`、`"TesseractOcr.recognize"` 都只是标签。今天 `crop` 声明的实现是 `_crop`，而 `_crop` 的函数体是 `return image.copy()`，什么也没裁。这种漂移不会有任何报错。
2. `process_image()` 的签名是 `-> np.ndarray`，只能返回图像。于是 `template_match`、`ocr`、`screen_diff` 被标成 `device_only`，`match_verify` 被标成 `planned`。**但它们根本不需要设备**——`template_similarity_check(area_img, tem_img)` 是两张图片进、一个 bool 出的纯函数，你在 MHXY 里就是这么用的。把设计局限当成了能力边界。
3. 参数是 `dict[str, str]`，每个函数内部用 `_number()` 各自校验。这样前端没法自动生成参数控件，每加一个算子就要改一次前端。

---

## 3. 核心设计决策

### D1 · 代码是唯一真相，数据库只存代码不知道的事

目录**不手写**，由装饰器在导入时注册：

```python
@operator(
    id="template_similarity",
    category=Category.LOCATE,
    name="模板相似度校验",
    summary="按颜色掩膜或轮廓求交集，给出 0–1 相似度，用于模板匹配的二次确认",
    origin=Origin.MHXY,
    origin_ref="mhxy/tools/images_tool.py::ExtractFrameData.template_similarity_check",
    params=[...],
    needs=("template",),
    cost=Cost.FAST,
)
def template_similarity(image, *, template, mode, words_color, similarity) -> OpResult:
    ...
```

于是：

- `qualname`（`pixelforge.ops.locate.template_similarity`）**由注册表自动填**，不可能写错，也不可能指向一个不存在的函数。
- `availability` **是派生的**，不是填的：注册表里有函数 → `ready`；声明了 `needs=("tesseract",)` 但依赖不在 → `needs_dep`；只有元信息没有函数（占位）→ `planned`。文档和现实不会各说各话。
- 数据库里的 `image_operator` 表是**投影**，每次启动按注册表全量覆盖。它存在的唯一理由是让目录能被 SQL 查询和 JOIN（比如"按调用次数排序"要和 `image_run` 表连接）。

**判断标准**：一条信息如果代码能算出来，就不许手填。手填的那些（中文显示名、分类、一句话说明、来源出处）才写在装饰器里。

### D2 · 算子的输出不是图像，是 `OpResult`

这是本方案与已有实现分歧最大、也最要紧的一条。你的函数返回：掩膜图、box 字典、重心点、bool、OCR 文本列表、坐标。硬把返回类型定成 `np.ndarray`，等于把一半能力挡在门外。

```python
@dataclass(frozen=True)
class OpResult:
    image: np.ndarray | None = None              # 主输出图，没有就沿用输入
    regions: tuple[Region, ...] = ()             # 命中框，带 label 和 score
    points: tuple[LabeledPoint, ...] = ()        # 重心、红点、点击位
    scalars: Mapping[str, float] = field(default_factory=dict)   # 相似度、分子分母、比例
    text: tuple[TextSpan, ...] = ()              # OCR 结果，带置信度
    artifacts: Mapping[str, np.ndarray] = field(default_factory=dict)  # 中间图
    notes: tuple[str, ...] = ()                  # 给人看的解释
```

三个直接收益：

1. `template_match` / `ocr` / `template_similarity` / `diff_ratio` **立刻成为能力库的一等公民**，不再是 `device_only`。
2. `artifacts` 接住 MHXY 那些 imwrite：`{"颜色掩膜": mask, "模板掩膜": tem_mask, "交集": intersection}`。实验台把它们做成 tab，你一眼能看出相似度低是因为掩膜没框住字，还是因为模板本身选歪了。**这正是你在 MHXY 里写 imwrite 想得到的东西。**
3. `scalars` 让"为什么是这个结果"可见。`template_similarity` 返回 `{"similarity": 0.62, "交集像素": 18412, "模板像素": 29700}`——阈值该调到多少，看着数调，不用猜。

`regions` / `points` 的坐标一律是**该次输入图像的像素坐标**，由调用方负责映射回设备坐标，统一走 `geometry/mapper.py`。理由见 ARCHITECTURE：全系统只能有一个坐标换算点。

### D3 · 参数要声明式 schema，前端才能不改代码

```python
params=[
    Choice("mode", "比对方式", options=[("color", "颜色掩膜"), ("contour", "轮廓")], default="color"),
    Choice("words_color", "文字颜色", options=COLOR_CHOICES, default="Y", when={"mode": "color"}),
    Slider("similarity", "判定阈值", low=0.0, high=1.0, step=0.01, default=0.5),
    IntRange("low", "Canny 低阈值", 0, 255, default=50, when={"mode": "contour"}),
]
```

前端拿 schema 渲染控件：`Choice` → 下拉框，`Slider` → 滑块，`IntRange` → 数字框 + 滑块。**`when` 是条件显示**——选了轮廓模式就不该还显示"文字颜色"。没有它，参数面板很快会变成一堆互相矛盾的控件。

后端用同一份 schema 做校验，前后端不会对参数范围有两种理解。加新算子时前端**一行都不用改**——这是"我以后还要不断加功能"这个诉求的唯一可持续答案。

### D4 · 算子必须是纯函数

契约写进注册表的文档，并用测试强制：

- 不写盘、不打印、不读全局配置、不碰设备
- 不修改入参的像素（`process_image` 里已有这个意识，保留）
- 同样的输入必须给出同样的输出（不许用时间戳、随机数、当前设备状态）

**用测试守住**：一个参数化测试跑遍注册表里每个算子，断言 ①输入数组未被修改 ②连续两次调用结果逐像素相同 ③进程工作目录下没有新文件。第三条专治 imwrite 复发。

纯函数还有个硬收益：可以安全地丢进 `ProcessPoolExecutor`（ARCHITECTURE §8.2 要求 CV/OCR 必须这么干），也可以按 `(operator_id, params, input_sha256)` 做结果缓存。

### D5 · 算子链 = 图形化 Jupyter = 撤销栈

一个"实验本"就是：一张输入图 + 一串 cell。每个 cell = 算子 + 参数 + 可选 ROI。

```
输入图 ─→ [1 区域裁剪] ─→ [2 颜色掩膜 Y] ─→ [3 文字增强] ─→ [4 OCR]
                              ↑ 改了 s_min
                              └─ 3、4 的缓存作废，1 保留
```

- **改第 N 个 cell，只重算 N 及其下游**。每个 cell 缓存自己的输出（按内容哈希），上游没变就不重算。大图上这是体验的分水岭。
- **cell 可以禁用而不删除**（`enabled` 字段）。关掉第 3 步看看 OCR 会差多少——这是调参时最常做的动作，而"删了再加回来"会丢参数。
- **撤销就是 cell 列表的历史**，不需要另做一套 undo 栈。上一轮讨论布局时我说过"撤销栈绝不能混进时间线"——时间线记录"发生过什么"（不可回退的事实），实验本记录"我想怎么处理"（随时可改的意图）。两者语义相反。现在这个意图被具体化成了一个可编辑的文档。
- **最终它能导出成 Step**。调通的那条链，就是脚本里"找到这个元素"该用的那串参数。这是实验台存在的商业理由——否则它只是个玩具。

### D6 · 效果图由 golden fixture 生成，绝不手维护

`效果图` 字段如果靠人截图再填路径，三周后一半是过期的。做法：

- 每个算子在装饰器里声明 `demo`：用哪张 fixture、哪组参数。
- `pixelforge ops render-demos` 跑一遍，输出到 `docs/assets/ops/<id>.png`，把 sha256 写进目录表。
- CI（或 `pytest -m demos`）重跑一遍比对 sha256，**不一致就失败**——算法改了、效果图没更新，构建就红。

fixture 用两张：一张合成图（形状/颜色/文字都是已知的，回归测试用），一张真实的手机截图（从 MHXY 或你现有素材里挑一张，脱敏）。合成图保证可重复，真实图保证不自欺。

### D7 · 算子 id 是公开契约

`operator_id` 会被写进实验本、项目脚本、导出的 profile。一旦发出就**不能改名、不能改语义**。要改：新增一个 id，老的标 `deprecated` 并在目录里指向新 id，保留至少一个版本。目录表里为此留 `deprecated_by` 字段。

---

## 4. 数据模型

新建 `data/ops.db`，与现有 `ProjectStore` 分开。

**为什么要引 SQLite**——现有 `ProjectStore` 是文件系统 + JSON（`_atomic_write`），没有数据库。加一个存储机制必须有理由，而这里的理由只有一个半：

- **成立**：`image_run`（执行历史）和 `image_asset`（资产索引）需要按时间、按算子、按哈希查询和聚合。这些用 JSON 文件做，等于自己写一个查询引擎。
- **勉强成立**：目录表本身其实不需要数据库——它每次启动都从代码重建。留在库里只是为了能和 `image_run` JOIN（"哪些算子从来没被用过"）。
- **不成立的理由**：不要因为"你说要建表"就建表。如果评审下来你觉得历史和资产索引不重要，**那就不要这个库**，目录直接由 `GET /api/ops` 从内存注册表返回，一行 SQL 都不写。这是本方案里最该被挑战的一条。

```sql
-- 目录投影。每次启动按注册表全量覆盖，手改无效。
CREATE TABLE image_operator (
    id            TEXT PRIMARY KEY,   -- 公开契约，见 D7
    category      TEXT NOT NULL,
    name          TEXT NOT NULL,      -- 中文显示名
    summary       TEXT NOT NULL,      -- 一句话
    description   TEXT,               -- Markdown 详解
    qualname      TEXT NOT NULL,      -- 实现的完整限定名，自动填
    input_kind    TEXT NOT NULL,      -- image | image+template | image+text
    output_kind   TEXT NOT NULL,      -- image | regions | text | scalar | composite
    needs_json    TEXT NOT NULL,      -- ["template"] / ["tesseract"] / ["device"]
    availability  TEXT NOT NULL,      -- ready | needs_dep | planned，派生
    origin        TEXT NOT NULL,      -- pixelforge | mhxy | third_party
    origin_ref    TEXT,               -- 源函数位置，可追溯
    params_schema TEXT NOT NULL,      -- JSON
    demo_params   TEXT,               -- 生成效果图用的参数
    demo_path     TEXT,
    demo_sha256   TEXT,               -- 过期检测，见 D6
    cost          TEXT NOT NULL,      -- fast | heavy，决定前端是否实时预览
    deprecated_by TEXT,               -- 见 D7
    sort_order    INTEGER NOT NULL,
    synced_at     TEXT NOT NULL
);

-- 内容寻址：同一张图只存一份，实验本引用哈希而不是路径
CREATE TABLE image_asset (
    sha256     TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    channels   INTEGER NOT NULL,
    bytes      INTEGER NOT NULL,
    kind       TEXT NOT NULL,        -- upload | capture | output | template | fixture
    project_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE image_pipeline (        -- 一个实验本
    id            TEXT PRIMARY KEY,
    project_id    TEXT,
    title         TEXT NOT NULL,
    input_sha256  TEXT NOT NULL REFERENCES image_asset(sha256),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE image_pipeline_cell (   -- 一个 cell = 一次算子调用
    pipeline_id   TEXT NOT NULL REFERENCES image_pipeline(id) ON DELETE CASCADE,
    cell_id       TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    operator_id   TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    roi_json      TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,   -- 关掉≠删除，见 D5
    output_sha256 TEXT,                          -- 缓存；上游变动时置空
    note          TEXT,
    PRIMARY KEY (pipeline_id, cell_id)
);

-- 每次真实执行。目录说"有什么"，这张表说"什么真的好用"。
CREATE TABLE image_run (
    id            TEXT PRIMARY KEY,
    operator_id   TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    input_sha256  TEXT NOT NULL,
    output_sha256 TEXT,
    result_json   TEXT,              -- regions/points/scalars/text
    duration_ms   INTEGER NOT NULL,
    status        TEXT NOT NULL,     -- ok | error
    error         TEXT,
    pipeline_id   TEXT,
    cell_id       TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_run_operator ON image_run(operator_id, created_at);
```

`image_run` 是你没要、但我建议加的一张。有了它，"我有多少功能"能进一步变成"**哪些功能我真的在用、平均多快、失败率多少、哪些从来没人点过**"。半年后决定砍掉哪些算子时，这张表是唯一的依据。

---

## 5. 后端接口

```
GET  /api/ops                      目录：分类树 + 可用性 + 效果图缩略图 URL
GET  /api/ops/{id}                 详情：params_schema、来源、等价代码、调用统计
GET  /api/ops/{id}/demo.png        效果图，ETag = demo_sha256
POST /api/ops/{id}/run             单步执行：{input: sha256|multipart, params, roi}
                                   → {result, output_sha256, artifacts: {...}, duration_ms}
POST /api/assets                   上传图片 / 把当前设备截图入库 → sha256
GET  /api/assets/{sha256}.png
POST /api/pipelines                新建实验本
GET  /api/pipelines/{id}
PUT  /api/pipelines/{id}/cells     整体替换 cell 列表（增删改拖拽都是它）
POST /api/pipelines/{id}/run       {from_cell?} 从某个 cell 起重算，返回每 cell 结果
POST /api/pipelines/{id}/export    导出成 Step / Python 片段
```

落在 `api/ops.py`，注册表放 `vision/ops/`（按分类分模块：`basic.py` / `color.py` / `locate.py` / `text.py`），`vision/ops/registry.py` 照 `exporters/registry.py` 的模式写。

**`ops` 是 ARCHITECTURE §3 的第五个扩展点**。原来四个是 DeviceProvider / Locator / Exporter / Listener，现在加 Operator。第三方能用同一个装饰器往里加算子而不动核心——和现有四个扩展点的哲学一致。文档要同步更新。

---

## 6. 页面与菜单

接上一轮定下的 IDE 布局（活动栏 + 可折叠侧栏 + 中央 tab + 底部面板）。活动栏第三、四个图标：

| 页面 | 路由 | 内容 |
| --- | --- | --- |
| **能力库** | `#/ops` | 左：分类树 + 可用性/来源筛选。右：卡片网格，每卡=效果图缩略图 + 名称 + 一句话 + 徽章（可用/缺依赖/计划中、来源 PixelForge/MHXY）。顶部搜索框。**这一页回答"我现在有多少功能"。** |
| **能力详情** | `#/ops/{id}` | 左：原图 / 结果 / 中间产物（artifacts 分 tab）。右：自动生成的参数面板，拖动即重算（`cost=fast` 实时，`heavy` 防抖 300ms）。下：等价 Python 调用（可复制）+ 来源函数出处 + 调用统计。**这一页回答"这个功能什么效果"。** |
| **实验台** | `#/lab/{id}` | 左：cell 列表（拖拽排序、禁用、插入）。中：画布，**复用刚做好的 viewport 缩放平移**。右：当前 cell 的参数面板。底：该 cell 的 scalars 和 notes。**这一页就是图形化 Jupyter。** |

一页放不下的部分用详情页承接，不要堆在一页里。三页之间：能力库点卡片 → 详情；详情页"加入实验台"→ 以当前参数新建一个 cell。

从设备画面框选取材后，右键"送入实验台"——这条路径把设备工作台和图像工作台接起来，也是 `input_kind=image+template` 那些算子拿模板图的方式。

---

## 7. MHXY 移植清单

| 源函数 | 目标算子 id | 要剥掉的 | 期 |
| --- | --- | --- | --- |
| `get_color_limit_from_hsv` | 常量表 `COLOR_RANGES`（**数值逐字保留**） | — | P0 |
| `img_coloer_optimization` | `color_mask` | logger | P0 |
| `get_box_area_frame` / `cut_img` | `crop` | — | P0 |
| `ocr_synthesis_words_img` | `text_enhance` | — | P1 |
| `get_highlight_mask` | `highlight_mask` | — | P1 |
| `template_similarity_check` | `template_similarity` | imwrite ×2、print ×2、logger；分子分母改为 `scalars` 返回，掩膜与交集图改为 `artifacts` 返回 | **P1，最高优先** |
| `area_image_similarity` | 合并进 `template_similarity` 的 `mode=contour` | 同上 | P1 |
| `get_match_template_info` | 并入 PixelForge 的 `template_match`，补"重心点"输出 | imwrite ×2、print ×3 | P2 |
| `get_words_xy` | `locate_text_by_color`（color_mask + OCR 组合） | 设备依赖 | P2 |
| `accurate_recognition` | `expanding_search`（min_pixel/add_pixel 逐步扩张） | 设备依赖、点击动作 | P3 |
| `line_detect_possible_demo` | `detect_lines` | — | P3 |
| `get_map_red_dot_xy` | 抽象成 `color_blob_locate`（找指定颜色的小色块） | 地图业务逻辑 | P3 |
| `face_detect` | 不移植 | 与本工具场景无关 | — |

移植规则：**先写测试再改代码**。拿 MHXY 的真实截图当 fixture，断言移植后的纯函数在同样输入上给出与原函数一致的结果（相似度分数误差 < 1e-6）。不这么做，"顺手优化"会悄悄改掉你调了很久的阈值。

---

## 8. 分期与验收

| 期 | 内容 | 验收标准 | 估时 |
| --- | --- | --- | --- |
| **P0** | 注册表 + `OpResult` + 参数 schema + 纯函数测试；3 个算子（crop / grayscale / color_mask）；`GET /api/ops`、`POST /api/ops/{id}/run` | 新增一个算子只写一个装饰器函数，前端零改动就能在下拉框里选到它并跑出结果 | 2–3 天 |
| **P1** | 能力库页 + 详情页 + 参数面板自动生成；效果图生成 CLI + sha256 校验；`template_similarity` 移植 | 打开页面能看到全部算子及其效果图；拖动阈值滑块，相似度分子分母和中间掩膜实时变化 | 3 天 |
| **P2** | 实验台：cell 链、缓存失效、禁用、拖拽；资产库 | 4 步链条上改第 2 步，只有 2–4 重算；关掉第 3 步能立刻看到差异 | 3–4 天 |
| **P3** | 其余 MHXY 移植；OCR / template_match 接入；设备截图直送实验台 | 从设备框选一块模板，在实验台调通匹配+校验参数 | 3 天 |
| **P4** | 导出成 Step / Python；`image_run` 统计视图 | 实验台调通的链能变成脚本里的一步并跑通 | 2 天 |

P0 是地基，它要是错了后面全要返工；P1 结束就已经能回答你最初的问题（"我有多少可用功能、各自什么效果"）。**建议 P0+P1 做完先停下来用一周**，再决定 P2 怎么做。

---

## 9. 风险与取舍

| 风险 | 判断 |
| --- | --- |
| **大图实时预览卡顿** | 1080×2400 上跑 Canny + 掩膜约 10–30ms，滑块实时没问题；OCR 是百毫秒级，必须防抖。用 `cost` 字段区分，别一刀切。 |
| **SQLite 是不是多余** | 见 §4。目录确实不需要它，历史和资产索引需要。如果你觉得历史不重要，砍掉整个库，方案其余部分不受影响。 |
| **`OpResult` 过度设计** | 六个字段里，第一期真正会用到的是 `image`/`scalars`/`artifacts`。但字段现在不留，P2 接入 OCR 和模板匹配时就要改所有算子的签名。这是我认为值得的前置成本。 |
| **算子 id 契约** | 第一期就要定好命名规则（小写下划线、动词或名词短语），否则后面改名要动历史数据。 |
| **opencv 已在依赖里** | `tool_catalog.py` 已经在用 `cv2`，不引入新依赖。 |
| **纯函数约束会不会太严** | 会挡住一类需求：需要跨调用累积状态的算子（比如"连续 N 帧稳定性"）。这类应该显式接收一个帧序列作为输入，而不是靠内部状态。`diff_ratio` 就是正例。 |

---

## 10. 污染声明

这份文档**不是在隔离条件下写的**。在动笔前，我为了盘点"现有图像函数"读到了仓库里一版未跟踪的实现，具体看到：

- `backend/src/pixelforge/vision/tool_catalog.py` **全文**：`TOOLS` 元组的 12 条目、`image_tools` 表结构、6 个算子实现、`demo_image()`
- `docs/README.md` 索引，它透露了另一份设计的存在、其目录结构（`docs/design/001-.../`、`TEMPLATE.md`）、名称（"图形化图片实验本"）和章节构成（整改、页面、数据库、接口、MHXY 复用、分期验收）

**没有打开**：`docs/IMAGE_TOOLS.md`、`docs/design/001-*` 下的任何文件、`docs/design/README.md`、`docs/design/TEMPLATE.md`、`api/image_tools.py` 正文。

据此判断，以下部分**可能受到影响**，对比时应打折：

- §2.4 的分类维度、`availability` 三态、`source` 来源标注——明确沿用自已看到的实现
- 「实验本 / 实验台」这个命名——我看到对方叫"图形化图片实验本"之后才定的，尽管概念是从你原话"图形化的 Jupyter"来的
- 文档编号 + 状态 + 目录式存放的规范形态——我看到了对方目录结构的形状（但没看内容）
- MHXY 作为能力来源——`tool_catalog.py` 里已有"MHXY 思路重构"的标注；不过你在我动笔时也直接让我去参考 MHXY，这条是独立的

以下部分**我认为是独立的**，也是本方案与已看到那版分歧最大的地方：`OpResult` 多态输出（D2）、声明式参数 schema（D3）、纯函数契约与副作用剥离（D4）、算子链即撤销栈（D5）、效果图 golden fixture + sha256 校验（D6）、`image_run` 执行历史表、把 Operator 列为第五个插件扩展点。
