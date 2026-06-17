"""单元测试 — Markdown 解析 + Card 构造。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# 让单测在不安装插件的情况下也能 import 本地模块
# 关键：插件目录名含 '-'，不是合法 Python 包名（实际加载时 Hermes 用
# importlib.util 把 manifest.path 当独立模块导入）。这里把目录当独立
# 包加载，但**跳过 __init__.py**——它会 import gateway，单测不该触发。
ROOT = Path(__file__).resolve().parent.parent  # feishu-md-tables/
import importlib.util as _ilu
import sys as _sys


def _load_sub_as_pkg(pkg_name: str, init_path: Path, sub_init: bool = False):
    """把某个目录当独立包加载。如果 ``sub_init=False``，__init__.py 不会被执行（仅作为包容器）。"""
    spec = _ilu.spec_from_file_location(
        pkg_name, init_path, submodule_search_locations=[str(init_path.parent)]
    )
    mod = _ilu.module_from_spec(spec)
    _sys.modules[pkg_name] = mod
    if sub_init:
        spec.loader.exec_module(mod)
    return mod


def _load_file(pkg_name: str, file_path: Path, parent_pkg=None):
    full = f"{pkg_name}.{file_path.stem}"
    spec = _ilu.spec_from_file_location(full, file_path)
    mod = _ilu.module_from_spec(spec)
    _sys.modules[full] = mod
    spec.loader.exec_module(mod)
    if parent_pkg is not None:
        parent_pkg.__dict__[file_path.stem] = mod
    return mod


# 把 feishu-md-tables/ 目录当包容器，但不执行其 __init__.py（避免 import gateway）
_plugin_pkg = _load_sub_as_pkg("feishu_md_tables", ROOT / "__init__.py", sub_init=False)
parser = _load_file("feishu_md_tables", ROOT / "parser.py", _plugin_pkg)
card_builder = _load_file(
    "feishu_md_tables", ROOT / "card_builder.py", _plugin_pkg
)

parse_markdown_table = parser.parse_markdown_table
split_into_blocks = parser.split_into_blocks
has_markdown_table = parser.has_markdown_table
MarkdownTable = parser.MarkdownTable
Block = parser.Block
CardConfig = card_builder.CardConfig
build_card = card_builder.build_card
build_card_payload = card_builder.build_card_payload


class TestParseSimple(unittest.TestCase):
    def test_basic(self):
        md = (
            "| Name | Score |\n"
            "|------|-------|\n"
            "| Alice | 98 |\n"
            "| Bob   | 85 |\n"
        )
        t = parse_markdown_table(md)
        self.assertIsNotNone(t)
        self.assertEqual(len(t.columns), 2)
        self.assertEqual(t.columns[0].display_name, "Name")
        self.assertEqual(t.columns[1].display_name, "Score")
        self.assertEqual(t.columns[0].name, "Name")
        self.assertEqual(len(t.rows), 2)
        self.assertEqual(t.rows[0], ("Alice", "98"))
        self.assertEqual(t.rows[1], ("Bob", "85"))

    def test_alignment(self):
        # GFM 标准对齐写法：:---  = left, :---: = center, ---: = right
        md = (
            "| L | C | R |\n"
            "|:---|:---:|---:|\n"
            "| 1 | 2 | 3 |\n"
        )
        t = parse_markdown_table(md)
        self.assertIsNotNone(t, "table should parse")
        self.assertEqual(t.columns[0].align, "left")
        self.assertEqual(t.columns[1].align, "center")
        self.assertEqual(t.columns[2].align, "right")

    def test_invalid(self):
        self.assertIsNone(parse_markdown_table(""))
        self.assertIsNone(parse_markdown_table("hello world"))
        self.assertIsNone(parse_markdown_table("| a | b |\nnot separator"))
        self.assertIsNone(parse_markdown_table("| a | b |\n| - | - |\n"))  # <2 dashes

    def test_escape_pipe(self):
        md = (
            "| k | v |\n"
            "|---|---|\n"
            r"| a \| b | c |" + "\n"
        )
        t = parse_markdown_table(md)
        self.assertEqual(t.rows[0], (r"a | b", "c"))

    def test_short_row_padded(self):
        md = (
            "| a | b | c |\n"
            "|---|---|---|\n"
            "| 1 |\n"
        )
        t = parse_markdown_table(md)
        self.assertEqual(t.rows[0], ("1", "", ""))

    def test_long_row_truncated(self):
        md = (
            "| a | b |\n"
            "|---|---|\n"
            "| 1 | 2 | 3 | 4 |\n"
        )
        t = parse_markdown_table(md)
        self.assertEqual(t.rows[0], ("1", "2"))


class TestSplitBlocks(unittest.TestCase):
    def test_pure_text(self):
        blocks = split_into_blocks("hello\nworld")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "text")
        self.assertIn("hello", blocks[0].text)

    def test_pure_table(self):
        md = "| x | y |\n|---|---|\n| 1 | 2 |\n"
        blocks = split_into_blocks(md)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "table")
        self.assertEqual(blocks[0].table.rows[0], ("1", "2"))

    def test_mixed_preserves_order(self):
        md = (
            "## 报告\n"
            "Some intro text.\n"
            "\n"
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |\n"
            "\n"
            "End text.\n"
        )
        blocks = split_into_blocks(md)
        kinds = [b.kind for b in blocks]
        self.assertEqual(kinds, ["text", "table", "text"])
        # text 1 has 报告 + intro
        self.assertIn("报告", blocks[0].text)
        # text 2 has end
        self.assertIn("End text", blocks[2].text)

    def test_two_tables_with_text(self):
        md = (
            "| A | B |\n|---|---|\n| 1 | 2 |\n"
            "\n"
            "middle\n"
            "\n"
            "| C | D |\n|---|---|\n| 3 | 4 |\n"
        )
        blocks = split_into_blocks(md)
        self.assertEqual([b.kind for b in blocks], ["table", "text", "table"])
        self.assertEqual(blocks[1].text.strip(), "middle")

    def test_blank_line_inside_table(self):
        md = (
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |\n"
            "\n"          # 空行：peek 下一行还是 | 起头，纳入
            "| 3 | 4 |\n"
        )
        blocks = split_into_blocks(md)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "table")
        self.assertEqual(len(blocks[0].table.rows), 2)


class TestHasMarkdownTable(unittest.TestCase):
    def test_positive(self):
        self.assertTrue(has_markdown_table("| x | y |\n|---|---|\n| a | b |"))
    def test_negative(self):
        self.assertFalse(has_markdown_table("just text"))
        # 注意：单列表也是合法 GFM 表格，has_markdown_table 应返回 True
        self.assertTrue(has_markdown_table("| x |\n|---|\n| a |\n"))


class TestBuildCard(unittest.TestCase):
    def test_pure_table_with_header(self):
        md = "| Name | Score |\n|------|-------|\n| Alice | 98 |\n| Bob | 85 |\n"
        blocks = split_into_blocks(md)
        card = build_card(blocks, CardConfig(header_title="Scoreboard"))
        self.assertEqual(card["schema"], "2.0")
        self.assertIn("body", card)
        self.assertIn("header", card)
        self.assertEqual(card["header"]["title"]["content"], "Scoreboard")
        elements = card["body"]["elements"]
        self.assertEqual(len(elements), 1)
        el = elements[0]
        self.assertEqual(el["tag"], "table")
        self.assertEqual(len(el["columns"]), 2)
        self.assertEqual(len(el["rows"]), 2)
        # row dict: {col_name: cell}
        row0 = el["rows"][0]
        self.assertEqual(row0["Name"], "Alice")
        self.assertEqual(row0["Score"], "98")

    def test_mixed_blocks_order(self):
        md = (
            "## 报告\n"
            "intro\n"
            "\n"
            "| A | B |\n|---|---|\n| 1 | 2 |\n"
            "\n"
            "end\n"
        )
        blocks = split_into_blocks(md)
        card = build_card(blocks)
        tags = [el["tag"] for el in card["body"]["elements"]]
        self.assertEqual(tags, ["markdown", "table", "markdown"])

    def test_data_type_default(self):
        md = "| a | b |\n|---|---|\n| 1 | 2 |\n"
        blocks = split_into_blocks(md)
        card = build_card(blocks, CardConfig(cell_data_type="markdown"))
        col = card["body"]["elements"][0]["columns"][0]
        self.assertEqual(col["data_type"], "markdown")

    def test_payload_is_valid_json(self):
        md = "| a | b |\n|---|---|\n| 1 | 2 |\n"
        blocks = split_into_blocks(md)
        payload = build_card_payload(blocks, CardConfig(header_title="t"))
        # 必须是合法 JSON
        parsed = json.loads(payload)
        self.assertEqual(parsed["schema"], "2.0")

    def test_empty_fallback(self):
        # 空 blocks -> 至少一个元素
        card = build_card([])
        self.assertEqual(len(card["body"]["elements"]), 1)


class TestRegressions(unittest.TestCase):
    def test_unicode_in_cells(self):
        md = (
            "| 姓名 | 分数 |\n"
            "|------|------|\n"
            "| 张三 | 98 |\n"
            "| 李四 | 85 |\n"
        )
        blocks = split_into_blocks(md)
        card = build_card(blocks, CardConfig(header_title="成绩单"))
        # 中文列名被 _sanitize_name 转成 ASCII key；display_name 保留原文
        col0 = card["body"]["elements"][0]["columns"][0]
        self.assertEqual(col0["display_name"], "姓名")
        # 第一个 row 的第一个 cell 应该是 "张三"
        rows = card["body"]["elements"][0]["rows"]
        self.assertEqual(rows[0][col0["name"]], "张三")

    def test_pipes_in_text_block_dont_split(self):
        # text block 里如果出现 | 字符（不是表格），不能被误判
        md = "Use `a | b` for bitwise or.\n"
        self.assertFalse(has_markdown_table(md))
        blocks = split_into_blocks(md)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "text")


class TestTableLimit(unittest.TestCase):
    """飞书 CardKit v2 单卡 table 组件上限：硬上限 3，插件保守用 2。

    build_card 在 table 数 > max_tables_per_card 时会**降级**为 markdown
    文本（key:value 格式），而不是 silent drop。这是修 Bug 1（2026-06-17
    入站死锁）后的新行为：宁可降级丢一些列宽对齐，也不能让飞书 API 拒收
    导致 SDK Event loop 关闭。
    """

    @staticmethod
    def _make_table(name: str) -> str:
        """生成一个 2 列 1 行的 markdown 表格（带前置文本分隔）。"""
        return (
            f"### {name}\n\n"
            f"| 名称 | 值 |\n"
            f"|------|----|\n"
            f"| {name} | 100 |"
        )

    def test_six_tables_overflow_gets_downgraded(self):
        """6 个表格时,前 2 个走原生 table,后 4 个降级为 markdown key:value。

        这是新行为（旧行为:6 个全塞,飞书拒收,SDK Event loop 关闭）。
        配套 feishu_send_card 走 split_blocks_by_table_groups 切分路径,
        上游调用方不应该再直接传 6 个表给 build_card。
        """
        md = "\n\n".join(
            self._make_table(f"t{i}") for i in range(6)
        )
        blocks = split_into_blocks(md)
        table_blocks = [b for b in blocks if b.kind == "table"]
        self.assertEqual(len(table_blocks), 6)
        card = build_card(blocks)  # 默认 max_tables_per_card=2
        elements = card["body"]["elements"]
        # 前 2 个是 table,后 4 个是 markdown(key:value 格式)
        table_elements = [e for e in elements if e["tag"] == "table"]
        self.assertEqual(len(table_elements), 2, "前 2 个表应该走原生 table")
        # 降级块:含 "名称:" 字样的是 key:value 降级,带 ### 的是标题文本块
        downgrade_blocks = [
            e for e in elements
            if e["tag"] == "markdown" and "名称:" in e.get("content", "")
        ]
        self.assertEqual(len(downgrade_blocks), 4, "后 4 个表应该降级为 markdown key:value")
        # 降级的内容应该含原始数据
        all_md_text = "\n".join(e["content"] for e in downgrade_blocks)
        for i in range(2, 6):
            self.assertIn(f"t{i}", all_md_text, f"降级内容应保留 t{i} 数据")

    def test_three_tables_one_native_two_downgraded(self):
        """3 个表格:第 1、2 个走 table,第 3 个降级。"""
        md = "\n\n".join(
            self._make_table(f"t{i}") for i in range(3)
        )
        blocks = split_into_blocks(md)
        card = build_card(blocks)
        elements = card["body"]["elements"]
        table_elements = [e for e in elements if e["tag"] == "table"]
        downgrade_blocks = [
            e for e in elements
            if e["tag"] == "markdown" and "名称:" in e.get("content", "")
        ]
        self.assertEqual(len(table_elements), 2)
        self.assertEqual(len(downgrade_blocks), 1)

    def test_two_tables_all_native(self):
        """2 个表格(等于上限),全部原生渲染,无降级。"""
        md = "\n\n".join(
            self._make_table(f"t{i}") for i in range(2)
        )
        blocks = split_into_blocks(md)
        card = build_card(blocks)
        table_elements = [e for e in card["body"]["elements"] if e["tag"] == "table"]
        self.assertEqual(len(table_elements), 2)
        md_elements = [e for e in card["body"]["elements"] if e["tag"] == "markdown" and e.get("content")]
        # 可能有 ### 标题的 markdown 块,但没有降级块
        downgrade_blocks = [
            e for e in card["body"]["elements"]
            if e["tag"] == "markdown" and "名称:" in e.get("content", "")
        ]
        self.assertEqual(len(downgrade_blocks), 0, "2 个表不应该触发降级")

    def test_custom_max_tables_per_card(self):
        """CardConfig.max_tables_per_card 调成 5 时,5 个表全走原生。"""
        md = "\n\n".join(
            self._make_table(f"t{i}") for i in range(5)
        )
        blocks = split_into_blocks(md)
        card = build_card(blocks, CardConfig(max_tables_per_card=5))
        table_elements = [e for e in card["body"]["elements"] if e["tag"] == "table"]
        self.assertEqual(len(table_elements), 5)

    def test_page_size_clamped_to_10(self):
        """page_size 不能超过飞书限制的 10。"""
        md = "| 名称 | 值 |\n|------|----|\n| test | 100 |\n"
        blocks = split_into_blocks(md)
        card = build_card(blocks, CardConfig(page_size=99))
        table_el = [e for e in card["body"]["elements"] if e["tag"] == "table"][0]
        self.assertEqual(table_el["page_size"], 10)


if __name__ == "__main__":
    unittest.main()
