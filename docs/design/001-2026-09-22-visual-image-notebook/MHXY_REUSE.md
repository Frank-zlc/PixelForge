---
design_id: PF-DES-001
title: MHXY 图像能力复用评估
doc_type: reuse-analysis
status: pending_review
version: 0.1.0
created: 2026-09-22
updated: 2026-09-22
---

# MHXY 图像能力复用评估

本文是图形化图片实验本设计的研究附件，整体方案待用户评审，本文涉及的新开发尚未开始。记录当前代码事实、值得借鉴的算法和后续验收要求；已有的单步工具原型属于基线能力。

检查日期为 2026-09-22。MHXY 仅做本地 Git 引用和源码只读检查，未联网刷新远端、未切换分支、未执行其脚本。外部仓库位置为 `/Users/zhangliangchen/Desktop/LSKJ/MHXY`，本文列出的 MHXY 源码不随 PixelForge 存档，复核时应使用下列提交定位。

## 1. 分支基线与选择

| 本地可见引用 | 提交 | 提交日期 | 与 `dev1` 的关系 | 复用用途 |
| --- | --- | --- | --- | --- |
| `dev1`、`origin/dev1` | `7801b4f` | 2021-12-21 | 当前检出分支，两引用一致 | 主要分析基线 |
| `origin/master` | `78d39d2` | 2019-01-02 | 初始提交，是 `dev1` 的祖先；`dev1` 多 37 个提交 | 不能作为功能完整的开发基线 |
| `origin/windows_1` | `8ca8489` | 2019-03-19 | 全部历史已进入 `dev1`；`dev1` 多 8 个提交 | 需要追溯历史实现时参考 |
| `origin/test` | `621de2e` | 2021-12-18 | 与 `dev1` 分叉；双方独有提交分别为 8、37 个 | 补查独有工具，不能当作已合并能力 |

本地只有 `dev1` 这个普通分支，其他三个名称以远端跟踪引用存在。本地记录的 `origin/HEAD` 指向 `origin/dev1`；这是本地保存的默认分支信息，本次没有重新确认服务器上的当前默认分支。

`dev1` 与 `test` 的共同祖先为 `78d39d229a16524db9f61533123e69b948f1ce5a`。按本地引用的提交日期，`dev1` 最新；按可达历史，`dev1` 包含 `windows_1`，但不包含 `test` 的 8 个独有提交。因此没有证据支持“一个分支包含所有独有实现”。采用 `dev1` 主体加 `test` 定向评估，不整体合并 MHXY 仓库。

可重复执行的只读核查命令：

```sh
git -C /Users/zhangliangchen/Desktop/LSKJ/MHXY branch -avv
git -C /Users/zhangliangchen/Desktop/LSKJ/MHXY rev-list --left-right --count dev1...origin/test
git -C /Users/zhangliangchen/Desktop/LSKJ/MHXY rev-list --left-right --count dev1...origin/windows_1
git -C /Users/zhangliangchen/Desktop/LSKJ/MHXY rev-list --left-right --count dev1...origin/master
git -C /Users/zhangliangchen/Desktop/LSKJ/MHXY show origin/test:utilities/image_handle.py
```

## 2. 值得借鉴的能力

下列 MHXY 路径均相对于上述外部仓库，除注明 `test` 外均按 `dev1@7801b4f` 检查。优先级表示实验本基础建成后的投入顺序，不表示已获准开发。

| 优先级 | 能力 | MHXY 文件与函数 | PixelForge 当前能力与拟改造方式 |
| --- | --- | --- | --- |
| 高 | HSV 颜色筛选及掩码 | `mhxy/tools/images_tool.py`：`ExtractFrameData.get_color_limit_from_hsv`；`mhxy/tools/dynamic_capture.py`：`ShiftInfo.img_coloer_optimization` | 已有 `color_mask` 单步工具。扩展任意 HSV 范围、取色与多范围并集；分别输出真正的二值 mask 和筛选后的彩色图，不能混用 |
| 高 | OCR 双阈值图像合成 | `mhxy/tools/ocr_tool.py`：`MyOCR.ocr_synthesis_words_img` | 已有 `text_enhance` 单步原型；当前亮度阈值 180/150、权重 0.7/0.3 固定。拟将阈值、权重和输入通道显式参数化，展示各中间图与 OCR 对照 |
| 高 | 颜色、形态学与轮廓组合定位文字区域 | `mhxy/tools/dynamic_capture.py`：`ShiftInfo.get_words_xy` | 当前 `text_regions` 仅规划。拆成可复用形态学、轮廓候选框及组合预设；输出 mask、候选框和过滤原因，再交给现有 OCR |
| 中 | 模板定位后的颜色/边缘复核 | `mhxy/tools/images_tool.py`：`ExtractFrameData.template_similarity_check`；`dynamic_capture.py`：`ShiftInfo.match_template_words_color`；`test@621de2e` 的 `utilities/image_handle.py`：`ImageHandle.similarity_check` | 当前 `match_verify` 仅规划。在既有多尺度匹配后增加可选复核，保存初筛分数、颜色/边缘分数、阈值和失败原因；先验证是否确实减少误报 |
| 中 | 按提示位置逐步扩大搜索区域 | `mhxy/tools/dynamic_capture.py`：`ShiftInfo.accurate_recognition` | 既有匹配支持 ROI；可增加搜索策略预设，参数为中心点、初始半径、增量、次数。展示每轮 ROI、分数与最终坐标，限制运行次数 |
| 低 | 通道差分辅助彩色关键词提取 | `mhxy/tools/static_capture.py`：`FixedInfo.get_task_info` 中的红通道减蓝通道 | 可提取“通道选择/通道差分 → 阈值 → OCR”通用流程；城市、NPC、任务次数及正则解析保留为外部业务逻辑，不进入图片核心库 |
| 低 | OCR 文字相似性辅助判断 | `mhxy/tools/ocr_tool.py`：`MyOCR.ocr_data_similarity` | 其字符计数交集忽略顺序，不能直接代表文字正确率。仅作为可解释辅助指标候选，须与逐字差错和有序文本比较一起评估 |

首批新增算法建议聚焦可调整 HSV、真正 mask、形态学和文字候选区域。模板二次校验和附近查找在多输入工具、结果类型及坐标映射完成后接入。每项独立登记工具 ID、版本、参数和样例，不以一个不透明的“大工具”隐藏整条流程。

## 3. 优先复用 PixelForge 的现有实现

| 现有实现 | 已有内容 | 设计中的复用方式 |
| --- | --- | --- |
| [工具目录与处理函数](../../../backend/src/pixelforge/vision/tool_catalog.py) | 6 个可试用工具：裁剪、灰度、颜色提取、边缘、二值化、彩色文字增强；数据库保存元信息、注册表限制可执行函数 | 保留稳定工具 ID；将元信息、参数 schema、算法实现和运行适配分层，已有工具逐项接入实验单元 |
| [模板匹配](../../../backend/src/pixelforge/vision/matching.py) | `match_template` 支持 ROI、多尺度、mask、分数、指标名称及逐尺度分数；`diff_ratio` 比较两张图片 | 增加离线输入选择与结构化结果展示，不重复移植 MHXY 的基础 `matchTemplate` 包装 |
| [OCR](../../../backend/src/pixelforge/vision/ocr.py) | `TesseractOcr.recognize` 接受图像，返回文字、词框、置信度、语言；独立进程与超时 | 增加离线调用适配、依赖/语言包诊断与结果叠加；继续复用现有识别器 |
| [离线 API](../../../backend/src/pixelforge/api/image_tools.py) | 当前上传图片与 ROI/参数，返回 PNG；限制 20 MB 和 3000 万像素 | 在新执行模型中统一输入解码、校验、输出类型和运行记录；兼容现有入口的迁移应单独验证 |
| [单步工具页](../../../frontend/tools.html)及[脚本](../../../frontend/tools.js) | 浏览器选择图片或目录、ROI、单次处理、PNG 预览/下载 | 作为 UI/算法原型基线；素材持久化、实验串联、历史、批处理均是后续待开发内容 |

目前目录状态为 6 项 `available`、3 项 `device_only`、3 项 `planned`。其中模板匹配、OCR、差异的 `device_only` 表示当前集成入口；底层算法本身已可接受图片。不能据此判断这些算法必须连接设备才能实现。

当前 `color_mask` 的处理结果是黑底彩色图，尚未将内部单通道 mask 作为独立输出。目录导入使用 `webkitdirectory` 获得 `File` 列表，未持久保存素材或目录访问句柄。上述事实与[现有工具说明](../../IMAGE_TOOLS.md)一起作为整改前基线，不能计作本方案的新交付。

## 4. 历史实现中需要修正的问题

以下是静态检查发现的问题，不代表本次已执行旧代码或完成兼容性测试。

| 来源 | 发现 | 重构要求 |
| --- | --- | --- |
| `ExtractFrameData.get_current_image`、`FixedInfo` 等 | 截图、旋转、ADB 命令、固定路径和游戏坐标与图像算法耦合 | 算法接收图像与参数；设备截图作为独立输入适配，运行过程不隐式操作设备 |
| `ShiftInfo.get_words_xy` | 参数 `words_color` 实际调用处写死 `'Y'`；形态学核与高度门槛固定 | 让实际参数生效，将核尺寸、迭代数、面积/高度/长宽比阈值声明并保存；提供多分辨率样图 |
| `ShiftInfo.get_words_xy` | 使用 `findContours` 三返回值解包和 `numpy.int` 历史写法 | 按本项目依赖版本重写并测试；不直接粘贴旧工具类 |
| `template_similarity_check`、`ImageHandle.similarity_check` | 使用交集像素之和除以模板像素之和；未处理模板无前景，指标不惩罚额外噪声 | 对空 mask/边缘返回可解释状态；明确指标定义，按样图比较覆盖率、IoU 等方案；不把不同指标分数混作同一置信度 |
| `test` 的 `ImageHandle.template_match` | ROI 切片写为 `img_cv[ay1:ay2, ax1, ax2]`；加回全图偏移后又用全图坐标截已裁 ROI | 修正切片与坐标空间，直接复用 PixelForge 匹配和坐标契约；覆盖非零 ROI 的回归样例 |
| `test` 的 `ImageHandle.similarity_check` | “原图检测”“特征检测”等分支仍为 `pass`，函数内含 `fixme` | 作为候选思路记录，不能登记为可用功能 |
| `get_match_template_info`、`ImageHandle.template_match` | 在输入图上直接画框，并写固定调试文件 | 原图不可变；标注为独立覆盖层，输出与调试图由运行记录统一管理 |
| 多个 MHXY 图像函数 | OpenCV BGR、PIL RGB、灰度/HSV 通道含义依赖调用上下文 | 输入统一规范与颜色空间声明；灰度、彩色图和 mask 分别校验，避免颜色错位 |
| `MyOCR.ocr_mask_img2data`、`ocr_data_similarity` | 固定中文识别并删除空格换行；字符计数相似度不包含词序 | 保留原始识别文本、框与置信度，清洗与相似性作为可选后处理，不破坏原始结果 |

基础 ADB 包装、固定地图/任务解析、导航点击、Redis/MySQL/代理工具与本次图片 IDE 目标没有直接对应，不列入移植清单。

## 5. 样图评估与验收方式

### 5.1 固定样图集

后续开发时建立可回归的样图集与清单。每项记录素材 ID/内容 hash、来源提交、原始尺寸、预期输出、人工标注、参数、工具版本和运行环境。原图、处理图、结构化结果、对照报告分别保存，避免只留一张“看起来有效”的效果图。

建议覆盖：

- 同一目标在不同亮度、色相、背景、抗锯齿与缩放下的正例；红色应覆盖 HSV 色相环两端。
- 与目标相似但不应命中的负例；无目标图、纯色图、无前景 mask、空文字结果。
- 靠边目标、非零 ROI、连续裁剪、缩放后识别，验证原图坐标回映射。
- 多行中文、单行中英数字、彩色细字与复杂背景；保留人工转写文本。
- 不同目录中的同名图片；重复导入、刷新与服务重启后可恢复同一实验输入。

MHXY 的 `test` 分支已有 `test/src.png`、`test/tem.png` 及 `templates/` 下历史图像，可先作为候选样本检查；本次未导入或验证这些图像，不假设它们足以证明通用效果。还需来自其他界面和真实使用场景的样图。

### 5.2 按能力验收

| 能力 | 检查方法 | 必须保留的证据 |
| --- | --- | --- |
| HSV 与 mask | 人工标注前景；检查颜色边界、红色色相回绕、mask 值域及尺寸 | 原图、参数、mask、筛选图、与标注比较的像素指标 |
| 双阈值增强 | 同图同识别器比较原图、现有原型、新参数预处理 | 三组图、完整 OCR 文本、字符错误率与耗时；包含变差样例 |
| 文字候选区域 | 标注文字框，比较召回和误检；检查形态学是否把相邻文字错误合并 | 候选框、过滤原因、标注匹配标准与逐图结果 |
| 模板二次校验 | 固定正/负例集，对比基础匹配和加复核两种流程 | 初筛/复核分数、各阈值、误报/漏报数量和耗时；若无改善，保持可选或暂不发布 |
| 附近查找 | 人工给定中心点、最大范围与步数，检查逐轮扩张 | 每轮 ROI/分数、停止原因、最终结果及全图坐标 |
| 实验复现 | 保存后刷新/重启再跑；修改上游参数再运行下游 | 输入 hash、工具版本、运行快照、历史结果和过期标识 |

评估阈值应在固定样图集和基线建立后写入对应工具的验收记录，并在同一批样图上比较。不能预先宣称所有图片 OCR 或匹配准确率提升。几何与数据约束可以直接作为硬性门槛：非法参数被拒绝、空前景不除零、输入不被修改、确定性变换能复现、坐标回映射正确、失败状态可解释。

## 6. 评审与后续更新

本评估建议采用“复用现有匹配/OCR → 建立实验与结果契约 → 扩展 MHXY 的预处理思路 → 用样图决定是否发布”的顺序。新工具只有在实现注册、输入输出校验、依赖检查、真实效果演示和验收记录齐备后，才进入可用数量统计。

用户确认整体方案后，开发时按实际提交更新各项状态与验证记录；若只是思路候选或环境依赖未就绪，继续明确展示原因。

| 日期 | 版本 | 变更 |
| --- | --- | --- |
| 2026-09-22 | 0.1.0 | 核对本地分支关系、外部函数与当前 PixelForge 能力；形成待评审复用评估，未开始新增功能开发 |
