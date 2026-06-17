"""Feishu 平台 ``send`` 方法的 monkey-patch 拦截器。

Hermes 飞书适配器（``gateway/platforms/feishu.py``）目前对包含 markdown
表格的出站消息会**强制降级为纯文本**::

    if _MARKDOWN_TABLE_RE.search(content):
        text_payload = {"text": content}
        return "text", json.dumps(text_payload, ensure_ascii=False)

这是因为 ``post`` 类型的 ``md`` 元素无法渲染表格，详见 feishu.py line 4376 的注释。

本拦截器把这段逻辑**升级**为 CardKit v2 ``interactive`` 消息：
检测到表格时把消息切成 ``Block(text|table)`` 序列，拼成 CardKit
``schema: 2.0`` 卡片，再走原本的 ``msg_type="interactive"`` 通道。

设计要点：

* **幂等**：多次调用 ``install()`` 不会叠加 patch；卸载会还原
* **容错**：解析失败时仍走原方法（不破坏任何现有功能）
* **小耦合**：只 import ``feishu`` 模块，不触碰其内部类
* **不阻塞**消息内容审查 / fallback 链——只在出站最外层做替换
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import time
from typing import Any, Dict, List, Optional

from .card_builder import (
    CardConfig,
    build_card,
    build_card_payload,
    build_plain_text_fallback,
)
from .parser import (
    has_markdown_table,
    split_blocks_by_table_groups,
    split_into_blocks,
)


logger = logging.getLogger("hermes.plugins.feishu_md_tables")

# 状态：是否已 patch（用闭包变量比 class 状态更简单）
_INSTALLED = False
_ORIGINAL_SEND = None
_ORIGINAL_BUILD_OUTBOUND = None
# 默认每张卡片最多放几个 table（飞书单卡 30KB 限制下，2 比较安全）。
# 用户可通过 CardConfig.max_tables_per_card 覆盖。
_DEFAULT_MAX_TABLES_PER_CARD = 2


def _group_to_payload(blocks_for_one_card: List, cfg: CardConfig) -> Dict[str, Any]:
    """一组 blocks → CardKit v2 card dict。

    单卡内 table 数已经被切分阶段保证 ≤ ``max_tables_per_card``，
    这里直接 ``build_card`` 即可，不需要再做 5 表截断。
    """
    return build_card(blocks_for_one_card, cfg)


def _build_outbound_payload_with_card(
    original_func, self, content: str, cfg: CardConfig
):
    """新版 ``_build_outbound_payload`` 的核心逻辑。

    仍然保留这个函数是为了向后兼容：旧调用方（比如单元测试）直接调
    ``_build_outbound_payload`` 时仍能拿到「≤ 2 表/卡」的合理行为。
    但实际的多卡发送逻辑在 ``send()`` 层（``_patched_send``）完成。

    行为：
      * 无表格 → 走原方法
      * 有 1-2 个表格 → 单卡 interactive，正常走原方法返回值
      * 有 ≥3 个表格 → 把所有 table 塞进单卡（会被 build_card 兜底警告），
        因为单次 _build_outbound_payload 调用只能返回一个 (msg_type, payload)。
        真正的多卡拆分在 send() 层。
    """
    if not has_markdown_table(content):
        return original_func(self, content)

    try:
        blocks = split_into_blocks(content)
    except Exception as exc:
        logger.warning("[feishu-md-tables] split_into_blocks failed: %s", exc)
        return original_func(self, content)

    has_table = any(b.kind == "table" for b in blocks)
    if not has_table:
        return original_func(self, content)

    try:
        card = build_card(blocks, cfg)
        payload = json.dumps(card, ensure_ascii=False)
        return "interactive", payload
    except Exception as exc:
        logger.warning(
            "[feishu-md-tables] build_card failed; falling back to original: %s",
            exc,
        )
        return original_func(self, content)


async def _patched_send(
    self,
    original_send,
    chat_id: str,
    content: str,
    cfg: CardConfig,
    max_tables_per_card: int,
    reply_to: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    """替换后的 ``send`` —— 多表自动拆多卡。

    流程：
      1. 快速判断是否有 markdown 表格（``has_markdown_table``）
         没有就走原 send（零开销）
      2. 解析成 blocks；table 数 ≤ max_tables_per_card 仍走原 send
      3. 超过则按 ``split_blocks_by_table_groups`` 切分，每组单独走原 send
         （绕过 ``_build_outbound_payload`` 的 monkey-patch，避免双重切分）

    reply_to 处理：只有第一张卡片带 reply_to，让飞书客户端把"回复 X"锚到
    多卡序列的首张上。后续卡片不传 reply_to，避免出现 N 个"回复 X"。
    """
    if not has_markdown_table(content):
        return await original_send(
            self,
            chat_id=chat_id,
            content=content,
            reply_to=reply_to,
            metadata=metadata,
        )

    # 解析 blocks（异常 = 走原 send）
    try:
        blocks = split_into_blocks(content)
    except Exception as exc:
        logger.warning(
            "[feishu-md-tables] split_into_blocks failed in send(): %s; "
            "falling back to original send", exc,
        )
        return await original_send(
            self,
            chat_id=chat_id,
            content=content,
            reply_to=reply_to,
            metadata=metadata,
        )

    table_count = sum(1 for b in blocks if b.kind == "table")
    if table_count <= max_tables_per_card:
        # 1 张卡就能装下 → 直接走原 send（单卡行为完全不变）
        return await original_send(
            self,
            chat_id=chat_id,
            content=content,
            reply_to=reply_to,
            metadata=metadata,
        )

    # 多卡路径：切分 + 循环发送
    groups = split_blocks_by_table_groups(blocks, max_tables_per_card=max_tables_per_card)
    if not groups:
        return await original_send(
            self,
            chat_id=chat_id,
            content=content,
            reply_to=reply_to,
            metadata=metadata,
        )

    logger.info(
        "[feishu-md-tables] splitting %d tables into %d cards (max %d/card)",
        table_count, len(groups), max_tables_per_card,
    )

    last_response = None
    for idx, group in enumerate(groups):
        is_first = idx == 0
        is_last = idx == len(groups) - 1
        # 把一组 blocks 拼回 markdown 字符串，再让原始 send() 处理
        # （原始 send() 自己会跑 truncate_message / build_outbound_payload，
        # 我们绕开 _build_outbound_payload 的 monkey-patch 二次切分 —— 见 _install 注释）
        sub_content = _blocks_to_markdown(group)
        sub_reply_to = reply_to if is_first else None

        # 注入「(N/M)」头标识，让用户在飞书客户端能看出这是多卡中的第几张
        if len(groups) > 1:
            sub_content = f"_（{idx + 1}/{len(groups)}）_\n\n" + sub_content

        response = await original_send(
            self,
            chat_id=chat_id,
            content=sub_content,
            reply_to=sub_reply_to,
            metadata=metadata,
        )
        last_response = response
        if not getattr(response, "success", False):
            logger.warning(
                "[feishu-md-tables] card %d/%d failed: %s; aborting remaining",
                idx + 1, len(groups), getattr(response, "error", "?"),
            )
            return response

    return last_response


def _blocks_to_markdown(blocks: List) -> str:
    """一组 blocks → markdown 字符串（让原始 send() 自己处理后续）。

    注意：split_into_blocks 在两个表格之间会插入空 text block（因为原 md
    里表格间有空行）。这里把它们过滤掉，避免拼出多余空行。
    """
    parts: List[str] = []
    for blk in blocks:
        if blk.kind == "table" and blk.table is not None:
            parts.append(blk.table.raw)
        elif blk.kind == "text" and blk.text.strip():
            parts.append(blk.text)
    return "\n\n".join(parts)


def _install_on_feishu_module(cfg: CardConfig) -> bool:
    """对 ``gateway.platforms.feishu`` 模块做 monkey-patch。

    Returns True on success, False if module isn't importable yet.
    """
    global _INSTALLED, _ORIGINAL_SEND, _ORIGINAL_BUILD_OUTBOUND
    if _INSTALLED:
        return True

    try:
        from gateway.platforms import feishu as feishu_mod
    except Exception as exc:
        # gateway.platforms.feishu 不一定能 import（lark_oapi 缺失、profile 隔离等）
        logger.info(
            "[feishu-md-tables] gateway.platforms.feishu not importable: %s", exc
        )
        return False

    # 找飞书平台类（历史上命名不一：FeishuAdapter 是当前实现）
    cls = (
        getattr(feishu_mod, "FeishuAdapter", None)
        or getattr(feishu_mod, "FeishuPlatform", None)
    )
    if cls is None:
        logger.info(
            "[feishu-md-tables] FeishuAdapter/FeishuPlatform class not found on feishu module"
        )
        return False

    # --- 1) patch send() —— 主要的多卡切分逻辑 ---
    if _ORIGINAL_SEND is None:
        _ORIGINAL_SEND = cls.send  # 绑定到实例的方法本体

        async def patched_send(
            self,
            chat_id: str,
            content: str,
            reply_to: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
        ):
            return await _patched_send(
                self,
                _ORIGINAL_SEND,
                chat_id=chat_id,
                content=content,
                cfg=cfg,
                max_tables_per_card=cfg.max_tables_per_card,
                reply_to=reply_to,
                metadata=metadata,
            )

        # functools.wraps 保留签名 / docstring（虽然 Hermes 不 inspect 这个）
        patched_send = functools.wraps(_ORIGINAL_SEND)(patched_send)
        cls.send = patched_send

    # --- 2) 仍然 patch _build_outbound_payload —— 单元测试 / 其它调用方的兜底 ---
    # send() 多卡路径会在 _blocks_to_markdown 后重新走原始 send(),
    # 但原始 send() 内部还会调 _build_outbound_payload（已被 patch）,
    # 会再次走"有表 → interactive"逻辑。这是我们想要的——单组 blocks
    # 仍然以 card 形式发出。
    if _ORIGINAL_BUILD_OUTBOUND is None:
        _ORIGINAL_BUILD_OUTBOUND = cls._build_outbound_payload

        def patched_build_outbound_payload(self, content: str):
            return _build_outbound_payload_with_card(
                _ORIGINAL_BUILD_OUTBOUND, self, content, cfg
            )

        cls._build_outbound_payload = patched_build_outbound_payload

    _INSTALLED = True
    logger.info(
        "[feishu-md-tables] Installed: FeishuAdapter.send + "
        "_build_outbound_payload patched (max_tables_per_card=%d)",
        _DEFAULT_MAX_TABLES_PER_CARD,
    )
    return True


def install(cfg: Optional[CardConfig] = None) -> bool:
    """对外公开的入口：尝试打 patch。

    在 register(ctx) 里调用。可以安全地多次调用——只在第一次真正做事。
    返回 True 表示已安装成功。
    """
    cfg = cfg or CardConfig()
    return _install_on_feishu_module(cfg)


def uninstall() -> bool:
    """还原 monkey-patch（用于热重载场景）。"""
    global _INSTALLED, _ORIGINAL_SEND, _ORIGINAL_BUILD_OUTBOUND
    if not _INSTALLED:
        return True
    try:
        from gateway.platforms import feishu as feishu_mod
        cls = (
            getattr(feishu_mod, "FeishuAdapter", None)
            or getattr(feishu_mod, "FeishuPlatform", None)
        )
        if cls is not None:
            if _ORIGINAL_SEND is not None:
                cls.send = _ORIGINAL_SEND
            if _ORIGINAL_BUILD_OUTBOUND is not None:
                cls._build_outbound_payload = _ORIGINAL_BUILD_OUTBOUND
    except Exception:
        pass
    _INSTALLED = False
    _ORIGINAL_SEND = None
    _ORIGINAL_BUILD_OUTBOUND = None
    logger.info("[feishu-md-tables] Uninstalled")
    return True


# ---------------------------------------------------------------------------
# 显式 send_card 工具：让 LLM 主动调用，发一个 card
# ---------------------------------------------------------------------------


SEND_CARD_SCHEMA = {
    "name": "feishu_send_card",
    "description": (
        "Send a Feishu CardKit v2 card message to the current chat. "
        "Use this when the user wants a rich-text card with structured "
        "layout — for example to render a markdown table as a native "
        "Feishu table component (which the regular `send` text path "
        "cannot do). Pass `content` as a normal markdown message; if it "
        "contains a GFM-style markdown table, it will be parsed and "
        "embedded as a `table` element. Optionally pass `title` and "
        "`header_template` to style the card header."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "Markdown text. If it contains a GFM table "
                    "(header row + separator row + data rows), the table "
                    "is rendered as a Feishu CardKit `table` component. "
                    "Surrounding text becomes markdown elements in the "
                    "same card, in document order."
                ),
            },
            "title": {
                "type": "string",
                "description": (
                    "Optional card header title. Empty = no header. "
                    "Example: 'Q2 业绩对照表'."
                ),
            },
            "header_template": {
                "type": "string",
                "description": (
                    "Header color template. One of: blue, wathet, "
                    "turquoise, green, yellow, orange, red, carmine, "
                    "violet, purple, indigo, grey. Default: blue."
                ),
            },
            "page_size": {
                "type": "integer",
                "description": (
                    "How many table rows to show before the "
                    "'show more' button. Default: 5."
                ),
            },
        },
        "required": ["content"],
    },
}


async def _handle_feishu_send_card(args: dict, **kwargs) -> str:
    """``feishu_send_card`` 工具的实际实现。

    多表自动切分+循环发送：跟 ``_patched_send`` 同源逻辑（用
    ``split_blocks_by_table_groups`` 切分），但每组直接走
    ``build_card`` + ``adapter._send_raw_message``，避免被
    ``_build_outbound_payload`` 的 monkey-patch 二次处理。

    修 Bug 历史（2026-06-17）：旧实现直接 ``build_card(blocks, cfg)`` 不切分，
    6 个表塞同一张卡片 → 飞书 ErrCode 11310 → SDK Event loop 关闭 → 网关
    入站死锁。新实现按 ``cfg.max_tables_per_card`` 切分，行为与 ``send()``
    路径完全一致。
    """
    import os
    from .parser import split_into_blocks, split_blocks_by_table_groups  # 本地包内 import,避免循环

    content = (args.get("content") or "").strip()
    if not content:
        return json.dumps({"success": False, "error": "content is required"})

    cfg = CardConfig(
        header_title=(args.get("title") or "").strip(),
        header_template=(args.get("header_template") or "blue").strip(),
        page_size=int(args.get("page_size") or 5),
    )

    blocks = split_into_blocks(content)
    has_table = any(b.kind == "table" for b in blocks)

    # ── 解析 feishu home channel（同旧实现）──
    chat_id = None
    pconfig = None
    try:
        from gateway.config import load_gateway_config, Platform
        config = load_gateway_config()
        platform = Platform("feishu")
        pconfig = config.platforms.get(platform)
        if pconfig and pconfig.enabled:
            home = config.get_home_channel(platform)
            if home:
                chat_id = home.chat_id
    except Exception as exc:
        logger.warning("[feishu-md-tables] Failed to resolve feishu config: %s", exc)

    if not chat_id:
        # 回退：只返回 payload 给 LLM（旧行为）
        card = build_card(blocks, cfg)
        payload = json.dumps(card, ensure_ascii=False)
        return json.dumps(
            {
                "success": True,
                "has_table": has_table,
                "block_count": len(blocks),
                "msg_type": "interactive",
                "payload": payload,
                "card": card,
                "sent": False,
                "error": "No feishu home channel configured; card payload returned but not sent.",
            },
            ensure_ascii=False,
        )

    # ── 多表切分（与 _patched_send 同源）──
    table_count = sum(1 for b in blocks if b.kind == "table")
    if table_count <= cfg.max_tables_per_card or not has_table:
        # 1 张卡就能装下,或者压根没表——单卡路径
        groups = [blocks]
    else:
        groups = split_blocks_by_table_groups(blocks, max_tables_per_card=cfg.max_tables_per_card)
        if not groups:
            groups = [blocks]

    if len(groups) > 1:
        logger.info(
            "[feishu-md-tables] feishu_send_card: splitting %d tables into %d cards",
            table_count, len(groups),
        )

    # ── 创建临时 feishu adapter ──
    try:
        from gateway.platforms.feishu import FeishuAdapter
        from model_tools import _run_async

        adapter = FeishuAdapter(pconfig)
        _run_async(adapter.connect())
    except Exception as exc:
        logger.error("[feishu-md-tables] Failed to init feishu adapter: %s", exc, exc_info=True)
        return json.dumps(
            {
                "success": False,
                "has_table": has_table,
                "block_count": len(blocks),
                "msg_type": "interactive",
                "sent": False,
                "error": f"Adapter init failed: {exc}",
            },
            ensure_ascii=False,
        )

    # ── 循环发送每组 ──
    message_ids: List[str] = []
    last_error: Optional[str] = None
    try:
        for idx, group in enumerate(groups):
            card = build_card(group, cfg)
            payload = json.dumps(card, ensure_ascii=False)

            # 多卡时给非首张加 (N/M) 标识
            if len(groups) > 1:
                # 把 (N/M) 注入到 header_title（如果原 title 为空,就用纯标识;
                # 原 title 不空的话,把 (N/M) 拼到 title 前面）
                marker = f"({idx + 1}/{len(groups)}) "
                if cfg.header_title:
                    card["header"] = {
                        "template": cfg.header_template,
                        "title": {"tag": "plain_text", "content": marker + cfg.header_title},
                    }
                    payload = json.dumps(card, ensure_ascii=False)
                else:
                    cfg_with_marker = CardConfig(
                        header_title=marker.rstrip(),
                        header_template=cfg.header_template,
                        page_size=cfg.page_size,
                        cell_data_type=cfg.cell_data_type,
                        use_markdown_for_text=cfg.use_markdown_for_text,
                        max_tables_per_card=cfg.max_tables_per_card,
                    )
                    card = build_card(group, cfg_with_marker)
                    payload = json.dumps(card, ensure_ascii=False)

            try:
                response = _run_async(
                    adapter._send_raw_message(
                        chat_id=chat_id,
                        msg_type="interactive",
                        payload=payload,
                        reply_to=None,
                        metadata=None,
                    )
                )
            except Exception as exc:
                logger.error(
                    "[feishu-md-tables] feishu_send_card card %d/%d raised: %s",
                    idx + 1, len(groups), exc, exc_info=True,
                )
                last_error = str(exc)
                break

            # response 可能是对象或 dict,做兼容
            sent_ok = bool(getattr(response, "success", None) if not isinstance(response, dict) else response.get("success"))
            if sent_ok:
                data = getattr(response, "data", None) if not isinstance(response, dict) else response.get("data")
                msg_id = None
                if data is not None:
                    msg_id = getattr(data, "message_id", None) or (isinstance(data, dict) and data.get("message_id"))
                if msg_id:
                    message_ids.append(msg_id)
            else:
                err = getattr(response, "msg", None) or (isinstance(response, dict) and response.get("msg")) or "unknown"
                logger.warning(
                    "[feishu-md-tables] feishu_send_card card %d/%d failed: %s",
                    idx + 1, len(groups), err,
                )
                last_error = f"Feishu API error on card {idx + 1}/{len(groups)}: {err}"
                # 继续尝试发剩下的卡（飞书 SDK 已经从之前的 ErrCode 11310 恢复过,
                # 不必因一张失败就 abort 整个序列）
        _run_async(adapter.disconnect())
    except Exception as exc:
        logger.error("[feishu-md-tables] feishu_send_card unexpected: %s", exc, exc_info=True)
        last_error = str(exc)
        try:
            _run_async(adapter.disconnect())
        except Exception:
            pass

    sent_count = len(message_ids)
    return json.dumps(
        {
            "success": sent_count > 0,
            "has_table": has_table,
            "block_count": len(blocks),
            "table_count": table_count,
            "card_count": len(groups),
            "msg_type": "interactive",
            "chat_id": chat_id,
            "message_ids": message_ids,
            "sent_count": sent_count,
            "sent": sent_count > 0,
            "error": last_error if sent_count == 0 else None,
        },
        ensure_ascii=False,
    )
