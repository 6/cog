import http.client
import json
import urllib.parse


def _connect(parsed_url):
    if parsed_url.scheme == "https":
        return http.client.HTTPSConnection(parsed_url.hostname, parsed_url.port or 443, timeout=30)
    return http.client.HTTPConnection(parsed_url.hostname, parsed_url.port or 80, timeout=30)


def _post_jsonrpc(server, method, params=None, is_notification=False):
    parsed = urllib.parse.urlparse(server["url"])
    path = parsed.path or "/"
    body = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if not is_notification:
        server["_next_id"] = server.get("_next_id", 0) + 1
        body["id"] = server["_next_id"]

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if server.get("_session_id"):
        headers["Mcp-Session-Id"] = server["_session_id"]
    for k, v in server.get("headers", {}).items():
        headers[k] = v

    conn = _connect(parsed)
    conn.request("POST", path, json.dumps(body), headers)
    resp = conn.getresponse()

    if is_notification:
        resp.read()
        conn.close()
        return None

    ct = resp.getheader("Content-Type", "")
    if "text/event-stream" in ct:
        result = _read_sse_response(resp, body.get("id"))
    else:
        raw = resp.read().decode("utf-8", errors="replace")
        result = json.loads(raw)

    session_id = resp.getheader("Mcp-Session-Id")
    if session_id:
        server["_session_id"] = session_id

    conn.close()
    return result


def _read_sse_response(resp, request_id):
    result = None
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith("data: "):
            data = json.loads(line[6:])
            if isinstance(data, dict) and data.get("id") == request_id:
                result = data
                break
    resp.read()
    return result


def initialize(server_config):
    server = dict(server_config)
    server["_next_id"] = 0
    server["_session_id"] = None

    result = _post_jsonrpc(server, "initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {"tools": {}},
        "clientInfo": {"name": "cog", "version": "0.1.0"},
    })

    if result and "error" in result:
        raise MCPError(f"initialize failed: {result['error']}")

    _post_jsonrpc(server, "notifications/initialized", is_notification=True)
    return server


def list_tools(server):
    result = _post_jsonrpc(server, "tools/list", {})
    if not result or "error" in result:
        return []
    return result.get("result", {}).get("tools", [])


def call_tool(server, tool_name, arguments):
    result = _post_jsonrpc(server, "tools/call", {
        "name": tool_name,
        "arguments": arguments,
    })
    if not result:
        return "ERROR: no response from MCP server"
    if "error" in result:
        err = result["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        return f"ERROR: {msg}"
    content = result.get("result", {}).get("content", [])
    parts = []
    for item in content:
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
        else:
            parts.append(json.dumps(item))
    return "\n".join(parts) if parts else "(empty result)"


def discover_all(mcp_configs):
    if not mcp_configs:
        return {}, []
    servers = []
    tools = {}
    multi = len(mcp_configs) > 1

    for cfg in mcp_configs:
        name = cfg.get("name", "mcp")
        try:
            server = initialize(cfg)
            server["name"] = name
            mcp_tools = list_tools(server)
            servers.append(server)
            for t in mcp_tools:
                tool_name = f"{name}__{t['name']}" if multi else t["name"]
                schema = {
                    "name": tool_name,
                    "description": t.get("description", ""),
                    "input_schema": t.get("inputSchema", {"type": "object", "properties": {}}),
                }
                tools[tool_name] = ("mcp", server, schema, t["name"])
        except Exception as e:
            import sys
            print(f"Warning: MCP server '{name}' failed: {e}", file=sys.stderr)

    return tools, servers


class MCPError(Exception):
    pass
