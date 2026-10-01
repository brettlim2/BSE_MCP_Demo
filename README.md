# BSE + Meltwater MIRA MCP Server

A single **public, remote [MCP](https://modelcontextprotocol.io) server** that lets
any MCP client (Claude, etc.) pull **live, on-demand**:

1. **BSE corporate disclosures** — company announcements/filings from `bseindia.com/corporates`.
2. **Current BSE stock prices** — live quotes for a scrip.
3. **Meltwater MIRA** intelligence — grounded answers with citations.

**Design pillar — nothing is stored.** Every tool call is a *stateless passthrough*:
we fetch from the upstream API on each call, normalize, and return. No database, no
persisted disclosures or prices. The only in-process state is volatile (an in-memory
BSE cookie jar and a ~5-second micro-cache to collapse duplicate bursts) and is lost
on restart.

---

## Tools

| Tool | Purpose | Key args |
|------|---------|----------|
| `bse_search_company` | Name/ticker → BSE `scrip_code` (**start here**) | `query` |
| `bse_get_stock_price` | Live quote (LTP, change, OHLC, volume, 52wk H/L) | `scrip_code` |
| `bse_list_disclosures` | Corporate announcements/filings with PDF links | `scrip_code`, `from_date?`, `to_date?`, `category?`, `limit?` |
| `mira_ask` | Grounded MIRA answer + citations | `question`, `project_id?`, `thread_id?` |
| `mira_list_projects` | List MIRA grounding scopes | — |

Typical flow: `bse_search_company("Reliance")` → `scrip_code` `500325` →
`bse_get_stock_price("500325")` / `bse_list_disclosures("500325")`.

---

## Architecture

```
MCP client ──(Streamable HTTP + API key)──▶ FastMCP server (container)
                                              ├─ BSEClient  ──▶ api.bseindia.com   (live)
                                              └─ MiraClient ──▶ api.meltwater.com  (live)
   no DB · no persisted data · in-memory cookie jar + optional 5s micro-cache
```

- **Transport:** Streamable HTTP at `/mcp` (a real remote endpoint, not stdio).
- **Upstream BSE:** unofficial JSON endpoints under `api.bseindia.com/BseIndiaAPI/api`.
  They require browser-like headers and a cookie warm-up against `www.bseindia.com`;
  the client handles this and retries once on `403`.
- **Upstream MIRA:** `POST /v3/mira/responses` with the `apikey` header (rate-limited
  to 60 req/min, enforced client-side by a token bucket).

---

## Local development

```bash
pip install -e .                 # or: uv sync
cp .env.example .env             # set MELTWATER_API_KEY to use MIRA tools

# Inspect tools in the MCP Inspector:
mcp dev src/bse_mcp/server.py

# Or run the HTTP server directly:
MCP_API_KEYS=dev-key bse-mcp     # serves http://localhost:8000/mcp
curl localhost:8000/healthz
```

Point an MCP client at `http://localhost:8000/mcp` with header
`Authorization: Bearer dev-key`.

---

## Hosting a public endpoint

Deploy the container to a **long-lived host** (not edge/serverless) — BSE blocks many
cloud IPs, and a warm, long-running instance with a stable egress IP is far more
reliable.

**Render** (simplest): push the repo, create a Blueprint from `render.yaml`, set the
secrets below. Public URL: `https://<service>.onrender.com/mcp`.

**Fly.io** (region control; `fly.toml` defaults to Mumbai `bom`):
```bash
fly launch --no-deploy
fly secrets set MELTWATER_API_KEY=... MCP_API_KEYS=key1,key2
fly deploy
```
Public URL: `https://<app>.fly.dev/mcp`.

Any container host works (Railway, Cloud Run, ECS/Fargate) — serve the Docker image,
expose `$PORT`, keep the instance warm.

---

## Auth & secrets

- **Client gate:** set `MCP_API_KEYS` (comma-separated). Clients must send
  `Authorization: Bearer <key>` or `X-API-Key: <key>`. If unset, the gate is disabled
  (dev only — the server holds a real Meltwater key, so **always set it in prod**).
- **Upstream secret:** `MELTWATER_API_KEY` (server-owned). BSE needs no key.
- Secrets live only in the host's environment; nothing is written to disk.

| Env var | Required | Description |
|---------|----------|-------------|
| `MCP_API_KEYS` | prod | Allowlist of client API keys (CSV). |
| `MELTWATER_API_KEY` | for MIRA | Meltwater API token (Account → Meltwater API). |
| `PORT` / `HOST` | no | Bind address (host usually sets `PORT`). |
| `MCP_MOUNT_PATH` | no | MCP path, default `/mcp`. |
| `MIRA_TIMEOUT` | no | MIRA request timeout (seconds), default 120 — grounded answers can take ~30s+. |
| `HTTP_TIMEOUT`, `MAX_RETRIES`, `MICRO_CACHE_TTL` | no | HTTP tuning (BSE). |

---

## Caveats

- **BSE is unofficial.** The endpoints are the website's own XHR calls; they can
  change, throttle, or block cloud IPs without notice. This server is intended for
  personal/demo/research use — respect BSE's terms and do not redistribute its data.
  If your host IP gets blocked, try Fly.io in the `bom` region or a licensed data vendor.
- **Meltwater** publishes its own MCP server; this project intentionally bundles a thin
  MIRA passthrough so BSE and MIRA live behind one endpoint. MIRA tools require a valid
  `MELTWATER_API_KEY`.
- **No persistence by design.** This is a live gateway, not a datastore; historical
  data is whatever the upstream APIs return at call time.
