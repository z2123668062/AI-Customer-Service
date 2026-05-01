# 智路由 AI 客服系统

这是一个面向终端用户的 AI 客服系统。目前处于 **第 2 期：语义路由分析架构完成** 状态。

## 1. 核心功能及进度
- [x] **第1期**：基础 FastAPI 框架、Pydantic 数据模型、内存会话结构、自动化测试打底。
- [x] **第2期**：利用“大模型（GLM-4-Flash）+ 结构化 JSON 提示词工程”实现语义路由，智能分析用户意图（闲聊/知识库/工具调取），并实现后端多分支控制流，配置 Pytest Mock 测试环境全覆盖打桩。
- [ ] **第3期计划**：接入连贯上下文记忆 / 对接本地化知识库 RAG 的搭建。

## 2. 原理由与架构（第 2 期迭代）
本期的升级极大地增强了系统的“智商”。流程现更新为：
`用户自然语言交互` -> `数据模型校验` -> `调用智谱免费版大模型进行意图识别(Router)` -> `依据解析后的路由对象走对应 if/else 分支` -> `记入缓存记忆` -> `标准格式回传前端`

## 3. 快速开始 (环境搭建)
本项目推荐使用 Python 开发并配制虚拟环境。

在 PowerShell 中执行以下命令准备环境：
```powershell
# 1. 创建虚拟环境
python -m venv .venv

# 2. 激活虚拟环境
.\.venv\Scripts\Activate.ps1

# 3. 安装依赖库
pip install -r requirements.txt

# 4. 启动 FastAPI 服务
uvicorn app.main:app --reload
```
服务启动完成后，可在浏览器打开可视化 API 说明：http://127.0.0.1:8000/docs

##4.自动化测试与工程规范  
系统已集成基础的自动化集成测试，并在意图识别服务上实现了 深度网络请求的 Mock (打桩)。 在 PowerShell 中执行以下命令进行自检：
```powershell
# 1. 激活虚拟环境
.\.venv\Scripts\Activate.ps1
# 2. 运行测试
pytest -v --cov=app --cov-report=html
```
测试覆盖率报告将生成在 `htmlcov/index.html`，打开即可查看详细覆盖率分析。
