#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流水线档位判定：回答「项目走到哪一步、下一步是什么」。

校验器回答门禁问题（满不满足契约），本脚本回答进度问题（在流水线哪一格）。
两者正交：C15 眼里「草稿」是合法取值，门禁正确放行，但没有任何组件说过
「全库 12 份里 9 份已冻结、3 份草稿，处于维护期变更中」——本脚本补这一维。

判定信号全部来自文件系统与既有解析器（Corpus / snapshot_recorded_docs /
访谈状态文件），不依赖 git。档位判定是读取不是回写。

    python3 pipeline_status.py --dir ./需求文档
    python3 pipeline_status.py --dir .            # 给上级目录会自动定位 需求文档/
    python3 pipeline_status.py --dir ./需求文档 --fast    # 跳过全量校验（L4/L6 降级为启发式）
    python3 pipeline_status.py --dir ./需求文档 --format json

退出码: 0 = 档位判定完成（L0 未开始也是合法判定）, 2 = 用法或环境错误。
它是仪表不是门禁，不设 1。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validate_requirements import (  # noqa: E402
    CHECKS, Corpus, VALID_DOC_STATUS, first_id, get_col, is_empty_marker,
    snapshot_recorded_docs,
)

# 档位 -> （名称, 所属轮次, 置信度, 下一步建议）。这是映射的唯一来源，
# SKILL.md 的流水线协议节引用它，不自抄。
GEARS: Dict[str, Tuple[str, str, str, str]] = {
    "L0": ("未开始", "第 1 轮 · 骨架", "确定",
           "进入第 1 轮：通读原始材料，产出模块/关系/NFR/术语四份清单"),
    "L1": ("骨架访谈中", "第 2 轮 · 访谈确认", "确定",
           "按 resume 协议从 next_question_id 接续访谈，不重问已定项"),
    "L2": ("已批准待展开", "第 2 轮 · 访谈确认", "确定",
           "展开目录：scaffold_docs.py skeleton.json -o ./需求文档，然后跑 --only C01,C05"),
    "L3": ("逐模块生成", "第 3 轮 · 逐模块生成", "确定",
           "从占位最多的模块继续填写；每完成一个跑 --only C02,C03,C04,C07"),
    "L4": ("全局审查", "第 4 轮 · 全局审查", "启发式",
           "全量校验清零；派未参与生成的子任务做独立语义审查；本轮不转状态"),
    "L5": ("场景编写", "第 5 轮 · 场景编写", "启发式",
           "编写/补齐场景切片并走查；跑 --only C02,C03,C14；本轮不转状态"),
    "L6": ("备料完成待评审", "第 6 轮 · 版本规划与里程碑评审", "确定",
           "组装评审包（--strict 已绿 + manual_review_checklist + diff 摘要），"
           "向评审人发起里程碑评审——这是人工触点，等「通过」或逐份打回"),
    "L7": ("评审打回回路中", "第 6 轮 · 版本规划与里程碑评审", "确定",
           "按打回理由修复并判对外契约面（decomposition-rules.md §8.7）；"
           "契约面动了则受影响邻接批准作废，并入增量重提"),
    "L8": ("定稿扫尾", "第 6 轮 · 版本规划与里程碑评审", "确定",
           "转已评审（两处一起改）→ --snapshot 打基线 → 转已冻结 → --only C15 复验"),
    "L9": ("已定稿", "维护期", "确定",
           "全库终稿。变更走维护期入口：先退状态再改，impact_analysis 查影响面"),
    "L10": ("维护期变更中", "维护期", "确定",
            "变更在草稿态迭代；攒齐后进入下一里程碑评审统一复审并重打基线"),
}

# 占位符判定与 C09 同口径：[待确认: ...] 与 [新增术语提案] 都是过程态占位。
# C09 的正则嵌在校验函数里不便复用，这里按同一词表另写一份；改动词表时
# S21 的冒烟会咬住两边不一致（空白骨架必须判 L3）。
RE_PENDING = re.compile(r"\[待确认|\[新增术语提案\]")

REVIEW_HEADING_KW = "评审记录"


def find_doc_dir(path: str) -> Optional[str]:
    """--dir 可以是文档目录本身或其上级（自动定位 需求文档/）。"""
    path = os.path.realpath(path)
    if not os.path.isdir(path):
        return None
    if os.path.isfile(os.path.join(path, "00-综述.md")) \
            or os.path.isdir(os.path.join(path, "modules")):
        return path
    for name in ("需求文档", "docs"):
        cand = os.path.join(path, name)
        if os.path.isdir(cand) and (os.path.isfile(os.path.join(cand, "00-综述.md"))
                                    or os.path.isdir(os.path.join(cand, "modules"))):
            return cand
    return None


def find_interview_state(doc_dir: Optional[str], start: str,
                         explicit: Optional[str]) -> Tuple[Optional[str], Optional[Dict[str, Any]], List[str]]:
    """定位并读取访谈状态文件。损坏不致命：降级为提示，继续按工件判定。"""
    notes: List[str] = []
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    else:
        candidates.append(os.path.join(start, "interview-state.json"))
        if doc_dir:
            candidates.append(os.path.join(os.path.dirname(doc_dir), "interview-state.json"))
            candidates.append(os.path.join(doc_dir, "interview-state.json"))
    path = next((c for c in candidates if os.path.isfile(c)), None)
    if not path:
        return None, None, notes
    try:
        with open(path, "r", encoding="utf-8") as f:
            return path, json.load(f), notes
    except (OSError, json.JSONDecodeError) as exc:
        notes.append(f"访谈状态文件 {path} 不可读（{exc}），第 2 轮进度无从判定，"
                     f"按文档工件继续判定")
        return path, None, notes


def placeholder_count(text: str) -> int:
    return len(RE_PENDING.findall(text))


def read_review_records(corpus: Corpus) -> List[Dict[str, str]]:
    """版本快照「评审记录」表的行。表头避让 文档/版本 子串（见 CLAUDE.md 约束）。"""
    if not corpus.snapshot:
        return []
    for t in corpus.tables.get(corpus.snapshot.rel, []):
        if REVIEW_HEADING_KW in t.heading:
            rows = []
            for row in t.rows:
                obj = get_col(row, "评审对象").strip()
                verdict = get_col(row, "结论").strip()
                if not obj or is_empty_marker(obj):
                    continue
                rows.append({"obj": obj, "verdict": verdict})
            return rows
    return []


def unbalanced_bounce(records: List[Dict[str, str]]) -> List[str]:
    """每个评审对象以最后一次记录为准；最新结论是打回的即为未平。"""
    latest: Dict[str, str] = {}
    for r in records:
        latest[r["obj"]] = r["verdict"]
    return sorted(obj for obj, v in latest.items() if "打回" in v)


def detect(args: argparse.Namespace) -> Dict[str, Any]:
    notes: List[str] = []
    evidence: List[str] = []
    start = os.path.realpath(args.dir)
    doc_dir = find_doc_dir(start)
    if not os.path.isdir(start):
        print(f"目录不存在: {args.dir}", file=sys.stderr)
        raise SystemExit(2)

    state_path, state, st_notes = find_interview_state(doc_dir, start, args.state)
    notes.extend(st_notes)

    # --- 访谈链信号优先：它管「骨架定下来之前」 ---
    if doc_dir is None:
        if state is not None:
            st = str(state.get("status", ""))
            if st in ("approved", "stopped"):
                return gear("L2", evidence=[f"访谈状态 {st}（{state_path}）",
                                        "文档目录尚未展开"],
                            notes=notes)
            nq = state.get("next_question_id", "?")
            return gear("L1", evidence=[f"访谈状态 {st}（{state_path}）",
                                    f"下一问 {nq}"],
                        notes=notes, extra={"next_question_id": nq})
        if state_path and state is None:
            # 状态文件损坏：目录也没有，无法判定更细
            return gear("L0", evidence=["无访谈状态可读，无文档目录"], notes=notes)
        return gear("L0", evidence=["无 interview-state.json，无文档目录"], notes=notes)

    if state is not None and str(state.get("status", "")) == "interviewing" \
            and not os.listdir(doc_dir):
        nq = state.get("next_question_id", "?")
        return gear("L1", evidence=[f"访谈仍在进行（{state_path}），目录为空"],
                    notes=notes, extra={"next_question_id": nq})

    if state is None and not state_path:
        notes.append("未找到 interview-state.json：第 2 轮未被记录"
                     "（存量项目的合法起点，按文档工件判定）")

    # --- 文档链信号 ---
    corpus = Corpus(doc_dir)
    recorded = snapshot_recorded_docs(corpus)
    baselines = bool(recorded)
    if recorded:
        total_rows = sum(len(v) for v in recorded.values())
        evidence.append(f"基线记录存在（{len(recorded)} 份文档、{total_rows} 条版本记录）")

    # 三态文档计数：排除快照文档（不参与生命周期）与 ADR（自己的词表）
    adr_rels = {d.rel for d in corpus.decisions}
    snapshot_rel = corpus.snapshot.rel if corpus.snapshot else None
    counts: Dict[str, int] = defaultdict(int)
    tri_total = 0
    for rel, (raw, _) in corpus.doc_status.items():
        if rel == snapshot_rel or rel in adr_rels:
            continue
        if raw in VALID_DOC_STATUS:
            counts[raw] += 1
            tri_total += 1

    records = read_review_records(corpus)
    bounced = unbalanced_bounce(records)
    if records:
        evidence.append(f"评审记录 {len(records)} 条"
                        + (f"，未平打回 {len(bounced)} 份: {'、'.join(bounced)}" if bounced
                           else "，无未平打回"))

    # 占位符分布：模块与场景分开数，L3/L5 的分界信号
    mod_pending = {d.rel: placeholder_count(d.text) for d in corpus.modules}
    mod_pending = {k: v for k, v in mod_pending.items() if v}
    sc_pending = {d.rel: placeholder_count(d.text) for d in corpus.scenarios}
    sc_pending = {k: v for k, v in sc_pending.items() if v}

    # 全量校验（--fast 跳过，L4/L6 判定降级为启发式并标注）
    n_err = n_warn = None
    if args.fast:
        notes.append("--fast 模式：未跑全量校验，L4/L6 档位按工件启发式判定")
    else:
        issues = [i for fn in CHECKS.values() for i in fn(corpus)]
        n_err = sum(1 for i in issues if i.level == "ERROR")
        n_warn = sum(1 for i in issues if i.level == "WARN")
        evidence.append(f"全量校验: ERROR {n_err}，WARN {n_warn}")

    def counts_line() -> str:
        return (f"三态文档 {tri_total} 份 | 已冻结 {counts.get('已冻结', 0)} | "
                f"已评审 {counts.get('已评审', 0)} | 草稿 {counts.get('草稿', 0)}")

    # --- 判定级联。每一档把依据写进 evidence，两可时不假装确定 ---
    if bounced:
        return gear("L7", evidence=evidence, notes=notes, progress=counts_line())

    if baselines:
        evidence.append(counts_line())
        if counts.get("已评审"):
            return gear("L8", evidence=evidence + ["存在已评审文档：扫尾未完成"],
                        notes=notes, progress=counts_line())
        if tri_total and counts.get("已冻结", 0) == tri_total:
            return gear("L9", evidence=evidence + ["三态文档全部已冻结"],
                        notes=notes, progress=counts_line())
        return gear("L10", evidence=evidence + [
            f"草稿 {counts.get('草稿', 0)} 份：基线之后又有变更在迭代"],
            notes=notes, progress=counts_line())

    if mod_pending:
        top = sorted(mod_pending.items(), key=lambda kv: -kv[1])[:3]
        evidence.append("模块占位未清: " + "、".join(f"{k}({v})" for k, v in top))
        return gear("L3", evidence=evidence, notes=notes, progress=counts_line())

    if not corpus.scenarios:
        evidence.append("模块占位已清，场景文档尚缺")
        return gear("L4", evidence=evidence, notes=notes, progress=counts_line())
    if sc_pending:
        evidence.append("场景占位未清: " + "、".join(f"{k}({v})" for k, v in sc_pending.items()))
        return gear("L5", evidence=evidence, notes=notes, progress=counts_line())

    if n_err is not None and (n_err or n_warn):
        evidence.append("模块与场景齐备但校验未清零")
        return gear("L4", evidence=evidence, notes=notes, progress=counts_line())

    if counts.get("已评审"):
        return gear("L8", evidence=evidence + ["已有文档转已评审：扫尾进行中"],
                    notes=notes, progress=counts_line())
    if records:
        # 有全员通过的评审记录且无基线：评审已过，待执行转移
        return gear("L8", evidence=evidence + ["评审记录均已通过，状态转移未执行"],
                    notes=notes, progress=counts_line())
    evidence.append("--strict 口径全绿，全库草稿：备料完成，等评审")
    return gear("L6", evidence=evidence, notes=notes, progress=counts_line())


def gear(code: str, evidence: List[str], notes: List[str],
         progress: str = "", extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    name, round_, confidence, next_step = GEARS[code]
    out: Dict[str, Any] = {
        "gear": code, "name": name, "round": round_, "confidence": confidence,
        "progress": progress, "evidence": evidence, "notes": notes,
        "next": next_step,
    }
    if extra:
        out.update(extra)
    return out


def render_text(r: Dict[str, Any]) -> str:
    bar = "═" * 44
    lines = [bar, f"流水线档位: {r['gear']} · {r['name']}（{r['round']}）"]
    if r.get("progress"):
        lines.append(f"进度: {r['progress']}")
    if r["confidence"] == "启发式":
        lines.append("置信: 启发式（依据如下，两可时以证据为准）")
    if r["evidence"]:
        lines.append("判定依据:")
        lines.extend(f"  - {e}" for e in r["evidence"])
    for n in r["notes"]:
        lines.append(f"  ! {n}")
    lines.append(f"下一步: {r['next']}")
    lines.append(bar)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="流水线档位判定：项目走到哪一步、下一步是什么")
    ap.add_argument("--dir", required=True, help="文档目录或其上级（自动定位 需求文档/）")
    ap.add_argument("--state", help="interview-state.json 路径（默认自动查找）")
    ap.add_argument("--fast", action="store_true",
                    help="跳过全量校验，只按工件判定（L4/L6 降级为启发式）")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()

    result = detect(args)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
