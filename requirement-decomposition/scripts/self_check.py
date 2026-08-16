#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""技能包内部契约自检。

validate_requirements.py 用 C01~C16 十六项检查管住它产出的文档，这个脚本管住
技能包自己：模板、脚手架、校验器
三层靠字符串约定耦合，任一层单独改都会静默失效——渲染出没替换的 `{{FOO}}`、
解析不到表而误判通过、schema 与脚本常量各说各话。这些错误不会报错，只会安静
地放行，所以必须有一道机器检查。

改了这个包里的任何文件之后跑它。改产出文档跑 validate_requirements.py，
改技能包本身跑这个。

    python3 self_check.py                 # 全部
    python3 self_check.py --only S03,S07  # 只跑指定项
    python3 self_check.py --format json
    python3 self_check.py --list          # 列出检查项

退出码: 0 = 通过, 1 = 存在契约错误, 2 = 用法错误
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

RE_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")

CHECK_TITLES = {
    "S01": "SKILL.md frontmatter 与版本一致性",
    "S02": "访谈 schema 与脚本常量一致",
    "S03": "模板占位符与脚手架提供的键一一对应",
    "S04": "关系类型词表在两个脚本中一致",
    "S05": "文档解析约定与模板实际标题／表头对得上",
    "S06": "禁用词表两栏与脚本常量一致",
    "S07": "自举与真实数据冒烟：空白骨架零 ERROR 且真实数据不崩",
    "S08": "引用的技能内文件真实存在",
    "S09": "技能包自身遵守写作规范",
    "S10": "检查项编号在脚本与文档中一致",
    "S11": "模板固定文案不埋 C07 地雷",
    "S12": "模板版本契约与校验器一致",
    "S13": "访谈链负例冒烟",
    "S14": "影响分析与复核销项冒烟",
    "S15": "路由脚本冒烟",
    "S16": "人工复核清单脚本冒烟",
    "S17": "文档状态词表在三处一致",
    "S18": "取值词表人读版与机器版一致",
    "S19": "聚合视图脚本冒烟",
    "S20": "人工自查词表与 §2.2 一致",
    "S21": "流水线档位判定冒烟",
}


class Result:
    def __init__(self) -> None:
        self.errors: List[Tuple[str, str]] = []
        self.warns: List[Tuple[str, str]] = []
        self.notes: List[Tuple[str, str]] = []

    def error(self, code: str, msg: str) -> None:
        self.errors.append((code, msg))

    def warn(self, code: str, msg: str) -> None:
        self.warns.append((code, msg))

    def note(self, code: str, msg: str) -> None:
        self.notes.append((code, msg))


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_json(path: str) -> object:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def fmt(version: Tuple[int, ...]) -> str:
    return ".".join(str(x) for x in version)


def skill_frontmatter() -> Dict[str, str]:
    text = read(os.path.join(SKILL_ROOT, "SKILL.md"))
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    out: Dict[str, str] = {}
    key: Optional[str] = None
    for line in m.group(1).splitlines():
        kv = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
        if kv:
            key = kv.group(1)
            out[key] = kv.group(2).strip()
        elif key and line.strip():
            out[key] += " " + line.strip()
    return out


# --------------------------------------------------------------------------
# S01 版本一致性
# --------------------------------------------------------------------------

def check_version(r: Result) -> None:
    fm = skill_frontmatter()
    if not fm:
        r.error("S01", "SKILL.md 未解析到 frontmatter")
        return

    for key in ("name", "version", "description"):
        if not fm.get(key):
            r.error("S01", f"SKILL.md frontmatter 缺少 {key}")

    version = fm.get("version", "")
    if version and not re.fullmatch(r"\d+\.\d+\.\d+", version):
        r.error("S01", f"version「{version}」需为 x.y.z 形式")

    name = fm.get("name", "")
    if name and name != os.path.basename(SKILL_ROOT):
        r.error("S01", f"frontmatter 的 name「{name}」与目录名"
                       f"「{os.path.basename(SKILL_ROOT)}」不一致")

    desc = fm.get("description", "")
    if len(desc) > 1024:
        r.error("S01", f"description 长度 {len(desc)} 超过 1024 字符上限")
    if desc and not any(k in desc for k in ("不用于", "不适用")):
        r.error("S01", "description 未写负触发范围，触发边界只靠正例撑不住")

    # 访谈状态样例声明的 skill_version 必须跟得上包版本，否则恢复时比对会误判
    from validate_interview import EXAMPLE_STATE  # noqa: E402
    sample = EXAMPLE_STATE.get("skill_version")
    if version and sample != version:
        r.error("S01", f"validate_interview.py 的 EXAMPLE_STATE.skill_version"
                       f"「{sample}」与 SKILL.md 的 version「{version}」不一致")

    # CHANGELOG 随技能包分发（打进 zip），在技能包内
    changelog = os.path.join(SKILL_ROOT, "CHANGELOG.md")
    if not os.path.exists(changelog):
        r.warn("S01", "缺少 CHANGELOG.md，版本演进无处可查")
        return
    text = read(changelog)
    entries = re.findall(r"^##\s+\[(\d+\.\d+\.\d+)\]", text, re.M)
    if version and version not in entries:
        r.error("S01", f"CHANGELOG.md 中没有当前版本 {version} 的条目")
    if entries and version and entries[0] != version:
        r.error("S01", f"CHANGELOG.md 最上面的条目是 {entries[0]}，"
                       f"当前版本却是 {version}。最新版本必须排在最前")

    # 版本序列必须严格递减且每次只进一位。走错层级（该记补丁号却升了次版本号）
    # 事后要回头改所有引用，包括脚本常量、CLAUDE.md 与说明文字。
    parsed = [tuple(int(x) for x in e.split(".")) for e in entries]
    for newer, older in zip(parsed, parsed[1:]):
        if newer <= older:
            r.error("S01", f"CHANGELOG.md 的版本顺序不对：{fmt(newer)} 排在 "
                           f"{fmt(older)} 之前，但并不更新")
            continue
        if newer == (older[0] + 1, 0, 0):
            continue
        if newer == (older[0], older[1] + 1, 0):
            continue
        if newer == (older[0], older[1], older[2] + 1):
            continue
        r.error("S01", f"{fmt(older)} → {fmt(newer)} 跨度不合法。"
                           f"一次只进一位：主版本号进位时次版本号与补丁号归零，"
                           f"次版本号进位时补丁号归零")

    if len(parsed) >= 2 and parsed[-1] != (1, 0, 0):
        r.warn("S01", f"最早的条目是 {fmt(parsed[-1])}，通常初版为 1.0.0")


# --------------------------------------------------------------------------
# S02 访谈 schema 与脚本常量
# --------------------------------------------------------------------------

def check_interview_contract(r: Result) -> None:
    from validate_interview import (  # noqa: E402
        GATE_KEYS, STATE_KEYS, VALID_STATUSES, SCHEMA_VERSION, EXAMPLE_STATE,
        EVIDENCE_KINDS, QUESTION_CATEGORIES,
    )
    path = os.path.join(SKILL_ROOT, "schemas", "interview-state.schema.json")
    if not os.path.exists(path):
        r.error("S02", "找不到 schemas/interview-state.schema.json")
        return
    schema = load_json(path)
    props = schema["properties"]

    pairs = [
        ("门禁项", set(props["completion_gate"]["properties"]), GATE_KEYS),
        ("状态必需字段", set(schema["required"]), STATE_KEYS),
        ("status 取值", set(props["status"]["enum"]), VALID_STATUSES),
        ("evidence.kind 取值",
         set(props["evidence"]["items"]["properties"]["kind"]["enum"]), EVIDENCE_KINDS),
        ("未决项 category 取值",
         set(props["unresolved_questions"]["items"]["properties"]["category"]["enum"]),
         QUESTION_CATEGORIES),
    ]
    for label, in_schema, in_code in pairs:
        if in_schema != in_code:
            only_schema = sorted(in_schema - in_code)
            only_code = sorted(in_code - in_schema)
            r.error("S02", f"{label}不一致："
                           f"仅 schema 有 {only_schema}；仅脚本有 {only_code}。"
                           f"schema 不被脚本读取，两处靠手工保持一致")

    if props["schema_version"]["const"] != SCHEMA_VERSION:
        r.error("S02", f"schema_version 不一致：schema 为 "
                       f"{props['schema_version']['const']}，脚本为 {SCHEMA_VERSION}")

    # 协议文档里写的门禁项数量不能与实际脱节
    proto = os.path.join(SKILL_ROOT, "references", "interview-protocol.md")
    if os.path.exists(proto):
        text = read(proto)
        cn_num = {6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}.get(len(GATE_KEYS))
        if cn_num and f"下列{cn_num}项" not in text:
            r.error("S02", f"interview-protocol.md 未写明「下列{cn_num}项」，"
                           f"与实际 {len(GATE_KEYS)} 项门禁不符")
        for key in sorted(GATE_KEYS):
            if key not in text:
                r.error("S02", f"门禁项 {key} 未在 interview-protocol.md 的门禁表中出现")

    # 样例必须自身合法，否则 --print-example 会教人写出过不了校验的状态
    import validate_interview  # noqa: E402
    errs: List[str] = []
    validate_interview.validate_state(json.loads(json.dumps(EXAMPLE_STATE)), errs)
    for e in errs:
        r.error("S02", f"--print-example 输出的样例自身不合法：{e}")


# --------------------------------------------------------------------------
# S03 模板占位符
# --------------------------------------------------------------------------

def _scaffold_keys() -> Tuple[set, set]:
    """取脚手架提供的占位符键，与它会加载的模板名。

    模板名从 ROOT_DOCS / ITEM_DOCS 取，是声明式的；占位符键只能从源码扒——
    它们散在 render() 的各个调用点。键有三种写法：字典字面量 "KEY":、
    关键字参数 KEY=、dict(common, KEY=...)，三种都认，只认一种会误报。
    """
    from scaffold_docs import ITEM_DOCS, ROOT_DOCS
    src = read(os.path.join(SCRIPT_DIR, "scaffold_docs.py"))
    keys = set(re.findall(r'"([A-Z][A-Z_]*)"\s*:', src))
    keys |= {k for k in re.findall(r'\b([A-Z][A-Z_]{2,})\s*=', src)
             if k.isupper() and k not in dir(__import__("scaffold_docs"))}
    templates = {tpl for tpl, _ in ROOT_DOCS.values()}
    templates |= {tpl for _, tpl, _, _ in ITEM_DOCS.values()}
    return keys, templates


def check_placeholders(r: Result) -> None:
    tpl_dir = os.path.join(SKILL_ROOT, "assets", "templates")
    provided, loaded = _scaffold_keys()
    if not loaded:
        r.error("S03", "未能从 scaffold_docs.py 解析出 load_template 调用")
        return

    used: set = set()
    for fn in sorted(os.listdir(tpl_dir)):
        if not fn.endswith(".md"):
            continue
        found = set(RE_PLACEHOLDER.findall(read(os.path.join(tpl_dir, fn))))
        used |= found
        if fn not in loaded:
            r.error("S03", f"模板 {fn} 未被 scaffold_docs.py 加载，"
                           f"它不会出现在任何产物里")
            continue
        missing = sorted(found - provided)
        if missing:
            r.error("S03", f"模板 {fn} 用了 {missing}，但 scaffold_docs.py 不提供这些键。"
                           f"脚手架无未知占位符检测，会把 {{{{{missing[0]}}}}} 原样写进产物")

    unused = sorted(provided - used)
    if unused:
        r.warn("S03", f"scaffold_docs.py 提供了 {unused}，但没有模板用到")

    for fn in sorted(loaded):
        if not os.path.exists(os.path.join(tpl_dir, fn)):
            r.error("S03", f"scaffold_docs.py 要加载 {fn}，"
                           f"但 assets/templates/ 下没有这个文件")


# --------------------------------------------------------------------------
# S04 关系类型词表
# --------------------------------------------------------------------------

def check_relation_tables(r: Result) -> None:
    from validate_requirements import RELATION_MIRROR as V_MIRROR, VALID_RELATIONS
    from scaffold_docs import RELATION_MIRROR as S_MIRROR

    if V_MIRROR != S_MIRROR:
        diff = sorted(set(V_MIRROR.items()) ^ set(S_MIRROR.items()))
        r.error("S04", f"RELATION_MIRROR 在两个脚本中不一致：{diff}。"
                       f"脚手架用它生成互补方向的本地视图，校验器用它提示应登记的镜像类型，"
                       f"两边不同会生成出过不了自己校验的文档")

    # 镜像必须是对合：mirror(mirror(x)) == x
    for k, v in sorted(V_MIRROR.items()):
        if V_MIRROR.get(v) != k:
            r.error("S04", f"镜像关系不对合：{k} → {v}，但 {v} → {V_MIRROR.get(v)}")
        if k not in VALID_RELATIONS or v not in VALID_RELATIONS:
            r.error("S04", f"镜像涉及的 {k} 或 {v} 不在 VALID_RELATIONS 词表内")

    # 人读版词表：十种正向类型必须都在 decomposition-rules.md 的表里
    rules = os.path.join(SKILL_ROOT, "references", "decomposition-rules.md")
    if os.path.exists(rules):
        text = read(rules)
        # 正向类型从 VALID_RELATIONS 推导：剔除镜像值（被依赖/隶属等反向词）。
        # 不硬编码清单——新增关系类型时，VALID_RELATIONS 加了它就自动被核对进 §5 表。
        backward = {v for k, v in V_MIRROR.items() if v != k}
        forward = sorted(t for t in VALID_RELATIONS if t not in backward)
        for t in forward:
            if not re.search(r"^\|\s*" + re.escape(t) + r"\s*\|", text, re.M):
                r.error("S04", f"关系类型「{t}」不在 decomposition-rules.md 第 5 节的表中")


# --------------------------------------------------------------------------
# S05 文档解析约定
# --------------------------------------------------------------------------

def _template_doc(fn: str):
    """把模板读成校验器认识的 Doc，不存在则返回 None。"""
    from validate_requirements import Doc
    path = os.path.join(SKILL_ROOT, "assets", "templates", fn)
    if not os.path.exists(path):
        return None
    text = read(path)
    return Doc(path=path, rel=fn, text=text, lines=text.splitlines())


def check_parsing_contract(r: Result) -> None:
    """模板里的标题与表头必须能被校验器的 pick_table 选中。

    关键词与判定逻辑都从 validate_requirements 导入，不在这里重抄一份：
    抄一份就等于多一处会漂移的副本，而这一项本来就是为了防漂移。
    """
    from validate_requirements import (
        DOC_KEYWORDS, GLOSSARY_COLUMNS, TABLE_SPECS, TRACE_COLUMNS,
        parse_tables, pick_table,
    )

    for key, (fn, label, heading_kw, header_kw) in sorted(TABLE_SPECS.items()):
        doc = _template_doc(fn)
        if doc is None:
            r.error("S05", f"模板 {fn} 不存在，{label}无从校验")
            continue
        if pick_table(parse_tables(doc), heading_kw, header_kw) is None:
            r.error("S05", f"{fn} 里的{label}选不中：标题需含 {heading_kw} 之一，"
                           f"或表头同时含 {header_kw}。解析不到内容会被当成通过")

    doc = _template_doc("glossary.md")
    if doc is not None:
        main = next((t for t in parse_tables(doc)
                     if any(kw in h for h in t.headers
                            for kw in GLOSSARY_COLUMNS["identity"])), None)
        if main is None:
            r.error("S05", f"glossary.md 选不到术语主表："
                           f"表头需含 {GLOSSARY_COLUMNS['identity']} 之一")
        else:
            for role, purpose in (("banned", "禁用同义词扫描的输入"),
                                  ("exemption", "同形异义的豁免机制")):
                col = GLOSSARY_COLUMNS[role]
                if not any(col in h for h in main.headers):
                    r.error("S05", f"glossary.md 术语主表缺「{col}」列，{purpose}会失效")

    doc = _template_doc("traceability.md")
    if doc is not None:
        main = next((t for t in parse_tables(doc)
                     if any("需求" in h for h in t.headers)), None)
        if main is None:
            r.error("S05", "traceability.md 选不到主表：表头需含「需求」")
        else:
            joined = " ".join(main.headers)
            for col in TRACE_COLUMNS:
                if col not in joined:
                    r.error("S05", f"traceability.md 主表缺必需列「{col}」")

    # 脚手架产出的文件名必须能被文件名关键词识别，否则校验器找不到这份产物
    from scaffold_docs import ROOT_DOCS
    for key, keywords in sorted(DOC_KEYWORDS.items()):
        entry = ROOT_DOCS.get(key)
        if entry is None:
            r.error("S05", f"校验器要找 {key} 这份产物，但脚手架不生成它")
            continue
        produced = entry[1]
        if not any(kw.lower() in produced.lower() for kw in keywords):
            r.error("S05", f"脚手架产出的 {produced} 不含任何识别关键词 {keywords}，"
                           f"校验器会找不到它")
    for key in sorted(set(ROOT_DOCS) - set(DOC_KEYWORDS)):
        r.error("S05", f"脚手架生成 {ROOT_DOCS[key][1]}，但校验器没有登记 {key} 的识别关键词")

    # 每条约定都要有人读版，否则改模板的人查不到它
    qg = os.path.join(SKILL_ROOT, "references", "quality-gates.md")
    if os.path.exists(qg):
        text = read(qg)
        for _, label, _, _ in TABLE_SPECS.values():
            if label.rstrip("表") not in text:
                r.warn("S05", f"quality-gates.md 的解析约定表中没有「{label}」")

    # 关系矩阵的标准列名必须在模板表头里出现。C05 与 impact_analysis 共用
    # RELATION_COLUMNS 读列，标准名（首个别名）选不中就静默读到空--C05 报
    # 无法解析、impact_analysis 传播图断链。别名兜底只对存量文档有用，标准名
    # 是脚手架生成的新文档必须用的。
    from validate_requirements import RELATION_COLUMNS
    ov_tpl = _template_doc("overview.md")
    if ov_tpl is not None:
        rel_t = pick_table(parse_tables(ov_tpl),
                           *TABLE_SPECS["relations"][2:])
        if rel_t is not None:
            headers_joined = " ".join(rel_t.headers)
            for role in ("source", "target", "type", "confidence",
                         "strength", "fallback"):
                standard = RELATION_COLUMNS[role][0]
                if standard not in headers_joined:
                    r.error("S05", f"overview.md 关系矩阵表头缺标准列「{standard}」"
                                   f"（{role}）。C05 与 impact_analysis 共用 "
                                   f"RELATION_COLUMNS 读这一列，选不中就静默读到空")


# --------------------------------------------------------------------------
# S06 禁用词表
# --------------------------------------------------------------------------

def _section(text: str, prefix: str) -> str:
    m = re.search(r"^###\s*" + re.escape(prefix) + r"[^\n]*$(.*?)(?=^###\s|^##\s|\Z)",
                  text, re.M | re.S)
    return m.group(1) if m else ""


def parse_scanned_words(text: str) -> set:
    """2.1 栏：加粗分类 + 顿号分隔的词列表。"""
    words: set = set()
    for line in _section(text, "2.1").splitlines():
        kv = re.match(r"\*\*(.+?)\*\*：(.+)", line.strip())
        if kv:
            words |= {w.strip() for w in kv.group(2).split("、") if w.strip()}
    return words


def parse_manual_words(text: str) -> set:
    """2.2 栏：表格首列是词，逐行一个。"""
    words: set = set()
    for line in _section(text, "2.2").splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("-:| "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0] and cells[0] != "词":
            words.add(cells[0])
    return words


def check_banned_words(r: Result) -> None:
    from validate_requirements import BANNED_WORDS
    path = os.path.join(SKILL_ROOT, "references", "writing-style.md")
    if not os.path.exists(path):
        r.error("S06", "找不到 references/writing-style.md")
        return
    text = read(path)
    in_code = {w for v in BANNED_WORDS.values() for w in v}
    scanned = parse_scanned_words(text)
    manual = parse_manual_words(text)

    if not scanned or not manual:
        r.error("S06", "writing-style.md 第 2 节未按「2.1 脚本扫描 / 2.2 人工自查」"
                       "两栏组织，无法核对与脚本常量的对应关系")
        return

    only_doc = sorted(scanned - in_code)
    only_code = sorted(in_code - scanned)
    if only_doc:
        r.error("S06", f"这些词列在「脚本扫描」栏但 BANNED_WORDS 里没有：{only_doc}。"
                       f"读者会以为 C08 管得住，实际不会报")
    if only_code:
        r.error("S06", f"BANNED_WORDS 里有但「脚本扫描」栏未列：{only_code}")

    overlap = sorted(scanned & manual)
    if overlap:
        r.error("S06", f"这些词同时出现在两栏：{overlap}，同一个词只能归一栏")

    # 人工自查栏的词若被误加进脚本，说明有人没读那栏的理由说明
    wrongly_scanned = sorted(manual & in_code)
    if wrongly_scanned:
        r.error("S06", f"「人工自查」栏的词进了 BANNED_WORDS：{wrongly_scanned}。"
                       f"这些词作为子串会误伤正常表述，那一栏写了理由")


def check_manual_words(r: Result) -> None:
    """MANUAL_WORDS 与 writing-style §2.2 一致。

    manual_review_checklist.py 的 MANUAL_WORDS 是 §2.2 表的第三份副本：S06 守
    §2.1↔BANNED_WORDS、拦 §2.2 词进脚本，但不核对 MANUAL_WORDS↔§2.2。改 §2.2 表
    时这份副本会静默漂移——正是 S06 要防的那类问题，只是换了个位置。
    """
    from manual_review_checklist import MANUAL_WORDS
    path = os.path.join(SKILL_ROOT, "references", "writing-style.md")
    if not os.path.exists(path):
        r.error("S20", "找不到 references/writing-style.md")
        return
    text = read(path)
    manual = parse_manual_words(text)
    in_script = set(MANUAL_WORDS)
    only_doc = sorted(manual - in_script)
    only_script = sorted(in_script - manual)
    if only_doc:
        r.error("S20", f"§2.2 人工自查栏列了但 MANUAL_WORDS 没有：{only_doc}")
    if only_script:
        r.error("S20", f"MANUAL_WORDS 里有但 §2.2 人工自查栏未列：{only_script}")


# --------------------------------------------------------------------------
# S21 流水线档位判定冒烟
# --------------------------------------------------------------------------

def check_pipeline_smoke(r: Result) -> None:
    """pipeline_status 冒烟：在同一临时目录里顺序合成七个状态，逐档断言。

    档位判定错了不会让任何门禁报错——它只决定「下一步建议」指哪条路，是纯文本
    drift 的无人区。这里固化：L0→L1→L2→L3→L10→L8→L7 的状态演进链、损坏状态
    文件的降级负例、以及 GEARS 映射表与 SKILL.md 流水线协议节的一致性（单一
    来源在脚本，SKILL.md 是抄本）。
    """
    from pipeline_status import GEARS
    skill = read(os.path.join(SKILL_ROOT, "SKILL.md"))
    for code, (name, round_, _, _) in sorted(GEARS.items()):
        if f"| {code} | {name} |" not in skill:
            r.error("S21", f"档位「| {code} | {name} |」未出现在 SKILL.md 流水线协议节")

    py = sys.executable
    script = os.path.join(SCRIPT_DIR, "pipeline_status.py")
    tmp = tempfile.mkdtemp(prefix="rd-pipeline-")

    def run():
        proc = subprocess.run([py, script, "--dir", tmp, "--format", "json"],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            return None, proc.stderr[:150]
        return json.loads(proc.stdout), ""

    def expect(gear: str, label: str) -> None:
        got, err = run()
        if got is None:
            r.error("S21", f"{label}: 运行失败 {err}")
        elif got["gear"] != gear:
            r.error("S21", f"{label}: 期望 {gear}，实际 {got['gear']}（依据: {got['evidence'][:2]}）")

    try:
        expect("L0", "空目录")

        state = {"schema_version": "1.0.0", "session_id": "s21", "skill_version": "0.0.0",
                 "mode": "skeleton-interview", "status": "interviewing",
                 "next_question_id": "Q-01"}
        with open(os.path.join(tmp, "interview-state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f)
        expect("L1", "访谈中")

        state["status"] = "approved"
        with open(os.path.join(tmp, "interview-state.json"), "w", encoding="utf-8") as f:
            json.dump(state, f)
        expect("L2", "已批准未展开")

        proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "scaffold_docs.py"),
                               "--print-schema"], capture_output=True, text=True)
        skel = os.path.join(tmp, "skeleton.json")
        with open(skel, "w", encoding="utf-8") as f:
            f.write(proc.stdout)
        subprocess.run([py, os.path.join(SCRIPT_DIR, "scaffold_docs.py"),
                        skel, "-o", os.path.join(tmp, "需求文档")],
                       capture_output=True, check=True)
        expect("L3", "空白骨架")

        proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "validate_requirements.py"),
                               os.path.join(tmp, "需求文档"), "--snapshot", "2026-08-16"],
                              capture_output=True, text=True)
        with open(os.path.join(tmp, "需求文档", "04-版本快照.md"), "a", encoding="utf-8") as f:
            f.write(proc.stdout)
        expect("L10", "有基线有草稿")

        for dirpath, _, files in os.walk(os.path.join(tmp, "需求文档")):
            for fn in files:
                if not fn.endswith(".md") or fn.startswith(("04-", "ADR")):
                    continue
                fp = os.path.join(dirpath, fn)
                body = read(fp)
                flipped = re.sub(r"(\|\s*状态\s*\|\s*)草稿(\s*\|)",
                                 r"\g<1>已评审\g<2>", body, count=1)
                if flipped != body:
                    with open(fp, "w", encoding="utf-8") as f:
                        f.write(flipped)
        expect("L8", "全库已评审未冻结")

        snap = os.path.join(tmp, "需求文档", "04-版本快照.md")
        body = read(snap)
        body = body.replace("| —— | —— | —— | —— |\n\n## 5. 变更记录",
                            "| MOD-0001 | 打回 | 验收标准不可测试 | 2026-08-16 |\n\n## 5. 变更记录", 1)
        with open(snap, "w", encoding="utf-8") as f:
            f.write(body)
        expect("L7", "打回未平")

        with open(os.path.join(tmp, "interview-state.json"), "w", encoding="utf-8") as f:
            f.write("{ 这不是合法 JSON")
        got, err = run()
        if got is None:
            r.error("S21", f"损坏状态文件应降级不崩溃，实际运行失败 {err}")
        elif not any("不可读" in n for n in got["notes"]):
            r.error("S21", f"损坏状态文件未给出降级提示: {got['notes']}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# S07 自举冒烟
# --------------------------------------------------------------------------

def check_bootstrap(r: Result) -> None:
    """示例骨架展开后必须零 ERROR。有 ERROR 说明契约断了，不是文档没填。"""
    tmp = tempfile.mkdtemp(prefix="rd-selfcheck-")
    try:
        skel = os.path.join(tmp, "skeleton.json")
        docs = os.path.join(tmp, "需求文档")
        py = sys.executable

        proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "scaffold_docs.py"),
                               "--print-schema"], capture_output=True, text=True)
        if proc.returncode != 0:
            r.error("S07", f"--print-schema 失败: {proc.stderr.strip()[:200]}")
            return
        with open(skel, "w", encoding="utf-8") as f:
            f.write(proc.stdout)

        proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "scaffold_docs.py"),
                               skel, "-o", docs], capture_output=True, text=True)
        if proc.returncode != 0:
            r.error("S07", f"脚手架展开失败: {proc.stderr.strip()[:200]}")
            return

        # 渲染后不得残留未替换的占位符
        for dirpath, _, files in os.walk(docs):
            for fn in files:
                if not fn.endswith(".md"):
                    continue
                left = set(RE_PLACEHOLDER.findall(read(os.path.join(dirpath, fn))))
                if left:
                    rel = os.path.relpath(os.path.join(dirpath, fn), docs)
                    r.error("S07", f"产物 {rel} 残留未替换的占位符 {sorted(left)}")

        proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "validate_requirements.py"),
                               docs, "--format", "json"], capture_output=True, text=True)
        if proc.returncode == 2:
            r.error("S07", f"校验器无法运行: {proc.stderr.strip()[:200]}")
            return
        payload = json.loads(proc.stdout)
        errs = [i for i in payload["issues"] if i["level"] == "ERROR"]
        if errs:
            for i in errs[:5]:
                r.error("S07", f"空白骨架产生了 ERROR（模板与脚本的契约已断）："
                               f"{i['code']} {i['file']} {i['message']}")
            if len(errs) > 5:
                r.error("S07", f"另有 {len(errs) - 5} 项 ERROR 未列出")

        # --strict 必须能卡住空白骨架，否则定稿门禁形同虚设
        proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "validate_requirements.py"),
                               docs, "--strict"], capture_output=True, text=True)
        if proc.returncode == 0:
            r.error("S07", "空白骨架在 --strict 下通过了。定稿门禁应当卡住满是"
                           "[待确认] 的骨架，通过说明 WARN 没有产生或 --strict 失效")

        # 真实数据冒烟：空白骨架的术语表全是 [待确认] 占位，_load_registry 对占位行
        # 直接 continue，永远走不到解析禁用同义词／豁免搭配／配置矛盾的分支。这些
        # 分支里的 bug（如变量改名后残留旧名）出厂就漏网。用一份填了真实数据的术语表
        # 再跑一遍，逼着双语标准名、含禁用词的合法豁免、不含任一禁用词的孤儿豁免、
        # 标准名与禁用同义词自相矛盾这几条分支真的执行。
        reg = os.path.join(tmp, "reg")
        os.makedirs(reg, exist_ok=True)
        realistic = (
            "# 测试项目 术语表\n\n"
            "| 项 | 值 |\n|------|------|\n"
            "| 文档版本 | V1.0.0 |\n| 更新日期 | 2026-08-10 |\n\n"
            "## 1. 术语\n\n"
            "| 术语 ID | 标准名 | 定义 | 禁用同义词 | 豁免上下文 | 是否数据实体 | 权威来源 | 首次出现 |\n"
            "|---------|--------|------|------------|------------|--------------|----------|----------|\n"
            "| TERM-0001 | 撤销 | 删除已保存对象 | 回退 | 失败回退加载 | 否 | -- | -- |\n"
            "| TERM-0002 | Order / 订单 | 一次购买请求 | 下单 | Matter 摘要 | 是 | -- | -- |\n"
            "| TERM-0003 | 笔记 | 用户记录片段 | 笔记、记录 | -- | 否 | -- | -- |\n"
            "| TERM-0004 | 样本 | 待检验物 | 物质 | Matter 物质摘要 | 否 | -- | -- |\n\n"
            "## 4. 变更记录\n\n"
            "| 版本 | 日期 | 变更人 | 变更内容 | 影响文档 |\n"
            "|------|------|--------|----------|----------|\n"
            "| V1.0.0 | 2026-08-10 | -- | 初始 | -- |\n"
        )
        with open(os.path.join(reg, "术语表-glossary.md"), "w", encoding="utf-8") as f:
            f.write(realistic)
        # 非术语表文档：逼着 C07 的扫描内循环真的执行。TERM-0001 的禁用同义词「回退」
        # 在「执行回退」里应报 WARN，在豁免搭配「失败回退加载」里应被掩护不报。
        with open(os.path.join(reg, "说明.md"), "w", encoding="utf-8") as f:
            f.write("# 说明\n\n执行回退操作时需谨慎。\n\n失败回退加载时记日志。\n")
        proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "validate_requirements.py"),
                               reg, "--format", "json"], capture_output=True, text=True)
        if proc.returncode == 2:
            r.error("S07", f"真实数据冒烟：校验器无法运行: {proc.stderr.strip()[:200]}")
        else:
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                tail = (proc.stderr.strip().splitlines() or ["(无 stderr)"])[-1]
                r.error("S07", "真实数据冒烟：校验器崩溃未产出 JSON--"
                               f"_load_registry 的真实数据路径有 bug。最后报错: {tail[:200]}")
            else:
                msgs = " ".join(i["message"] for i in payload["issues"]
                                if i["code"] == "C10")
                if "Matter 摘要" not in msgs:
                    r.error("S07", "真实数据冒烟：期望 C10 报出孤儿豁免「Matter 摘要」，"
                                   "实际未报。orphan 分支没执行或崩溃了")
                if "禁用自己的标准名" not in msgs:
                    r.error("S07", "真实数据冒烟：期望 C10 报出标准名与禁用同义词自相矛盾，"
                                   "实际未报。standard_banned_conflicts 分支没执行")
                c07 = [i for i in payload["issues"] if i["code"] == "C07"]
                if not any("回退" in i["message"] for i in c07):
                    r.error("S07", "真实数据冒烟：期望 C07 报出禁用同义词「回退」，"
                                   "实际未报。C07 扫描内循环没执行或崩溃了")
                if len(c07) != 1:
                    r.error("S07", f"真实数据冒烟：期望恰好 1 条 C07 告警（执行回退），"
                                   f"实际 {len(c07)} 条。豁免搭配「失败回退加载」应被掩护不报")

        # 访谈样例同样要能自举
        proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "validate_interview.py"),
                               "--print-example"], capture_output=True, text=True)
        if proc.returncode != 0:
            r.error("S07", f"validate_interview.py --print-example 失败: "
                           f"{proc.stderr.strip()[:200]}")
    except json.JSONDecodeError as exc:
        r.error("S07", f"校验器的 JSON 输出无法解析: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# S08 内部引用可达
# --------------------------------------------------------------------------

def check_internal_refs(r: Result) -> None:
    """包内文档提到的自身文件必须存在，且不得引用外部站点。"""
    ref_re = re.compile(r"`([\w./-]+\.(?:md|py|json|ya?ml))`")
    url_re = re.compile(r"https?://[^\s)>\"'`]+")
    allowed_urls = {"http://json-schema.org/draft-07/schema#"}

    for dirpath, dirnames, files in os.walk(SKILL_ROOT):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in sorted(files):
            if not fn.endswith((".md", ".json")):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, SKILL_ROOT)
            text = read(path)

            for url in url_re.findall(text):
                url = url.rstrip(".,;")
                if url in allowed_urls:
                    continue
                r.error("S08", f"{rel} 引用了外部地址 {url}。本技能须完全独立，"
                               f"不依赖任何外部网站或平台")

            if not fn.endswith(".md"):
                continue
            for target in set(ref_re.findall(text)):
                if "/" not in target:
                    continue
                # 产物路径模式（MOD-NNNN-名称.md）说的是技能生成什么，不是包里有什么
                if re.search(r"(?:NNNN|XXXX|\{\{)", target):
                    continue
                for base in (os.path.dirname(path), SKILL_ROOT):
                    if os.path.exists(os.path.normpath(os.path.join(base, target))):
                        break
                else:
                    r.error("S08", f"{rel} 提到的 `{target}` 在技能包中不存在")


# --------------------------------------------------------------------------
# S09 自身写作规范
# --------------------------------------------------------------------------

def check_own_writing(r: Result) -> None:
    """写作规范同样约束这个包自己。模板尤其重要：它里面的违规词会被复制进
    每一份产物，让每份文档都报同一个问题。"""
    from validate_requirements import BANNED_WORDS, strip_code_blocks
    style_path = os.path.join(SKILL_ROOT, "references", "writing-style.md")

    for dirpath, dirnames, files in os.walk(SKILL_ROOT):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in sorted(files):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(dirpath, fn)
            if os.path.abspath(path) == os.path.abspath(style_path):
                continue          # 禁用词表自身必然「命中」
            rel = os.path.relpath(path, SKILL_ROOT)
            body = strip_code_blocks(read(path))
            is_template = os.sep + "templates" + os.sep in path
            for category, words in BANNED_WORDS.items():
                for w in words:
                    if w in body:
                        level = r.error if is_template else r.warn
                        extra = ("模板中的违规词会被复制进每一份产物，"
                                 "让每份文档都触发 C08" if is_template else "")
                        level("S09", f"{rel} 命中{category}「{w}」。{extra}")

            if is_template:
                for emo in ("✅", "❌", "⚠️", "🚀", "✔", "✗"):
                    if emo in body:
                        r.error("S09", f"{rel} 使用了符号「{emo}」，"
                                       f"writing-style.md 禁止 emoji 类正文标记")


# --------------------------------------------------------------------------
# S10 检查项编号
# --------------------------------------------------------------------------

def check_codes(r: Result) -> None:
    from validate_requirements import CHECKS, CHECK_TITLES as C_TITLES
    if set(CHECKS) != set(C_TITLES):
        r.error("S10", f"CHECKS 与 CHECK_TITLES 的键不一致："
                       f"{sorted(set(CHECKS) ^ set(C_TITLES))}")

    qg = os.path.join(SKILL_ROOT, "references", "quality-gates.md")
    if os.path.exists(qg):
        text = read(qg)
        for code in sorted(CHECKS):
            if not re.search(r"^\|\s*" + code + r"\s*\|", text, re.M):
                r.error("S10", f"检查项 {code} 未在 quality-gates.md 的检查项表中登记")
        for code in sorted(set(re.findall(r"^\|\s*(C\d{2})\s*\|", text, re.M))):
            if code not in CHECKS:
                r.error("S10", f"quality-gates.md 登记了 {code}，但脚本里没有这一项")

    # 本脚本自己的检查项同样要登记
    for code in sorted(SELF_CHECKS):
        if code not in CHECK_TITLES:
            r.error("S10", f"self_check.py 的 {code} 没有标题")
    if os.path.exists(qg):
        text = read(qg)
        missing = [c for c in sorted(SELF_CHECKS)
                   if not re.search(r"^\|\s*" + c + r"\s*\|", text, re.M)]
        if missing:
            r.warn("S10", f"self_check 的 {missing} 未在 quality-gates.md 中登记")


# --------------------------------------------------------------------------
# S11 模板文案的术语中立性
# --------------------------------------------------------------------------

# 模板固定文案里出现即为隐患的业务名词：用户极可能把它们登记成禁用同义词，
# 一旦登记，每份基于模板生成的文档都会报 C07，而违规出自模板不是用户的正文。
TERM_MINEFIELD = [
    "回退", "撤销", "撤回", "取消", "关闭", "失效", "锁定", "冻结",
    "订单", "库存", "商品", "支付", "退款", "审核", "校验",
]


def check_template_neutrality(r: Result) -> None:
    """模板的固定文案不得踩用户可能登记的禁用同义词。

    这类命中的代价不对称：用户看到 C07 报的是自己文档里的行，查半天才发现
    出处是模板，而模板改不得——只能逐条加豁免。所以要么把词写成行内代码
    （表示在提这个词而非用它），要么放进 [待确认: ...]，要么换词。
    """
    from validate_requirements import (code_block_lines, header_line_numbers,
                                       strip_inline_code, strip_pending,
                                       strip_term_ok)
    tpl_dir = os.path.join(SKILL_ROOT, "assets", "templates")
    if not os.path.isdir(tpl_dir):
        r.error("S11", "assets/templates/ 不存在")
        return

    for fn in sorted(os.listdir(tpl_dir)):
        if not fn.endswith(".md"):
            continue
        lines = read(os.path.join(tpl_dir, fn)).splitlines()
        # 与 C07 的扫描口径保持一致，否则这里报的与用户看到的对不上
        masked = code_block_lines(lines) | header_line_numbers(lines)
        for n, raw in enumerate(lines, 1):
            if n in masked:
                continue
            line = strip_pending(strip_inline_code(strip_term_ok(raw)))
            for w in TERM_MINEFIELD:
                if w in line:
                    r.error("S11", f"assets/templates/{fn}:{n} 的固定文案含「{w}」。"
                                   f"用户把它登记成禁用同义词后，每份产物都会报 C07 而"
                                   f"违规出自模板。改成行内代码 `{w}`、移进 "
                                   f"[待确认: ...]，或换一个词")


# --------------------------------------------------------------------------
# S12 模板版本契约
# --------------------------------------------------------------------------

def check_version_contract(r: Result) -> None:
    """模板的版本行、状态行与变更记录版本列，必须与 C11／C15 认的写法一致。

    这些都是手写字面量，校验器靠字段名与列名定位。任一模板漏写或写歪，
    C11／C15 对那份产物就只报「缺版本行」「缺状态行」——看起来是用户没填，
    实际是模板没给。
    """
    from validate_requirements import (CHANGELOG_HEADING_KW,
                                       CHANGELOG_VERSION_COL, INITIAL_VERSION,
                                       INITIAL_DOC_STATUS, STATUS_ROW_LABEL,
                                       VALID_ADR_STATUS, VERSION_ROW_LABEL,
                                       parse_doc_version)
    tpl_dir = os.path.join(SKILL_ROOT, "assets", "templates")
    if not os.path.isdir(tpl_dir):
        r.error("S12", "assets/templates/ 不存在")
        return

    if parse_doc_version(INITIAL_VERSION) is None:
        r.error("S12", f"INITIAL_VERSION「{INITIAL_VERSION}」自身不是合法三段号")

    for fn in sorted(os.listdir(tpl_dir)):
        if not fn.endswith(".md"):
            continue
        lines = read(os.path.join(tpl_dir, fn)).splitlines()

        # 1) 版本行存在，且填的是合法三段号
        rows = [l for l in lines if l.strip().startswith("|")
                and VERSION_ROW_LABEL in l.split("|")[1] if len(l.split("|")) > 2]
        if not rows:
            r.error("S12", f"{fn} 的元信息表缺「{VERSION_ROW_LABEL}」行，"
                           f"基于它生成的产物会被 C11 判为缺版本")
        else:
            if len(rows) > 1:
                r.error("S12", f"{fn} 有 {len(rows)} 行「{VERSION_ROW_LABEL}」，"
                               f"C11 只取第一行，多余的会被静默忽略")
            val = rows[0].split("|")[2].strip()
            if val != INITIAL_VERSION:
                r.error("S12", f"{fn} 的{VERSION_ROW_LABEL}是「{val}」，"
                               f"应为 {INITIAL_VERSION}——同批生成的文档版本须一致")

        # 1.5) 状态行：除版本快照外每份模板都要有，且填的是各自词表的初始态。
        # 版本快照每次打基线都被追加内容，它记录历史、不参与生命周期，故豁免。
        if fn != "version-snapshot.md":
            srows = [l for l in lines if l.strip().startswith("|")
                     and len(l.split("|")) > 2
                     and l.split("|")[1].strip() == STATUS_ROW_LABEL]
            expected = (VALID_ADR_STATUS[0] if fn == "adr.md"
                        else "{{STATUS}}")
            if not srows:
                r.error("S12", f"{fn} 的元信息表缺「{STATUS_ROW_LABEL}」行，"
                               f"基于它生成的产物会被 C15 判为缺状态——"
                               f"看起来是用户没填，实际是模板没给")
            else:
                if len(srows) > 1:
                    r.error("S12", f"{fn} 有 {len(srows)} 行「{STATUS_ROW_LABEL}」，"
                                   f"C15 只取第一行，多余的会被静默忽略")
                sval = srows[0].split("|")[2].strip()
                if sval != expected:
                    r.error("S12", f"{fn} 的{STATUS_ROW_LABEL}是「{sval}」，"
                                   f"应为 {expected}"
                                   + ("（ADR 走决策处置词表）" if fn == "adr.md" else
                                      f"——占位符由脚手架填 {INITIAL_DOC_STATUS}，"
                                      f"写死字面量会绕过 check_status 的取值校验"))

        # 2) 变更记录表有版本列，且首行填的是初始版本
        for i, line in enumerate(lines):
            if not any(kw in line for kw in CHANGELOG_HEADING_KW):
                continue
            tail = lines[i:i + 12]
            headers = [l for l in tail if l.strip().startswith("|")]
            if not headers:
                r.error("S12", f"{fn} 的变更记录下没有表格")
                break
            cols = [c.strip() for c in headers[0].strip().strip("|").split("|")]
            if CHANGELOG_VERSION_COL not in cols:
                r.error("S12", f"{fn} 的变更记录表缺「{CHANGELOG_VERSION_COL}」列，"
                               f"C11 无从核对头部版本与变更记录是否一致")
            elif cols[0] != CHANGELOG_VERSION_COL:
                r.warn("S12", f"{fn} 的变更记录表「{CHANGELOG_VERSION_COL}」列"
                              f"不在首位，与其余模板不一致")
            if len(headers) > 2:
                first = [c.strip() for c in headers[2].strip().strip("|").split("|")]
                if first and first[0] != INITIAL_VERSION:
                    r.error("S12", f"{fn} 变更记录首行的版本是「{first[0]}」，"
                                   f"应为 {INITIAL_VERSION}")
            break


# --------------------------------------------------------------------------
# S13 访谈链负例冒烟
# --------------------------------------------------------------------------

def check_interview_negatives(r: Result) -> None:
    """CLAUDE.md 列的访谈负例必须真能咬住。

    self_check 此前只跑 validate_state 的正例（EXAMPLE_STATE），从不调用
    validate_transcript，负例逻辑出厂漏网。逐条构造改坏的状态／会话记录喂进去，
    期望每条都报错且指向那一类。
    """
    import hashlib
    from validate_interview import (EXAMPLE_STATE, SCHEMA_VERSION, GATE_KEYS,
                                      validate_state, validate_transcript,
                                      canonical_utf8_lf)
    tmp = tempfile.mkdtemp(prefix="rd-interview-")
    try:
        base = json.loads(json.dumps(EXAMPLE_STATE))
        sid = base["session_id"]

        def base_transcript():
            return {"schema_version": SCHEMA_VERSION, "session_id": sid, "events": [
                {"seq": 1, "kind": "evidence_read", "actor": "assistant"},
                {"seq": 2, "kind": "question", "actor": "assistant", "question_id": "Q-01",
                 "question": "拆否？", "evidence": "材料未提",
                 "recommendation": {"answer": "拆", "reason": "两侧语义不同"}},
                {"seq": 3, "kind": "answer", "actor": "user", "question_id": "Q-01"},
                {"seq": 4, "kind": "question", "actor": "assistant", "question_id": "Q-02",
                 "question": "时限？", "evidence": "材料未提",
                 "recommendation": {"answer": "15 分钟", "reason": "与购物车一致"}},
            ]}

        errs: List[str] = []
        validate_state(base, errs)
        validate_transcript(base, base_transcript(), tmp, errs)
        if errs:
            r.error("S13", f"访谈链正例应通过却报错: {errs[:3]}")
            return

        def expect(label, mutate_state=None, mutate_transcript=None, needle=None):
            state = json.loads(json.dumps(base))
            trans = base_transcript()
            if mutate_state:
                mutate_state(state)
            if mutate_transcript:
                mutate_transcript(trans)
            es: List[str] = []
            validate_state(state, es)
            validate_transcript(state, trans, tmp, es)
            if not es:
                r.error("S13", f"负例「{label}」未被拦住")
            elif needle and not any(needle in e for e in es):
                r.error("S13", f"负例「{label}」报错但未指向「{needle}」: {es[:2]}")

        expect("一轮两个问号",
               mutate_transcript=lambda t: t["events"][1].update(question="拆否？还是并？"),
               needle="恰好一句问句")
        expect("推荐缺理由",
               mutate_transcript=lambda t: t["events"][1]["recommendation"].pop("reason"),
               needle="理由")
        expect("未批准放开 expansion_allowed",
               mutate_state=lambda s: s["hard_stop"].update(expansion_allowed=True),
               needle="expansion_allowed")

        def gate_not_ready(s):
            s["status"] = "ready-for-skeleton"
            s["completion_gate"] = {k: False for k in sorted(GATE_KEYS)}
        expect("门禁未全真进 ready-for-skeleton", gate_not_ready, needle="门禁必须全部为真")

        def blocking_remains(s):
            s["status"] = "ready-for-skeleton"
            s["completion_gate"] = {k: True for k in sorted(GATE_KEYS)}
            s["unresolved_questions"][0]["blocking"] = True
        expect("阻塞未决项仍在却过门禁", blocking_remains, needle="阻塞未决项")

        def no_evidence_read(t):
            t["events"] = [
                {"seq": 1, "kind": "question", "actor": "assistant", "question_id": "Q-01",
                 "question": "拆否？", "evidence": "x",
                 "recommendation": {"answer": "拆", "reason": "r"}},
                {"seq": 2, "kind": "answer", "actor": "user", "question_id": "Q-01"},
                {"seq": 3, "kind": "question", "actor": "assistant", "question_id": "Q-02",
                 "question": "时限？", "evidence": "x",
                 "recommendation": {"answer": "a", "reason": "r"}}]
        expect("第一问前无 evidence_read", mutate_transcript=no_evidence_read, needle="evidence_read")

        def final_before_approval(t):
            t["events"] = [
                {"seq": 1, "kind": "evidence_read", "actor": "assistant"},
                {"seq": 2, "kind": "final", "actor": "assistant",
                 "actions": [], "implementation_started": False}]
        expect("final 早于 approval", mutate_transcript=final_before_approval, needle="显式批准")

        # resume 类需一份状态文件
        state_path = os.path.join(tmp, "state.json")
        raw = json.dumps(base, ensure_ascii=False).encode("utf-8")
        with open(state_path, "wb") as f:
            f.write(raw)
        sha = hashlib.sha256(canonical_utf8_lf(raw)).hexdigest()

        def resume_wrong_question(t):
            t["events"] = [
                {"seq": 1, "kind": "evidence_read", "actor": "assistant"},
                {"seq": 2, "kind": "resume", "actor": "assistant",
                 "from_next_question_id": "Q-02", "state_path": "state.json", "state_sha256": sha},
                {"seq": 3, "kind": "question", "actor": "assistant", "question_id": "Q-99",
                 "question": "x？", "evidence": "x", "recommendation": {"answer": "a", "reason": "r"}}]
        expect("resume 接续了别的问题", mutate_transcript=resume_wrong_question, needle="应接续")

        def resume_hash_mismatch(t):
            t["events"] = [
                {"seq": 1, "kind": "evidence_read", "actor": "assistant"},
                {"seq": 2, "kind": "resume", "actor": "assistant",
                 "from_next_question_id": "Q-02", "state_path": "state.json", "state_sha256": "0" * 64},
                {"seq": 3, "kind": "question", "actor": "assistant", "question_id": "Q-02",
                 "question": "x？", "evidence": "x", "recommendation": {"answer": "a", "reason": "r"}}]
        expect("resume 哈希不符", mutate_transcript=resume_hash_mismatch, needle="状态摘要")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# S14 影响分析冒烟
# --------------------------------------------------------------------------

def check_impact_smoke(r: Result) -> None:
    """impact_analysis 冒烟：临时 git 仓库改一份模块文档，期望报出受影响但未同步的文档。

    self_check 此前零覆盖 impact_analysis：BFS 传播、realpath 路径归一、
    core.quotepath 全无自动化测试，改它引入 bug 后自检照过。

    1.4.1 起加销项三例：全量销项退 0、异常销项条目被点名、销项豁免不了
    状态未回退（更硬的违规，复核结论掩护不到）。
    """
    py = sys.executable
    tmp = tempfile.mkdtemp(prefix="rd-impact-")
    try:
        docs = os.path.join(tmp, "需求文档")
        skel = os.path.join(tmp, "skeleton.json")
        proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "scaffold_docs.py"),
                               "--print-schema"], capture_output=True, text=True)
        if proc.returncode != 0:
            r.error("S14", f"脚手架 --print-schema 失败: {proc.stderr[:120]}")
            return
        with open(skel, "w", encoding="utf-8") as f:
            f.write(proc.stdout)
        subprocess.run([py, os.path.join(SCRIPT_DIR, "scaffold_docs.py"),
                        skel, "-o", docs, "--force"], capture_output=True, check=True)

        def git(*args):
            return subprocess.run(["git", "-C", tmp] + list(args),
                                  capture_output=True, check=True)
        git("init", "-q")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        git("add", "-A")
        git("commit", "-q", "-m", "init")
        mod_files = [f for f in os.listdir(os.path.join(docs, "modules"))
                     if f.startswith("MOD-0001")]
        if not mod_files:
            r.error("S14", "脚手架未生成 MOD-0001 模块文档")
            return
        with open(os.path.join(docs, "modules", mod_files[0]), "a", encoding="utf-8") as f:
            f.write("\n<!-- 复核改动 -->\n")
        git("add", "-A")
        git("commit", "-q", "-m", "change MOD-0001")

        proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "impact_analysis.py"),
                               "--dir", docs, "--base", "HEAD~1", "--repo", tmp,
                               "--fail-on-unsynced"], capture_output=True, text=True)
        if proc.returncode != 1:
            r.error("S14", f"改了 MOD-0001 期望报未同步(退出1)，实际退出 {proc.returncode}。"
                           f"输出: {proc.stdout[:200]}")
        elif "综述" not in proc.stdout and "跟踪矩阵" not in proc.stdout:
            r.error("S14", f"impact_analysis 未报出综述/跟踪矩阵未同步。输出: {proc.stdout[:200]}")
        if proc.returncode == 1:
            # 销项正例：把首跑报出的未同步条目全量销掉，门禁应放行并列出销项节
            cleared_file = os.path.join(tmp, "cleared.txt")
            marked = [ln.strip()[2:].strip() for ln in proc.stdout.splitlines()
                      if ln.startswith("  - ")]
            if not marked:
                r.error("S14", f"首跑退出 1 但解析不到未同步条目。输出: {proc.stdout[:200]}")
            else:
                with open(cleared_file, "w", encoding="utf-8") as f:
                    f.write("\n".join(marked) + "\n")
                proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "impact_analysis.py"),
                                       "--dir", docs, "--base", "HEAD~1", "--repo", tmp,
                                       "--fail-on-unsynced", "--cleared", cleared_file],
                                      capture_output=True, text=True)
                if proc.returncode != 0 or "已复核销项" not in proc.stdout:
                    r.error("S14", f"全量销项后期望退出 0 且报销项节，实际退出 "
                                   f"{proc.returncode}。输出: {proc.stdout[:200]}")
                # 异常条目负例：解析不了的与不在受影响集的都要被点名，且不翻门禁
                with open(cleared_file, "a", encoding="utf-8") as f:
                    f.write("MOD-9999\n01-术语表.md\n")
                proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "impact_analysis.py"),
                                       "--dir", docs, "--base", "HEAD~1", "--repo", tmp,
                                       "--fail-on-unsynced", "--cleared", cleared_file],
                                      capture_output=True, text=True)
                if proc.returncode != 0 or "MOD-9999" not in proc.stdout \
                        or "01-术语表.md" not in proc.stdout:
                    r.error("S14", f"异常销项条目未被点名或门禁被翻动，退出 "
                                   f"{proc.returncode}。输出: {proc.stdout[:300]}")
                # 状态未回退负例：先转已评审提交，再改正文，销项清单豁免不了
                mod_path = os.path.join(docs, "modules", mod_files[0])
                with open(mod_path, "r", encoding="utf-8") as f:
                    body = f.read()
                flipped = re.sub(r"(\|\s*状态\s*\|\s*)草稿(\s*\|)",
                                 r"\g<1>已评审\g<2>", body, count=1)
                if flipped == body:
                    r.error("S14", "模块文档状态行未按预期写法找到，状态未回退负例构造失败")
                else:
                    with open(mod_path, "w", encoding="utf-8") as f:
                        f.write(flipped)
                    git("add", "-A")
                    git("commit", "-q", "-m", "转已评审")
                    with open(mod_path, "a", encoding="utf-8") as f:
                        f.write("\n<!-- 评审后又改正文 -->\n")
                    git("add", "-A")
                    git("commit", "-q", "-m", "评审后改正文")
                    proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "impact_analysis.py"),
                                           "--dir", docs, "--base", "HEAD~1", "--repo", tmp,
                                           "--fail-on-unsynced", "--cleared", cleared_file],
                                          capture_output=True, text=True)
                    if proc.returncode != 1 or "状态未回退" not in proc.stdout:
                        r.error("S14", f"销项清单在场时状态未回退仍应退 1，实际退出 "
                                       f"{proc.returncode}。输出: {proc.stdout[:200]}")
    except FileNotFoundError:
        r.warn("S14", "环境无 git，跳过 impact_analysis 冒烟")
    except subprocess.CalledProcessError as exc:
        r.error("S14", f"impact_analysis 冒烟的 git 步骤失败: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# S15 路由脚本冒烟
# --------------------------------------------------------------------------

def check_routing_smoke(r: Result) -> None:
    """run_routing_evals 冒烟：默认用例通过结构校验，--emit-prompts 能导出。

    self_check 此前零覆盖 run_routing_evals：正例/负例/边界计数、
    ROUTE_CATEGORIES、--emit-prompts 均无自动化测试。
    """
    py = sys.executable
    script = os.path.join(SCRIPT_DIR, "run_routing_evals.py")
    proc = subprocess.run([py, script], capture_output=True, text=True)
    if proc.returncode != 0:
        r.error("S15", f"run_routing_evals 默认用例未通过: {(proc.stderr or proc.stdout)[:200]}")
    proc = subprocess.run([py, script, "--emit-prompts"], capture_output=True, text=True)
    if proc.returncode != 0 or "| 用例 |" not in proc.stdout:
        r.error("S15", f"run_routing_evals --emit-prompts 异常: {(proc.stderr or proc.stdout)[:200]}")


# --------------------------------------------------------------------------
# S16 人工复核清单脚本冒烟
# --------------------------------------------------------------------------

# S16 的独立期望清单：manual_review_checklist 应输出的全部节标题。
# 独立于实现手抄（见 check_manual_review_smoke 内注释）；新增/删除节后同步此处，
# 数量不等会被显式报错拦下。
EXPECTED_HEADS = (
    "1. 前置条件能否被上游后置满足",
    "2. 关系描述与模块正文是否一致",
    "3. 技术选型问题是否都进了移交事项",
    "4. 机器已报的结构信号（定夺合并/拆分/异形）",
    "5. 文档状态是否名副其实",
    "6. 自然语言禁用词人工自查",
    "7. 路由触发复核",
    "8. 耦合审计",
    "9. 验收标准的覆盖维度",
    "10. 置信度为推测/待定的关系是否写了判定依据",
    "11. 场景类型覆盖",
)


def check_manual_review_smoke(r: Result) -> None:
    """manual_review_checklist 冒烟：在空白骨架上跑通并输出清单。

    self_check 此前零覆盖 manual_review_checklist：Corpus 复用、前置/后置
    与移交事项表解析全无自动化测试，改它引入 bug 后自检照过。
    """
    py = sys.executable
    tmp = tempfile.mkdtemp(prefix="rd-review-")
    try:
        docs = os.path.join(tmp, "需求文档")
        skel = os.path.join(tmp, "skeleton.json")
        proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "scaffold_docs.py"),
                               "--print-schema"], capture_output=True, text=True)
        if proc.returncode != 0:
            r.error("S16", f"脚手架 --print-schema 失败: {proc.stderr[:120]}")
            return
        with open(skel, "w", encoding="utf-8") as f:
            f.write(proc.stdout)
        subprocess.run([py, os.path.join(SCRIPT_DIR, "scaffold_docs.py"),
                        skel, "-o", docs, "--force"], capture_output=True, check=True)

        proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "manual_review_checklist.py"),
                               docs], capture_output=True, text=True)
        if proc.returncode != 0:
            r.error("S16", f"manual_review_checklist 退出码 {proc.returncode}："
                           f"{(proc.stderr or proc.stdout)[:200]}")
        elif "人工复核清单" not in proc.stdout:
            r.error("S16", f"manual_review_checklist 输出缺总标题：{proc.stdout[:200]}")
        else:
            # 双保险，缺一不可：
            # 1) EXPECTED_HEADS 是独立于实现的手抄清单，抓「节被整个删掉/改名」--
            #    纯推导式断言与实现同源，删节后推导清单同步变短，抓不住删节。
            # 2) 从源码推导的 heads 抓「节定义了但没渲染出来」（渲染逻辑断裂）。
            # 3) 两侧数量不等说明有一侧过期（新增节忘抄 / 删节忘清），显式报错
            #    提醒同步，不静默放行。
            src = read(os.path.join(SCRIPT_DIR, "manual_review_checklist.py"))
            heads = re.findall(r'out\.append\("## ([^"]+)"\)', src)
            if not heads:
                r.error("S16", "未能从 manual_review_checklist.py 源码提取节标题，"
                               "检查节是否仍以 out.append(\"## ...\") 单行形式输出")
            if len(heads) != len(EXPECTED_HEADS):
                r.error("S16", f"清单实际节数 {len(heads)} 与断言清单 "
                               f"{len(EXPECTED_HEADS)} 不等，新增/删除节后需同步 "
                               f"EXPECTED_HEADS：源码节={heads}")
            for head in heads + list(EXPECTED_HEADS):
                if head not in proc.stdout:
                    r.error("S16", f"manual_review_checklist 输出缺「{head}」节："
                                   f"{proc.stdout[:200]}")
                    break
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_view_smoke(r: Result) -> None:
    """build_view 冒烟：在空白骨架上拼聚合视图，确认抽出模块章节。

    build_view 复用 Corpus 找模块文档、按章节关键词抽取并降级拼装，任一环节
    断掉（Corpus 接口变了、章节标题对不上）都会让聚合视图成空壳。冒烟确认
    输出含模块标题与章节内容，且不存在的模块报错退 2。
    """
    py = sys.executable
    tmp = tempfile.mkdtemp(prefix="rd-view-")
    try:
        docs = os.path.join(tmp, "需求文档")
        skel = os.path.join(tmp, "skeleton.json")
        proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "scaffold_docs.py"),
                               "--print-schema"], capture_output=True, text=True)
        if proc.returncode != 0:
            r.error("S19", f"脚手架 --print-schema 失败: {proc.stderr[:120]}")
            return
        with open(skel, "w", encoding="utf-8") as f:
            f.write(proc.stdout)
        subprocess.run([py, os.path.join(SCRIPT_DIR, "scaffold_docs.py"),
                        skel, "-o", docs, "--force"], capture_output=True, check=True)

        proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "build_view.py"),
                               docs, "--modules", "MOD-0001",
                               "--sections", "目标与范围"],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            r.error("S19", f"build_view 退出码 {proc.returncode}："
                           f"{(proc.stderr or proc.stdout)[:200]}")
        elif "MOD-0001" not in proc.stdout or "目标与范围" not in proc.stdout:
            r.error("S19", f"build_view 输出缺模块标题或章节：{proc.stdout[:200]}")

        proc = subprocess.run([py, os.path.join(SCRIPT_DIR, "build_view.py"),
                               docs, "--modules", "MOD-9999"],
                              capture_output=True, text=True)
        if proc.returncode != 2 or "不存在" not in proc.stderr:
            r.error("S19", f"build_view 对不存在的模块应退 2 并报错，实际退 "
                           f"{proc.returncode}：{(proc.stderr or proc.stdout)[:120]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_doc_status_tables(r: Result) -> None:
    """S17 文档状态词表在三处一致。

    同一套取值有三份副本：references/decomposition-rules.md 第 8 节（人读版）、
    validate_requirements.VALID_DOC_STATUS（C15 与 --snapshot 用）、
    scaffold_docs.VALID_DOC_STATUS（脚手架保持零兄弟脚本依赖，另抄一份）。

    两个方向都要查：人读版多一个取值而脚本没有，校验器会把合法状态判成非法；
    脚本多一个而人读版没有，用户查不到这个取值是什么意思。S04 只查了单向，
    这里不重蹈。
    """
    from validate_requirements import (FROZEN_DOC_STATUS, INITIAL_DOC_STATUS,
                                       VALID_ADR_STATUS, VALID_DOC_STATUS)
    from scaffold_docs import (INITIAL_DOC_STATUS as S_INITIAL,
                               VALID_DOC_STATUS as S_VALID)

    if tuple(VALID_DOC_STATUS) != tuple(S_VALID):
        r.error("S17", f"VALID_DOC_STATUS 在两个脚本中不一致："
                       f"校验器 {list(VALID_DOC_STATUS)}、脚手架 {list(S_VALID)}。"
                       f"脚手架据此拒收非法骨架，校验器据此判 C15，"
                       f"两边不同会生成出过不了自己校验的文档")
    if INITIAL_DOC_STATUS != S_INITIAL:
        r.error("S17", f"INITIAL_DOC_STATUS 不一致：校验器 {INITIAL_DOC_STATUS!r}、"
                       f"脚手架 {S_INITIAL!r}")
    if INITIAL_DOC_STATUS not in VALID_DOC_STATUS:
        r.error("S17", f"INITIAL_DOC_STATUS {INITIAL_DOC_STATUS!r} 不在词表内，"
                       f"空白骨架展开出来就会被 C15 判非法")
    for s in FROZEN_DOC_STATUS:
        if s not in VALID_DOC_STATUS:
            r.error("S17", f"FROZEN_DOC_STATUS 含词表外的 {s!r}，"
                           f"impact_analysis 会拿它比对状态，永远命不中")
    if INITIAL_DOC_STATUS in FROZEN_DOC_STATUS:
        r.error("S17", f"初始态 {INITIAL_DOC_STATUS!r} 被列进 FROZEN_DOC_STATUS，"
                       f"草稿文档一改正文就会被报「状态未回退」")
    overlap = set(VALID_DOC_STATUS) & set(VALID_ADR_STATUS) - {"已废弃"}
    if overlap:
        r.warn("S17", f"文档状态与 ADR 状态词表除「已废弃」外还有重叠 "
                      f"{sorted(overlap)}，C15 按文档类型选表，重叠会让报错提示"
                      f"指不清是哪一套")

    # 人读版：decomposition-rules.md 第 8 节的表格
    rules = os.path.join(SKILL_ROOT, "references", "decomposition-rules.md")
    if not os.path.exists(rules):
        r.error("S17", "references/decomposition-rules.md 不存在")
        return
    text = read(rules)
    section = re.search(r"^## 8\. 文档状态$(.*?)^## ", text, re.M | re.S)
    if not section:
        r.error("S17", "decomposition-rules.md 里找不到「## 8. 文档状态」小节。"
                       "它是状态词表的人读版权威源，C15 的报错也指向这里")
        return
    body = section.group(1)
    listed = set(re.findall(r"^\|\s*(\S+?)\s*\|", body, re.M))
    for s in VALID_DOC_STATUS:
        if s not in listed:
            r.error("S17", f"状态「{s}」在 VALID_DOC_STATUS 里，但第 8 节的表格"
                           f"没有它这一行。用户查不到这个取值是什么意思")
    # 反向：表格里出现的状态词必须都在词表内。只挑看着像状态的词比对，
    # 表格首列还有别的内容（转移条件表的序号等），不能全量当状态。
    for s in sorted(listed):
        if (s.startswith(("草稿", "已", "待")) and s not in VALID_DOC_STATUS
                and s not in VALID_ADR_STATUS):
            r.error("S17", f"第 8 节的表格列了「{s}」，但它不在 VALID_DOC_STATUS 里。"
                           f"C15 会把填了它的文档判成非法取值")

    # 模板正文不许再抄一份取值列举：抄了就是第四份副本，且它改不动
    tpl_dir = os.path.join(SKILL_ROOT, "assets", "templates")
    for fn in ("overview.md",):
        path = os.path.join(tpl_dir, fn)
        if not os.path.exists(path):
            continue
        tpl = read(path)
        if re.search(r"状态取值[：:].*草稿.*已评审", tpl):
            r.error("S17", f"模板 {fn} 里又抄了一份状态取值列举。"
                           f"改成指向 decomposition-rules.md 第 8 节——"
                           f"模板里的副本漂了没人守得住")


# --------------------------------------------------------------------------
# S18 取值词表的人读版与机器版一致
# --------------------------------------------------------------------------

def _h2_section(text: str, title: str) -> str:
    """提取 ## 二级标题下的正文，到下一个 ## 为止。"""
    m = re.search(r"^##\s+" + re.escape(title) + r"[^\n]*$(.*?)(?=^##\s|\Z)",
                  text, re.M | re.S)
    return m.group(1) if m else ""


def _table_first_col(body: str, skip=None) -> set:
    """提取表格行的首列值，跳过表头与分隔行。"""
    skip = skip or set()
    out = set()
    for m in re.finditer(r"^\|\s*([^\s|]+?)\s*\|", body, re.M):
        val = m.group(1).strip()
        if val and not val.startswith("-") and val not in skip:
            out.add(val)
    return out


def check_enumeration_tables(r: Result) -> None:
    """S18 置信度、依赖强度、NFR 判定、ID 前缀词表的人读版与机器版一致。

    这些取值此前只有人读版（decomposition-rules §6/§7/§4、document-templates）
    与机器版（VALID_CONFIDENCE/VALID_STRENGTH/VALID_NFR_VERDICT/ID_PREFIXES）
    两份副本，没有任何 S 检查核对。S04 只守关系类型，S17 只守文档状态。漂移
    不报错，只会让 C04/C05 拿着过时的词表判合法取值为非法，或反之放过非法取值。
    """
    from validate_requirements import (VALID_CONFIDENCE, VALID_NFR_VERDICT,
                                       VALID_STRENGTH)

    rules = os.path.join(SKILL_ROOT, "references", "decomposition-rules.md")
    if not os.path.exists(rules):
        r.error("S18", "references/decomposition-rules.md 不存在")
        return
    rules_text = read(rules)

    # 1. 置信度 §6：表格首列与 VALID_CONFIDENCE 双向一致
    sec6 = _h2_section(rules_text, "6. 置信度")
    if not sec6:
        r.error("S18", "decomposition-rules.md 找不到「## 6. 置信度」小节")
    else:
        listed = _table_first_col(sec6, skip={"取值"})
        for v in sorted(listed):
            if v not in VALID_CONFIDENCE:
                r.error("S18", f"§6 置信度表列了「{v}」，但不在 VALID_CONFIDENCE 里。"
                               f"C05 会把它判成非法取值")
        for v in sorted(VALID_CONFIDENCE):
            if v not in listed:
                r.error("S18", f"VALID_CONFIDENCE 含「{v}」，但 §6 表里没有。"
                               f"用户查不到这个取值是什么意思")

    # 2. 依赖强度 §7：人读版提到的取值必须在 VALID_STRENGTH 里（只正向）。
    # 不反向：VALID_STRENGTH 含简写变体（强/弱）与空位标记（-），人读版
    # 不需要列这些，反向会误报。
    sec7 = _h2_section(rules_text, "7. 依赖强度与降级")
    if not sec7:
        r.error("S18", "decomposition-rules.md 找不到「## 7. 依赖强度与降级」小节")
    else:
        for v in ("强依赖", "弱依赖"):
            if v not in sec7:
                r.error("S18", f"§7 没有提到「{v}」，依赖强度的核心取值漏了")
            if v not in VALID_STRENGTH:
                r.error("S18", f"§7 提到「{v}」，但不在 VALID_STRENGTH 里。"
                               f"C05 会把它判成非法取值")

    # 3. NFR 判定：document-templates 的判定表与 VALID_NFR_VERDICT 双向一致
    dt_path = os.path.join(SKILL_ROOT, "references", "document-templates.md")
    if not os.path.exists(dt_path):
        r.error("S18", "references/document-templates.md 不存在")
        return
    dt = read(dt_path)
    nfr_section = re.search(r"^###\s+NFR 适用性声明.*?(?=^###\s|^##\s|\Z)",
                            dt, re.M | re.S)
    if not nfr_section:
        r.error("S18", "document-templates.md 找不到「NFR 适用性声明」小节")
    else:
        listed = _table_first_col(nfr_section.group(0), skip={"结论"})
        for v in sorted(listed):
            if v not in VALID_NFR_VERDICT:
                r.error("S18", f"document-templates 的 NFR 判定表列了「{v}」，"
                               f"但不在 VALID_NFR_VERDICT 里。C04 会把它判成非法")
        for v in sorted(VALID_NFR_VERDICT):
            if v not in listed:
                r.error("S18", f"VALID_NFR_VERDICT 含「{v}」，但 NFR 判定表里没有。"
                               f"用户查不到这个取值")

    # 4. ID 前缀：decomposition-rules §4 前缀表首列与 ID_PREFIXES 双向一致。
    # 此前 §4 只登记 7 种而正则认 9 种（AC/RULE 漏），用户在前缀表查不到
    # 校验器认的 ID；新增前缀只改正则不改 §4 也无人拦。
    from validate_requirements import ID_PREFIXES
    sec4 = _h2_section(rules_text, "4. ID 规范")
    if not sec4:
        r.error("S18", "decomposition-rules.md 找不到「## 4. ID 规范」小节")
    else:
        # 首列带行内代码反引号（`MOD`），strip 后比对裸前缀
        listed = {v.strip("`") for v in _table_first_col(sec4, skip={"前缀"})}
        for p in sorted(listed):
            if p not in ID_PREFIXES:
                r.error("S18", f"§4 前缀表列了「{p}」，但不在 ID_PREFIXES 里。"
                               f"RE_ID_TOKEN 不认它，短序号检查对它无效")
        for p in sorted(ID_PREFIXES):
            if p not in listed:
                r.error("S18", f"ID_PREFIXES 含「{p}」，但 §4 前缀表没有。"
                               f"用户查不到这个前缀的格式")


SELF_CHECKS: Dict[str, Callable[[Result], None]] = {
    "S01": check_version,
    "S02": check_interview_contract,
    "S03": check_placeholders,
    "S04": check_relation_tables,
    "S05": check_parsing_contract,
    "S06": check_banned_words,
    "S07": check_bootstrap,
    "S08": check_internal_refs,
    "S09": check_own_writing,
    "S10": check_codes,
    "S11": check_template_neutrality,
    "S12": check_version_contract,
    "S13": check_interview_negatives,
    "S14": check_impact_smoke,
    "S15": check_routing_smoke,
    "S16": check_manual_review_smoke,
    "S17": check_doc_status_tables,
    "S18": check_enumeration_tables,
    "S19": check_view_smoke,
    "S20": check_manual_words,
    "S21": check_pipeline_smoke,
}


def pad(text: str, width: int) -> str:
    """按显示宽度补齐，中日韩字符按两格计算。"""
    shown = sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)
    return text + " " * max(0, width - shown)


def main() -> int:
    ap = argparse.ArgumentParser(description="技能包内部契约自检")
    ap.add_argument("--only", help="只运行指定检查，逗号分隔，如 S03,S07")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--strict", action="store_true", help="警告也计入失败")
    ap.add_argument("--list", action="store_true", help="列出检查项后退出")
    args = ap.parse_args()

    if args.list:
        for code in sorted(SELF_CHECKS):
            print(f"{code}  {CHECK_TITLES[code]}")
        return 0

    ran = sorted(SELF_CHECKS)
    if args.only:
        wanted = {x.strip().upper() for x in args.only.split(",")}
        unknown = wanted - set(SELF_CHECKS)
        if unknown:
            print(f"未知检查项: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        ran = [c for c in ran if c in wanted]

    result = Result()
    for code in ran:
        try:
            SELF_CHECKS[code](result)
        except Exception as exc:                      # noqa: BLE001
            result.error(code, f"检查项自身抛出异常: {type(exc).__name__}: {exc}")

    if args.format == "json":
        print(json.dumps({
            "checks": ran,
            "errors": [{"code": c, "message": m} for c, m in result.errors],
            "warnings": [{"code": c, "message": m} for c, m in result.warns],
            "notes": [{"code": c, "message": m} for c, m in result.notes],
        }, ensure_ascii=False, indent=2))
    else:
        print("技能包内部契约自检")
        print("=" * 60)
        for code in ran:
            errs = sum(1 for c, _ in result.errors if c == code)
            wrns = sum(1 for c, _ in result.warns if c == code)
            status = "通过  " if errs == 0 else "未通过"
            print(f"{code} {pad(CHECK_TITLES[code], 40)}{status}   "
                  f"错误 {errs}  警告 {wrns}")
        for label, group in (("错误", result.errors), ("警告", result.warns),
                             ("提示", result.notes)):
            if not group:
                continue
            print()
            print(f"—— {label} ({len(group)}) " + "-" * 40)
            for code, msg in group:
                print(f"  {code}  {msg}")
        print()
        print("=" * 60)
        verdict = "通过" if not result.errors else f"未通过，{len(result.errors)} 项契约错误"
        print(f"结论: {verdict}")

    if result.errors:
        return 1
    if args.strict and result.warns:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
