"""Markdown 表格解析器。

将 GitHub-Flavored-Markdown (GFM) 风格的管道表格解析为 (columns, rows)
结构，供飞书 CardKit v2 的 ``table`` 组件使用。

规范要点（与 GFM / CommonMark 表格扩展保持一致）：

* 表格起始：至少一行以 ``|`` 开头、含 ``|`` 的内容
* 紧跟一行由 ``|``、``-``、``:``、空格组成的"分隔行"
  * 默认对齐：``---`` = left，``:---`` = left，``---:`` = right，``:---:`` = center
* 数据行：每行按 ``|`` 切分；首尾空 cell 容忍（GFM 习惯）
* 转义：cell 内的 ``\\|`` 视为字面量 ``|``
* 行内格式：cell 内容**按原样保留**（含 ``**bold**``、`` `code` `` 等），
  飞书 table 组件支持 ``data_type: lark_md``，会在客户端再渲染
* 列数对齐：数据行 cell 数若与表头不一致，按表头列数截断/补空

所有函数都是纯函数，**不依赖 Hermes**——便于在隔离环境单测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableColumn:
    """单列表头定义。"""

    name: str               # 列内部 key（飞书要求字母数字下划线，本解析器自动 sanitize）
    display_name: str       # 显示文本（来自 markdown 表头）
    align: str = "left"     # "left" | "center" | "right"
    data_type: str = "text" # "text" | "lark_md" | "markdown" | ...


@dataclass(frozen=True)
class MarkdownTable:
    """一个 markdown 表格的解析结果。"""

    columns: Tuple[TableColumn, ...]
    rows: Tuple[Tuple[str, ...], ...]   # 每一行 = 一组 cell 文本
    raw: str                            # 原始 markdown 文本，便于调试/回退


@dataclass
class Block:
    """消息中的一个 block：要么是纯文本，要么是一个表格。"""

    kind: str  # "text" | "table"
    text: str = ""                 # for "text"
    table: Optional[MarkdownTable] = None  # for "table"


# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------

# 表格头行：以 | 开头、含至少一个 |
_TABLE_HEADER_RE = re.compile(r"^\s*\|(.+)\|\s*$")
# 表格分隔行： | --- | :---: | ---: |  这种
# 兼容有空格和无空格的紧凑写法
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|(\s*:?-{2,}:?\s*\|)+\s*$"
)
# 单个 cell 的对齐指示符（cell 内部，至少 2 个 -）
_ALIGN_RE = re.compile(r"^\s*:?(-{2,}):?\s*$")
# 任意 cell 文本里可能的前后空白
_CELL_TRIM_RE = re.compile(r"^\s+|\s+$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_name(s: str, idx: int) -> str:
    """把表头文本变成合法的飞书 column name（仅字母数字下划线）。"""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", s.strip())
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"col_{idx}_{cleaned}" if cleaned else f"col_{idx}"
    return cleaned


def _split_row(line: str) -> List[str]:
    """把一行 ``| a | b | c |`` 切成 ``[a, b, c]``。

    GFM 习惯允许首尾的 ``|`` 之后无内容；我们也容忍。
    转义的 ``\\|`` 在 cell 内还原为 ``|``。
    """
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    # 保留转义：把 \| 临时替换成占位符
    PLACEHOLDER = "\x00PIPE\x00"
    parts = stripped.replace("\\|", PLACEHOLDER).split("|")
    return [
        _CELL_TRIM_RE.sub("", p).replace(PLACEHOLDER, "|")
        for p in parts
    ]


def _parse_align(token: str) -> str:
    m = _ALIGN_RE.match(token)
    if not m:
        return "left"
    t = token.strip()
    if t.startswith(":") and t.endswith(":"):
        return "center"
    if t.endswith(":"):
        return "right"
    return "left"


# ---------------------------------------------------------------------------
# Single-table parser
# ---------------------------------------------------------------------------


def parse_markdown_table(block: str) -> Optional[MarkdownTable]:
    """解析一个 *单独的* markdown 表格块（包含表头 + 分隔行 + 数据行）。

    返回 ``None`` 表示该块不是一个合法表格。
    """
    if not block or not block.strip():
        return None

    lines = block.strip("\n").splitlines()
    if len(lines) < 2:
        return None

    if not _TABLE_HEADER_RE.match(lines[0]):
        return None
    if not _TABLE_SEPARATOR_RE.match(lines[1]):
        return None

    header_cells = _split_row(lines[0])
    sep_cells = _split_row(lines[1])

    columns: List[TableColumn] = []
    for i, (hcell, scell) in enumerate(zip(header_cells, sep_cells)):
        columns.append(
            TableColumn(
                name=_sanitize_name(hcell, i),
                display_name=hcell.strip(),
                align=_parse_align(scell),
            )
        )

    rows: List[Tuple[str, ...]] = []
    n_cols = len(columns)
    for raw in lines[2:]:
        # 容错：完全空行跳过
        if not raw.strip():
            continue
        if not raw.lstrip().startswith("|"):
            # 数据行必须以 | 开头；不合法行跳过
            continue
        cells = _split_row(raw)
        # 对齐列数
        if len(cells) < n_cols:
            cells = cells + [""] * (n_cols - len(cells))
        elif len(cells) > n_cols:
            cells = cells[:n_cols]
        rows.append(tuple(cells))

    return MarkdownTable(
        columns=tuple(columns),
        rows=tuple(rows),
        raw=block,
    )


# ---------------------------------------------------------------------------
# Block splitter — 把一整段消息切成 (text | table) 序列，**保序**
# ---------------------------------------------------------------------------


def _is_separator_line(line: str) -> bool:
    return bool(_TABLE_SEPARATOR_RE.match(line))


def _is_table_header_line(line: str) -> bool:
    return bool(_TABLE_HEADER_RE.match(line))


def split_into_blocks(content: str) -> List[Block]:
    """把消息内容切成 ``Block`` 列表。

    规则：
    1. 逐行扫描
    2. 当连续出现 ``header + separator`` 模式时，进入"表格捕获状态"
    3. 捕获状态里所有以 ``|`` 开头的行都视作数据行（包括空行分隔的继续行）
    4. 一旦遇到不以 ``|`` 开头的行，结束当前表格，输出 ``Block(kind="table")``
    5. 非表格行累积成 ``Block(kind="text")``

    简单清晰，对绝大多数 LLM 输出稳定。极端情况（表格前后夹杂未转义 ``|``
    的行）会回退为 text，由调用方决定降级策略。
    """
    if not content:
        return []

    lines = content.splitlines()
    blocks: List[Block] = []
    text_buffer: List[str] = []
    i = 0
    n = len(lines)

    def flush_text() -> None:
        if text_buffer:
            blocks.append(Block(kind="text", text="\n".join(text_buffer)))
            text_buffer.clear()

    while i < n:
        line = lines[i]
        # 尝试识别一个表格：要求 line[i] 是表头, line[i+1] 是分隔
        if (
            _is_table_header_line(line)
            and i + 1 < n
            and _is_separator_line(lines[i + 1])
        ):
            # 进入表格：收集表头 + 分隔 + 后续数据行
            table_lines = [line, lines[i + 1]]
            j = i + 2
            while j < n:
                nxt = lines[j]
                if not nxt.strip():
                    # 表格内空行：可能是软分隔。peek 一下：
                    #   * 如果空行之后是「header + separator」模式 → 那是下一个新表，结束当前表
                    #   * 如果空行之后只是普通 | 起头 → 当前表的继续行
                    if (
                        j + 2 < n
                        and _is_table_header_line(lines[j + 1])
                        and _is_separator_line(lines[j + 2])
                    ):
                        # 空行之后是另一个表 — 结束当前表（保留空行作为分隔，由调用方重组）
                        break
                    if j + 1 < n and lines[j + 1].lstrip().startswith("|"):
                        table_lines.append(nxt)
                        j += 1
                        continue
                    else:
                        break
                if nxt.lstrip().startswith("|"):
                    # 普通 | 起头的行：还要区分「本表数据行」vs「新表 header」
                    # 如果下一行是 separator → 这是新表的 header，停止本表
                    if j + 1 < n and _is_separator_line(lines[j + 1]):
                        break
                    table_lines.append(nxt)
                    j += 1
                else:
                    break

            # 把累积的 text 先 flush
            flush_text()

            table = parse_markdown_table("\n".join(table_lines))
            if table is not None and table.rows:
                blocks.append(Block(kind="table", table=table))
            else:
                # 解析失败——当作普通 text 保留原始内容
                text_buffer.extend(table_lines)

            i = j
            continue

        text_buffer.append(line)
        i += 1

    flush_text()
    return blocks


# ---------------------------------------------------------------------------
# Convenience: detect whether a message contains any table
# ---------------------------------------------------------------------------


# 跟 feishu.py 里现有 _MARKDOWN_TABLE_RE 保持一致；只用来快速判断
_QUICK_TABLE_HINT_RE = re.compile(r"^\|.*\|\s*\n\s*\|[-:| ]+\|", re.MULTILINE)


def has_markdown_table(content: str) -> bool:
    """快速判断一段文本里是否包含 GFM 表格。"""
    if not content:
        return False
    return bool(_QUICK_TABLE_HINT_RE.search(content))


# ---------------------------------------------------------------------------
# Block grouping — 把 block 序列切成「每组最多 N 个 table」的多张卡片
# ---------------------------------------------------------------------------


def split_blocks_by_table_groups(
    blocks: List[Block], max_tables_per_card: int = 2
) -> List[List[Block]]:
    """把 ``blocks`` 切成一组一组的子序列,每组里 table block 数 ≤ ``max_tables_per_card``。

    用途: 飞书 CardKit v2 单卡最多 5 个 table 组件,但我们更保守地用 2 个/卡
    以避开「每张表很大 + 单卡 30KB 限制」的复合翻车。超过阈值的表格数会触发
    多张卡片顺序发送(由调用方在 ``send()`` 层循环)。

    切分规则:
      1. 保留文档顺序(text block / table block 都在原位)
      2. 按 table block 数量切——一旦当前组内 table 数达到上限,
         立即开新组
      3. text block 跟在其最近的 table 后面归到同一组;如果一组全是 text block
         没有 table,会单独成一组(为了保留所有 text 内容,不丢)
      4. 末尾组若为空会被丢弃

    例子 (max_tables_per_card=2):
      [text1, table1, table2, text2, table3, table4, table5]
        -> [[text1, table1, table2], [text2, table3, table4], [table5]]
    """
    if max_tables_per_card < 1:
        raise ValueError("max_tables_per_card must be >= 1")

    if not blocks:
        return []

    groups: List[List[Block]] = []
    current: List[Block] = []
    table_count = 0

    for blk in blocks:
        is_table = blk.kind == "table" and blk.table is not None

        if is_table:
            if table_count >= max_tables_per_card and current:
                # 当前组已满,切到下一组
                groups.append(current)
                current = []
                table_count = 0
            current.append(blk)
            table_count += 1
        else:
            # text block 永远跟最近的 table 同组,无 table 时单成一组
            current.append(blk)

    if current:
        groups.append(current)

    return groups

