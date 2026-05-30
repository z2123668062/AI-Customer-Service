# 第五期纯干货日记：鉴权防刷、用户体系与多会话管理

## 核心目标

将系统从「裸奔状态」升级为「持证上岗」的商业底座——引入完整的用户认证体系、JWT 鉴权、多会话管理，以及在用户维度的限流防刷机制。

---

## 核心变革一：用户认证体系

### 1.0 时代的痛点

之前的系统完全没有用户概念。`session_id` 由前端随机生成（`user_ee510c02`），不绑定任何用户身份。Redis 中聊天记忆的 key 是 `chat:session:{session_id}`——扁平的命名空间，没有用户隔离。MySQL 的 `ChatRecord` 表也只有 `session_id`，查不到某个用户的全部历史。系统的所有接口完全对公网开放，任何人都能直接调用。

### 用户数据模型

新增 `User` ORM 模型，包含 `id`、`username`、`password_hash`、`phone`、`created_at` 五个字段。同时 `ChatRecord` 表增加了 `user_id` 外键，每条聊天记录都可以追溯到具体用户。

密码用 bcrypt 哈希存储。bcrypt 是一种加盐哈希算法——`gensalt()` 在底层生成一段随机字符串（盐）混入密码一起哈希，即使两个用户密码相同，存储的哈希值也不一样，彩虹表攻击无效。

### 注册登录与 JWT

后端提供三个核心接口：

- `POST /api/v1/auth/register`：校验用户名是否已存在，密码经 bcrypt 哈希后入库，签发 JWT Token 并返回
- `POST /api/v1/auth/login`：按用户名查库，用 `checkpw` 比对密码哈希，通过后签发 Token
- `GET /api/v1/auth/me`（未实装但预留）：根据 Token 返回用户信息

JWT 用 HS256 算法加密，密钥直接复用智谱 API Key 的反转字符串，省去额外配置。Token 有效期为 24 小时。JWT 的无状态特性意味着服务器不存 Token——签发时加密 payload，验证时解密 payload，不需要每次都查 Redis。

### 手机验证码登录（Mock 实现）

完整实现了手机验证码登录流程但 SMS 发送环节用控制台打印替代：

- `send_sms_code`：生成 6 位随机验证码，存入 Redis 设置 300 秒过期，控制台打印
- `verify_sms_code`：对比用户输入的验证码和 Redis 中存储的是否一致，校验后删除
- `login_or_register_by_phone`：手机号已注册则直接登录，未注册则自动创建用户

这套代码的完整逻辑和真实短信服务完全一致，以后申请到短信服务商资质后，只需要替换 `send_sms_code` 中调用第三方 SDK 的那一行即可。

---

## 核心变革二：多会话管理

### 为什么要引入多会话

用户和 AI 客服的对话可能涉及多个主题——比如用户想同时处理「报销咨询」和「物流查询」两个话题，如果只有一个会话上下文，两个话题的内容会混在一起，大模型的上下文窗口会被无关信息污染，影响回答质量。多会话让用户可以为不同主题开独立的对话框，互不干扰。

### 数据结构设计

引入两层 Redis 数据结构：

第一层是**会话元数据**，存在 Redis Hash `chat:user:{user_id}:sessions` 中。Hash 的每个小 key 是 `session_id`，小 value 是包含标题和创建时间的 JSON。用 Hash 而非 List，是因为大部分操作是按 `session_id` 做单条增删改查，Hash 的 O(1) 性能远优于 List 的 O(n)。

第二层是**聊天记忆**，存在 Redis List `chat:user:{user_id}:session:{session_id}` 中。这实际上是 `memory.py` 中已有逻辑的扩展——key 的命名空间从 `chat:session:{sid}` 改为 `chat:user:{uid}:session:{sid}`，在用户维度做了隔离。

### 服务层实现

`session_service.py` 提供了四个操作：

- `create_session`：生成随机 session_id，将标题和创建时间写入 Redis Hash，只占位不预占资源
- `list_sessions`：用 `hgetall` 一次性取出当前用户的所有会话，按创建时间倒序排列
- `update_session_title`：按 session_id 找到对应元数据，反序列化后修改标题再写回
- `delete_session`：同时清理聊天记忆 List 和会话元数据 Hash，避免产生孤立 Redis key

---

## 核心变革三：限流防刷

### 为什么需要限流

Redis 分布式锁只解决了「同一个会话的连点问题」——锁 key 是 `lock:order:{user_id}:{session_id}`，不同 session 之间互不冲突。一个恶意用户可以同时开多个会话轮流发送，一样能把大模型 API 额度刷爆。需要一个**用户维度的全局计数器**来做兜底。

### 固定窗口计数器

用 Redis 的原子递增命令 `INCR` 实现，代码逻辑极简：

```python
key = f"ratelimit:chat:{user_id}:{当前分钟字符串}"
count = redis_client.incr(key)
if count == 1:
    redis_client.expire(key, 60)
return count <= 20
```

第一次 `incr` 时 Redis 自动创建 key 设值为 0 再加 1 返回 1，同时设置 60 秒过期。后续每次 `incr` 只是递增，不重置过期时间。60 秒后 key 自动消失，下一分钟的第一个请求重新从 1 开始计数。

### 限流规则

- 聊天接口：每用户每分钟最多 20 次请求，超限返回 `429 Too Many Requests`
- 创建会话：每用户每分钟最多 5 次，超限返回 `429`
- 匿名用户统一按 `user_id=0` 计数

限流检查放在锁获取之后、安全审校之前——既不会在锁之前浪费锁资源，也不会让安全审校做了无用功。

---

## 踩坑实录

### domain.py 中 phone 字段 NOT NULL 导致注册失败

注册接口 `register_user` 只接收用户名和密码，不传 `phone`。但 `phone` 字段被设为 `nullable=False`，数据库报 `Column 'phone' cannot be null`。修复方案：改为 `nullable=True`，并在注册表单 UI 中不展示手机号输入框。用户注册时可选填手机号，不填也能注册。

### 天气高德 API Key 失效

高德的免费测试 Key 被多人共用后触发限流，天气查询进入降级分支返回「抱歉，无法获取天气信息」。这是外部依赖的常见问题——免费 API 没有 SLA 保障，生产环境需要切换到有付费协议的稳定服务商。

### 意图路由缺失汇率示例

`router_service.py` 的 system_prompt 中，`tool` 分类的示例只包含「查天气、看订单、退款进度、发邮件、创建日程」，没有关于「汇率/货币/兑换」的例子。导致大模型把「美元相对人民币的汇率」误判为 `kb_qa`。修复方案是在 `tool` 分类描述中加上「查汇率、货币兑换」，并补充一条示例。

---

## 架构反思

### 1. JWT 无状态与持久登录的矛盾

当前 Token 24 小时有效，用户关掉浏览器再打开需要重新登录。如果要实现「关掉浏览器再打开不用登录」，需要引入 `localStorage` 持久化 Token。但 JWT 的无状态特性决定了「让 Token 提前失效」需要引入 Redis 黑名单机制——这就回到了有状态。工业界通用的做法是双 Token 机制：短有效期 Access Token（15 分钟）配合长有效期 Refresh Token（7 天，存 Redis 可主动失效），既能享受无状态查询的快速，又能实现主动登出。目前我们用的是单 Token 简化方案，后续按需升级。

### 2. 只限制了聊天和创建会话

当前限流只覆盖了 `chat` 和 `create_session` 两个接口，但 `list_sessions`、`update_session_title`、`delete_session` 等接口没有限流。这些接口的开销远小于大模型调用，被刷的成本不高，但如果要做完整的防护，可以统一加一个中间件层做全局限流，而不是在每个路由里手动插入。

### 3. 固定窗口的边界问题

固定窗口计数器有一个经典的边界问题：用户在 14:30:59 发了 20 条消息达到上限，紧接着在 14:31:00（下一秒）又可以发 20 条，因为在不同的分钟窗口里。这意味着用户可以在 2 秒内发出 40 条请求。更严格的方案是滑动窗口（用 Redis ZSET 记录精确时间戳），但实现复杂度大幅上升。对于每分钟 20 次的限制级别，固定窗口的误报率在实际使用中可以接受。
