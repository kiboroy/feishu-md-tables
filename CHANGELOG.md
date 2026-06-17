# Changelog

All notable changes to `feishu-md-tables` are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

**Bug fix:**
- `parser.parse_markdown_table` now de-duplicates sanitized column `name`s.
  When two header cells sanitize to the same `name` (e.g. two `"YoY"`
  columns), the second and subsequent occurrences get a `_N` suffix
  (where `N` is the column's 0-based index) so the resulting `name` list
  is guaranteed unique. Without this, `card_builder._row_dict` built
  `rows` as `{name: cell}` dicts, and a duplicate `name` caused the
  second cell to overwrite the first — the resulting row had fewer
  keys than there were columns, and Feishu's CardKit v2 server rejected
  the card with `errCode 200908 'column idx:N'` (the index it reports
  is the 0-based offset where the missing key was expected).
  `display_name` is unchanged, so cell labels in the Feishu client still
  show the original header text.
  - Reproduced 2026-06-17: a 7-column financial summary table (Q1 2026
    招商银行) with two `"YoY"` columns was split into the 2/2 card
    group and the second card failed to send with `column idx:5`.
  - 5 new regression tests in
    `tests/test_parser_and_card.py::TestDuplicateColumnNames`.

**Tests:**
- 5 new tests in `TestDuplicateColumnNames` covering: 2 duplicates,
  3 duplicates, duplicates after sanitize prefix, row-dict key parity
  (the exact end-to-end shape Feishu sees), and no-false-positives on
  non-duplicate input.

## [0.2.2] - 2026-06-17

**Changed:**
- `tests/test_interceptor_e2e.py` and `tests/test_multi_card_send.py`
  resolve the `hermes-agent` checkout path from the `HERMES_HOME` env
  var (with `Path.home() / ".hermes"` as fallback) instead of
  hardcoding `/home/ubuntu/.hermes/hermes-agent`. This makes the test
  suite runnable on any contributor's machine without requiring
  `HERMES_HOME` to be set, and stops shipping the author's local OS
  username into the public repo.

**Notes:**
- Production code is unchanged; only test bootstrap paths.
- No tests added or removed. All 53 existing tests still pass with
  and without `HERMES_HOME` set.

## [0.2.1] - 2026-06-17

**Bug:** `v0.2.0` introduced a regression in the `_handle_feishu_send_card`
tool — it bypassed the `split_blocks_by_table_groups` split path and
called `build_card(blocks, cfg)` directly, packing all tables into a
single card. When the LLM emitted 6+ tables this caused:

1. Feishu API rejected the card ([ErrCode 11310](https://open.feishu.cn/document/feishu-cards/feishu-card-cardkit/components/table)
   "card table number over limit")
2. Feishu Lark SDK's internal exception handler raised
   `asyncio Event loop is closed`
3. WebSocket disconnected and reconnect failed
   (`connect failed, err: Event loop is closed`)
4. **All incoming Feishu messages were dropped** — every keystroke
   from the user vanished until the gateway was restarted

**Fix (3 changes):**
1. `build_card` now degrades safely: when a single card would contain
   more than `cfg.max_tables_per_card` tables, it downgrades those
   tables to `markdown` `key:value` text instead of silently stuffing
   them into the `elements` list. No data is lost, only the format
   degrades.
2. `_handle_feishu_send_card` now goes through the split + per-group
   send path: it reuses `parser.split_blocks_by_table_groups`, then
   for each group calls `build_card` and an independent
   `adapter._send_raw_message`. Behavior is now identical to the
   `send()` path.
3. Removed the orphan constant `MAX_TABLES_PER_CARD=3`. `build_card`
   now strictly uses `cfg.max_tables_per_card`, so user configuration
   actually takes effect.

**Tests:**
- 4 new assertions cover the downgrade behavior
  (`test_six_tables_overflow_gets_downgraded` and friends)
- 2 old assertions ("build_card does not split, 6 tables all come
  out") updated to reflect the new behavior
- Test count: 51 → 53, all passing

**Upgrade notes:** Takes effect after gateway restart. Pre-existing
inbound deadlocks from before the upgrade **will not** auto-recover —
`hermes gateway restart` is required.

## [0.2.0] - multi-card auto-split

> Initial release notes were not captured in this changelog. The
> summary below is reconstructed from the README's
> "Multi-card auto-split (since v0.2.0)" section.

**Added:**
- When a single LLM response contains **≥3 markdown tables**, the
  plugin automatically splits the message into multiple Feishu
  cards instead of packing them into one (which would exceed
  Feishu's ~30KB per-card limit and cause silent send failures).
- Default `max_tables_per_card = 2` (conservative). Configurable
  via `CardConfig(max_tables_per_card=N)`.
- Each card carries a small header like `_（1/3）_` (italic gray)
  so the user can see which card is which in the sequence.
- `reply_to` is forwarded only to the first card to avoid the user
  seeing N "reply to X" annotations.
- If one card fails to send, the loop aborts and returns the failed
  response — the remaining cards are **not** sent.

## Earlier versions

Versions prior to 0.2.0 were not tagged or shipped publicly on
GitHub. No release notes survive.

[Unreleased]: https://github.com/kiboroy/feishu-md-tables/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/kiboroy/feishu-md-tables/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/kiboroy/feishu-md-tables/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/kiboroy/feishu-md-tables/releases/tag/v0.2.0
