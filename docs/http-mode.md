# Running the server as a remote (HTTP) connector

By default the server talks **stdio** to an MCP client on the same machine. HTTP mode serves the same tools over MCP's Streamable HTTP transport. Use it when the client can't start a local process, for example a cloud agent session that has no access to your computer.

## How a request is authorised

Each request carries **two credentials**, both in headers:

| Header | What it is | Checked by |
| --- | --- | --- |
| `X-Armavita-Key: <shared secret>` | Proves the caller is allowed to use **this server**. | This server, before anything else runs. A missing or wrong key gets a 401. |
| `Authorization: Bearer <Meta access token>` | The Meta token the tools act with. | Meta's Graph API, on every call. |

In HTTP mode:

- **The Meta token is per request.** It comes only from the `Authorization` header. It is never cached or written anywhere. `META_ACCESS_TOKEN`, the `--login` cache and the app credentials are all ignored. A `meta_access_token` tool argument is refused.
- **The token only travels in the header.** It's sent to Graph only in the `Authorization` header, never in a URL or form body.
- **The host is never exposed.** Nothing reads or writes the host's files, and nothing fetches a URL the caller chose:
  - `search_web_content`, `read_web_content` and `export_ad_image_file` aren't offered;
  - image uploads take base64 or a `data:` URL only;
  - `video_file_path` is refused (use `video_source_url`, which Meta fetches itself).
- **Request bodies are capped at 44 MiB.** That's enough for the largest image upload (30 MB as base64) plus JSON.
  - A larger body gets a 413 before the server reads it.
  - A body must arrive within 120 seconds, or it gets a 408.
  - All requests together may hold at most about 96 MiB of body at once; past that, new ones get a 503.
- **Logs go to stderr only**, through a filter that removes Bearer tokens, `access_token` values, Meta-token-shaped strings and the shared secret. No `server.log` is written, and uvicorn's access log is off.

## 1. Create the shared secret

Generate it straight into a file only you can read, so it never appears in your shell history or on screen:

```bash
( umask 077 && mkdir -p ~/.config/armavita-meta-ads-mcp && \
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/armavita-meta-ads-mcp/http-secret )
```

The server refuses to start in HTTP mode without a secret of at least 32 characters, made only of letters, digits, `-` and `_`. `secrets.token_urlsafe` produces exactly that.

## 2. Start the server

```bash
ARMAVITA_MCP_SHARED_SECRET="$(cat ~/.config/armavita-meta-ads-mcp/http-secret)" \
  armavita-meta-ads-mcp --transport streamable-http --host 127.0.0.1 --port 8765 --allowed-host mcp.example.com
```

- `--host 127.0.0.1` keeps the server on loopback, and the tunnel (step 3) is the only way in. Don't bind `0.0.0.0` on a machine with open ports.
- `--allowed-host` is the public name the tunnel forwards. The MCP SDK's DNS-rebinding protection refuses any other `Host` header with a 421. Repeat the flag for more than one name.
- The endpoint is `/mcp` (change it with `--path`).

**For a quick local test with nothing exposed**, `--no-shared-secret` skips the key check. It's refused unless `--host` is loopback and no `--allowed-host` is given. **Never point a tunnel at a server started this way.** A tunnel that rewrites the `Host` header to `localhost` would get past both checks and make it public with no key.

**To check it:** a request without the key must get a 401:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/mcp   # 401
```

## 3. Expose it

The connector has to reach the server over HTTPS. Two options:

### Cloudflare Tunnel (works on any plan)

`cloudflared` makes an outbound-only connection, so no inbound firewall ports are needed. With a domain on Cloudflare:

```bash
cloudflared tunnel login
cloudflared tunnel create armavita-mcp
cloudflared tunnel route dns armavita-mcp mcp.example.com
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: armavita-mcp
credentials-file: /home/<you>/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: mcp.example.com
    service: http://127.0.0.1:8765
  - service: http_status:404
```

```bash
cloudflared tunnel run armavita-mcp
```

**What Cloudflare can see:** Cloudflare terminates TLS at its edge, so it handles the request headers, including the Meta token and the shared secret, in the clear.
- Keep `cloudflared` at its default log level. Don't turn on debug logging on the tunnel host.
- If you use Logpush or Logs, don't add request headers to the logged fields.
- For a second gate before requests reach the tunnel, put a Cloudflare Access application with a service token in front of the hostname.

### Claude MCP tunnels (Enterprise plan only)

[MCP tunnels](https://claude.com/docs/connectors/mcp-tunnels/overview) carry Claude's requests to a server inside your network. Unlike a Cloudflare Tunnel, the inner TLS ends at your own proxy, so Cloudflare carries only ciphertext. As of October 2026:
- they're a research preview, available on request only to claude.ai Enterprise organisations;
- the stack runs in Docker or Kubernetes;
- the proxy forwards only to private (RFC 1918) IPv4 addresses by default, so the server must listen on a LAN address. Give that address as `--host`, and give the tunnel hostname as `--allowed-host`.

## 4. Add the connector

Point the client at `https://mcp.example.com/mcp`, with both headers. **Don't put either value on a command line:** it lands in shell history and is visible in the process list. Put them in the client's config file instead. For example, Claude Code's `.mcp.json` expands environment variables in headers:

```json
{
  "mcpServers": {
    "armavita-meta-ads": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${META_ACCESS_TOKEN}",
        "X-Armavita-Key": "${ARMAVITA_MCP_KEY}"
      }
    }
  }
}
```

Set those two variables in the environment the client starts from. Use `read -rs` to enter them, so they aren't echoed or saved to history.

Every client stores these headers or variables somewhere, so treat that place like a password: owner-only permissions, and keep it out of repositories and chat transcripts.

## Rotating

- **Shared secret:** write a new one (step 1), restart the server, and update the connector's `X-Armavita-Key`.
- **Meta token:** update the connector's `Authorization` header. The server keeps no copy.
