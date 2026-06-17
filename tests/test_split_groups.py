"""测试 ``parser.split_blocks_by_table_groups`` —— 多卡切分的核心。

新行为（multi-card split）：
  * send() 层检测到 > 2 个表格时，调用本函数把 blocks 切成 N 组
  * 每组 ≤ max_tables_per_card 个 table（默认 2）
  * 每组单独走原始 send() → 变成多张独立飞书卡片
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
import importlib.util as _ilu


def _load_pkg(pkg_name, init_path):
    spec = _ilu.spec_from_file_location(
        pkg_name, init_path, submodule_search_locations=[str(init_path.parent)]
    )
    mod = _ilu.module_from_spec(spec)
    _sys.modules[pkg_name] = mod
    return mod


def _load_file(pkg_name, file_path, parent_pkg):
    full = f"{pkg_name}.{file_path.stem}"
    spec = _ilu.spec_from_file_location(full, file_path)
    mod = _ilu.module_from_spec(spec)
    _sys.modules[full] = mod
    spec.loader.exec_module(mod)
    parent_pkg.__dict__[file_path.stem] = mod
    return mod


import sys as _sys
_plugin_pkg = _load_pkg("feishu_md_tables_split", ROOT / "__init__.py")
parser = _load_file("feishu_md_tables_split", ROOT / "parser.py", _plugin_pkg)

split_blocks_by_table_groups = parser.split_blocks_by_table_groups
Block = parser.Block
MarkdownTable = parser.MarkdownTable
TableColumn = parser.TableColumn


def _make_table(name: str) -> Block:
    """造一个最小的 table block（不依赖 parse_markdown_table）。"""
    col = TableColumn(name="col_0", display_name=name, align="left")
    md = MarkdownTable(
        columns=(col,),
        rows=(("v1",), ("v2",)),
        raw=f"| {name} |\n|---|\n| v1 |\n| v2 |",
    )
    return Block(kind="table", table=md)


def _make_text(text: str) -> Block:
    return Block(kind="text", text=text)


class TestSplitBlocksByTableGroups(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(split_blocks_by_table_groups([]), [])

    def test_invalid_max(self):
        with self.assertRaises(ValueError):
            split_blocks_by_table_groups([_make_text("x")], max_tables_per_card=0)

    def test_no_tables_text_only(self):
        """纯文本: 不切,单组返回."""
        blocks = [_make_text("a"), _make_text("b")]
        groups = split_blocks_by_table_groups(blocks, max_tables_per_card=2)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_two_tables_single_group(self):
        """2 个表格 = 单卡 (因为默认 max=2)."""
        blocks = [_make_table("t1"), _make_table("t2")]
        groups = split_blocks_by_table_groups(blocks, max_tables_per_card=2)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_three_tables_two_groups(self):
        """3 个表格 → 2 组 [t1,t2] + [t3]."""
        blocks = [_make_table("t1"), _make_table("t2"), _make_table("t3")]
        groups = split_blocks_by_table_groups(blocks, max_tables_per_card=2)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[0]), 2)
        self.assertEqual(len(groups[1]), 1)

    def test_six_tables_three_groups(self):
        """6 个表格 → 3 组 (用户原始需求)."""
        blocks = [_make_table(f"t{i}") for i in range(6)]
        groups = split_blocks_by_table_groups(blocks, max_tables_per_card=2)
        self.assertEqual(len(groups), 3)
        for g in groups:
            table_in_g = sum(1 for b in g if b.kind == "table")
            self.assertLessEqual(table_in_g, 2)

    def test_text_attaches_to_table(self):
        """text block 跟最近的 table 同组 —— 不跨组前瞻。

        具体行为: text block 出现在 current group 就属于当前 group;
        即使它后面有更多 table 触发开新组,这个 text 也不会被"拖"到新组。
        """
        blocks = [
            _make_text("intro"),
            _make_table("t1"),
            _make_table("t2"),
            _make_text("between"),
            _make_table("t3"),
        ]
        groups = split_blocks_by_table_groups(blocks, max_tables_per_card=2)
        # 算法: intro → current; t1, t2 → current (满了, count=2);
        # between 是 text → 追加到当前(current=[intro,t1,t2,between]);
        # t3 触发开新组 → current=t3
        self.assertEqual(len(groups), 2)
        # 第一组: [intro, t1, t2, between] (text 属于它所在的位置)
        self.assertEqual(len(groups[0]), 4)
        # 第二组: [t3]
        self.assertEqual(len(groups[1]), 1)

    def test_text_before_first_table(self):
        """表格前的 intro text 应该跟第一张表同组."""
        blocks = [
            _make_text("这是导言"),
            _make_table("t1"),
            _make_table("t2"),
            _make_table("t3"),
        ]
        groups = split_blocks_by_table_groups(blocks, max_tables_per_card=2)
        # group[0] = [导言, t1, t2]; group[1] = [t3]
        self.assertEqual(len(groups), 2)
        self.assertIn("导言", groups[0][0].text)

    def test_max_1_each_card_one_table(self):
        """max_tables_per_card=1: 每个表格单独成卡."""
        blocks = [_make_table(f"t{i}") for i in range(4)]
        groups = split_blocks_by_table_groups(blocks, max_tables_per_card=1)
        self.assertEqual(len(groups), 4)

    def test_max_5_five_tables_one_card(self):
        """max_tables_per_card=5: 5 个表格刚好 1 张卡."""
        blocks = [_make_table(f"t{i}") for i in range(5)]
        groups = split_blocks_by_table_groups(blocks, max_tables_per_card=5)
        self.assertEqual(len(groups), 1)

    def test_preserves_document_order(self):
        """切分后, 每个 group 内部和 group 之间都保持原文档序."""
        blocks = [
            _make_text("a"),
            _make_table("t1"),
            _make_text("b"),
            _make_table("t2"),
            _make_text("c"),
            _make_table("t3"),
            _make_text("d"),
        ]
        groups = split_blocks_by_table_groups(blocks, max_tables_per_card=2)
        # 顺序应该保持
        all_texts = [
            blk.text for g in groups for blk in g if blk.kind == "text"
        ]
        self.assertEqual(all_texts, ["a", "b", "c", "d"])


if __name__ == "__main__":
    unittest.main()
