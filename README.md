# 个人助手 · 长期记忆系统

单用户个人助手 + 长期记忆系统。对话侧只做一件事：把用户说过的事记住，并在需要时想起来。

上下文窗口有限，而用户的历史持续增长——这个矛盾用操作系统内存管理的思路解决：
**分层存储 + 按需调入**。常用记忆常驻内存（快表），冷记忆落库并建向量索引，
需要时按"缺页 → 回源 → 回填"的路径取回。

## 快速开始

```bash
# 1. 依赖
pip install -r requirements.txt

# 2. 配置
cp .env.example .env          # 至少填 LLM_API_KEY 与 DATABASE_URL

# 3. 中间件（可缺省：Redis / Qdrant 不可用时自动降级）
redis-server                                   # 热层滑动窗口
docker run -p 6335:6333 qdrant/qdrant          # 向量库（.env 配 QDRANT_URL 指向 6335）

# 4. 启动
uvicorn app.main:app --reload                   # 后端 http://127.0.0.1:8000
streamlit run frontend/streamlit_app.py        # 前端（另开一个终端）
```

## 目录

```
app/
  api/v1/endpoints/     chat.py（SSE 对话）· memory.py（记忆面板接口）
  core/                 config · database · short_term_memory（热层 + 组装包）· telemetry
  models/               domain（三张表）· schemas（接口模型）
  services/
    chat_pipeline.py    主链路编排（锁 → 读记忆 → 组装 → 生成 → 归档 → 写路径）
    generation_service.py  全系统唯一 LLM 生成入口
    history_service.py  原文归档
    long_term_memory/   记忆包：写入 / 检索 / 治理
frontend/               Streamlit 两页：对话 + 记忆面板
tests/                  单元与集成测试
```

## 记忆包（app/services/long_term_memory/）

| 模块 | 职责 |
|---|---|
| `store.py` | 三表 CRUD + 唯一提交点 `commit_and_sync`（DB → 快表 → 向量，失败降级） |
| `pool.py` | 进程内快表：实体倒排 / 时间轴 / 邻接镜像 / SLRU 两段 + 换出记录簿 |
| `retriever.py` | 分诊 → 抽线索 → 扫快表 → 缺页回源（L1 结构化 + 语义 / L2 原文）→ 顺线 → 粗排 |
| `reranker.py` | 逐卡判档（low/mid/high）→ 档位折算硬阈值门 → 截断注入上限 |
| `injector.py` | 渲染注入段：不暴露卡 ID，窗口已有的不重复注 |
| `gate.py` | 全部判断题的单一出口（五个函数，永不抛异常，失败走保守方向） |
| `consolidator.py` | 写路径编排：三触发（攒批 / 重要声明 / 空闲）→ 后台串行抽取 → 水位推进 |
| `extractor.py` | 消息 → 卡片草稿（分段抽取，段失败不拖垮整批） |
| `writer.py` | 判重（完全同文）→ 取代判定 → 因果回填 → 落库 |
| `journal.py` | 抽取水位：追加式日志，末行 `watermark=N` 即进度 |
| `evictor.py` | 淘汰：目标过期 / 偏好衰减（确定性规则，零 LLM） |
| `explicit.py` | "记住 X" 的零延迟通道（Redis 暂存 → 优先单抽 → `source=explicit`） |

## 三张表

- **cards**：记忆卡。`entity`（用户的状态/偏好/目标/关系）与 `event`（发生过的事）同表；
  `status` = `active` / `superseded`（被新卡取代）/ `revoked`（用户撤销）/ `evicted`（衰减下架）
- **edges**：卡间关系。`caused_by`（事件→事件）/ `led_to`（事件→实体）/ `supersedes`（新→旧）；
  `cut_at` 非空 = 已剪断（保留审计，不参与检索）
- **messages**：对话原文。只增不改不删——卡片是有损抽象，原文才是真相

## 测试

```bash
pytest -q
```

配置项说明见 `.env.example`；`app/core/config.py` 只有一条主干，没有运行模式开关。
