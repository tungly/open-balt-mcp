#!/usr/bin/env python3
"""
MCP Server for Baltimore Housing Permit Data
Connects to data.baltimorecity.gov to query building permits
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence
from urllib.parse import quote, urlencode

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    EmbeddedResource,
    LoggingLevel
)


# Baltimore Open Data API endpoint. Override with BALTIMORE_PERMIT_API if the
# city moves the service again (it was previously on egisdata.baltimorecity.gov/egis).
BALTIMORE_PERMIT_API = os.environ.get(
    "BALTIMORE_PERMIT_API",
    "https://baltegis.baltimorecity.gov/mapping/rest/services/Housing/DHCD_Open_Baltimore_Datasets/FeatureServer/3/query",
)

app = Server("balt-permit-mcp-server")


async def query_permit_api(
    where_clause: str = "1=1",
    out_fields: str = "*",
    result_offset: Optional[int] = None,
    max_records: Optional[int] = None,
    return_count_only: bool = False
) -> dict:
    """Query the Baltimore permit API with given parameters."""
    params = {
        "where": where_clause,
        "outFields": out_fields,
        "f": "geojson",
        "returnGeometry": "true"
    }

    if result_offset is not None:
        params["resultOffset"] = result_offset

    if max_records is not None:
        params["resultRecordCount"] = max_records

    if return_count_only:
        params["returnCountOnly"] = "true"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(BALTIMORE_PERMIT_API, params=params)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as e:
            raise Exception(f"Failed to fetch permit data: {str(e)}")

    # ArcGIS reports query errors as HTTP 200 with an "error" body. Without this
    # check a rejected query has no "features" and reads as "No permits found".
    if isinstance(data, dict) and "error" in data:
        err = data["error"] or {}
        details = "; ".join(err.get("details") or [])
        raise Exception(
            f"Permit API rejected the query ({err.get('code')}): {err.get('message')}"
            + (f" {details}" if details else "")
        )
    return data


async def query_permit_api_paginated(
    where_clause: str = "1=1",
    out_fields: str = "*",
    max_records: Optional[int] = None,
    page_size: int = 1000
) -> dict:
    """
    Query the Baltimore permit API with automatic pagination.

    Args:
        where_clause: SQL WHERE clause for filtering
        out_fields: Fields to return
        max_records: Maximum total records to return (None for all)
        page_size: Records per page (default 1000, which is typically the API limit)

    Returns:
        dict with 'features' list containing all permits and 'totalRetrieved' count
    """
    all_features = []
    offset = 0
    total_requested = max_records if max_records is not None else float('inf')

    while len(all_features) < total_requested:
        # Calculate how many records to fetch in this batch
        remaining = total_requested - len(all_features)
        batch_size = min(page_size, remaining) if max_records is not None else page_size

        # Fetch batch
        data = await query_permit_api(
            where_clause=where_clause,
            out_fields=out_fields,
            result_offset=offset,
            max_records=batch_size
        )

        features = data.get("features", [])
        if not features:
            # No more results
            break

        all_features.extend(features)

        # Check if we got fewer results than requested (end of data)
        if len(features) < batch_size:
            break

        # Check if API indicates no more data
        if not data.get("exceededTransferLimit", False):
            break

        offset += len(features)

    return {
        "type": "FeatureCollection",
        "features": all_features,
        "totalRetrieved": len(all_features)
    }


def format_permit(permit: dict) -> str:
    """Format a permit feature into a readable string."""
    props = permit.get("properties") or {}
    geom = permit.get("geometry") or {}  # GeoJSON allows "geometry": null
    
    # Format dates
    issued_date = ""
    if props.get("IssuedDate"):
        try:
            issued_ts = props["IssuedDate"] / 1000  # Convert from milliseconds
            issued_date = datetime.fromtimestamp(issued_ts).strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            issued_date = str(props.get("IssuedDate", "N/A"))
    
    expiration_date = ""
    if props.get("ExpirationDate"):
        try:
            exp_ts = props["ExpirationDate"] / 1000
            expiration_date = datetime.fromtimestamp(exp_ts).strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            expiration_date = str(props.get("ExpirationDate", "N/A"))
    
    # Format coordinates
    coords = ""
    if geom.get("coordinates"):
        lon, lat = geom["coordinates"]
        coords = f"({lat:.6f}, {lon:.6f})"
    
    cost = props.get("Cost")
    cost_str = f"${cost:,.0f}" if cost else "N/A"
    
    return f"""
    Permit: {props.get('CaseNumber', 'N/A')}
    Address: {props.get('Address', 'N/A')}
    Neighborhood: {props.get('Neighborhood', 'N/A')}
    Issued: {issued_date}
    Expires: {expiration_date}
    Cost: {cost_str}
    Description: {props.get('Description', 'N/A')[:200]}...
    Block/Lot: {props.get('BLOCKLOT', 'N/A')}
    Council District: {props.get('Council_District', 'N/A')}
    Existing Use: {props.get('ExistingUse', 'N/A')}
    Proposed Use: {props.get('ProposedUse', 'N/A')}
    Location: {coords}
    Modification: {'Yes' if props.get('IsPermitModification') else 'No'}
---"""


def _ms_to_date(value: Any) -> Optional[str]:
    """Convert an ArcGIS epoch-milliseconds timestamp to YYYY-MM-DD, or None."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return None


def to_record(permit: dict) -> dict:
    """Convert a permit feature into a typed record matching PERMIT_RECORD_SCHEMA."""
    props = permit.get("properties") or {}
    coords = (permit.get("geometry") or {}).get("coordinates") or [None, None]
    lon, lat = (coords + [None, None])[:2]
    cost = props.get("Cost")
    return {
        "case_number": props.get("CaseNumber") or "",
        "address": props.get("Address") or "",
        "neighborhood": props.get("Neighborhood"),
        "issued_date": _ms_to_date(props.get("IssuedDate")),
        "expiration_date": _ms_to_date(props.get("ExpirationDate")),
        "cost": float(cost) if isinstance(cost, (int, float)) else None,
        "description": props.get("Description"),
        "block_lot": props.get("BLOCKLOT"),
        "council_district": props.get("Council_District"),
        "existing_use": props.get("ExistingUse"),
        "proposed_use": props.get("ProposedUse"),
        "is_modification": bool(props.get("IsPermitModification")),
        "lat": lat if isinstance(lat, (int, float)) else None,
        "lon": lon if isinstance(lon, (int, float)) else None,
    }


_nullable_string = {"type": ["string", "null"]}

PERMIT_RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "case_number": {"type": "string"},
        "address": {"type": "string"},
        "neighborhood": _nullable_string,
        "issued_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "expiration_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "cost": {"type": ["number", "null"]},
        "description": _nullable_string,
        "block_lot": _nullable_string,
        "council_district": {"type": ["string", "integer", "null"]},
        "existing_use": _nullable_string,
        "proposed_use": _nullable_string,
        "is_modification": {"type": "boolean"},
        "lat": {"type": ["number", "null"]},
        "lon": {"type": ["number", "null"]},
    },
    "required": ["case_number", "address"],
}

PERMIT_LIST_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "The search term the tool was called with"},
        "total": {"type": "integer", "description": "Number of permits returned"},
        "permits": {"type": "array", "items": PERMIT_RECORD_SCHEMA},
    },
    "required": ["query", "total", "permits"],
}


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools for querying permit data."""
    return [
        Tool(
            name="search_permits_by_address",
            description="Search for building permits by address or address pattern. Returns permits matching the address search.",
            inputSchema={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Address or part of address to search for (e.g., '100 Main St' or 'Main')"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default: 50)",
                        "default": 50
                    }
                },
                "required": ["address"]
            }
        ),
        Tool(
            name="search_permits_by_date_range",
            description="Search for permits issued within a date range. Dates should be in YYYY-MM-DD format.",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Start date in YYYY-MM-DD format (e.g., '2024-01-01')"
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date in YYYY-MM-DD format (e.g., '2024-12-31')"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default: 50)",
                        "default": 50
                    }
                },
                "required": ["start_date", "end_date"]
            }
        ),
        Tool(
            name="search_permits_by_neighborhood",
            description="Search for permits in a specific neighborhood.",
            inputSchema={
                "type": "object",
                "properties": {
                    "neighborhood": {
                        "type": "string",
                        "description": "Neighborhood name (e.g., 'Fells Point', 'Canton')"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default: 50)",
                        "default": 50
                    }
                },
                "required": ["neighborhood"]
            },
            outputSchema=PERMIT_LIST_OUTPUT_SCHEMA
        ),
        Tool(
            name="search_permits_by_case_number",
            description="Get a specific permit by case number (e.g., 'COM2018-86246').",
            inputSchema={
                "type": "object",
                "properties": {
                    "case_number": {
                        "type": "string",
                        "description": "Permit case number (e.g., 'COM2018-86246')"
                    }
                },
                "required": ["case_number"]
            }
        ),
        Tool(
            name="get_recent_permits",
            description="Get recently issued permits. Returns the most recent permits up to the specified limit.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default: 50)",
                        "default": 50
                    },
                    "days": {
                        "type": "integer",
                        "description": "Number of days to look back (default: 30)",
                        "default": 30
                    }
                }
            }
        ),
        Tool(
            name="count_permits",
            description="Count permits matching a query. Useful for checking how many permits match criteria before fetching details.",
            inputSchema={
                "type": "object",
                "properties": {
                    "where_clause": {
                        "type": "string",
                        "description": "SQL WHERE clause for filtering (e.g., \"Address LIKE '%Main%'\", \"Council_District = 1\")"
                    }
                },
                "required": ["where_clause"]
            }
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> Sequence[TextContent | EmbeddedResource]:
    """Handle tool calls."""
    
    if name == "search_permits_by_address":
        address = arguments.get("address", "")
        limit = arguments.get("limit", 50)

        # Build WHERE clause - handle partial matches
        escaped_address = address.replace("'", "''") if address else ""
        where = f"Address LIKE '%{escaped_address}%'" if address else "1=1"

        data = await query_permit_api_paginated(where_clause=where, max_records=limit)

        if not data.get("features"):
            return [TextContent(
                type="text",
                text=f"No permits found matching address: {address}"
            )]

        results = [format_permit(feat) for feat in data["features"]]
        total = data.get("totalRetrieved", len(results))
        result_text = f"Found {total} permit(s) matching '{address}':\n\n" + "\n".join(results)

        return [TextContent(type="text", text=result_text)]
    
    elif name == "search_permits_by_date_range":
        start_date = arguments.get("start_date", "")
        end_date = arguments.get("end_date", "")
        limit = arguments.get("limit", 50)

        # Validate dates; strptime also guarantees only digits reach the query.
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError as e:
            return [TextContent(
                type="text",
                text=f"Invalid date format. Please use YYYY-MM-DD format. Error: {str(e)}"
            )]

        # ArcGIS standardized SQL date literals; the service rejects epoch-ms
        # comparisons. The end date is inclusive (through 23:59:59).
        where = (
            f"IssuedDate >= TIMESTAMP '{start:%Y-%m-%d} 00:00:00' "
            f"AND IssuedDate <= TIMESTAMP '{end:%Y-%m-%d} 23:59:59'"
        )

        data = await query_permit_api_paginated(where_clause=where, max_records=limit)
        
        if not data.get("features"):
            return [TextContent(
                type="text",
                text=f"No permits found for date range {start_date} to {end_date}"
            )]
        
        results = [format_permit(feat) for feat in data["features"]]
        total = data.get("totalRetrieved", len(results))
        result_text = f"Found {total} permit(s) from {start_date} to {end_date}:\n\n" + "\n".join(results)

        return [TextContent(type="text", text=result_text)]

    elif name == "search_permits_by_neighborhood":
        neighborhood = arguments.get("neighborhood", "")
        limit = arguments.get("limit", 50)

        escaped_neighborhood = neighborhood.replace("'", "''")
        where = f"Neighborhood LIKE '%{escaped_neighborhood}%'"

        data = await query_permit_api_paginated(where_clause=where, max_records=limit)
        features = data.get("features") or []

        # outputSchema is declared for this tool, so every path returns
        # (text for the model, structured data validated against the schema).
        structured = {
            "query": neighborhood,
            "total": len(features),
            "permits": [to_record(feat) for feat in features],
        }

        if not features:
            return (
                [TextContent(type="text", text=f"No permits found in neighborhood: {neighborhood}")],
                structured,
            )

        results = [format_permit(feat) for feat in features]
        result_text = f"Found {len(features)} permit(s) in '{neighborhood}':\n\n" + "\n".join(results)

        return [TextContent(type="text", text=result_text)], structured
    
    elif name == "search_permits_by_case_number":
        case_number = arguments.get("case_number", "")
        
        escaped_case_number = case_number.replace("'", "''")
        where = f"CaseNumber = '{escaped_case_number}'"
        
        data = await query_permit_api(where_clause=where)
        
        if not data.get("features"):
            return [TextContent(
                type="text",
                text=f"No permit found with case number: {case_number}"
            )]
        
        result = format_permit(data["features"][0])
        return [TextContent(type="text", text=f"Permit details:\n\n{result}")]
    
    elif name == "get_recent_permits":
        limit = arguments.get("limit", 50)
        days = arguments.get("days", 30)

        # Calculate date threshold
        cutoff_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=int(days))

        where = f"IssuedDate >= TIMESTAMP '{cutoff_date:%Y-%m-%d %H:%M:%S}'"

        # Get more records to sort properly (fetch 2x the limit, then sort)
        data = await query_permit_api_paginated(where_clause=where, max_records=limit * 2)

        if not data.get("features"):
            return [TextContent(
                type="text",
                text=f"No permits found in the last {days} days"
            )]

        # Sort by IssuedDate descending and take the limit
        features = sorted(
            data["features"],
            key=lambda x: x.get("properties", {}).get("IssuedDate", 0),
            reverse=True
        )[:limit]

        results = [format_permit(feat) for feat in features]
        result_text = f"Found {len(results)} recent permit(s) (last {days} days):\n\n" + "\n".join(results)
        
        return [TextContent(type="text", text=result_text)]
    
    elif name == "count_permits":
        where_clause = arguments.get("where_clause", "1=1")
        
        data = await query_permit_api(where_clause=where_clause, return_count_only=True)
        
        count = data.get("properties", {}).get("count", 0) if "properties" in data else data.get("count", 0)
        
        return [TextContent(
            type="text",
            text=f"Found {count} permit(s) matching the query: {where_clause}"
        )]
    
    else:
        raise ValueError(f"Unknown tool: {name}")


async def main():
    """Main entry point for the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
