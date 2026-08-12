#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""触发边界用例的结构校验。

单技能包内只能做静态检查：schema 合法、id 唯一、prompt 不重复、负例给了去向、
边界例写了理由。**它不验证模型是否真的按预期触发** —— 那需要跑真实会话，
用 --emit-prompts 导出人工或 agent 复核用的清单。

改动 SKILL.md 的 description 之后应当跑一次，确认负例仍然写得住。

用法:
    python3 run_routing_evals.py
    python3 run_routing_evals.py --file evals/routing-evals.json
    python3 run_routing_evals.py --emit-prompts        # 导出待人工复核的触发清单

退出码: 0 = 通过, 1 = 存在错误, 2 = 用法或读取错误
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_EVALS = os.path.join(SKILL_ROOT, "evals", "routing-evals.json")
SKILL_MD = os.path.join(SKILL_ROOT, "SKILL.md")

REQUIRED = {"id", "prompt", "should_trigger", "reason"}
OPTIONAL = {"route_to", "boundary", "expected_mode"}
KNOWN_MODES = {"skeleton-interview"}
ID_RE = re.compile(r"^[a-z]{2,4}-\d{2,}$")

# route_to 只能填能力类别，不能填某个具体技能／助手／产品的名字。
# 本技能包不假设自己身处哪一套技能生态，写死兄弟技能名会让负例在
# 换环境后集体失真——那时 route_to 指向的东西根本不存在，边界却看着还在。
ROUTE_CATEGORIES = {
    "none",                 # 无需转交，这类请求本就不该产出需求文档网络
    "single-document",      # 一份文档写完更划算，拆网络是净亏
    "architecture-design",  # 技术方案、选型、分层、接口技术契约
    "product-strategy",     # 产品策略、增长、指标诊断
    "market-research",      # 多源引用的调研
    "implementation",       # 写代码、建表、接口实现
}


def load(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"用例文件不存在: {path}", file=sys.stderr)
        raise SystemExit(2)
    except json.JSONDecodeError as exc:
        print(f"用例 JSON 解析失败: {exc}", file=sys.stderr)
        raise SystemExit(2)


def check(cases: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(cases, list) or not cases:
        return ["用例文件顶层必须是非空数组"]

    seen_ids: Dict[str, int] = {}
    seen_prompts: Dict[str, str] = {}
    positives = negatives = boundaries = 0

    for i, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"用例[{i}] 必须是对象")
            continue

        missing = REQUIRED - case.keys()
        if missing:
            errors.append(f"用例[{i}] 缺少字段: {sorted(missing)}")
            continue
        unknown = case.keys() - REQUIRED - OPTIONAL
        if unknown:
            errors.append(f"用例[{i}] 出现未知字段: {sorted(unknown)}")

        cid = str(case["id"])
        if not ID_RE.fullmatch(cid):
            errors.append(f"用例[{i}] 的 id「{cid}」格式应为 前缀-序号，如 rd-01")
        if cid in seen_ids:
            errors.append(f"id {cid} 重复（另见用例[{seen_ids[cid]}]）")
        seen_ids[cid] = i

        prompt = " ".join(str(case["prompt"]).split())
        if not prompt:
            errors.append(f"{cid}: prompt 不得为空")
        elif prompt in seen_prompts:
            errors.append(f"{cid}: prompt 与 {seen_prompts[prompt]} 完全重复")
        else:
            seen_prompts[prompt] = cid

        if not isinstance(case["should_trigger"], bool):
            errors.append(f"{cid}: should_trigger 必须是布尔值")
        if not str(case["reason"]).strip():
            errors.append(f"{cid}: reason 不得为空")

        if case["should_trigger"] is True:
            positives += 1
            if case.get("route_to") and case.get("route_to") != "none" and not case.get("boundary"):
                errors.append(f"{cid}: 正例不应指定 route_to（除非标了 boundary）")
        else:
            negatives += 1
            if not case.get("route_to"):
                errors.append(f"{cid}: 负例必须给出 route_to（无处可去时填 none）")

        route = case.get("route_to")
        if route is not None and route not in ROUTE_CATEGORIES:
            errors.append(
                f"{cid}: route_to「{route}」不在能力类别词表内，"
                f"只能取 {sorted(ROUTE_CATEGORIES)}。填具体技能名会把本包"
                f"绑死在某套技能生态上")

        if case.get("boundary") is True:
            boundaries += 1
            if len(str(case["reason"])) < 20:
                errors.append(f"{cid}: 边界用例的 reason 需说清两可之处与倾向")

        mode = case.get("expected_mode")
        if mode is not None:
            if mode not in KNOWN_MODES:
                errors.append(f"{cid}: expected_mode「{mode}」未知，已知 {sorted(KNOWN_MODES)}")
            if case["should_trigger"] is not True:
                errors.append(f"{cid}: 负例不应声明 expected_mode")

    if positives == 0:
        errors.append("缺少正例：至少需要一条 should_trigger=true")
    if negatives == 0:
        errors.append("缺少负例：不写负例就测不出触发边界")
    if boundaries < 2:
        errors.append(
            f"边界用例仅 {boundaries} 条，至少 2 条。本技能的边界至少有两侧："
            "向上是架构设计（技术方案不归它），向下是文档规模（一份文档够用时不归它）。"
            "只写一侧，另一侧的误触发无人把守")

    negative_routes = {c.get("route_to") for c in cases
                       if isinstance(c, dict) and c.get("should_trigger") is False}
    negative_routes.discard(None)
    if len(negative_routes) < 3:
        errors.append(
            f"负例只覆盖了 {sorted(negative_routes)} 共 {len(negative_routes)} 类去向，"
            "至少 3 类。去向单一说明只测了一个方向的误触发")

    return errors


def check_against_skill() -> List[str]:
    """核对 SKILL.md 的 description：长度上限与负触发范围是否写明。"""
    warnings: List[str] = []
    if not os.path.exists(SKILL_MD):
        return warnings
    with open(SKILL_MD, "r", encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"^description:\s*(.*?)(?=^\w+:|^---)", text, re.M | re.S)
    if not m:
        return ["SKILL.md 未解析到 description，无法交叉核对"]
    desc = " ".join(m.group(1).split())
    if len(desc) > 1024:
        warnings.append(f"SKILL.md 的 description 长度 {len(desc)} 超过 1024 字符上限")
    if "不用于" not in desc and "不适用" not in desc:
        warnings.append("SKILL.md 的 description 未写负触发范围，触发边界只靠正例撑不住")
    return warnings


def emit_prompts(cases: List[Dict[str, Any]]) -> str:
    lines = ["# 触发边界复核清单", "",
             "逐条在干净会话里发出 prompt，记录技能是否被触发，与「期望」列比对。", ""]
    lines.append("| 用例 | prompt | 期望 | 去向 |")
    lines.append("|------|--------|------|------|")
    for c in cases:
        expect = "触发" if c["should_trigger"] else "不触发"
        if c.get("expected_mode"):
            expect += f"（{c['expected_mode']}）"
        if c.get("boundary"):
            expect += " · 两可"
        route = c.get("route_to") or "——"
        prompt = str(c["prompt"]).replace("|", "\\|")
        lines.append(f"| {c['id']} | {prompt} | {expect} | {route} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="触发边界用例结构校验")
    ap.add_argument("--file", default=DEFAULT_EVALS, help="用例文件路径")
    ap.add_argument("--emit-prompts", action="store_true", help="导出人工复核清单")
    args = ap.parse_args()

    cases = load(args.file)
    errors = check(cases)

    if args.emit_prompts:
        if errors:
            print("用例存在错误，先修复再导出：", file=sys.stderr)
            for e in errors:
                print(f"  错误 {e}", file=sys.stderr)
            return 1
        print(emit_prompts(cases))
        return 0

    warnings = check_against_skill() if not errors else []

    total = len(cases) if isinstance(cases, list) else 0
    pos = sum(1 for c in cases if isinstance(c, dict) and c.get("should_trigger") is True)
    print(f"用例 {total} 条（正例 {pos}，负例 {total - pos}）")
    for w in warnings:
        print(f"警告 {w}")
    for e in errors:
        print(f"错误 {e}")
    if errors:
        print(f"未通过，{len(errors)} 项错误")
        return 1
    print("通过 结构校验（触发行为需用 --emit-prompts 人工复核）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
