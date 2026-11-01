# 个人助手 · 长期记忆系统

单用户个人助手 + 长期记忆。对话侧只做一件事：把用户说过的事记住，需要时想起来。

核心矛盾是**窗口有限而历史持续增长**。解法沿用操作系统内存管理的思路——分层存储 + 按需调入：
常用记忆常驻进程内（快表），冷记忆落库并建向量索引，需要时按「抽线索 → 扫快表 → 缺页回源」取回。

## 快速开始

```bash
# 1. 依赖
pip install -r requirements.txt

# 2. 配置
cp .env.example .env            # 至少填 LLM_API_KEY；DATABASE_URL 默认 sqlite

# 3. 中间件（可缺省：Redis / Qdrant 不可用时自动降级）
redis-server                                    # 热层滑动窗口
docker run -p 6335:6333 qdrant/qdrant           # 向量库（.env 的 QDRANT_URL 指向 6335）

# 4. 后端
uvicorn app.main:app --port 8000

# 5. 前端（另开终端）
cd web && npm install && npm run dev            # http://localhost:5173
```

前端是 Vue 3 + Vite，开发期通过 Vite 代理直连 8000，不需要配 CORS。

## 目录

```
app/
  api/v1/endpoints/     chat.py（SSE 对话）· memory.py（面板接口）· sessions.py（会话列表）
  core/                 config · database · short_term_memory（热层 + 组装包）· telemetry
  models/               domain（三张表）· schemas（接口模型）
  services/
    chat_pipeline.py    主链路编排（锁 → 读记忆 → 组装 → 生成 → 归档 → 写路径）
    generation_service.py  全系统唯一 LLM 生成入口
    history_service.py  原文归档
    long_term_memory/   记忆包：写入 / 检索 / 治理
web/                    Vue 前端：对话（含引用胶囊）+ 记忆星图 + 卡片抽屉
bench/                  评测：脚本、结论、逐题记录、数据集
tests/                  单元与集成测试
```

## 记忆包（app/services/long_term_memory/）

| 模块 | 职责 |
|---|---|
| `store.py` | 三表 CRUD + 唯一提交点 `commit_and_sync`（DB → 快表 → 向量，失败降级） |
| `pool.py` | 进程内快表：实体倒排 / 时间轴 / 邻接镜像 |
| `retriever.py` | 抽线索 → 扫快表 → 缺页回源（L1 结构化 + 语义 / L2 原文）→ 粗排截断 |
| `reranker.py` | 逐卡判档（low/mid/high）→ 档位折算 → 硬阈值门 → 截断注入上限 |
| `injector.py` | 渲染注入段：不暴露卡 ID，窗口已有的不重复注 |
| `gate.py` | 三个判定（取代 / 回填 / 精筛）的单一出口，永不抛异常 |
| `consolidator.py` | 写路径编排：三触发（攒批 / 重要声明 / 空闲）→ 后台串行抽取 → 水位推进 |
| `extractor.py` | 消息 → 卡片草稿（只吃用户消息，助手回复不建卡） |
| `writer.py` | 判重（完全同文）→ 取代判定 → 因果回填 → 落库 |
| `journal.py` | 抽取水位：追加式日志，末行 `watermark=N` 即进度 |
| `evictor.py` | 淘汰：目标过期 / 偏好衰减（确定性规则，零 LLM） |
| `explicit.py` | 「记住 X」的零延迟通道（Redis 暂存 → 优先单抽 → `source=explicit`） |

## 精排开关

精排是每轮 60 多次逐卡 LLM 判定，占端到端 token 的九成、延迟的七成。它可以在两个层面切换：

- **服务端默认**：`.env` 的 `RERANK_ENABLED`
- **逐轮覆盖**：对话请求体里的 `use_rerank`（前端输入框下方的开关）

关闭后走兜底排序：有语义分用语义分，没有就按入口给固定分（实体 0.60 / 时间 0.55 / 顺线 0.50 / 原文 0.45），取前八。

实测同一道题：关闭 2.2 s、注入 8 卡；打开 6.1 s、注入 3 卡。

## 接口

| 端点 | 用途 |
|---|---|
| `POST /api/v1/chat/` | SSE 对话（`status` / `meta` / `message` / `done` 四类事件） |
| `GET /api/v1/memory` | 卡片列表（status / kind / subtype 过滤） |
| `GET /api/v1/memory/{id}` | 单卡详情：双向边 + 版本链 + **来源原文** |
| `PATCH /api/v1/memory/{id}` | 内容修订（新卡 + supersedes 旧卡，不原地改） |
| `DELETE /api/v1/memory/{id}` | 撤销（可恢复）；`/purge` 为物理删除 |
| `GET /api/v1/memory/graph` | 人物节点 + 共现关系（一次取全，供星图渲染） |
| `GET /api/v1/sessions` | 会话列表（标题取首条用户消息） |

## 三张表

- **cards**：记忆卡。`entity`（状态/偏好/目标/关系）与 `event`（发生过的事）同表；
  `status` = `active` / `superseded` / `revoked` / `evicted`
- **edges**：卡间关系。`caused_by` / `led_to` / `supersedes`；`cut_at` 非空 = 已剪断（保留审计）
- **messages**：对话原文。只增不改不删——卡片是有损抽象，原文才是真相

会话（session）只决定哪 20 条原文进短期窗口，**不影响记忆**：记忆全局共享。

## 评测数据（bench/）

```
bench/
├── docs/        实验设计与结论文档
├── results/     结论数字（三臂对照、全上下文两档、挤出曲线）
├── raw/         逐题记录（dilution 48 个 / s0 三臂 12 个）
├── scripts/     评测脚本（run_eval / score / judging / run_dilution_eval …）
└── data/        locomo10.json
```

脚本用相对路径定位仓库根，`DATA_FILE` 可被 `LOCOMO_PATH` 覆盖。

## 测试

```bash
pytest -q        # 252 项
```

## 已知边界

- **单租户**：卡片与索引没有用户维度，多套记忆靠部署级隔离（独立 DB + 独立集合 + 独立端口）
- **不主张整体优于全上下文**：199 题上全上下文加日期前缀是 77.0%，本系统 53.9%；优势在注入成本与挤出鲁棒性
- **因果链已回滚**：分数沿边传播与多跳的实现曾落地并实测无效，随 stash 保留，不在主链路
- **精排买 9.2 个点要付 14 倍 token**，是否开启取决于预算
