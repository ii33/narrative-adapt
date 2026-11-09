# ADR-004 提示词按数据对待

状态：已采纳

## 背景

提示词长期被当成文案：随手改一句、没有版本、测试用 `assert '不能因本批、切片或章节结束就闭合主要故事' in prompt` 逐字锁整句。结果是每次措辞微调都让一批测试变红，改的人干脆删断言；同时没人说得清某次真实验收用的是哪一版提示词，回归只能凭印象对比输出。

## 决策

提示词是数据，不是散落在代码里的字符串常量。每条有 `label`、`description`、`version`、`text` 四个字段；改动必须把 `version` 加一；测试按要点断言；风格以 `docs/PROMPT_STYLE.md` 为唯一来源。

可判定的判据：读任何一条提示词都能同时拿到文本与版本；任务审计能回答「这次用的是哪一版」；改一条提示词时，测试里锁定的是关键词而非整句。

## 影响

- 代码：`DEFAULT_PROMPTS` 是纯数据字典；`prompt_settings.py` 在它之上加工程覆盖（`save_prompt_override` 走修订号 CAS、必填 `reason`、写动作审计），生效版本号带 `/project-N` 后缀。
- 运行时：任务开始时冻结快照（`bind_prompt_snapshot`，`conversation_session.py`），同一任务内改设置不影响本轮；请求里带 `prompt_config`，审计因此可回溯。
- 测试：断言改为要点（关键词/短语），版本号在用例里显式列出；`tests/test_screenplay_writing_authority.py` 的版本表就是一类。
- 文档：`PROMPT_STYLE.md` 规定五段结构、字数与「不能」次数上限，并记「已重写」表；行为类改动还要做真实模型多次对照。

## 代价

- 改一句话的成本被拉高：量指标 → 改 → 升版本 → 改断言 → 写改动记录 → 真实模型对照。随手改文案的便利没有了，这是刻意的。
- 版本号靠人维护。代码不校验「`text` 变了就必须 `version`+1」，只有测试里写死的版本表间接拦一下；漏改仍能合入，审计里的版本号就会失真。
- 要点断言放宽了回归网：意思变了而关键词还在的改动不会被拦住，只能靠真实模型对照兜底。
- 双轨读取：生效文本不等于 `DEFAULT_PROMPTS`，必须走 `effective_prompt`；同一条提示词可能同时存在「默认 v23」与「工程 project-3」两个版本号，排查时要看清是哪一轨。
- 真实模型对照要隔离工程、同一输入多次运行并比较结构规模，时间成本远高于单测。

## 另见

- `docs/PROMPT_STYLE.md`、`docs/EXTRACTION_PROMPT_REWRITE.md`；`docs/MODEL.md`（规则编号只在这一侧改）
- `src/narrative/prompt_defaults.py`、`src/narrative/prompt_settings.py`、`src/narrative/conversation_session.py`（`bind_prompt_snapshot` 调用点）
