# 04 · 排障与上 CVM

## 目标

前 3 章你已经在本地把 4 个容器跑起来了。这一章讲两件事：

1. 出问题时，**按什么顺序查**；
2. 怎么把同一套东西放到**腾讯云 CVM** 上。

---

## 1. 排障顺序：ps → logs → exec → curl → config

不要一上来就改代码。按这个顺序，90% 的问题能在前两步定位：

### 第 1 步：看谁没起来

```powershell
docker compose -f deploy/docker-compose.yml ps
```

看 `STATUS`：

- `Up (healthy)`：正常
- `Up (health: starting)`：还在启动，等
- `Up (unhealthy)`：起来了但健康检查不过
- `Restarting` / `Exited`：崩了

### 第 2 步：看日志

```powershell
docker compose -f deploy/docker-compose.yml logs --tail 100 backend
```

重点找：

- `Traceback` / `Error` / `Exception`
- `Field required`（配置缺失）
- `Connection refused` / `Name or service not known`（网络/依赖）
- `Permission denied`（权限）

### 第 3 步：进容器看

```powershell
docker compose -f deploy/docker-compose.yml exec backend sh
```

进去后可以：

```sh
ls /app
ls /app/data
python -c "import app.main; print('ok')"
```

### 第 4 步：从容器内部请求自己

```sh
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read())"
```

能通，说明应用本身没问题，问题在 nginx/端口/网络。

### 第 5 步：才看配置

- `docker compose config`：确认变量替换后的最终配置
- 对比 `.env`、`environment`、`--env-file` 三层

---

## 2. 五个真实案例

### 案例 1：后端刚重启，浏览器 502

- **现象**：`restart backend` 后立刻访问，nginx 返回 502。
- **定位**：后端还在启动（建快表、巡检），nginx 已经把请求转过来了。
- **修复**：等健康检查通过；`up` 用 `depends_on: service_healthy` 保证顺序。
- **结论**：预期行为，不是 bug。`502` 先看 backend 是否 healthy。

### 案例 2：`docker build` 第一步 EOF

- **现象**：拉 `python:3.13-slim` 或 `docker/dockerfile:1` 时报 `EOF`。
- **定位**：Docker Hub 网络不通。
- **修复**：镜像前缀 + `--build-arg`；本机用 daocloud，CVM 配腾讯云镜像加速。
- **验证**：`docker pull` 能拉下来，再 build。

### 案例 3：停 Redis 后对话等 36 秒

- **现象**：HTTP 200，但回复是「我思考得有点久了」。
- **定位**：Redis 客户端没有连接超时；容器停机后连接挂起 12 秒+，多个调用叠加超过 30 秒。
- **修复**：1 秒 socket 超时 + `asyncio.wait_for` 硬超时。
- **验证**：停 Redis，对话 11 秒正常回复。

### 案例 4：curl 发中文返回 400

- **现象**：`curl -d '{"message":"你好"}'` 400；浏览器/前端正常。
- **定位**：Windows 终端把中文按非 UTF-8 传给 curl。
- **修复**：用 Python httpx 或 UTF-8 文件；项目里用 `deploy/smoke_test.py`。
- **结论**：服务端没问题。

### 案例 5：CVM 上 `data/` 写不进去

- **现象**：`PermissionError: [Errno 13] ... /app/data/...`
- **定位**：容器用 UID 10001 跑，宿主机 `data/` 是 root 的。
- **修复**：

```bash
sudo mkdir -p data logs
sudo chown -R 10001:10001 data logs
```

---

## 3. 上 CVM：一步一步

CVM = 腾讯云上的一台 Linux 电脑。本地跑通 ≠ 线上能跑，这一步就是验证「换个干净环境能不能复现」。

### 3.1 开机器

- 地域：上海 / 北京
- 配置：**2 核 4G**
- 系统盘：40G
- 镜像：Ubuntu 22.04 / 24.04
- 计费：按量计费（约 0.6 元/小时）
- 安全组：
  - `22`：只放你本人的公网 IP
  - `80`：只放你本人的公网 IP
  - **不要**开 8000 / 6333 / 6379

### 3.2 装 Docker

SSH 登录后：

```bash
sudo apt update
sudo apt install -y docker.io git
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
# 退出 SSH 重新登录，让 docker 组生效
```

> 如果系统里没有 `docker compose`（v2），按 Docker 官方文档添加 apt 源安装 compose 插件。
> 验证：`docker --version` 和 `docker compose version` 都有输出。

### 3.3 配镜像加速（腾讯云）

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "registry-mirrors": ["https://mirror.ccs.tencentyun.com"]
}
EOF
sudo systemctl restart docker
docker info | grep -A2 "Registry Mirrors"
```

### 3.4 加 2G swap

2C4G 跑 Qdrant 时，加 swap 更稳：

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

### 3.5 拉代码、配环境

```bash
git clone https://github.com/z2123668062/AI-Customer-Service.git
cd AI-Customer-Service
cp .env.example .env
nano .env        # 填 LLM_API_KEY / EMBEDDING_API_KEY 等
chmod 600 .env
mkdir -p data logs
sudo chown -R 10001:10001 data logs
```

### 3.6 启动

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml ps
```

第一次会构建后端镜像、前端多阶段镜像、拉 qdrant/redis，时间取决于网络。

### 3.7 验证

```bash
curl http://127.0.0.1/health
```

浏览器打开 `http://<CVM公网IP>/`，发消息、记一件事、开新会话回忆。

### 3.8 收尾

```bash
docker compose -f deploy/docker-compose.yml down
```

然后到腾讯云控制台**关机**。按量计费，关机不计计算费用（磁盘仍计少量）。

---

## 4. 上线验收清单

- [ ] `docker compose ps` 四个服务 healthy
- [ ] `curl http://127.0.0.1/health` 返回 ok
- [ ] 浏览器公网 IP 能打开页面
- [ ] 能聊天，SSE 逐字输出
- [ ] 说「记住 X」，开新会话能回忆
- [ ] `down` + `up` 后数据还在
- [ ] `docker compose logs` 没有持续报错
- [ ] 安全组只开了 22 + 80（限本人 IP）
- [ ] 记录从零复现耗时

---

## 5. 每条故障怎么记（面试素材）

一个合格的故障记录只要 4 行：

```text
时间：
现象/报错原文：
定位层（网络/镜像/配置/权限/代码）：
修复 + 验证：
```

不要写「解决了」，要写「怎么定位到这一层的」。面试官问的是过程，不是结论。
