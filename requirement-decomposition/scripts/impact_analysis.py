#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""需求文档变更影响分析。

依据综述关系矩阵从「本次变更了哪些文档」推导「哪些文档也该跟着改」，
再检查它们是否真的在同一次变更里动过。用于阻断「改了一处、忘了另一处」。

变更集是本脚本的输入。取变更集有两条路，git 只是其中之一：

    # 一、从 git 取（默认，需在仓库内）
    python3 impact_analysis.py --dir ./需求文档 --base origin/main
    python3 impact_analysis.py --dir ./需求文档 --base HEAD~1 --format markdown

    # 二、直接给文件清单（无 VCS、用 SVN、或变更集来自别处时）
    python3 impact_analysis.py --dir ./需求文档 --changed-files changed.txt
    git diff --name-only main | python3 impact_analysis.py --dir ./需求文档 --changed-files -

--changed-files 接一个每行一个路径的文本文件，`-` 表示从标准输入读。
路径相对仓库根或相对文档目录都认。

邻接传播是粗粒度的：改了模块，它的全部邻接模块都进「受影响」清单。人工复核
后确认无需改动的，走 --cleared 销项，而不是空转版本号让门禁闭嘴——空转进位
正好腐蚀 C11 的进位判据（读者是否需要重读）：

    python3 impact_analysis.py --dir ./需求文档 --base origin/main \
        --fail-on-unsynced --cleared cleared.txt

--cleared 清单每行一个文档路径（口径同变更清单）或 MOD/UC/ADR 的 ID，`-` 表示
标准输入。判定由此从二值（改了/没改）补成三值（改了/核过无需改/没核）。销项
只抵扣「受影响但未同步」；「状态未回退」是更硬的违规，复核结论豁免不了。销项
不跨变更集粘连：它是每次运行的输入，工具不记历史，里程碑级登记用版本快照文档
的「变更复核登记」节。

除「该改而没改」之外，还查「改了但状态没退」：正文改动后状态仍挂在已评审／
已冻结的文档会单独列出。这是文档生命周期回路上唯一的自动强制点——正向路径
靠人记得，回退只能靠机器盯。

退出码: 0 = 无未同步项（或已全部销项）,
        1 = 存在未同步文档或状态未回退（需 --fail-on-unsynced）, 2 = 用法或环境错误
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from typing import Dict, List, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validate_requirements import (  # noqa: E402
    FROZEN_DOC_STATUS, RE_ADR, RE_MOD, RE_UC, RELATION_COLUMNS, Corpus,
    first_id, get_col, strip_code_blocks,
)


def git_diff_files(base: str, repo: str) -> List[str]:
    """从 git 取变更集。默认实现，另一条路见 read_changed_files。"""
    # core.quotepath=false 保证中文路径原样输出，否则会被转义成 \346\226\207 形式
    prefix = ["git", "-C", repo, "-c", "core.quotepath=false"]
    try:
        proc = subprocess.run(prefix + ["diff", "--name-only", f"{base}...HEAD"],
                              capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            proc = subprocess.run(prefix + ["diff", "--name-only", base],
                                  capture_output=True, text=True, check=True)
        return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    except FileNotFoundError:
        print("未找到 git 命令。不在 git 仓库中时，改用 --changed-files 直接给变更清单",
              file=sys.stderr)
        raise SystemExit(2)
    except subprocess.CalledProcessError as exc:
        print(f"git diff 失败: {exc.stderr or exc}\n"
              f"若本目录不是 git 仓库，改用 --changed-files 直接给变更清单",
              file=sys.stderr)
        raise SystemExit(2)


def read_line_list(source: str, what: str) -> List[str]:
    """从文件或标准输入读每行一项的清单。`-` 表示标准输入。

    变更集与销项清单都是本脚本的输入，不该被 VCS 绑死：SVN、无版本控制的
    共享目录、从别处导出的清单，都走这个入口。
    """
    try:
        if source == "-":
            raw = sys.stdin.read()
        else:
            with open(source, "r", encoding="utf-8") as f:
                raw = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        print(f"无法读取{what} {source}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    out: List[str] = []
    for line in raw.splitlines():
        line = line.strip().strip('"')
        if line and not line.startswith("#"):
            out.append(line)
    return out


def build_adjacency(corpus: Corpus) -> Dict[str, Set[Tuple[str, str]]]:
    """模块 ID -> {(相邻模块, 关系类型)}，按无向处理。

    列名取自 validate_requirements.RELATION_COLUMNS，与 C05 共用一份，不再
    硬编码别名。改关系矩阵表头时 S05 会同时核对 C05 与本函数。
    """
    adj: Dict[str, Set[Tuple[str, str]]] = defaultdict(set)
    for row in corpus.relations:
        src = first_id(get_col(row, *RELATION_COLUMNS["source"]), RE_MOD)
        dst = first_id(get_col(row, *RELATION_COLUMNS["target"]), RE_MOD)
        rtype = get_col(row, *RELATION_COLUMNS["type"]).strip() or "未标注"
        if src and dst:
            adj[src].add((dst, rtype))
            adj[dst].add((src, rtype))
    return adj


def scenarios_of(corpus: Corpus, mod_ids: Set[str]) -> Dict[str, Set[str]]:
    """受影响模块 -> 涉及它的场景文档相对路径。"""
    hit: Dict[str, Set[str]] = defaultdict(set)
    for sc in corpus.scenarios:
        # 跳过代码块，避免 Mermaid 示例中的占位 ID 造成误判
        for mid in set(RE_MOD.findall(strip_code_blocks(sc.text))):
            if mid in mod_ids:
                hit[mid].add(sc.rel)
    return hit


def decisions_of(corpus: Corpus, mod_ids: Set[str]) -> Dict[str, Set[str]]:
    """受影响模块 -> 点名它的决策记录相对路径。"""
    hit: Dict[str, Set[str]] = defaultdict(set)
    for adr in getattr(corpus, "decisions", []):
        for mid in set(RE_MOD.findall(strip_code_blocks(adr.text))):
            if mid in mod_ids:
                hit[mid].add(adr.rel)
    return hit


def modules_of_decisions(corpus: Corpus, adr_paths: Set[str]) -> Dict[str, Set[str]]:
    """已变更的决策记录 -> 它点名的模块文档相对路径。"""
    hit: Dict[str, Set[str]] = defaultdict(set)
    for adr in getattr(corpus, "decisions", []):
        if adr.rel not in adr_paths:
            continue
        for mid in set(RE_MOD.findall(strip_code_blocks(adr.text))):
            doc = corpus.module_doc(mid)
            if doc:
                hit[adr.rel].add(doc.rel)
    return hit


def resolve_cleared(corpus: Corpus, entries: List[str],
                    doc_root: str) -> Tuple[Set[str], List[str]]:
    """销项清单 -> (解析出的文档相对路径集合, 提示条目)。

    每行认两种写法：文档路径（相对仓库根或相对文档目录，与变更清单同口径，
    再兜底按文件名匹配），或文档级 ID（MOD/UC/ADR）。NFR/TERM 之类活在其他
    文档内部的 ID 定位不到一份文档，整条不匹配。

    解析失败只提示不报错退出：匹配不上意味着对应文档仍留在未同步清单里，
    门禁照败，天然 fail closed。提示是为了让人看见清单里那条错别字。
    """
    resolved: Set[str] = set()
    notes: List[str] = []
    for entry in entries:
        rel = ""
        if first_id(entry, RE_MOD) == entry:
            doc = corpus.module_doc(entry)
            rel = doc.rel if doc else ""
        elif first_id(entry, RE_UC) == entry:
            rel = next((d.rel for d in corpus.scenarios
                        if first_id(os.path.basename(d.rel), RE_UC) == entry), "")
        elif first_id(entry, RE_ADR) == entry:
            rel = next((d.rel for d in corpus.decisions
                        if first_id(os.path.basename(d.rel), RE_ADR) == entry), "")
        else:
            p = os.path.normpath(entry)
            candidates = [p]
            if doc_root not in (".", ""):
                if p.startswith(doc_root + os.sep):
                    candidates.insert(0, os.path.relpath(p, doc_root))
                else:
                    candidates.append(os.path.join(doc_root, p))
            rel = next((c for c in candidates if c in corpus.by_rel), "")
            if not rel:
                base_hits = [d.rel for d in corpus.docs
                             if os.path.basename(d.rel) == os.path.basename(p)]
                if len(base_hits) == 1:
                    rel = base_hits[0]
        if rel:
            resolved.add(rel)
        else:
            notes.append(f"「{entry}」无法解析为库内文档，已忽略"
                         f"（它若真受影响，仍会报在未同步清单里）")
    return resolved, notes


def main() -> int:
    ap = argparse.ArgumentParser(description="需求文档变更影响分析")
    ap.add_argument("--dir", required=True, help="需求文档根目录")
    ap.add_argument("--base", default="origin/main", help="对比基线，默认 origin/main")
    ap.add_argument("--repo", default=".", help="Git 仓库根目录，默认当前目录")
    ap.add_argument("--changed-files", metavar="路径",
                    help="变更文件清单（每行一个路径，- 表示标准输入）。"
                         "给了它就不调 git，用于无 VCS 或非 git 的环境")
    ap.add_argument("--cleared", metavar="路径",
                    help="已复核确认无需改动的文档清单（每行一个路径或 MOD/UC/ADR 的 ID，"
                         "- 表示标准输入）。只抵扣「受影响但未同步」，不豁免「状态未回退」")
    ap.add_argument("--depth", type=int, default=1, help="关系传播跳数，默认 1")
    ap.add_argument("--format", choices=["text", "markdown"], default="text")
    ap.add_argument("--fail-on-unsynced", action="store_true",
                    help="存在未同步文档时以退出码 1 结束")
    args = ap.parse_args()

    if args.changed_files == "-" and args.cleared == "-":
        print("--changed-files 与 --cleared 不能同时从标准输入读", file=sys.stderr)
        return 2

    if not os.path.isdir(args.dir):
        print(f"目录不存在: {args.dir}", file=sys.stderr)
        return 2

    corpus = Corpus(args.dir)

    if args.changed_files:
        changed_all = read_line_list(args.changed_files, "变更清单")
        source_label = f"清单 {args.changed_files}"
        # 清单模式不要求处于 git 仓库内，路径按文档目录自身解析
        doc_root = os.path.relpath(os.path.realpath(args.dir), os.path.realpath(args.repo))
        if doc_root.startswith(".."):
            doc_root = "."
    else:
        changed_all = git_diff_files(args.base, args.repo)
        source_label = f"基线 {args.base}"
        # realpath 而非 abspath：macOS 上 /tmp 之类的符号链接会让前缀匹配失效
        doc_root = os.path.relpath(os.path.realpath(args.dir), os.path.realpath(args.repo))
        if doc_root.startswith(".."):
            print(f"文档目录不在仓库内: {args.dir} 不属于 {args.repo}\n"
                  f"改用 --changed-files 直接给变更清单，或用 --repo 指向正确的仓库根",
                  file=sys.stderr)
            return 2

    # 路径归一到「相对文档目录」。相对仓库根与相对文档目录两种写法都认，
    # 手写清单时都常见，认死一种只会让人反复试。
    known = set(corpus.by_rel)
    changed_rel: Set[str] = set()
    for p in changed_all:
        if not p.endswith(".md"):
            continue
        p = os.path.normpath(p)
        candidates = [p]
        if doc_root not in (".", ""):
            if p.startswith(doc_root + os.sep):
                candidates.insert(0, os.path.relpath(p, doc_root))
            else:
                candidates.append(os.path.join(doc_root, p))
        hit = next((c for c in candidates if c in known), None)
        if hit:
            changed_rel.add(hit)
        elif doc_root in (".", "") or p.startswith(doc_root + os.sep):
            # 落在文档目录内但已被删除的文件：仍要参与影响推导
            changed_rel.add(os.path.relpath(p, doc_root) if doc_root not in (".", "") else p)

    if not changed_rel:
        print(f"按 {source_label} 比对，没有需求文档发生变更。")
        return 0

    changed_docs = sorted(changed_rel)
    changed_mods = {first_id(os.path.basename(p), RE_MOD) for p in changed_docs}
    changed_mods.discard("")
    changed_ucs = {first_id(os.path.basename(p), RE_UC) for p in changed_docs}
    changed_ucs.discard("")

    overview_changed = bool(corpus.overview and corpus.overview.rel in changed_rel)
    glossary_changed = bool(corpus.glossary and corpus.glossary.rel in changed_rel)

    adj = build_adjacency(corpus)
    frontier = set(changed_mods)
    visited: Set[str] = set(changed_mods)
    impacted: Dict[str, List[str]] = defaultdict(list)
    for hop in range(1, max(1, args.depth) + 1):
        nxt: Set[str] = set()
        for mid in frontier:
            for neighbor, rtype in adj.get(mid, set()):
                if neighbor in visited:
                    continue
                suffix = f"{mid} 通过「{rtype}」关联"
                impacted[neighbor].append(suffix if hop == 1 else f"{suffix}（第 {hop} 跳）")
                nxt.add(neighbor)
        visited |= nxt
        frontier = nxt
        if not frontier:
            break

    sc_hits = scenarios_of(corpus, changed_mods)
    adr_hits = decisions_of(corpus, changed_mods)
    changed_adrs = {rel for rel in changed_rel
                    if first_id(os.path.basename(rel), RE_ADR)}
    adr_targets = modules_of_decisions(corpus, changed_adrs)

    unsynced: List[Tuple[str, str]] = []
    for mid, reasons in sorted(impacted.items()):
        doc = corpus.module_doc(mid)
        rel = doc.rel if doc else f"modules/{mid}(缺失)"
        if rel not in changed_rel:
            unsynced.append((rel, "；".join(sorted(set(reasons)))))
    for mid, paths in sorted(sc_hits.items()):
        for rel in sorted(paths):
            uc = first_id(os.path.basename(rel), RE_UC)
            if rel not in changed_rel and uc not in changed_ucs:
                unsynced.append((rel, f"包含已变更的 {mid}，需复核步骤编号与数据传递"))
    # 模块变了 → 点名它的决策记录需复核；决策变了 → 它点名的模块需复核
    for mid, paths in sorted(adr_hits.items()):
        for rel in sorted(paths):
            if rel not in changed_rel:
                unsynced.append((rel, f"该决策点名了已变更的 {mid}，需复核决策是否仍然成立"))
    for adr_rel, paths in sorted(adr_targets.items()):
        aid = first_id(os.path.basename(adr_rel), RE_ADR)
        for rel in sorted(paths):
            if rel not in changed_rel:
                unsynced.append((rel, f"{aid} 决策已变更，需复核本模块是否受影响"))
    if changed_mods and not overview_changed:
        ov = corpus.overview.rel if corpus.overview else "00-综述.md"
        unsynced.append((ov, "模块内容变更但综述未更新，需复核模块索引、关系矩阵与结构图"))
    if changed_mods and corpus.traceability and corpus.traceability.rel not in changed_rel:
        unsynced.append((corpus.traceability.rel, "模块内容变更但需求跟踪矩阵未更新"))

    seen: Set[str] = set()
    unique_unsynced: List[Tuple[str, str]] = []
    for rel, reason in unsynced:
        if rel not in seen:
            seen.add(rel)
            unique_unsynced.append((rel, reason))

    # 复核销项：判定从二值（改了/没改）补成三值（改了/核过无需改/没核）。
    # 没有这一格，让门禁闭嘴的唯一办法是给无需改动的文档空转版本号——那正好
    # 腐蚀 C11 的进位判据（读者是否需要重读）。销项只抵扣「受影响但未同步」，
    # 状态未回退豁免不了；销项也不跨变更集粘连，它是每次运行的输入。
    cleared_hits: List[Tuple[str, str]] = []
    cleared_notes: List[str] = []
    if args.cleared:
        cleared_set, cleared_notes = resolve_cleared(
            corpus, read_line_list(args.cleared, "销项清单"), doc_root)
        cleared_hits = [(rel, reason) for rel, reason in unique_unsynced
                        if rel in cleared_set]
        unique_unsynced = [(rel, reason) for rel, reason in unique_unsynced
                           if rel not in cleared_set]
        for rel in sorted(cleared_set - {r for r, _ in cleared_hits}):
            if rel in changed_rel:
                cleared_notes.append(f"「{rel}」已在本次变更集里，本就算已同步，无需销项")
            else:
                cleared_notes.append(f"「{rel}」不在本次受影响清单里"
                                     f"（可能沿用了上一轮里程碑的销项清单）")

    # 状态未回退：正文改了，状态还挂在已评审／已冻结。
    #
    # 这是文档生命周期回路上唯一的自动强制点。正向路径（草稿→已评审→已冻结）
    # 是人主动触发的，人会记得；回退是人不想触发的动作，只能靠机器盯。没有这一
    # 条，跑上三个月全库都是「已冻结」而没有一份真的冻结着。
    #
    # 与「受影响但未同步」分开列：那一类是「该改的没改」，这一类是「改了但没退
    # 状态」，处理方式不同。
    stale_status: List[Tuple[str, str]] = []
    snapshot_rel = corpus.snapshot.rel if corpus.snapshot else None
    adr_rels = {d.rel for d in corpus.decisions}
    for rel in sorted(changed_rel):
        # 版本快照每次打基线都被追加内容，正文必然改动，它不参与生命周期
        if rel == snapshot_rel or rel in adr_rels:
            continue
        raw = corpus.doc_status.get(rel, ("", None))[0].strip()
        if raw in FROZEN_DOC_STATUS:
            stale_status.append((rel, raw))

    lines: List[str] = []
    if args.format == "markdown":
        lines.append("## 需求文档变更影响分析")
        lines.append("")
        lines.append(f"变更来源 `{source_label}`，传播深度 {args.depth} 跳。")
        lines.append("")
        lines.append("### 本次变更文档")
        lines.append("")
        for p in sorted(changed_rel):
            lines.append(f"- `{p}`")
        lines.append("")
        if glossary_changed:
            lines.append("> 术语表发生变更，全部文档需复核术语一致性。")
            lines.append("")
        lines.append("### 受影响但未同步的文档")
        lines.append("")
        if unique_unsynced:
            lines.append("| 文档 | 影响原因 |")
            lines.append("|------|----------|")
            for rel, reason in unique_unsynced:
                lines.append(f"| `{rel}` | {reason} |")
        elif cleared_hits:
            lines.append("无。受影响文档均已同步更新或经复核销项。")
        else:
            lines.append("无。受影响文档均已在本次变更中同步更新。")
        if cleared_hits:
            lines.append("")
            lines.append("### 已复核销项")
            lines.append("")
            lines.append("下列文档经人工复核确认无需改动（`--cleared` 清单），"
                         "原影响原因保留备查：")
            lines.append("")
            lines.append("| 文档 | 原影响原因 |")
            lines.append("|------|------------|")
            for rel, reason in cleared_hits:
                lines.append(f"| `{rel}` | {reason} |")
        for note in cleared_notes:
            lines.append("")
            lines.append(f"> 销项清单提示: {note}")
        if stale_status:
            lines.append("")
            lines.append("### 状态未回退的文档")
            lines.append("")
            lines.append("| 文档 | 当前状态 |")
            lines.append("|------|----------|")
            for rel, raw in stale_status:
                lines.append(f"| `{rel}` | {raw} |")
            lines.append("")
            lines.append("正文已改动，状态却仍是已评审／已冻结。改动前阶段内容要"
                         "先退回「草稿」，再走一遍评审。若本次改的只是状态行本身"
                         "（评审或冻结动作），忽略此节。")
    else:
        lines.append("需求文档变更影响分析")
        lines.append("=" * 60)
        lines.append(f"变更来源: {source_label}   传播深度: {args.depth} 跳")
        lines.append(f"变更文档 {len(changed_rel)} 份，变更模块 {len(changed_mods)} 个")
        lines.append("")
        lines.append("本次变更:")
        for p in sorted(changed_rel):
            lines.append(f"  * {p}")
        if glossary_changed:
            lines.append("")
            lines.append("  ! 术语表发生变更，全部文档需复核术语一致性")
        lines.append("")
        if unique_unsynced:
            lines.append(f"受影响但未同步 ({len(unique_unsynced)}):")
            for rel, reason in unique_unsynced:
                lines.append(f"  - {rel}")
                lines.append(f"      {reason}")
        elif cleared_hits:
            lines.append("受影响文档均已同步更新或经复核销项。")
        else:
            lines.append("受影响文档均已同步更新。")
        if cleared_hits:
            lines.append("")
            lines.append(f"已复核销项 ({len(cleared_hits)}):  --cleared 确认无需改动，"
                         f"原影响原因保留备查")
            for rel, reason in cleared_hits:
                lines.append(f"  - {rel}")
                lines.append(f"      {reason}")
        for note in cleared_notes:
            lines.append(f"  ! 销项清单: {note}")
        if stale_status:
            lines.append("")
            lines.append(f"状态未回退 ({len(stale_status)}):")
            for rel, raw in stale_status:
                lines.append(f"  - {rel}")
                lines.append(f"      正文已改动，状态仍是「{raw}」。"
                             f"改动前阶段内容要先退回「草稿」再走评审")
            lines.append("  （本次若只改了状态行本身，即评审或冻结动作，忽略本节）")
        lines.append("=" * 60)

    print("\n".join(lines))

    if (unique_unsynced or stale_status) and args.fail_on_unsynced:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
