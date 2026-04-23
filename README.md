# 智路由 AI 客服系统

这是一个面向终端用户的 AI 客服系统。目前处于 **第 1 期：基础架构搭建完成** 状态。
目前已实现：基础的 FastAPI 框架搭建、数据结构标准规范、内存级多轮会话记忆结构、基础聊天联调闭环响应。

## 1. 原理与架构（第 1 期）
系统最基础的骨架与流程如下：
`用户提问交互` -> `数据模型校验 (Pydantic)` -> `历史记忆写入 (Memory)` -> `生成并返回标准格式`

## 2. 快速开始 (环境搭建)
本项目推荐使用 Python 虚拟环境开发。

在 PowerShell 中执行以下命令准备环境：
```powershell
# 1. 创建虚拟环境
python -m venv .venv

# 2. 激活虚拟环境
.\.venv\Scripts\Activate.ps1

# 3. 安装依赖库
pip install -r requirements.txt
# 4. 启动 FastAPI 服务
uvicorn main:app --reload
```
服务启动完成后，可在浏览器打开可视化API说明书：http://127.0.0.1:8000/docs

## 3.自动化测试
系统已集成基础的自动化测试框架，使用 pytest 进行测试。
在 PowerShell 中执行以下命令运行测试：
```powershell
# 1. 激活虚拟环境
.\.venv\Scripts\Activate.ps1
# 2. 运行测试
pytest tests/
```
测试结果将显示在控制台，确保所有测试通过以验证系统的稳定性和正确性。