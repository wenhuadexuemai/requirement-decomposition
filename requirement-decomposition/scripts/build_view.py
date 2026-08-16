#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按视图配方把多个模块文档的指定章节拼成一份聚合视图。

文档网络拆成多份后，人读时要在模块文档间反复跳转。本脚本反向操作：给定一组
模块 ID 与章节关键词，从各模块文档抽出对应章节，拼成一份临时聚合 Markdown，
供按角色/主题集中阅读（如「支付相关需求集成视图」）。只读源文档、不改动它们；
输出到 stdout 或 -o 指定的文件。

注意：-o 的输出不要放进被校验的需求文档目录——聚合视图抽出的相对链接在新位置
会失效，会被 C03 报链接断裂，且它不带版本/状态行会触发 C11/C15。输出到文档目录之外。

用法:
    python3 build_view.py ./需求文档 --modules MOD-0001,MOD-0003 \
        --sections 目标与范围,主要流程 -o 支付视图.md
    python3 build_view.py ./需求文档 --modules MOD-0001            # 抽全部章节
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validate_requirements import Corpus, RE_MOD, first_id  # noqa: E402

RE_H2 = re.compile(r"^##\s")
RE_ATX = re.compile(r"^(#{1,6})\s")


def extract_sections(text: str, keywords: List[str]) -> List[str]:
    """按二级标题切块，返回标题含任一关键词的章节；keywords 为空则返回全部。"""
    blocks: List[List[str]] = []
    cur = None
    for line in text.split("\n"):
        if RE_H2.match(line):
            cur = [line]
            blocks.append(cur)
        elif cur is not None:
            cur.append(line)
    out = []
    for block in blocks:
        if not keywords or any(k in block[0] for k in keywords):
            out.append("\n".join(block).strip())
    return out


def downgrade(text: str) -> str:
    """所有 ATX 标题降一级，让模块标题在聚合文档里独占二级。"""
    return RE_ATX.sub(lambda m: m.group(1) + "# ", text)


def module_name(text: str, mid: str) -> str:
    m = re.match(r"^#\s+" + re.escape(mid) + r"\s+(.+)$", text)
    return m.group(1).strip() if m else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="把多个模块文档的指定章节拼成聚合视图")
    ap.add_argument("directory", help="需求文档根目录")
    ap.add_argument("--modules", required=True, help="逗号分隔的模块 ID")
    ap.add_argument("--sections", default="", help="逗号分隔的章节关键词，缺省抽全部章节")
    ap.add_argument("--title", default="需求聚合视图", help="聚合文档标题")
    ap.add_argument("-o", "--output", help="输出文件，缺省打印到 stdout。勿指向被校验的需求文档目录（链接会在新位置失效）")
    args = ap.parse_args()

    if not os.path.isdir(args.directory):
        print(f"目录不存在: {args.directory}", file=sys.stderr)
        return 2
    c = Corpus(args.directory)
    if not c.docs:
        print(f"目录中没有 Markdown 文件: {args.directory}", file=sys.stderr)
        return 2

    wanted = [m.strip() for m in args.modules.split(",") if m.strip()]
    keywords = [s.strip() for s in args.sections.split(",") if s.strip()]

    docs_by_id: Dict[str, object] = {}
    for d in c.modules:
        mid = first_id(os.path.basename(d.rel), RE_MOD)
        if mid:
            docs_by_id[mid] = d

    missing = [m for m in wanted if m not in docs_by_id]
    for m in missing:
        print(f"模块不存在: {m}", file=sys.stderr)
    if missing:
        return 2

    out: List[str] = [f"# {args.title}", ""]
    for mid in wanted:
        d = docs_by_id[mid]
        name = module_name(d.text, mid)
        out.append(f"## {mid} {name}".rstrip())
        out.append(f"> 源文档：[{os.path.basename(d.rel)}](./{d.rel})")
        out.append("")
        blocks = extract_sections(d.text, keywords)
        if not blocks:
            out.append("（未抽到匹配章节）")
            out.append("")
            continue
        for b in blocks:
            out.append(downgrade(b))
            out.append("")

    result = "\n".join(out).rstrip() + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result)
        print(f"聚合视图已生成: {args.output}（{len(wanted)} 个模块）")
    else:
        print(result, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
