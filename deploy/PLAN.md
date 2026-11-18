# 部署方案：Docker + 腾讯云 CVM

> 生成：2026-11-17 ｜ 代码基线：`main @ 6e60466`（本地领先 `origin/main` 7 个提交）
> 目标：本地 compose 全绿 → CVM 公网可访问 → 部署文档 + 故障清单，作为毕设答辩与求职证据。

## 1. 目标与验收

| 级别 | 验收标准 |
|---|---|
| M1 本地全栈 | `docker compose -f deploy/docker-compose.yml up -d --build` 后，浏览器打开 `http://localhost:${WEB_PORT}` 能聊天、记忆面板可读、Qdrant 有 collection、Redis 有窗口键 |
| M2 测试 | 容器内 `pytest -q` 256 项全绿（测试镜像与运行镜像可分开） |
| M3 CVM | `http://<CVM_IP>/` 公网可访问；`POST /api/v1/chat/` 的 SSE 正常；重启容器后 SQLite 数据还在 |
| M4 文档 | `deploy/部署文档.md` + `deploy/故障清单.md`；从零复现计时 |
| M5 仓库 | 不含 `.env`/密钥；`bench/`、`design/` 私人内容不入库 |

## 2. 运行架构

```
浏览器
  │  http://<host>:80
  ▼
web (nginx:alpine) ── /api/* ──► backend (FastAPI:8000)
  │                                 │
  │ 静态 web/dist                    ├── SQLite  data/app.db
  │                                 ├── Redis   redis:6379（热层窗口，可降级）
  │                                 └── Qdrant  qdrant:6333（向量，可降级）
  │
  └── 外部 API：DeepSeek（生成）、硅基流动（embedding）
```

关键点：前端 `web/src/api.js` 用相对路径 `/api/v1`，生产环境由 nginx 同源反代，**前端代码零改动**。

## 3. 容器与服务

| 服务 | 镜像 | 端口 | 持久化 | 健康检查 |
|---|---|---|---|---|
| backend | 自建 `python:3.13-slim` | 8000（仅内网） | `../data:/app/data`、`../logs:/app/logs` | `GET /health` |
| web | 自建 `nginx:1.27-alpine` | `${WEB_PORT:-80}:80` | 无 | `GET /` |
| qdrant | `qdrant/qdrant`（实现时 pin 版本） | 6333（仅内网） | `qdrant_data` | TCP 6333 |
| redis | `redis:7-alpine` | 6379（仅内网） | `redis_data` | `redis-cli ping` |

- 后端非 root 运行（UID 10001）。
- 8000/6333/6379 一律不映射到宿主机；公网只开 nginx 的 80（或 8080）。
- Qdrant/Redis 不可用时后端自动降级，但 compose 仍用 `service_healthy` 保证启动顺序。

## 4. 文件清单

### 新增

- `deploy/Dockerfile`：后端镜像
- `deploy/Dockerfile.web`：前端多阶段构建
- `deploy/nginx.conf`：静态托管 + `/api` 反代 + SSE 关缓冲
- `deploy/docker-compose.yml`：四服务编排
- `deploy/PLAN.md`：本文件
- `.dockerignore`（仓库根）：排除 `.env`、`.git`、`.venv`、`data/`、`logs/`、`bench/`、`design/`、`web/node_modules` 等

### 修改

- `.gitignore`：把 `docker-compose.yml` 规则改成只忽略根目录 `/docker-compose.yml`，放行 `deploy/`
- `.env.example`：补齐 Docker 部署项（`QDRANT_URL`、`EMBEDDING_*` 等）
- `requirements-prod.txt`：删除本地 embedding 的 torch 栈（见第 6 节）
- `README.md`：加「Docker 部署」一节（阶段 M4 再做）

## 5. 环境变量

`.env`（仓库根，不入库）关键项：

```dotenv
LLM_API_KEY=...
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash

DATABASE_URL=sqlite+aiosqlite:///./data/app.db

REDIS_HOST=redis
REDIS_PORT=6379
REDIS_DB=0

QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=memory_cards
QDRANT_ENABLED=true

EMBEDDING_PROVIDER=api
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-8B
EMBEDDING_DIM=1024
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_API_KEY=...
```

`docker-compose.yml` 用 `env_file: ../.env` 注入，并在 `environment:` 里强制覆盖 `REDIS_HOST=redis`、`QDRANT_URL=http://qdrant:6333`、`DATABASE_URL=...`。

## 6. 依赖裁剪

现状：`requirements-prod.txt` 仍带 `torch / transformers / sentence-transformers / huggingface_hub` 等约 3GB 的本地向量栈；但代码默认 `EMBEDDING_PROVIDER=api`，`sentence-transformers` 只在 `store.py:_embed_via_local()` 里懒加载。

做法：

1. 先删：`torch`、`transformers`、`sentence-transformers`、`huggingface_hub`、`hf-xet`、`safetensors`、`tokenizers`、`scipy`、`scikit-learn`、`joblib`、`threadpoolctl`、`sympy`、`networkx`、`mpmath`、`onnxruntime`、`logger==1.4`。
2. 干净 venv 里 `pip install -r 候选` → `pytest -q`（256 项）→ `pip check`。
3. 再按实际 import 删可选包（`pandas`、`pyarrow`、`GitPython`、`opentelemetry-*` 等大概率不需要）。
4. 以 `pip freeze` 生成最终 pin 清单，确保与 `requirements.txt` 同步。
5. 验收：镜像内 `python -c "import app.main"` 成功；`docker images` 后端镜像 < 500MB。

注意：`EMBEDDING_PROVIDER=local` 的能力保留在代码里，但生产镜像不装 torch；如要本地兜底，另做 `Dockerfile.local-embed` 或本地 venv，不塞进主镜像。

## 7. 本地验证清单

```bash
# 1) 构建并起栈
WEB_PORT=8080 docker compose -f deploy/docker-compose.yml up -d --build

# 2) 健康
curl http://localhost:8080/health
docker compose -f deploy/docker-compose.yml ps

# 3) 前端
# 浏览器打开 http://localhost:8080/，发一条消息，确认 SSE 逐字输出

# 4) 记忆链路
# 说一句「我叫XX，记住」，然后开新会话问「我叫什么」
docker compose -f deploy/docker-compose.yml exec backend \
  python -c "import urllib.request;print(urllib.request.urlopen('http://qdrant:6333/collections').read())"

# 5) 持久化
docker compose -f deploy/docker-compose.yml restart backend
# 确认历史/记忆还在（SQLite + Qdrant volume）

# 6) 降级
docker compose -f deploy/docker-compose.yml stop redis qdrant
# 仍能单轮对话（窗口缺失、向量检索跳过），再 start 恢复
```

## 8. CVM 部署步骤（2核4G，按量计费）

1. 安全组：**22 保留但来源改为本人 IP/32**（不勾就没有 22，SSH 直接连不上）；**80 对 0.0.0.0/0 开放**（要凭密码对外分享，不能限本人 IP）；不开 443/8000/6333/6379。
2. 系统：Ubuntu 22.04/24.04，40G 系统盘；`sudo fallocate -l 2G /swapfile` 并写入 `/etc/fstab`。
3. Docker：安装 `docker.io` + compose 插件；`/etc/docker/daemon.json` 配 `mirror.ccs.tencentyun.com`；`systemctl restart docker`。
4. 代码：`git clone https://github.com/z2123668062/AI-Customer-Service.git`。
5. 配置：`cp .env.example .env`，填 DeepSeek / 硅基流动 key；`chmod 600 .env`。
6. **鉴权密码**：`.htpasswd` 不入库，必须现生成，否则 `docker compose build` 会在 `COPY deploy/.htpasswd` 处直接失败。
   ```bash
   printf 'admin:%s\n' "$(openssl passwd -apr1 '你的密码')" > deploy/.htpasswd
   chmod 600 deploy/.htpasswd
   ```
   账号固定 `admin`，密码自定；改密码只要重跑这条命令再重建 web 镜像。密码存进密码管理器。
7. 目录权限：`mkdir -p data logs && sudo chown -R 10001:10001 data logs`（后端非 root）。
8. 启动：`docker compose -f deploy/docker-compose.yml up -d --build`。
9. 验证：`curl http://127.0.0.1/health`（免鉴权）；浏览器访问 `http://<CVM_IP>/` 应弹登录框，输入 `admin` + 密码后进页面；发消息、记事实、新会话回忆。
10. 记录故障：每条写「报错原文 / 定位层 / 修复 / 验证」。
11. 成本：练完 `docker compose down`，CVM 关机；余额告警 20 元。

## 9. 排期（每天约 2h，每周 5.5 天）

| 阶段 | 时间 | 内容 | 验收 |
|---|---|---|---|
| P0 准备 | 11/18 | 推送 GitHub；确认 DeepSeek / 硅基流动 key 与 CVM 出网；确认 CVM 已开通 | push 成功、两个 API 各调通一次 |
| P1 镜像与编排 | 11/19–11/23 | 依赖裁剪；Dockerfile×2、nginx.conf、compose、.dockerignore、.gitignore | 本地 `compose build` 成功，后端镜像 <500MB |
| P2 本地全栈 | 11/24–11/30 | 起四服务；修 SSE / 权限 / 健康检查；跑 256 测试 | 本地全链路可聊天、记忆可回忆、重启不丢数据 |
| P3 CVM | 12/01–12/10 | 安全组、Docker、swap、clone、部署；逐条记故障 | 公网可访问；故障清单 ≥8 条 |
| P4 文档与复现 | 12/11–12/16 | 从零复现计时；写部署文档 + 故障清单；更新 README | 两份文档定稿 |
| P5 收尾 | 12/17–12/20 | GitHub 公开检查、简历素材卡；打 tag | 仓库无密钥、素材可复述 |
| 缓冲 | 12/21–12/31 | 补坑 / 期末 | — |

## 10. 风险与预案

| 风险 | 预案 |
|---|---|
| CVM 访问不到 DeepSeek / 硅基流动 | P0 先 `curl` 验证；必要时配代理或换可用 API |
| SSE 被 nginx 缓冲 | `proxy_buffering off` + `X-Accel-Buffering: no`；本地先验 |
| 非 root 写 `data/` 权限失败 | CVM 上 `chown 10001:10001 data logs`；本地 Docker Desktop 一般无感 |
| Qdrant 在 2C4G 占内存 | 限制 `mem_limit: 1g`；必要时 `QDRANT_ENABLED=false` 先跑通问答，再开 |
| 镜像构建慢 / 拉取失败 | 腾讯云镜像加速；本机 Docker Desktop 先构建缓存 |
| `.env` 泄漏 | `.dockerignore` 排除、`.gitignore` 已忽略；公开仓库前 `git log --all -- .env` 复查 |
| 无鉴权被公网滥用 | 已实现 nginx Basic Auth（全站，`/health` 豁免）；80 必须对公网开放才能分享，门禁由密码承担；密码用 20 位以上随机，给出后及时更换 |

## 11. 待确认

1. **Docker 文件谁写？** 沿用之前 HANDOFF 的学习目标（你写我审），还是我直接写、你审？
2. CVM 是否已开通、地域/配置？（2 核 4G、上海/北京）
3. 演示期 80 端口对公网开放，还是只限你本人 IP？

---

## 12. 本地验证结果（2026-11-17）

已完成（本机 Docker Desktop）：

- 4 服务起栈，backend / qdrant / redis / web 全部 healthy
- `/health`、Vue 首页、中文 SSE 对话（`status → meta → message → done`）通过
- 显式记忆落库（`source=explicit`）；backend 重启与整栈 down/up 后数据仍在（27 → 27）
- Redis 停机降级：**11s**（修复前 36s）；Qdrant 停机降级：15s
- 256 测试全绿（最小依赖集，无 torch）；backend 镜像 442MB
- 本机前端用预构建 `web/dist` + `deploy/Dockerfile.web.local` 验证；多阶段 `Dockerfile.web` 待 CVM 验证
- 详细故障见 `deploy/故障清单.md`

决策更新：

- Docker 文件由 Agent 编写、用户审阅（已产出）
- CVM 尚未开通：P0/P3 需先开通 2 核 4G 按量实例
- 公网仅放本人 IP
