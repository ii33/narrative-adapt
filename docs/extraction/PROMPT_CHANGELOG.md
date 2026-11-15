# 提示词版本记录

本页记 `src/narrative/prompt_defaults.py` 里 `DEFAULT_PROMPTS` 各条的版本。**历史变更从这里开始记**：本次建页只给出当前版本表，不回填既往改动（既往每次改写的字数与结构对照在 [提示词写作风格](../PROMPT_STYLE.md) 第 7 节与 [提取提示词重写](../EXTRACTION_PROMPT_REWRITE.md)）。

改一条提示词要同批做三件事，缺一件就红：

1. 改文本并升该条的 `version`（只升版本不改文本会被 `tests/test_prompt_version_snapshot.py` 拦）；
2. 同批更新 [SPEC §7 提示词索引](SPEC.md) 的版本号与该条覆盖的 `E-xx`（`tests/test_extraction_spec.py` 会拦）；
3. 在本页表格的「本次变更」列补一行，写清改的是哪一条、为什么。

## 当前版本

| id | version | 本次变更 |
| --- | --- | --- |
| `system` | v11 | 已按五段式重写（见 [PROMPT_STYLE](../PROMPT_STYLE.md) 第 7 节） |
| `extraction` | v27 | 关系方向按语义核对：互逆关系正反两条都记、单向关系只记有依据的方向、`symmetric` 只管同一个值对称成立（`U-04`） |
| `granularity_standard` | v6 | 标准档只回答切多细，事实怎么取指向「提取通用要求」 |
| `granularity_fine` | v8 | 出场人物都要建档（建人物节点 + 写清能支撑造型的属性；提取阶段不建资料卡），视觉资产据此建立（`U-05`） |
| `source_extraction` | v9 | 范围与完成口径独立成段，分层与取证指向「提取通用要求」 |
| `review` | v12 | 复核 = 查漏、补缺、勘误；勘误直接改那条记录，不追加修正记录（`U-02`、`U-03`） |
| `event_merge_review` | v3 | 补重复的定义、判据与合并字段（旧版未定义） |
| `action_extraction` | v7 | 补事件 E 与动作 A 的分界，动作直属事件 |
| `scene_table` | v6 | 场表任务：场界按返回的 `scene_table_policy` 判断 |
| `writing` | v8 | 吸收原 `batch_writing` 的批量语义 |
| `writing_shortcut` | v1 | 新建：合并原三条快捷提示 |
| `scene_policy` | v6 | 一行一条规则（代码按行切 `rules`），首行点明与提取层场景 S 不同 |
| `storyboard` | v6 | 分镜导演台：三个毫秒量分开写，视频提交只做一次 |
| `sketch_style` | v3 | 工作用描述型分镜的画法与省略清单 |
| `storyboard_assets` | v3 | 只判出场元素需求，不创建也不绑定资产 |
| `conversation_actions` | v3 | 已完成动作摘要：不是原文，也不是新的授权 |
| `reference_policy` | v4 | 依据优先级与「不授权恢复已排除情节」 |

版本号只以代码为准：本表与 [SPEC §7](SPEC.md) 由 `tests/test_extraction_spec.py` 逐条比对 `DEFAULT_PROMPTS` 的运行时值。

## 怎么记

每次改提示词都在上面「本次变更」列补一行，格式固定为：

```
日期 | id | 旧 → 新版本 | 影响的 E-xx／§6 码 | 原因
```

例：`2026-11-20 | extraction | v26 → v27 | E-27；无新码 | 关系方向按语义核对（U-04）`。

已记的变更：

```
2026-11-15 | extraction | v26 → v27 | E-27 | 关系方向按语义核对：互逆双向、单向不猜（U-04）
2026-11-15 | granularity_fine | v6 → v8 | E-17 | 精细级出场人物都要建档；提取阶段不建资料卡（U-05）
2026-11-15 | review | v11 → v12 | E-28 | 复核=查漏补缺勘误；勘误直接改记录，不追加修正记录（U-02/U-03）
```

本轮（建页）之前的历史没有系统记录，从这次开始按上面的格式记；既往改动只能到 [PROMPT_STYLE](../PROMPT_STYLE.md) 第 7 节与 [EXTRACTION_PROMPT_REWRITE](../EXTRACTION_PROMPT_REWRITE.md) 里核对，不回填本页表格。改提示词必须同批升版本（只升版本不改文本会被 `tests/test_prompt_version_snapshot.py` 拦）。
