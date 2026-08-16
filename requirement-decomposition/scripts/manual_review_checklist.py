#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人工复核清单：把「脚本管不住、需人工过」固化成一条命令。

校验器（validate_requirements.py）管得住结构硬错误：ID 悬空、NFR 漏项、
关系不对称、术语串味等。但有一类复核只能人做：前置条件能否被上游后置
满足、关系描述与模块正文是否一致、技术选型问题是否都进了移交事项、自然
语言禁用词自查、路由触发是否真按预期。这些散落在 SKILL.md 与 quality-gates.md
各处，靠人记得该查什么。本脚本读取文档库，把它们连同具体待复核内容列成
一份清单。

它不判对错、不报 ERROR/WARN，退出码恒为 0（用法错误才退 2）。

用法:
    python3 manual_review_checklist.py ./需求文档
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validate_requirements import (  # noqa: E402
    Corpus, RE_MOD, RE_UC, first_id, get_col, pick_table,
)

# 本脚本自用的表规约。不进 validate_requirements.TABLE_SPECS：校验器不查
# 这两张表（前置后置、移交事项是自由文本，机器判不了对错），加进去会让 S05
# 以为有检查项在用。规约在此自包含，S16 守脚本在空白骨架能跑通。
PREPOST_SPEC = (["前置与后置", "前置后置", "前后置"], ["类型", "条件"])
HANDOFF_SPEC = (["移交事项", "移交"], ["事项", "移交给"])

# 脚本不扫的禁用词（子串误伤，见 references/writing-style.md §2.2）。全文自查。
MANUAL_WORDS = ["大量", "少量", "若干", "比较", "可能会", "生态", "链路"]


def collect_prepost(c: Corpus) -> List[str]:
    """前置/后置条件：列每模块的前置与后置文本，供人核能否被上游满足。"""
    lines: List[str] = []
    for doc in c.modules:
        t = pick_table(c.tables[doc.rel], *PREPOST_SPEC)
        if t is None:
            continue
        pre = post = ""
        for row in t.rows:
            kind = get_col(row, "类型").strip()
            cond = get_col(row, "条件").strip()
            if "前置" in kind:
                pre = cond
            elif "后置" in kind:
                post = cond
        mid = first_id(os.path.basename(doc.rel), RE_MOD) or doc.rel
        lines.append(f"- {mid}（{doc.rel}）")
        lines.append(f"  - 前置：{pre or '（未填）'}")
        lines.append(f"  - 后置：{post or '（未填）'}")
        lines.append("  - 复核：前置能否被上游模块的后置条件满足，接不上即拆分有洞")
    return lines


def collect_relations(c: Corpus) -> List[str]:
    """关系描述与正文一致：列关系矩阵每条，指向双方模块文档。"""
    lines: List[str] = []
    for row in c.relations:
        src = get_col(row, "源模块", "源").strip()
        dst = get_col(row, "目标模块", "目标", "目的").strip()
        rtype = get_col(row, "关系类型", "类型").strip()
        desc = get_col(row, "关系描述", "描述").strip()
        lines.append(f"- {src} → {dst}（{rtype}）：{desc}")
        lines.append("  - 复核：描述与双方模块正文说的是否一回事")
    return lines


def collect_coupling(c: Corpus) -> List[str]:
    """耦合审计：列数据共享/事件触发/依赖的模块对，供人抽检未声明的耦合。

    关系矩阵只登记了已声明的耦合；没声明但实际存在的（隐式时序假设、正文里
    共享了数据却没登记）机器列不出来，只能人定期抽检。数据共享/事件触发/依赖
    是最容易产生隐式耦合的三类，列出来作为抽检对象。
    """
    lines: List[str] = []
    for row in c.relations:
        rtype = get_col(row, "关系类型", "类型").strip()
        if rtype not in ("数据共享", "事件触发", "依赖"):
            continue
        src = get_col(row, "源模块", "源").strip()
        dst = get_col(row, "目标模块", "目标", "目的").strip()
        lines.append(f"- {src} ↔ {dst}（{rtype}）")
    if lines:
        lines.append("  - 复核：通读双方模块正文，是否存在未在关系矩阵登记的"
                     "时序假设或数据耦合；发现即补录进矩阵并标置信度")
    return lines


def collect_confidence(c: Corpus) -> List[str]:
    """置信度判定依据：列推测/待定的关系，供人核描述里是否写了判定依据。"""
    lines: List[str] = []
    for row in c.relations:
        conf = get_col(row, "置信度").strip()
        if conf not in ("推测", "待定"):
            continue
        src = get_col(row, "源模块", "源").strip()
        dst = get_col(row, "目标模块", "目标", "目的").strip()
        desc = get_col(row, "关系描述", "描述").strip()
        lines.append(f"- {src} → {dst}（{conf}）：{desc}")
    if lines:
        lines.append("  - 复核：每条描述里是否写明了判定依据（谁判的、依据什么）；"
                     "没写的补依据，能确认的就升级为已证实")
    return lines


def collect_scenario_types(c: Corpus) -> List[str]:
    """场景类型覆盖：列每个场景的类型，供人核必盖类是否都盖了。"""
    lines: List[str] = []
    for doc in c.scenarios:
        t = pick_table(c.tables[doc.rel], ["场景信息"], ["项", "值"])
        stype = ""
        if t is not None:
            for row in t.rows:
                if get_col(row, "项").strip() == "场景类型":
                    stype = get_col(row, "值").strip()
        uid = first_id(os.path.basename(doc.rel), RE_UC) or doc.rel
        if not stype or stype.startswith("[待确认"):
            lines.append(f"- {uid}：场景类型未标注")
        else:
            lines.append(f"- {uid}：{stype}")
    if lines:
        lines.append("  - 复核：正常 / 备选 / 异常 / 边缘四类必盖；其余类型本期不涉及的"
                     "要声明理由，不许静默略过")
    return lines


def collect_handoff(c: Corpus) -> List[str]:
    """技术选型移交：列综述第 9 节移交事项，供人核挡回的选型是否都进了。"""
    lines: List[str] = []
    if c.overview:
        t = pick_table(c.tables[c.overview.rel], *HANDOFF_SPEC)
        if t:
            for row in t.rows:
                item = get_col(row, "事项").strip()
                if not item:
                    continue
                why = get_col(row, "为什么", "原因").strip()
                to = get_col(row, "移交给", "承接").strip()
                lines.append(f"- {item}（移交给 {to or '（未填）'}）：{why}")
            lines.append("  - 复核：过程中挡回去的技术选型问题是否都进了此表，"
                         "还是只在对话里说过就散了")
    return lines


def collect_status(c: Corpus) -> List[str]:
    """文档状态：按状态分组列出，供人核状态是否名副其实。

    C15 查取值合法与索引/文档两处一致，查不了「标着已评审的是不是真评审过」——
    评审是发生在文档之外的事实，机器只看得见那一格里写了什么。
    """
    from validate_requirements import VALID_DOC_STATUS

    adr_rels = {d.rel for d in c.decisions}
    snapshot_rel = c.snapshot.rel if c.snapshot else None
    grouped: Dict[str, List[str]] = {}
    for rel, (raw, _) in sorted(c.doc_status.items()):
        if rel in adr_rels or rel == snapshot_rel:
            continue
        grouped.setdefault(raw.strip(), []).append(rel)

    lines: List[str] = []
    hints = {
        "草稿": "确认没有下游已按它开工——草稿随时会变",
        "已评审": "确认真开过评审，且评审的就是当前这一版",
        "已冻结": "确认已进版本快照基线，且此后正文未再改动",
        "已废弃": "确认没有文档仍在引用它的 ID 与内容",
    }
    for status in VALID_DOC_STATUS:
        rels = grouped.pop(status, [])
        if not rels:
            continue
        lines.append(f"- **{status}**（{len(rels)} 份）：{'、'.join(rels)}")
        lines.append(f"  - 复核：{hints[status]}")
    for status, rels in sorted(grouped.items()):
        lines.append(f"- **{status or '（空）'}**（{len(rels)} 份）："
                     f"{'、'.join(rels)}")
        lines.append("  - 复核：取值不在词表内，C15 会报 ERROR")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(
        description="列出脚本管不到、需人工过的复核项")
    ap.add_argument("directory", help="需求文档根目录")
    args = ap.parse_args()

    if not os.path.isdir(args.directory):
        print(f"目录不存在: {args.directory}", file=sys.stderr)
        return 2

    c = Corpus(args.directory)
    if not c.docs:
        print(f"目录中没有 Markdown 文件: {args.directory}", file=sys.stderr)
        return 2

    out: List[str] = []
    out.append("# 人工复核清单")
    out.append("")
    out.append("下列项是脚本管不住、只能人过的复核。校验器管结构硬错误；"
               "这里列的是语义与判断。逐条过，确认即勾掉。")
    out.append("")

    out.append("## 1. 前置条件能否被上游后置满足")
    out.append("")
    ls = collect_prepost(c)
    out.append("\n".join(ls) if ls else "- （未解析到任何模块的前置/后置表）")
    out.append("")

    out.append("## 2. 关系描述与模块正文是否一致")
    out.append("")
    ls = collect_relations(c)
    out.append("\n".join(ls) if ls else "- （关系矩阵为空）")
    out.append("")

    out.append("## 3. 技术选型问题是否都进了移交事项")
    out.append("")
    ls = collect_handoff(c)
    out.append("\n".join(ls) if ls else "- （综述未解析到移交事项表）")
    out.append("")

    out.append("## 4. 机器已报的结构信号（定夺合并/拆分/异形）")
    out.append("")
    out.append("C12（模块体量）、C13（数据写主）能机器查，但报出来后该合并、拆分还是"
               "确认异形，仍由人定。跑校验器看 WARN 项：")
    out.append("")
    out.append("    python3 validate_requirements.py ./需求文档 --only C12,C13")
    out.append("")
    out.append("- C12 报存在感低 → 该模块是否应并入相邻模块")
    out.append("- C12 报存在感高 → 是否应拆出子模块")
    out.append("- C13 报两写主 → 是真冲突还是异形同义")
    out.append("")

    out.append("## 5. 文档状态是否名副其实")
    out.append("")
    out.append("C15 查得了取值合法与索引/文档两处一致，查不了「标着已评审的"
               "是不是真评审过」——那是一个发生在文档之外的事实。")
    out.append("")
    ls = collect_status(c)
    out.append("\n".join(ls) if ls else "- （未解析到任何文档的状态行）")
    out.append("")
    out.append("- 本轮影响分析若用了 `--cleared` 复核销项：确认销项连同结论已登记入"
               "「04-版本快照.md」的变更复核登记节。销项不跨变更集沿用，"
               "下一轮要重新复核、重新登记。")
    out.append("")

    out.append("## 6. 自然语言禁用词人工自查")
    out.append("")
    out.append("下列词脚本不扫（子串误伤，见 writing-style.md §2.2），全文自查：")
    out.append("、".join(MANUAL_WORDS))
    out.append("")

    out.append("## 7. 路由触发复核")
    out.append("")
    out.append("run_routing_evals.py 只校验用例结构，不验证模型是否真按预期触发。"
               "导出 prompt 清单逐条过：")
    out.append("")
    out.append("    python3 run_routing_evals.py --emit-prompts")
    out.append("")

    out.append("## 8. 耦合审计")
    out.append("")
    out.append("关系矩阵只登记了已声明的耦合。没声明但实际存在的（隐式时序假设、"
               "正文里共享了数据却没登记），机器列不出来，只能定期人抽检。")
    out.append("")
    ls = collect_coupling(c)
    out.append("\n".join(ls) if ls else "- （关系矩阵为空，暂无抽检对象）")
    out.append("")

    out.append("## 9. 验收标准的覆盖维度")
    out.append("")
    out.append("每条需求的验收标准应覆盖适用的维度（不是每条都全盖）：正常路径 / "
               "边界 / 错误处理 / 权限 / 并发 / 状态流转 / 幂等。逐需求问一遍："
               "这条的验收有没有漏掉该盖的维度？")
    out.append("")

    out.append("## 10. 置信度为推测/待定的关系是否写了判定依据")
    out.append("")
    out.append("规则（decomposition-rules.md 第 6 节）：推测/待定必须在描述里写明判定依据，"
               "已证实必须能指认依据。脚本只查取值合法，查不了依据是否真写了。")
    out.append("")
    ls = collect_confidence(c)
    out.append("\n".join(ls) if ls else "- （没有推测/待定态的关系）")
    out.append("")

    out.append("## 11. 场景类型覆盖")
    out.append("")
    out.append("SKILL.md 第 5 轮要求场景按 12 类核对覆盖：正常/备选/异常/边缘必盖，"
               "其余类型本期不涉及的声明理由。脚本只查 ID 与链接，查不了类型覆盖。")
    out.append("")
    ls = collect_scenario_types(c)
    out.append("\n".join(ls) if ls else "- （未解析到任何场景文档）")
    out.append("")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
