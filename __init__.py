"""feishu-md-tables — Hermes 飞书 markdown 表格 → CardKit v2 卡片 插件。

* 自动拦截：飞书适配器 ``FeishuPlatform._build_outbound_payload`` 被替换；
  当出站消息含 markdown 表格时，自动把消息重写为 CardKit v2
  ``schema: "2.0"`` ``interactive`` 卡片（text + table 混合块按文档序排列）。
  解析失败时回退原行为（feishu.py 自己的 text / post 降级链），不破坏
  任何已有功能。
* 显式工具：注册 ``feishu_send_card`` 工具，让 LLM 在需要时主动
  拼一个卡片 payload 并通过原有 send 通道发出去（payload 已经计算好，
  工具返回 ``{"success": true, "payload": ...}`` 给 LLM 以便复制/调试）。

参考：
  * Hermes plugin API：``hermes_cli/plugins.py::PluginContext``
  * 飞书 Card 2.0 (CardKit) 文档：
    https://open.feishu.cn/document/feishu-cards/feishu-card-cardkit/components/table
  * 飞书 send message 文档：
    https://open.feishu.cn/document/server-docs/im-v1/message/create

使用：

.. code-block:: yaml

    # ~/.hermes/config.yaml
    plugins:
      enabled:
        - feishu-md-tables

然后重启 gateway。无需配置环境变量——纯本地纯函数。
"""
from __future__ import annotations

import logging

from .card_builder import CardConfig
from .interceptor import (
    SEND_CARD_SCHEMA,
    _handle_feishu_send_card,
    install as install_interceptor,
)


logger = logging.getLogger(__name__)


# 用户可改：默认 CardConfig；以后可扩展为从 config.yaml 读
_DEFAULT_CONFIG = CardConfig()


def register(ctx) -> None:
    """Hermes 插件注册入口（PluginManager 会在启动时调用）。

    步骤：
      1. 注册 ``feishu_send_card`` 工具（让 LLM 主动发富文本）
      2. 对 ``FeishuPlatform._build_outbound_payload`` 打 monkey-patch
         （让自动检测生效）
    """
    # 1) 显式 send_card 工具
    ctx.register_tool(
        name="feishu_send_card",
        toolset="feishu",
        schema=SEND_CARD_SCHEMA,
        handler=_handle_feishu_send_card,
        is_async=True,
        description=SEND_CARD_SCHEMA["description"],
        emoji="📊",
    )

    # 2) monkey-patch 飞书 platform
    ok = install_interceptor(_DEFAULT_CONFIG)
    if ok:
        logger.info("feishu-md-tables plugin registered and interceptor installed")
    else:
        logger.info(
            "feishu-md-tables plugin registered; interceptor will be applied "
            "lazily when feishu module becomes importable (normal during CLI "
            "sessions without the gateway running)"
        )
