---
design_id: PF-DES-001
title: 图片实验本数据模型与工具契约
doc_type: data-model
status: in_progress
version: 1.0.0
created: 2026-09-22
updated: 2026-09-22
---

# 数据模型与工具契约

[返回总方案](README.md)。这是实施中的逻辑模型和字段约束；具体完成情况以 [阶段记录](DELIVERY.md) 为准。现有 `image_tools` 表继续可读。

## 1. 数据边界

- **元数据权威来源**：运行数据目录的 `image_tools.sqlite3`。工具目录、版本、素材、实验本、运行记录都由该库管理。业务图片和大结果以文件存储，库中只保存受控相对路径、哈希和尺寸。
- **设备项目保持现状**：原有 `ProjectStore` 的 `project.json` 和模板 PNG 不搬入新库。实验本的 `project_id` 为可空逻辑关联，不设跨存储外键；删除项目时需先解除或处理关联，不级联删除实验本与产物。
- **托管素材是快照**：浏览器选择文件/目录后，用户明确导入，服务端复制图片并记录导入批次、相对路径与 SHA-256。同名文件可以并存。原图字节保留；预览用经过方向和色彩规范化的衍生图。
- **运行数据不提交仓库**：运行数据根由 `PIXELFORGE_DATA_DIR` 决定。建议 `image_lab/assets/<id前两位>/<id>/<文件>` 与 `image_lab/outputs/<run_id>/<step_run_id>/<port>.<ext>`；路径永远从可信 ID 构造，客户端不得提交服务器绝对路径。
- **唯一状态源**：实验本和步骤仅在 SQLite 保存；导出包是显式生成的可移植快照，不是第二份自动双写数据。

## 2. 表结构建议

主键均用稳定的 UUID/ULID 字符串。时间均存 UTC ISO 8601；前端再按本地时区显示。`*_json` 字段保存经 schema 验证的 JSON，不存 Python 对象表示。外键启用 `PRAGMA foreign_keys=ON`，常用查询建索引。DDL 在 N1 迁移时按此契约落地并以迁移脚本为准。

| 表 | 关键字段 | 用途与约束 |
| --- | --- | --- |
| `schema_migrations` | `version` PK、`applied_at`、`checksum` | 显式、幂等的版本迁移记录；禁止靠启动时建表语句猜测版本 |
| `image_tools`（现有表扩展） | `id` PK、`category`、`name`、`description`、`function_name`、`source`、`sort_order`、`catalog_state`、`current_version`、`created_at`、`updated_at` | 目录实体。描述字段可管理；当前 `effect_image` 迁到示例表后仅作兼容字段；现有 `availability` 继续兼容旧接口直到前端迁完 |
| `tool_versions` | `id` PK、`tool_id` FK、`version`、`implementation_key`、`params_schema_json`、`inputs_schema_json`、`outputs_schema_json`、`code_hash`、`dependency_json`、`published_at`、`state` | `(tool_id,version)` 唯一。发布版不可改写，运行和示例均固定引用；实现用注册表白名单查找 `implementation_key`，不执行目录中的函数名 |
| `tool_examples` | `id` PK、`tool_version_id` FK、`title`、`description`、`input_refs_json`、`params_json`、`roi_json`、`result_refs_json`、`sort_order`、`verified_at` | 一个版本可有多组前后对照、mask、OCR 文本及失败边界。示例应由同一版本真实生成并保存输入哈希；版本变化需重新验证 |
| `image_assets` | `id` PK、`kind`、`origin`、`import_batch_id`、`relative_name`、`storage_path`、`sha256`、`mime_type`、`byte_size`、`width`、`height`、`channels`、`orientation`、`created_at`、`deleted_at` | `kind` 为 original/preview/output/example 等；`origin` 为 local_import/device_capture/derived。`storage_path` 位于受控根下。软删除前核查引用，历史使用的文件不能物理删除 |
| `notebooks` | `id` PK、`project_id` NULL、`title`、`description`、`revision`、`status`、`created_at`、`updated_at` | 草稿实体；`revision` 乐观锁，修改请求须携带基线版本，冲突返回 409 |
| `notebook_steps` | `id` PK、`notebook_id` FK、`position`、`kind`、`title`、`tool_version_id` NULL、`inputs_json`、`params_json`、`roi_json`、`created_at`、`updated_at` | `kind` 为 tool/note。`(notebook_id,position)` 唯一；输入仅可指素材或前序步骤的具名输出。删除/重排先验引用；note 无工具版本 |
| `notebook_runs` | `id` PK、`notebook_id`、`notebook_revision`、`snapshot_json`、`snapshot_hash`、`status`、`started_at`、`finished_at`、`error_json`、`idempotency_key` | 不可变运行快照。`snapshot_json` 固定步骤顺序、工具版本、输入 ID/哈希、参数、ROI、环境摘要；同一 notebook 的活动 run 至多一个；`idempotency_key` 防重复提交 |
| `step_runs` | `id` PK、`run_id` FK、`step_id_snapshot`、`position_snapshot`、`tool_version_id`、`resolved_inputs_json`、`outputs_json`、`status`、`started_at`、`finished_at`、`duration_ms`、`error_json` | 不依赖可变 `notebook_steps` 的级联生命周期。`resolved_inputs_json` 明确输入的素材/上游 `step_run_id`+输出端口+哈希；`outputs_json` 指向文件资产和结构化 JSON |

建议索引：`image_tools(category,sort_order)`、`tool_examples(tool_version_id,sort_order)`、`image_assets(import_batch_id,relative_name)`、`notebook_steps(notebook_id,position)`、`notebook_runs(notebook_id,started_at DESC)`、`step_runs(run_id,position_snapshot)`；活动运行用条件唯一索引或受事务保护的活动标志实现。原始文件内容可以按哈希去重，但不同导入记录与来源路径仍保留独立 ID。不要以文件名作唯一键。

### 对用户最关心字段的落点

| 用户字段 | 保存位置 | 页面展示 |
| --- | --- | --- |
| id、类别、功能、工具函数名、功能介绍 | `image_tools` | 目录卡片和工具详情 |
| 效果图 | `tool_examples.result_refs_json` → `image_assets` | 前后对比，可有多例和多种结果 |
| 参数说明、默认值、支持输入输出 | `tool_versions` 的各 schema | 自动表单及工具详情 |
| 此功能能否使用 | 注册实现 + 适配器 + 依赖探测的实时状态 | 可运行/待接入/缺依赖/规划和原因 |
| 实际做过什么、得到什么 | `notebook_runs`、`step_runs`、输出资产 | 实验历史与运行详情 |

`function_name` 从注册函数的限定名派生，仅供人识别和检索。执行权由服务端注册表和已发布工具版本决定。现有启动时 UPSERT 会覆盖内置记录的分类、描述、效果图等字段；N1 改为显式迁移和受控同步。内置工具代码是实现、schema 与默认介绍的真相来源；用户自定义文案若以后开放，放独立覆盖字段，不能混入代码投影。历史发布版本保持不可变。

目录的 `availability` 不能手填“ready”：只在注册函数、输入输出适配器、依赖探测、可运行 demo 和非空验收记录全部通过时计入可运行算法数。尚未满足条件的条目保留为 `pending_adapter`、`needs_dep` 或 `planned` 并给出原因。工作流动作和算法分开统计。统计使用次数、耗时及失败率从 `step_runs` 按工具版本聚合，旧单步接口的调用在迁移期可另记兼容事件，不能伪装成完整实验运行。

## 3. 工具输入输出契约

工具实现建议位于 `backend/src/pixelforge/vision/`，统一适配器与版本注册建议位于新 `backend/src/pixelforge/image_lab/`；具体文件命名可在 N1 代码评审时确定。注册表是可运行能力的唯一清单，不从数据库字符串 import 任意函数。`implementation_key` 由代码注册，发布版本锁定参数与端口定义；缺少该版本的代码时历史仍可读，重跑需显式升级。

| 类型 | 约束 | 典型来源/输出 |
| --- | --- | --- |
| `IMAGE_RGB8` | H×W×3，uint8，RGB | 原图、裁剪、彩色筛选结果 |
| `IMAGE_RGBA8` | H×W×4，uint8；使用前需明确 alpha 策略 | 带透明通道导入图 |
| `IMAGE_GRAY8` | H×W，uint8 | 灰度图、OCR 预处理 |
| `MASK8` | H×W，uint8，值域 0/255 | 颜色提取、形态学结果 |
| `TEXT` | UTF-8 文本和语言信息 | OCR 完整原文 |
| `REGIONS` | 框、多边形、标签、分数、坐标空间 | OCR 框、模板匹配框 |
| `METRICS` | 有名字和单位的数值 | 差异比例、匹配分数、耗时 |
| `TABLE` | schema 明确的多行结果 | OCR 逐词、逐尺度分数 |

图像端口可多选，比如颜色提取返回 `image: IMAGE_RGB8` 和 `mask: MASK8`；OCR 返回 `text: TEXT`、`words: TABLE`、`regions: REGIONS`，标注图由 UI 用原图加覆盖层生成，不原地修改输入。结果还可带 `points`（重心/关键点）、`metrics`（命名数值与单位）、`artifacts`（中间图和诊断图）和 `notes`（解释）。这些字段以具名端口映射保存，不能把图片、文本和布尔硬塞进同一个 `np.ndarray`。工具的“没有找到目标”返回合法空结果和状态说明，不当作算法异常。

参数 schema 采用 JSON Schema 的受控子集：数字上下限、步长、枚举、布尔、颜色、ROI、文件输入和 `when` 条件显示。`when` 只影响控件可见性，不跳过服务端参数校验；例如模板复核的“颜色模式”显示色彩选择，“轮廓模式”显示 Canny 阈值。服务端验证是权威，前端据同一 schema 生成表单和文案；每个发布版本声明默认参数，运行快照保存展开后的实际参数。函数不得读隐式全局设备、不得修改输入数组、不得自写文件。具名输入示例：模板匹配 `source: IMAGE_RGB8` + `template: IMAGE_RGB8` + 可选 `mask: MASK8`；差异计算 `before` + `after`。

确定性纯图像算子的注册表测试遍历每个可运行实现：检查输入不变、相同输入和参数产生相同输出、无隐式文件写入。OCR 等依赖外部版本的算子另锁定引擎/语言包版本并用固定样图验收。demo 哈希变化只提示回归差异；发布“可运行”还需有已记录的正负样图验收证据。

颜色处理契约固定入口 `IMAGE_RGB8`。调用 `cv2.COLOR_RGB2HSV` 前需断言数组确为 RGB；从 OpenCV BGR 解码的图片先转换成 RGB。MHXY 的 BGR 输入配 `BGR2HSV`，在通道表示和转换码都正确时，与 RGB 配 `RGB2HSV` 的物理颜色含义相同。迁移阈值需保存旧参数、旧通道表示和转换码，并用已知红/黄/蓝色块和实际截图对照，不机械替换数值。

实验本与设备项目仅作可空关联。N4 导出 Step 时建立显式映射表：实验输入端口对应设备截图/模板来源，图像处理步骤对应脚本中的可执行定位或断言能力，无法映射的工具给出错误和原因；Step 导出记录所用实验 revision、工具版本和输入契约。不能把整本实验序列直接序列化成现有 Step。

### 坐标与预览规则

ROI 是输入图片的像素整数 `(x,y,width,height)`，左上包含，右下不包含，必须在图片范围内且面积大于 0。EXIF 方向在预览和处理前统一归一化，坐标依标准化图。裁剪输出记录到父图的平移变换；连续裁剪组合变换，OCR/匹配框存“所属输出图坐标”并提供可计算的原图坐标，避免重复加偏移。缩放展示不改变持久坐标。透明图必须指定背景合成或保留 alpha，不能默默丢弃。

## 4. 版本、失效和历史

工具版本发布后参数 schema、输入输出端口、实现哈希不可原地更改；修正算法发布新版本。运行锁定版本和素材哈希，历史可查看。历史结果相对**当前草稿**可能过期，但历史自身仍是一次成功运行。

改变工具、版本、输入引用、参数或 ROI 会使该步骤及所有传递依赖步骤的当前预览标为过期；只改标题或折叠状态不会。修改后旧结果仍可看，不能显示为“当前参数结果”。运行期间编辑产生新 revision，本次运行继续使用已锁定快照，结束结果不覆盖新草稿状态。

### 输入引用示例

~~~json
{
  "source": {"kind": "asset", "asset_id": "asset-A"},
  "mask": {"kind": "step_output", "step_id": "step-2", "port": "mask"}
}
~~~

保存时校验引用指向同一本实验的前序步骤、端口存在且类型兼容。执行时快照将 `step_id` 解析为本次或明确选定缓存的 `step_run_id`、端口、资产 ID 和哈希；禁止从“这个步骤最近一次输出”模糊读取。顺序模型不允许前向引用与环。

## 5. 迁移、备份和删除

N1 前先用 SQLite backup API 建立包含 WAL 的一致性备份，并检查空间与版本。迁移脚本逐版本执行、记录 `schema_migrations`，成功后才切换新接口；失败时不运行半成品结构。旧 `/api/image-tools` 在迁移期可读可试用，新旧目录共用同一个注册表与版本定义，待前端替换后再按兼容计划移除。旧表内 12 个稳定 ID 保留；原有 `parameters_json` 转为版本默认参数，其余字段按数据来源分类迁移；无法确认的值不猜测。

素材、运行产物先写临时文件并校验哈希，再原子改名，然后提交引用记录；异常时清理孤立临时文件，启动时检查遗留文件和未完成运行。删除以软删除和引用检测为默认；实验草稿、历史运行、示例或设备模板仍引用时阻止物理删除，并展示引用处。清理策略须有保留期限、容量上限及可预览的删除清单，另行评审后启用。

数据库和资产目录需一起备份与恢复，导出包使用清单及各文件 SHA-256 验证完整性。首版暂不提供自动同步或跨机器路径引用。
