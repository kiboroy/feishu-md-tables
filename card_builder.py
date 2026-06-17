"""把 ``Block`` 序列拼成飞书 CardKit v2 ``schema: 2.0`` 的 card JSON。

飞书 Card 2.0 (CardKit) 整体结构（参考官方文档）::

    {
        "schema": "2.0",
        "header": { "template": "blue", "title": { "tag": "plain_text", "content": "..." } },
        "body": {
            "elements": [
                { "tag": "markdown", "content": "..." },
                { "tag": "table", "page_size": 5, "columns": [...], "rows": [...] },
                ...
            ]
        }
    }

约束：
* 单条 message 的 body.elements 不限数量，但飞书客户端有总大小上限
  （官方建议 < 30KB 文本 + 表格合计）。本构造器不做硬截断，由调用方
  在 monkey-patch 里通过 ``truncate_message`` 控制
* 表格 ``data_type`` 支持 ``text`` / ``lark_md`` / ``markdown`` / ``number``
  / ``options`` / ``persons`` / ``date``。本构造器默认用 ``lark_md``，
  这样 cell 里的 ``**bold**`` / `` `code` `` 能在飞书客户端继续渲染
* 表格 ``page_size`` 控制客户端一次显示多少行（"查看更多"按钮的阈值）
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .parser import Block, MarkdownTable  # noqa: F401

logger = logging.getLogger("hermes.plugins.feishu_md_tables")


# 飞书 header template 颜色
HEADER_TEMPLATES = {
    "blue", "wathet", "turquoise", "green", "yellow",
    "orange", "red", "carmine", "violet", "purple",
    "indigo", "grey",
}


@dataclass
class CardConfig:
    """用户可配置项。默认值合理，无需配置即可工作。"""

    header_template: str = "blue"
    header_title: str = ""               # 为空时不渲染 header
    page_size: int = 5                   # 表格 "查看更多" 阈值
    cell_data_type: str = "lark_md"      # 默认 cell 类型
    use_markdown_for_text: bool = True   # text block 用 markdown 标签（支持粗体/链接等）
    # 单张卡片最多放几个 table。飞书 CardKit v2 硬上限 = 3（API 拒收 ErrCode 11310）。
    # 留 2 是因为表格行多/带 lark_md 时容易撞 30KB 整体 body 限制。
    max_tables_per_card: int = 2


def _column_dict(col, default_data_type: str) -> Dict[str, Any]:
    """单列定义 -> 飞书 table column dict。"""
    return {
        "name": col.name,
        "display_name": col.display_name,
        "data_type": default_data_type,
        "horizontal_align": col.align,
    }


def _row_dict(row: Tuple[str, ...], columns: Tuple, data_type: str) -> Dict[str, Any]:
    """单行 -> 飞书 table row dict。

    row 是 ``(cell_value_for_col_0, cell_value_for_col_1, ...)``。
    飞书 ``lark_md`` / ``markdown`` 类型下 cell 必须是 string。
    """
    return {col.name: cell for col, cell in zip(columns, row)}


def _table_element(table: MarkdownTable, cfg: CardConfig) -> Dict[str, Any]:
    """一个 ``Block(kind="table")`` -> 飞书 ``{tag: "table", ...}`` 元素。"""
    return {
        "tag": "table",
        "page_size": max(1, min(cfg.page_size, 10)),  # 飞书限制 [1,10]
        "columns": [_column_dict(c, cfg.cell_data_type) for c in table.columns],
        "rows": [_row_dict(row, table.columns, cfg.cell_data_type) for row in table.rows],
    }


# 飞书 CardKit v2 单卡 table 组件上限：硬上限 = 3（API ErrCode 11310），
# 默认安全值 = 2（保守，避开 30KB body 限制）。具体生效值由调用方通过
# ``CardConfig.max_tables_per_card`` 注入；本模块不再持有 module-level 常量。
#
# 历史：2026-06-17 复现，6 张表（均为 5 行 4 列）的单卡被飞书 API 拒收
# 报 ErrCode 11310 "card table number over limit"。旧实现只在 logger.error
# 警告但**仍然把超限 table 塞进 elements**，导致飞书拒收、SDK Event loop
# 关闭、网关入站死锁——本次修复后改在 build_card 内部自动降级为 markdown，
# 配合上游 feishu_send_card 走 split_blocks_by_table_groups 切分路径，
# 双保险。


def _table_as_markdown_fallback(table: MarkdownTable) -> Dict[str, Any]:
    """超出 5 个 table 限制时，把表格降级为纯文本块。

    不用 markdown 表格语法（飞书 API 可能把 |---| 也算作 table 组件），
    改用简单的 key: value 列表格式。
    """
    lines = []
    for row in table.rows:
        pairs = []
        for col, cell in zip(table.columns, row):
            pairs.append(f"{col.display_name}: {cell}")
        lines.append(" | ".join(pairs))
    content = "\n".join(lines)
    return {"tag": "markdown", "content": content}


def _text_element(text: str, cfg: CardConfig) -> Optional[Dict[str, Any]]:
    """一个 ``Block(kind="text")`` -> 飞书 text/markdown 元素。"""
    text = text.strip()
    if not text:
        return None
    if cfg.use_markdown_for_text:
        return {"tag": "markdown", "content": text}
    return {"tag": "plain_text", "content": text}


def build_card(blocks: List[Block], cfg: Optional[CardConfig] = None) -> Dict[str, Any]:
    """把 ``Block`` 列表拼成 CardKit v2 card dict。

    安全保证（防御性兜底）：

    * 单卡内 table 组件数 ≤ ``cfg.max_tables_per_card``（默认 2）
    * 超限的 table 会被降级为 markdown key:value 文本块（不是 silent drop）
    * 调用方应在切分阶段保证不超限（见 ``parser.split_blocks_by_table_groups``）
    * 本函数本身不再抛异常——降级路径保证客户端永远能拿到可渲染 card

    设计取舍：选择降级而非抛错，是因为：

    1. ``build_card`` 是纯函数，多个调用方（feishu_send_card 工具、单元测试、
       未来扩展）都依赖它。抛错会破坏向后兼容。
    2. 降级保留信息（不丢数据），只是格式从原生 table 退化为文本块。
    3. 飞书客户端不会拒收降级后的 card（ErrCode 11310 完全规避）。
    """
    cfg = cfg or CardConfig()
    max_tables = max(1, cfg.max_tables_per_card)
    elements: List[Dict[str, Any]] = []
    table_count = 0
    overflowed = 0
    for blk in blocks:
        if blk.kind == "table" and blk.table is not None:
            if table_count >= max_tables:
                # 防御性降级：超限的 table 转为 markdown key:value 文本
                overflowed += 1
                fallback = _table_as_markdown_fallback(blk.table)
                elements.append(fallback)
                logger.warning(
                    "[feishu-md-tables] card had >%d tables; overflowing table "
                    "#%d downgraded to markdown key:value to avoid Feishu "
                    "ErrCode 11310 'card table number over limit'",
                    max_tables, table_count + 1,
                )
            else:
                elements.append(_table_element(blk.table, cfg))
                table_count += 1
        elif blk.kind == "text":
            el = _text_element(blk.text, cfg)
            if el is not None:
                elements.append(el)
    if overflowed:
        logger.error(
            "[feishu-md-tables] %d table(s) overflowed and were downgraded; "
            "upstream caller should use split_blocks_by_table_groups() first",
            overflowed,
        )
    if not elements:
        elements.append({"tag": "markdown", "content": ""})

    card: Dict[str, Any] = {
        "schema": "2.0",
        "body": {"elements": elements},
    }
    if cfg.header_title:
        tpl = cfg.header_template if cfg.header_template in HEADER_TEMPLATES else "blue"
        card["header"] = {
            "template": tpl,
            "title": {"tag": "plain_text", "content": cfg.header_title},
        }
    return card


def build_card_payload(blocks: List[Block], cfg: Optional[CardConfig] = None) -> str:
    """便利函数：返回 ``json.dumps(card)`` 的字符串，可直接喂给飞书 API。"""
    return json.dumps(build_card(blocks, cfg), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 降级
# ---------------------------------------------------------------------------


def build_plain_text_fallback(content: str) -> str:
    """当解析失败/不想走 CardKit 时，回退到 ``text`` 类型 payload。

    飞书的 ``text`` 类型只接受 string，所以这里返回 JSON 字符串。
    """
    return json.dumps({"text": content}, ensure_ascii=False)
