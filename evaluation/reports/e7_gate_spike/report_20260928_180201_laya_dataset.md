# E7 判定器 Spike 报告 · laya 本地后端（20260928_180201）

> 后端：laya 本地模型（进程内推理）｜设计与流程：docs/判定器Spike文档.md。
> 判定映射已登记于本报告头部；结论需人工最终拍板。

## 1. 环境登记（实验条件）

- 后端：`laya(PyPI 0.3.21, multilingual checkpoint, torch CPU)`
- HF 镜像：`https://hf-mirror.com`
- 判定映射：noul(p≥0.5→yes) / choice 取字段 / score argmax→档位
- 数据集：`evaluation\spikes\e7_gate\dataset.jsonl`（sha256前12位 `f97da05330f1`，共 46 条，repeat=1）
- 预热耗时：21590.2ms（模型加载+首推理，不计入延迟统计）；运行时间：20260928_180201

## 2. 汇总指标

| 任务 | n | accuracy | effective_acc | parse | failsafe | P50(ms) | P95(ms) | avg_conf | avg_tokens |
|---|---|---|---|---|---|---|---|---|---|
| 缺页判定（是否需要回源） | 16 | 0.5 | 0.5 | 1.0 | 0.0 | 1087.8 | 1479.7 | 0.9095 | 143.3125 |
| 取代判定（无关/并存/取代） | 15 | 0.3333 | 0.3333 | 1.0 | 0.0 | 738.1 | 952.1 | 0.6976 | 111.8 |
| 回填判定（是否存在因果） | 15 | 0.6667 | 0.6667 | 1.0 | 0.0 | 694.4 | 831.9 | 0.6986 | 110.6667 |

## 3. 难度分层准确率（v2 价值区：easy→hard 梯度）

| 任务 | easy | medium | hard |
|---|---|---|---|
| 缺页判定（是否需要回源） | — (n=0) | — (n=0) | — (n=0) |
| 取代判定（无关/并存/取代） | — (n=0) | — (n=0) | — (n=0) |
| 回填判定（是否存在因果） | — (n=0) | — (n=0) | — (n=0) |

## 4. 任务专项指标（误差方向分解）

- **缺页**：`dangerous_miss`（该回忆没回忆）= 0；`safe_extra`（多回源）= 8
- **取代**：`missed_supersede`（该取代未取代，旧卡滞留）= 5；`wrong_supersede`（误取代，信息丢失）= 0；混淆对：{'supersede→coexist': 5, 'coexist→unrelated': 1, 'unrelated→coexist': 4}
- **回填**：`dangerous_connect`（错连线）= 2；`missed_link`（漏连线）= 3

## 5. 错例与兜底明细

### 5.1 判定错误（校验通过但判错）

| 任务 | id | 难度 | gold | 判定 | 陷阱 | rationale |
|---|---|---|---|---|---|---|
| page_fault | pf_09 | v1 | no | yes | — | 闲聊/实时信息，无历史钩子 |
| page_fault | pf_10 | v1 | no | yes | — | 纯生成任务 |
| page_fault | pf_11 | v1 | no | yes | — | 常识问答 |
| page_fault | pf_12 | v1 | no | yes | — | 寒暄 |
| page_fault | pf_13 | v1 | no | yes | — | 闲聊 |
| page_fault | pf_14 | v1 | no | yes | — | 工具型任务，无需记忆 |
| page_fault | pf_15 | v1 | no | yes | — | 通用知识问答 |
| page_fault | pf_16 | v1 | no | yes | — | 情感反馈，无检索需求 |
| supersede | sup_01 | v1 | supersede | coexist | — | 同一属性(住址)变更 |
| supersede | sup_02 | v1 | supersede | coexist | — | 同一属性(雇主)变更 |
| supersede | sup_03 | v1 | supersede | coexist | — | 同一属性(手机号)变更 |
| supersede | sup_04 | v1 | supersede | coexist | — | 同一目标数值修订 |
| supersede | sup_05 | v1 | supersede | coexist | — | 偏好翻转 |
| supersede | sup_08 | v1 | coexist | unrelated | — | 两段经历并存 |
| supersede | sup_11 | v1 | unrelated | coexist | — | 住址与口味无关 |
| supersede | sup_12 | v1 | unrelated | coexist | — | 工作与宠物无关 |
| supersede | sup_13 | v1 | unrelated | coexist | — | 生日与新爱好无关 |
| supersede | sup_14 | v1 | unrelated | coexist | — | 偏好与行程无关 |
| backfill | bf_03 | v1 | yes | no | — | 计划→准备 |
| backfill | bf_04 | v1 | yes | no | — | 通勤变化→作息调整 |
| backfill | bf_06 | v1 | yes | no | — | 环境→应对 |
| backfill | bf_11 | v1 | no | yes | — | 无因果 |
| backfill | bf_12 | v1 | no | yes | — | 无因果 |

（无兜底样本：三层校验 100% 通过）

## 6. 拍板结论

**机器建议：未达标** —— 多任务 accuracy < 0.70，需重新评估。
未达冲刺线（0.85）的任务：page_fault=0.5, supersede=0.3333, backfill=0.6667

> ⚠️ 本节为机器建议，最终拍板与决策记录（Dxx）由人工写入 docs/决策记录.md。
