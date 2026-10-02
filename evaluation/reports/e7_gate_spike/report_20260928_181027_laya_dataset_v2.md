# E7 判定器 Spike 报告 · laya 本地后端（20260928_181027）

> 后端：laya 本地模型（进程内推理）｜设计与流程：docs/判定器Spike文档.md。
> 判定映射已登记于本报告头部；结论需人工最终拍板。
> 调用规格经 3 轮受控校准（calibrate_laya{,2,3}.py）定为 best-effort；官方 benchmark：multilingual 非英语 MASSIVE-intent 0.451 / XNLI 0.731（能力边界佐证）。

## 1. 环境登记（实验条件）

- 后端：`laya(PyPI 0.3.21, multilingual checkpoint, torch CPU)`
- HF 镜像：`https://hf-mirror.com`
- 判定映射：noul(p≥0.5→yes) / choice 取字段 / score argmax→档位
- 数据集：`evaluation\spikes\e7_gate\dataset_v2.jsonl`（sha256前12位 `e769b9ebdda0`，共 96 条，repeat=2）
- 预热耗时：15530.2ms（模型加载+首推理，不计入延迟统计）；运行时间：20260928_181027

## 2. 汇总指标

| 任务 | n | accuracy | effective_acc | parse | failsafe | P50(ms) | P95(ms) | avg_conf | avg_tokens |
|---|---|---|---|---|---|---|---|---|---|
| 缺页判定（是否需要回源） | 24 | 0.5417 | 0.5417 | 1.0 | 0.0 | 387.2 | 487.5 | 0.8324 | 62.625 |
| 取代判定（无关/并存/取代） | 24 | 0.5 | 0.5 | 1.0 | 0.0 | 496.5 | 650.6 | 0.6094 | 93.6667 |
| 回填判定（是否存在因果） | 24 | 0.5 | 0.5 | 1.0 | 0.0 | 392.1 | 537.0 | 0.8611 | 71.5833 |
| 精筛判定（卡片相关档位） | 24 | 0.5417 | 0.5417 | 1.0 | 0.0 | 581.9 | 639.4 | 0.5545 | 107.4583 |

## 3. 难度分层准确率（v2 价值区：easy→hard 梯度）

| 任务 | easy | medium | hard |
|---|---|---|---|
| 缺页判定（是否需要回源） | 0.6667 (n=6) | 0.4444 (n=9) | 0.5556 (n=9) |
| 取代判定（无关/并存/取代） | 0.6667 (n=6) | 0.2222 (n=9) | 0.6667 (n=9) |
| 回填判定（是否存在因果） | 0.5 (n=6) | 0.6667 (n=9) | 0.3333 (n=9) |
| 精筛判定（卡片相关档位） | 1.0 (n=6) | 0.3333 (n=6) | 0.4167 (n=12) |

## 4. 任务专项指标（误差方向分解）

- **缺页**：`dangerous_miss`（该回忆没回忆）= 11；`safe_extra`（多回源）= 0
- **取代**：`missed_supersede`（该取代未取代，旧卡滞留）= 2；`wrong_supersede`（误取代，信息丢失）= 1；混淆对：{'coexist→unrelated': 5, 'supersede→unrelated': 1, 'coexist→supersede': 1, 'unrelated→coexist': 4, 'supersede→coexist': 1}
- **回填**：`dangerous_connect`（错连线）= 11；`missed_link`（漏连线）= 1
- **精筛**：二值准确率（相关/不相关）= 0.7083；`noise_injection`（低档误升，注入噪声风险）= 7；`missed_relevant`（相关误降，漏注入）= 0

## 5. 重复稳定性（laya 确定性验证）

- 总体一致率：**1.0**（2 轮判定完全一致的比例）
- 分任务：{'page_fault': 1.0, 'supersede': 1.0, 'backfill': 1.0, 'shortlist': 1.0}
- 无不一致条目（全部样例在重复轮次间判定稳定）

## 6. 错例与兜底明细

### 6.1 判定错误（校验通过但判错）

| 任务 | id | 难度 | gold | 判定 | 陷阱 | rationale |
|---|---|---|---|---|---|---|
| page_fault | pf2_02 | easy | yes | no | — | 『之前定的目标』指向已存储的个人目标卡。 |
| page_fault | pf2_03 | easy | yes | no | — | 回忆个人健康事实（过敏原），必须回源。 |
| page_fault | pf2_07 | medium | yes | no | — | 『老规矩』是隐式引用，具体指什么只有历史记忆知道。 |
| page_fault | pf2_08 | medium | yes | no | — | 『我这样』指代进行中的减重行为，需要历史中的目标与进度才能判断。 |
| page_fault | pf2_09 | medium | yes | no | — | 总结训练完成情况依赖历史训练记录。 |
| page_fault | pf2_13 | medium | yes | no | — | 需要历史体检记录才能确定时间（判定只关心是否需要回源，不涉及日期计算本身）。 |
| page_fault | pf2_15 | medium | yes | no | — | 『之前那样』强引用过往安排，具体所指需回源确认。 |
| page_fault | pf2_18 | hard | yes | no | ambiguous_reference | 『去看看』的对象缺失；若为历史提及的地点则需要回源，不确定时按宁多勿漏判需要。 |
| page_fault | pf2_20 | hard | yes | no | — | 引用历史事件（上次那笔退款），话题转移词不改变回源需求。 |
| page_fault | pf2_22 | hard | yes | no | — | 『上次商量好的方案』指向跨会话的约定，必须回源。 |
| page_fault | pf2_24 | hard | yes | no | — | 『上次你说的方法』引用历史对话中的助手建议，需要回源。 |
| supersede | sup2_03 | easy | coexist | unrelated | — | 技能新增，两者可同时成立。 |
| supersede | sup2_04 | easy | coexist | unrelated | — | 宠物新增，非替换。 |
| supersede | sup2_09 | medium | supersede | unrelated | — | 同一属性（绑定号码）变更，旧号码失效。 |
| supersede | sup2_10 | medium | coexist | supersede | — | 工作地与居住地是相邻但不同的属性，新信息补充居住地，不等于改写工作地。 |
| supersede | sup2_11 | medium | coexist | unrelated | — | 同一对象的新状态（健康事件），旧卡仍成立。 |
| supersede | sup2_12 | medium | coexist | unrelated | — | 星座与生日互补，信息一致可并存。 |
| supersede | sup2_13 | medium | unrelated | coexist | — | 个人爱好与公司事务无关。 |
| supersede | sup2_14 | medium | unrelated | coexist | — | 体质与兴趣爱好无关。 |
| supersede | sup2_15 | medium | unrelated | coexist | — | 籍贯与阅读行为无关。 |
| supersede | sup2_16 | hard | coexist | unrelated | intention_only | 『考虑』只是意向，尚未落实为事实变更；意向不构成 supersede。 |
| supersede | sup2_17 | hard | supersede | coexist | — | 习惯被明确终止，旧行为状态已失效。 |
| supersede | sup2_22 | hard | unrelated | coexist | — | 作息与工位无关。 |
| backfill | bf2_04 | easy | no | yes | — | 时间相邻但互无因果解释。 |
| backfill | bf2_05 | easy | no | yes | — | 两件无关的事。 |
| backfill | bf2_06 | easy | no | yes | — | 两件无关的事。 |
| backfill | bf2_12 | medium | no | yes | — | 时间相邻但无因果。 |
| backfill | bf2_13 | medium | no | yes | — | 两件事发生在公司但无因果。 |
| backfill | bf2_14 | medium | no | yes | — | 消费主题相邻但无因果。 |
| backfill | bf2_17 | hard | yes | no | — | 移民计划驱动语言准备（B→A，跨代际的长因果链）。 |
| backfill | bf2_20 | hard | no | yes | parallel_same_motivation | 两者是同一动机（护眼）下的并列行为，互为因果的解释都不成立。 |
| backfill | bf2_21 | hard | no | yes | coincidence | 巧合相邻，无因果解释。 |
| backfill | bf2_22 | hard | no | yes | topic_adjacent | 同属车辆主题但年检与座椅无因果。 |
| backfill | bf2_23 | hard | no | yes | coincidence | 无因果解释。 |
| backfill | bf2_24 | hard | no | yes | coincidence | 无因果解释。 |
| shortlist | sl2_08 | medium | mid | high | — | 阅读偏好可裁剪推荐方向，但不是回答所必需（无书单信息）。 |
| shortlist | sl2_09 | medium | low | high | entity_adjacent | 卡片的职业信息与『那个方案』无实质关联，实体相邻但不解决检索需求。 |
| shortlist | sl2_11 | medium | mid | high | — | 人数与历史偏好可裁剪推荐，但餐厅本身信息缺失，属辅助背景。 |
| shortlist | sl2_12 | medium | low | mid | stale_state | 带明确时间限定的旧住址，对『现在』的问题存在误导风险。 |
| shortlist | sl2_13 | hard | low | high | intent_mismatch | 出差是工作行程，不提供个人旅行预算的任何有效信息，意图与主题错位。 |
| shortlist | sl2_16 | hard | low | high | same_anchor_diff_entity | 『上周』时间锚点相同，但周报与基金收益毫无关系。 |
| shortlist | sl2_18 | hard | mid | high | partial_help | 地址习惯是排查快递去向的部分线索，有帮助但不能直接回答。 |
| shortlist | sl2_20 | hard | mid | high | — | 内容偏好可裁剪推荐方向，属背景性辅助（非决定答案）。 |
| shortlist | sl2_21 | hard | low | high | redundant_info | 编程场景在查询中已给定，该卡为冗余信息，未提供任何增量。 |
| shortlist | sl2_22 | hard | low | high | — | 完全无关。 |
| shortlist | sl2_24 | hard | low | high | — | 籍贯信息对面试准备无帮助，跨域无关。 |

（无兜底样本：三层校验 100% 通过）

## 7. 拍板结论

**机器建议：未达标** —— 多任务 accuracy < 0.70，需重新评估。
未达冲刺线（0.85）的任务：page_fault=0.5417, supersede=0.5, backfill=0.5, shortlist=0.5417

> ⚠️ 本节为机器建议，最终拍板与决策记录（Dxx）由人工写入 docs/决策记录.md。
