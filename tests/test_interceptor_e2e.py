"""E2E 集成测试 — 真实打 patch，模拟 Hermes 飞书 send 流程。

目标：把 ``FeishuPlatform._build_outbound_payload`` 替换后，传入一段含
markdown 表格的内容，验证返回值是 ``("interactive", <card JSON>)``。

注意：本测试**不**真正连飞书 SDK；它只验证我们的 patch 在
``gateway.platforms.feishu`` 模块的当前实现上能正确改写出站载荷。
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path


# ---- 把 feishu-md-tables 当独立包加载，绕过 hyphen 目录名 ----
ROOT = Path(__file__).resolve().parent.parent
import importlib.util as _ilu
import sys as _sys


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


_plugin_pkg = _load_pkg("feishu_md_tables", ROOT / "__init__.py")
parser = _load_file("feishu_md_tables", ROOT / "parser.py", _plugin_pkg)
card_builder = _load_file(
    "feishu_md_tables", ROOT / "card_builder.py", _plugin_pkg
)


# 加载 hermes 源码。
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


# ---- 真正的核心测试 ----


class TestInterceptor(unittest.TestCase):
    """验证 patch 后 ``_build_outbound_payload`` 行为正确。"""

    def setUp(self):
        # 加载真实的 feishu 模块
        try:
            from gateway.platforms import feishu as feishu_mod
        except Exception as e:
            self.skipTest(f"feishu module not importable in this env: {e}")
            return

        self.feishu_mod = feishu_mod
        # 实际类名是 FeishuAdapter（不是 FeishuPlatform；后者是策划中的别名）
        self.cls = getattr(feishu_mod, "FeishuAdapter", None) or getattr(
            feishu_mod, "FeishuPlatform", None
        )
        if self.cls is None:
            self.skipTest("FeishuAdapter class not found on feishu module")
            return
        # 记住原始方法
        self._orig_build = self.cls._build_outbound_payload
        # 加载拦截器（不调 install，避免污染全局）——直接绑到 Stub 上模拟
        from feishu_md_tables.interceptor import (
            _build_outbound_payload_with_card,
        )
        from feishu_md_tables.card_builder import CardConfig

        class _Stub:
            pass

        self._stub = _Stub()
        cfg = CardConfig()
        # _ORIGINAL_BUILD_OUTBOUND 是未绑定函数，调用时需传 self
        self._patched_fn = lambda content: _build_outbound_payload_with_card(
            self._orig_build, self._stub, content, cfg
        )

    def tearDown(self):
        if hasattr(self, "cls") and hasattr(self, "_orig_build"):
            self.cls._build_outbound_payload = self._orig_build

    def test_plain_text_passthrough(self):
        """不含表格的普通文本——patch 不应改写。"""
        result = self._patched_fn("hello world")
        msg_type, payload = result
        # 原始方法对纯文本返回 ("text", "{\"text\": \"hello world\"}")
        self.assertEqual(msg_type, "text")
        self.assertIn("hello world", payload)

    def test_markdown_no_table_passthrough(self):
        """含 markdown 但无表格——patch 不应改写（保留原 post 路径）。"""
        result = self._patched_fn("## 标题\n**粗体**\n- 列表")
        msg_type, payload = result
        # 原方法有 markdown hint 时返回 ("post", "<zh_cn post>")
        self.assertEqual(msg_type, "post")
        body = json.loads(payload)
        self.assertIn("zh_cn", body)

    def test_table_only_message(self):
        """纯表格消息——patch 应改写为 interactive card。"""
        content = (
            "| Name | Score |\n"
            "|------|-------|\n"
            "| Alice | 98 |\n"
            "| Bob | 85 |\n"
        )
        msg_type, payload = self._patched_fn(content)
        self.assertEqual(msg_type, "interactive")
        card = json.loads(payload)
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(len(card["body"]["elements"]), 1)
        el = card["body"]["elements"][0]
        self.assertEqual(el["tag"], "table")
        self.assertEqual(len(el["columns"]), 2)
        self.assertEqual(len(el["rows"]), 2)
        self.assertEqual(el["rows"][0]["Name"], "Alice")

    def test_mixed_text_and_table(self):
        """text + table 混合——patch 改写为 card，blocks 按文档序。"""
        content = (
            "## 报告\n"
            "intro paragraph\n"
            "\n"
            "| A | B |\n|---|---|\n| 1 | 2 |\n"
            "\n"
            "end paragraph\n"
        )
        msg_type, payload = self._patched_fn(content)
        self.assertEqual(msg_type, "interactive")
        card = json.loads(payload)
        tags = [el["tag"] for el in card["body"]["elements"]]
        self.assertEqual(tags, ["markdown", "table", "markdown"])
        # text 段保留
        self.assertIn("报告", card["body"]["elements"][0]["content"])
        self.assertIn("end", card["body"]["elements"][2]["content"])

    def test_fallback_on_parse_error(self):
        """如果解析/构造阶段抛错，应回退到原方法（不破坏功能）。"""
        # 制造一个边角：含 markdown hint 但同时含表格，原方法会降级为 text
        # 我们想验证：patch 后还是 text（因为 patch 内部捕获异常并回退）
        # 这里我们用 monkeypatch 让 build_card 抛错
        import feishu_md_tables.interceptor as interceptor_mod

        original_build_card = interceptor_mod.build_card
        try:
            interceptor_mod.build_card = lambda *a, **kw: (_ for _ in ()).throw(
                RuntimeError("forced failure")
            )
            # 现在 patch 内部捕获到异常，应该调 original_build(content) → 返回 ("text", ...)
            content = (
                "| Name | Score |\n"
                "|------|-------|\n"
                "| Alice | 98 |\n"
            )
            result = self._patched_fn(content)
            msg_type, payload = result
            # 原方法对含表格的内容会强制 text
            self.assertEqual(msg_type, "text")
        finally:
            interceptor_mod.build_card = original_build_card

    def test_six_tables_through_patched_method(self):
        """6 个表格通过 patched _build_outbound_payload —— 单次调用只产单卡,
        但 build_card 防御性降级避免飞书 ErrCode 11310 拒收。

        新行为（2026-06-17 Bug fix）：
        - ``_build_outbound_payload`` 单次调用仍然只产单卡（一个 payload）
        - ``build_card`` 内部对 >max_tables_per_card 的 table 降级为 markdown
        - 多卡拆分由 ``_patched_send`` 在 send() 层负责,不在本层

        真正的多卡路径见 test_multi_card_send.py::test_six_tables_three_cards。
        """
        from feishu_md_tables.interceptor import install, uninstall, CardConfig

        tables = []
        for i in range(6):
            tables.append(
                f"### Table {i}\n\n| Col | Value |\n|-----|-------|\n| item | {i} |"
            )
        content = "\n\n".join(tables)

        try:
            ok = install(CardConfig())
            self.assertTrue(ok)
            result = self.cls._build_outbound_payload(self._stub, content)
            msg_type, payload = result
            self.assertEqual(msg_type, "interactive")
            card = json.loads(payload)
            table_els = [e for e in card["body"]["elements"] if e["tag"] == "table"]
            # 默认 max_tables_per_card=2,所以单卡里 2 个原生 table + 4 个降级 markdown
            self.assertEqual(len(table_els), 2, "Default max_tables_per_card=2 → 2 native tables")
            # 剩下的 4 个降级为 markdown key:value 块(防御飞书 ErrCode 11310)
            downgrade_els = [
                e for e in card["body"]["elements"]
                if e["tag"] == "markdown" and "Col:" in e.get("content", "")
            ]
            self.assertEqual(len(downgrade_els), 4, "剩下的 4 个表降级为 markdown")
        finally:
            uninstall()

    def test_install_uninstall(self):
        """install() 真的替换了类方法，uninstall() 还原。"""
        from feishu_md_tables.interceptor import install, uninstall, CardConfig

        try:
            ok = install(CardConfig())
            self.assertTrue(ok)
            # 类方法已经被替换
            self.assertNotEqual(
                self.cls._build_outbound_payload, self._orig_build
            )
            # 调一次验证——需要 self，所以用 Stub 实例
            result = self.cls._build_outbound_payload(
                self._stub, "| a | b |\n|---|---|\n| 1 | 2 |\n"
            )
            self.assertEqual(result[0], "interactive")
        finally:
            uninstall()
        # 还原后
        self.assertEqual(
            self.cls._build_outbound_payload, self._orig_build
        )


if __name__ == "__main__":
    unittest.main()
