"""离线回放器（占位）：evaluation 的核心入口。

职责（方案 6.1 / 架构文档 5.4）：读取埋点日志（telemetry 落盘的 JSONL）×
标签数据集（datasets/，每条含"期望命中的 memory_id 集合"），在**不改线上代码**的
前提下重算检索指标——即"离线重放每一次检索决策"。

预期接口（阶段 6）：
    python -m evaluation.replay --log <telemetry.jsonl> --dataset datasets/<v1>.jsonl
输出：reports/ 下的指标文件（供 metrics.py 计算、绘图）。

纪律：只消费埋点与公开接口，不 import 线上服务内部状态（架构硬约束 3）。
"""
# TODO(阶段6): 实现回放；输出格式与 metrics.py 的输入约定阶段 6 定稿。
