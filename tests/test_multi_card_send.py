"""测试 _patched_send 在多表格场景下的多卡循环行为。

策略：用 stub 实例替换 FeishuAdapter，验证：
  1. 单表 / 双表 → original_send 只被调用 1 次（零额外开销）
  2. ≥3 表 → original_send 被调用 ceil(N/2) 次（默认 max=2）
  3. reply_to 只传给第一张卡
  4. 每张 sub_content 都带有「(N/M)」序号头
  5. 一张卡失败 → 立即中断后续，返回失败响应
  6. 无表格 → 走原 send（行为完全不变）
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


ROOT = Path(__file__).resolve().parent.parent

import importlib.util as _ilu


def _load_pkg(pkg_name, init_path):
    spec = _ilu.spec_from_file_location(
        pkg_name, init_path, submodule_search_locations=[str(init_path.parent)]
    )
    mod = _ilu.module_from_spec(spec)
    sys.modules[pkg_name] = mod
    return mod


def _load_file(pkg_name, file_path, parent_pkg):
    full = f"{pkg_name}.{file_path.stem}"
    spec = _ilu.spec_from_file_location(full, file_path)
    mod = _ilu.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    parent_pkg.__dict__[file_path.stem] = mod
    return mod


_plugin_pkg = _load_pkg("feishu_md_tables_send", ROOT / "__init__.py")
parser_mod = _load_file("feishu_md_tables_send", ROOT / "parser.py", _plugin_pkg)
card_builder_mod = _load_file(
    "feishu_md_tables_send", ROOT / "card_builder.py", _plugin_pkg
)
interceptor_mod = _load_file(
    "feishu_md_tables_send", ROOT / "interceptor.py", _plugin_pkg
)

# 加载 hermes 源码（提供 FeishuAdapter 类）。
# Resolve hermes-agent the same way hermes itself does: read HERMES_HOME
# env var (the documented override), fall back to ~/.hermes, then assume
# the agent checkout lives at <hermes-home>/hermes-agent. This keeps the
# test suite runnable on any contributor's machine instead of hardcoding
# /home/ubuntu/.hermes/hermes-agent.
_HERMES_HOME = Path(
    os.environ.get("HERMES_HOME", "").strip() or str(Path.home() / ".hermes")
)
HERMES = _HERMES_HOME / "hermes-agent"
sys.path.insert(0, str(HERMES))


def _make_table_md(name: str, value: str = "v") -> str:
    """构造一个合法的 markdown 表格。"""
    return f"| {name} | Value |\n|---|---|\n| {value} | 100 |\n"


class _FakeResponse:
    def __init__(self, success=True, error=None):
        self.success = success
        self.error = error


class TestPatchedSend(unittest.TestCase):
    """_patched_send 的核心行为单测 — 不真正打 patch 到 FeishuAdapter,
    直接调用函数,传 stub self + AsyncMock original_send。"""

    def setUp(self):
        self.stub = MagicMock()
        self.calls = []  # 记录 (content, reply_to) 调用

        async def fake_original_send(self, chat_id, content, reply_to=None, metadata=None):
            self.calls.append({"content": content, "reply_to": reply_to})
            return _FakeResponse(success=True)

        self.fake_original_send = fake_original_send

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _send(self, content, *, reply_to=None, max_tables_per_card=2):
        """调用 _patched_send 并返回 (result, calls).

        注意: _patched_send 的 self 参数必须是 TestPatchedSend 实例本身,
        因为 fake_original_send 闭包捕获 self.calls。如果传 MagicMock,
        append 会写到错的地方。
        """
        from feishu_md_tables_send.card_builder import CardConfig

        cfg = CardConfig()
        # 重置 calls
        self.calls.clear()
        result = self._run(
            interceptor_mod._patched_send(
                self,   # 用 self 代替 self.stub
                self.fake_original_send,
                chat_id="chat_123",
                content=content,
                cfg=cfg,
                max_tables_per_card=max_tables_per_card,
                reply_to=reply_to,
                metadata=None,
            )
        )
        return result, self.calls.copy()

    # ----- 测试：无表格 → 走原 send，零改动 -----

    def test_no_table_passthrough(self):
        """纯文本：不切分，original_send 调用 1 次，content 不变."""
        result, calls = self._send("hello world")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["content"], "hello world")
        self.assertTrue(result.success)

    # ----- 测试：1-2 表 → 单卡（原行为不变） -----

    def test_one_table_single_card(self):
        content = _make_table_md("A")
        result, calls = self._send(content)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result.success)

    def test_two_tables_single_card(self):
        content = _make_table_md("A") + "\n\n" + _make_table_md("B")
        result, calls = self._send(content)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result.success)

    # ----- 测试：≥3 表 → 拆多卡（核心新行为） -----

    def test_three_tables_two_cards(self):
        content = (
            _make_table_md("t0")
            + "\n\n"
            + _make_table_md("t1")
            + "\n\n"
            + _make_table_md("t2")
        )
        result, calls = self._send(content, max_tables_per_card=2)
        # 3 表 → 2 卡（2+1）
        self.assertEqual(len(calls), 2)
        # 第一张卡带 (1/2), 第二张带 (2/2)  （实际是全角中文括号，与 md 渲染兼容）
        self.assertIn("（1/2）", calls[0]["content"])
        self.assertIn("（2/2）", calls[1]["content"])
        # 每张卡的 markdown 内容里都应含有原始表格名
        self.assertIn("t0", calls[0]["content"])
        self.assertIn("t1", calls[0]["content"])
        self.assertIn("t2", calls[1]["content"])

    def test_six_tables_three_cards(self):
        """用户原始需求: 6 个表格 → 3 张卡 (2/2/2)."""
        content = "\n\n".join(_make_table_md(f"t{i}") for i in range(6))
        result, calls = self._send(content, max_tables_per_card=2)
        self.assertEqual(len(calls), 3)
        # 序号头
        self.assertIn("（1/3）", calls[0]["content"])
        self.assertIn("（2/3）", calls[1]["content"])
        self.assertIn("（3/3）", calls[2]["content"])
        # 每张卡 2 个表名
        for idx, expected in [(0, ["t0", "t1"]), (1, ["t2", "t3"]), (2, ["t4", "t5"])]:
            for name in expected:
                self.assertIn(name, calls[idx]["content"], f"card {idx+1} should contain {name}")

    # ----- 测试：reply_to 只贴在第一张卡 -----

    def test_reply_to_only_first_card(self):
        content = "\n\n".join(_make_table_md(f"t{i}") for i in range(4))
        result, calls = self._send(content, reply_to="orig_msg_id", max_tables_per_card=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["reply_to"], "orig_msg_id")
        self.assertIsNone(calls[1]["reply_to"], "Only first card should carry reply_to")

    # ----- 测试：1 张卡失败 → 中断后续 -----

    def test_failure_aborts_remaining(self):
        content = "\n\n".join(_make_table_md(f"t{i}") for i in range(6))
        call_count = {"n": 0}

        async def flaky_send(self, chat_id, content, reply_to=None, metadata=None):
            call_count["n"] += 1
            # 第二张卡失败
            if call_count["n"] == 2:
                return _FakeResponse(success=False, error="API timeout")
            return _FakeResponse(success=True)

        from feishu_md_tables_send.card_builder import CardConfig

        result = self._run(
            interceptor_mod._patched_send(
                self,   # TestCase instance (consumed by _patched_send as 'self')
                flaky_send,
                chat_id="chat_x",
                content=content,
                cfg=CardConfig(),
                max_tables_per_card=2,
                reply_to=None,
                metadata=None,
            )
        )
        # 失败后立即停止, 不会继续发第 3 张
        self.assertEqual(call_count["n"], 2)
        self.assertFalse(result.success)
        self.assertEqual(result.error, "API timeout")

    # ----- 测试：自定义 max_tables_per_card -----

    def test_custom_max_1(self):
        """max_tables_per_card=1 → 每张卡只有 1 表."""
        content = "\n\n".join(_make_table_md(f"t{i}") for i in range(3))
        result, calls = self._send(content, max_tables_per_card=1)
        self.assertEqual(len(calls), 3)
        for idx, expected in enumerate(["t0", "t1", "t2"]):
            self.assertIn(expected, calls[idx]["content"])
            # 每张卡只有 1 张表 → 没有相邻表的表名
            for other in ["t0", "t1", "t2"]:
                if other != expected:
                    self.assertNotIn(other, calls[idx]["content"])

    def test_custom_max_5(self):
        """max_tables_per_card=5 → 6 表拆 2 卡 (5+1)."""
        content = "\n\n".join(_make_table_md(f"t{i}") for i in range(6))
        result, calls = self._send(content, max_tables_per_card=5)
        self.assertEqual(len(calls), 2)
        self.assertIn("（1/2）", calls[0]["content"])
        self.assertIn("（2/2）", calls[1]["content"])


class TestBlocksToMarkdown(unittest.TestCase):
    """验证 _blocks_to_markdown 的格式还原."""

    def test_roundtrip(self):
        content = _make_table_md("MyTable") + "\n\n" + "这是说明文字"
        blocks = parser_mod.split_into_blocks(content)
        md = interceptor_mod._blocks_to_markdown(blocks)
        # 应该含原表格的 raw markdown 和原文字
        self.assertIn("MyTable", md)
        self.assertIn("| Value |", md)
        self.assertIn("这是说明文字", md)


if __name__ == "__main__":
    unittest.main()
