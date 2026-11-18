# 03 · 用 compose 把四个容器连起来

## 目标

上一章你手动做了这些事：

```text
docker network create memory-net
docker run ... --name backend ...
docker run ... --name web ...
```

这一章把它们写成**一个 YAML 文件**，然后用一条命令：

```powershell
docker compose up -d --build
```

把 backend / web / redis / qdrant 四个容器一起起来。

检查点：

- `docker compose ps` 四个服务都是 `healthy`
- 浏览器 8080 能打开、能聊天
- `down` 再 `up`，数据还在

---

## 1. compose 到底替你做了什么

你手写的那些东西，compose 都会自动做：

| 手动 | compose 对应 |
|---|---|
| `docker network create memory-net` | 自动创建一个项目网络 |
| `docker run --name backend` | `services.backend`，容器名自动生成 |
| `--network memory-net` | 默认都在同一个项目网络 |
| `--env-file .env` | `env_file: ../.env` |
| `-p 8081:80` | `ports: "${WEB_PORT:-80}:80"` |
| `-v ...:/app/data` | `volumes:` |
| 先起 redis 再起 backend | `depends_on: condition: service_healthy` |

compose 还解决了「启动顺序」和「谁先健康」的问题——这是手动 `docker run` 很难做好的。

---

## 2. 先看最小骨架

```yaml
name: memory-assistant

services:
  backend:
    build:
      context: ..
      dockerfile: deploy/Dockerfile
    env_file:
      - ../.env
    volumes:
      - ../data:/app/data
    depends_on:
      redis:
        condition: service_healthy

  web:
    build:
      context: ..
      dockerfile: deploy/Dockerfile.web
    ports:
      - "${WEB_PORT:-80}:80"
    depends_on:
      backend:
        condition: service_healthy

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
```

逐段解释：

| 写法 | 含义 |
|---|---|
| `name:` | 项目名。容器名会变成 `memory-assistant-backend-1` 这种 |
| `services:` | 下面每个 key 是一个容器 |
| `build.context: ..` | 构建上下文是**仓库根**（因为 compose 文件在 `deploy/`，`..` 是上一级） |
| `dockerfile: deploy/Dockerfile` | 用哪个 Dockerfile |
| `env_file: ../.env` | 运行时把 `.env` 里的变量注入容器 |
| `volumes: ../data:/app/data` | 宿主机 `data/` ↔ 容器 `/app/data` |
| `ports: "${WEB_PORT:-80}:80"` | `${变量:-默认值}`：有 `WEB_PORT` 就用，没有就 80 |
| `depends_on` + `condition: service_healthy` | 等对方健康再启动 |
| `image:` | 不自己构建，直接拉现成镜像 |

---

## 3. 再加 qdrant、卷和健康检查

完整文件在 `deploy/docker-compose.yml`。重点看这几块：

### 3.1 backend 的环境变量覆盖

```yaml
    environment:
      DATABASE_URL: sqlite+aiosqlite:///./data/app.db
      REDIS_HOST: redis
      REDIS_PORT: "6379"
      QDRANT_URL: http://qdrant:6333
      QDRANT_ENABLED: "true"
```

为什么 `.env` 里已经写了 `REDIS_HOST=localhost`，这里还要覆盖？

因为**容器里的 localhost 指的是容器自己**，不是宿主机，也不是别的容器。容器之间要用**服务名**互访：

- 连 Redis → `redis:6379`
- 连 Qdrant → `qdrant:6333`

`environment:` 的优先级高于 `env_file:`，所以能覆盖掉 `.env` 里的 localhost。

### 3.2 qdrant 和 redis 为什么不写 ports

```yaml
  qdrant:
    image: qdrant/qdrant:v1.13.6
    volumes:
      - qdrant_data:/qdrant/storage
    healthcheck:
      test: ["CMD-SHELL", "bash -c 'exec 3<>/dev/tcp/127.0.0.1/6333'"]
```

没有 `ports:`，意味着**宿主机访问不到**，只有 compose 内网里的 backend 能访问。这是安全设计：数据库/向量库不该暴露到公网。

### 3.3 两种卷

```yaml
    volumes:
      - ../data:/app/data          # bind mount：宿主机目录
      - qdrant_data:/qdrant/storage # named volume：Docker 管理的卷
```

| 类型 | 长什么样 | 适合 |
|---|---|---|
| bind mount | `宿主机路径:容器路径` | 你想直接看到/备份文件（SQLite、日志） |
| named volume | `卷名:容器路径` | 数据库类，不想关心宿主机路径（Qdrant、Redis） |

`docker compose down` 不会删 named volume；只有 `down -v` 才会删。

### 3.4 健康检查

```yaml
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 15s
      timeout: 3s
      retries: 10
```

| 字段 | 含义 |
|---|---|
| `test` | 执行什么命令；返回 0 = 健康 |
| `interval` | 多久查一次 |
| `timeout` | 单次超时 |
| `retries` | 连续失败几次算不健康 |
| `start_period` | 启动宽限期，这期间失败不算 |

没有健康检查，`depends_on` 只能等「容器启动了」，不能等「服务真的能用了」，很容易 502。

---

## 4. 环境变量三件套（最容易混）

| 写法 | 作用对象 | 例子 |
|---|---|---|
| `.env`（仓库根） | 应用运行时读取 | `LLM_API_KEY`、`DATABASE_URL` |
| compose 的 `env_file:` | 把文件里的变量注入**某个容器** | `env_file: ../.env` |
| `docker compose --env-file` | 给 **compose 自己**做变量替换 | `WEB_PORT=8080`、镜像源参数 |

一句话：

- **应用要用的** → `env_file`（`.env`）
- **compose 文件里 `${...}` 要用的** → `--env-file`（`deploy/.env.docker.local`）

`.env` 和 `deploy/.env.docker.local` 都不入库（`.gitignore` 已忽略），里面可能有密钥/本地路径。

---

## 5. 构建参数与国内镜像

`deploy/docker-compose.yml` 里：

```yaml
    build:
      args:
        PYTHON_IMAGE: ${PYTHON_IMAGE:-python:3.13-slim}
        PIP_INDEX_URL: ${PIP_INDEX_URL:-https://pypi.org/simple}
```

对应 `deploy/.env.docker.local`：

```dotenv
PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.13-slim
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
NODE_IMAGE=docker.m.daocloud.io/library/node:22-alpine
NGINX_IMAGE=docker.m.daocloud.io/library/nginx:1.27-alpine
NPM_REGISTRY=https://registry.npmmirror.com
QDRANT_IMAGE=docker.m.daocloud.io/qdrant/qdrant:v1.13.6
REDIS_IMAGE=docker.m.daocloud.io/library/redis:7-alpine
WEB_PORT=8080
```

这样同一份 compose：

- 本地：`--env-file deploy/.env.docker.local` → 用国内镜像源 + 8080；
- CVM：不传这个文件 → 用默认官方源/80，或在 daemon 配腾讯云镜像。

Dockerfile 本身不用改。

---

## 6. 动手

```powershell
cd E:\毕设_记忆系统\AI-Customer-Service

# 构建并启动（第一次会慢）
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml up -d --build

# 看状态
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml ps

# 看后端日志
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml logs -f backend

# 停止（保留数据）
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml down

# 再启动（不重新构建）
docker compose --env-file deploy/.env.docker.local -f deploy/docker-compose.yml up -d --no-build
```

检查点：

```powershell
curl http://localhost:8080/health
```

```json
{"status":"ok"}
```

浏览器打开 http://localhost:8080/ ，发一条消息；然后 `down` + `up`，确认记忆还在。

---

## 7. 常见错误

| 现象 | 原因 | 解决 |
|---|---|---|
| `port is already allocated` | 8080/80 被占用 | 改 `WEB_PORT=8081` |
| `env file ... not found` | `env_file` 相对路径写错 | 它是相对 **compose 文件** 的，不是相对你执行命令的目录 |
| backend 一直 `unhealthy` | 启动慢/依赖连不上 | `logs backend`；健康检查 `start_period` 是否够 |
| web 502 | backend 没健康 | 看 `ps` 和 `logs`；`depends_on` 是否生效 |
| 数据没权限 | 容器 UID 10001 写挂载目录 | CVM 上 `chown -R 10001:10001 data logs` |
| 拉镜像失败 | Docker Hub 不通 | 改 `.env.docker.local` 的镜像前缀，或给 Docker 配镜像加速 |

---

## 8. 检查点

- [ ] `docker compose ps` 四个服务都 `healthy`
- [ ] `curl http://localhost:8080/health` 返回 ok
- [ ] 浏览器 8080 能聊天
- [ ] `down` + `up` 后记忆还在
- [ ] 能说出 `env_file`、`environment`、`--env-file` 的区别
- [ ] 能说出为什么 qdrant/redis 不写 `ports`

全过了再进第 04 章。
