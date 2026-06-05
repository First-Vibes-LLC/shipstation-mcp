#!/usr/bin/env python3
"""ShipStation MCP Server — HTTP/SSE transport with OAuth 2.0 support."""

import asyncio
import json
import logging
import os
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastmcp import FastMCP
from mcp_oauth import (
    get_asgi_app,
    init_oauth,
    negotiate_mcp_protocol_version,
    verify_token,
    verify_token_except_initialize,
)
from shipstation import ShipStationClient, ShipStationError
from sse_starlette.sse import EventSourceResponse
from starlette.responses import Response

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

SHIPSTATION_API_KEY = os.environ.get("SSKEY")
SHIPSTATION_API_SECRET = os.environ.get("SSSEC")
SHIPSTATION_BASE_URL = os.environ.get("SSURL", "https://ssapi.shipstation.com")

HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8000"))

MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN")
MCP_BASE_URL = os.environ.get("MCP_BASE_URL", "http://localhost:8000")

server_instructions = """
This MCP server provides ShipStation shipping and fulfillment data access capabilities
for chat and deep research connectors. Use the search_orders tool to find orders,
get_order_details to retrieve complete order information, track_shipment to get tracking info,
and get_shipments to retrieve shipment details.
"""


class ShipStationMCPClient:
    """Wrapper around ShipStationClient with MCP-oriented methods."""

    def __init__(self):
        if not SHIPSTATION_API_KEY or not SHIPSTATION_API_SECRET:
            raise ValueError(
                "ShipStation credentials not found. Set SSKEY and SSSEC environment variables."
            )
        self.client = ShipStationClient(
            api_key=SHIPSTATION_API_KEY,
            api_secret=SHIPSTATION_API_SECRET,
            base_url=SHIPSTATION_BASE_URL,
        )
        logger.info("ShipStation client initialized")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def health_check(self) -> bool:
        try:
            self.client.get_orders()
            return True
        except Exception as e:
            logger.warning("ShipStation health check failed: %s", e)
            return False

    def search_orders(
        self,
        order_number: str | None = None,
        order_status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        parameters = {}
        if order_number:
            parameters["order_number"] = order_number
        if order_status:
            parameters["order_status"] = order_status

        result = self.client.get_orders(
            parameters=parameters if parameters else None
        )
        orders = (
            result.get("orders", []) if isinstance(result, dict) else result
        )
        if isinstance(orders, list) and limit:
            orders = orders[:limit]
        return {"orders": orders}

    def get_order_by_id(self, order_id: str) -> dict[str, Any]:
        result = self.client.get_orders(
            parameters={"order_number": order_id}
        )
        if isinstance(result, dict) and "orders" in result:
            orders = result["orders"]
            if orders:
                return orders[0]
        elif isinstance(result, list) and result:
            return result[0]
        return result

    def get_shipments(
        self,
        order_id: str | None = None,
        tracking_number: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        parameters = {}
        if order_id:
            parameters["order_id"] = order_id
        if tracking_number:
            parameters["tracking_number"] = tracking_number

        result = self.client.get_shipments(
            parameters=parameters if parameters else None
        )
        shipments = (
            result.get("shipments", [])
            if isinstance(result, dict)
            else result
        )
        if isinstance(shipments, list) and limit:
            shipments = shipments[:limit]
        return {"shipments": shipments}

    def track_shipment(
        self, tracking_number: str, carrier_code: str | None = None
    ) -> dict[str, Any]:
        shipments = self.get_shipments(tracking_number=tracking_number)
        if not shipments.get("shipments"):
            return {"tracking_info": None, "error": "Shipment not found"}

        shipment = shipments["shipments"][0]
        tracking_info = {
            "tracking_number": tracking_number,
            "carrier_code": shipment.get("carrier_code"),
            "service_code": shipment.get("service_code"),
            "ship_date": shipment.get("ship_date"),
            "tracking_url": shipment.get("tracking_url"),
            "shipment_cost": shipment.get("shipment_cost"),
            "delivery_date": shipment.get("delivery_date"),
            "order_id": shipment.get("order_id"),
            "order_number": shipment.get("order_number"),
        }
        return {"tracking_info": tracking_info}


def create_server(shipstation_client: ShipStationMCPClient):
    """Create and configure the FastMCP server."""

    def ensure_connection() -> None:
        if not shipstation_client.health_check():
            raise ValueError("ShipStation connection is not available")

    mcp = FastMCP(
        name="ShipStation MCP Server", instructions=server_instructions
    )

    @mcp.tool()
    async def search_orders(
        order_number: str | None = None,
        order_status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        Search for orders in ShipStation.

        Args:
            order_number: Optional order number to search for
            order_status: Optional status filter (e.g. 'awaiting_shipment', 'shipped')
            limit: Maximum number of results (default 50, max 500)
        """
        logger.info(
            "search_orders number=%s status=%s", order_number, order_status
        )
        ensure_connection()
        result = shipstation_client.search_orders(
            order_number=order_number,
            order_status=order_status,
            limit=limit,
        )
        logger.info("search_orders returned %d results", len(result.get("orders", [])))
        return result

    @mcp.tool()
    async def get_order_details(order_id: str) -> dict[str, Any]:
        """
        Retrieve complete order details by ID.

        Args:
            order_id: ShipStation order ID
        """
        if not order_id:
            raise ValueError("order_id is required")
        logger.info("get_order_details order_id=%s", order_id)
        ensure_connection()
        return shipstation_client.get_order_by_id(order_id)

    @mcp.tool()
    async def get_shipments(
        order_id: str | None = None,
        tracking_number: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        Get shipments from ShipStation.

        Args:
            order_id: Optional order ID to filter shipments
            tracking_number: Optional tracking number
            limit: Maximum number of results (default 50, max 500)
        """
        logger.info(
            "get_shipments order_id=%s tracking=%s", order_id, tracking_number
        )
        ensure_connection()
        result = shipstation_client.get_shipments(
            order_id=order_id, tracking_number=tracking_number, limit=limit
        )
        logger.info("get_shipments returned %d results", len(result.get("shipments", [])))
        return result

    @mcp.tool()
    async def track_shipment(
        tracking_number: str, carrier_code: str | None = None
    ) -> dict[str, Any]:
        """
        Track a shipment by tracking number.

        Args:
            tracking_number: Tracking number to look up
            carrier_code: Optional carrier code (e.g. 'fedex', 'ups', 'usps')
        """
        if not tracking_number:
            raise ValueError("tracking_number is required")
        logger.info("track_shipment %s", tracking_number)
        ensure_connection()
        return shipstation_client.track_shipment(tracking_number, carrier_code)

    return mcp


def _tool_definitions() -> list[dict]:
    return [
        {
            "name": "search_orders",
            "description": "Search for orders in ShipStation",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "order_number": {
                        "type": "string",
                        "description": "Optional order number to search for",
                    },
                    "order_status": {
                        "type": "string",
                        "description": "Optional status filter (e.g. 'awaiting_shipment', 'shipped')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results to return (default 50, max 500)",
                        "default": 50,
                    },
                },
            },
        },
        {
            "name": "get_order_details",
            "description": "Retrieve complete order details by ID",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "ShipStation order ID",
                    }
                },
                "required": ["order_id"],
            },
        },
        {
            "name": "get_shipments",
            "description": "Get shipments from ShipStation",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "Optional order ID to filter shipments",
                    },
                    "tracking_number": {
                        "type": "string",
                        "description": "Optional tracking number",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum results to return (default 50, max 500)",
                        "default": 50,
                    },
                },
            },
        },
        {
            "name": "track_shipment",
            "description": "Track a shipment by tracking number",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tracking_number": {
                        "type": "string",
                        "description": "Tracking number to look up",
                    },
                    "carrier_code": {
                        "type": "string",
                        "description": "Optional carrier code (e.g. 'fedex', 'ups', 'usps')",
                    },
                },
                "required": ["tracking_number"],
            },
        },
    ]


def _initialize_result(params: dict | None) -> dict:
    tools = _tool_definitions()
    negotiated = negotiate_mcp_protocol_version(params)
    names = ", ".join(t["name"] for t in tools)
    instructions = (
        f"{server_instructions.strip()}\n\nAvailable tools: {names}."
    )
    return {
        "protocolVersion": negotiated,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "ShipStation MCP Server", "version": "1.0.0"},
        "instructions": instructions,
        "tools": tools,
    }


app = FastAPI(title="ShipStation MCP Server", version="1.0.0")
shipstation_client = None
mcp_server = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if os.environ.get("LOG_REQUESTS"):

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        response = await call_next(request)
        logger.info("%s %s -> %s", request.method, request.url.path, response.status_code)
        return response


init_oauth(app, base_url=MCP_BASE_URL, static_token=MCP_AUTH_TOKEN)


@app.get("/")
async def root(request: Request):
    return {
        "name": "ShipStation MCP Server",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "mcp_sse": "/mcp",
            "sse": "/sse",
            "mcp_post": "/mcp (POST)",
            "health": "/health",
        },
    }


@app.get("/health")
async def health_check():
    try:
        if shipstation_client and shipstation_client.health_check():
            return {"status": "healthy", "shipstation": "connected"}
        return {"status": "unhealthy", "shipstation": "disconnected"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/sse")
async def sse_endpoint(request: Request, auth: bool = Depends(verify_token)):
    async def event_generator():
        try:
            yield {
                "event": "message",
                "data": json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "notification",
                        "params": {
                            "type": "connection",
                            "message": "ShipStation MCP Server connected",
                        },
                    }
                ),
            }
            while True:
                if await request.is_disconnected():
                    break
                await asyncio.sleep(30)
                yield {
                    "event": "heartbeat",
                    "data": json.dumps({"type": "heartbeat"}),
                }
        except Exception as e:
            logger.error("SSE error: %s", e)
            yield {
                "event": "error",
                "data": json.dumps(
                    {"jsonrpc": "2.0", "error": {"code": -32603, "message": str(e)}}
                ),
            }

    return EventSourceResponse(
        event_generator(),
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/mcp")
async def mcp_sse_endpoint(
    request: Request, auth: bool = Depends(verify_token)
):
    async def mcp_event_generator():
        try:
            yield {
                "event": "message",
                "data": json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {
                                "name": "ShipStation MCP Server",
                                "version": "1.0.0",
                            },
                        },
                    }
                ),
            }
            while True:
                if await request.is_disconnected():
                    break
                await asyncio.sleep(30)
                yield {"event": "ping", "data": json.dumps({"type": "ping"})}
        except Exception as e:
            logger.error("MCP SSE error: %s", e)
            yield {
                "event": "error",
                "data": json.dumps(
                    {"jsonrpc": "2.0", "error": {"code": -32603, "message": str(e)}}
                ),
            }

    return EventSourceResponse(
        mcp_event_generator(),
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.post("/mcp")
@app.post("/")
async def handle_mcp_message(
    request: Request, auth: bool = Depends(verify_token_except_initialize)
):
    """Handle MCP JSON-RPC messages over HTTP POST."""
    try:
        raw = getattr(request.state, "_mcp_post_body_bytes", None)
        body = json.loads(raw.decode("utf-8") if raw else await request.body())
        logger.info("MCP message: %s", body.get("method"))

        method = body.get("method")

        if method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": _initialize_result(body.get("params")),
            }
        elif method == "notifications/initialized":
            return Response(status_code=202, content=b"")
        elif method == "tools/list":
            response = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {"tools": _tool_definitions()},
            }
        elif method == "tools/call":
            tool_name = body.get("params", {}).get("name")
            tool_args = body.get("params", {}).get("arguments", {})
            try:
                if tool_name == "search_orders":
                    result = shipstation_client.search_orders(
                        order_number=tool_args.get("order_number"),
                        order_status=tool_args.get("order_status"),
                        limit=tool_args.get("limit", 50),
                    )
                elif tool_name == "get_order_details":
                    result = shipstation_client.get_order_by_id(
                        tool_args.get("order_id")
                    )
                elif tool_name == "get_shipments":
                    result = shipstation_client.get_shipments(
                        order_id=tool_args.get("order_id"),
                        tracking_number=tool_args.get("tracking_number"),
                        limit=tool_args.get("limit", 50),
                    )
                elif tool_name == "track_shipment":
                    result = shipstation_client.track_shipment(
                        tracking_number=tool_args.get("tracking_number"),
                        carrier_code=tool_args.get("carrier_code"),
                    )
                else:
                    result = {"error": f"Unknown tool: {tool_name}"}

                response = {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "content": [
                            {"type": "text", "text": json.dumps(result, indent=2)}
                        ]
                    },
                }
            except Exception as e:
                response = {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "error": {
                        "code": -32603,
                        "message": f"Tool execution failed: {e}",
                    },
                }
        else:
            response = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {"message": "received", "method": method},
            }

        return response

    except Exception as e:
        logger.error("Error processing MCP message: %s", e)
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32603, "message": "Internal error", "data": str(e)},
        }


def start_http_server():
    global shipstation_client, mcp_server

    if not SHIPSTATION_API_KEY or not SHIPSTATION_API_SECRET:
        raise ValueError(
            "ShipStation credentials not found. Set SSKEY and SSSEC environment variables."
        )

    shipstation_client = ShipStationMCPClient()
    mcp_server = create_server(shipstation_client)

    logger.info("Starting ShipStation MCP server on %s:%s", HTTP_HOST, HTTP_PORT)

    with shipstation_client:
        import uvicorn

        uvicorn.run(
            get_asgi_app(app),
            host=HTTP_HOST,
            port=HTTP_PORT,
            log_level="info",
        )


def main():
    start_http_server()


if __name__ == "__main__":
    main()
