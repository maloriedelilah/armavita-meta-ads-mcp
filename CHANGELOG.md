# Changelog

## Unreleased

### Added

- Streamable HTTP transport (`--transport streamable-http`, with `--host`, `--port`, `--path` and repeatable `--allowed-host` for a tunnel's public name). In HTTP mode the Meta token comes only from each request's `Authorization: Bearer` header. It is never cached, and there is no fallback to `META_ACCESS_TOKEN`, the `--login` cache or app credentials. A `meta_access_token` tool argument is refused on every tool, and uvicorn's access log is off. stdio stays the default and is unchanged.
- `list_value_rule_sets` and `read_value_rule_set`: read an ad account's value rule sets (`act_<id>/value_rule_set`) and the rules inside a set.
- `create_ad_set` and `update_ad_set` accept `value_rule_set_id` to attach a value rule set; on `update_ad_set` an empty string detaches it. `read_ad_set` and `list_ad_sets` now return `value_rule_set_id`.
- `create_ad_creative` accepts `display_link`, the display URL shown under single image and video ads (sent as `link_data.caption` for images and `call_to_action.value.link_caption` for videos).

### Fixed

- **HTTP mode no longer exposes the host (ADS-48, ADS-50, ADS-51, security).**
  - `search_web_content`, `read_web_content` (a process-wide, unauthenticated cache) and `export_ad_image_file` (writes to disk) aren't offered in HTTP mode.
  - `upload_ad_image_asset` treats `image_file_path` as base64 or a data URL only and never reads the filesystem. It refuses `image_source_url`, so uploads are base64-only.
  - `upload_ad_video_asset` refuses `video_file_path`. `video_source_url` still works, because Meta fetches it itself.
  - Image downloads (`read_ad_image`) fetch only https URLs on Meta's image hosts, with no redirects.
  - stdio is unchanged.
- `description_text` is now passed through on single video creatives as `video_data.link_description`; it was previously dropped.
- Removed the client-side 40-character headline and 125-character description checks. Those are Meta's recommended display lengths, not API limits, so valid creatives with longer text were being rejected before reaching the API. Meta still validates and reports genuinely invalid lengths.

## 1.2.0 — 2026-07-07

### Added

- New tool groups, bringing the surface to **78 tools**: account controls, custom audiences (incl. lookalikes and user management), catalogs & products, Conversions API (`send_capi_events`, datasets, quality), custom conversions, async insights jobs (incl. MMM CSV export), custom derived metrics, reach & frequency predictions, partnership / branded-content ad permissions, recommendations, and Threads accounts.
- MCP tool annotations (`readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`) on all 78 tools.
- Cursor pagination (`page_cursor`) across all list streams; `create_report` follows insights cursor pagination end to end.
- Retry with exponential backoff for rate-limited Graph calls (rate limits retry any method; transient 5xx/transport errors retry idempotent GET/DELETE only).
- `default_conversion_value` support on custom conversion create/update/read.
- `level` validation on `create_insights_job`, matching `list_insights`.

### Changed

- v25 payload guards: `create_campaign`/`update_campaign` and `update_ad_set` drop non-writable fields (`campaign_budget_optimization`, `bid_cap`, `is_dynamic_creative` on update) and surface warnings instead of sending invalid params.
- `search_ads_archive`: `search_terms` is optional; requires at least one of `search_terms`/`search_page_ids`; v25 plural field names by default.
- `update_account_controls` sends `audience_controls`/`placement_controls` as whole objects per the v25 schema.
- Branded-content permissions moved to the Instagram User node; `grant_branded_content_ad_permission` replaces the former campaign-code tool.
- PII for CAPI events and Custom Audience uploads is normalized per Meta's field-specific rules (phone/DOB digits-only, city/state stripped of punctuation, zip without spaces/dashes) before SHA-256 hashing.

### Security

- OAuth login flow hardened: CSRF `state` nonce with fail-closed, constant-time validation; callback output HTML-escaped; token-leaking local `/token` route removed.
- Access tokens are redacted from returned URLs (including nested `paging.next`) and from logs; token cache written with `0600` permissions.
- Windows token-cache path falls back to the user home directory when `APPDATA` is unset.

## 1.1.0

- Meta Marketing API v25.0 baseline (`META_GRAPH_API_VERSION` override supported).
- Python 3.11+, `mcp[cli]==1.27.2`, stdio-only MCP transport.
