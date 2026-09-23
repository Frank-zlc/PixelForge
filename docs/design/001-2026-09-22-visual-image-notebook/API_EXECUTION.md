---
design_id: PF-DES-001
title: 图片实验本接口与执行设计
doc_type: api-execution
status: in_progress
version: 1.0.0
created: 2026-09-22
updated: 2026-09-22
---

# 接口与执行设计

[返回总方案](README.md)。本页描述目标契约和当前分期实现；以 [交付记录](DELIVERY.md) 区分已完成与待开发。旧 `/api/image-tools` 仍提供单次 PNG 处理。

## 1. 路由与返回

新增 API 统一以 `/api/image-lab` 为前缀。列表支持分页、搜索和稳定排序。记录返回稳定 ID、状态、创建时间；图片输出经受控资产端点读取，严格校验 ID 与路径，不向前端暴露服务器路径。初期与现有 API 同源部署；若未来增加用户账户，再增加授权校验。

| 方法 | 路径 | 请求要点 | 响应/用途 |
| --- | --- | --- | --- |
| GET | `/tools`、`/tools/{id}` | 类别、关键词、版本 | 目录、schema、动态可用状态及原因、示例摘要 |
| GET | `/tools/{id}/examples` | 版本、分页 | 示例的输入/参数/结果引用 |
| POST | `/imports` | 当前采用单文件原始字节 + `relative_name`/`batch_id` 查询参数；目录由前端逐文件提交 | 返回单个素材及预览地址，前端汇总成功/失败；单图 20 MB/3000 万像素、单批最多 500 张/512 MB |
| GET | `/assets`、`/assets/{id}` | 批次、名称、来源、分页 | 素材元数据、缩略图、引用数 |
| GET | `/assets/{id}/content` | 可选预览尺寸 | 受控图片流；原图下载需明确选项 |
| POST | `/notebooks`、GET `/notebooks` | 标题、可选项目关联 | 创建/查找实验本 |
| GET | `/notebooks/{id}` | 无 | 草稿、revision、步骤及最近运行概览 |
| PUT | `/notebooks/{id}` | 完整草稿、`expected_revision` | 原子保存；校验工具版本、端口、参数与前序引用；返回新 revision |
| POST | `/notebooks/{id}/runs` | 运行模式、目标步骤、`expected_revision`、幂等键 | 202 + run ID、将执行的步骤清单；同一本同时只允许一个活动 run |
| GET | `/notebooks/{id}/runs`、`/runs/{run_id}` | 分页或无 | 历史快照、各步状态、结果引用 |
| POST | `/runs/{run_id}/cancel` | 无 | 202 + `cancelling`；进程退出后终态为 `cancelled` |
| GET | `/runs/{run_id}/events` | 事件游标 | SSE 或短轮询的状态更新；首版选一种并保留 GET 快照恢复 |
| POST | `/exports` | notebook ID、指定 run ID | 生成带清单和哈希的下载包；N4 |

N1 已实现 `/tools`、`/tools/{id}`、`/tools/{id}/examples`、`POST /tools/{id}/run`、`POST /imports`、`GET /assets`、`GET /assets/{id}` 和内容读取。快速预览接口返回 JSON，图片/掩码端口暂用 base64 data URL 传回浏览器，限制单输出 PNG 40 MB；它不生成 notebook run。N2 的多步运行改为托管产物引用。当前目录页使用旧单步 PNG 入口以保持兼容，并从新目录接口读取 schema 和可用状态。

N2 草稿 API 已先行实现 `POST/GET /notebooks`、`GET/PUT /notebooks/{id}`：保存时校验步骤顺序、具名端口类型、素材存在性、参数和 revision。它只保存草稿；`/runs` 路由和实验本页面尚未实现。

设备快照入口在 N4 接入：用户从设备页点击“保存截图并在实验本打开”，设备端先产生无损 PNG，复制到素材库后返回 asset ID。实验运行使用冻结图片，不能隐式订阅实时流。N4 批量 API 复用已发布实验快照，另建批次任务与逐文件运行记录；在 N4 细化后补充字段，不挤入本期单图 run 表。

### 错误格式

统一返回 `code`、`message`、`details`、`request_id`。关键状态：400 请求格式错误；404 素材/实验/版本不存在；409 revision 冲突、活动运行冲突、删除被引用资源；413 文件或目录超过限额；422 参数、ROI、类型或引用不合法；424 依赖缺失（或约定同等业务错误码）；429 资源繁忙；500/503 后端或依赖异常。前端展示可操作原因，日志保存可定位请求 ID，不向页面暴露绝对路径或原始异常栈。

旧 `/api/image-tools/{id}/run` 在 N1—N3 期间保持可用并增加迁移验证；新实验统一调用新 API，不把新状态塞进旧的 PNG 响应。移除或改变旧路由须另行记录兼容期限。

## 2. 一次运行的生命周期

~~~mermaid
sequenceDiagram
  actor User as 用户
  participant UI as 实验本页面
  participant API as API/SQLite
  participant Worker as 受控计算进程
  participant Files as 托管文件
  User->>UI: 保存草稿并选择运行范围
  UI->>API: PUT 草稿(expected_revision)
  API-->>UI: 新 revision
  UI->>API: POST runs(范围, revision, 幂等键)
  API->>API: 校验与固定运行快照
  API-->>UI: 202 + run_id
  loop 对已解析的步骤依次执行
    API->>Worker: 输入文件/端口/参数/超时
    Worker-->>API: 具名结果/错误
    API->>Files: 临时写入、校验、原子改名
    API->>API: 提交 step_run 与输出引用
  end
  UI->>API: GET run 或事件流
  API-->>UI: 状态、结果与错误
~~~

运行请求先校验草稿 revision，并在一个事务中生成不可变 notebook/step 快照、锁定工具版本与素材哈希、检查单本活动运行，再返回 202。浏览器断线不撤销已提交运行。重复提交同一幂等键和等价请求返回同一 run；相同键配不同请求报冲突。恢复页面根据 run ID 查询权威状态。

### 运行模式和依赖

| 模式 | 范围 | 缓存与前置步骤 |
| --- | --- | --- |
| 当前步骤 | 目标单元 | 先计算其传递依赖；有可复用的同快照结果则复用，提交前显示实际清单 |
| 运行到此 | 从首个必要步骤到目标单元 | 无关步骤跳过；相同输入/版本/参数/ROI 的结果可复用 |
| 运行全部 | 当前草稿的所有工具单元 | 顺序执行；无变化的有效结果可复用 |
| 从此重跑 | 指定单元及其传递依赖的后继 | 目标步骤强制重新计算，受影响后继随之运行；不关联的步骤保留 |

缓存键由工具版本及代码哈希、展开后的参数、ROI、输入内容哈希、输入输出端口类型、规范化策略组成。只在实现声明为确定性且依赖/环境兼容时复用；OCR 包版本、语言包、预处理改变时不复用。缓存命中在 `step_runs` 记录来源 run/step 和输出哈希，仍产生当前 run 的步骤记录。旧结果可保留，但不能误当当前草稿结果。运行前的页面清单应区分“会计算”和“会复用”。

步骤失败后，依赖它的步骤标 `blocked` 并记录原因；不依赖的步骤是否继续执行由首版明确为**继续执行**，便于查看其他分支的结果。整次 run 若有失败步骤则为 `failed`，并保留已成功输出。OCR 空文本、模板未命中、差异值为 0 都是成功结果。

## 3. 状态、取消、超时和恢复

| 实体 | 状态 | 说明 |
| --- | --- | --- |
| run | queued、running、cancelling、succeeded、failed、cancelled、interrupted | 单调向终态前进；终态不可改写 |
| step | queued、running、succeeded、failed、cancelled、blocked、skipped | `skipped` 仅用于未选范围或无需执行且已有明确缓存记录时；缓存命中应显式标注来源 |

图片计算首版使用受控子进程，进程间只传资产路径/参数和受控输出位置，不接收用户代码或任意函数名。默认每次一个计算步骤，单本一个活动 run，全局并发上限在 N1 压测后配置。进程设置执行超时、内存/像素/输出大小上限；超限记录可解释失败。OpenCV 在普通线程中无法可靠强制停止，因此取消需停止派发后继步骤、终止当前受控进程并确认退出；OCR 若启动 Tesseract 子进程，也须终止和等待子进程。UI 在确认前显示 `cancelling`，不能提前展示为已取消。

服务重启时将遗留 queued/running/cancelling 标记 `interrupted`，不自动重跑；用户可从历史页重试。临时文件清理与孤儿资产检测在启动时进行，不修改已完成历史。受控进程的崩溃、超时和系统资源不足分别记录错误码。

目录导入与批量运行设置逐文件、总文件数、总字节数、像素、解码时间、输出大小和磁盘余量上限；N1 先维持当前单张 20 MB、解码后 3000 万像素的限制，再基于实测确定目录和输出限额。路径规范化拒绝 `..`、绝对路径、符号链接逃逸；仅图片解码成功后进入素材库。压缩包导入和服务端任意路径浏览不在首版范围。

## 4. N1 需要验证的技术点

1. 用两个同名文件和一个带 EXIF 方向的图片验证导入与坐标一致。
2. 验证 SQLite 迁移/备份、WAL 情况及中途失败回滚，旧功能目录保持可用。
3. N2 引入受控进程时，用真实尺寸图片验证启动开销、内存和取消；据实测选择默认限额。
4. N2/N3 验证 OpenCV、OCR 二级子进程退出与服务重启后的 `interrupted` 恢复。
5. 验证旧单步 API 和新工具注册表对同一参数的结果一致，避免两套实现分叉。

N1 已有针对性测试覆盖旧接口、目录状态、托管导入、同名文件、EXIF 方向、透明图白底合成和掩码端口。完整执行记录见 [DELIVERY.md](DELIVERY.md)。受控进程与实验运行仍待 N2。
