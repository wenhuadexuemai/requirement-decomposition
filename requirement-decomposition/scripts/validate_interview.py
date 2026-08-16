#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""骨架访谈契约校验。

检查第 2 轮访谈的状态文件、会话记录与骨架产物是否满足 references/interview-protocol.md
的契约：证据先于提问、一轮一个问题、推荐必带理由、恢复接续唯一的 next_question_id、
门禁全真才出稿、批准显式且早于交付、批准前不得展开目录。

只依赖 Python 标准库，失败即退出 1（fail closed）。

用法:
    python3 validate_interview.py --state interview-state.json --transcript transcript.json
    python3 validate_interview.py --state s.json --transcript t.json --skeleton skeleton.json
    python3 validate_interview.py --print-example        # 打印最小合法状态样例

退出码: 0 = 通过, 1 = 契约错误, 2 = 用法或读取错误
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

GATE_KEYS = {
    "module_boundaries",
    "data_ownership",
    "relations",
    "nfr_thresholds",
    "glossary_seed",
    "scenarios",
    "scope_non_goals",
    "open_questions",
}

STATE_KEYS = {
    "schema_version", "session_id", "skill_version", "mode", "status",
    "evidence", "decision_log", "unresolved_questions", "completion_gate",
    "skeleton", "hard_stop",
}

GATED_STATUSES = {"ready-for-skeleton", "awaiting-approval", "approved", "stopped"}
FINAL_STATUSES = {"approved", "stopped"}
VALID_STATUSES = {"evidence-gathering", "interviewing"} | GATED_STATUSES

# evidence.kind 与 unresolved_questions.category 的取值词表。schema 里这两个枚举是
# 纯字面约束（schema 不被脚本读取），这里放机器版并校验取值，S02 核对两处一致。
EVIDENCE_KINDS = {"fact", "user_decision", "assumption"}
QUESTION_CATEGORIES = {"module_boundary", "data_ownership", "relation_strength",
                       "nfr_threshold", "glossary", "scenario", "scope", "other"}

# 门禁字段不接受占位话术
PLACEHOLDER_RE = re.compile(r"\bTBD\b|\bTODO\b|待补充|后续确认|\[待确认", re.IGNORECASE)

# 批准前禁止的动作
ALWAYS_FORBIDDEN = {"write_code", "implement", "design_architecture", "create_branch", "commit"}
APPROVAL_GATED = {"scaffold", "expand_docs", "write_module_body", "write_scenario_body"}
FORBIDDEN_BEFORE_APPROVAL = ALWAYS_FORBIDDEN | APPROVAL_GATED

SCHEMA_VERSION = "1.0.0"

EXAMPLE_STATE = {
    "schema_version": SCHEMA_VERSION,
    "session_id": "sess-2026-08-08-order",
    "skill_version": "1.4.2",
    "mode": "skeleton-interview",
    "status": "interviewing",
    "next_question_id": "Q-02",
    "evidence": [
        {
            "path": "原始材料/需求评审纪要-0805.md",
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "summary": "确认下单与库存为两个团队维护，库存写主在库存服务",
            "kind": "fact",
        }
    ],
    "decision_log": [
        {
            "id": "D-01",
            "question_id": "Q-01",
            "decision": "订单创建与库存锁定拆为两个模块，库存为唯一写主",
            "rationale": "两侧「订单」语义不同，且库存数据写权限已归属库存服务",
            "source": "user",
            "affects": ["MOD-0001", "MOD-0002"],
        }
    ],
    "unresolved_questions": [
        {
            "id": "Q-02",
            "question": "支付超时的释放时限取 15 分钟还是 30 分钟？",
            "blocking": False,
            "recommendation": {
                "answer": "15 分钟",
                "reason": "与现有购物车锁定时长一致，避免两套超时口径",
            },
            "category": "nfr_threshold",
        }
    ],
    "completion_gate": {k: False for k in sorted(GATE_KEYS)},
    "skeleton": {
        "draft_path": None,
        "final_path": None,
        "approved_by_user": False,
        "module_count": 0,
    },
    "hard_stop": {"expansion_allowed": False, "implementation_allowed": False},
}


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def canonical_utf8_lf(data: bytes) -> bytes:
    """规范文本字节：严格 UTF-8 解码，CRLF/CR 归一为 LF，再编回 UTF-8。

    只归一换行，不做其他 Unicode 或空白归一化——保证跨平台摘要一致，
    同时除换行外的任何改动都能被检出。
    """
    text = data.decode("utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def load_json(path: str) -> Any:
    try:
        with open(path, "rb") as f:
            raw = f.read()
        return json.loads(canonical_utf8_lf(raw).decode("utf-8"))
    except FileNotFoundError:
        raise ValueError(f"{path}: 文件不存在")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: 无法读取 ({exc})")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: JSON 解析失败 ({exc})")


def question_count(text: str) -> int:
    return len(re.findall(r"[?？]", text or ""))


# --------------------------------------------------------------------------
# 状态校验
# --------------------------------------------------------------------------

def validate_state(state: Any, errors: List[str]) -> None:
    if not isinstance(state, dict):
        errors.append("状态文件顶层必须是对象")
        return

    missing = STATE_KEYS - state.keys()
    if missing:
        errors.append(f"状态缺少必需字段: {sorted(missing)}")
        return

    if state["schema_version"] != SCHEMA_VERSION:
        errors.append(f"schema_version 必须为 {SCHEMA_VERSION}")
    if state["mode"] != "skeleton-interview":
        errors.append("mode 必须为 skeleton-interview")
    if not str(state.get("session_id", "")).strip():
        errors.append("session_id 不得为空")
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(state.get("skill_version", ""))):
        errors.append("skill_version 需为 x.y.z 形式")

    status = state["status"]
    if status not in VALID_STATUSES:
        errors.append(f"status「{status}」非法，只能取 {sorted(VALID_STATUSES)}")

    evidence = state["evidence"]
    if not isinstance(evidence, list) or not evidence:
        errors.append("evidence 至少需要一条（无材料时记 evidence_unavailable 并写明原因）")
    else:
        for i, item in enumerate(evidence):
            if not isinstance(item, dict):
                errors.append(f"evidence[{i}] 必须是对象")
                continue
            sha = str(item.get("sha256", ""))
            if not re.fullmatch(r"[a-f0-9]{64}|evidence_unavailable", sha):
                errors.append(f"evidence[{i}] 的 sha256 非法（需 64 位十六进制或 evidence_unavailable）")
            if not str(item.get("path", "")).strip() or not str(item.get("summary", "")).strip():
                errors.append(f"evidence[{i}] 需同时给出 path 与 summary")
            if item.get("kind") is not None and item.get("kind") not in EVIDENCE_KINDS:
                errors.append(f"evidence[{i}] 的 kind 只能取 {sorted(EVIDENCE_KINDS)}")

    decisions = state["decision_log"]
    if not isinstance(decisions, list):
        errors.append("decision_log 必须是数组")
        decisions = []
    dids = [d.get("id") for d in decisions if isinstance(d, dict)]
    if len(dids) != len(set(dids)):
        errors.append("decision_log 的 id 必须唯一")
    for i, d in enumerate(decisions):
        if isinstance(d, dict) and d.get("source") not in {"user", "evidence", "assumption"}:
            errors.append(f"decision_log[{i}] 的 source 必须取 user / evidence / assumption")

    unresolved = state["unresolved_questions"]
    if not isinstance(unresolved, list):
        errors.append("unresolved_questions 必须是数组")
        unresolved = []
    uids = [u.get("id") for u in unresolved if isinstance(u, dict)]
    if len(uids) != len(set(uids)):
        errors.append("unresolved_questions 的 id 必须唯一")
    for i, u in enumerate(unresolved):
        if not isinstance(u, dict):
            errors.append(f"unresolved_questions[{i}] 必须是对象")
            continue
        if not isinstance(u.get("blocking"), bool):
            errors.append(f"unresolved_questions[{i}] 的 blocking 必须是布尔值")
        rec = u.get("recommendation")
        if rec is not None:
            if not isinstance(rec, dict) or not str(rec.get("answer", "")).strip() \
                    or not str(rec.get("reason", "")).strip():
                errors.append(f"unresolved_questions[{i}] 的 recommendation 需同时给出 answer 与 reason")
        for opt in ("owner", "needed_by", "resolves_when"):
            if opt in u and not isinstance(u.get(opt), str):
                errors.append(f"unresolved_questions[{i}] 的 {opt} 必须是字符串")
        cat = u.get("category")
        if cat is not None and cat not in QUESTION_CATEGORIES:
            errors.append(f"unresolved_questions[{i}] 的 category 只能取 {sorted(QUESTION_CATEGORIES)}")

    gate = state["completion_gate"]
    if not isinstance(gate, dict) or set(gate) != GATE_KEYS:
        errors.append(f"completion_gate 的键必须恰好为 {sorted(GATE_KEYS)}")
    else:
        for k, v in sorted(gate.items()):
            if isinstance(v, str):
                errors.append(f"门禁 {k} 填的是文本「{v}」，只能是布尔值")
            elif not isinstance(v, bool):
                errors.append(f"门禁 {k} 必须是布尔值")
        if status in GATED_STATUSES and not all(v is True for v in gate.values()):
            unmet = sorted(k for k, v in gate.items() if v is not True)
            errors.append(f"status 为 {status} 时门禁必须全部为真，未满足: {unmet}")

    blocking = [u.get("id") for u in unresolved
                if isinstance(u, dict) and u.get("blocking") is True]
    if blocking and status in GATED_STATUSES:
        errors.append(f"仍有阻塞未决项 {sorted(blocking)}，不得进入 {status}")

    hard_stop = state["hard_stop"]
    if not isinstance(hard_stop, dict):
        errors.append("hard_stop 必须是对象")
    else:
        if hard_stop.get("implementation_allowed") is not False:
            errors.append("hard_stop.implementation_allowed 必须恒为 false")
        expansion = hard_stop.get("expansion_allowed")
        if not isinstance(expansion, bool):
            errors.append("hard_stop.expansion_allowed 必须是布尔值")
        elif expansion is True and status not in FINAL_STATUSES:
            errors.append(f"status 为 {status} 时不得放开 expansion_allowed，展开目录需先获批准")

    # 骨架产物
    skeleton = state["skeleton"]
    if not isinstance(skeleton, dict):
        errors.append("skeleton 必须是对象")
    elif status in FINAL_STATUSES:
        if skeleton.get("approved_by_user") is not True:
            errors.append(f"status 为 {status} 时必须有用户的明确批准（approved_by_user=true）")
        if not skeleton.get("final_path"):
            errors.append(f"status 为 {status} 时必须给出 final_path")
    elif isinstance(skeleton, dict) and skeleton.get("approved_by_user") is True:
        errors.append(f"status 为 {status} 时不应已标记 approved_by_user")

    # next_question_id 指向
    nqid = state.get("next_question_id")
    if status == "interviewing":
        if not nqid:
            errors.append("status 为 interviewing 时必须给出唯一的 next_question_id")
        elif nqid not in set(uids):
            errors.append(f"next_question_id「{nqid}」不在 unresolved_questions 中")


# --------------------------------------------------------------------------
# 会话记录校验
# --------------------------------------------------------------------------

def validate_transcript(state: Dict[str, Any], transcript: Any,
                        transcript_dir: str, errors: List[str]) -> None:
    if not isinstance(transcript, dict):
        errors.append("会话记录顶层必须是对象")
        return
    if transcript.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"会话记录 schema_version 必须为 {SCHEMA_VERSION}")
    if transcript.get("session_id") != state.get("session_id"):
        errors.append("会话记录的 session_id 与状态不一致")

    events = transcript.get("events")
    if not isinstance(events, list) or not events:
        errors.append("会话记录 events 必须是非空数组")
        return

    seqs = [e.get("seq") for e in events if isinstance(e, dict)]
    if seqs != list(range(1, len(events) + 1)):
        errors.append("会话记录的 seq 必须从 1 起连续递增")

    seen_questions: Dict[str, int] = {}
    answered: set = set()
    first_question_seq: Optional[int] = None
    draft_seq: Optional[int] = None
    approval_seq: Optional[int] = None
    resume_expected: Optional[str] = None

    for event in events:
        if not isinstance(event, dict):
            errors.append("每个会话事件都必须是对象")
            continue
        seq = event.get("seq")
        kind = event.get("kind")
        actor = event.get("actor")

        if kind == "question":
            if first_question_seq is None:
                first_question_seq = seq
            qid = event.get("question_id")
            if not qid or qid in seen_questions:
                errors.append(f"事件 {seq}: question_id 缺失或重复使用")
            else:
                seen_questions[qid] = seq
            n = question_count(str(event.get("question", "")))
            if n != 1:
                errors.append(f"事件 {seq}: 一个提问回合必须恰好一句问句，当前 {n} 句")
            rec = event.get("recommendation") or {}
            if not isinstance(rec, dict) or not str(rec.get("answer", "")).strip() \
                    or not str(rec.get("reason", "")).strip():
                errors.append(f"事件 {seq}: 提问必须同时给出推荐答案与理由")
            if not event.get("evidence"):
                errors.append(f"事件 {seq}: 提问必须说明已知证据与为何仍未决")
            if resume_expected and qid != resume_expected:
                errors.append(f"事件 {seq}: 恢复后应接续 {resume_expected}，实际问的是 {qid}")
            resume_expected = None

        elif kind == "answer":
            qid = event.get("question_id")
            if actor != "user":
                errors.append(f"事件 {seq}: answer 必须来自 user")
            elif qid not in seen_questions:
                errors.append(f"事件 {seq}: answer 未对应任何在先的提问")
            else:
                answered.add(qid)

        elif kind == "resume":
            if actor != "assistant":
                errors.append(f"事件 {seq}: resume 必须是 assistant 事件")
            if resume_expected:
                errors.append(f"事件 {seq}: 上一条 resume 期望接续 {resume_expected}，"
                              f"但未提问就又 resume")
            resume_expected = event.get("from_next_question_id")
            state_path = event.get("state_path")
            state_sha = event.get("state_sha256")
            if not resume_expected or not state_path or not state_sha:
                errors.append(f"事件 {seq}: resume 需同时记录状态路径、SHA-256 与 next_question_id")
            else:
                try:
                    with open(os.path.join(transcript_dir, state_path), "rb") as f:
                        raw = f.read()
                    canonical = canonical_utf8_lf(raw)
                    if hashlib.sha256(canonical).hexdigest() != state_sha:
                        errors.append(f"事件 {seq}: resume 记录的状态摘要与文件实际内容不符")
                    resumed = json.loads(canonical.decode("utf-8"))
                    if resumed.get("session_id") != state.get("session_id"):
                        errors.append(f"事件 {seq}: resume 状态的 session_id 不匹配")
                    if resumed.get("next_question_id") != resume_expected:
                        errors.append(f"事件 {seq}: resume 状态的 next_question_id 不匹配")
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    errors.append(f"事件 {seq}: 无法加载 resume 状态 ({exc})")

        elif kind == "skeleton_draft":
            draft_seq = seq
            if event.get("gate_passed") is not True:
                errors.append(f"事件 {seq}: 门禁未通过就产出了骨架草稿")
            n = question_count(str(event.get("question", "")))
            if n != 1:
                errors.append(f"事件 {seq}: 请求批准的回合必须恰好一句问句，当前 {n} 句")
            rec = event.get("recommendation") or {}
            if not isinstance(rec, dict) or not str(rec.get("answer", "")).strip() \
                    or not str(rec.get("reason", "")).strip():
                errors.append(f"事件 {seq}: 请求批准同样需要推荐答案与理由")

        elif kind == "approval":
            if actor != "user" or event.get("approved") is not True:
                errors.append(f"事件 {seq}: 批准必须来自 user 且为显式的 approved=true")
            approval_seq = seq

        elif kind == "final":
            if approval_seq is None or approval_seq >= seq:
                errors.append(f"事件 {seq}: 交付骨架前必须有在先的显式批准")
            actions = set(event.get("actions") or [])
            always = sorted(actions & ALWAYS_FORBIDDEN)
            if always:
                errors.append(f"事件 {seq}: final 含永远禁止的动作 {always}"
                              f"（实现未批准，硬停止）")
            if event.get("implementation_started") is not False:
                errors.append(f"事件 {seq}: implementation_started 必须为 false")

        # 批准之前不得出现展开或实现动作
        if kind not in {"final", "approval"}:
            actions = set(event.get("actions") or [])
            forbidden = sorted(actions & FORBIDDEN_BEFORE_APPROVAL)
            if forbidden and (approval_seq is None or seq <= approval_seq):
                errors.append(f"事件 {seq}: 批准之前不得执行 {forbidden}（硬停止）")

    if first_question_seq is not None:
        prior = [e for e in events
                 if isinstance(e, dict) and e.get("kind") == "evidence_read"
                 and (e.get("seq") or 0) < first_question_seq]
        if not prior:
            errors.append("第一个提问之前必须先有 evidence_read 事件（证据优先）")

    if draft_seq is not None and (approval_seq is None or approval_seq <= draft_seq):
        errors.append("骨架草稿之后必须有显式批准，沉默不视为批准")

    if resume_expected:
        errors.append(f"resume 事件之后没有接续它记录的 {resume_expected}")

    logged = {d.get("question_id") for d in state.get("decision_log", [])
              if isinstance(d, dict) and d.get("question_id")}
    missing = sorted(answered - logged)
    if missing:
        errors.append(f"已回答但未进决策记录的问题: {missing}")


# --------------------------------------------------------------------------
# 骨架产物校验
# --------------------------------------------------------------------------

def validate_skeleton(state: Dict[str, Any], skeleton: Any, errors: List[str]) -> None:
    if state.get("status") not in FINAL_STATUSES:
        return
    if not isinstance(skeleton, dict):
        errors.append("骨架 JSON 顶层必须是对象")
        return

    for key in ("project", "modules", "nfrs", "scenarios", "relations"):
        if key not in skeleton:
            errors.append(f"骨架缺少字段 {key}")

    modules = skeleton.get("modules") or []
    if not modules:
        errors.append("骨架的 modules 不得为空")

    declared = state.get("skeleton", {}).get("module_count")
    if isinstance(declared, int) and declared != len(modules):
        errors.append(f"状态记录 module_count={declared}，骨架实际 {len(modules)} 个模块")

    scenarios = skeleton.get("scenarios") or []
    if len(scenarios) < 3:
        errors.append(f"骨架仅 {len(scenarios)} 条场景，要求覆盖 3~5 条核心旅程")

    # 关系两端必须是已登记模块
    mod_ids = {str(m.get("id", "")).strip() for m in modules if isinstance(m, dict)}
    for i, r in enumerate(skeleton.get("relations") or []):
        if not isinstance(r, dict):
            errors.append(f"relations[{i}] 必须是对象")
            continue
        for side in ("source", "target"):
            mid = str(r.get(side, "")).strip()
            if mid and mid not in mod_ids:
                errors.append(f"relations[{i}] 的 {side}={mid} 未登记在 modules 中")

    # 门禁字段不接受占位话术
    for i, n in enumerate(skeleton.get("nfrs") or []):
        if isinstance(n, dict) and PLACEHOLDER_RE.search(str(n.get("threshold", ""))):
            errors.append(f"nfrs[{i}] 的阈值仍是占位内容，定稿骨架不接受 TBD / 待补充")


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="骨架访谈契约校验")
    ap.add_argument("--state", help="访谈状态 JSON")
    ap.add_argument("--transcript", help="会话记录 JSON")
    ap.add_argument("--skeleton", help="骨架 JSON（状态为 approved/stopped 时必检）")
    ap.add_argument("--print-example", action="store_true", help="打印最小合法状态样例后退出")
    args = ap.parse_args()

    if args.print_example:
        print(json.dumps(EXAMPLE_STATE, ensure_ascii=False, indent=2))
        return 0
    if not args.state or not args.transcript:
        ap.error("需同时提供 --state 与 --transcript（或使用 --print-example）")

    errors: List[str] = []
    try:
        state = load_json(args.state)
        transcript = load_json(args.transcript)
        validate_state(state, errors)
        if isinstance(state, dict):
            validate_transcript(state, transcript,
                                os.path.dirname(os.path.abspath(args.transcript)), errors)
            if args.skeleton:
                validate_skeleton(state, load_json(args.skeleton), errors)
            elif state.get("status") in FINAL_STATUSES:
                errors.append(f"status 为 {state['status']} 时需用 --skeleton 提供骨架产物一并校验")
    except ValueError as exc:
        print(f"错误 {exc}", file=sys.stderr)
        return 2

    if errors:
        for e in errors:
            print(f"错误 {e}")
        print(f"未通过，{len(errors)} 项契约错误")
        return 1
    print("通过 骨架访谈契约")
    return 0


if __name__ == "__main__":
    sys.exit(main())
