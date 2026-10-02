r"""laya 三题型输出结构探针（E7 · 为 runner 校验层设计取证）。

用途：一次性打印 noul / choice / score 三种题型的完整返回结构，
确认字段名与值域，供 run_spike_laya.py 的校验层与指标映射设计使用。

运行（项目 venv）：
    $env:HF_ENDPOINT="https://hf-mirror.com"
    & "E:\毕设_记忆系统\.venv\Scripts\python.exe" -X utf8 `
        "evaluation/spikes/e7_gate/probe_types.py"
"""
import json
import os
import sys

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


def main() -> int:
    from laya import Router

    router = Router()
    state = "客服：您好。用户：我上个月买的产品有质量问题，要求退货退款。"
    questions = {
        "relation": {
            "type": "choice",
            "instructions": "新信息与旧记忆卡片的关系属于哪一类？",
            "criteria": {
                "unrelated": "主题不同，互不影响",
                "coexist": "补充或新增，两者可同时成立",
                "supersede": "改写了旧卡片的同一属性，旧内容已过期",
            },
        },
        "relevance": {
            "type": "score",
            "instructions": "该候选记忆卡片与用户当前问题的相关程度是几档？",
            "criteria": ["不相关", "部分相关", "高度相关"],
        },
        "need_memory": {
            "type": "noul",
            "instructions": "回答该问题是否需要调取用户的历史记忆？",
        },
    }
    result = router.predict(state, questions)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    print("\n== 字段小结 ==")
    for qid, ans in result["answers"].items():
        print(f"{qid}: keys={sorted(ans.keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
