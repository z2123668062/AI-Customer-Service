# 02 · 把前端和 nginx 装进容器

## 目标

这一章做两件事：

1. 把 Vue 前端编译成静态文件；
2. 用 nginx 托管它，并把 `/api` 转发给上一章的后端容器。

检查点：

- 浏览器打开 `http://localhost:8081/` 能看到 Vue 页面
- `curl http://localhost:8081/health` 返回 `{"status":"ok"}`
- 页面上发一条消息，能正常回复

> 教程里用 **8081**，是为了避开你已经在跑的 compose 栈（它占用了 8080）。

---

## 1. 前端为什么要「构建」

Vue 源码（`.vue`、`.js`）浏览器不能直接跑。`npm run build` 会把它们编译成：

```
web/dist/
├── index.html
└── assets/
    ├── index-xxxx.js
    └── index-xxxx.css
```

浏览器只认这些静态文件。**生产环境不需要 Node，只需要一个能发文件的 web 服务器。**

先构建：

```powershell
cd web
npm run build
cd ..
```

> 如果 `node_modules` 不存在，先 `npm ci`（或 `npm install`）。

构建完确认 `web/dist/index.html` 存在。

---

## 2. 为什么需要 nginx

三个理由：

1. **发静态文件**：浏览器请求 `/`，nginx 把 `index.html` 发回去。
2. **反向代理**：浏览器请求 `/api/v1/chat`，nginx 转给后端容器 `backend:8000`。
3. **SSE 不缓冲**：对话是流式输出，nginx 默认会攒一段再发，必须关掉缓冲。

---

## 3. 最小 nginx 配置，逐行看

```nginx
server {
    listen 80;
    root /usr/share/nginx/html;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location = /health {
        proxy_pass http://backend:8000/health;
    }

    location /api/ {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 3600s;
        add_header X-Accel-Buffering no;
    }
}
```

| 行 | 含义 |
|---|---|
| `listen 80` | 容器内监听 80 端口 |
| `root /usr/share/nginx/html` | 静态文件根目录 |
| `index index.html` | 访问 `/` 时默认发 index.html |
| `try_files $uri $uri/ /index.html` | Vue Router 用 history 模式时，刷新 `/memory` 这种路径不会 404，会回退到 index.html |
| `location = /health` | 精确匹配 `/health`，转给后端 `/health` |
| `location /api/` | 所有 `/api/` 开头的请求转给后端 |
| `proxy_pass http://backend:8000` | `backend` 是**容器名/服务名**，不是 localhost |
| `proxy_set_header Host $host` | 把原始 Host 传给后端 |
| `proxy_set_header Connection ""` | 保持长连接 |
| `proxy_buffering off` | **SSE 关键**：不攒数据，来一点发一点 |
| `proxy_read_timeout 3600s` | 流式对话可能很久，别 60 秒就断 |
| `X-Accel-Buffering: no` | 再给一层保险，告诉上游不要缓冲 |

---

## 4. 手动跑两个容器，理解「容器网络」

先创建一张「内网」：

```powershell
docker network create memory-net
```

启动后端（容器名 `backend`）：

```powershell
docker run -d `
  --name backend `
  --network memory-net `
  --env-file .env `
  -p 8000:8000 `
  memory-backend:learn
```

启动 nginx，挂载配置和静态文件：

```powershell
docker run -d `
  --name web `
  --network memory-net `
  -p 8081:80 `
  -v "E:\毕设_记忆系统\AI-Customer-Service\deploy\nginx.conf:/etc/nginx/conf.d/default.conf:ro" `
  -v "E:\毕设_记忆系统\AI-Customer-Service\web\dist:/usr/share/nginx/html:ro" `
  nginx:1.27-alpine
```

关键点：

- 两个容器都在 `memory-net` 上，所以 nginx 里写 `http://backend:8000` 能解析到后端容器。
  **同一网络内，容器名就是 DNS。**
- `-v 宿主机路径:容器路径:ro`：把宿主机的文件/目录挂进容器，`:ro` 只读。
- `-p 8081:80`：宿主机 8081 → nginx 容器的 80。

检查：

```powershell
curl http://localhost:8081/health
```

浏览器打开 http://localhost:8081/ ，发一条消息。应该能看到逐字输出。

> 如果页面能开但发消息 502：`docker ps` 看 `backend` 是否在跑；`docker logs backend --tail 50` 看后端报错。

---

## 5. 把这两步写成 Dockerfile

### 版本 A：本机简单版（用预构建的 dist）

```dockerfile
FROM nginx:1.27-alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY web/dist /usr/share/nginx/html
```

构建：

```powershell
docker build -f deploy/Dockerfile.web.local -t memory-web:dev .
```

这就是项目里的 `deploy/Dockerfile.web.local`。它要求 `web/dist` 已经构建好，并且 `.dockerignore` 不能排除 `web/dist`。

### 版本 B：标准多阶段（CVM 用）

```dockerfile
# 第一阶段：用 node 构建
FROM node:22-alpine AS build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# 第二阶段：只保留 nginx + 产物
FROM nginx:1.27-alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
```

逐行解释：

| 行 | 含义 |
|---|---|
| `FROM node:22-alpine AS build` | 第一阶段命名为 `build`，里面有 node/npm |
| `WORKDIR /web` | 第一阶段的工作目录 |
| `COPY package*.json` + `npm ci` | 先只复制依赖清单，利用缓存；`npm ci` 按 lock 文件精确安装 |
| `COPY web/ ./` + `npm run build` | 复制源码并构建出 `dist` |
| `FROM nginx:1.27-alpine` | 第二阶段从干净的 nginx 开始 |
| `COPY --from=build /web/dist ...` | **从第一阶段**复制产物，而不是从宿主机 |
| 最终镜像 | 只有 nginx + 静态文件，没有 node/npm，体积小很多 |

构建（国内网络）：

```powershell
docker build -f deploy/Dockerfile.web `
  --build-arg NODE_IMAGE=docker.m.daocloud.io/library/node:22-alpine `
  --build-arg NGINX_IMAGE=docker.m.daocloud.io/library/nginx:1.27-alpine `
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com `
  -t memory-web:multi .
```

> 如果 `node:22-alpine` 拉取卡住（本机就遇到了），先用版本 A 跑通；多阶段版本到 CVM 上验证。

---

## 6. 常见错误

| 报错/现象 | 原因 | 解决 |
|---|---|---|
| 页面 404 | `root` 或 `try_files` 不对，或 dist 没挂对 | 确认容器里 `/usr/share/nginx/html/index.html` 存在 |
| 页面能开，`/api` 502 | `backend` 名字不对/不在同一网络/后端没起 | `docker ps`；确认两个容器都在 `memory-net` |
| 聊天不流式，等半天一次性出来 | nginx 缓冲了 | 确认 `proxy_buffering off` |
| `npm ci` 失败 | 没有 `package-lock.json` 或不同步 | 用 `npm install` 生成 lock；或本机先 `npm run build` |
| 端口 8081 被占用 | 本机已有服务 | 换一个端口，如 8082 |
| 挂载路径含中文报错 | 路径没加引号 | 用双引号把 `-v` 整段包起来 |

---

## 7. 检查点

- [ ] `web/dist/index.html` 存在
- [ ] 两个容器都在 `memory-net`
- [ ] `curl http://localhost:8081/health` 返回 `{"status":"ok"}`
- [ ] 浏览器 8081 能打开页面并聊天
- [ ] 能说出 `proxy_pass http://backend:8000` 里的 `backend` 是什么
- [ ] 能说出 `COPY --from=build` 的作用

全过了再进第 03 章。

---

## 8. 清理

```powershell
docker rm -f web backend
docker network rm memory-net
```
