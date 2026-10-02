# qa_v1 —— 记忆系统 v1.0 · 系统性实测（只测不改）

> 定位：本目录是 **v1.0 版本的 bug 记录与分析的证据仓**。本轮只做三件事——
> **实测、记录、原因分析**。不改动 `app/` 下任何源码，也不对系统行为做任何人为干预
> （除下文声明的环境隔离外）。
>
> 起因：2026-10-02 的一次随手垃圾信息测试暴露出若干问题（连发超时丢失、
> 持久失效、垃圾入卡、性能雪崩）。本目录用一组**有章法的用例矩阵**扩大测试面，
> 把"没扛住"从模糊观感变成可复现、可定位、有边界的问题清单。

---

## 1. 测试目标

1. **验证系统承诺的核心能力**是否真实可用：写入（三通道）→ 读取（多问法）→ 取代 → 拒答；
2. **扩大问题面**：不只测 10-02 已知的四类问题，而是按能力维度设计矩阵（含对照与陷阱），
   暴露**更多**潜在问题；
3. **定义问题边界**：每个问题记录触发条件、复现条件、不触发的对照情形与证据；
4. **全程留证**：对话原文、埋点 JSONL、journal、数据库快照四方对账。

## 2. 执行方式

```powershell
# 在 AI-Customer-Service 目录下运行（venv 的 python）
.venv 的 python 与工作目录均按仓库现有环境；脚本内部会自行 chdir 到 AI-Customer-Service

# 全量（三段依次跑）
python qa_v1\run_qa.py --sections 1,2,3

# 分段跑（建议：段1 跑完检查记录再继续；--run-id 相同则续跑同一记录目录与测试库）
python qa_v1\run_qa.py --sections 1
python qa_v1\run_qa.py --sections 2
python qa_v1\run_qa.py --sections 3
```

- 用例集：`cases.jsonl`（唯一用例源，按执行顺序排列；字段说明见文件内注释行）；
- 执行器：`run_qa.py`（同进程直驱真实 `chat_endpoint`，完整复用真实 30s 超时 / SSE / meta 逻辑）；
- 记录：`records/<run_id>/`（见 §5）。

## 3. 环境隔离（关键，防止污染主库与演示状态）

| 资源 | 生产值 | 测试值 | 手段 |
|---|---|---|---|
| SQLite 库 | `data/app.db` | `data/qa_v1/app_<run_id>.db`（**复制副本**） | 环境变量 `DATABASE_URL`（脚本启动时校验 URL 解析路径一致性） |
| 抽取 journal | `data/extract_journal.log` | `records/<run_id>/journal.log` | 环境变量 `EXTRACT_JOURNAL_PATH` |
| 埋点 telemetry | `data/telemetry/` | `records/<run_id>/telemetry/` | 环境变量 `TELEMETRY_DIR` |
| 淘汰/编辑日志 | `data/evict_log.jsonl` 等 | `records/<run_id>/` | 环境变量（`EVICT_LOG_PATH` / `EDIT_LOG_PATH`） |
| Redis 短记忆窗口 | DB 0 | **DB 1** | 环境变量 `REDIS_DB` |
| Qdrant 向量集合 | `memory_cards` | `qa_v1_cards`（独立集合） | 环境变量 `QDRANT_COLLECTION` |

- 唯一无法隔离的落盘：`logs/ai_agent_2026-10-02.log`（loguru 固定路径）——
  测试日志会追加在该文件中，分析时按 Trace 前缀区分；
- 主库、主 journal、主 telemetry、主 Redis 库、主 Qdrant 集合**全程零接触**（脚本启动时自检断言）。

## 4. 用例矩阵总览（A~O 组，共 66 条对话用例 + 11 个等待锚点）

| 段 | 组 | 会话 | 主题 | 要点 |
|---|---|---|---|---|
| 1 | A | qa-v1-s1 | 基线闲聊 | 无记忆需求问题的对照通路（skip） |
| 1 | B | qa-v1-s1 | 写入三通道 | explicit（记住）/ important（对…过敏）/ rounds（攒批） |
| 1 | C | qa-v1-s1 | 多事实单条 | 一条消息三个事实的拆卡完整性 |
| 1 | D | qa-v1-s1 | 读取问法矩阵 | 同事实 5 种问法；含 10-02 故障 B 的受控复刻（不含实体词） |
| 1 | E | qa-v1-s1 | 窗口外长期记忆 | 挤出窗口后的纯长期路径 + 聚合观察 |
| 1 | F | qa-v1-s1 | 取代/更新 | 事件更新（钢琴→油画）、居所更新（杭州→苏州） |
| 1 | G | qa-v1-s1 | 拒答/不编造 | 三条"从未提过"的诱导问题 |
| 1 | H | qa-v1-s1 | 历史卡复测 | 10-02 原题（周宁/王小羊）直接复测 |
| 2 | I | qa-v1-s2 | 连发压力 | 10 条快速连发（复刻 10-02 场景）+ 丢失对账 |
| 2 | J | qa-v1-s2 | 轻度连发+写入 | 3 条连发中含 explicit 写入 |
| 2 | K | qa-v1-s2 | 压力后一致性 | 压力下写入的窗口读取 |
| 3 | L | qa-v1-s3 | 输入鲁棒性 | 超长文本（埋事实）、纯表情、特殊字符、多语言、重复串 |
| 3 | M | qa-v1-s3 | 会话隔离/跨会话 | 新会话读全局长期记忆；会话内容边界观察 |
| 3 | N | qa-v1-s3 | 污染与判重 | 假消息 explicit、元指令假信息、垃圾、同文重发判重、污染探测 |
| 3 | O | qa-v1-s3 | 能力边界探索 | 对话内记忆列举 / 遗忘请求（系统未设计的能力） |

### 与 10-02 事件的复测映射

| 10-02 问题 | 本目录复测用例 |
|---|---|
| A 连发超时+消息丢失 | I 组（10 条连发，对账丢失数） |
| B 实体撞名短路致假性失忆 | D2（受控复刻）+ H1（原题原样） |
| C 垃圾/假消息入卡与注入污染 | N 组（假消息卡、元指令、污染探测） |
| D 性能雪崩（库大→精筛多→超时） | I 组延迟分布 + 各轮 telemetry（stage_ms） |

## 5. 记录与证据（records/<run_id>/）

| 文件 | 内容 |
|---|---|
| `environment.md` | 配置快照（PRESET/hash/隔离断言结果/库副本来源/启动耗时） |
| `conversation.jsonl` | 每轮：用例 id、消息、状态（ok/timeout/error）、耗时、meta、回复全文 |
| `journal.log` | 抽取 journal（水位真相） |
| `telemetry/telemetry-*.jsonl` | 埋点（chain/cost/health 三组） |
| `db_before.json` / `db_after.json` | 测试库卡表全量快照（判定"卡未建"还是"建了没召回"的唯一依据） |
| `waits.jsonl` | 每个等待锚点的结果（等待时长/是否超时/nudge 补发记录） |
| `summary.json` | 汇总（轮数/超时数/错误数/新增卡数） |
| `run.log` | 脚本自身日志 |

## 6. 判定口径

- **结构判定（自动）**：超时兜底文案、错误、等待超时、丢失消息数（DB 对账）——脚本直出；
- **内容判定（人工）**：回复是否正确使用记忆、是否编造、是否污染——由分析阶段逐条读
  `conversation.jsonl` 给出，写入 `ANALYSIS.md`；
- **归因纪律**：判定某条"读取失败"前，必须先用 `db_*.json` 确认目标卡**已建**
  （区分"没建卡"与"建了读不出"）；
- **观察项 vs 断言项**：用例的 `expect` 字段为测量口径；标注"观察"的不做对错强判，
  只记录行为事实。

## 7. 已知边界声明

- 直驱 `chat_endpoint` 覆盖：锁、超时、检索、生成、归档、写路径、埋点、SSE 事件构造；
  **不覆盖**：uvicorn/HTTP 传输层、trace_id 中间件、FastAPI 参数解析（`ChatRequest`
  校验在脚本内直接触发，等价）；
- 启动序列复刻 `main.py` lifespan 的最小集（init_db → startup_check → pool.build →
  钩子注册 → 淘汰巡检 → startup_scan）；**不启用**空闲收口巡检（`start_idle_reaper`，30 分钟
  阈值在测试窗口内不会触发，跳过以避免后台循环干扰计时）；
- 测试库为 `data/app.db` 的**时点副本**——库内既有历史卡（10-02 及更早）会参与检索，
  这是**刻意保留**的：既验证历史状态的真实行为（H 组），也让"跨历史污染"暴露在光下
  （判定时以 `db_before.json` 为基线区分）。
