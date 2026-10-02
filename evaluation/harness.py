"""评测脚手架（占位）：配置登记 + 记忆库快照 + 结果落盘，实验可复现的总控。

职责（实验方案.md §4-5）：
- 配置登记：本次运行的全部开关取值（config 预设）+ 模型版本 + 数据集版本；
- 快照：运行前导出 cards/edges/messages 为 JSONL + hash（提交粒度=快照，不逐条附）；
- 落盘：reports/ 下统一产物目录（指标 JSON + 中间数据 + 配置指纹）。

预期用法：python -m evaluation.harness --preset B5 --dataset datasets/<v1>.jsonl
"""
# TODO(论文路线 P1): 实现；先有 harness 才跑实验（纪律：评测先行，禁止裸跑）。
