# WSDC Points MCP Server

<!-- mcp-name: com.wsdcpoints/wsdc-points -->

**Ask Claude, ChatGPT, Cursor or any AI assistant about West Coast Swing dancers' WSDC points,
divisions, level-ups and competition results.** A free, public, read-only
[Model Context Protocol](https://modelcontextprotocol.io) (MCP) server for the
World Swing Dance Council (WSDC) Points Registry: ~27,000 dancers and ~8,000 events.

- **Endpoint:** `https://wsdcpoints.com/mcp` (Streamable HTTP, no API key, no sign-up)
- **Website:** [wsdcpoints.com](https://wsdcpoints.com) · [llms.txt](https://wsdcpoints.com/llms.txt) · [server card](https://wsdcpoints.com/mcp/server-card)
- **Official MCP Registry name:** `com.wsdcpoints/wsdc-points`

> Unofficial project, not affiliated with the World Swing Dance Council. The official registry is at
> [worldsdc.com/registry](https://worldsdc.com/registry/).

## What you can ask

- "What divisions can I dance as a follower? My name is …"
- "How many more Intermediate points do I need to move up? My WSDC ID is …"
- "Show the Champions results of the latest Swing Diego."
- "Who moved up to All-Stars in Germany in the last 3 months?"
- "How many active Advanced leaders are there in France?"
- "How many points does 1st place earn in a Novice Jack & Jill with 45 followers?"

## Connect

| Client | How |
|---|---|
| **Claude** (claude.ai, Desktop) | Settings → Connectors → *Add custom connector* → URL `https://wsdcpoints.com/mcp` |
| **Claude Code** | `claude mcp add --transport http wsdc-points https://wsdcpoints.com/mcp` |
| **ChatGPT** | Enable developer mode for apps/connectors, create a connector with the URL above, no authentication |
| **Cursor / Windsurf** | `{"mcpServers": {"wsdc-points": {"url": "https://wsdcpoints.com/mcp"}}}` |
| **VS Code (Copilot)** | `.vscode/mcp.json`: `{"servers": {"wsdc-points": {"type": "http", "url": "https://wsdcpoints.com/mcp"}}}` |
| **Anything else** | Any MCP client with Streamable HTTP transport |

## Tools

All tools are read-only (`readOnlyHint: true`) and return structured JSON with an output schema.

| Tool | What it does |
|---|---|
| `search_dancers` | Find dancers by name (any order, partial, misspelled, Cyrillic) or WSDC ID |
| `get_dancer_profile` | Current division and points per role (Leader/Follower), allowed Jack & Jill divisions per role, points per division, level-up timeline |
| `get_dancer_results` | A dancer's placements: event, date, division, role, placement, points; filters by division, role, year |
| `search_events` | Find WSDC registry events by name, city, year, country |
| `get_event_results` | All Jack & Jill placements of one event, by division |
| `get_recent_levelups` | Dancers who recently moved up a division, by country and division |
| `get_division_stats` | Dancer counts by current division and role, worldwide or per country |
| `search_wsdc_rules` | Search the current WSDC Registry Event Rules (move-up thresholds, points per tier, secondary role rules…) |
| `get_registry_info` | Data coverage and freshness |

Resource: `wsdc://rules/current`, the full text of the current rulebook.

## How it works

- Data comes from a continuously synchronised mirror of the public WSDC Points Registry
  (the same database behind the [@wsdc_bot](https://t.me/wsdc_bot) Telegram bot).
- Division eligibility per role, including the primary/secondary role rules, is computed in the
  database and returned as-is, so the server and the bot always agree.
- Stateless Streamable HTTP (JSON responses), results cached for minutes, per-IP rate limits.
- Python 3.12, [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) v2, psycopg 3, Starlette.

## Development

```bash
python3.12 -m venv venv && venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest -q                       # unit tests (no database needed)
WSDC_MCP_TEST_DSN="dbname=wsdc host=127.0.0.1" venv/bin/python -m pytest -q   # + end-to-end tests
WSDC_MCP_DB_DSN="dbname=wsdc host=127.0.0.1" venv/bin/python -m wsdc_mcp      # serve on 127.0.0.1:8001
```

| Variable | Meaning |
|---|---|
| `WSDC_MCP_DB_DSN` | PostgreSQL DSN of the WSDC registry database (read-only role) |
| `WSDC_MCP_PUBLIC_URL` | Public origin used in links and discovery documents (default `https://wsdcpoints.com`) |
| `WSDC_MCP_HOST` / `WSDC_MCP_PORT` | Bind address (default `127.0.0.1:8001`) |
| `WSDC_MCP_RULES_PATH` | Plain-text WSDC rulebook; enables `search_wsdc_rules` |
| `WSDC_MCP_POOL_MAX`, `WSDC_MCP_STATEMENT_TIMEOUT_MS` | DB pool size (3) and per-query timeout (8000 ms) |

The server expects the registry schema of the WSDC parser project (`contester_view`,
`contester_view_roles`, `entry_view`, `event`, …); the code is published for transparency and reuse.

## Privacy

No accounts, cookies or trackers. Requests and tool calls are logged for up to 30 days for operations.
Results contain only what the WSDC publishes in its public registry. See [wsdcpoints.com/privacy](https://wsdcpoints.com/privacy).

## License

MIT
