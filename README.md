# shipstation-mcp

MCP server for [ShipStation](https://www.shipstation.com/). Exposes order search, order details, shipment queries, and shipment tracking as MCP tools over HTTP/SSE with OAuth 2.0 (authorization code + PKCE) or static Bearer token authentication.

## Tools

| Tool | Description |
|---|---|
| `search_orders` | Search orders by number or status |
| `get_order_details` | Retrieve full order by ID |
| `get_shipments` | List shipments, filtered by order or tracking number |
| `track_shipment` | Get tracking info for a tracking number |

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- ShipStation API key and secret

## Quick start

```bash
git clone https://github.com/First-Vibes-LLC/shipstation-mcp.git
cd shipstation-mcp
uv sync
cp env.example .env   # fill in SSKEY and SSSEC
uv run python shipstation_mcp_server.py
```

The server starts at `http://localhost:8000`.

## Configuration

Copy `env.example` to `.env`:

| Variable | Required | Description |
|---|---|---|
| `SSKEY` | yes | ShipStation API key |
| `SSSEC` | yes | ShipStation API secret |
| `SSURL` | no | Custom API base URL (default: `https://ssapi.shipstation.com`) |
| `HTTP_HOST` | no | Bind address (default: `0.0.0.0`) |
| `HTTP_PORT` | no | Port (default: `8000`) |
| `MCP_AUTH_TOKEN` | no | Static Bearer token for protected routes |
| `MCP_BASE_URL` | no | Public URL for OAuth discovery (default: `http://localhost:8000`) |

## Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | public | Server info |
| `GET` | `/health` | public | Health check |
| `GET` | `/mcp` | Bearer | MCP SSE (LM Studio) |
| `POST` | `/mcp` | Bearer* | MCP JSON-RPC |
| `GET` | `/sse` | Bearer | Generic SSE |
| `GET` | `/.well-known/oauth-authorization-server` | public | OAuth discovery |

*`initialize`, `notifications/initialized`, and `tools/list` are allowed without a token.

## Authentication

### Static Bearer token

Set `MCP_AUTH_TOKEN` and pass it as `Authorization: Bearer <token>`.

### OAuth 2.0 (authorization code + PKCE)

Set `MCP_BASE_URL` to your public server URL. Clients discover the authorization server via `/.well-known/oauth-authorization-server`. Dynamic client registration (`/oauth/register`), authorization (`/oauth/authorize`), and token exchange (`/oauth/token`) are all implemented.

## Docker

```bash
docker build -t shipstation-mcp .

docker run -d \
  --name shipstation-mcp \
  -p 8000:8000 \
  -e SSKEY=your_key \
  -e SSSEC=your_secret \
  -e MCP_AUTH_TOKEN=your_token \
  shipstation-mcp
```

## Order statuses

Common values for the `order_status` filter:

- `awaiting_shipment` — ready to ship
- `shipped` — fulfilled
- `on_hold` — held
- `cancelled` — cancelled

## Carrier codes

Common values for `carrier_code`:

- `ups`, `fedex`, `usps`, `dhl`, `ontrac`

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format .
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for pull request guidelines.

## License

This project is licensed under the Apache License 2.0 — see [LICENSE](LICENSE) for details.
