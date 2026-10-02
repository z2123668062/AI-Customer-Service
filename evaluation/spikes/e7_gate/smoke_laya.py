r"""laya pip 包安装冒烟测试（E7 判定器 · 本地后端可行性验证）。

用途：验证 `pip install laya` 路线可用性——首次运行会自动下载模型 checkpoint
（走 HF 镜像 hf-mirror.com），随后跑一条中文判定样例并打印耗时与结果。

运行（项目 venv）：
    $env:HF_ENDPOINT="https://hf-mirror.com"
    & "E:\毕设_记忆系统\.venv\Scripts\python.exe" -X utf8 `
        "evaluation/spikes/e7_gate/smoke_laya.py"
"""
import os
import sys
import time

# HF 镜像必须在 import laya / huggingface_hub 之前生效
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


def main() -> int:
    from laya import Router

    t0 = time.perf_counter()
    router = Router()
    t_init = time.perf_counter() - t0
    print(f"[smoke] Router 初始化耗时 {t_init:.2f}s")

    state = (
        "用户：我上个月的积分一直没到账，能帮我补一下吗？"
        "客服：您好，我帮您查一下。"
    )
    questions = {
        "page_fault": {
            "type": "noul",
            "instructions": "回答该问题是否需要调取用户的历史记忆（如往期积分纠纷记录、账户变更记录）？",
        },
    }

    t0 = time.perf_counter()
    router.predict(state, questions)
    t_warm = time.perf_counter() - t0
    print(f"[smoke] 预热（首次加载模型+推理）耗时 {t_warm:.2f}s")

    t0 = time.perf_counter()
    result = router.predict(state, questions)
    t_pred = time.perf_counter() - t0
    print(f"[smoke] 热推理（第2次 predict）耗时 {t_pred:.2f}s")
    print(f"[smoke] routing.model = {result.get('routing', {}).get('model')}")
    print(f"[smoke] answers = {result.get('answers')}")
    print(f"[smoke] full = {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
