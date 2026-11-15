"""《飘邈之旅》前三章的真实内容生产基准：一次完整 fine 提取 + 有界外部 QA。

原文是**外部版权文本**，只按路径读取：验收目录里只落哈希、统计与结构化指标，
不复制任何正文片段。缺文件、边界对不上就断言失败，不猜。
用法（仓库根目录，需模型凭据，只跑一次、不自动重试）：
  .venv\\Scripts\\python.exe scripts\\accept_piaomiao_three_chapters.py
默认输出 data/acceptance/piaomiao-three-chapters/<UTC 时间戳>/（data/ 已被 gitignore）。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import accept_screenplay as qa
from qa_live_observer import LiveObserver, observe_real_calls

SOURCE_PATH = Path("C:/Users/youai/Downloads/飘邈之旅UTF8.txt")
SOURCE_CHARS = 2027329
HEADING_COUNT = 297
HEADING_RE = r"(?m)^\s*第[零一二三四五六七八九十百千0-9]+集[^\n]*?第[零一二三四五六七八九十百千0-9]+章[^\n]*"
# 前四章的标题起点：选 N 章就是 [0, CHAPTER_STARTS[N])。
CHAPTER_STARTS = [146, 5783, 11193, 16517]
# 默认 1 章：一次 fine 提取几分钟跑完，适合改完提示词自检；
# --chapters 3 是完整验收（约 20 分钟），留给"改完规则要一次总验"。
SELECTED_CHAPTERS = 1
START_CHAPTER = 1
TASK_START = CHAPTER_STARTS[0]
TASK_END = CHAPTER_STARTS[SELECTED_CHAPTERS]
CHAPTER_CHARS = TASK_END - TASK_START
REPORT_ROOT = Path("data/acceptance/piaomiao-ch1")
# 实测写死：text[146:16517] 的 UTF-8 sha256（第一次运行前用文件实算，不是抄来的）。
CHAPTER_TEXT_SHA256 = "2e68b28d06f1d86ce169cb28b9e07d9be1a1c442d24975682ed201dfe8791dd6"

# main() 会把 qa.TEXT 落成 input.txt；这里只放说明，原文不进验收目录。
TEXT_NOTE = "外部版权原文按路径读取，未复制；范围、字数与摘要见 fixture.json 的 target_* 字段。"
TASK_TEMPLATE = """请在当前空工程中处理《飘邈之旅》第 {first} 到第 {last} 章的完整原文（源文本字符区间 {start} 到 {end}，共 {chars} 字），按工程 fine 精细粒度逐章完整读取并提取。不缩短章节、不只做开头示例、不处理第 {next_chapter} 章。
请只用两三次读取确认标题，然后**立刻提交第一批**，不要先通读全章；逐章完整读取并提取，按内容分批提交，**每批范围不得与已接受批次重叠**（只补未覆盖的段落），每批使用对应的实际原文范围与逐字证据。覆盖有叙事作用的主要及次要/无名人物、具体地点、关系（含互逆关系的正反两条）、心理认知、物品与持有者、事件及所属场景，不只列名词或梗概。核对人物身份与代词、叙事时空、关系方向、物品持有者及换手区间，事件归属场景。默认不提取动作，不为章节尚未结束的较大篇章创建临时主要故事。
正常提取流程自主完成；报告各章实际完成范围、入库结果与存疑。若未完成不能声称这几章全部完成。只处理指定的这几章，不修改其他业务对象，不另开任务或重试服务。"""
OBSERVER = LiveObserver()


def task_text() -> str:
    """按本次选定的章数生成任务描述（默认 1 章，几分钟一轮）。"""
    last = START_CHAPTER + SELECTED_CHAPTERS - 1
    return TASK_TEMPLATE.format(first=START_CHAPTER, last=last, count=SELECTED_CHAPTERS,
                                start=TASK_START, end=TASK_END, chars=CHAPTER_CHARS,
                                next_chapter=last + 1 if last < 3 else 3)


# 基准用的模型运行参数。这里踩过一次反直觉的坑，记下来：
# 为了少让长生成被流掐断，曾把输出上限从 65,536 压到 16,384——结果**单章反而比三章更贵**
# （36 次请求 / 1.71M token，而三章那次是 30 次 / 1.36M）：上限一小，模型交不完的草稿被拆成
# 更多批，每次请求都要重发整段上下文，成本随请求数线性涨。所以输出上限回到 65,536。
# 空转超时 300 s 与成本无关，保留（120 s 对慢模型偏紧，是 `STREAM_CLOSED` 的可疑诱因之一）。
BENCH_MAX_TOKENS = 65_536
BENCH_TIMEOUT_SECONDS = 300


def configure(chapters: int, start_chapter: int = 1) -> None:
    """选本次处理哪几章（起止都 1~3）：重算范围常量与报告目录。"""
    global SELECTED_CHAPTERS, START_CHAPTER, TASK_START, TASK_END, CHAPTER_CHARS, REPORT_ROOT
    if chapters not in (1, 2, 3) or start_chapter not in (1, 2, 3) or start_chapter + chapters - 1 > 3:
        raise SystemExit("--chapters / --start-chapter 只接受 1~3，且不得越过第三章")
    SELECTED_CHAPTERS, START_CHAPTER = chapters, start_chapter
    TASK_START = CHAPTER_STARTS[start_chapter - 1]
    TASK_END = CHAPTER_STARTS[start_chapter - 1 + chapters]
    CHAPTER_CHARS = TASK_END - TASK_START
    span = f"ch{start_chapter}" if chapters == 1 else f"ch{start_chapter}-{start_chapter + chapters - 1}"
    REPORT_ROOT = Path(f"data/acceptance/piaomiao-{span}")


def load_input():
    from narrative.source_documents import read_source_file

    text = read_source_file(SOURCE_PATH)
    headings = list(re.finditer(HEADING_RE, text))
    assert len(text) == SOURCE_CHARS, f"原文长度变了：{len(text)}"
    assert len(headings) == HEADING_COUNT, f"章节标题数变了：{len(headings)}"
    assert [match.start() for match in headings[:4]] == CHAPTER_STARTS, [match.start() for match in headings[:4]]
    stop = START_CHAPTER - 1 + SELECTED_CHAPTERS
    assert headings[stop].start() == TASK_END, f"第 {stop + 1} 章起点变了：{headings[stop].start()}"
    assert TASK_END - TASK_START == CHAPTER_CHARS
    if SELECTED_CHAPTERS == 3:
        # 三章全跑是完整验收：摘要写死，素材一换就硬失败；单章/双章只比对标题边界。
        assert qa.digest(text[TASK_START:TASK_END].encode()) == CHAPTER_TEXT_SHA256, "前三章文本摘要变了"
    return text, headings


def seed(ops):
    text, headings = load_input()
    ops.save_project_settings(ops.load_project_settings() | {"extraction_granularity": "fine"})
    # 基准的模型参数（见 BENCH_MAX_TOKENS 注释）：输出上限保持 65,536，别为了防流断把它压小——
    # 压小会让一次能交完的草稿被拆成多批，请求数与 token 都跟着涨。
    current = ops.harness_settings()
    ops.save_harness_settings(current | {"max_tokens": BENCH_MAX_TOKENS,
                                         "output_reserve": BENCH_MAX_TOKENS,
                                         "timeout_seconds": BENCH_TIMEOUT_SECONDS})
    source = ops.import_source(SOURCE_PATH.stem, text)
    script = ops.create_screenplay("内容基准采集空容器", reason="共享证据采集器的确定性空容器")["screenplay"]
    return {
        "source_id": source,
        "screenplay_id": script["id"],
        "source_chars": len(text),
        "source_file_sha256": qa.digest(SOURCE_PATH.read_bytes()),
        "source_text_sha256": qa.digest(text.encode()),
        "target_start": TASK_START,
        "target_end": TASK_END,
        "target_chars": CHAPTER_CHARS,
        "target_text_sha256": qa.digest(text[TASK_START:TASK_END].encode()),
        "chapters": [
            {
                "index": index + 1,
                "title": headings[index].group().strip(),
                "start": headings[index].start(),
                "end": headings[index + 1].start(),
            }
            for index in range(3)
        ],
    }


def _observations(ops, graph, records):
    """本轮两条新口径的观察值：只记录，不作为失败条件。"""
    from narrative import property_categories

    config = property_categories.load(ops.project.store)
    node_types = {node["id"]: node.get("node_type") for node in graph["nodes"]}
    identity, vocabulary_named, same_name, sources = [], 0, 0, {}
    groups, add_records = {}, 0
    for row in records:
        if row.get("kind") != "property_change":
            continue
        node_type = node_types.get(row.get("node_id"))
        category, category_source = property_categories.resolve(row, node_type, config)
        sources[category_source] = sources.get(category_source, 0) + 1
        name_in_vocabulary = row.get("property_id") in property_categories.vocabulary_for(node_type, config)
        if name_in_vocabulary:
            vocabulary_named += 1
            same_name += category == row.get("property_id")
        if category_source == "identity":
            identity.append({"node_type": node_type, "property": row.get("property_id"), "category": category})
        if row.get("op") == "add":
            add_records += 1
            key = (row.get("node_id"), row.get("property_id"), row.get("scope") or "world", row.get("holder_id"))
            groups.setdefault(key, set()).add(row.get("event_id"))
    multi = {key: events for key, events in groups.items() if len(events) > 1}
    return {
        "identity_category": {
            "record_count": len(identity),
            "category_source_counts": sources,
            "property_name_in_vocabulary_records": vocabulary_named,
            "explicit_category_equals_property_name": same_name,
            "sample": identity[:10],
            "note": "属性名恰好等于分类词表项时 category_source 应为 identity（本轮新规则）；只观察，不判失败。",
        },
        "cross_event_accumulation": {
            "add_record_count": add_records,
            "multi_event_property_count": len(multi),
            "records_in_multi_event_properties": sum(len(events) for events in multi.values()),
            "sample": [
                {"node_id": key[0], "property": key[1], "event_count": len(events)}
                for key, events in list(multi.items())[:10]
            ],
            "note": "同一属性跨多个事件仍用 add 累积（而不是后写取代）的规模。按现行规则应接近 0：同一属性的先后是取代关系；并列只发生在同一事件点多值、以及不同属性名两种情形。不为 0 说明模型把先后写成了并列，属提示词要盯的点；只观察，不判失败。",
        },
    }


def validate(ops, fixture, screenplay, calls, report):
    text, _ = load_input()
    graph, records = ops.graph_snapshot(), ops.property_records()
    audit = json.loads((Path(report["audit_directory"]) / "task.json").read_text(encoding="utf-8"))
    steps = audit.get("steps", [])
    submissions = [step for step in steps if step["tool"] == "submit_extraction"]
    patches = [step for step in steps if step["tool"] == "patch_extraction_submission"]

    # 真实流程里草稿常常先 staged、再经 patch 逐条纠正：批次被接受可能发生在
    # submit_extraction 也可能是 patch_extraction_submission（返回 submission_status
    # committed）。只认 submit 的 status 会漏掉已经入库的批次——首次真实运行踩到了。
    def accepted_batch(result):
        result = result or {}
        return result.get("status") == "accepted" or result.get("submission_status") in {"accepted", "committed"}

    accepted_ids = {step["result"]["submission_id"] for step in submissions if accepted_batch(step.get("result"))}
    accepted_ids |= {step["args"].get("submission_id") for step in patches if accepted_batch(step.get("result"))}
    accepted = [step for step in submissions if (step.get("result") or {}).get("submission_id") in accepted_ids]
    spans = [span for node in graph["nodes"] for span in node.get("evidence_spans", [])]
    events = [node for node in graph["nodes"] if node["node_type"] == "event"]
    slices = ops.source_slices(fixture["source_id"])
    if isinstance(slices, dict):
        slices = slices.get("slices", [])
    coverage = []
    for chapter in fixture["chapters"]:
        members = [node for node in events if chapter["start"] <= (node.get("disclosure") or {}).get("start", -1) < chapter["end"]]
        evidence = [span for span in spans if chapter["start"] <= span["start"] < chapter["end"]]
        width = chapter["end"] - chapter["start"]
        bins = [
            sum(
                chapter["start"] + width * index / 8 <= node["disclosure"]["start"] < chapter["start"] + width * (index + 1) / 8
                for node in members
            )
            for index in range(8)
        ]
        coverage.append(
            chapter
            | {
                "event_count": len(members),
                "first_event_start": min((node["disclosure"]["start"] for node in members), default=None),
                "last_event_start": max((node["disclosure"]["start"] for node in members), default=None),
                "evidence_count": len(evidence),
                "last_evidence_end": max((span["end"] for span in evidence), default=None),
                "event_counts_by_eighth": bins,
            }
        )
    declared = sorted(
        (max(TASK_START, step["args"].get("start", 0)), min(TASK_END, step["args"].get("end") or len(text)))
        for step in accepted
    )
    cursor = TASK_START
    for start, end in declared:
        if start <= cursor:
            cursor = max(cursor, end)
    # 分八段只做"有没有整段没读"的粗看，写成测量值；它不构成通过/不通过（`M-01` 不设配额）。
    empty_bins = sum(row["event_counts_by_eighth"].count(0) for row in coverage)
    longest_gap = 0
    for row in coverage:
        run = 0
        for count in row["event_counts_by_eighth"]:
            run = run + 1 if count == 0 else 0
            longest_gap = max(longest_gap, run)
    report["eighth_observation"] = {"empty_bins": empty_bins, "longest_empty_run": longest_gap,
                                    "bins_total": 8 * len(coverage),
                                    "note": "分八段只用来发现「整段没读」，不是事件数配额（M-01）"}
    check = ops.check_project()
    node_counts = {}
    for node in graph["nodes"]:
        node_counts[node["node_type"]] = node_counts.get(node["node_type"], 0) + 1
    report["large_sample_evidence"] = {
        "chapter_coverage": coverage,
        "created_slices": slices,
        "accepted_batch_count": len(accepted),
        "submission_attempt_count": len(submissions),
        "patch_attempt_count": len(patches),
        "accepted_submission_ids": sorted(accepted_ids),
        "accepted_declared_ranges": declared,
        "node_counts": node_counts,
        "evidence_span_count": len(spans),
        "property_record_count": len(records),
        "locate_calls": [step for step in steps if step["tool"] == "locate_source"],
        "preview_calls": [step for step in steps if step["tool"] == "preview_extraction"],
        "static_check": check,
        "live_observation": OBSERVER.snapshot(),
        "coverage_limit": "提交的范围声明与已读全文不等于穷尽提取：这里只报实际证据坐标与事件分布，语义完整性需人工与众章原文对照。不设事件条数配额。",
    }
    report["observations"] = _observations(ops, graph, records)
    report["observations_are_not_failures"] = ["identity_category", "cross_event_accumulation"]
    report["input_assets"] = {
        "source_path": str(SOURCE_PATH),
        "source_chars": fixture["source_chars"],
        "source_file_sha256": fixture["source_file_sha256"],
        "target_chars": fixture["target_chars"],
        "target_text_sha256": fixture["target_text_sha256"],
        "input_file_contains_source_text": False,
        "input_sha256_is_hash_of": "input.txt 的说明文字（原文不复制进验收目录），不是原文摘要",
    }
    report["semantic_review_status"] = "pending_independent_source_comparison"
    # 提取正确性不由程序判定。程序只回答"这条记录在结构上成不成立"（引文坐标是否逐字对得上、
    # 证据是否落在本次范围、静态校验有没有结构错误、原文有没有被改写），不回答"提取得好不好"。
    # 事件密度、覆盖、动作有无、批次范围这些是**测量**，写下来给人看，不给通过/不通过。
    report["judgement_owner"] = "human_or_model_review"
    report["validation_scope"] = "程序只判结构成立与数据完整性；提取正确性、完整性与粒度好坏由人工对照原文判定。"
    # 结构事实：程序能机械核对的部分。
    structural = {
        # 模型自己建了覆盖每章的切片即可。**不要求逐字等于章节边界**：从 146 起还是 150 起是排版细节，
        # 不是结构错误；精确边界记在 measurements.slice_bounds 里给人看。
        "three_chapters_created_by_model": all(
            any(row["start"] <= chapter["start"] + 8 and row["end"] >= chapter["end"] - 8 for row in slices)
            for chapter in fixture["chapters"]
        ),
        "all_node_quote_coordinates_exact": bool(spans)
        and all(
            span.get("source_id") == fixture["source_id"] and text[span["start"]:span["end"]] == span["quote"]
            for span in spans
        ),
        # 允许 8 字容差：切片边界落在章节标题的换行上时，多带几个字属排版细节，不是数据完整性问题。
        "all_evidence_inside_three_chapters": all(
            TASK_START - 8 <= span["start"] < span["end"] <= TASK_END + 8 for span in spans
        ),
        "static_check_no_errors": check["counts"].get("error", 0) == 0,
        "whole_original_source_unchanged": qa.digest(ops.source(fixture["source_id"])["text"].encode())
        == fixture["source_text_sha256"],
    }
    # 提取质量的测量值：只记数字，不判定。分八段与事件密度都不是配额（`M-01`）。
    report["measurements"] = {
        "accepted_batches": len(accepted),
        "declared_ranges_cover_three_chapters": cursor >= TASK_END,
        "declared_ranges": [{"start": start, "end": end} for start, end in declared],
        "event_count_by_chapter": {str(row["index"]): row["event_count"] for row in coverage},
        "evidence_count_by_chapter": {str(row["index"]): row["evidence_count"] for row in coverage},
        "event_counts_by_eighth": {str(row["index"]): row["event_counts_by_eighth"] for row in coverage},
        "empty_eighth_bins": empty_bins,
        "longest_empty_run": longest_gap,
        "action_nodes": sum(1 for node in graph["nodes"] if node["node_type"] == "action"),
        "slice_bounds": [{"start": row["start"], "end": row["end"]} for row in slices],
        "evidence_outside_task_range": [
            {"start": span["start"], "end": span["end"]}
            for span in spans if span["start"] < TASK_START or span["end"] > TASK_END
        ][:10],
        "chapter_bounds": [{"index": c["index"], "start": c["start"], "end": c["end"]} for c in fixture["chapters"]],
        "note": "这些是测量，不是判定：事件密度、覆盖与动作有无都不能说明提取对错（M-01）。",
    }
    return structural


# 程序能机械核对的结构事实。其余一切（事件密度、场景数、覆盖、动作有无）都是测量值，
# 不能变成"提取正确性"的判定——那是人和模型对照原文才能做的判断。
STRUCTURAL_KEYS = (
    "task_completed",
    "all_required_tools_called",
    "three_chapters_created_by_model",
    "all_node_quote_coordinates_exact",
    "all_evidence_inside_three_chapters",
    "static_check_no_errors",
    "whole_original_source_unchanged",
)


def normalize(report: dict) -> dict:
    """把共享验收框架的 passed/not_passed 改写成"结构是否成立 + 待人工判定"。

    `accept_screenplay` 会算一批含质量含义的 check（例如 `scene_count_2_to_3`）并据此给出
    passed。按「提取正确性不由程序判断」，这些只能在测量值里留数字，状态由结构事实决定，
    最终结论交人工。
    """
    raw = report.get("checks") or {}
    structural = {key: raw[key] for key in STRUCTURAL_KEYS if key in raw}
    quality = {key: value for key, value in raw.items() if key not in structural}
    measurements = report.setdefault("measurements", {})
    if quality:
        measurements["program_view_not_a_verdict"] = quality
    report["structural_checks"] = structural
    report["failed_structural_checks"] = sorted(key for key, value in structural.items() if not value)
    report["judgement_owner"] = "human_or_model_review"
    report["verdict"] = "pending_human_review"
    report["harness_status"] = report.get("status")
    report["status"] = "structural_ok" if not report["failed_structural_checks"] else "structural_failed"
    report["status_meaning"] = (
        "structural_ok 只表示数据在结构上成立（引文逐字对齐、证据在范围内、静态校验无结构错误、"
        "原文未被改写）；它不表示提取正确或完整，正确性由人对照原文判定。"
    )
    return report


def recheck(directory):
    """对**已有一次真实运行**的产物离线重算判读：不调用模型、不改动工程数据。

    存在的理由：判读代码本身可能写错（首次运行就把 patch 入库的批次判成未接受）。
    重算只读 report.json / fixture.json 与已落盘的工程，把新判读写回 report.json，
    首次判读原文另存 report-first-scoring.json，保留出处。
    """
    from narrative.operations import Operations

    directory = Path(directory).resolve()
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    fixture = json.loads((directory / "fixture.json").read_text(encoding="utf-8"))
    # 离线重算也必须按这份报告当时的范围判，不能沿用模块默认（默认是 1 章）。
    configure(len(fixture.get("chapters") or [1]))
    projects = sorted(directory.glob("*.nproj"))
    assert len(projects) == 1, f"报告目录里应恰好有一个工程：{projects}"
    ops = Operations.open(projects[0])
    saved_live = (report.get("large_sample_evidence") or {}).get("live_observation")
    first_scoring = directory / "report-first-scoring.json"
    if not first_scoring.is_file():
        first_scoring.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    checks = validate(ops, fixture, None, [], report)
    # 两个观察项也一并重算：它们是本轮改动（identity 分类、add 累积）在真实数据上的读数，
    # 少了它们看报告的人会以为没测。
    report["observations"] = _observations(ops, ops.graph_snapshot(), ops.property_records())
    if saved_live is not None:
        # 离线重算没有观察者线程：沿用首次运行落盘的实测观察数据。
        report["large_sample_evidence"]["live_observation"] = saved_live
    # 首次判读里的框架 check 原样留一份，便于对照"框架当时怎么看的"。
    report.setdefault("harness_checks", report.get("checks"))
    report["checks"] = checks
    report["recheck"] = {
        "at": datetime.now(UTC).isoformat(),
        "mode": "offline_recompute_no_model_call",
        "reason": "首次判读只认 submit_extraction 的 status，漏掉了经 patch 提交入库的已接受批次；修正判读后离线重算。",
        "first_scoring_file": first_scoring.name,
    }
    report.pop("failed_checks", None)
    normalize(report)
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"directory": str(directory), "status": report["status"],
                      "failed_structural_checks": report["failed_structural_checks"],
                      "verdict": report["verdict"]}, ensure_ascii=False))
    return 0 if not report["failed_structural_checks"] else 1


def main():
    text, _ = load_input()
    qa.seed, qa.TEXT, qa.TASK = seed, TEXT_NOTE, task_text()
    qa.PROMPT_VERSION = "real-piaomiao-three-chapters-fine/1"
    qa.FIXTURE_ORIGIN = f"外部版权原文经 Operations 原样导入（只按路径读，不复制）；由真实模型自行建切片并提取前 {SELECTED_CHAPTERS} 章。"
    with observe_real_calls(OBSERVER):
        exit_code = qa.main(
            validate=validate,
            required_tools=["submit_extraction"],
            prompt_factory=lambda fixture: task_text() + "\nsource_id=" + fixture["source_id"],
            use_writing_target=False,
            allow_extraction_change=True,
            on_started=OBSERVER.started,
            on_progress=OBSERVER.progress,
        )
    # 共享框架会按含质量含义的 check 给出 passed；这里改写成结构结论 + 待人工判定。
    for report_file in sorted(REPORT_ROOT.glob("*/report.json"), key=lambda item: item.stat().st_mtime):
        report = json.loads(report_file.read_text(encoding="utf-8"))
        if report.get("verdict") == "pending_human_review":
            continue
        report_file.write_text(json.dumps(normalize(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


def _argv():
    """不传 --output 时落到带时间戳的标准报告目录；其余默认值只在本脚本里给。"""
    argv = sys.argv[1:]
    if "--recheck" in argv:
        index = argv.index("--recheck")
        target = argv[index + 1] if len(argv) > index + 1 else ""
        if not target:
            raise SystemExit("--recheck 需要一个已有的报告目录")
        return recheck(target)

    def given(name):
        return any(item == name or item.startswith(name + "=") for item in argv)

    chapters, start = 1, 1
    for flag in ("--chapters", "--start-chapter"):
        for index, item in enumerate(list(argv)):
            value = None
            if item == flag and index + 1 < len(argv):
                value, span = argv[index + 1], 2
            elif item.startswith(flag + "="):
                value, span = item.split("=", 1)[1], 1
            if value is None:
                continue
            if not value.isdigit():
                raise SystemExit(f"{flag} 需要一个数字")
            if flag == "--chapters":
                chapters = int(value)
            else:
                start = int(value)
            del argv[index:index + span]
            break
    configure(chapters, start)

    if not given("--output"):
        argv += ["--output", str(REPORT_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))]
    if not given("--seconds"):
        argv += ["--seconds", "3600"]
    if not given("--token-budget"):
        argv += ["--token-budget", "2000000"]
    sys.argv = [sys.argv[0], *argv]
    return main()


if __name__ == "__main__":
    raise SystemExit(_argv())
