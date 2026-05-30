# 第四期纯干货日记：Agent 工具生态对接与流式输出（SSE）改造

## 核心目标

将原本占位模拟的工具调用替换为真实第三方 API（高德天气），同时把后端接口从「等五秒才回一整段」改造为「逐字跳动式返回」的 SSE 流式输出，极大提升前端用户的交互体验。此外，拓展工具生态圈，支持更多真实数据查询。

---

## 核心变革一：从 Mock 数据到真实 API

### 1.0 时代的痛点

最初的 `get_weather` 是一个本地伪造函数，直接返回写死的 `"北京今天晴，25度"`。这在开发联调阶段勉强能用，但带来的问题是：

1. **永远正确的假数据**给了前端一种虚假的安全感，真上线后 API 返回的各种异常状态码（限流、参数错误、服务不可用）完全没有被处理过
2. **系统缺乏真实网络调用的异常处理体感**——超时怎么办、连不上怎么办、返回格式变了怎么办，这些都是线上才会炸的坑

### 实战：高德天气 API 接入

用 `httpx.AsyncClient` 替代了 `requests`，这是整个改造最关键的一步。

```python
async with httpx.AsyncClient() as client:
    resp = await client.get(url, timeout=5.0)
    data = resp.json()
```

这里最核心的认知升级是：**FastAPI 的事件循环是单线程的，任何同步阻塞调用都会堵死所有用户**。如果用 `requests.get(url)`，在等待高德返回的那 5 秒里，其他所有用户的请求——不管是闲聊还是查知识库——全部都得排队等着。而 `httpx.AsyncClient` + `await` 会在发起网络请求后把执行权还给事件循环，等待期间事件循环可以去处理其他请求，等网络响应回来了再回来继续执行。这就是「等待时不占坑」的异步核心原则。

此外，对高德 API 的各种异常做了处理：城市不在映射表时给出清晰提示（而不是抛 KeyError）、网络故障时返回降级文案而非崩溃、API 返回非预期格式时也能兜住。这种「外部依赖必须有容错」的思维是工具调用上线的基本要求。

---

## 核心变革二：SSE 流式输出改造

### 为什么需要 SSE

传统的 HTTP 接口模式是「请求 → 等全部结果算完 → 返回一整段 JSON」。对于大模型应用，这意味着一句回复可能要等 5-10 秒，用户面对一个空白页面在干等，体验极差。

SSE（Server-Sent Events）的协议极其简单：后端以 `text/event-stream` 格式持续往同一个 HTTP 连接里写数据，每行以 `data: ` 开头，用两个换行符分隔每个事件。前端通过 `response.iter_lines()` 逐行读取，每收到一滴水就更新一次 UI。

### 后端实现：StreamingResponse + 生成器

在 chat.py 中，我们用 `StreamingResponse` 包装了一个异步生成器 `stream_generator`：

```python
return StreamingResponse(stream_generator(), media_type="text/event-stream")
```

生成器的核心逻辑是按 event 类型推送不同的事件：

- `event: status` — 告诉前端「正在处理你的请求」，展示一个小状态的暗色提示
- `event: message` — 真正的对话内容，每个字符一条事件
- `event: error` — 某条链路出错了，展示错误信息
- `event: done` — 传输闭环，触发前端做善后处理

四个意图分支（chitchat、kb_qa、tool、complaint）都是先拿到完整的回复文本，然后逐字 yield 出去。为了制造打字机效果，每个字符之间用 `await asyncio.sleep(0.01~0.05)` 控制节奏。这个 sleep 有三重作用：

1. **用户体验**：把「秒出整段」拉成「逐字展现」，模拟人的打字速度，消除等待焦虑
2. **事件循环让出**：每次 sleep 都把执行权交还给事件循环，让它能处理其他用户的请求
3. **渲染节奏控制**：给前端 UI 足够的时间逐帧刷新，而非一次性涌入全部文本

### 前端实现：Streamlit 的 SSE 消费

Streamlit 端通过 `requests.post(url, stream=True)` 开启流式模式，然后用 `response.iter_lines()` 逐行读取 `data: ` 前缀的事件。

前端维护了一个 `message_placeholder` 占位符（`st.empty()`），每收到一个字符就做 `full_reply += content` 然后重新渲染 `message_placeholder.markdown(full_reply + "▌")`。那个闪烁的「▌」光标是模拟打字机的点睛之笔——`done` 事件到达时才去掉光标，定型为最终文本。

事件类型在 `streamlit_app.py` 中分别处理：
- `status` 事件用灰色小字显示在回复上方
- `message` 事件清除状态提示，逐字追加到正文
- `error` 事件用 Streamlit 的红色 `st.error` 框展示
- `done` 事件定型文本，去除光标

---

## 核心变革三：Agent 工具调用的两回合模式

`execute_tool_call` 使用了业界标准的 OpenAI Function Calling 两回合对话模式：

**第一回合**：把用户原话 + `tools_schema`（工具说明书）发给大模型。大模型不直接执行操作，而是返回一个结构化的 `tool_calls` 指令，指明要调用的函数名和参数。

**第二回合**：后端执行完函数后，把工具返回的原始数据（比如高德返回的 JSON）打包成 `role: "tool"` 消息，再次发给大模型。大模型根据这些原始数据组织成人话回复。

为什么要两回合？有三个层面的原因：

- **用户体验**：高德返回的 `{"status":"1","lives":[{"weather":"晴"}]}` 用户看不懂，必须包装成「北京目前天气晴朗，气温 25 度」
- **容错防御**：如果 API 返回 `{"status":"0","info":"INVALID_USER_KEY"}`，大模型会包装成「天气服务暂时不可用，请稍后再试」，而不是把内部错误信号暴露给用户
- **对话连续性**：当一次请求涉及多个工具调用时（比如「查北京天气，再查下杭州天气」），第二回合能把两个结果融合成一句连贯的人话

---

## 扩展工具生态：汇率查询函数

在理解了两回合工具调用模式后，我们新增了 `get_exchange_rate` 函数，接入 `exchangerate-api.com` 的免费汇率接口。

新增一个工具需要三个步骤：

1. **写异步函数**：用 `httpx.AsyncClient` 请求 `https://api.exchangerate-api.com/v4/latest/{base_currency}`，从返回的 `rates` 字典中提取目标币种汇率
2. **写工具说明书**：在 `tools_schema` 中追加一份符合 OpenAI Function Calling 格式的描述，包含函数名、用途说明、参数定义（基准货币和目标货币）
3. **注册映射**：在 `available_functions` 字典中添加 `"get_exchange_rate": get_exchange_rate`

大模型通过说明书知道何时该调用汇率查询、该传什么参数。执行过程和天气查询完全复用同一套两回合流程，体现了工具编排架构的「可插拔」特性。

---

## 踩坑实录

### 坑一：Redis 分布式锁的双重释放

**现象**：第一句话正常，第二句话就报 `Cannot release a lock that's not owned or is already unlocked`。

**根因**：我们在修复锁泄漏问题时，加了一层外层 `try/finally`，但外层 `finally` 的执行时机是在 `return StreamingResponse(...)` 语句返回之前。当 `StreamingResponse` 对象被 Python 返回时，生成器 `stream_generator` 还没开始迭代——外层 `finally` 就把锁还了。等生成器后续开始运行、跑完后又在内层 `finally` 里调用了一次 `lock.release()`，此时锁早就不是它持有的了，直接炸了。

**解决方案**：去掉外层 `finally`，改为在安全审校拒绝路径显式释放，同时在生成器的 `finally` 内层和内层外面各包一层 `try/except LockError`。形成三层防线：内层 finally 负责正常释放 → 外层 except 补刀异常中断路径 → Redis 的 TTL 5 秒自动过期兜底。

### 坑二：语义路由前未写入用户消息

**现象**：无论问什么问题，闲聊分支都返回「不好意思，我刚才走神了，你能再说一遍吗？」

**根因**：`generate_chitchat` 从 Redis 拿历史记录作为上下文发给 GLM-4，但用户的当前消息从未被写入 Redis。第一轮沟通时 Redis 里只有 `system prompt`，没有 `user` 角色的消息。GLM-4 API 要求至少有一条用户消息才会产生回复，没有的时候就报错，被 `except` 捕获后返回了那句「走神了」。

**解决方案**：在语义路由之前、安全审校之后，插入一行 `add_message(request.session_id, role="user", content=request.message)`。这样无论哪个意图分支，都能从 Redis 里拿到完整的对话上下文。

### 坑三：前端 API 地址缺少末尾斜杠触发 307 重定向

**现象**：每次请求日志都先打印一条 `307 Temporary Redirect`，然后请求被 Starlette 内部重定向一次才到达真正的路由。

**根因**：FastAPI 的路由挂在 `/api/v1/chat/`（有斜杠），前端请求 `http://127.0.0.1:8000/api/v1/chat`（无斜杠），Starlette 会先发出 307 重定向再到正确路由，浪费一次 HTTP 往返。

**解决方案**：在 `streamlit_app.py` 中把 `API_URL` 末尾补上斜杠。

---

## 破坏性实验中的认知收获

### 实验一：同步替换异步

在执行破坏性实验时，把 `httpx.AsyncClient` 换成 `httpx.Client`（同步），把 `await client.get()` 换成 `client.get()`（去掉 await），再把 `async def get_weather` 改成 `def get_weather`。表面上虽然前端没有报错，但回复内容变成了「抱歉，目前无法获取北京今天的天气信息」。

这是因为 `execute_tool_call` 里用 `await function_to_call(**function_args)` 去等一个普通函数（非协程）的返回值，Python 的 `await` 一个非可等待对象会抛出 `TypeError`，而这个异常在链路中被吞掉了，导致 `function_response` 没有拿到正确的工具结果，大模型面对空数据返回了降级文案。

更致命的后果是：同步的 `client.get()` 在等待网络响应期间把 FastAPI 的事件循环完全卡死了——所有用户的请求都被堵住，只是单用户测试时察觉不到。

### 实验二：拔掉 asyncio.sleep

删掉所有 `await asyncio.sleep(...)` 后，回复变成了瞬间蹦出来的整段文字。这是因为生成器以 CPU 纳秒级的速度把所有字符一口气 yield 出去，前端虽然还是收到逐条 SSE 事件，但所有事件在几百微秒内全部抵达，Streamlit 的 `message_placeholder` 看到的就是一次性完整文本刷新。打字机效果的「灵魂」不在 SSE 协议本身，而在于字符间那 20 毫秒的节奏控制。

---

## 架构反思与未解决的问题

### 1. 流式锁持有时间过长

SSE 流式输出的生成器跑了多久，Redis 分布式锁就持有多久。在 `asyncio.sleep(0.02)` 的节奏下，一句 200 字的回复需要持有锁约 4 秒。这意味着同一个 session_id 的用户在这 4 秒内无法发送新的请求。如果用户连点发送，会被 409 拦截。

目前的方案是设置锁的 `timeout=5` 秒作为自动过期保险，但理想的做法应该是：拆掉这个分布式锁，改为基于请求队列的串行化机制；或者锁只保护 Redis 的读写操作（微秒级），而不覆盖大模型调用的整个耗时。

### 2. 流式输出尚未覆盖所有链路

目前的流式输出是「先完整拿到回复 → 再逐字 yield」的伪流式。真正的流式应该是大模型那边支持 `stream=True`，后端拿到 token 流就直接往前端透传。但智谱 GLM-4 的 API 支持流式输出，后续可以升级为真正的前后端全链路流式。

### 3. 工具调用的状态管理

当前 `execute_tool_call` 中的两回合对话是在内存中搭建的临时 `messages` 列表完成的，没有走 Redis 持久化。如果工具执行过程中服务器崩溃，这个临时上下文会丢失。后续可以探索把工具调用的中间状态也写入 Redis 或引入消息队列保证持久性。

### 4. 工具函数的异常粒度过粗

目前 `get_weather` 和 `get_exchange_rate` 的异常处理用的是笼统的 `except Exception`，把所有错误类型一视同仁地返回降级文案。更好的做法是区分：网络超时（可重试）、API Key 无效（需管理员介入）、城市编码不存在（提示用户换一种问法）等不同层级的错误，分别给出更精准的用户反馈。
