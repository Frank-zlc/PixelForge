# 设计文档规范与索引

本目录用于长期维护 PixelForge 的产品设计、整改方案、数据模型与开发验收记录。文档与代码一起版本管理。

## 1. 固定目录

~~~text
docs/
  README.md                          文档入口
  IMAGE_TOOLS.md                      已实现功能的使用说明
  design/
    README.md                        本规范与方案索引
    TEMPLATE.md                      新方案模板
    001-2026-09-22-visual-image-notebook/
      README.md                      总方案、范围、现状、评审记录
      UI.md                          页面与交互
      DATA_MODEL.md                  数据字典、工具契约、保存规则
      API_EXECUTION.md                接口、执行与异常语义
      DELIVERY.md                     整改任务、阶段、验收证据
      MHXY_REUSE.md                   来源分析与移植清单
      assets/                        需要时创建，保存文档配图
~~~

目录名固定为 `NNN-YYYY-MM-DD-english-topic`，如上例；编号递增，日期为创建日期。正式创建后不因修改日期而重命名目录。方案 ID 为 `PF-DES-NNN`。小方案可只有 README.md；子文档按需要拆分。

## 2. 保存格式

- Markdown，UTF-8，LF 换行；正文用中文，标识符与代码用英文。
- 一级标题说明文档用途，列表和表格前留空行；代码块注明语言。
- 仓库内使用相对链接，保证换电脑、切分支后仍可阅读。外部项目注明仓库名、提交、相对源码位置，不把本机路径当作长期依赖。
- 配图放当前方案的 assets/，采用 `page-name-v01.png` 等可识别名称，并写明是设计稿还是实际截图。
- 设计目录不保存生产数据库、用户原图、设备截图或大批测试结果；验收记录引用提交、测试用例、受控样例和运行报告。
- 每份正式方案及子文档包含下方 YAML 元数据；规范、索引和模板本身除外。

~~~yaml
design_id: PF-DES-001
title: 图形化图片实验本
doc_type: proposal
status: pending_review
version: 0.1.0
created: 2026-09-22
updated: 2026-09-22
~~~

`doc_type` 可用 proposal、ui、data-model、api-execution、delivery、reuse-analysis。日期采用 YYYY-MM-DD；版本使用 major.minor.patch。评审前为 0.x，通过评审形成 1.0.0；调整范围或行为提升 minor，文字勘误提升 patch。破坏已确认契约的改动提升 major 并重新评审。

## 3. 状态与更新责任

| 状态 | 含义 | 进入条件 |
| --- | --- | --- |
| draft | 草稿 | 仍有待补齐的关键设计 |
| pending_review | 待评审 | 方案已形成，等待用户意见 |
| approved | 已确认 | 在主文档记录用户确认的日期、范围、版本 |
| in_progress | 开发中 | 已确认范围中的任务开始实现 |
| implemented | 已交付 | 对应验收项有实际证据，使用说明已更新 |
| superseded | 已替代 | 保留文档，链接替代方案及原因 |

由执行该改动的开发者同步更新对应文档。主文档维护总体状态，各子文档可独立标记状态；部分完成时主文档保持 in_progress，在 DELIVERY.md 标明已完成范围。不能因为某段代码存在就把整份方案标为 implemented。

每次开发提交需检查：

- [ ] 实际功能是否符合已确认范围；新增范围是否已写入方案并确认。
- [ ] 数据结构、API、工具输入输出、菜单或依赖改变时，是否同步对应文档。
- [ ] DELIVERY.md 是否更新任务状态、提交和实际验收结果；未执行项明确写“未执行”。
- [ ] 已实现能力是否同步到使用说明和功能目录；规划能力是否仍标记规划中。
- [ ] 破坏性变更是否记录迁移、备份、回退及兼容期限。

运行结果的历史记录属于产品数据；这里的验收记录属于开发证据，两者分别维护。不要把测试计划写成已通过结果。链接与 Markdown 格式应在提交前检查。

## 4. 方案索引

| ID | 方案 | 状态 | 版本 | 创建日期 |
| --- | --- | --- | --- | --- |
| PF-DES-001 | [图形化图片实验本](001-2026-09-22-visual-image-notebook/README.md) | in_progress | 1.0.0 | 2026-09-22 |

`A1-image-capability-library.md` 和 `A2-comparison-and-merge.md` 是方案形成过程中的备选分析记录，采用早期保存格式。它们不作为实施依据；后续设计与维护以 PF-DES-001 及其子文档为准。新设计文件统一按本规范创建。
