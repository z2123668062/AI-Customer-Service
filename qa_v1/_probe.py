# qa_v1/_probe.py —— 一次性取证探针（跑完即删）
import os
import sqlite3
import time

paths = [
    r"e:\毕设_记忆系统\AI-Customer-Service\data\app.db",
    r"e:\毕设_记忆系统\AI-Customer-Service\data\qa_v1\app_smoke_A.db",
    r"e:\毕设_记忆系统\AI-Customer-Service\qa_v1\data\app_smoke_A.db",
]
for p in paths:
    print("=" * 70)
    print(p)
    print("exists:", os.path.exists(p))
    if not os.path.exists(p):
        continue
    print("mtime:", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
          "size:", os.path.getsize(p))
    con = sqlite3.connect(p)
    try:
        print("counts: messages=%s cards=%s edges=%s" % (
            con.execute("select count(*) from messages").fetchone()[0],
            con.execute("select count(*) from cards").fetchone()[0],
            con.execute("select count(*) from edges").fetchone()[0],
        ))
        print("-- last 8 messages --")
        for row in con.execute(
                "select id, session_id, role, substr(replace(content, char(10), ' '),1,50) "
                "from messages order by id desc limit 8"):
            print("   ", row)
        print("-- cards status dist --")
        for row in con.execute("select status, count(*) from cards group by status"):
            print("   ", row)
    finally:
        con.close()
