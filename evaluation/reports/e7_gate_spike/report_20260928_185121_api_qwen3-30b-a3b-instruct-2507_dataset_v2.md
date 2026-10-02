# E7 判定器 Spike 报告 · API 后端 `Qwen/Qwen3-30B-A3B-Instruct-2507`（20260928_185121）

> 设计：docs/判定器Spike文档.md ｜ prompt=四任务约束模式（与 D19/shortlist 登记一致）。
> 目的：同数据集同 prompt 下换模型，测「延迟 + 准确率」小模型对照。

## 1. 环境登记（实验条件）

- 模型：`Qwen/Qwen3-30B-A3B-Instruct-2507` @ `https://api.siliconflow.cn/v1`
- 判定模式：temperature=0.0 / top_p=0.1 / json_mode=True / extra_body=无 / 重试=0 次
- 数据集：`evaluation\spikes\e7_gate\dataset_v2.jsonl`（sha256前12位 `e769b9ebdda0`，共 96 条）
- 运行时间：20260928_185121

## 2. 汇总指标（含延迟——本次实验的焦点）

| 任务 | n | accuracy | eff_acc | parse | failsafe | P50(ms) | P95(ms) | avg(ms) | prompt tok | completion tok |
|---|---|---|---|---|---|---|---|---|---|---|
| 缺页判定（是否需要回源） | 24 | 0.9167 | 0.9167 | 1.0 | 0.0 | 1431.6 | 2201.6 | 1465.6417 | 227.4167 | 32.7083 |
| 取代判定（无关/并存/取代） | 24 | 0.9583 | 0.9583 | 1.0 | 0.0 | 1451.1 | 1883.8 | 1408.8917 | 262.4583 | 37.625 |
| 回填判定（是否存在因果） | 24 | 0.9583 | 0.9583 | 1.0 | 0.0 | 1680.0 | 2027.6 | 1726.3417 | 239.1667 | 40.3333 |
| 精筛判定（卡片相关档位） | 24 | 0.7917 | 0.7917 | 1.0 | 0.0 | 1437.8 | 2071.9 | 1550.05 | 253.375 | 35.4583 |

## 3. 难度分层准确率（easy→hard 梯度）

| 任务 | easy | medium | hard |
|---|---|---|---|
| 缺页判定（是否需要回源） | 1.0 (n=6) | 1.0 (n=9) | 0.7778 (n=9) |
| 取代判定（无关/并存/取代） | 1.0 (n=6) | 1.0 (n=9) | 0.8889 (n=9) |
| 回填判定（是否存在因果） | 1.0 (n=6) | 0.8889 (n=9) | 1.0 (n=9) |
| 精筛判定（卡片相关档位） | 0.8333 (n=6) | 0.8333 (n=6) | 0.75 (n=12) |

## 4. 任务专项指标（误差方向分解）

- **缺页**：`dangerous_miss`（该回忆没回忆）= 1；`safe_extra`（多回源）= 1
- **取代**：`missed_supersede`（旧卡滞留）= 0；`wrong_supersede`（误取代信息丢失）= 1；混淆对：{'coexist→supersede': 1}
- **回填**：`dangerous_connect`（错连线）= 0；`missed_link`（漏连线）= 1
- **精筛**：二值准确率（相关/不相关）= 0.9167；`noise_injection`（低档误升）= 2；`missed_relevant`（相关误降）= 0

## 5. 错例与兜底明细

### 5.1 判定错误（校验通过但判错）

| 任务 | id | 难度 | gold | 判定 | 陷阱 | rationale |
|---|---|---|---|---|---|---|
| page_fault | pf2_18 | hard | yes | no | ambiguous_reference | 『去看看』的对象缺失；若为历史提及的地点则需要回源，不确定时按宁多勿漏判需要。 |
| page_fault | pf2_23 | hard | no | yes | write_not_read | 写入路径请求；回应（确认已记录）本身不需要回源历史。 |
| supersede | sup2_16 | hard | coexist | supersede | intention_only | 『考虑』只是意向，尚未落实为事实变更；意向不构成 supersede。 |
| backfill | bf2_15 | medium | yes | no | — | 新业务需要正版授权（B→A）。 |
| shortlist | sl2_02 | easy | high | mid | — | 宠物信息直接决定推荐方向（犬种、活动偏好）。 |
| shortlist | sl2_08 | medium | mid | high | — | 阅读偏好可裁剪推荐方向，但不是回答所必需（无书单信息）。 |
| shortlist | sl2_13 | hard | low | mid | intent_mismatch | 出差是工作行程，不提供个人旅行预算的任何有效信息，意图与主题错位。 |
| shortlist | sl2_20 | hard | mid | high | — | 内容偏好可裁剪推荐方向，属背景性辅助（非决定答案）。 |
| shortlist | sl2_21 | hard | low | mid | redundant_info | 编程场景在查询中已给定，该卡为冗余信息，未提供任何增量。 |

（无兜底样本：三层校验 100% 通过）

## 6. 拍板结论

**机器建议：部分降级** —— 存在任务 accuracy < 0.70，该判断点需补强或换档。
未达冲刺线（0.85）的任务：shortlist=0.7917

> ⚠️ 本节为机器建议，最终拍板与决策记录（Dxx）由人工写入 docs/决策记录.md。
