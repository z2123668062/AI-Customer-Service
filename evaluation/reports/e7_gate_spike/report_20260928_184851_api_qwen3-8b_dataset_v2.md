# E7 判定器 Spike 报告 · API 后端 `Qwen/Qwen3-8B`（20260928_184851）

> 设计：docs/判定器Spike文档.md ｜ prompt=四任务约束模式（与 D19/shortlist 登记一致）。
> 目的：同数据集同 prompt 下换模型，测「延迟 + 准确率」小模型对照。

## 1. 环境登记（实验条件）

- 模型：`Qwen/Qwen3-8B` @ `https://api.siliconflow.cn/v1`
- 判定模式：temperature=0.0 / top_p=0.1 / json_mode=True / extra_body={'enable_thinking': False} / 重试=0 次
- 数据集：`evaluation\spikes\e7_gate\dataset_v2.jsonl`（sha256前12位 `e769b9ebdda0`，共 96 条）
- 运行时间：20260928_184851

## 2. 汇总指标（含延迟——本次实验的焦点）

| 任务 | n | accuracy | eff_acc | parse | failsafe | P50(ms) | P95(ms) | avg(ms) | prompt tok | completion tok |
|---|---|---|---|---|---|---|---|---|---|---|
| 缺页判定（是否需要回源） | 24 | 0.875 | 0.875 | 1.0 | 0.0 | 1878.6 | 2718.3 | 1989.7917 | 231.4167 | 34.9583 |
| 取代判定（无关/并存/取代） | 24 | 0.9583 | 0.9583 | 1.0 | 0.0 | 2146.8 | 2626.5 | 2151.3667 | 266.4583 | 40.25 |
| 回填判定（是否存在因果） | 24 | 0.9167 | 0.9167 | 1.0 | 0.0 | 2083.9 | 2722.2 | 2166.0208 | 243.1667 | 40.5417 |
| 精筛判定（卡片相关档位） | 24 | 0.8333 | 0.8333 | 1.0 | 0.0 | 2005.3 | 3052.4 | 2137.3375 | 257.375 | 36.125 |

## 3. 难度分层准确率（easy→hard 梯度）

| 任务 | easy | medium | hard |
|---|---|---|---|
| 缺页判定（是否需要回源） | 1.0 (n=6) | 0.8889 (n=9) | 0.7778 (n=9) |
| 取代判定（无关/并存/取代） | 1.0 (n=6) | 0.8889 (n=9) | 1.0 (n=9) |
| 回填判定（是否存在因果） | 1.0 (n=6) | 1.0 (n=9) | 0.7778 (n=9) |
| 精筛判定（卡片相关档位） | 1.0 (n=6) | 0.8333 (n=6) | 0.75 (n=12) |

## 4. 任务专项指标（误差方向分解）

- **缺页**：`dangerous_miss`（该回忆没回忆）= 2；`safe_extra`（多回源）= 1
- **取代**：`missed_supersede`（旧卡滞留）= 0；`wrong_supersede`（误取代信息丢失）= 1；混淆对：{'coexist→supersede': 1}
- **回填**：`dangerous_connect`（错连线）= 0；`missed_link`（漏连线）= 2
- **精筛**：二值准确率（相关/不相关）= 0.875；`noise_injection`（低档误升）= 3；`missed_relevant`（相关误降）= 0

## 5. 错例与兜底明细

### 5.1 判定错误（校验通过但判错）

| 任务 | id | 难度 | gold | 判定 | 陷阱 | rationale |
|---|---|---|---|---|---|---|
| page_fault | pf2_08 | medium | yes | no | — | 『我这样』指代进行中的减重行为，需要历史中的目标与进度才能判断。 |
| page_fault | pf2_18 | hard | yes | no | ambiguous_reference | 『去看看』的对象缺失；若为历史提及的地点则需要回源，不确定时按宁多勿漏判需要。 |
| page_fault | pf2_23 | hard | no | yes | write_not_read | 写入路径请求；回应（确认已记录）本身不需要回源历史。 |
| supersede | sup2_12 | medium | coexist | supersede | — | 星座与生日互补，信息一致可并存。 |
| backfill | bf2_17 | hard | yes | no | — | 移民计划驱动语言准备（B→A，跨代际的长因果链）。 |
| backfill | bf2_18 | hard | yes | no | — | 邻居的猫带来的困扰驱动阳台改造（B→A）。 |
| shortlist | sl2_12 | medium | low | high | stale_state | 带明确时间限定的旧住址，对『现在』的问题存在误导风险。 |
| shortlist | sl2_13 | hard | low | mid | intent_mismatch | 出差是工作行程，不提供个人旅行预算的任何有效信息，意图与主题错位。 |
| shortlist | sl2_20 | hard | mid | high | — | 内容偏好可裁剪推荐方向，属背景性辅助（非决定答案）。 |
| shortlist | sl2_21 | hard | low | mid | redundant_info | 编程场景在查询中已给定，该卡为冗余信息，未提供任何增量。 |

（无兜底样本：三层校验 100% 通过）

## 6. 拍板结论

**机器建议：基本达标** —— accuracy ≥ 0.80，需对最低任务补确定性前置或调整文案后复测。
未达冲刺线（0.85）的任务：shortlist=0.8333

> ⚠️ 本节为机器建议，最终拍板与决策记录（Dxx）由人工写入 docs/决策记录.md。
