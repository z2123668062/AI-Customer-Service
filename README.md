# 智路由 AI 客服系统（V3.0 — 企业级深度优化进行中）

本项目是一个面向终端用户的企业级 AI 客服系统，采用"意图分发 + RAG 知识库 + API 工具编排"的混合架构。

V3.0 已完成填坑阶段的全部 5 个 P0/P1 任务：**并发模型全线异步化、安全漏洞纵深加固、异常全链路可追溯、测试体系重构、基础设施配置统一**。共改动 26 个文件，新增 31 个测试用例，消除 3 类系统性隐患。

> **一句话总结**：之前是"能跑"，现在是"出了事能查、被刷了能抗、密钥不会裸奔、测试不会炸"。

---

## 核心特性

1. **精准意图路由**——大模型（GLM-4）将用户输入划分为 `kb_qa`（知识问答）、`chitchat`（闲聊）、`tool`（工具调用）、`complaint`（投诉转人工），结构化 JSON 输出

2. **RAG 检索增强生成**——LlamaIndex + Qdrant 向量数据库 + BGE 本地向量模型 + 交叉重排（BGE Reranker），10 进 2 出，答案有据可查

3. **Agent 工具编排**——动态识别工具调用诉求，两轮对话式闭环：先调真实 API（高德天气、汇率）拿数据，再包装成人话

4. **SSE 流式输出**——Server-Sent Events 逐字推送，前端实时渲染，不卡等

5. **全链路用户体系**——JWT 无状态认证（注册/登录/手机验证码登录）+ 用户隔离的多会话管理

6. **多层防御体系**——Trie 树敏感词拦截、Redis 分布式锁防连点、固定窗口限流防刷、IP 级别接口限流、管理接口 Admin-Token 鉴权

7. **异常全链路可追溯**——每个请求自动分配 TraceID，业务异常自带 `error_code` + `module` 标识来源模块，响应体携带 `session_id` + `trace_id` + `error_code`，前端可差异化处理

8. **并发非阻塞**——全链路异步（AsyncOpenAI + `asyncio.to_thread` + `asyncio.wait_for`），所有外部调用有超时熔断，单用户卡死不拖累其他用户

9. **冷热分离记忆系统**——Redis 滑动窗口（最近 10 轮）做短期热记忆，MySQL 归档做长期冷存储，BackgroundTasks 异步双写不阻塞主流程

---

## V3.0 已完成亮点

### 并发模型异步化（任务 1.1）
- `OpenAI()` → `AsyncOpenAI()`，`query_knowledge` 同步转异步
- 所有外部调用加 `asyncio.wait_for` 超时熔断（大模型 15s / RAG 10s / 工具 15s）
- 同步 `redis.Redis` → `redis.asyncio.Redis`，5 个服务层全链路 await

### 安全漏洞修复（任务 1.2）
- 独立 JWT 密钥，不再复用 API Key
- 高德 Key 走 `.env` 配置，代码零硬编码
- 知识库重建接口增加 Admin-Token 校验
- 登录/注册加 IP 限流，每 IP 每分钟 5 次
- 全局 CORS 中间件配置

### 异常全链路追溯（任务 1.3）
- 新建 `exceptions.py` + `error_handlers.py`，异常处理器独立解耦
- 全局 TraceID 中间件，每个请求自动分配
- 异常响应体带 `session_id` + `trace_id` + `error_code` + `module`
- 业务异常基类 `AppException`，支持自定义错误码和模块标记

### 测试体系重构（任务 1.4）
- 现有测试全部对齐异步代码，修复断开的 mock 路径
- 新建 4 个测试文件，覆盖 auth / session / memory / ratelimit 四个核心服务层
- **共 31 个测试用例，全部通过**
- 全局 `conftest.py` 统一 mock Redis fixture

### 基础设施坑修复（任务 1.5）
- Redis 连接参数走 `config.py` 配置，支持按环境切换
- `requirements.txt` 移除 `kubernetes`、`chromadb` 等未使用依赖
- 拆分为 `requirements-prod.txt`（运行时）和 `requirements-dev.txt`（开发测试）

---

## 技术选型栈

| 层级 | 选型 |
|------|------|
| 应用框架 | FastAPI + Uvicorn |
| 模型底座 | 智谱 GLM-4-flash（API）+ HuggingFace BGE 向量模型（本地） |
| 数据检索 | LlamaIndex + Qdrant + BGE Reranker |
| 短期记忆 | Redis（滑动窗口 + 限流计数器 + 分布式锁） |
| 长期归档 | MySQL + SQLAlchemy 异步 ORM |
| 认证鉴权 | JWT（HS256）+ bcrypt 密码哈希 |
| 参数验证 | Pydantic v2 |
| 日志体系 | Loguru |
| 测试框架 | Pytest + pytest-asyncio（31 个用例） |

---

## 快速启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 .env（根目录）
ZHIPU_API_KEY=your_key
DATABASE_URL=mysql+aiomysql://root:pass@localhost:3306/ai_agent
JWT_SECRET_KEY=your_32_char_random_string
ADMIN_TOKEN=your_admin_token
DB_CONNECT_RETRIES=3

# 3. 确保基础设施运行
#    Redis（默认 localhost:6379）
#    MySQL
#    Qdrant（默认 localhost:6333）

# 4. 启动虚拟环境和后端
.venv\Scripts\activate
uvicorn app.main:app --reload

# 5. 启动前端（新终端）
streamlit run frontend/streamlit_app.py

# 6. 首次使用需构建知识库
curl -X POST http://127.0.0.1:8000/api/v1/kb/build \
  -H "X-Admin-Token: your_admin_token"
```

---

## 项目结构

```
app/
  api/v1/endpoints/     # API 路由层（chat / auth / sessions / kb）
  core/                 # 基础设施层（config / database / memory / logging / exceptions / error_handlers）
  models/               # 数据模型（domain ORM / schemas Pydantic）
  services/             # 业务服务层（router / rag / tool / safety / auth / session / history / ratelimit）
  main.py              # FastAPI 应用入口
frontend/
  streamlit_app.py      # 前端可视化界面
tests/                  # 31 个测试用例
迭代计划/               # 版本演进方案设计文档
开发日记/               # 各阶段开发总结与架构反思
```

---

## 运行测试

```bash
pytest tests/ -v
```

当前覆盖：tool_service（4）、rag_service（1）、safety_service（4）、memory（6）、ratelimit（5）、session_service（5）、auth_service（6），共 **31 个测试用例全部通过**。

---

## 架构概览

```mermaid
flowchart TD
    A[用户端 Streamlit] -->|SSE 流式请求| B[FastAPI 应用层]
    B --> M1[TraceID 中间件]
    M1 --> M2[CORS 中间件]

    subgraph 安全与接入控制
        B --> C{安全审校 Trie树}
        C --违规--> Z[拦截响应]
        C --安全--> D[JWT 鉴权]
        D --> R[IP 限流 check]
        R --> E[限流检查 Redis]
    end

    subgraph 核心业务编排
        E --> F[语义路由器 GLM-4]
        F -->|chitchat| G[闲聊生成]
        F -->|kb_qa| H[RAG 流水线]
        F -->|tool| I[工具调度引擎]
        F -->|complaint| J[投诉转人工]

        H --> H1[Qdrant 向量检索]
        H1 --> H2[BGE Reranker 重排]
        H2 --> H3[GLM-4 生成回答]

        I --> I1[天气 / 汇率 / 订单 API]
    end

    subgraph 数据与状态
        G --> K[(Redis 短期记忆 10轮滑动窗口)]
        H --> K
        I --> K
        K --> L[(MySQL 长期归档 BackgroundTasks)]
    end

    subgraph 可观测性
        B --> M[Loguru 日志 TraceID 追踪]
        B --> N[全局异常分级降级<br/>AppException + error_code + module]
    end

    subgraph 超时熔断
        G --> T[asyncio.wait_for 15s]
        H --> T
        I --> T
    end

    G --> O[SSE 流式返回]
    H --> O
    I --> O
    J --> O
```

---

## 接口一览

| 端点 | 方法 | 说明 | 鉴权 |
|------|------|------|------|
| `/health` | GET | 健康检查 | 无 |
| `/api/v1/chat/` | POST | 发送聊天消息（SSE 流式） | Bearer Token |
| `/api/v1/kb/build` | POST | 重建知识库索引 | Admin-Token |
| `/api/v1/auth/register` | POST | 用户注册 | IP 限流 5/min |
| `/api/v1/auth/login` | POST | 用户登录 | IP 限流 5/min |
| `/api/v1/auth/send-code` | POST | 发送手机验证码（Mock） | IP 限流 3/min |
| `/api/v1/auth/phone-login` | POST | 手机验证码登录 | IP 限流 5/min |
| `/api/v1/sessions/` | GET | 列出会话 | Bearer Token |
| `/api/v1/sessions/` | POST | 创建会话 | Bearer Token |
| `/api/v1/sessions/{id}` | PATCH | 更新会话标题 | Bearer Token |
| `/api/v1/sessions/{id}` | DELETE | 删除会话 | Bearer Token |

详细定义见启动后的 Swagger 文档：`http://127.0.0.1:8000/docs`

---

## 版本演进

- **V1.0（MVP 闭环）**：语义路由 + ChromeDB RAG + 基础工具调用 + Trie 敏感词拦截，跑通最短路径
- **V2.0（生产级架构升级）**：Redis + MySQL 记忆持久化、Qdrant + Reranker 冷热分离、真实异步工具、JWT 鉴权、多会话管理、限流防刷、Loguru 日志体系、全局异常降级
- **V3.0 填坑阶段（✅ 已完成）**：并发模型异步化、安全纵深加固、异常全链路可追溯、31 个测试用例覆盖、基础设施配置统一
- **V3.0 改旧阶段（🔄 规划中）**：语义路由置信度加固、工具注册表 + 结果缓存、记忆系统深度优化、架构分层解耦、PII 检测、可观测性三支柱

详细方案见 [V3.0 企业级深度优化方案](迭代计划/PROJECT_V3_UPGRADE_PLAN.md)。
