# 部署入门说明：从「能跑」到「能上线」

> **想动手学：先看 [`tutorial/00-怎么读.md`](tutorial/00-怎么读.md)。**
> 这份 README 是总览/字典，适合查阅，不适合从头读；教程是每章只做一件事的手把手版本。
>
> 读者：第一次接触部署和 Docker 的人。
> 配套文件：`PLAN.md`（计划/排期）、`故障清单.md`（这次真实踩的坑）、同目录下的 Docker 文件。
> 当前状态：本地 4 服务已经跑通，浏览器打开 http://localhost:8080/ 就能用。

---

## 0. 先用一句话说清楚「部署」是什么

**部署 = 把「在我电脑上能跑」变成「在一台干净的服务器上也能跑，而且别人能访问」。**

你现在的代码在 `E:\毕设_记忆系统\AI-Customer-Service`，能跑，是因为这台电脑上已经装好了 Python、Redis、Qdrant、各种依赖。换一台电脑，这些全都没有，项目就起不来。

部署要解决的就是：**把「需要装什么、怎么配、怎么启动」写成一套可重复的东西**，让任何一台机器都能一键复现。

---

## 1. Docker 是什么（三个比喻）

| 概念 | 比喻 | 实际含义 |
|---|---|---|
| **镜像 image** | 一张装好系统和软件的「安装盘」 | 只读模板：Python 3.13 + 依赖 + 你的代码 |
| **容器 container** | 用安装盘启动的一台「迷你电脑」 | 镜像跑起来后的进程沙箱，有独立的文件系统和网络 |
| **Dockerfile** | 制作安装盘的「菜谱」 | 一行行写：用哪个基础镜像、装什么、复制什么、怎么启动 |
| **Compose** | 一桌菜的「菜单 + 上菜顺序」 | 一个 YAML：要起哪几个容器、谁连谁、端口/数据/环境变量 |
| **Volume** | 容器外面的「移动硬盘」 | 容器删了，数据还在（数据库、向量库） |
| **端口映射** | 把容器内的门接到宿主机的门 | 例如容器 80 → 本机 8080 |
| **反向代理** | 门卫/前台 | 外面只看到 nginx 的 80，nginx 把 `/api` 转给后端 |
| **健康检查** | 定期问「你还活着吗」 | Docker 定时请求 `/health`，不健康就不放流量 |
| **环境变量** | 不写死在代码里的配置 | API key、数据库地址、Redis 地址 |

一句话：**Docker 让「运行环境」本身变成可以打包、复制、版本化的东西。**

---

## 2. 这个项目部署后的样子

```
你的浏览器
   │  http://localhost:8080
   ▼
┌─────────────────────┐
│ web（nginx:1.27）   │  静态页面 + 反向代理
│  /        → Vue 页面 │
│  /api/*   → backend  │
│  /health  → backend  │
└─────────┬───────────┘
          │ 容器内网
          ▼
┌─────────────────────┐
│ backend（FastAPI）   │  8000，仅内网
│  对话 / 记忆 / 抽取  │
└───┬───────┬─────┬───┘
    │       │     │
    ▼       ▼     ▼
 SQLite   Redis  Qdrant
 data/    热层   向量索引
 （真相） （可降级）（可降级）
          │
          └──► 外部 API：DeepSeek（生成）、硅基流动（embedding）
```

四个容器的分工：

- **web**：唯一对外的入口。托管 Vue 打包后的静态文件，并把 `/api` 请求转给 backend。
- **backend**：真正干活的 FastAPI。对话、记忆读写、抽取、检索都在这。
- **redis**：热层。存最近 10 轮对话窗口 + 显式记忆的暂存。**挂了会降级，不影响启动。**
- **qdrant**：向量库。语义检索用。**挂了也会降级，不影响启动。**

记忆的「真相」在 SQLite（`data/app.db`）；Redis 和 Qdrant 都是加速层。这是整个降级设计的前提。

---

## 3. 我具体做了什么（按顺序）

### 第 1 步：先搞清楚项目原来怎么跑

读 `README.md`、`app/core/config.py`、`app/main.py`、依赖清单，确认：

- 后端是 FastAPI + Uvicorn；
- 数据库默认 SQLite（不是 MySQL）；
- Redis、Qdrant 都是可选/可降级；
- embedding 默认走 API（硅基流动），**不是本地 torch 模型**；
- 前端是 Vue 3 + Vite，开发期用 Vite 代理。

### 第 2 步：依赖裁剪（这是镜像从 3GB 降到 442MB 的关键）

原来 `requirements-prod.txt` 里带着 `torch / transformers / sentence-transformers / huggingface_hub` 等本地向量栈，约 3GB。但代码默认 `EMBEDDING_PROVIDER=api`，`sentence-transformers` 只在 `store.py:_embed_via_local()` 里**懒加载**，正常运行时根本不碰。

所以我把运行时依赖收敛成 **13 个直接依赖**：

```
fastapi / uvicorn / pydantic / pydantic-settings / python-dotenv
httpx / openai / SQLAlchemy / aiosqlite / greenlet
redis / qdrant-client / loguru
```

验证方式：新建一个干净 venv，只装这 13 个，跑项目自带的 **256 个测试，全绿**。证明这些依赖够用。

> 代价：`EMBEDDING_PROVIDER=local` 的离线兜底能力不在主镜像里了；代码保留，需要时另装 torch 或做单独的 local 镜像。

### 第 3 步：写后端 Dockerfile（`deploy/Dockerfile`）

做了这些事：

1. 基础镜像 `python:3.13-slim`（体积小，和本地 Python 版本一致）；
2. 只装 `requirements-prod.txt`；
3. 创建非 root 用户 `appuser`（UID 10001）——容器被攻破时不会直接拿到 root；
4. 复制 `app/` 代码；
5. 健康检查：定期请求 `http://127.0.0.1:8000/health`；
6. 启动命令：`uvicorn app.main:app --host 0.0.0.0 --port 8000`。

**基础镜像和 pip 源做成了 build args**（`PYTHON_IMAGE`、`PIP_INDEX_URL`），因为国内直连 Docker Hub / PyPI 经常失败；本机用 daocloud + 清华源，CVM 换腾讯云镜像即可，Dockerfile 不用改。

### 第 4 步：写前端 Dockerfile（`deploy/Dockerfile.web`）

标准做法是两阶段构建：

1. `node:22-alpine` 里 `npm ci` + `npm run build`，产出 `web/dist`；
2. 把 `dist` 复制到 `nginx:1.27-alpine`，用 nginx 托管。

但本机从 daocloud 拉 `node:22-alpine` 时卡在 30/56MB 不动，所以本地先用一个**本机专用的** `Dockerfile.web.local`：跳过 node 构建，直接把本机已经 `npm run build` 出来的 `web/dist` 复制进 nginx。这个文件已被 `.gitignore` 忽略，不会入库。

> 多阶段 `Dockerfile.web` 留到 CVM 上验证（那边网络/镜像加速更稳定）。

### 第 5 步：写 nginx 配置（`deploy/nginx.conf`）

nginx 做三件事：

1. **托管静态文件**：Vue 打包后的 `index.html` / `assets/*`；
2. **反向代理**：`/api/` 转发到 `http://backend:8000`，`/health` 也透传；
3. **SSE 特殊处理**：`proxy_buffering off` + `X-Accel-Buffering: no` + 长超时。因为对话是流式输出（一个字一个字推），nginx 一缓冲，打字机效果就变成「等半天一次性出现」。

同源反代还有一个好处：前端请求 `/api/v1` 是同源，不需要 CORS 配置。

### 第 6 步：写 compose（`deploy/docker-compose.yml`）

4 个服务 + 卷 + 健康检查 + 启动顺序：

- 只有 `web` 映射端口：本机 `${WEB_PORT:-80}:80`，本地 `.env.docker.local` 设成 8080；
- backend / qdrant / redis **都不映射到宿主机**，只在 compose 内网互通；
- 数据：
  - `../data:/app/data`：SQLite、抽取水位、telemetry、淘汰/编辑日志；
  - `../logs:/app/logs`：应用日志；
  - `qdrant_data`、`redis_data`：命名卷，容器删了数据还在；
- `depends_on: condition: service_healthy`：qdrant/redis 健康后才起 backend，backend 健康后才起 web；
- `env_file: ../.env`：密钥不进镜像，运行时注入；
- `environment:` 强制覆盖 `REDIS_HOST=redis`、`QDRANT_URL=http://qdrant:6333`、`DATABASE_URL=...`，避免 `.env` 里的 localhost 在容器里指错地方。

### 第 7 步：修 Redis 降级太慢的问题

这是这轮最有价值的修复，也是面试可以讲的故事：

- **现象**：停掉 Redis 后，对话不崩（HTTP 200），但触发 30 秒超时兜底，用户实际等了 36 秒。
- **根因**：Redis 客户端没设连接超时；Docker 网络里容器停机后，连接/DNS 会挂起，实测单次 12.76 秒。
- **修复**：
  1. `config.py` 加 `REDIS_CONNECT_TIMEOUT=1.0`、`REDIS_SOCKET_TIMEOUT=1.0`；
  2. Redis 客户端传入 socket 超时；
  3. 新增 `redis_call()`，所有 Redis 操作都用 `asyncio.wait_for` 做**硬超时**（`short_term_memory` + `explicit` 全部覆盖）。
- **结果**：同样停 Redis，对话 **11 秒**完成，回复正常（“记住了，小羊”），不再是超时兜底。

---

### 第 8 步：给全站加一道唯一鉴权

服务本身没有用户体系，公网一开就等于把 DeepSeek 的 key 借给所有人用。加的是 **HTTP Basic Auth**：nginx 在请求进入后端之前先核对密码，没有用户表，只有一组固定账号。

改了三处：

1. `deploy/nginx.conf`：在 **server 层**写 `auth_basic`（所有 location 默认继承，页面、静态资源、`/api` 一并拦住），只在 `/health` 里 `auth_basic off`。
2. `deploy/.htpasswd`：`账号:apr1 哈希`，不存明文；文件不入库（`.gitignore`）。
3. 两个前端 Dockerfile：`COPY deploy/.htpasswd`，并把 HEALTHCHECK 从 `/` 改成 `/health`。

**撞到的坑**：探针原来打 `/`，加了鉴权后返回 401，busybox 的 wget 对 401 返回退出码 1，容器立刻变 unhealthy（服务其实是好的）。凡是探针/监控/负载均衡走的路径都要显式豁免。详见 `故障清单.md` 第 10 条。

**已知代价**：现在只有 HTTP，没有 HTTPS，密码是 Base64 传输而非加密，公共网络下可被嗅探。自己用够了，要做给别人用的产品必须先上 TLS。

**改密码**：

```bash
printf 'admin:%s\n' "$(openssl passwd -apr1 '新密码')" > deploy/.htpasswd
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml build web
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml up -d --no-build web
```

## 4. 我是怎么验证的（你可以照着复现）

### 4.1 起栈

```powershell
cd E:\毕设_记忆系统\AI-Customer-Service
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml up -d --build
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml ps
```

### 4.2 基础检查

- `curl http://localhost:8080/health` → `{"status":"ok"}`
- 浏览器打开 http://localhost:8080/ → Vue 页面
- 发一条消息 → 看到逐字输出

### 4.3 冒烟测试（中文对话 + 记忆接口）

```powershell
SMOKE_AUTH=admin:你的密码 python deploy/smoke_test.py http://localhost:8080
```

> 不带 `SMOKE_AUTH` 会拿到 401（nginx 拦住了），这本身就是鉴权生效的证明。
> 为什么不用 curl 直接发中文？Windows 的 Git Bash/curl 会把中文按非 UTF-8 传，后端返回 400。这是终端编码问题，不是服务问题。`smoke_test.py` 用 Python httpx 按 UTF-8 发，等价于浏览器行为。

### 4.4 降级测试

```powershell
# 停 Redis，再发消息：应正常回复，不崩
docker compose ... stop redis
python deploy/smoke_test.py http://localhost:8080
docker compose ... start redis

# 停 Qdrant，再发消息：语义路降级，仍正常回复
docker compose ... stop qdrant
python deploy/smoke_test.py http://localhost:8080
docker compose ... start qdrant
```

### 4.5 持久化测试

```powershell
docker compose ... down      # 删容器，保留卷/ bind mount
docker compose ... up -d --no-build
```

实测：down 前 27 张记忆卡，up 后还是 27 张。

### 4.6 验证结果

| 项 | 结果 |
|---|---|
| 4 服务 | backend / qdrant / redis / web 全 healthy |
| `/health` + 首页 | 正常 |
| 中文 SSE 对话 | 200，`status → meta → message → done` 完整 |
| 显式记忆 | `source=explicit` 落库 |
| backend 重启 | 25 张卡仍在 |
| 整栈 down/up | 27 → 27 |
| Redis 停机 | 11s，正常回复 |
| Qdrant 停机 | 15s，正常回复，语义路自动降级 |
| 测试 | 256 passed（最小依赖） |
| 镜像 | backend 442MB；本地 web 73.9MB |

---

## 5. 关键文件逐个讲

| 文件 | 作用 | 你改的时候注意 |
|---|---|---|
| `deploy/Dockerfile` | 后端镜像配方 | 改依赖/启动命令时看它；基础镜像和 pip 源有 build args |
| `deploy/Dockerfile.web` | 前端多阶段镜像（CVM 用） | node 构建 + nginx 托管 |
| `deploy/Dockerfile.web.local` | 本机专用，跳过 node，用预构建 dist | 不入库 |
| `deploy/nginx.conf` | 静态托管 + `/api` 反代 + SSE 关缓冲 | SSE 的 `proxy_buffering off` 不能删 |
| `deploy/docker-compose.yml` | 4 服务编排 | 端口、卷、健康检查、环境变量都在这里 |
| `deploy/.env.docker.local` | 本机构建参数（镜像源、WEB_PORT=8080） | 不入库；CVM 不用它 |
| `.dockerignore` | 构建上下文排除 | 确保 `.env`、`data/`、`logs/`、`bench/`、`design/` 不进镜像 |
| `.env.example` | 环境变量模板 | 新增配置项时同步它（项目约定） |
| `requirements-prod.txt` | 运行时依赖（13 个直接依赖） | 改版本前先跑 256 测试 |
| `deploy/smoke_test.py` | 冒烟测试 | 中文对话 + 记忆接口 |
| `deploy/PLAN.md` | 部署计划/排期 | 阶段、验收标准、风险 |
| `deploy/故障清单.md` | 真实故障记录 | 每条有报错、定位层、修复、验证 |

---

## 6. 你以后怎么自己操作（命令速查）

```powershell
cd E:\毕设_记忆系统\AI-Customer-Service
$DC = "docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml"

# 看状态
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml ps

# 看后端日志（实时）
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml logs -f backend

# 进后端容器（排查用）
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml exec backend sh

# 停止（保留数据）
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml down

# 再启动（不重新构建）
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml up -d --no-build

# 改代码后重建后端并重启
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml build backend
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml up -d --no-build backend
```

---

## 7. 常见故障怎么查（从真实故障里提炼）

| 现象 | 先看哪里 | 常见原因 |
|---|---|---|
| 浏览器 502 | `ps` + `logs backend` | backend 还没起来 / 健康检查没过 / 启动报错 |
| 端口被占用 | 改 `WEB_PORT` | 本机 80 被别的软件占了；本地用 8080 |
| 镜像拉取失败 | Docker Hub 连通性 | 国内网络；用镜像前缀 + build args |
| `data/` 写入 Permission denied | 容器用户 UID 10001 | CVM 上 `chown -R 10001:10001 data logs` |
| curl 发中文 400 | 终端编码 | 用 `smoke_test.py` 或 UTF-8 文件 |
| 停 Redis 后很慢 | 超时配置 | 已修：1s socket + `asyncio.wait_for` 硬超时 |
| 页面能开但聊天没反应 | `logs backend` | LLM key / 网络 / 超时；看 Trace 日志 |

**排查顺序口诀**：先 `ps` 看谁没起来 → 再 `logs` 看报错 → 再进容器 `exec` 验证网络/文件 → 最后才改代码。

---

## 8. 上 CVM 还要做什么

CVM = 腾讯云上的一台 Linux 电脑。本地跑通不等于线上能跑，P3 要做的就是把同一套 compose 放到 CVM 上，并记录所有新撞的坑。

需要你做的准备：

1. 开通 **2 核 4G、Ubuntu 22.04/24.04、40G 系统盘、上海/北京、按量计费**；
2. 安全组只放 **22（你本人 IP）** 和 **80（你本人 IP）**；不要开 8000/6333/6379；
3. 把公网 IP 和 SSH 方式给我。

我要做的：

1. 装 Docker + compose，配腾讯云镜像加速；
2. 加 2G swap（2C4G 跑 Qdrant 更稳）；
3. `git clone` → `cp .env.example .env` → 填 key → `docker compose up -d --build`；
4. 验证公网 `/health`、页面、SSE、记忆；
5. 记录故障 + 从零复现计时；
6. 产出 `部署文档.md` + `故障清单.md`。

成本护栏：按量约 0.6 元/小时，练完 `down` + 关机；余额告警 20 元。

---

## 9. 术语表

| 术语 | 大白话 |
|---|---|
| 镜像 image | 装好环境的「安装盘」，只读 |
| 容器 container | 用镜像启动的一台小机器 |
| Dockerfile | 制作镜像的菜谱 |
| Compose | 多个容器的编排菜单 |
| Volume | 容器外面的硬盘，删容器不丢数据 |
| bind mount | 把宿主机的一个目录直接挂进容器 |
| 端口映射 | 宿主机的门 → 容器的门 |
| 反向代理 | 门卫：外面只看到 nginx，它把请求转给内部服务 |
| 健康检查 | Docker 定时问服务「还活着吗」 |
| 环境变量 | 不写死在代码里的配置（密钥/地址） |
| SSE | 服务器把回答一个字一个字推给浏览器 |
| 非 root | 容器里不用管理员账号跑应用 |
| 多阶段构建 | 一个 Dockerfile 里先用 node 构建，再把产物交给 nginx |
| 软依赖 | 挂了也能降级运行，不阻塞主流程 |
| 降级 | 出问题时关掉非核心能力，保证主流程可用 |

---

## 10. 现在建议你做什么

1. **先玩一下**：浏览器打开 http://localhost:8080/，发几句话，看看记忆面板。
2. **读这三份**：
   - 本文件（部署是什么、我做了什么）
   - `deploy/PLAN.md`（计划、验收、排期）
   - `deploy/故障清单.md`（真实踩的坑，面试素材）
3. **审 4 个核心文件**：`Dockerfile`、`Dockerfile.web`、`nginx.conf`、`docker-compose.yml`。看不懂的地方直接问我，我用大白话解释。
4. 审完再提交推送；想上 CVM 时再开通，我陪你做 P3。
