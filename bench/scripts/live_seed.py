"""把「关于用户的真实上下文」灌进记忆库，用真实链路跑一轮。

语料来源：这次对话里能确认的事实（项目、评测数字、技术选型、决策、偏好）。
没有编造家庭、健康、住址这类我并不知道的信息。

用法：先跑本脚本灌库 → 重启后端（快表是进程内构建的）→ 跑 live_ask.py 提问。
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta

ROOT = r"E:\毕设_记忆系统\AI-Customer-Service"
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///data/bench/ui.db"
os.environ["EXTRACT_JOURNAL_PATH"] = "data/bench/_ui_journal.log"
os.environ["QDRANT_ENABLED"] = "true"
os.environ["QDRANT_URL"] = "http://localhost:6335"
os.environ["QDRANT_COLLECTION"] = "mem_live"

SESSION_A = "s_live_a"
SESSION_B = "s_live_b"

CORPUS: list[tuple[str, str]] = [
    # —— 会话 A：项目与决策 ——
    (SESSION_A, "我这两天在赶本科毕设，题目是 AI 记忆系统，做结构化记忆卡片那套"),
    (SESSION_A, "后端是 FastAPI，前端今天刚从 Streamlit 换成了 Vue"),
    (SESSION_A, "项目放在 E 盘的「毕设_记忆系统」目录里，主服务叫 AI-Customer-Service"),
    (SESSION_A, "记住，我最讨厌别人绕弯子说话，有话直说"),
    (SESSION_A, "我用 LoCoMo 数据集做评测，一共 199 道题"),
    (SESSION_A, "严格判定下正确率是 53.9%，F1 是 0.414"),
    (SESSION_A, "全上下文那条基线是 55.3%，比我高一点，但它的 token 是我的十几倍"),
    (SESSION_A, "把精筛关掉之后是 44.7%，延迟从 11 秒降到 3.4 秒"),
    (SESSION_A, "我昨天把图传播那套代码回滚了，存在 stash 里"),
    (SESSION_A, "我判断结构化卡片的优势应该在检索侧，不在图连接"),
    (SESSION_A, "多跳题我逐条看过，20 道错题里 15 道是答了但漏，只有 5 道是拒答"),
    (SESSION_A, "所以瓶颈是枚举不全，不是桥接缺失"),
    (SESSION_A, "生成用的是 DeepSeek，embedding 用的是硅基流动"),
    (SESSION_A, "向量库是 Qdrant，跑在 6335 端口，Redis 在 6379"),
    (SESSION_A, "今天下午把记忆库清空了，准备重新养数据"),
    (SESSION_A, "清库的时候踩了个坑，水位文件没删，新消息进不了抽取队列"),
    (SESSION_A, "我时间很紧，答辩之前必须把结论收住"),
    (SESSION_A, "论文只打算主张三件事：规模解耦、时间维度、成本核算"),
    (SESSION_A, "归因幻觉那条我没验出来，测完是 4% 对 4%"),
    (SESSION_A, "现在最缺的是一个能站住的数字，不是新功能"),
    # —— 会话 A 尾部：噪声，把上面的事实挤出窗口 ——
    (SESSION_A, "我每改一次代码就重跑一遍单测，现在是 261 个用例"),
    (SESSION_A, "我用的是 Windows，路径分隔符老出问题"),
    (SESSION_A, "每次跑一遍评测大概花四到五块钱"),
    (SESSION_A, "我不想再加功能了，现在只做收敛"),
    # —— 会话 B：补充事实（跨会话） ——
    (SESSION_B, "补充一下，我那个评测脚本叫 run_eval.py"),
    (SESSION_B, "判定用的是严格模式，回答里必须真的给出答案才算对"),
    (SESSION_B, "我最近在纠结要不要把精筛换成更便宜的方案"),
    (SESSION_B, "卡片的写入成本是每条 5.4 秒，太贵了"),
    (SESSION_B, "库里 293 张卡的时候，实体命中有 165 张，选择性只有 57%"),
    (SESSION_B, "我想过用 BM25 加稠密做混合粗排，还没试"),
    (SESSION_B, "答辩 PPT 还没开始做"),
    (SESSION_B, "我有点担心评委会问「你的图和 RAG 有什么区别」"),
    (SESSION_B, "如果被问到，我打算说结构化的优势在时间可寻址"),
    (SESSION_B, "今天就先到这，明天继续"),
]


async def main() -> None:
    from app.core.database import AsyncSessionLocal, init_db
    from app.models.domain import Message
    from app.services.long_term_memory import consolidator, pool, store

    await init_db()

    base = datetime(2026, 11, 1, 9, 0, 0)
    async with AsyncSessionLocal() as s:
        for i, (sess, text) in enumerate(CORPUS):
            s.add(Message(session_id=sess, role="user", content=text,
                          created_at=base + timedelta(minutes=4 * i)))
        await s.commit()
    print(f"写入 {len(CORPUS)} 条消息")

    async with AsyncSessionLocal() as s:
        await pool.build(s)
        before = len(pool.all_cards()) if hasattr(pool, "all_cards") else -1

    rounds = await consolidator.backfill(drain=True, max_rounds=20)
    print(f"抽取完成，跑了 {rounds} 轮")

    async with AsyncSessionLocal() as s:
        cards = await store.list_cards(s, limit=500)
    print(f"库里现在 {len(cards)} 张卡")
    for c in cards[:40]:
        when = (c.event_time or c.created_at)
        print(f"  [{c.kind:6s}] {when and when.strftime('%m-%d')} {c.content[:60]}")


if __name__ == "__main__":
    asyncio.run(main())
