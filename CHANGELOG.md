# Changelog

## Unreleased

### Removed

- `delete_custom_audience` and `delete_custom_conversion` (ADS-54). This server never deletes anything; the account owner deletes in Ads Manager.

### Added

- Streamable HTTP transport (`--transport streamable-http`, with `--host`, `--port`, `--path` and repeatable `--allowed-host` for a tunnel's public name). In HTTP mode the Meta token comes only from each request's `Authorization: Bearer` header. It is never cached, and there is no fallback to `META_ACCESS_TOKEN`, the `--login` cache or app credentials. A `meta_access_token` tool argument is refused on every tool, and uvicorn's access log is off. stdio stays the default and is unchanged.
- `list_value_rule_sets` and `read_value_rule_set`: read an ad account's value rule sets (`act_<id>/value_rule_set`) and the rules inside a set (each rule's name, adjustment and status; Meta does not return the condition a rule matches).
- `create_ad_set` and `update_ad_set` accept `value_rule_set_id` to attach a value rule set; on `update_ad_set` an empty string detaches it. `read_ad_set` and `list_ad_sets` now return `value_rule_set_id`.
- `create_ad_creative` accepts `display_link`, the display URL shown under single image and video ads (sent as `link_data.caption` for images and `call_to_action.value.link_caption` for videos).

### Fixed

- **Nothing is created on the host at import (Errata's nit on #16).** The `~/.config/armavita-meta-ads-mcp` folder is made only when server.log is first written (stdio), or when a `--login` token is saved, and then with 0700 permissions. HTTP mode creates no folder at all.
- **Untrusted image bytes are handled with limits (ADS-62, security).**
  - Pillow opens only JPEG, PNG, WEBP and GIF. An EPS in a creative can no longer make Pillow launch Ghostscript, which would inherit the server's environment.
  - Images over 40 megapixels are refused before decoding. Pillow's default only warned up to twice its limit, and a 20 KB PNG could reach ~900 MB of memory.
  - Image downloads ask for uncompressed bytes and refuse any compressed response, stream, stop at 30 MB (Meta's ad image limit), and have one 60-second deadline for all attempts together. A too-big response isn't retried.
  - `upload_ad_image_asset` refuses base64 over 30 MB, and checks the decoded bytes are a JPEG, PNG, WEBP or GIF within the limits before sending them. A path that isn't a file is never sent to Meta as if it were image bytes, and the reply doesn't echo it. The check runs in linear time. Local files are read with a cap, not stat-then-read.
  - `upload_ad_video_asset` refuses local files over 1 GB (it sends the file in one request) and points to `video_source_url` instead.
- **No token in any log (ADS-45, security).**
  - A redacting filter on every handler (server.log, root, the MCP SDK, httpx and uvicorn) rewrites messages, arguments and tracebacks. It removes `Bearer …`, `access_token=…`, `"access_token": …` and Meta-token-shaped strings.
  - The Graph error paths log only exception types.
  - In HTTP mode, uvicorn runs with the filter in its own log config, and its access log stays off.
  - In HTTP mode there is no `server.log`: the package logs to stderr only, so nothing collects quietly on the host or in a container. The switch happens before the first log line, and the file is opened lazily, so it's never created.
  - `server.log` is created 0600 (owner only), and an existing one is tightened to 0600 when it's opened.
- **The Meta token is sent only in the `Authorization: Bearer` header (ADS-44, security).** It used to travel as an `access_token` query parameter (GET/DELETE) or form field (POST). That way it could end up in URLs, proxy logs and exception messages. Any token-named key in a tool's params is dropped. Graph honours the header: probed Oct 28, a fake bearer gets error 190 "Cannot parse access token", while no token gets 2500.
- **Review follow-ups (ADS-64, ADS-66).**
  - Outside `ad_account_id` and `object_id`, ID arguments refuse `act_…` and `me`, so an object call can't be pointed at the ad account or the user. Before, `update_campaign(campaign_id="act_123", spend_cap=…)` would have set the account's spend cap.
  - `update_*` treat `status=""` as "unchanged".
  - The `value_rule_set_id: null` refusal now offers `"keep"`.
  - The OAuth callback logs only exception types.
- `update_ad_set`: an explicit `null` `value_rule_set_id` is now refused with a message (`""` detaches; leaving it out keeps). It used to be read as "not given" and silently changed nothing, while in Graph `null` detaches. Refusing it, instead of detaching, protects clients that send `null` for every unused optional field: a budget edit can never strip a value rule. The reply now says when a value rule set was attached or detached.
- **IDs can no longer reshape Graph requests (ADS-53, security).** Every tool's ID arguments must be plain IDs. `*_id` and `*_ids` allow only letters, digits and `_`; `ad_account_id` must be `act_<digits>` or digits, and `meta_user_id` must be `me` or digits. `make_api_request` also refuses any Graph path that isn't plain segments. Before, `update_ad(ad_id="321?status=ACTIVE", bid_amount=50)` activated the ad, and `custom_audience_id="77#"` on a remove-users call deleted the whole audience. Any argument mentioning `access_token` (for example `fields="name,access_token"`) is refused, and any response field whose name contains `access_token` (such as Page tokens) is redacted.
- **Status safety (ADS-54).** Following the house rules: everything is built paused, the user reviews before activating, and nothing is deleted.
  - `create_campaign`, `create_ad_set`, `create_ad` and `clone_campaign`/`clone_ad_set`/`clone_ad` always create PAUSED. A missing, null or `""` status means PAUSED (before, `status=""` replaced the default), and ACTIVE or any other value is refused with a message to activate later through the update tool.
  - `update_campaign`, `update_ad_set` and `update_ad` accept only ACTIVE or PAUSED (any case) and refuse DELETED and ARCHIVED.
  - `batch_products` refuses DELETE items.
  - `destructiveHint` is now true on `batch_products` and `update_account_controls`.
- **`--login` no longer writes secrets to server.log (ADS-55, security).**
  - The token exchanges POST the app secret, auth code and short-lived token in the body, not the URL.
  - Failures log only the exception type, the HTTP status and Graph's error code. Before, a network error logged the full URL with `client_secret` and `code`.
  - Only Graph code 190 on the cached login token clears the login. Before, any permission error (10) or code 102 deleted it.
  - The OAuth callback ignores `?error=` unless it carries this login's state, and prints callback text without control characters.
  - The unused `AuthManager.authenticate()`, which logged the full login URL, is removed.
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
