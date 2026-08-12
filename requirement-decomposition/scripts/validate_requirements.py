#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""需求文档网络一致性校验。

扫描需求文档目录，检查 ID 引用完整性、NFR 覆盖率、关系矩阵对称性、
跟踪矩阵完整性、术语一致性与写作规范，输出报告并以退出码表示结果。

用法:
    python3 validate_requirements.py <需求文档目录>
    python3 validate_requirements.py ./需求文档 --format json
    python3 validate_requirements.py ./需求文档 --strict        # 警告也算失败
    python3 validate_requirements.py ./需求文档 --only C02,C05  # 只跑指定检查

退出码: 0 = 通过, 1 = 存在 ERROR, 2 = 用法错误
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Set, Tuple

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

RE_MOD = re.compile(r"\bMOD-\d{4,}(?:-[A-Z])?\b")
RE_REQ = re.compile(r"\bMOD-\d{4,}(?:-[A-Z])?-REQ-\d{4,}\b")
RE_NFR = re.compile(r"\bNFR-\d{4,}\b")
RE_UC = re.compile(r"\bUC-\d{4,}\b")
RE_TERM = re.compile(r"\bTERM-\d{4,}\b")
RE_UI = re.compile(r"\bUI-\d{4,}\b")
RE_ADR = re.compile(r"\bADR-\d{4,}\b")
# 整串扫描，用于查任一段序号不足四位的写法。不能只匹配首段：
# MOD-0001-REQ-01 的首段合法、尾段不合法，漏掉它等于既不报悬空也不生效。
RE_ID_TOKEN = re.compile(
    r"\b(?:MOD|NFR|UC|TERM|UI|ADR|RULE|AC|REQ)(?:-(?:[0-9]+|[A-Z]{1,4}))+")
RE_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
RE_HEADING = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)

# --------------------------------------------------------------------------
# 文档定位约定
#
# 校验器不认路径，只认关键词：文件名关键词定位产物，标题／表头关键词定位表格。
# 模板改了标题或表头而这里没同步，解析不到内容会被当成通过——所以两边必须
# 出自同一份定义。self_check.py 导入本节常量校验模板，不再手抄一份。
# --------------------------------------------------------------------------

# 产物 -> 文件名须含的关键词（任一命中即可）
DOC_KEYWORDS = {
    "overview": ["综述", "overview"],
    "glossary": ["术语表", "glossary"],
    "traceability": ["跟踪矩阵", "traceability"],
    "release": ["版本计划", "release"],
    "snapshot": ["版本快照", "snapshot"],
}

# 表格 -> (所在模板, 标题关键词, 表头关键词)
# pick_table 先按标题匹配，匹配不到再用表头兜底。
TABLE_SPECS = {
    "module_index": ("overview.md", "模块索引表",
                     ["模块索引", "模块清单", "模块目录"], ["模块", "状态"]),
    "nfr": ("overview.md", "全局 NFR 表",
            ["非功能", "NFR"], ["NFR", "阈值"]),
    "relations": ("overview.md", "关系矩阵表",
                  ["关系矩阵"], ["关系类型", "置信度"]),
    "nfr_declaration": ("module.md", "NFR 适用性声明表",
                        ["NFR 适用性", "NFR适用性", "非功能需求适用性"], ["NFR", "结论"]),
    "local_relations": ("module.md", "关系本地视图表",
                        ["关系本地视图", "关系视图", "本地关系"], ["关系类型"]),
    "business_rules": ("module.md", "业务规则清单表",
                       ["业务规则"], ["规则 ID", "规则描述"]),
    "data_requirements": ("module.md", "数据需求表",
                          ["数据需求"], ["实体", "写主"]),
}

# 跟踪矩阵主表的必需列，C06 逐条核对
TRACE_COLUMNS = ["需求", "模块", "场景", "优先级", "来源", "验收", "版本"]

# 关系矩阵的列名与别名，C05 与 impact_analysis 共用这一份。
# 此前两处各硬编码一份别名，改表头时 S05 同步 TABLE_SPECS 但同步不到这两处--
# C05 静默读到空列、impact_analysis 传播图断链。收进一处定义，S05 核对首个
# （标准名）在模板表头里出现。别名保留：存量文档可能用简写（「源」而非「源模块」）。
RELATION_COLUMNS = {
    "source": ("源模块", "源", "起点", "A 模块"),
    "target": ("目标模块", "目标", "终点", "B 模块"),
    "type": ("关系类型", "类型"),
    "confidence": ("置信度",),
    "strength": ("依赖强度", "强度"),
    "fallback": ("降级策略", "降级"),
}

# 术语表主表的列名约定：主表靠 identity 命中，另两列各自驱动一项机制
GLOSSARY_COLUMNS = {
    "identity": ["标准名", "术语"],
    "banned": "禁用同义词",
    "exemption": "豁免上下文",
}

# 术语表单元格的短语分隔符：顿号、中英文逗号、斜杠、分号。刻意不含空白--
# 豁免搭配可能是含空格的复合短语（如「Matter 摘要」），拿空白当分隔符会把它拆成
# 两个短语，不含禁用同义词的那半截变成假孤儿、C10 误报。多个短语用顿号分隔；
# 标准名双语并列靠 `/` 拆（Order / 订单），两侧空格由各元素的 strip 吃掉。
PHRASE_SEPARATORS = r"[、,，/;；]+"

# 子目录名，脚手架按此展开，校验器按此归类
SUBDIRS = {"modules": "modules", "scenarios": "scenarios", "decisions": "decisions"}


def table_spec(key: str) -> Tuple[List[str], Optional[List[str]]]:
    """取某张表的 (标题关键词, 表头关键词)，供 pick_table 使用。"""
    _, _, heading_kw, header_kw = TABLE_SPECS[key]
    return heading_kw, header_kw


# --------------------------------------------------------------------------
# 文档版本契约
#
# 「版本」在需求文档里有三种互不相干的语义，混用是版本号写法漂移的根源。
# 只有第一种由本技能定义格式，另两种沿用产品既有约定，不强加三段号。
#
#   文档修订  这份 Markdown 改到第几版        V1.0.0   本节定义
#   产品发布  需求排进哪个迭代上线            V1       产品约定
#   模块引入  模块从哪个产品版本开始存在      V1       引用产品版本
# --------------------------------------------------------------------------

RE_DOC_VERSION = re.compile(r"^[Vv](\d+)\.(\d+)\.(\d+)$")

# 文档版本行的字段名与变更记录表的版本列名，模板与校验器共用这一份
VERSION_ROW_LABEL = "文档版本"
CHANGELOG_HEADING_KW = ["变更记录", "修订记录"]
CHANGELOG_VERSION_COL = "版本"
INITIAL_VERSION = "V1.0.0"

# --------------------------------------------------------------------------
# 文档状态（C15）
#
# 版本号回答「改到第几版」，状态回答「这一版能不能信」。两个问题都要答，
# 缺一个读者就无从判断手里这份能不能拿去实现。
#
# 词表的人读版在 references/decomposition-rules.md 第 8 节，scaffold_docs.py
# 另有一份独立副本（保持脚手架零兄弟脚本依赖），三处由 self_check.py --only S17
# 核对逐字一致。ADR 走自己的词表（提议/已采纳/已废弃/被取代），管的是决策的
# 处置结果而非文档修订的生命周期，两套不通用。
# --------------------------------------------------------------------------

STATUS_ROW_LABEL = "状态"
VALID_DOC_STATUS = ("草稿", "已评审", "已冻结", "已废弃")
INITIAL_DOC_STATUS = "草稿"
# 已进基线的状态：正文被改动时必须先退回草稿，impact_analysis.py 也用这一份
FROZEN_DOC_STATUS = ("已评审", "已冻结")
# ADR 的独立词表，C15 按文档类型选表
VALID_ADR_STATUS = ("提议", "已采纳", "已废弃", "被取代")

# 跨版本修订标记：<!-- rev: V1.1.0 原为「保留 7 天」，因 NFR-0003 阈值调整 -->
RE_REV_MARK = re.compile(r"<!--\s*rev:\s*([Vv]\d+\.\d+\.\d+)\s+([^>]*?)\s*-->")
# 废止标注：[V1.2.0 废止: 理由]
RE_DEPRECATED = re.compile(r"\[([Vv]\d+\.\d+\.\d+)\s*废止[:：]\s*([^\]]*)\]")


def parse_doc_version(text: str) -> Optional[Tuple[int, int, int]]:
    """三段文档版本号 -> (主, 次, 修订)，格式不合返回 None。"""
    m = RE_DOC_VERSION.match(text.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def version_step(prev: Tuple[int, int, int],
                 cur: Tuple[int, int, int]) -> Optional[str]:
    """相邻两版之间的进位是否合法，合法返回 None，否则返回问题描述。

    一次只进一位、进位时低位归零——判据与技能包自身的版本规则同源。
    """
    if cur <= prev:
        return "版本号未递增"
    pmaj, pmin, ppat = prev
    cmaj, cmin, cpat = cur
    if cmaj == pmaj and cmin == pmin and cpat == ppat + 1:
        return None
    if cmaj == pmaj and cmin == pmin + 1 and cpat == 0:
        return None
    if cmaj == pmaj + 1 and cmin == 0 and cpat == 0:
        return None
    return "一次只能进一位，且进位时低位归零"


# --------------------------------------------------------------------------
# 取值词表
# --------------------------------------------------------------------------

VALID_RELATIONS = {
    "依赖", "关联", "组合", "顺序", "互斥", "约束", "数据共享", "事件触发", "回退",
    "被依赖", "隶属", "后置于", "受约束于", "被触发", "补偿对象",
}
RELATION_MIRROR = {
    "依赖": "被依赖", "被依赖": "依赖",
    "组合": "隶属", "隶属": "组合",
    "顺序": "后置于", "后置于": "顺序",
    "约束": "受约束于", "受约束于": "约束",
    "事件触发": "被触发", "被触发": "事件触发",
    "回退": "补偿对象", "补偿对象": "回退",
    "关联": "关联", "互斥": "互斥", "数据共享": "数据共享",
}
VALID_CONFIDENCE = {"已证实", "推测", "待定"}
VALID_STRENGTH = {"强依赖", "弱依赖", "强", "弱", "不适用", "—", "-"}
VALID_NFR_VERDICT = {"完全适用", "部分适用", "不适用"}

# 表格里表示「此格无内容」的写法。模板预填的就是「——」，漏掉它会让这个占位
# 被当成真实取值：填进豁免上下文列时，全库每一行破折号都成了万能豁免。
EMPTY_MARKERS = {"无", "——", "—", "--", "-", "/", "n/a", "na", "暂无", "无同义词", "不适用"}


def is_empty_marker(cell: str) -> bool:
    return cell.strip().lower() in EMPTY_MARKERS


# 无效理由：整格就只有这么一个词。全等比较。
EMPTY_REASON_PATTERNS = [
    "不涉及", "无关", "不适用", "n/a", "na", "无", "暂无", "略", "—", "--", "-", "/",
]

# 无效理由的句式：修饰 + 否定 + 指代，去掉这三段后不剩实质内容。
# 「本模块不涉及该项」比「不涉及」长，说的是同一件事。
TAUTOLOGY_RE = re.compile(
    r"^(?:本模块|该模块|模块|本文档|此处)?(?:与本模块)?"
    r"(?:"
    r"(?:不涉及|无关|不适用|没有涉及|不相关|未涉及|不需要|无此需求|无相关需求|不做要求)"
    r"(?:该项|此项|本项|该需求|这一项|相关内容|该场景|此类场景|相关场景)?"
    r"|"
    r"(?:该项|此项|本项|该需求|这一项|相关内容|该场景|此类场景|相关场景)"
    r"(?:不涉及|无关|不适用|没有涉及|不相关|未涉及|不需要|无此需求|无相关需求|不做要求)"
    r")$"
)


def is_tautology(reason: str) -> bool:
    """判断理由是否只是把结论换个说法重复一遍。"""
    plain = reason.strip().lower().rstrip("。.；;，,")
    if not plain:
        return True
    if plain in EMPTY_REASON_PATTERNS:
        return True
    return bool(TAUTOLOGY_RE.fullmatch(plain))


# 写作规范禁用词，须与写作规范文档中「脚本扫描」一栏逐字一致。
# 该文档另有一栏是「人工自查」：中文没有词边界，短词作为子串会误伤合法表述
# （「大量」会命中「大量数据」），那些词只登记不入库。self_check.py 核对两栏。
BANNED_WORDS = {
    "空洞形容词": ["高效", "便捷", "友好", "强大", "灵活", "丰富", "完善", "全面",
                   "优质", "卓越", "极致", "丝滑", "无缝"],
    "营销黑话": ["赋能", "抓手", "闭环", "打通", "生态化", "中台化", "一站式",
                 "全方位", "深度融合", "有效提升", "显著改善"],
    "过渡废话": ["值得注意的是", "需要强调的是", "众所周知", "不难看出", "综上所述",
                 "总而言之", "总的来说", "由此可见", "在这个基础上", "与此同时"],
    "万能开场": ["本模块旨在", "本文档旨在", "为了更好地", "随着业务的发展",
                 "在当今", "通过合理的设计"],
    "模糊量词": ["一定程度上", "相对较", "较为", "通常情况下", "一般来说", "大量地"],
}

# 不含「占位」：它是合法技术词（「占位符」），子串匹配会误报正常表述。
# 残留占位用上面的 TODO/TBD/待填 与 TRACKED_MARKERS 的 [待确认] 等标记兜底。
PLACEHOLDER_MARKERS = ["TODO", "TBD", "待填", "xxx", "XXX"]
TRACKED_MARKERS = ["[待确认", "[推测", "[新增术语提案"]

CHECK_TITLES = {
    "C01": "文档结构与必需文件",
    "C02": "ID 定义与引用完整性",
    "C03": "相对链接可达性",
    "C04": "NFR 全覆盖声明",
    "C05": "关系矩阵合法性与对称性",
    "C06": "需求跟踪矩阵完整性",
    "C07": "术语一致性（禁用同义词）",
    "C08": "写作规范（禁用词）",
    "C09": "占位符与未决项统计",
    "C10": "表格结构与豁免标记卫生",
    "C11": "文档版本一致性",
    "C12": "模块体量合理性",
    "C13": "数据写主唯一性",
    "C14": "Mermaid 图内模块 ID",
    "C15": "文档状态一致性",
    "C16": "终态文档引用完整性",
}


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class Issue:
    level: str          # ERROR / WARN / INFO
    code: str
    file: str
    message: str
    line: Optional[int] = None

    def render(self) -> str:
        loc = self.file + (f":{self.line}" if self.line else "")
        return f"[{self.level:<5}] {self.code}  {loc}\n         {self.message}"


@dataclass
class Doc:
    path: str
    rel: str
    text: str
    lines: List[str] = field(default_factory=list)

    def line_of(self, needle: str) -> Optional[int]:
        for i, ln in enumerate(self.lines, 1):
            if needle in ln:
                return i
        return None


@dataclass
class Table:
    headers: List[str]
    rows: List[Dict[str, str]]
    heading: str
    start: int = 0
    row_lines: List[int] = field(default_factory=list)
    ragged: List[Tuple[int, int]] = field(default_factory=list)


# --------------------------------------------------------------------------
# Markdown 解析
# --------------------------------------------------------------------------

def read_docs(root: str) -> List[Doc]:
    docs = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in sorted(filenames):
            if not fn.lower().endswith(".md"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, "r", encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError) as exc:
                print(f"跳过无法读取的文件 {full}: {exc}", file=sys.stderr)
                continue
            docs.append(Doc(path=full, rel=os.path.relpath(full, root),
                            text=text, lines=text.splitlines()))
    return docs


def split_row(line: str) -> List[str]:
    """按 | 切分表格行，认 Markdown 标准的 \\| 转义。

    不认转义的话，单元格里合法写出的 `\\|` 会被当成列分隔符，把整行的列切错位，
    后续检查读到的就是隔壁列的内容。
    """
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    cells: List[str] = []
    buf: List[str] = []
    i = 0
    while i < len(line):
        if line[i] == "\\" and i + 1 < len(line) and line[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if line[i] == "|":
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(line[i])
        i += 1
    cells.append("".join(buf).strip())
    return cells


def is_separator(line: str) -> bool:
    body = line.strip().strip("|").replace(" ", "")
    return bool(body) and set(body) <= set("-:|")


def parse_tables(doc: Doc) -> List[Table]:
    """提取文档中全部 Markdown 表格，附带所属最近标题。"""
    tables: List[Table] = []
    heading = ""
    i = 0
    lines = doc.lines
    masked = code_block_lines(lines)      # 代码块内的示例表不参与解析，否则污染注册表
    while i < len(lines):
        line = lines[i]
        m = RE_HEADING.match(line)
        if m:
            heading = m.group(2).strip()
            i += 1
            continue
        if ((i + 1) not in masked and line.strip().startswith("|")
                and i + 1 < len(lines) and is_separator(lines[i + 1])):
            headers = split_row(line)
            rows: List[Dict[str, str]] = []
            row_lines: List[int] = []
            ragged: List[Tuple[int, int]] = []
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = split_row(lines[j])
                if len(cells) != len(headers):
                    ragged.append((j + 1, len(cells)))
                if len(cells) < len(headers):
                    cells += [""] * (len(headers) - len(cells))
                rows.append({headers[k]: cells[k] for k in range(len(headers))})
                row_lines.append(j + 1)
                j += 1
            tables.append(Table(headers=headers, rows=rows, heading=heading,
                                start=i + 1, ragged=ragged, row_lines=row_lines))
            i = j
            continue
        i += 1
    return tables


def pick_table(tables: List[Table], heading_kw: List[str],
               header_kw: Optional[List[str]] = None) -> Optional[Table]:
    """按标题关键词优先、表头关键词兜底地选表。"""
    for t in tables:
        if any(kw in t.heading for kw in heading_kw):
            return t
    if header_kw:
        for t in tables:
            joined = " ".join(t.headers)
            if all(kw in joined for kw in header_kw):
                return t
    return None


def get_col(row: Dict[str, str], *keywords: str) -> str:
    for key, val in row.items():
        if any(kw in key for kw in keywords):
            return val.strip()
    return ""


def strip_code_blocks(text: str) -> str:
    # 未闭合的围栏算到文末，与 code_block_lines 的逐行 toggle 一致：否则未闭合
    # 代码块里的内容会被当正文扫描（假阳性），而逐行版会屏蔽后续（假阴性）。
    return re.sub(r"```.*?(?:```|\Z)", "", text, flags=re.DOTALL)


def code_block_lines(lines: List[str]) -> Set[int]:
    """返回处于围栏代码块内的行号集合（1 起）。含围栏行本身。

    strip_code_blocks 会改变字符偏移，逐行扫描时改用本函数屏蔽代码块，
    这样报出的行号与原文一致。
    """
    masked: Set[int] = set()
    inside = False
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            masked.add(i)
            inside = not inside
            continue
        if inside:
            masked.add(i)
    return masked


# 行内豁免标记：<!-- term-ok --> 或 <!-- term-ok: 回退、撤回 -->
RE_TERM_OK = re.compile(r"<!--\s*term-ok\s*(?::\s*([^>]*?))?\s*-->")


def term_ok_words(line: str) -> Optional[Set[str]]:
    """解析行内豁免标记。返回 None 表示无标记；空集合表示豁免整行。

    一行有多个标记时全部合并，不只取第一个——手写时把两个标记分写在
    两个单元格里很自然，只认第一个会让第二个静默失效。
    """
    found = RE_TERM_OK.findall(line)
    if not found:
        return None
    words: Set[str] = set()
    for raw in found:
        raw = (raw or "").strip()
        if not raw:
            return set()                    # 任一标记不带词即豁免整行
        # 按 | 也切一刀：写成 `回退|撤回` 是错的（C10 会报），但仍按用户本意
        # 解析成两个词，免得再叠一条「本行没有『回退|撤回』」的假告警
        words.update(w.strip() for w in re.split(r"[、,，/;；|\s]+", raw) if w.strip())
    return words


def strip_term_ok(line: str) -> str:
    """去掉行内豁免标记本身，避免标记里的词被当成正文命中。"""
    return RE_TERM_OK.sub(" ", line)


RE_INLINE_CODE = re.compile(r"`[^`\n]+`")
# 未决标记及其内容，允许内嵌一层方括号（`[待确认: 如 [MOD-0001 名](路径)]`）
RE_PENDING = re.compile(
    r"\[(?:待确认|推测|新增术语提案)[^\[\]]*(?:\[[^\]]*\][^\[\]]*)*\]")


def _blank(m: "re.Match") -> str:
    return " " * len(m.group(0))


def strip_inline_code(line: str) -> str:
    """抹掉行内代码，长度不变以保住列位置。

    保留字、示例标记、字段名按 Markdown 惯例写成行内代码，那是在「提到」这个词
    而非「使用」它，不该算作正文命中。
    """
    return RE_INLINE_CODE.sub(_blank, line)


def strip_pending(line: str) -> str:
    """抹掉未决标记及其内容，长度不变。

    `[待确认: ...]` 里的文字是待替换的示例，尚未成为文档的主张，对它报术语
    违规是重复施压——C09 已经在追这些标记清零，用户一填就消失。
    """
    return RE_PENDING.sub(_blank, line)


def header_line_numbers(lines: List[str]) -> Set[int]:
    """表格的表头行与分隔行行号（1 起）。

    表头由模板定死，且校验器靠表头关键词定位表格——改表头会让解析失效。
    因此不能既要求表头不动，又对表头里的词报违规。
    """
    marked: Set[int] = set()
    for i in range(len(lines) - 1):
        if lines[i].strip().startswith("|") and is_separator(lines[i + 1]):
            marked.add(i + 1)
            marked.add(i + 2)
    return marked


def first_id(text: str, pattern: re.Pattern) -> str:
    m = pattern.search(text or "")
    return m.group(0) if m else ""


def short_id_fix(token: str) -> Optional[str]:
    """ID 中任一数字段不足四位时返回补零后的正确写法，全部合法则返回 None。

    逐段检查而非只看首段：MOD-0001-REQ-01 的首段合法、尾段不合法，
    只查首段会让这种写法既不报悬空也不生效——比报错危险。
    """
    parts = token.split("-")
    fixed = [p.zfill(4) if p.isdigit() else p for p in parts]
    return "-".join(fixed) if fixed != parts else None


# --------------------------------------------------------------------------
# 文档集合
# --------------------------------------------------------------------------

class Corpus:
    def __init__(self, root: str):
        self.root = root
        self.docs = read_docs(root)
        self.by_rel = {d.rel: d for d in self.docs}
        self.tables: Dict[str, List[Table]] = {d.rel: parse_tables(d) for d in self.docs}

        self.overview = self._find(DOC_KEYWORDS["overview"])
        self.glossary = self._find(DOC_KEYWORDS["glossary"])
        self.traceability = self._find(DOC_KEYWORDS["traceability"])
        self.release = self._find(DOC_KEYWORDS["release"])
        self.snapshot = self._find(DOC_KEYWORDS["snapshot"])
        self.modules = [d for d in self.docs if re.match(r"^MOD-\d{4,}", os.path.basename(d.rel))]
        self.scenarios = [d for d in self.docs if re.match(r"^UC-\d{4,}", os.path.basename(d.rel))]
        self.decisions = [d for d in self.docs if re.match(r"^ADR-\d{4,}", os.path.basename(d.rel))]

        self.module_ids: Set[str] = set()
        self.nfr_ids: Set[str] = set()
        self.uc_ids: Set[str] = {first_id(os.path.basename(d.rel), RE_UC) for d in self.scenarios}
        self.uc_ids.discard("")
        self.adr_ids: Set[str] = {first_id(os.path.basename(d.rel), RE_ADR) for d in self.decisions}
        self.adr_ids.discard("")
        self.term_ids: Set[str] = set()
        self.banned_synonyms: Dict[str, str] = {}   # 同义词 -> 标准名
        self.term_exemptions: Dict[str, Set[str]] = {}   # 同义词 -> 豁免搭配集合
        # 豁免上下文列填了搭配却不含该行任何禁用同义词：(标准名, 搭配)。
        # 这是真·空转--搭配覆盖的字符区间里不可能出现这一行的词，C10 报这一类。
        # 含某个同义词的搭配已按包含关系挂到该同义词上（见 _load_registry），
        # 不再逐词要求「搭配含该词」：一行多词共享本列，逐词要求会把含同行
        # 另一个词的合法搭配误报成空转。
        self.orphan_exemptions: List[Tuple[str, str]] = []
        # 标准名的某形式同时出现在禁用同义词列：(标准名原文, 该形式)。等于禁用
        # 自己的标准名，C10 报这一类配置矛盾。中英双语并列时容易把另一语言形式误填。
        self.standard_banned_conflicts: List[Tuple[str, str]] = []
        self.req_ids: Set[str] = set()
        self.index_rows: List[Dict[str, str]] = []
        self.relations: List[Dict[str, str]] = []
        self.ui_names: Dict[str, Set[Tuple[str, str]]] = {}
        self.doc_versions: Dict[str, Tuple[str, Optional[int]]] = {}
        self.doc_status: Dict[str, Tuple[str, Optional[int]]] = {}
        self.changelog_versions: Dict[str, List[Tuple[str, Optional[int]]]] = {}

        self._load_registry()

    def _find(self, keywords: List[str]) -> Optional[Doc]:
        for d in self.docs:
            base = os.path.basename(d.rel).lower()
            if any(kw.lower() in base for kw in keywords):
                return d
        return None

    def module_doc(self, mod_id: str) -> Optional[Doc]:
        for d in self.modules:
            if first_id(os.path.basename(d.rel), RE_MOD) == mod_id:
                return d
        return None

    def _load_registry(self) -> None:
        if self.overview:
            ov = self.tables[self.overview.rel]
            idx = pick_table(ov, *table_spec("module_index"))
            if idx:
                self.index_rows = idx.rows
                for row in idx.rows:
                    mid = first_id(get_col(row, "模块 ID", "模块ID", "ID"), RE_MOD)
                    if mid:
                        self.module_ids.add(mid)
            nfr = pick_table(ov, *table_spec("nfr"))
            if nfr:
                for row in nfr.rows:
                    nid = first_id(get_col(row, "NFR", "ID"), RE_NFR)
                    if nid:
                        self.nfr_ids.add(nid)
            rel = pick_table(ov, *table_spec("relations"))
            if rel:
                self.relations = rel.rows

        for d in self.modules:
            mid = first_id(os.path.basename(d.rel), RE_MOD)
            if mid:
                self.module_ids.add(mid)

        if self.glossary:
            for t in self.tables[self.glossary.rel]:
                joined = " ".join(t.headers)
                if not any(kw in joined for kw in GLOSSARY_COLUMNS["identity"]):
                    continue
                for row in t.rows:
                    tid = first_id(get_col(row, "术语 ID", "术语ID", "ID"), RE_TERM)
                    if tid:
                        self.term_ids.add(tid)
                    standard_raw = get_col(row, "标准名", "术语名")
                    banned = get_col(row, GLOSSARY_COLUMNS["banned"], "禁用", "同义词")
                    # 跳过占位行：模板预填 [待确认: ...]，不跳会被拆词当成真实禁用词
                    if not standard_raw or not banned:
                        continue
                    if standard_raw.startswith("[") or "[待确认" in banned or "[推测" in banned:
                        continue
                    # 标准名支持中英双语并列（如「Order / 订单」），各形式等价、均不参与
                    # 扫描。拆分口径与禁用同义词、豁免搭配一致，复用 PHRASE_SEPARATORS，
                    # 旧的单值标准名是其特例。报错提示直接展示标准名原文，多形式一目了然。
                    standard_forms = [
                        s for s in (x.strip() for x in re.split(PHRASE_SEPARATORS, standard_raw))
                        if s and not s.startswith("[")
                    ]
                    # 先解析本行的禁用同义词（一行可多个，顿号分隔）
                    words: List[str] = []
                    for word in re.split(PHRASE_SEPARATORS, banned):
                        word = word.strip()
                        if not word or word.startswith("[") or word.endswith("]"):
                            continue
                        if is_empty_marker(word):
                            continue
                        # 禁用同义词列不得出现标准名的任一形式，否则等于禁用
                        # 自己的标准名（中英双语时尤其容易把另一语言形式误填进来）。
                        # C10 报这一类配置矛盾，该词也不登记进 banned_synonyms，
                        # 否则它会反过来扫掉标准名自身的合法用法。
                        if word in standard_forms:
                            self.standard_banned_conflicts.append((standard_raw, word))
                            continue
                        words.append(word)
                        self.banned_synonyms[word] = standard_raw
                    # 该行禁用同义词在哪些搭配里属正常表述，不报警
                    exempt_raw = get_col(row, GLOSSARY_COLUMNS["exemption"], "豁免", "例外")
                    if exempt_raw and not exempt_raw.startswith("["):
                        for phrase in re.split(PHRASE_SEPARATORS, exempt_raw):
                            phrase = phrase.strip()
                            # 空位标记不是搭配。模板预填 `--`，收进来的话全库
                            # 每一行破折号都会成为万能豁免，C07 整项静默失效。
                            if not phrase or is_empty_marker(phrase):
                                continue
                            # 豁免只掩护它实际覆盖的命中：搭配含该词，该词落在搭配
                            # 的字符区间内才被掩护。一行多个同义词共享本列，逐词全挂
                            # 会让不含该词但含同行另一个词的合法搭配也算成空转，
                            # C10 误报一片。所以只挂到搭配实际包含的那些同义词上，
                            # 不含本行任何同义词的搭配记为 orphan 单独报。
                            matched = [w for w in words if w in phrase]
                            if matched:
                                for w in matched:
                                    self.term_exemptions.setdefault(w, set()).add(phrase)
                            else:
                                self.orphan_exemptions.append((standard_raw, phrase))

        if self.traceability:
            for t in self.tables[self.traceability.rel]:
                for row in t.rows:
                    rid = first_id(get_col(row, "需求 ID", "需求ID", "ID"), RE_REQ)
                    if rid:
                        self.req_ids.add(rid)

        # 文档版本与变更记录版本序列，C11 用；文档状态，C15 与 --snapshot 用
        for d in self.docs:
            for t in self.tables[d.rel]:
                for idx, row in enumerate(t.rows):
                    # 元信息表是「项 | 值」两列的竖表，版本与状态都在值列
                    key = next(iter(row.values())) if row else ""
                    vals = list(row.values())
                    if VERSION_ROW_LABEL in key and d.rel not in self.doc_versions:
                        raw = vals[1].strip() if len(vals) > 1 else ""
                        self.doc_versions[d.rel] = (raw, d.line_of(VERSION_ROW_LABEL))
                    # 状态行用全等而非子串匹配：「状态」两字太短，子串会命中
                    # 模块文档流程表的「前置/后置状态」之类的行，把正文内容
                    # 当成状态取值。行号取 row_lines 而不是 line_of 反查全文——
                    # 「状态」在一份文档里会出现多次，反查必然指错行。
                    if (key.strip() == STATUS_ROW_LABEL
                            and d.rel not in self.doc_status):
                        raw = vals[1].strip() if len(vals) > 1 else ""
                        lineno = (t.row_lines[idx]
                                  if idx < len(t.row_lines) else t.start or None)
                        self.doc_status[d.rel] = (raw, lineno)
                if not any(CHANGELOG_VERSION_COL == h.strip() for h in t.headers):
                    continue
                if not any(kw in t.heading for kw in CHANGELOG_HEADING_KW):
                    continue
                seq: List[Tuple[str, Optional[int]]] = []
                for idx, row in enumerate(t.rows):
                    raw = get_col(row, CHANGELOG_VERSION_COL).strip()
                    if raw and not is_empty_marker(raw):
                        seq.append((raw, t.row_lines[idx] if idx < len(t.row_lines) else None))
                self.changelog_versions.setdefault(d.rel, []).extend(seq)

        # 界面引用表：UI ID 与界面名的对应关系，分散登记在各模块文档中
        for d in self.docs:
            for t in self.tables[d.rel]:
                if not any(h.strip() == "UI" or "UI ID" in h for h in t.headers):
                    continue
                for row in t.rows:
                    uid = first_id(get_col(row, "UI ID", "UI"), RE_UI)
                    name = get_col(row, "界面名称", "界面名", "名称")
                    if uid and name and not name.startswith("["):
                        self.ui_names.setdefault(uid, set()).add((d.rel, name))


# --------------------------------------------------------------------------
# 检查项
# --------------------------------------------------------------------------

def check_structure(c: Corpus) -> List[Issue]:
    out: List[Issue] = []
    required = [("综述", c.overview), ("术语表", c.glossary),
                ("需求跟踪矩阵", c.traceability), ("版本计划", c.release),
                ("版本快照", c.snapshot)]
    for name, doc in required:
        if doc is None:
            out.append(Issue("ERROR", "C01", "-", f"缺少必需产物《{name}》，文件名需包含「{name}」"))
    if not c.modules:
        out.append(Issue("ERROR", "C01", "-", "未找到任何功能模块文档（文件名需以 MOD-XX 开头）"))
    if len(c.scenarios) < 3:
        out.append(Issue("WARN", "C01", "-",
                         f"端到端场景切片仅 {len(c.scenarios)} 份，要求覆盖 3~5 条核心旅程"))
    elif len(c.scenarios) > 5:
        # 上限同样是判据：场景铺到七八条，通常是把模块内的备选流当成了独立旅程。
        # 场景切片的作用是横穿多模块反验拆分，不是穷举所有走法。
        out.append(Issue("WARN", "C01", "-",
                         f"端到端场景切片有 {len(c.scenarios)} 份，超出 3~5 条的建议范围。"
                         f"核对是否把模块内的备选流当成了独立旅程——那类分支属于模块文档"
                         f"的备选流表，不单独立场景"))
    if c.overview and not c.index_rows:
        out.append(Issue("ERROR", "C01", c.overview.rel, "综述中未解析到模块索引表"))
    if c.overview and not c.nfr_ids:
        out.append(Issue("ERROR", "C01", c.overview.rel, "综述中未解析到全局 NFR 表"))

    indexed = {first_id(get_col(r, "模块 ID", "模块ID", "ID"), RE_MOD) for r in c.index_rows}
    indexed.discard("")
    filed = {first_id(os.path.basename(d.rel), RE_MOD) for d in c.modules}
    for mid in sorted(indexed - filed):
        out.append(Issue("ERROR", "C01", c.overview.rel if c.overview else "-",
                         f"{mid} 出现在模块索引中，但没有对应的模块文档文件"))
    for mid in sorted(filed - indexed):
        doc = c.module_doc(mid)
        out.append(Issue("ERROR", "C01", doc.rel if doc else "-",
                         f"{mid} 存在模块文档，但未登记进综述的模块索引"))

    seen: Set[str] = set()
    for row in c.index_rows:
        mid = first_id(get_col(row, "模块 ID", "模块ID", "ID"), RE_MOD)
        if mid and mid in seen:
            out.append(Issue("ERROR", "C01", c.overview.rel if c.overview else "-",
                             f"模块索引中 {mid} 重复出现，ID 必须全局唯一"))
        seen.add(mid)
    return out


def check_id_references(c: Corpus) -> List[Issue]:
    out: List[Issue] = []
    known = {"MOD": c.module_ids, "NFR": c.nfr_ids, "UC": c.uc_ids,
             "TERM": c.term_ids, "ADR": c.adr_ids}
    patterns = [("MOD", RE_MOD), ("NFR", RE_NFR), ("UC", RE_UC),
                ("TERM", RE_TERM), ("ADR", RE_ADR)]
    for doc in c.docs:
        body = strip_code_blocks(doc.text)
        reported: Set[str] = set()
        for kind, pat in patterns:
            # 其余类型的注册表为空时跳过，避免骨架阶段满屏误报；ADR 不跳过，
            # 它没有「还没写到那一步」的合法情形，引用了就必须有对应文档。
            if not known[kind] and kind != "ADR":
                continue
            for found in pat.findall(body):
                if found in known[kind] or found in reported:
                    continue
                # 子模块 MOD-0003-A 允许父模块已登记
                if kind == "MOD" and found.rsplit("-", 1)[0] in c.module_ids:
                    continue
                reported.add(found)
                source = {"MOD": "综述的模块索引", "NFR": "综述的全局 NFR 表",
                          "UC": "scenarios/ 下的场景文件", "TERM": "术语表",
                          "ADR": "decisions/ 下的决策记录"}[kind]
                out.append(Issue("ERROR", "C02", doc.rel,
                                 f"引用了未定义的 {found}（未登记在 {source} 中）",
                                 doc.line_of(found)))

        # 位数不足的 ID 不匹配上面的正则，会被静默忽略，单独检出。
        for m in RE_ID_TOKEN.finditer(body):
            token = m.group(0)
            if token in reported:
                continue
            fixed = short_id_fix(token)
            if not fixed:
                continue
            reported.add(token)
            out.append(Issue("ERROR", "C02", doc.rel,
                             f"{token} 的序号不足四位，应写成 "
                             f"{fixed}（序号四位起，不足补 0）",
                             doc.line_of(token)))

    for doc in c.modules:
        mid = first_id(os.path.basename(doc.rel), RE_MOD)
        for uc in RE_UC.findall(strip_code_blocks(doc.text)):
            sc = next((s for s in c.scenarios
                       if first_id(os.path.basename(s.rel), RE_UC) == uc), None)
            if sc and mid not in sc.text:
                out.append(Issue("WARN", "C02", doc.rel,
                                 f"{mid} 声称参与 {uc}，但 {uc} 的场景文档中没有提到 {mid}"))

    # UI ID 分散登记在各模块，没有中央注册表，无从查悬空；
    # 但同号指向不同界面名可查，那说明有一处写错。
    for uid, entries in sorted(c.ui_names.items()):
        names = {name for _, name in entries}
        if len(names) > 1:
            where = "；".join(f"{rel} 记为「{name}」" for rel, name in sorted(entries))
            out.append(Issue("WARN", "C02", sorted(entries)[0][0],
                             f"{uid} 在不同文档中对应了不同界面名（{where}）。"
                             f"UI ID 全局唯一，同号不同名说明有一处写错"))
    return out


def check_links(c: Corpus) -> List[Issue]:
    out: List[Issue] = []
    for doc in c.docs:
        base = os.path.dirname(doc.path)
        for label, target in RE_LINK.findall(strip_code_blocks(doc.text)):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            clean = target.split("#")[0]
            if not clean:
                continue
            if not os.path.exists(os.path.normpath(os.path.join(base, clean))):
                out.append(Issue("ERROR", "C03", doc.rel,
                                 f"链接目标不存在: [{label}]({target})", doc.line_of(target)))
    for doc in c.modules + c.scenarios:
        linked = " ".join(t for _, t in RE_LINK.findall(doc.text))
        self_id = first_id(os.path.basename(doc.rel), RE_MOD) or \
            first_id(os.path.basename(doc.rel), RE_UC)
        for mid in set(RE_MOD.findall(strip_code_blocks(doc.text))):
            if mid != self_id and mid not in linked and not RE_REQ.search(mid):
                out.append(Issue("WARN", "C03", doc.rel,
                                 f"提到 {mid} 但全文没有指向它的 Markdown 链接，跨文档引用需可点击"))
    return out


def check_nfr_coverage(c: Corpus) -> List[Issue]:
    out: List[Issue] = []
    if not c.nfr_ids:
        return out
    for doc in c.modules:
        tables = c.tables[doc.rel]
        t = pick_table(tables, *table_spec("nfr_declaration"))
        if t is None:
            out.append(Issue("ERROR", "C04", doc.rel,
                             "缺少「全局 NFR 适用性声明」表，该章节为强制项"))
            continue
        declared: Dict[str, Tuple[str, str]] = {}
        for row in t.rows:
            nid = first_id(get_col(row, "NFR", "ID"), RE_NFR)
            if not nid:
                continue
            verdict = get_col(row, "结论", "适用性", "判定")
            reason = get_col(row, "说明", "理由", "备注")
            declared[nid] = (verdict, reason)
        for nid in sorted(c.nfr_ids - set(declared)):
            out.append(Issue("ERROR", "C04", doc.rel,
                             f"未对 {nid} 作出适用性声明，全局 NFR 必须逐条应答"))
        for nid in sorted(set(declared) - c.nfr_ids):
            out.append(Issue("ERROR", "C04", doc.rel,
                             f"声明了 {nid}，但综述的全局 NFR 表中没有这一条"))
        for nid, (verdict, reason) in sorted(declared.items()):
            vkey = verdict.replace(" ", "")
            if vkey.startswith(("[待确认", "[推测", "[新增术语提案")):
                out.append(Issue("WARN", "C04", doc.rel,
                                 f"{nid} 的适用性结论尚未填写（当前为 {verdict} 占位），定稿前必须给出结论"))
                continue
            if not any(vkey.startswith(v) for v in VALID_NFR_VERDICT):
                out.append(Issue("ERROR", "C04", doc.rel,
                                 f"{nid} 的结论「{verdict or '空'}」非法，只能取"
                                 f"「完全适用 / 部分适用 / 不适用」"))
                continue
            if "完全适用" in vkey:
                continue
            if is_tautology(reason):
                out.append(Issue("ERROR", "C04", doc.rel,
                                 f"{nid} 判为「{verdict}」但理由为空或属于同义反复"
                                 f"（「{reason}」），必须给出实质理由：本模块的哪个特性"
                                 f"使该指标不成立"))
    return out


def _local_relation_table(c: Corpus, doc: Doc) -> Optional[Table]:
    return pick_table(c.tables[doc.rel], *table_spec("local_relations"))


def _local_relation_map(c: Corpus, doc: Doc) -> Dict[str, Set[str]]:
    """模块文档关系本地视图: 对方模块 ID -> 关系类型集合。"""
    result: Dict[str, Set[str]] = {}
    t = _local_relation_table(c, doc)
    if t is None:
        return result
    for row in t.rows:
        other = first_id(get_col(row, "对方模块", "关联模块", "模块", "目标", "源"), RE_MOD)
        rtype = get_col(row, "关系类型", "类型").strip()
        if other:
            result.setdefault(other, set()).add(rtype)
    return result


def check_relations(c: Corpus) -> List[Issue]:
    out: List[Issue] = []
    ov = c.overview.rel if c.overview else "-"
    if c.overview and not c.relations:
        out.append(Issue("ERROR", "C05", ov, "综述中未解析到关系矩阵表"))
        return out

    local_maps = {first_id(os.path.basename(d.rel), RE_MOD): _local_relation_map(c, d)
                  for d in c.modules}
    # 「表存在」与「表内有内容」分开判断：合并的话，清空整张表反而能绕过检查
    has_local_table = {first_id(os.path.basename(d.rel), RE_MOD):
                       _local_relation_table(c, d) is not None for d in c.modules}
    doc_of = {first_id(os.path.basename(d.rel), RE_MOD): d.rel for d in c.modules}

    for doc in c.modules:
        mid = first_id(os.path.basename(doc.rel), RE_MOD)
        if _local_relation_table(c, doc) is None:
            out.append(Issue("ERROR", "C05", doc.rel,
                             "缺少「关系本地视图」表，需从综述关系矩阵摘录相关行"))
        elif not local_maps.get(mid):
            out.append(Issue("WARN", "C05", doc.rel,
                             "关系本地视图表存在但未填写模块 ID。若本模块确实无跨模块关系，"
                             "需确认是否为遗漏——孤立模块通常意味着拆分有误"))

    for row in c.relations:
        src = first_id(get_col(row, *RELATION_COLUMNS["source"]), RE_MOD)
        dst = first_id(get_col(row, *RELATION_COLUMNS["target"]), RE_MOD)
        rtype = get_col(row, *RELATION_COLUMNS["type"]).strip()
        conf = get_col(row, *RELATION_COLUMNS["confidence"]).strip()
        strength = get_col(row, *RELATION_COLUMNS["strength"]).strip()
        fallback = get_col(row, *RELATION_COLUMNS["fallback"]).strip()
        tag = f"{src or '?'} → {dst or '?'}"

        if not src or not dst:
            out.append(Issue("ERROR", "C05", ov, f"关系矩阵存在无法解析的行: {row}"))
            continue
        for mid in (src, dst):
            if mid not in c.module_ids:
                out.append(Issue("ERROR", "C05", ov, f"关系 {tag} 引用了未登记的模块 {mid}"))
        if rtype not in VALID_RELATIONS:
            out.append(Issue("ERROR", "C05", ov,
                             f"关系 {tag} 的类型「{rtype}」不在允许词表内"))
        if conf not in VALID_CONFIDENCE:
            out.append(Issue("ERROR", "C05", ov,
                             f"关系 {tag} 的置信度「{conf}」非法，只能取 已证实 / 推测 / 待定"))
        if strength.replace(" ", "") not in VALID_STRENGTH:
            out.append(Issue("WARN", "C05", ov,
                             f"关系 {tag} 的依赖强度「{strength}」建议写成 强依赖 / 弱依赖"))
        if "弱" in strength and (not fallback or is_empty_marker(fallback)
                                 or fallback in EMPTY_REASON_PATTERNS):
            out.append(Issue("ERROR", "C05", ov,
                             f"关系 {tag} 标为弱依赖但未填降级策略，弱依赖必须写明降级后的行为"))

        # 对称性：只要对方模块文档里有关系本地视图表，就必须登记这条关系
        if has_local_table.get(src) and dst not in local_maps.get(src, {}):
            out.append(Issue("ERROR", "C05", doc_of.get(src, f"modules/{src}"),
                             f"关系矩阵登记了 {tag}（{rtype}），但 {src} 的关系本地视图中缺少 {dst}"))
        if has_local_table.get(dst) and src not in local_maps.get(dst, {}):
            mirror = RELATION_MIRROR.get(rtype, rtype)
            out.append(Issue("ERROR", "C05", doc_of.get(dst, f"modules/{dst}"),
                             f"关系矩阵登记了 {tag}（{rtype}），但 {dst} 的关系本地视图中缺少 {src}"
                             f"（应登记为「{mirror}」）"))
    return out


def check_traceability(c: Corpus) -> List[Issue]:
    out: List[Issue] = []
    if not c.traceability:
        return out
    tdoc = c.traceability
    table = None
    for t in c.tables[tdoc.rel]:
        if any("需求" in h for h in t.headers):
            table = t
            break
    if table is None:
        out.append(Issue("ERROR", "C06", tdoc.rel, "未解析到需求跟踪矩阵主表"))
        return out

    joined = " ".join(table.headers)
    for col in TRACE_COLUMNS:
        if col not in joined:
            out.append(Issue("ERROR", "C06", tdoc.rel, f"跟踪矩阵缺少「{col}」列"))

    seen: Set[str] = set()
    for row in table.rows:
        rid = first_id(get_col(row, "需求 ID", "需求ID"), RE_REQ)
        raw_rid = get_col(row, "需求 ID", "需求ID")
        if not rid:
            out.append(Issue("ERROR", "C06", tdoc.rel,
                             f"需求 ID「{raw_rid}」格式非法，应为 MOD-XX-REQ-YY"))
            continue
        if rid in seen:
            out.append(Issue("ERROR", "C06", tdoc.rel, f"需求 ID {rid} 重复"))
        seen.add(rid)
        mid = first_id(get_col(row, "模块"), RE_MOD) or rid.rsplit("-REQ-", 1)[0]
        if mid not in c.module_ids:
            out.append(Issue("ERROR", "C06", tdoc.rel, f"{rid} 归属的模块 {mid} 未登记"))
        if not rid.startswith(mid):
            out.append(Issue("ERROR", "C06", tdoc.rel,
                             f"{rid} 的前缀与所属模块 {mid} 不一致"))
        for uc in RE_UC.findall(get_col(row, "场景")):
            if uc not in c.uc_ids:
                out.append(Issue("ERROR", "C06", tdoc.rel, f"{rid} 关联的场景 {uc} 不存在"))
        for col, label in [("优先级", "优先级"), ("来源", "需求来源"), ("验收", "验收标准 ID")]:
            val = get_col(row, col)
            if not val or is_empty_marker(val):
                out.append(Issue("ERROR", "C06", tdoc.rel, f"{rid} 的「{label}」为空"))

    for doc in c.modules:
        for rid in sorted(set(RE_REQ.findall(strip_code_blocks(doc.text)))):
            if rid not in seen:
                out.append(Issue("ERROR", "C06", doc.rel,
                                 f"{rid} 出现在模块文档中，但未登记进需求跟踪矩阵",
                                 doc.line_of(rid)))
    for rid in sorted(seen):
        mid = rid.rsplit("-REQ-", 1)[0]
        doc = c.module_doc(mid)
        if doc and rid not in doc.text:
            out.append(Issue("WARN", "C06", tdoc.rel,
                             f"{rid} 在矩阵中登记，但模块文档 {mid} 里找不到这条需求"))
    return out


def _exempt_spans(line: str, exempts: Set[str]) -> List[Tuple[int, int]]:
    """豁免搭配在本行覆盖的字符区间。"""
    spans: List[Tuple[int, int]] = []
    for phrase in exempts:
        start = line.find(phrase)
        while start >= 0:
            spans.append((start, start + len(phrase)))
            start = line.find(phrase, start + 1)
    return spans


def check_terminology(c: Corpus) -> List[Issue]:
    """逐行扫描禁用同义词。

    中文没有词边界，`回退` 这类词既是「撤销」的同义词，也可能是「失败回退加载」
    里的正常表述。所以命中判 WARN 而非 ERROR，并提供两级豁免：术语表的
    「豁免上下文」列（整库生效）与行内 `<!-- term-ok -->` 标记（单行生效）。

    豁免按**出现位置**判定，不是整行判定：一行里「回退加载」被豁免，同一行的
    「执行回退」仍要报出来。整行判定会让一个合法搭配掩护掉同行的真实违规。
    """
    out: List[Issue] = []
    if not c.banned_synonyms:
        return out
    for doc in c.docs:
        if c.glossary and doc.rel == c.glossary.rel:
            continue
        masked = code_block_lines(doc.lines) | header_line_numbers(doc.lines)
        for lineno, raw_line in enumerate(doc.lines, 1):
            if lineno in masked:
                continue
            stripped = strip_inline_code(raw_line)
            inline = term_ok_words(stripped)
            if inline is not None and not inline:
                continue                      # 整行豁免
            # 标记、行内代码、未决占位都不参与扫描：`回退` 是在提这个词不是在用它，
            # [待确认: ...] 里的字是待替换的示例、尚未成为文档的主张
            line = strip_pending(strip_term_ok(stripped))
            for bad, standard in sorted(c.banned_synonyms.items()):
                if bad not in line:
                    continue
                if inline and bad in inline:
                    continue                  # 行内点名豁免该词
                # 术语表登记的豁免搭配：只掩护它自己覆盖的那几处出现
                spans = _exempt_spans(line, c.term_exemptions.get(bad, set()))
                hits = [m.start() for m in re.finditer(re.escape(bad), line)
                        if not any(s <= m.start() and m.end() <= e for s, e in spans)]
                if not hits:
                    continue
                count = f"（本行 {len(hits)} 处）" if len(hits) > 1 else ""
                out.append(Issue("WARN", "C07", doc.rel,
                                 f"出现禁用同义词「{bad}」{count}，术语表标准名为「{standard}」。"
                                 f"确属无关表述时，在术语表补「豁免上下文」列或本行加 "
                                 f"<!-- term-ok: {bad} -->",
                                 lineno))
    return out


def check_writing_style(c: Corpus) -> List[Issue]:
    out: List[Issue] = []
    for doc in c.docs:
        body = strip_code_blocks(doc.text)
        for category, words in BANNED_WORDS.items():
            for w in words:
                if w in body:
                    out.append(Issue("WARN", "C08", doc.rel,
                                     f"{category}「{w}」，参见 references/writing-style.md",
                                     doc.line_of(w)))
    return out


def check_placeholders(c: Corpus) -> List[Issue]:
    out: List[Issue] = []
    tracked = 0
    for doc in c.docs:
        body = strip_code_blocks(doc.text)
        for marker in PLACEHOLDER_MARKERS:
            if marker in body:
                out.append(Issue("ERROR", "C09", doc.rel,
                                 f"残留占位符「{marker}」，未定内容请改用 [待确认: 描述] 标记",
                                 doc.line_of(marker)))
        for marker in TRACKED_MARKERS:
            tracked += body.count(marker)
    if tracked:
        out.append(Issue("INFO", "C09", "-", f"全库共有 {tracked} 处已标记的未决项，定稿前需清零"))
    return out


def check_exemption_hygiene(c: Corpus) -> List[Issue]:
    """豁免机制自身的卫生：豁免标记不得破坏它所在的结构，也不得空转。

    C07 报错时建议「本行加 <!-- term-ok: 词 -->」，而违规常出现在表格里。
    注释含 `|` 会被当成列分隔符，把整行的列切错位——后果是别的检查项报出
    一堆与真实原因无关的错（关系类型变成「依赖 <!-- term-ok: 回退」），
    人照着那些错去查，查不到根因。这一项直接把根因指出来。
    """
    out: List[Issue] = []
    for doc in c.docs:
        masked = code_block_lines(doc.lines)
        for lineno, raw in enumerate(doc.lines, 1):
            if lineno in masked:
                continue
            # 写成行内代码的是示例（模板里教用法的那句），不是生效的标记
            line = strip_inline_code(raw)
            in_table = line.strip().startswith("|")
            for m in RE_TERM_OK.finditer(line):
                body = m.group(0)
                if in_table and "|" in body:
                    out.append(Issue("ERROR", "C10", doc.rel,
                                     f"表格行内的豁免标记 `{body}` 含「|」，会被当成列分隔符"
                                     f"把本行的列切错位，导致其他检查项报出与真实原因无关的错。"
                                     f"多个词改用顿号分隔：<!-- term-ok: 甲、乙 -->",
                                     lineno))
                elif in_table and is_separator(line.split("<!--")[0]):
                    out.append(Issue("ERROR", "C10", doc.rel,
                                     f"豁免标记 `{body}` 放在了表格分隔行上，整张表会解析不出来。"
                                     f"移到需要豁免的数据行",
                                     lineno))
            # 空转的标记：本行根本没有它点名的词
            words = term_ok_words(line)
            if words:
                text = strip_inline_code(strip_term_ok(line))
                for w in sorted(words):
                    if w not in text:
                        out.append(Issue("WARN", "C10", doc.rel,
                                         f"豁免标记点名了「{w}」，但本行没有这个词。"
                                         f"改动正文后忘了撤标记时会这样——留着会掩护以后写进来的真实违规",
                                         lineno))
                    elif w not in c.banned_synonyms:
                        out.append(Issue("WARN", "C10", doc.rel,
                                         f"豁免标记点名了「{w}」，但它不在术语表的禁用同义词列中，"
                                         f"这条豁免不起作用。核对是否拼错，或该词的登记已被删除",
                                         lineno))

    # orphan 搭配：不含本行任何禁用同义词，对该行所有词都无效（见 _load_registry）

    for standard, phrase in sorted(set(c.orphan_exemptions)):
        out.append(Issue("WARN", "C10", c.glossary.rel if c.glossary else "-",
                         f"标准名「{standard}」的豁免上下文填了「{phrase}」，但这个搭配"
                         f"不含该行任一禁用同义词，对这一行的词都无效。"
                         f"豁免的是含禁用词的搭配，核对是否拼错"))


    # 标准名的某形式同时出现在禁用同义词列：等于禁用自己的标准名。中英双语并列
    # （Order / 订单）时最常见的误填是把中文形式顺手写进禁用同义词列。该词未登记
    # 进 banned_synonyms（否则会扫掉标准名自身），这里单独报出来让用户去掉。
    for standard, word in sorted(set(c.standard_banned_conflicts)):
        out.append(Issue("ERROR", "C10", c.glossary.rel if c.glossary else "-",
                         f"标准名「{standard}」的「{word}」同时出现在禁用同义词列，"
                         f"等于禁用自己的标准名。标准名的各语言形式都合法，去掉这条禁用"))
    # 表格列数与表头不符。解析时缺列补空、多列丢弃，两个方向都不报错——
    # 单元格里混进一个 | 就会静默错位，后续检查读到的是隔壁列的内容。
    for doc in c.docs:
        masked = code_block_lines(doc.lines)
        for t in c.tables[doc.rel]:
            if t.start in masked:
                continue
            for lineno, got in t.ragged:
                want = len(t.headers)
                cause = ("单元格里的 `|` 被当成了列分隔符，用 `\\|` 转义"
                         if got > want else "少写了列，或末尾漏了 `|`")
                level = "ERROR" if got > want else "WARN"
                out.append(Issue(level, "C10", doc.rel,
                                 f"表格行有 {got} 列，表头是 {want} 列。{cause}。"
                                 f"列错位会让后续检查读到隔壁列的内容，报出与真实原因无关的错",
                                 lineno))
    return out


def check_doc_versions(c: Corpus) -> List[Issue]:
    """文档版本一致性。

    同一批生成的文档版本必须相同，改过正文的必须在变更记录里留痕。缺了这项，
    「V1 / V1.0 / V0.1 / 不写」四种写法会同时存在于同一批产物里——这不是笔误，
    是没有任何一层定义过「哪些产物需要版本、格式是什么」。
    """
    out: List[Issue] = []
    if not c.docs:
        return out

    for doc in c.docs:
        rel = doc.rel
        declared = c.doc_versions.get(rel)
        if declared is None:
            # 存量文档没有版本行时判 WARN，给迁移留窗口，定稿 --strict 卡死
            out.append(Issue("WARN", "C11", rel,
                             f"元信息表缺「{VERSION_ROW_LABEL}」行。"
                             f"补一行 | {VERSION_ROW_LABEL} | {INITIAL_VERSION} |"))
            continue
        raw, lineno = declared
        if raw.startswith("["):
            out.append(Issue("WARN", "C11", rel,
                             f"{VERSION_ROW_LABEL}尚未填写（当前为占位），"
                             f"定稿前须给出三段号", lineno))
            continue
        if parse_doc_version(raw) is None:
            out.append(Issue("ERROR", "C11", rel,
                             f"{VERSION_ROW_LABEL}「{raw}」不是三段号。"
                             f"须写成 V<主>.<次>.<修订>，如 {INITIAL_VERSION}——"
                             f"V1、V1.0、v0.1 都不合格式", lineno))
            continue

        # 变更记录的最新版本须与声明的文档版本一致
        seq = c.changelog_versions.get(rel, [])
        parsed = [(parse_doc_version(v), v, ln) for v, ln in seq]
        legal = [(p, v, ln) for p, v, ln in parsed if p is not None]
        for p, v, ln in parsed:
            if p is None and not v.startswith("["):
                out.append(Issue("ERROR", "C11", rel,
                                 f"变更记录中的版本「{v}」不是三段号", ln))
        if not legal:
            out.append(Issue("WARN", "C11", rel,
                             f"变更记录里没有版本条目。改动正文时同步加一行，"
                             f"否则无从追溯 {raw} 是怎么来的"))
        else:
            latest, latest_raw, latest_ln = legal[-1]
            if latest != parse_doc_version(raw):
                out.append(Issue("ERROR", "C11", rel,
                                 f"{VERSION_ROW_LABEL}是 {raw}，但变更记录最新一条是 "
                                 f"{latest_raw}。两者必须一致——对不上说明改了正文没记录，"
                                 f"或记了没更新头部", lineno))
            # 序列单调递增且一次只进一位
            for i in range(1, len(legal)):
                prev, _, _ = legal[i - 1]
                cur, cur_raw, cur_ln = legal[i]
                problem = version_step(prev, cur)
                if problem:
                    prev_raw = legal[i - 1][1]
                    out.append(Issue("ERROR", "C11", rel,
                                     f"变更记录从 {prev_raw} 到 {cur_raw}：{problem}",
                                     cur_ln))

    # 同一批文档版本应当一致：骨架展开时全部 V1.0.0，之后按各自节奏演进。
    # 判 INFO 不判 WARN——文档本来就会分头推进，这里只是提示分布。
    versions: Dict[str, List[str]] = {}
    for rel, (raw, _) in sorted(c.doc_versions.items()):
        if parse_doc_version(raw):
            versions.setdefault(raw.upper(), []).append(rel)
    if len(versions) > 1:
        spread = "；".join(f"{v}: {len(f)} 份" for v, f in sorted(versions.items()))
        out.append(Issue("INFO", "C11", "-",
                         f"全库文档版本分布：{spread}。"
                         f"跨版本引用时注意被引用方是否已更新"))

    # 修订标记与废止标注引用的版本必须存在于该文档的变更记录中
    for doc in c.docs:
        masked = code_block_lines(doc.lines)
        known = {v.upper() for v, _ in c.changelog_versions.get(doc.rel, [])}
        for lineno, line in enumerate(doc.lines, 1):
            if lineno in masked:
                continue
            clean = strip_inline_code(line)
            marks = [(m.group(1), "修订标记") for m in RE_REV_MARK.finditer(clean)]
            marks += [(m.group(1), "废止标注")
                      for m in RE_DEPRECATED.finditer(clean)]
            for ver, label in marks:
                if known and ver.upper() not in known:
                    out.append(Issue("WARN", "C11", doc.rel,
                                     f"{label}引用了 {ver}，但本文档变更记录里没有这一版。"
                                     f"补上对应的变更记录行，否则读者查不到当时改了什么",
                                     lineno))
    return out


# --------------------------------------------------------------------------
# C12 / C13 / C14：结构合理性，均判 WARN（需人定夺的信号），--strict 卡死

def _is_filled(cell: str) -> bool:
    """单元格有实质内容：非空、非空位标记（--/无/N/A）、非未决占位（[待确认…）。

    供 C12/C13 区分「模块还没写」与「模块写了但偏少/有冲突」。空白骨架的规则
    描述、实体、属性都是 [待确认]，落在这里返回 False，三项都不报。
    """
    s = cell.strip()
    return bool(s) and not is_empty_marker(s) and not any(s.startswith(m) for m in TRACKED_MARKERS)


def check_module_size(c: Corpus) -> List[Issue]:
    """C12 模块体量合理性。"""
    out: List[Issue] = []
    LOW, HIGH = 2, 30
    for doc in c.modules:
        t = pick_table(c.tables[doc.rel], *table_spec("business_rules"))
        if t is None:
            continue
        n = sum(1 for row in t.rows if _is_filled(get_col(row, "规则描述", "描述")))
        if n == 0:
            continue
        if n <= LOW:
            out.append(Issue("WARN", "C12", doc.rel,
                             f"仅 {n} 条业务规则，存在感偏低，确认是否应并入相邻模块"))
        elif n > HIGH:
            out.append(Issue("WARN", "C12", doc.rel,
                             f"业务规则达 {n} 条，偏多，确认是否应拆出子模块"))
    return out


def check_data_owners(c: Corpus) -> List[Issue]:
    """C13 数据写主唯一性。

    按实体名精确匹配：异形同义（「订单」/「Order」）不报，属漏报；
    但不会把同名当冲突误报。中文无词边界，模糊匹配会大面积误判，
    此处宁可漏报，符合「误报比漏报更糟」的既有取舍。
    """
    out: List[Issue] = []
    owners: Dict[Tuple[str, str], Set[str]] = {}
    where: Dict[Tuple[str, str], List[str]] = {}
    for doc in c.modules:
        t = pick_table(c.tables[doc.rel], *table_spec("data_requirements"))
        if t is None:
            continue
        for row in t.rows:
            entity = get_col(row, "实体")
            attr = get_col(row, "属性")
            if not _is_filled(entity) or not _is_filled(attr):
                continue
            owner = first_id(get_col(row, "写主"), RE_MOD)
            if not owner:
                continue
            key = (entity.strip(), attr.strip())
            owners.setdefault(key, set()).add(owner)
            where.setdefault(key, []).append(f"{owner}@{doc.rel}")
    for (entity, attr), ws in sorted(owners.items()):
        if len(ws) >= 2:
            out.append(Issue("WARN", "C13", "-",
                             f"数据「{entity}.{attr}」有多个写主 {sorted(ws)}，"
                             f"确认是冲突还是异形同义：{where[(entity, attr)]}"))
    return out


def check_mermaid_ids(c: Corpus) -> List[Issue]:
    """C14 Mermaid 图内模块 ID。

    代码块平时被 strip 掉以防示例 ID 被当真实引用误报（见 C02）；这里反其道，
    只解析 ```mermaid 块，提取真实 MOD-NNNN（泛指的 MOD-NNNN 不匹配 \\d{4,}，
    不报），比对模块索引。图里画了不存在的模块即文档错误。
    """
    out: List[Issue] = []
    mermaid_block = re.compile(r"```mermaid\n(.*?)```", re.S)
    for doc in c.docs:
        for m in mermaid_block.finditer(doc.text):
            for mid in sorted(set(RE_MOD.findall(m.group(1)))):
                if mid not in c.module_ids:
                    out.append(Issue("WARN", "C14", doc.rel,
                                     f"Mermaid 图引用了未登记的模块 {mid}，"
                                     f"需补模块或在图中改用已登记的 ID"))
    return out


# --------------------------------------------------------------------------
# C15：文档状态一致性
# --------------------------------------------------------------------------

def snapshot_recorded_docs(c: Corpus) -> Optional[Dict[str, Set[str]]]:
    """版本快照里的基线记录：文档名 -> 曾入基线的版本集合。库内没有快照产物时返回 None。

    返回 None 与返回空字典是两回事：前者是「这个库还没有快照文档」，C15 整条
    跳过；后者是「有快照文档但一条基线都没打」，此时标已冻结确实可疑。合并成
    一个空判断的话，骨架阶段会满屏误报。

    连版本一起收，不只收文档名：只比名字的话，「进过基线之后又改了正文、升了
    版本、状态没退」这一手完全看不出来——文档名还在基线里，检查照过。基线记录
    的是某一版的内容，比对的口径也必须是版本而不是文件。

    版本值统一大写：C11 允许 V1.0.0 / v1.0.0 两种写法（RE_DOC_VERSION 用
    [Vv]），基线表与文档头部各写各的时，逐字比对会误报。
    """
    if not c.snapshot:
        return None
    recorded: Dict[str, Set[str]] = {}
    for t in c.tables[c.snapshot.rel]:
        for row in t.rows:
            name = get_col(row, "文档", "产物", "文件").strip()
            if not name or is_empty_marker(name):
                continue
            ver = get_col(row, "版本").strip()
            slot = recorded.setdefault(os.path.basename(name), set())
            # 旧基线表只有「文档 | 版本」两列甚至只有文档名；版本读不到时不记，
            # 该文档仍算「进过基线」，只是版本无从比对（见 C15 第 3 项的分支）。
            if ver and not is_empty_marker(ver) and parse_doc_version(ver):
                slot.add(ver.upper())
    return recorded


def check_doc_status(c: Corpus) -> List[Issue]:
    """C15 文档状态一致性。

    状态回答的是「这一版能不能信」，版本回答「改到第几版」。C11 把版本管得很死，
    状态此前一项不管——四个取值只写在模板正文的一句注释里，填「进行中」也没人
    知道。这项检查把它从注释变成字段。

    权威源是各产物头部的状态行，综述模块索引表的状态列是视图。两处不一致时报
    ERROR 并给出两个行号——与 C01 的「索引 vs 文档」双向检查同模式：索引是视图，
    文档是事实。

    空白骨架必须零 ERROR：scaffold 生成的全是「草稿」，草稿是合法起点。
    """
    out: List[Issue] = []
    if not c.docs:
        return out

    adr_rels = {d.rel for d in c.decisions}
    snapshot_rel = c.snapshot.rel if c.snapshot else None

    # 1) 取值必须在封闭词表内。ADR 走自己的词表。
    for doc in c.docs:
        rel = doc.rel
        # 版本快照每次打基线都被追加内容，正文必然改动，给它状态行会让它永远
        # 处于「刚被改过」。它记录历史，不参与生命周期。
        if rel == snapshot_rel:
            continue
        declared = c.doc_status.get(rel)
        is_adr = rel in adr_rels
        valid = VALID_ADR_STATUS if is_adr else VALID_DOC_STATUS
        if declared is None:
            # 存量文档没有状态行时判 WARN，与 C11 缺版本行同口径，留迁移窗口
            out.append(Issue("WARN", "C15", rel,
                             f"元信息表缺「{STATUS_ROW_LABEL}」行。"
                             f"补一行 | {STATUS_ROW_LABEL} | {valid[0]} |，"
                             f"否则读者无从判断这份能不能拿去实现"))
            continue
        raw, lineno = declared
        if not raw or is_empty_marker(raw):
            out.append(Issue("ERROR", "C15", rel,
                             f"{STATUS_ROW_LABEL}为空。取值只能是 "
                             f"{' / '.join(valid)}", lineno))
            continue
        if raw.startswith("["):
            out.append(Issue("WARN", "C15", rel,
                             f"{STATUS_ROW_LABEL}尚未填写（当前为占位），"
                             f"定稿前须给出结论", lineno))
            continue
        if raw not in valid:
            hint = ("ADR 用的是决策处置词表，与其余产物的文档状态不通用"
                    if is_adr else
                    "词表封闭，见 references/decomposition-rules.md 第 8 节")
            out.append(Issue("ERROR", "C15", rel,
                             f"{STATUS_ROW_LABEL}「{raw}」不在取值范围内，"
                             f"只能是 {' / '.join(valid)}。{hint}", lineno))

    # 2) 综述模块索引表的状态列必须与模块文档头部一致
    for row in c.index_rows:
        mid = first_id(get_col(row, "模块 ID", "模块ID", "ID"), RE_MOD)
        if not mid:
            continue
        listed = get_col(row, STATUS_ROW_LABEL).strip()
        doc = c.module_doc(mid)
        if not doc or not listed or is_empty_marker(listed) or listed.startswith("["):
            continue
        declared = c.doc_status.get(doc.rel)
        if declared is None:
            continue
        actual, actual_ln = declared
        if actual.startswith("[") or not actual:
            continue
        if actual != listed:
            ov_rel = c.overview.rel if c.overview else "-"
            ov_ln = c.overview.line_of(mid) if c.overview else None
            out.append(Issue("ERROR", "C15", ov_rel,
                             f"{mid} 在模块索引表里是「{listed}」，模块文档头部是"
                             f"「{actual}」（{doc.rel}:{actual_ln}）。权威源是模块"
                             f"文档，索引表是视图——改状态时两处一起改", ov_ln))

    # 3) 已冻结的文档，必须有一条与当前版本相符的基线记录
    #
    # 只比文档名不够：进过基线之后又改正文、升版本、状态没退，文档名仍在基线里，
    # 检查照过。基线记的是某一版的内容，比对口径也必须是版本。
    recorded = snapshot_recorded_docs(c)
    if recorded is not None:
        for rel, (raw, lineno) in sorted(c.doc_status.items()):
            if raw != "已冻结":
                continue
            versions = recorded.get(os.path.basename(rel))
            if versions is None:
                out.append(Issue("WARN", "C15", rel,
                                 f"标记为已冻结，但版本快照里没有这份文档的基线"
                                 f"记录。已冻结的进入条件是「已被写入某次版本快照"
                                 f"基线」，用 --snapshot 补一次，或退回已评审",
                                 lineno))
                continue
            declared = c.doc_versions.get(rel)
            if declared is None or not versions:
                # 文档没版本行（C11 已报），或旧基线表没记版本：进过基线这一点
                # 成立，版本无从比对，不在这里重复报
                continue
            cur = declared[0].strip().upper()
            if parse_doc_version(cur) and cur not in versions:
                out.append(Issue("WARN", "C15", rel,
                                 f"标记为已冻结，当前版本是 {declared[0].strip()}，"
                                 f"但基线记录的是 {'、'.join(sorted(versions))}。"
                                 f"进基线之后正文又改过——冻结的是基线里那一版，"
                                 f"不是现在这一版。退回「草稿」重走评审，"
                                 f"或补打一次 --snapshot",
                                 lineno))

    # 4) 全库状态分布。判 INFO，与 C11 的版本分布提示对齐。
    dist: Dict[str, int] = {}
    for rel, (raw, _) in c.doc_status.items():
        if rel in adr_rels or not raw or raw.startswith("["):
            continue
        if raw in VALID_DOC_STATUS:
            dist[raw] = dist.get(raw, 0) + 1
    if dist:
        spread = "；".join(f"{s}: {n} 份" for s, n in
                          sorted(dist.items(), key=lambda kv: VALID_DOC_STATUS.index(kv[0])))
        out.append(Issue("INFO", "C15", "-",
                         f"全库文档状态分布：{spread}"))
    return out


# --------------------------------------------------------------------------
# C16：终态文档引用完整性
# --------------------------------------------------------------------------

def check_terminal_refs(c: Corpus) -> List[Issue]:
    """C16 终态文档引用完整性。

    两类检查，都是「终态状态值与引用关系的一致性」：
    - ADR 标「被取代」是关系性状态：不写编号就是话只说了一半。查编号存在、
      双向声明、循环取代。
    - 模块标「已废弃」与关系矩阵的活跃依赖冲突：废弃模块不应还在关系矩阵
      里作为源或目标。

    只查结构化引用（ADR 正文 + 关系矩阵列），不查自由文本里的 ID -- 修订标记、
    变更记录、ADR 理由段里的 ID 都有合法的历史追溯语义，查了就是误报。
    """
    out: List[Issue] = []

    # --- P5: ADR 取代关系 ---
    # 被取代的 ADR -> 正文里找到的 ADR-NNNN 引用（排除自身）
    # 占位内容 [待确认: ...] 里的示例 ADR 编号不算真实引用，先去掉再扫描--
    # 否则模板预填的 ADR-0001 会被当成取代者声明，静默放行真正的漏填。
    superseded: Dict[str, Tuple[str, List[str], Optional[int]]] = {}
    for doc in c.decisions:
        rel = doc.rel
        aid = first_id(os.path.basename(rel), RE_ADR)
        if not aid:
            continue
        status = c.doc_status.get(rel)
        if not status:
            continue
        raw, lineno = status
        if raw.strip() != "被取代":
            continue
        body = strip_code_blocks(doc.text)
        body = re.sub(r"\[待确认:[^\]]*\]", "", body)
        refs = [r for r in RE_ADR.findall(body) if r != aid]
        superseded[aid] = (rel, refs, lineno)

    for aid, (rel, refs, lineno) in sorted(superseded.items()):
        if not refs:
            out.append(Issue("WARN", "C16", rel,
                             f"{aid} 标「被取代」但正文没有取代它的 ADR 编号。"
                             f"「被取代」是关系性状态，不写编号就是话只说了一半",
                             lineno))
            continue
        declared = False
        for ref in refs:
            if ref not in c.adr_ids:
                continue  # C02 报悬空
            ref_doc = next((d for d in c.decisions
                            if first_id(os.path.basename(d.rel), RE_ADR) == ref), None)
            if ref_doc:
                ref_body = re.sub(r"\[待确认:[^\]]*\]", "",
                                  strip_code_blocks(ref_doc.text))
                if aid in ref_body:
                    declared = True
                    break
        if not declared:
            out.append(Issue("WARN", "C16", rel,
                             f"{aid} 标被 {'、'.join(refs)} 取代，但取代者的正文"
                             f"都没有提到 {aid}。取代关系应双向声明",
                             lineno))
        # 循环检测：A 标被 B 取代、B 标被 A 取代。只在 A < B 时报，避免两条重复
        for ref in refs:
            if ref in superseded and aid < ref:
                _, ref_refs, _ = superseded[ref]
                if aid in ref_refs:
                    out.append(Issue("ERROR", "C16", rel,
                                     f"{aid} 标被 {ref} 取代，{ref} 又标被 {aid} "
                                     f"取代。循环取代是逻辑错误",
                                     lineno))

    # --- P6: 已废弃模块在关系矩阵 ---
    deprecated_mods: Set[str] = set()
    for doc in c.modules:
        mid = first_id(os.path.basename(doc.rel), RE_MOD)
        if not mid:
            continue
        status = c.doc_status.get(doc.rel)
        if status and status[0].strip() == "已废弃":
            deprecated_mods.add(mid)

    if deprecated_mods and c.relations:
        ov = c.overview.rel if c.overview else "-"
        reported: Set[str] = set()
        for row in c.relations:
            src = first_id(get_col(row, *RELATION_COLUMNS["source"]), RE_MOD)
            dst = first_id(get_col(row, *RELATION_COLUMNS["target"]), RE_MOD)
            for mid, label in ((src, "源"), (dst, "目标")):
                if mid in deprecated_mods:
                    other = dst if label == "源" else src
                    key = f"{mid}-{other}"
                    if key in reported:
                        continue
                    reported.add(key)
                    out.append(Issue("WARN", "C16", ov,
                                     f"关系矩阵的{label}模块 {mid} 已废弃，"
                                     f"但仍出现在与 {other or '?'} 的关系中。"
                                     f"已废弃模块不应有活跃依赖声明--"
                                     f"要么模块没真废弃，要么该清理这条关系"))
    return out


CHECKS = {
    "C01": check_structure,
    "C02": check_id_references,
    "C03": check_links,
    "C04": check_nfr_coverage,
    "C05": check_relations,
    "C06": check_traceability,
    "C07": check_terminology,
    "C08": check_writing_style,
    "C09": check_placeholders,
    "C10": check_exemption_hygiene,
    "C11": check_doc_versions,
    "C12": check_module_size,
    "C13": check_data_owners,
    "C14": check_mermaid_ids,
    "C15": check_doc_status,
    "C16": check_terminal_refs,
}


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

def pad(text: str, width: int) -> str:
    """按显示宽度补齐，中日韩字符按两格计算。"""
    shown = sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)
    return text + " " * max(0, width - shown)


def render_snapshot(corpus: Corpus, review_date: str) -> Tuple[str, List[str]]:
    """输出版本快照的表格行，供追加进《版本快照》。返回 (表格, 门禁提示)。

    脚本生成而非人工维护：快照的价值全在「与实际一致」，手抄一份必然漂移，
    那时它记录的就不再是基线，而是某人上次记得更新的时刻。

    带状态列：一份声称记录「某次评审通过的是哪一版内容」的基线表，不含「是否
    通过评审」这一半信息就只是一次脚本运行记录。非「已评审」的文档会进门禁提示，
    由调用方打到 stderr——本函数的返回值会被 `>>` 追加进文档，提示混进去就成了
    基线正文的一部分。

    脚本不回写文档：这个包除 scaffold_docs.py 外没有写文档的脚本，破这个约定的
    代价大于省下的手工。这里只提示该改哪些，人去改，C15 下一轮验证改对了没有。
    """
    rels = sorted(corpus.doc_versions)
    if not rels:
        return ("未解析到任何文档版本，先给各产物补「文档版本」行", [])

    snapshot_rel = corpus.snapshot.rel if corpus.snapshot else None
    # ADR 走决策处置词表（提议/已采纳/…），「已评审」对它不是合法取值，
    # 要求它已评审是拿错词表卡人。它照常进基线表，只是不进门禁清单。
    adr_rels = {d.rel for d in corpus.decisions}
    # 纵表：一行一份文档。横表在文档数上去之后会宽到没法读。
    lines = [f"## {review_date} 基线", "",
             "| 文档 | 版本 | 状态 |", "|------|------|------|"]
    unreviewed: List[str] = []
    for rel in rels:
        raw = corpus.doc_status.get(rel, ("", None))[0].strip()
        shown = raw if raw and not raw.startswith("[") else "——"
        ver = corpus.doc_versions[rel][0]
        lines.append(f"| {rel} | {ver or '——'} | {shown} |")
        # 版本快照自身不参与生命周期（每次打基线都被追加内容），不计入门禁
        if rel != snapshot_rel and rel not in adr_rels and raw != "已评审":
            unreviewed.append(f"{rel}（{shown}）")

    missing = [d.rel for d in corpus.docs if d.rel not in corpus.doc_versions]
    lines += ["", f"共 {len(rels)} 份文档。"]
    if missing:
        lines.append(f"另有 {len(missing)} 份缺「文档版本」行，未计入："
                     + "、".join(sorted(missing)))

    notes: List[str] = []
    if unreviewed:
        notes.append(f"以下 {len(unreviewed)} 份文档不是「已评审」，仍被写进了基线："
                     + "、".join(unreviewed))
        notes.append("基线记录的是评审通过的内容。先评审、把状态改成「已评审」"
                     "再打快照，否则这份基线记的是某次脚本运行而非某次评审。")
    else:
        gated = len([r for r in rels
                     if r != snapshot_rel and r not in adr_rels])
        notes.append(f"参与门禁的 {gated} 份文档均为「已评审」。基线落定后把它们"
                     "改成「已冻结」——脚本不回写文档，改完跑 --only C15 验证。")
    return ("\n".join(lines), notes)


def render_text(issues: List[Issue], corpus: Corpus, ran: List[str]) -> str:
    errors = [i for i in issues if i.level == "ERROR"]
    warns = [i for i in issues if i.level == "WARN"]
    infos = [i for i in issues if i.level == "INFO"]

    lines = ["需求文档一致性校验报告", "=" * 60,
             f"扫描目录: {corpus.root}",
             f"文档总数: {len(corpus.docs)}  模块: {len(corpus.modules)}  "
             f"场景: {len(corpus.scenarios)}  NFR: {len(corpus.nfr_ids)}  "
             f"需求条目: {len(corpus.req_ids)}",
             ""]

    for code in ran:
        sub = [i for i in issues if i.code == code]
        errs = sum(1 for i in sub if i.level == "ERROR")
        wrns = sum(1 for i in sub if i.level == "WARN")
        status = "通过  " if errs == 0 else "未通过"
        lines.append(f"{code} {pad(CHECK_TITLES[code], 30)}{status}   "
                     f"错误 {errs}  警告 {wrns}")
    lines.append("")

    for title, group in (("错误", errors), ("警告", warns), ("提示", infos)):
        if not group:
            continue
        lines.append(f"—— {title} ({len(group)}) " + "-" * 40)
        for issue in group:
            lines.append(issue.render())
        lines.append("")

    # 结论只看 ERROR，不感知 --strict：表达的是「结构契约是否完整、能否继续推进」。
    # 退出码才是定稿门禁（--strict 下 WARN 也计入失败，见 main()）。两者正交--
    # --strict 下「结论: 通过」+ 退出码 1 是正常的：结构没问题，但还没到能定稿的程度。
    # 不让结论随 WARN 翻成「未通过」，是因为 WARN 是过程态（占位符边写边填），
    # 否则第 2~3 轮推进时报告会一直喊失败，与「承认过程态允许边写边推进」相悖。
    verdict = "通过" if not errors else f"未通过，{len(errors)} 项错误需修复"
    lines.append("=" * 60)
    lines.append(f"结论: {verdict}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="需求文档网络一致性校验")
    ap.add_argument("directory", help="需求文档根目录")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--strict", action="store_true", help="警告也计入失败")
    ap.add_argument("--only", help="只运行指定检查，逗号分隔，如 C04,C05")
    ap.add_argument("--snapshot", metavar="日期",
                    help="不跑检查，输出全库文档版本与状态快照的 Markdown 表格行，"
                         "参数是评审日期（如 2026-08-09）。追加进《版本快照》即可。"
                         "非「已评审」的文档会在 stderr 列出；配 --strict 时退出 1")
    args = ap.parse_args()

    if not os.path.isdir(args.directory):
        print(f"目录不存在: {args.directory}", file=sys.stderr)
        return 2

    corpus = Corpus(args.directory)
    if not corpus.docs:
        print(f"目录中没有 Markdown 文件: {args.directory}", file=sys.stderr)
        return 2

    if args.snapshot:
        table, notes = render_snapshot(corpus, args.snapshot)
        print(table)
        # 门禁提示走 stderr：标准输出会被 `>> 04-版本快照.md` 追加进文档，
        # 提示混进去就成了基线正文的一部分。
        for note in notes:
            print(note, file=sys.stderr)
        # 不查「这一版是否已进过基线」：同一版本重复进基线是正常操作（同一天打两次、
        # 评审后未改动再打一次），报它就是误报。「版本没变而内容变了」由 C11 与
        # impact_analysis 管，不归快照门禁。
        blocked = any("不是「已评审」" in n for n in notes)
        return 1 if (blocked and args.strict) else 0

    ran = list(CHECKS)
    if args.only:
        wanted = {x.strip().upper() for x in args.only.split(",")}
        unknown = wanted - set(CHECKS)
        if unknown:
            print(f"未知检查项: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        ran = [c for c in CHECKS if c in wanted]

    issues: List[Issue] = []
    for code in ran:
        issues.extend(CHECKS[code](corpus))

    if args.format == "json":
        payload = {
            "root": corpus.root,
            "summary": {
                "documents": len(corpus.docs),
                "modules": len(corpus.modules),
                "scenarios": len(corpus.scenarios),
                "nfrs": len(corpus.nfr_ids),
                "requirements": len(corpus.req_ids),
                "errors": sum(1 for i in issues if i.level == "ERROR"),
                "warnings": sum(1 for i in issues if i.level == "WARN"),
            },
            "checks": ran,
            "issues": [asdict(i) for i in issues],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(issues, corpus, ran))

    # 退出码 = 定稿门禁：默认只看 ERROR，--strict 把 WARN 也计入失败。
    # 与 render_text 的「结论」文字正交--结论只看 ERROR（结构契约），这里看门禁。
    # 所以 --strict 下结论显示「通过」、退出码 1 不矛盾。
    failed = any(i.level == "ERROR" for i in issues)
    if args.strict:
        failed = failed or any(i.level == "WARN" for i in issues)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
