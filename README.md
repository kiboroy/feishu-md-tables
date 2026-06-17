# feishu-md-tables

A Hermes plugin that **automatically converts markdown tables in outgoing
Feishu (Lark) messages into native Feishu CardKit v2 `table` components**.

Without this plugin, the Feishu gateway falls back to plain text for any
message containing a markdown table (`feishu.py::_build_outbound_payload`
line ~4376 explicitly forces `text` mode because `post` type `md` elements
do not render tables). With this plugin, those messages become proper
interactive cards that render as native Feishu tables in the client.

> Track: official issue
> [NousResearch/hermes-agent#27695](https://github.com/NousResearch/hermes-agent/issues/27695).
> Multiple competing PRs are open; this plugin is the userland alternative
> that works against any Hermes version until the core lands table support.

## What it does

| Input (markdown)                         | Output (Feishu client)                                         |
|------------------------------------------|----------------------------------------------------------------|
| `\| a \| b \|\n\|---...\|\n\| 1 \| 2 \|` | CardKit v2 card with `tag: "table"` element, blue header       |
| Text + table mixed                       | Single card: `markdown` → `table` → `markdown` in document order |
| 1–2 tables (default `max_tables_per_card`) | Single card, all tables rendered natively                   |
| ≥3 tables                                | **Multiple cards** — auto-split into N cards (2 tables per card by default); each card carries a `(N/M)` header |
| Plain text (no table)                    | Pass-through to existing `post` / `text` path (unchanged)      |
| Malformed table                          | Fall back to original `text` path (safe degradation)           |

## Install

```bash
# Plugin is already in place if you cloned this repo to ~/.hermes/plugins/
hermes plugins enable feishu-md-tables
# Add to ~/.hermes/config.yaml if not already there:
#   plugins:
#     enabled:
#       - feishu-md-tables
```

Then restart the gateway:

```bash
hermes gateway restart
```

The plugin works with **zero configuration** — no env vars, no API keys,
no extra config. The default header is `blue` and `page_size` is `5`.

## How it works

1. **`register(ctx)`** runs once at startup. It registers a
   `feishu_send_card` LLM tool, then monkey-patches **two methods** on
   `FeishuAdapter`:
   - `send()` — the primary multi-card splitter
   - `_build_outbound_payload()` — kept for backwards compatibility
     and for unit-test / explicit-card-tool paths
2. **On every outbound Feishu message**, the patched `send()` checks for
   markdown tables:
   - No table → call original `send()` (zero overhead, behavior unchanged)
   - 1–2 tables (≤ `cfg.max_tables_per_card`) → call original `send()`
     with the original content (single card, unchanged behavior)
   - **≥3 tables** → split blocks into groups of `max_tables_per_card`
     tables each, prepend a `（N/M）` header to each sub-message, and
     loop-call original `send()` once per group. The original
     `_build_outbound_payload` (also patched) converts each sub-message
     into a CardKit v2 `schema: "2.0"` card on its way through.
   - Parsing fails → catch the exception, fall back to original `send()`
3. **`reply_to` is forwarded only to the first card** to avoid the user
   seeing N "reply to X" annotations. A failure on any card aborts the
   remaining cards and returns the failed response.
4. The existing `_feishu_send_with_retry` / `truncate_message` /
   `_finalize_send_result` flow is untouched — the patch sits *above*
   `send()` and replays the original method N times.

The patch is **idempotent and reversible**: `hermes plugins disable
feishu-md-tables` followed by a gateway restart restores the original
behavior.

## Layout

```
~/.hermes/plugins/feishu-md-tables/
├── __init__.py            # register(ctx) entry point
├── plugin.yaml            # manifest
├── parser.py              # markdown → MarkdownTable + split_into_blocks + split_blocks_by_table_groups
├── card_builder.py        # MarkdownTable → CardKit v2 JSON
├── interceptor.py         # monkey-patch (send + _build_outbound_payload) + tool handler
├── README.md              # this file
└── tests/
    ├── test_parser_and_card.py    # 21 unit tests
    ├── test_interceptor_e2e.py    # 7 e2e tests against real Feishu module
    ├── test_split_groups.py       # 10 tests for split_blocks_by_table_groups
    └── test_multi_card_send.py    # 9 tests for the multi-card send() loop
```

Run the test suite:

```bash
cd ~/.hermes/plugins/feishu-md-tables
python3 -m unittest discover tests -v
```

All **53 tests** pass in ~2 seconds.

## CardKit v2 reference

The card JSON this plugin produces:

```json
{
  "schema": "2.0",
  "header": { "template": "blue", "title": { "tag": "plain_text", "content": "..." } },
  "body": {
    "elements": [
      { "tag": "markdown", "content": "..." },
      {
        "tag": "table",
        "page_size": 5,
        "columns": [
          { "name": "col_0", "display_name": "Name", "data_type": "lark_md", "horizontal_align": "left" }
        ],
        "rows": [
          { "col_0": "Alice" }
        ]
      }
    ]
  }
}
```

References:

- [Feishu CardKit v2 table component docs](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/card-json-v2-components/content-components/table)
- [Feishu CardKit overview](https://open.feishu.cn/document/feishu-cards/feishu-card-cardkit/feishu-cardkit-overview)
- [Hermes plugin guide](https://hermes-agent.nousresearch.com/docs/zh-Hans/guides/build-a-hermes-plugin)

## `feishu_send_card` tool

The plugin also registers a tool the LLM can call explicitly when it
wants to force a card send with custom styling:

| Parameter        | Type    | Default  | Description                                              |
|------------------|---------|----------|----------------------------------------------------------|
| `content`        | string  | required | Markdown content (table is auto-extracted)               |
| `title`          | string  | `""`     | Optional card header title                               |
| `header_template`| string  | `"blue"` | `blue`/`wathet`/`turquoise`/`green`/`yellow`/`orange`/`red`/`carmine`/`violet`/`purple`/`indigo`/`grey` |
| `page_size`      | integer | `5`      | How many table rows before the "show more" button         |

Returns a JSON envelope with `card`, `payload`, `block_count`, etc.
The actual sending still flows through the patched `send()` /
`_build_outbound_payload`, so the explicit-tool path and the auto-
conversion path are consistent.

## Multi-card auto-split (since v0.2.0)

When a single LLM response contains **≥3 markdown tables**, the plugin
automatically splits the message into multiple Feishu cards instead of
packing them into one (which would exceed Feishu's ~30KB per-card limit
and cause silent send failures). The default `max_tables_per_card = 2`
is conservative — increase via `CardConfig(max_tables_per_card=5)` if
your tables are tiny and you want fewer messages.

Each card carries a small header like `_（1/3）_` (italic gray) so the
user can see which card is which in the sequence. The first card keeps
the original `reply_to` reference; subsequent cards do not (otherwise
the user would see N "reply to X" annotations).

If one card fails to send, the loop aborts and returns the failed
response — the remaining cards are **not** sent.

## Changelog

### 0.2.1 (2026-06-17) — fix 飞书入站死锁

**Bug**: v0.2.0 引入的 `_handle_feishu_send_card` 工具**不**走 `split_blocks_by_table_groups` 切分路径,直接 `build_card(blocks, cfg)` 把所有 table 塞进一张卡片。当 LLM 拼 6+ 表消息时:

1. 飞书 API 拒收 ([ErrCode 11310](https://open.feishu.cn/document/feishu-cards/feishu-card-cardkit/components/table) "card table number over limit")
2. 飞书 Lark SDK 内部异常处理中 `asyncio Event loop is closed`
3. WebSocket 断连,且重连失败 (`connect failed, err: Event loop is closed`)
4. **飞书入站消息全丢** — 用户在飞书打的字一个字都收不到,直到 gateway 重启

**Fix** (3 处):

1. `build_card` 防御性降级:单卡 table 数 > `cfg.max_tables_per_card` 时,**降级**为 markdown key:value 文本(不丢数据,只是格式退化),不再 silent 塞进 elements 列表
2. `_handle_feishu_send_card` 走切分+循环发送路径:复用 `parser.split_blocks_by_table_groups`,每组 build_card + 独立 `adapter._send_raw_message`,行为与 `send()` 路径完全一致
3. 删掉孤儿常量 `MAX_TABLES_PER_CARD=3` —— build_card 现在严格使用 `cfg.max_tables_per_card`,用户配置真正生效

**Tests**:
- 4 个新断言覆盖降级行为(`test_six_tables_overflow_gets_downgraded` 等)
- 2 个老断言("build_card 不切分, 6 表全要出来")更新为反映新行为
- 总测试数 51 → 53,全部通过

**升级提示**: gateway 重启后生效。重启前已有的入站死锁**不会**自动恢复 —— 必须 `hermes gateway restart`。

## Caveats

- **column names with non-ASCII characters** are sanitized to ASCII
  (`张三` → `col_0___`). The original text is preserved in `display_name`.
  This is required by Feishu's API (column name must match `[A-Za-z0-9_]`).
- **Single-column tables** are valid GFM and are rendered correctly.
- **Tables inside fenced code blocks** are intentionally **not**
  converted — they remain in the surrounding text as code. (Feishu's
  markdown tag handles code blocks; tables inside code blocks are an
  edge case the LLM almost never produces.)
- **Card size limit**: even with auto-split, a single very large table
  (e.g. 500+ rows) can still push one card past Feishu's ~30KB limit.
  If you regularly produce huge tables, lower `max_tables_per_card` to
  `1` and rely on `page_size` to add a "show more" button.
- **Multi-card ordering**: cards are sent sequentially in document
  order via the same `send()` retry path. They appear in the chat in
  order, but Feishu may briefly interleave typing indicators.
- **feishu platform version compatibility**: tested against
  `feishu.py` from hermes-agent HEAD Jun 2026 (class name
  `FeishuAdapter`, method `_build_outbound_payload`).
  Falls back to `FeishuPlatform` alias if a future rename occurs.

## License

MIT
