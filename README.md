# Baltimore Permit MCP Server

An MCP (Model Context Protocol) server that provides access to Baltimore City's housing and building permit data from the [Baltimore Open Data Portal](https://baltegis.baltimorecity.gov/mapping/rest/services/Housing/DHCD_Open_Baltimore_Datasets/FeatureServer/3).

## Features

This MCP server provides tools to query Baltimore's building permit database:

- **Search by Address**: Find permits for specific addresses or address patterns
- **Search by Date Range**: Get permits issued within a date range
- **Search by Neighborhood**: Find all permits in a specific neighborhood
- **Search by Case Number**: Look up a specific permit by its case number
- **Recent Permits**: Get the most recently issued permits, newest first
- **Count Permits**: Count permits matching specific criteria
- **Search Near a Point**: Find permits within a radius of a lat/lon, most recent first, with distances

## Installation

1. Install Python 3.10 or higher (Python 3.11 recommended):
   ```bash
   brew install python@3.11
   ```

2. Install dependencies:
   ```bash
   pip3.11 install -r requirements.txt
   ```

## Configuration

No configuration is required. The server connects directly to Baltimore's public API endpoint. Two optional environment variables override the services it uses, in case the city moves them again:

- `BALTIMORE_PERMIT_API`: the permit layer's query URL (default: the baltegis endpoint under [Data Source](#data-source)). `test_server.py` reads the same variable, so the tests check the endpoint the server will use.
- `BALTIMORE_BOUNDARY_LAYER`: the city boundary layer `search_permits_near` checks points against (default: the council-district layer at `https://baltegis.baltimorecity.gov/mapping/rest/services/Planning/Boundaries/MapServer/4/query`).

## Usage

### Running the Server

Run the server using stdio (standard input/output):

```bash
python3.11 server.py
```

Or use the provided run script:

```bash
./run_server.sh
```

### Testing

Run the comprehensive test suite:

```bash
python3.11 test_comprehensive.py
```

Or the quick live check against the permit API, which prints the endpoint it tests and fails if the API returns an error or no recent permits:

```bash
python3.11 test_server.py
```

### MCP Client Configuration

To use this server with an MCP client (like Claude Desktop), add it to your MCP configuration file:

**For Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "balt-permit": {
      "command": "python3.11",
      "args": ["/path/to/balt-permit-mcp-server/server.py"]
    }
  }
}
```

Replace `/path/to/balt-permit-mcp-server` with the actual absolute path to this directory.

**For other MCP clients**, configure according to your client's documentation.

## Available Tools

### `search_permits_by_address`

Search for permits by address or address pattern.

**Parameters:**
- `address` (required): Address or part of address to search (e.g., "100 Main St" or "Main"). Matching is case-insensitive and literal: `%`, `_` and `[` match only themselves. A blank address is an error.
- `limit` (optional): Maximum results to return (default: 50, min: 1, max: 2000)

**Example:**
```json
{
  "address": "Pratt St",
  "limit": 20
}
```

### `search_permits_by_date_range`

Search for permits issued within a date range.

**Parameters:**
- `start_date` (required): Start date in YYYY-MM-DD format
- `end_date` (required): End date in YYYY-MM-DD format. The end date is included.

An invalid date, or a start date after the end date, is a tool error.
- `limit` (optional): Maximum results to return (default: 50, min: 1, max: 2000)

**Example:**
```json
{
  "start_date": "2024-01-01",
  "end_date": "2024-12-31",
  "limit": 100
}
```

Dates are calendar days in Baltimore time. Results are not sorted, so when more permits match than `limit`, which ones are returned is up to the service; use `get_recent_permits` for the newest.

### `search_permits_by_neighborhood`

Search for permits in a specific neighborhood.

**Parameters:**
- `neighborhood` (required): Neighborhood name or part of one (e.g., "Fells Point", "Canton"). Matching is case-insensitive and literal, as for addresses. A blank neighborhood is an error.
- `limit` (optional): Maximum results to return (default: 50, min: 1, max: 2000)

**Example:**
```json
{
  "neighborhood": "Fells Point",
  "limit": 50
}
```

Returns readable text plus structured content (`{query, total, permits[]}`) validated against the tool's `outputSchema`.

### `search_permits_near`

Find permits within a radius of a point, most recently issued first. Each result includes `distance_m` from the point. Returns readable text plus structured content validated against the tool's `outputSchema`.

**Parameters:**
- `lat` (required): Latitude, WGS84 decimal degrees (e.g., 39.2763)
- `lon` (required): Longitude, WGS84 decimal degrees (e.g., -76.5688)
- `radius_m` (optional): Search radius in meters (default: 250, min: 1, max: 2000)
- `limit` (optional): Maximum results to return (default: 25, min: 1, max: 200)

Points outside Baltimore City return an error rather than an empty result, so a wrong coordinate is not mistaken for "no permits here." The server checks the point against the city's boundary (its council districts): a point is accepted if the search circle reaches city land, so a point in the harbor near the shore still works, while points in Towson, Brooklyn Park or Dundalk are rejected. If the boundary service is unavailable, a rectangle around the city limits is used instead.

Out-of-range `radius_m` or `limit` values are errors; they are not silently clamped. Results are deduplicated and ordered by issue date on the server. When the text says the result limit was reached, there may be more permits in the radius; narrow it to see older ones.

**Example:**
```json
{
  "lat": 39.2763,
  "lon": -76.5688,
  "radius_m": 150,
  "limit": 10
}
```

### `search_permits_by_case_number`

Get a specific permit by its case number.

**Parameters:**
- `case_number` (required): Permit case number (e.g., "COM2018-86246")

**Example:**
```json
{
  "case_number": "COM2018-86246"
}
```

### `get_recent_permits`

Get the most recently issued permits, newest first. Sorting is done by the service, so these are the newest permits in the window, not a sorted sample.

**Parameters:**
- `limit` (optional): Maximum results to return (default: 50, min: 1, max: 2000)
- `days` (optional): Number of days to look back, counting from midnight today in Baltimore (default: 30, min: 1, max: 3650)

**Example:**
```json
{
  "limit": 25,
  "days": 7
}
```

### `count_permits`

Count permits matching a SQL WHERE clause. The server checks the clause before sending it and returns an error unless it uses only:

- the permit layer's fields, case-insensitive. They are read from the layer's metadata, so an endpoint set with `BALTIMORE_PERMIT_API` uses its own fields; the tool description lists them. For the default layer that is the fields under [Permit Data Fields](#permit-data-fields) plus `OBJECTID`, `PermitName`, `csm_projname`, `prc_block_no`, `prc_lot` and `HousingMarketTypology2017`. If the metadata can't be read, that default list is used.
- `'quoted strings'` (double a quote to include one) and numbers
- `=`, `<>`, `!=`, `<`, `<=`, `>`, `>=`, `+`, `-` and parentheses
- `AND`, `OR`, `NOT`, `IN`, `BETWEEN`, `LIKE` (with optional `ESCAPE`) and `IS [NOT] NULL`
- `UPPER()`, `LOWER()`, `CURRENT_DATE` and `CURRENT_TIMESTAMP`. The last two are replaced with today's date and the current time in Baltimore before the clause is sent, because the service evaluates `CURRENT_DATE` in UTC (tomorrow, in Baltimore, from 7 or 8 pm). The reply shows the clause as sent.
- `DATE 'YYYY-MM-DD'` or `TIMESTAMP 'YYYY-MM-DD HH:MM:SS'` for dates; numeric timestamps are rejected with a hint, since the service does not accept them

Subqueries, other tables, comments, semicolons and clauses over 1,000 characters are rejected.

**Parameters:**
- `where_clause` (required): SQL WHERE clause for filtering

**Example:**
```json
{
  "where_clause": "Council_District = 1 AND Cost > 10000"
}
```

## Data Source

This server connects to Baltimore City's ArcGIS FeatureServer:

- **Endpoint**: `https://baltegis.baltimorecity.gov/mapping/rest/services/Housing/DHCD_Open_Baltimore_Datasets/FeatureServer/3`
- **Format**: GeoJSON
- **Data**: Building Permits from the Department of Housing & Community Development (DHCD)

## Permit Data Fields

Each permit includes the following information:

- **CaseNumber**: Unique permit case number
- **Address**: Property address
- **Neighborhood**: Neighborhood name
- **IssuedDate**: Date permit was issued (shown as YYYY-MM-DD in Baltimore time)
- **ExpirationDate**: Date permit expires (shown as YYYY-MM-DD in Baltimore time)
- **Cost**: Estimated project cost
- **Description**: Detailed work description
- **BLOCKLOT**: Block and lot number
- **Council_District**: City council district number
- **ExistingUse**: Existing land use code
- **ProposedUse**: Proposed land use code
- **IsPermitModification**: Whether this is a permit modification
- **Location**: Geographic coordinates (latitude, longitude)

## License

This MCP server is provided as-is. The data source is Baltimore City's open data, available under their [Open Data Policy](https://technology.baltimorecity.gov/open-data-policy).

## Notes

- Results are paged automatically up to `limit`, using the service's "more records" flag wherever it appears (ArcGIS Online reports it only under `properties`).
- When the service rejects a query, the tool returns an error with the service's message instead of reporting "No permits found."
- The service stores dates as midnight in Baltimore time (America/New_York) and reads date literals in that zone. The server converts dates in that zone too, so output is the same whatever time zone the server runs in.
- Address and neighborhood searches are case-insensitive partial matches, and wildcard characters in the search text are matched literally.

## Support

For issues or questions about this MCP server, please open an issue in the repository.

For questions about the underlying data, contact Baltimore City's open data team.
