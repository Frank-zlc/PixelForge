---
design_id: PF-DES-001
title: 图形化图片实验本开发交接
doc_type: handoff
status: in_progress
version: 1.1.0
updated: 2026-09-22
---

# 开发交接

建议给下一个开发者或模型的指令：

> 继续开发 `/Users/zhangliangchen/Desktop/LSKJ/PixelForge` 的图形化图片实验本。先读 `docs/design/001-2026-09-22-visual-image-notebook/README.md`、`DELIVERY.md`、`DATA_MODEL.md`、`API_EXECUTION.md`、`UI.md`，再用 `git status` 和代码核对实际状态——**不要直接相信本文件或 `DELIVERY.md` 里的测试数字和完成度描述，先跑一遍测试确认**。N1 已完成，N2 后端（草稿 + 执行引擎）已完成并通过测试；N2 前端、取消/超时的测试覆盖、重启恢复尚未做。每完成一项同步更新 `DELIVERY.md`，记录测试证据，区分已实现、已验证和待做项。

## 交接历史说明（2026-09-22 二次核实）

上一版交接（Kimi 会话）遗留的文档与实际代码状态不符，已在本次核实中发现并修正：

- **文档滞后于代码**：`runs.py`、`api/notebooks.py`、`image_lab/notebooks.py`、`test_notebook_runs.py` 的最后修改时间晚于上一版 `HANDOFF.md` 的记录时间，说明执行引擎（运行、快照、产物持久化、列表、详情、取消端点）在写交接文档前就已经写好，但没有被记录进 `HANDOFF.md`/`DELIVERY.md`——这两份文档把 N2 描述成"仅完成草稿 API"，与实际代码不符。
- **测试数字不准**：`DELIVERY.md` 声称"完整后端 423 项测试通过"，实测（含 `frontend/`）为 **412 passed, 14 skipped**，无失败；不含 `frontend/` 目录时会额外出现 1 项 `test_frontend_is_served` 失败，这是测试环境未挂载前端目录导致的假失败，不是代码回归。423 这个数字目前找不到对应的真实运行记录。
- **发现并修复了 2 个真实 bug**（详见下方"本次修复"），修复前完整测试为 3 项失败，不是 0 项。

## 当前核实状态

- N1：目录、工具详情、托管素材导入、6 个工具的类型化运行接口已实现。真实设备与 MHXY 正负样图未验收。
- N2 后端（已实现并测试通过）：
  - 草稿 CRUD：`POST/GET /api/image-lab/notebooks`、`GET/PUT /api/image-lab/notebooks/{id}`；保存校验素材/前序步骤的具名图像端口、参数、ROI、版本、revision 乐观锁。
  - 执行引擎：`POST /{id}/runs`（含 idempotency_key、单本单活动运行冲突检测 409）、`GET /{id}/runs`（历史列表）、`GET /{id}/runs/{run_id}`（含每步状态、产物 URL、SHA-256）、`POST /{id}/runs/{run_id}/cancel`。快照式执行、内容寻址产物存储（sha256）、失败步骤记录 `error_json` 而不中断整表。
  - 执行是**同步阻塞**的：`start_run` 用 `run_in_threadpool` 等待整个运行跑完才返回 202，不是真正的后台任务；`BackgroundTasks` 参数已声明但未使用，属于误导性遗留代码，可以清理但不影响正确性。
- N2 后端已知空白（不是"未实现"，是"实现了但没有验证/没有生效"）：
  - **取消功能没有测试覆盖**。`cancel()` 只在 `_execute_step` 每步开始时检查 `cancel_event`，理论上能在两个真正并发的线程间生效（Starlette 的 threadpool 是真线程），但目前没有任何测试证明它真的能取消一个正在跑的运行。
  - **超时形同虚设**。`runs.py` 里 `_RUN_TIMEOUT_S = 300.0` 只是声明，全代码库没有任何地方读取或使用它——不会真的在 300 秒后掐断运行。
  - **重启恢复未定义行为**。如果进程在某次运行 `status='running'` 时被杀掉，重启后这行数据会永久卡在 `running`，没有启动时扫描/修正的逻辑，也没有测试覆盖这种情况。
- N2 前端：完全未开始。实验本编辑页、运行触发/历史查看 UI、失效传播提示均不存在，只有 N1 遗留的 `tools.html`/`assets.html`/`tool.html`。
- N3—N5：尚未开始。分别是离线 OCR/匹配/差异、流程复用及批量/设备截图、MHXY 算法移植与真实样例验证。
- 当前工作区可能仍有既有未提交改动；编辑前用 `git status`/`git diff` 核对。

## 本次修复（2026-09-22）

诊断真实测试失败（而非直接改断言）后，修了 2 个生产代码 bug、1 个过时测试、补了 1 个测试的并发场景：

1. **端口类型校验漏洞**（`image_lab/notebooks.py`）：保存草稿时，MASK8 类型的输出端口（如 `color_mask` 的 `mask` 端口）能满足要求 `IMAGE_RGB8` 的输入端口，本该拒绝的错误接线被放行。修复：在 `operators.py` 新增 `IMAGE_INPUT_TYPE` 常量，`notebooks.py` 改为精确匹配该常量，消除两处校验集合语义漂移。
2. **草稿保存与运行时校验矛盾**（`image_lab/notebooks.py`）：旧代码要求保存草稿时素材必须已存在（否则 422），但运行引擎（`runs.py`）已经内建了"素材不存在则该步骤运行失败、其余步骤照常"的优雅降级逻辑，两者互相矛盾。按"类 Jupyter 草稿可以先引用还不存在的东西"的产品方向，改为保存时只校验 `asset_id` 的格式（32 位十六进制），存在性检查推迟到运行时。同步更新了 `test_notebook_drafts.py` 里过时的断言。
3. **并发冲突检测测试本身写错了**（`tests/test_notebook_runs.py`）：原测试用两次连续 `client.post()` 断言第二次返回 409，但 `start_run` 是同步阻塞的，第一次 `post` 返回时运行早已结束，第二次请求永远撞不上"运行中"状态，409 分支实际没被测到。改为用真线程 + `threading.Event` 强制制造真实并发（monkeypatch `_execute_step` 在真正进入执行后阻塞，等主线程确认运行确实处于 `running` 状态后再发第二个请求),现在能真正验证 409。

修复前完整测试（含 frontend）：3 failed。修复后：**412 passed, 14 skipped, 0 failed**。

## 关键位置与检查命令

| 内容 | 位置 |
| --- | --- |
| 内置算子及契约 | `backend/src/pixelforge/image_lab/operators.py` |
| 素材及迁移 | `backend/src/pixelforge/image_lab/storage.py` |
| 草稿与引用校验 | `backend/src/pixelforge/image_lab/notebooks.py` |
| 执行引擎（运行、快照、取消、产物） | `backend/src/pixelforge/image_lab/runs.py` |
| API | `backend/src/pixelforge/api/image_lab.py`、`backend/src/pixelforge/api/notebooks.py` |
| N1 页面 | `frontend/tools.html`、`frontend/assets.html`、`frontend/tool.html` |
| 草稿测试 | `backend/tests/test_notebook_drafts.py` |
| 执行测试 | `backend/tests/test_notebook_runs.py` |

在项目根目录运行 `backend/.venv/bin/pytest backend/tests -q --tb=short`（**必须在包含 `frontend/` 目录的完整工作区里跑**，否则 `test_frontend_is_served` 会假失败）。定向静态检查用 `backend/.venv/bin/ruff check` 与 `backend/.venv/bin/mypy`。全仓库 Ruff 目前有既存告警，应单独核对是否由新代码引入。

> 注：本次核实是在云端容器里用独立搭建的 Python 3.11 venv 跑的测试（Mac 本机 `.venv` 是 3.13 符号链接，跨环境不可执行），文件改动已通过设备桥写回本机仓库。建议下一位开发者在本机 `.venv`（3.13）里重新跑一遍确认环境无关。

## N2 接续顺序

1. **先补测试再补前端**：给取消功能写一个真并发测试（做法同本次给 409 测试写的并发模式）；决定超时策略——要么实现真正的超时掐断并测试，要么在文档里明确写"当前版本不限时"并删掉死代码 `_RUN_TIMEOUT_S`；给"进程重启时清理卡住的 running 行"写明确行为（例如启动时把所有 `status='running'` 的行标记为 `interrupted`）并补测试。
2. 在实验本页面接入草稿列表和编辑，确保同名素材按 ID/相对路径区分，选择前序具名输出时只显示类型兼容端口。
3. 前端接入已有的执行/历史 API（`POST .../runs`、`GET .../runs`、`GET .../runs/{id}`）：触发运行、展示每步状态和产物、查看历史列表；保存运行 ID，支持刷新和服务重启后查看历史。
4. 实现参数或上游变化后的结果过期提示、必要步骤重跑与缓存来源。
5. 全部完成后再按 [DELIVERY.md](DELIVERY.md) 的退出条件判断 N2 是否完成。
