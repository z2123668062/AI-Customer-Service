# 智路由 AI 客服系统（V2.0 — 生产级升级完成）

本项目是一个直接面向终端用户的企业级 AI 客服系统。采用"意图分发 + RAG 知识库 + API 工具编排"的混合架构，经过 V2.0 阶段的生产级架构升级，已完成从"能跑的玩具"到"可抗并发、有记忆、有鉴权、有流控"的系统演进。现正进入 **V3.0 企业级深度优化**阶段。

---

## 核心特性

1. **精准意图路由**：利用大模型（GLM-4）将用户意图划分为 `kb_qa`（知识问答）、`chitchat`（闲聊）、`tool`（工具调用）和 `complaint`（投诉转人工），结构化 JSON 输出，后续可按置信度分级处理。

2. **RAG 检索增强生成**：基于 LlamaIndex + Qdrant 向量数据库 + BGE 本地向量模型的完整 RAG 流水线，离线建库 + 在线查询冷热分离，加入交叉重排（BGE Reranker）提升召回精度，10 进 2 出，答案有据可查。

3. **Agent 工具编排**：动态识别工具调用诉求，真实异步 HTTP 请求（高德天气、汇率 API），两轮对话式 Agent 闭环——先调工具拿数据，再包装成人话。

4. **SSE 流式输出**：所有回复以 Server-Sent Events 逐字推送，前端实时渲染，交互体验流畅自然。

5. **全链路用户体系**：JWT 无状态认证（注册/登录/手机验证码登录），用户隔离的多会话管理，每个会话独立上下文，切换不丢失。

6. **多层防御体系**：Trie 树敏感词拦截（O(N) 时间复杂度），Redis 分布式锁防连点，固定窗口限流防刷（聊天 20 次/分钟，创建会话 5 次/分钟），全局异常分级兜底。

7. **冷热分离记忆系统**：Redis 滑动窗口（最近 10 轮）做短期热记忆，MySQL 归档做长期冷存储，BackgroundTasks 异步双写不阻塞主流程。

8. **灰度级容错防御**：全局 TraceID 链路追踪，三层异常降级网（可恢复重试、局部降级、系统级兜底），确保极端异常下前端不崩。

---

## 技术选型栈

- **应用框架**：FastAPI + Streamlit
- **模型底座**：智谱 GLM-4-flash（API）+ HuggingFace BGE 向量模型（本地）
- **数据检索**：LlamaIndex + Qdrant（独立向量数据库）
- **短期记忆**：Redis（滑动窗口 + 限流计数器）
- **长期归档**：MySQL + SQLAlchemy（异步 ORM）
- **认证鉴权**：JWT（HS256）+ bcrypt 密码哈希
- **参数验证**：Pydantic v2
- **日志体系**：Loguru（控制台 + 文件轮转切割）
- **测试框架**：Pytest + pytest-asyncio

---

## 版本演进里程碑

- **V1.0**：MVP 闭环。搭通语义路由 + ChromeDB RAG + 基础工具调用 + Trie 树敏感词拦截的最短路径，跑通全流程。
- **V2.0（当前）**：生产级架构升级。记忆持久化（Redis + MySQL）、RAG 冷热分离（Qdrant + Reranker）、真实工具生态（异步 HTTP）、SSE 流式输出、JWT 用户认证、多会话管理、限流防刷、Loguru 日志体系、全局异常分级降级。从"能跑"到"能抗"。
- **V3.0（规划中）**：企业级深度优化。15 项改进覆盖四大方向——并发模型异步化（AsyncOpenAI + asyncio.wait_for）、安全纵深加固（独立 JWT 密钥 + PII 检测 + CORS）、架构分层优化（巨石 stream_generator 拆分为 ChatService）、可观测性建设（Prometheus 指标 + 深度健康检查）、测试体系重构（全覆盖 + 集成测试）。详见 [V3.0 升级方案](迭代计划/PROJECT_V3_UPGRADE_PLAN.md)。

---

## 快速启动指南

### 1. 环境准备

确保 Python 3.10+，创建虚拟环境：

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

在项目根目录创建 `.env` 文件：

```env
ZHIPU_API_KEY=your_zhipu_api_key_here
DATABASE_URL=mysql+aiomysql://root:password@localhost:3306/ai_agent
DB_CONNECT_RETRIES=3
```

### 3. 启动基础设施

需要确保以下中间件已运行：

- **Redis**（默认 localhost:6379）
- **MySQL**（DATABASE_URL 指向的实例）
- **Qdrant**（默认 localhost:6333）

### 4. 一键启动后端

```bash
uvicorn app.main:app --reload
```

后端启动后，可访问 `http://127.0.0.1:8000/docs` 查看 Swagger 互动接口文档。

### 5. 启动可视化 Web 界面

新开一个终端：

```bash
streamlit run frontend/streamlit_app.py
```

### 6. 构建知识库（首次使用）

```bash
curl -X POST http://127.0.0.1:8000/api/v1/kb/build
```

---

## 项目结构

```
app/
  api/v1/endpoints/     # API 路由层（chat / auth / sessions / kb）
  core/                 # 基础设施层（config / database / memory / logging）
  models/               # 数据模型（domain ORM / schemas Pydantic）
  services/             # 业务服务层（router / rag / tool / safety / auth / session / history / ratelimit）
  main.py              # FastAPI 应用入口
frontend/
  streamlit_app.py      # 前端可视化界面
tests/                  # 测试用例
迭代计划/               # 版本演进方案设计文档
开发日记/               # 各阶段开发总结与架构反思
```

---

## 运行测试

```bash
pytest tests/ -v
```

---

## 架构概览（V2.0 目标态）

```mermaid
flowchart TD
    A[用户端 Streamlit] -->|SSE 流式请求| B[FastAPI 应用层]

    subgraph 安全与接入控制
        B --> C{安全审校 Trie树}
        C --违规--> Z[拦截响应]
        C --安全--> D[JWT 鉴权]
        D --> E[限流检查 Redis]
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
        B --> N[全局异常分级降级]
    end

    G --> O[SSE 流式返回]
    H --> O
    I --> O
    J --> O
```

---

## 接口文档

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/api/v1/chat/` | POST | 发送聊天消息（SSE 流式返回） |
| `/api/v1/kb/build` | POST | 重建知识库索引（冷任务） |
| `/api/v1/auth/register` | POST | 用户注册 |
| `/api/v1/auth/login` | POST | 用户登录 |
| `/api/v1/auth/send-code` | POST | 发送手机验证码（Mock） |
| `/api/v1/auth/phone-login` | POST | 手机验证码登录 |
| `/api/v1/sessions/` | GET/POST | 列出/创建会话 |
| `/api/v1/sessions/{id}` | PATCH/DELETE | 更新/删除会话 |

详细接口定义见 Swagger 文档：`http://127.0.0.1:8000/docs`

---

## V3.0 规划方向

V3.0 的核心目标是 **从"能跑"到"能扛、能查、能信、能改"**，覆盖 15 个改进维度：

- **Phase 1（核心质量加固）**：路由置信度评分 / RAG 异步化 / 语言组织模块独立 / 敏感词库扩充 + PII 检测 / 工具注册表 + 缓存 / 分级异常体系
- **Phase 2（工程体系加固）**：测试全覆盖 / 安全纵深（独立 JWT 密钥 + CORS + 防暴力破解）/ 全链路 AsyncOpenAI / 可观测性三支柱 / 架构分层解耦
- **Phase 3（体验与运维闭环）**：API 设计规范化 / 前端体验优化 / 记忆系统深度优化 / Docker 化 + CI

详见 [V3.0 企业级深度优化方案](迭代计划/PROJECT_V3_UPGRADE_PLAN.md)。
