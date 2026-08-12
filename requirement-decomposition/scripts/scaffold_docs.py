#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按骨架定义批量生成需求文档网络。

读取第 1 轮产出的骨架 JSON，展开成完整目录结构：综述、术语表、跟踪矩阵、
版本计划、每模块一份文档、每场景一份文档。模块文档中的 NFR 适用性声明表
按全局 NFR 逐条预填行，从源头杜绝漏项。

用法:
    python3 scaffold_docs.py skeleton.json --output ./需求文档
    python3 scaffold_docs.py skeleton.json --output ./需求文档 --force
    python3 scaffold_docs.py --print-schema

骨架 JSON 结构见 --print-schema 输出。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from typing import Any, Dict, List, Optional

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "assets", "templates")

DEFAULT_OUTPUT = "./需求文档"

# 产物键 -> (模板, 输出文件名)。文件名里的关键词是校验器识别产物的依据，
# 改名要同时改 validate_requirements.DOC_KEYWORDS。
ROOT_DOCS = {
    "overview": ("overview.md", "00-综述.md"),
    "glossary": ("glossary.md", "01-术语表.md"),
    "traceability": ("traceability.md", "02-需求跟踪矩阵.md"),
    "release": ("release-plan.md", "03-版本计划.md"),
    "snapshot": ("version-snapshot.md", "04-版本快照.md"),
}

# 逐条目展开的产物：子目录、模板、ID 正则、骨架里的字段名
ITEM_DOCS = {
    "modules": ("modules", "module.md", r"MOD-\d{4,}(?:-[A-Z])?", "MOD-0001"),
    "scenarios": ("scenarios", "scenario.md", r"UC-\d{4,}", "UC-0001"),
    "decisions": ("decisions", "adr.md", r"ADR-\d{4,}", "ADR-0001"),
}

# --print-schema 的输出必须是一份可直接喂回本脚本的合法骨架（S07 的自举冒烟
# 依赖这一点），所以这里放不下注释。modules[].status 的取值受 VALID_DOC_STATUS
# 约束，骨架阶段只接受「草稿」，见 check_status()。
SCHEMA_EXAMPLE = {
    "project": "示例项目",
    "nfrs": [
        {
            "id": "NFR-0001",
            "category": "性能",
            "metric": "列表类接口响应时间",
            "threshold": "P95 ≤ 800ms，并发 200",
            "measure": "压测报告 + 线上 APM 监控",
            "consequence": "不通过上线评审",
        },
        {
            "id": "NFR-0002",
            "category": "安全",
            "metric": "个人敏感字段存储方式",
            "threshold": "手机号、身份证号密文落库，日志中脱敏展示",
            "measure": "代码审计 + 日志抽检",
            "consequence": "不通过安全评审",
        },
    ],
    "modules": [
        {
            "id": "MOD-0001",
            "name": "订单创建",
            "goal": "校验下单请求并生成待支付订单",
            "version": "V1",
            "status": "草稿",
            "relation_summary": "依赖 MOD-0002，事件触发 MOD-0003",
        },
        {
            "id": "MOD-0002",
            "name": "库存锁定",
            "goal": "按商品维度锁定可售库存并在超时后释放",
            "version": "V1",
            "status": "草稿",
            "relation_summary": "被 MOD-0001 依赖",
        },
        {
            "id": "MOD-0003",
            "name": "订单通知",
            "goal": "订单状态变更后向买家推送消息",
            "version": "V1",
            "status": "草稿",
            "relation_summary": "由 MOD-0001 事件触发",
        },
    ],
    "scenarios": [
        {"id": "UC-0001", "name": "买家下单到支付完成"},
        {"id": "UC-0002", "name": "支付超时自动取消"},
        {"id": "UC-0003", "name": "库存不足下单失败"},
    ],
    "decisions": [
        {"id": "ADR-0001", "title": "库存锁定采用预占而非下单时扣减"},
    ],
    "relations": [
        {
            "source": "MOD-0001",
            "target": "MOD-0002",
            "type": "依赖",
            "description": "创建订单前需锁定库存，锁定失败则下单终止",
            "confidence": "推测",
            "strength": "强依赖",
            "fallback": "不可降级，失败表现见模块文档异常流 E1",
        },
        {
            "source": "MOD-0001",
            "target": "MOD-0003",
            "type": "事件触发",
            "description": "订单创建成功后发出事件，由通知模块异步消费",
            "confidence": "推测",
            "strength": "弱依赖",
            "fallback": "通知不可用时订单流程照常完成，消息进重试队列，24 小时内补发",
        },
    ],
}

ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\s]+')


def load_template(name: str) -> str:
    path = os.path.normpath(os.path.join(TEMPLATE_DIR, name))
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def render(template: str, mapping: Dict[str, str]) -> str:
    for key, value in mapping.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def safe_name(text: str) -> str:
    return ILLEGAL_FILENAME.sub("-", text.strip()).strip("-") or "未命名"


def cell(value: Any, default: str = "[待确认]") -> str:
    text = str(value).strip() if value not in (None, "") else ""
    return text.replace("|", "\\|") if text else default


def write_file(path: str, content: str, force: bool, created: List[str],
               skipped: List[str]) -> None:
    if os.path.exists(path) and not force:
        skipped.append(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    created.append(path)


def build_nfr_rows(nfrs: List[Dict[str, Any]]) -> str:
    if not nfrs:
        return "| NFR-0001 | [待确认] | [待确认] | [待确认] | [待确认] | [待确认] |"
    return "\n".join(
        "| {} | {} | {} | {} | {} | {} |".format(
            cell(n.get("id")), cell(n.get("category")), cell(n.get("metric")),
            cell(n.get("threshold")), cell(n.get("measure")), cell(n.get("consequence")))
        for n in nfrs
    )


def build_index_rows(modules: List[Dict[str, Any]]) -> str:
    rows = []
    for m in modules:
        mid = cell(m.get("id"))
        name = cell(m.get("name"))
        link = "[{0}](./modules/{0}-{1}.md)".format(mid, safe_name(str(m.get("name") or "").strip() or "[待确认]"))
        rows.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            mid, name, cell(m.get("goal")), link,
            cell(m.get("relation_summary"), "——"),
            cell(m.get("version"), "V1"),
            cell(m.get("status"), INITIAL_DOC_STATUS)))
    return "\n".join(rows) or (
        "| [待确认] | [待确认] | [待确认] | [待确认] | —— | V1 | "
        + INITIAL_DOC_STATUS + " |")


def build_relation_rows(relations: List[Dict[str, Any]]) -> str:
    if not relations:
        return "| [待确认] | [待确认] | [待确认] | [待确认] | 待定 | [待确认] | [待确认] |"
    return "\n".join(
        "| {} | {} | {} | {} | {} | {} | {} |".format(
            cell(r.get("source")), cell(r.get("target")), cell(r.get("type")),
            cell(r.get("description")), cell(r.get("confidence"), "推测"),
            cell(r.get("strength")), cell(r.get("fallback")))
        for r in relations
    )


def build_nfr_decl_rows(nfrs: List[Dict[str, Any]]) -> str:
    if not nfrs:
        return "| [待确认] | [待确认] | [待确认] |"
    return "\n".join(
        "| {} | [待确认: 完全适用/部分适用/不适用] | [待确认: 部分适用或不适用时必须给实质理由] |".format(
            cell(n.get("id")))
        for n in nfrs
    )


# 文档状态词表。人读版在 references/decomposition-rules.md 第 8 节，校验器侧是
# validate_requirements.VALID_DOC_STATUS——这里刻意不 import 校验器，保持脚手架
# 零兄弟脚本依赖（与下面的 RELATION_MIRROR 同一取舍），三处由 S17 核对逐字一致。
VALID_DOC_STATUS = ("草稿", "已评审", "已冻结", "已废弃")
INITIAL_DOC_STATUS = "草稿"

RELATION_MIRROR = {
    "依赖": "被依赖", "被依赖": "依赖",
    "组合": "隶属", "隶属": "组合",
    "顺序": "后置于", "后置于": "顺序",
    "约束": "受约束于", "受约束于": "约束",
    "事件触发": "被触发", "被触发": "事件触发",
    "回退": "补偿对象", "补偿对象": "回退",
    "关联": "关联", "互斥": "互斥", "数据共享": "数据共享",
}


def build_local_relation_rows(mod_id: str, relations: List[Dict[str, Any]],
                              links: Dict[str, str]) -> str:
    """按关系矩阵为单个模块生成关系本地视图行，方向自动互补，对方模块生成可点击链接。"""
    rows = []
    for r in relations:
        src = str(r.get("source", "")).strip()
        dst = str(r.get("target", "")).strip()
        rtype = str(r.get("type", "")).strip()
        base = (cell(r.get("description")), cell(r.get("confidence"), "推测"),
                cell(r.get("strength")))
        if src == mod_id and dst:
            rows.append("| {} | {} | 出向 | {} | {} | {} | [待确认: 本模块视角的补充说明] |"
                        .format(links.get(dst, dst), cell(rtype), *base))
        elif dst == mod_id and src:
            mirror = RELATION_MIRROR.get(rtype, rtype)
            rows.append("| {} | {} | 入向 | {} | {} | {} | [待确认: 本模块视角的补充说明] |"
                        .format(links.get(src, src), cell(mirror), *base))
    if not rows:
        rows.append("| [待确认] | [待确认] | 出向/入向 | [待确认: 本模块尚未登记任何跨模块关系，"
                    "确认是否遗漏] | 待定 | [待确认] | [待确认] |")
    return "\n".join(rows)


def build_trace_rows(modules: List[Dict[str, Any]]) -> str:
    if not modules:
        return "| [待确认] | [待确认] | [待确认] | [待确认] | Must | [待确认] | —— | [待确认] | V1 | —— |"
    rows = []
    for m in modules:
        mid = cell(m.get("id"))
        seq = mid.split("-")[1] if "-" in mid else "0001"
        rows.append(
            "| {0}-REQ-0001 | {0} | [待确认] | [待确认] | Must | [待确认: 原始文档 + 章节号] "
            "| —— | AC-{1}-0001 | {2} | —— |".format(mid, seq, cell(m.get("version"), "V1")))
    return "\n".join(rows)


def check_ids(items: List[Dict[str, Any]], kind: str, label: str,
              key: str = "id") -> Optional[str]:
    """校验一组条目的 ID 合法且不重复，返回错误信息或 None。"""
    _, _, pattern, sample = ITEM_DOCS[kind]
    seen = set()
    for item in items:
        value = str(item.get(key, "")).strip()
        if not re.fullmatch(pattern, value):
            return (f"{label} ID 非法: {value!r}，格式应为 {sample}"
                    f"（序号四位起，不足补 0）")
        if value in seen:
            return f"{label} ID 重复: {value}"
        seen.add(value)
    return None


def check_status(modules: List[Dict[str, Any]]) -> Optional[str]:
    """校验骨架里的模块状态取值，返回错误信息或 None。

    骨架阶段只接受「草稿」：其余取值的进入条件（评审通过、写入版本快照基线）
    在目录还没展开的时候不可能满足。写「已评审」不报错的话，展开出来的就是一批
    自称评审过而从未被评审的文档——C15 只能看出取值合法，看不出它是假的。
    """
    for m in modules:
        raw = m.get("status")
        if raw is None or not str(raw).strip():
            continue
        value = str(raw).strip()
        if value not in VALID_DOC_STATUS:
            return (f"模块 {m.get('id')} 的 status 非法: {value!r}，"
                    f"只能取 {' / '.join(VALID_DOC_STATUS)}"
                    f"（见 references/decomposition-rules.md 第 8 节）")
        if value != INITIAL_DOC_STATUS:
            return (f"模块 {m.get('id')} 的 status 是 {value!r}，骨架阶段只能是 "
                    f"{INITIAL_DOC_STATUS!r}——{value} 的进入条件要等目录展开、"
                    f"评审通过之后才可能满足")
    return None


def scaffold(skeleton: Dict[str, Any], output: str, force: bool) -> int:
    project = str(skeleton.get("project", "")).strip() or "[待确认: 项目名]"
    modules = skeleton.get("modules") or []
    nfrs = skeleton.get("nfrs") or []
    scenarios = skeleton.get("scenarios") or []
    relations = skeleton.get("relations") or []
    decisions = skeleton.get("decisions") or []
    today = date.today().isoformat()

    for items, kind, label in ((modules, "modules", "模块"),
                               (scenarios, "scenarios", "场景"),
                               (decisions, "decisions", "决策")):
        problem = check_ids(items, kind, label)
        if problem:
            print(problem, file=sys.stderr)
            return 2

    problem = check_status(modules)
    if problem:
        print(problem, file=sys.stderr)
        return 2

    created: List[str] = []
    skipped: List[str] = []

    # STATUS 进 common：除版本快照外每份产物的元信息表都有状态行，取值恒为初始态。
    # 版本快照模板没有这个占位符（它记录历史，不参与生命周期），多给一个键无害。
    common = {"PROJECT": project, "DATE": today,
              "STATUS": INITIAL_DOC_STATUS}
    # 各产物的专属占位符；未列出的产物只用 common
    extra_rows = {
        "overview": {
            "NFR_ROWS": build_nfr_rows(nfrs),
            "MODULE_INDEX_ROWS": build_index_rows(modules),
            "RELATION_ROWS": build_relation_rows(relations),
        },
        "traceability": {"TRACE_ROWS": build_trace_rows(modules)},
        "snapshot": {
            "SNAPSHOT_ROWS": "（首次评审定版后用 `--snapshot` 追加基线记录）",
        },
    }
    for key, (template, filename) in ROOT_DOCS.items():
        content = render(load_template(template),
                         dict(common, **extra_rows.get(key, {})))
        write_file(os.path.join(output, filename), content, force, created, skipped)

    mod_subdir, mod_template, _, _ = ITEM_DOCS["modules"]
    module_tpl = load_template(mod_template)
    decl_rows = build_nfr_decl_rows(nfrs)
    # 模块间互相引用的相对链接（模块文档同处 modules/ 目录）
    links = {str(m["id"]).strip():
             "[{0}](./{0}-{1}.md)".format(str(m["id"]).strip(),
                                          safe_name(str(m.get("name") or "").strip() or "[待确认]"))
             for m in modules}
    for m in modules:
        mid = str(m["id"]).strip()
        name = str(m.get("name") or "").strip() or "[待确认]"
        seq = mid.split("-")[1] if "-" in mid else "0001"
        content = render(module_tpl, dict(common, **{
            "MOD_ID": mid,
            "MOD_NAME": name,
            "MOD_GOAL": str(m.get("goal") or "").strip() or "[待确认: 一句话目标]",
            "MOD_SEQ": seq,
            "VERSION": str(m.get("version") or "V1"),
            "STATUS": str(m.get("status") or INITIAL_DOC_STATUS),
            "NFR_DECL_ROWS": decl_rows,
            "LOCAL_RELATION_ROWS": build_local_relation_rows(mid, relations, links),
        }))
        write_file(os.path.join(output, mod_subdir, f"{mid}-{safe_name(name)}.md"),
                   content, force, created, skipped)

    uc_subdir, uc_template, _, _ = ITEM_DOCS["scenarios"]
    scenario_tpl = load_template(uc_template)
    for s in scenarios:
        uid = str(s["id"]).strip()
        name = str(s.get("name") or "").strip() or "[待确认]"
        content = render(scenario_tpl, dict(common, UC_ID=uid, UC_NAME=name))
        write_file(os.path.join(output, uc_subdir, f"{uid}-{safe_name(name)}.md"),
                   content, force, created, skipped)

    if decisions:
        adr_subdir, adr_template, _, _ = ITEM_DOCS["decisions"]
        adr_tpl = load_template(adr_template)
        for d in decisions:
            aid = str(d["id"]).strip()
            title = str(d.get("title") or "").strip() or "[待确认: 决策标题]"
            content = render(adr_tpl, dict(common, **{
                "ADR_SEQ": aid.split("-")[1],
                "ADR_TITLE": title,
            }))
            write_file(os.path.join(output, adr_subdir, f"{aid}-{safe_name(title)}.md"),
                       content, force, created, skipped)

    print(f"生成完成: {output}")
    print(f"  新建 {len(created)} 个文件"
          + (f"，跳过 {len(skipped)} 个已存在文件（加 --force 覆盖）" if skipped else ""))
    for p in created:
        print("  + " + os.path.relpath(p, output))
    for p in skipped:
        print("  = " + os.path.relpath(p, output) + " (已存在)")
    print("\n下一步: 逐模块填写内容，完成后运行")
    print(f"  python3 {os.path.join(os.path.dirname(os.path.abspath(__file__)), 'validate_requirements.py')} {output}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="按骨架定义生成需求文档网络")
    ap.add_argument("skeleton", nargs="?", help="骨架 JSON 文件路径")
    ap.add_argument("--output", "-o", default=DEFAULT_OUTPUT, help="输出目录")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    ap.add_argument("--print-schema", action="store_true",
                    help="打印骨架 JSON 示例后退出。modules[].status 只接受 "
                         + INITIAL_DOC_STATUS
                         + "，其余取值须在文档展开后按 decomposition-rules.md "
                           "第 8 节的转移条件推进")
    args = ap.parse_args()

    if args.print_schema:
        print(json.dumps(SCHEMA_EXAMPLE, ensure_ascii=False, indent=2))
        return 0
    if not args.skeleton:
        ap.error("缺少骨架 JSON 路径（或使用 --print-schema 查看结构）")

    try:
        with open(args.skeleton, "r", encoding="utf-8") as f:
            skeleton = json.load(f)
    except FileNotFoundError:
        print(f"骨架文件不存在: {args.skeleton}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"骨架 JSON 解析失败: {exc}", file=sys.stderr)
        return 2

    return scaffold(skeleton, args.output, args.force)


if __name__ == "__main__":
    sys.exit(main())
