#!/usr/bin/env python3
"""cog - minimal coding agent. Stdlib-only, Python 3.9+."""

from __future__ import annotations

import argparse
import atexit
import base64
import fcntl
import hashlib
import http.client
import http.server
import json
import os
import queue
import re
import secrets
import select
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# --- Tools ---

_cwd = "."
_shell_timeout = 30


def tools_configure(cwd=".", shell_timeout=30):
    global _cwd, _shell_timeout
    _cwd = os.path.abspath(cwd)
    _shell_timeout = shell_timeout


def _resolve(path):
    p = Path(path)
    if not p.is_absolute():
        p = Path(_cwd) / p
    resolved = str(p.resolve())
    # Boundary check must not allow a co-named sibling like `/cwd2/x` to pass
    # when _cwd is `/cwd`. Accept the cwd itself or any path under `cwd + sep`.
    if resolved != _cwd and not resolved.startswith(_cwd + os.sep):
        raise ValueError(f"path escapes working directory: {path}")
    return resolved


def tool_read_file(args):
    path = args.get("path", "?")
    try:
        path = _resolve(path)
        with open(path, "rb") as f:
            sample = f.read(8192)
        if b"\x00" in sample:
            return f"ERROR: {path} appears to be a binary file"
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except PermissionError:
        return f"ERROR: permission denied: {path}"
    except Exception as e:
        return f"ERROR: {e}"


def tool_list_dir(args):
    try:
        path = _resolve(args.get("path", "."))
        entries = sorted(os.listdir(path))
    except ValueError as e:
        return f"ERROR: {e}"
    except FileNotFoundError:
        return f"ERROR: directory not found: {args.get('path', '.')}"
    except PermissionError:
        return f"ERROR: permission denied: {args.get('path', '.')}"
    lines = []
    for name in entries:
        full = os.path.join(path, name)
        kind = "[dir] " if os.path.isdir(full) else "[file]"
        lines.append(f"{kind} {name}")
    return "\n".join(lines) if lines else "(empty directory)"


def tool_write_file(args):
    try:
        path = _resolve(args["path"])
        content = args["content"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"OK: wrote {len(content.encode('utf-8'))} bytes to {path}"
    except Exception as e:
        return f"ERROR: {e}"


def tool_str_replace(args):
    try:
        path = _resolve(args["path"])
        old_str, new_str = args["old_str"], args["new_str"]
        content = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"ERROR: file not found: {args.get('path', '?')}"
    except Exception as e:
        return f"ERROR: {e}"
    count = content.count(old_str)
    if count == 0:
        return "ERROR: old_str not found in file"
    if count > 1:
        return (
            f"ERROR: old_str matched {count} times, must be unique. "
            "Add more surrounding context to make it unique."
        )
    Path(path).write_text(content.replace(old_str, new_str, 1), encoding="utf-8")
    return "OK: replacement made"


def tool_run_shell(args):
    try:
        r = subprocess.run(
            args["command"],
            shell=True,
            capture_output=True,
            text=True,
            timeout=_shell_timeout,
            cwd=_cwd,
        )
        out = (r.stdout or "") + (r.stderr or "") + f"\n[exit code: {r.returncode}]"
        return out.strip()
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {_shell_timeout}s"
    except Exception as e:
        return f"ERROR: {e}"


def _schema(name, desc, props, required):
    return {
        "name": name,
        "description": desc,
        "input_schema": {"type": "object", "properties": props, "required": required},
    }


_TOOL_DEFS = {
    "read_file": (
        tool_read_file,
        _schema(
            "read_file",
            "Read the contents of a file at the given path.",
            {
                "path": {
                    "type": "string",
                    "description": "File path to read (relative to cwd or absolute)",
                }
            },
            ["path"],
        ),
    ),
    "list_dir": (
        tool_list_dir,
        _schema(
            "list_dir",
            "List directory contents with type indicators ([dir] or [file]).",
            {
                "path": {
                    "type": "string",
                    "description": "Directory path (default: current directory)",
                }
            },
            [],
        ),
    ),
    "write_file": (
        tool_write_file,
        _schema(
            "write_file",
            "Write content to a file, creating parent directories if needed.",
            {
                "path": {"type": "string", "description": "File path to write"},
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            ["path", "content"],
        ),
    ),
    "str_replace": (
        tool_str_replace,
        _schema(
            "str_replace",
            "Replace an exact string match in a file. The old_str must appear exactly once. "
            "Include enough surrounding context lines in old_str to make the match unique.",
            {
                "path": {"type": "string", "description": "File path to edit"},
                "old_str": {
                    "type": "string",
                    "description": "Exact string to find (must appear exactly once)",
                },
                "new_str": {
                    "type": "string",
                    "description": "String to replace it with",
                },
            },
            ["path", "old_str", "new_str"],
        ),
    ),
    "run_shell": (
        tool_run_shell,
        _schema(
            "run_shell",
            "Run a shell command and return combined stdout and stderr with exit code.",
            {"command": {"type": "string", "description": "Shell command to execute"}},
            ["command"],
        ),
    ),
}


def get_tools():
    return {name: ("builtin", fn, schema) for name, (fn, schema) in _TOOL_DEFS.items()}


# --- Anthropic API ---


class APIError(Exception):
    def __init__(self, status, body, retry_after=None):
        self.status, self.body, self.retry_after = status, body, retry_after
        self.retryable = status in (429, 500, 502, 503, 529)
        super().__init__(f"API error {status}: {body}")


def build_request(model, system, messages, tools, max_tokens=4096):
    req = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "stream": True,
    }
    if tools:
        req["tools"] = [entry[2] for entry in tools.values()]
    return req


def stream_request(api_key, request_body, api_base_url="https://api.anthropic.com"):
    parsed = urllib.parse.urlparse(api_base_url)
    host = parsed.hostname
    if not host:
        raise ValueError(f"invalid api_base_url (no host): {api_base_url}")
    port = parsed.port
    base_path = (parsed.path or "").rstrip("/")
    if parsed.scheme == "https":
        conn = http.client.HTTPSConnection(
            host, port or 443, timeout=120, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(host, port or 80, timeout=120)
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": api_key,
        "anthropic-version": "2023-06-01",
    }
    conn.request("POST", f"{base_path}/v1/messages", json.dumps(request_body), headers)
    resp = conn.getresponse()
    if resp.status != 200:
        retry_after = resp.getheader("retry-after")
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        try:
            ra = float(retry_after) if retry_after else None
        except (ValueError, TypeError):
            ra = None
        raise APIError(resp.status, body, ra)
    return resp, conn


def parse_sse_stream(response) -> Iterator[tuple[str, Any]]:
    event_type = None
    block_type = block_id = block_name = None
    block_index = -1
    json_accum = text_accum = ""
    usage = {"input_tokens": 0, "output_tokens": 0}

    while True:
        raw = response.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith("event: "):
            event_type = line[7:]
            continue
        if not line.startswith("data: "):
            continue
        data = json.loads(line[6:])

        if event_type == "message_start":
            u = data.get("message", {}).get("usage", {})
            usage["input_tokens"] = u.get("input_tokens", 0)
        elif event_type == "content_block_start":
            cb = data.get("content_block", {})
            block_index = data.get("index", block_index + 1)
            block_type = cb.get("type")
            text_accum = json_accum = ""
            if block_type == "tool_use":
                block_id, block_name = cb.get("id"), cb.get("name")
        elif event_type == "content_block_delta":
            delta = data.get("delta", {})
            dtype = delta.get("type")
            if dtype == "text_delta":
                text = delta.get("text", "")
                text_accum += text
                yield ("text_delta", text)
            elif dtype == "input_json_delta":
                json_accum += delta.get("partial_json", "")
            elif dtype == "thinking_delta":
                text_accum += delta.get("thinking", "")
        elif event_type == "content_block_stop":
            if block_type == "tool_use":
                try:
                    tool_input = json.loads(json_accum) if json_accum else {}
                except json.JSONDecodeError:
                    tool_input = {"_raw": json_accum}
                yield (
                    "block_done",
                    {
                        "type": "tool_use",
                        "id": block_id,
                        "name": block_name,
                        "input": tool_input,
                        "index": block_index,
                    },
                )
                yield (
                    "tool_use",
                    {"id": block_id, "name": block_name, "input": tool_input},
                )
            elif block_type == "text":
                yield (
                    "block_done",
                    {"type": "text", "text": text_accum, "index": block_index},
                )
                yield ("text_final", text_accum)
            elif block_type == "thinking":
                yield (
                    "block_done",
                    {"type": "thinking", "thinking": text_accum, "index": block_index},
                )
            else:
                yield ("block_done", {"type": block_type, "index": block_index})
            block_type = block_id = block_name = None
        elif event_type == "message_delta":
            usage["output_tokens"] = data.get("usage", {}).get("output_tokens", 0)
            yield ("stop", data.get("delta", {}).get("stop_reason", "end_turn"))
        elif event_type == "message_stop":
            yield ("usage", dict(usage))
        event_type = None


# --- MCP Client ---


class MCPError(Exception):
    pass


class MCPAuthRequired(MCPError):
    def __init__(self, name):
        self.name = name
        super().__init__(f"MCP server '{name}' requires auth (run /mcp auth {name})")


def _mcp_connect(parsed_url):
    if parsed_url.scheme == "https":
        return http.client.HTTPSConnection(
            parsed_url.hostname, parsed_url.port or 443, timeout=30
        )
    return http.client.HTTPConnection(
        parsed_url.hostname, parsed_url.port or 80, timeout=30
    )


def _mcp_post(server, method, params=None, is_notification=False):
    parsed = urllib.parse.urlparse(server["url"])
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
    if server.get("_oauth_token"):
        headers["Authorization"] = f"Bearer {server['_oauth_token']}"
    for k, v in server.get("headers", {}).items():
        headers[k] = v
    conn = _mcp_connect(parsed)
    conn.request("POST", parsed.path or "/", json.dumps(body), headers)
    resp = conn.getresponse()
    if resp.status == 401:
        www_auth = resp.getheader("WWW-Authenticate", "")
        resp.read()
        conn.close()
        server["_www_authenticate"] = www_auth
        # Try refresh first
        if not server.get("_refresh_attempted"):
            server["_refresh_attempted"] = True
            new_token = _mcp_try_refresh(server)
            if new_token:
                server["_oauth_token"] = new_token
                server["_refresh_attempted"] = False
                return _mcp_post(server, method, params, is_notification)
        # Interactive auth if triggered by /mcp auth
        if server.get("_oauth_interactive"):
            server["_oauth_interactive"] = False
            server["_refresh_attempted"] = False
            token = _mcp_oauth_flow(server)
            server["_oauth_token"] = token
            return _mcp_post(server, method, params, is_notification)
        raise MCPAuthRequired(server.get("name", "mcp"))
    if is_notification:
        resp.read()
        conn.close()
        return None
    ct = resp.getheader("Content-Type", "")
    if "text/event-stream" in ct:
        result = _mcp_read_sse(resp, body.get("id"))
    else:
        result = json.loads(resp.read().decode("utf-8", errors="replace"))
    sid = resp.getheader("Mcp-Session-Id")
    if sid:
        server["_session_id"] = sid
    conn.close()
    return result


def _mcp_read_sse(resp, request_id):
    while True:
        line = resp.readline()
        if not line:
            break
        line = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith("data: "):
            data = json.loads(line[6:])
            if isinstance(data, dict) and data.get("id") == request_id:
                resp.read()
                return data
    resp.read()
    return None


# --- MCP OAuth 2.1 ---


def _mcp_auth_base(url):
    """Strip path from MCP URL to get auth base URL."""
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.hostname}" + (f":{p.port}" if p.port else "")


def _mcp_http_get(url):
    p = urllib.parse.urlparse(url)
    if p.scheme == "https":
        conn = http.client.HTTPSConnection(
            p.hostname, p.port or 443, timeout=10, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=10)
    conn.request("GET", p.path or "/", headers={"MCP-Protocol-Version": "2025-03-26"})
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    conn.close()
    return resp.status, body


def _mcp_http_post_form(url, data):
    p = urllib.parse.urlparse(url)
    if p.scheme == "https":
        conn = http.client.HTTPSConnection(
            p.hostname, p.port or 443, timeout=10, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=10)
    body = urllib.parse.urlencode(data)
    conn.request(
        "POST",
        p.path or "/",
        body,
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = conn.getresponse()
    result = resp.read().decode("utf-8", errors="replace")
    conn.close()
    return resp.status, result


def _mcp_http_post_json(url, data):
    p = urllib.parse.urlparse(url)
    if p.scheme == "https":
        conn = http.client.HTTPSConnection(
            p.hostname, p.port or 443, timeout=10, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=10)
    conn.request(
        "POST", p.path or "/", json.dumps(data), {"Content-Type": "application/json"}
    )
    resp = conn.getresponse()
    result = resp.read().decode("utf-8", errors="replace")
    conn.close()
    return resp.status, result


def _mcp_token_path(name):
    d = os.path.expanduser("~/.cog/tokens")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{name}.json")


def _mcp_load_token(name):
    path = _mcp_token_path(name)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
        if data.get("access_token"):
            return data
    except Exception:
        pass
    return None


def _mcp_save_token(name, data):
    path = _mcp_token_path(name)
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(data, f)
        f.flush()


def _mcp_parse_www_auth(header):
    """Extract resource_metadata URI from WWW-Authenticate header."""
    for part in header.split(","):
        part = part.strip()
        if "resource_metadata=" in part:
            val = part.split("resource_metadata=", 1)[1].strip().strip('"')
            return val
    return None


def _mcp_discover_auth_server(server_url, www_auth=""):
    """Discover OAuth authorization server metadata per MCP spec."""
    base = _mcp_auth_base(server_url)
    resource = server_url
    # Try Protected Resource Metadata from WWW-Authenticate
    res_meta_url = _mcp_parse_www_auth(www_auth) if www_auth else None
    if not res_meta_url:
        res_meta_url = f"{base}/.well-known/oauth-protected-resource"
    status, body = _mcp_http_get(res_meta_url)
    if status == 200:
        try:
            res_meta = json.loads(body)
            resource = res_meta.get("resource", resource)
            auth_servers = res_meta.get("authorization_servers", [])
            if auth_servers:
                as_url = auth_servers[0]
                status2, body2 = _mcp_http_get(
                    f"{as_url}/.well-known/oauth-authorization-server"
                )
                if status2 == 200:
                    return json.loads(body2), resource
        except (json.JSONDecodeError, KeyError):
            pass
    # Fallback: try AS metadata directly on base URL
    status, body = _mcp_http_get(f"{base}/.well-known/oauth-authorization-server")
    if status == 200:
        try:
            return json.loads(body), resource
        except json.JSONDecodeError:
            pass
    # Fallback: default endpoints
    return {
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
    }, resource


def _mcp_oauth_flow(server_cfg):
    """Run OAuth 2.1 PKCE flow for an MCP server. Returns access token string."""
    name = server_cfg.get("name", "mcp")
    server_url = server_cfg["url"]

    # Check cached token
    cached = _mcp_load_token(name)
    if cached and cached.get("access_token"):
        return cached["access_token"]

    # Discover auth server (with Protected Resource Metadata support)
    www_auth = server_cfg.get("_www_authenticate", "")
    meta, resource = _mcp_discover_auth_server(server_url, www_auth)
    auth_ep = meta.get("authorization_endpoint")
    token_ep = meta.get("token_endpoint")
    reg_ep = meta.get("registration_endpoint")

    # Find free port BEFORE registration so redirect_uri is consistent
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    redirect_uri = f"http://localhost:{port}/callback"

    # Dynamic client registration
    if reg_ep:
        status, body = _mcp_http_post_json(
            reg_ep,
            {
                "client_name": "cog",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        if status in (200, 201):
            reg = json.loads(body)
            client_id = reg["client_id"]
        else:
            raise MCPError(f"Dynamic client registration failed ({status}): {body}")
    else:
        raise MCPError("No registration endpoint and no client_id configured")

    # PKCE
    verifier = secrets.token_urlsafe(43)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )

    auth_code: list[str | None] = [None]

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            auth_code[0] = qs.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Authorization complete.</h2>"
                b"<p>You can close this tab.</p></body></html>"
            )

        def log_message(self, format: str, *args: object) -> None:
            pass

    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": resource,
        }
    )
    print(f"Opening browser for {name} authorization...", file=sys.stderr)
    webbrowser.open(f"{auth_ep}?{params}")

    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    srv.timeout = 120
    while auth_code[0] is None:
        srv.handle_request()
    srv.server_close()

    if not auth_code[0]:
        raise MCPError("OAuth callback did not receive authorization code")

    # Exchange code for token
    status, body = _mcp_http_post_form(
        token_ep,
        {
            "grant_type": "authorization_code",
            "code": auth_code[0],
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "client_id": client_id,
            "resource": resource,
        },
    )
    if status != 200:
        raise MCPError(f"Token exchange failed ({status}): {body}")
    token_data = json.loads(body)
    token_data["client_id"] = client_id
    token_data["resource"] = resource
    _mcp_save_token(name, token_data)
    return token_data["access_token"]


def _mcp_try_refresh(server):
    """Try to refresh an OAuth token. Returns new access_token or None."""
    name = server.get("name", "mcp")
    cached = _mcp_load_token(name)
    if not cached or not cached.get("refresh_token"):
        return None
    www_auth = server.get("_www_authenticate", "")
    meta, resource = _mcp_discover_auth_server(server["url"], www_auth)
    token_ep = meta.get("token_endpoint")
    if not token_ep:
        return None
    resource = cached.get("resource", resource)
    status, body = _mcp_http_post_form(
        token_ep,
        {
            "grant_type": "refresh_token",
            "refresh_token": cached["refresh_token"],
            "client_id": cached.get("client_id", ""),
            "resource": resource,
        },
    )
    if status == 200:
        token_data = json.loads(body)
        token_data.setdefault("client_id", cached.get("client_id", ""))
        token_data.setdefault("resource", resource)
        if not token_data.get("refresh_token"):
            token_data["refresh_token"] = cached["refresh_token"]
        _mcp_save_token(name, token_data)
        return token_data["access_token"]
    return None


def mcp_initialize(cfg):
    server = dict(
        cfg, _next_id=0, _session_id=None, _oauth_attempted=False, _oauth_token=None
    )
    # Load cached OAuth token if available
    cached = _mcp_load_token(cfg.get("name", "mcp"))
    if cached and cached.get("access_token"):
        server["_oauth_token"] = cached["access_token"]
    result = _mcp_post(
        server,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "cog", "version": "0.1.0"},
        },
    )
    if result and "error" in result:
        raise MCPError(f"initialize failed: {result['error']}")
    _mcp_post(server, "notifications/initialized", is_notification=True)
    return server


def mcp_call_tool(server, tool_name, arguments):
    result = _mcp_post(
        server, "tools/call", {"name": tool_name, "arguments": arguments}
    )
    if not result:
        return "ERROR: no response from MCP server"
    if "error" in result:
        err = result["error"]
        return f"ERROR: {err.get('message', err) if isinstance(err, dict) else err}"
    parts = []
    for item in result.get("result", {}).get("content", []):
        parts.append(
            item.get("text", "") if item.get("type") == "text" else json.dumps(item)
        )
    return "\n".join(parts) if parts else "(empty result)"


def mcp_discover_all(mcp_configs):
    if not mcp_configs:
        return {}, [], []
    servers, tools, pending_auth, multi = [], {}, [], len(mcp_configs) > 1
    for cfg in mcp_configs:
        name = cfg.get("name", "mcp")
        try:
            server = mcp_initialize(cfg)
            server["name"] = name
            result = _mcp_post(server, "tools/list", {})
            mcp_tools = (
                (result or {}).get("result", {}).get("tools", [])
                if result and "error" not in result
                else []
            )
            servers.append(server)
            for t in mcp_tools:
                tname = f"{name}__{t['name']}" if multi else t["name"]
                tools[tname] = (
                    "mcp",
                    server,
                    {
                        "name": tname,
                        "description": t.get("description", ""),
                        "input_schema": t.get(
                            "inputSchema", {"type": "object", "properties": {}}
                        ),
                    },
                    t["name"],
                )
        except MCPAuthRequired:
            pending_auth.append(cfg)
        except Exception as e:
            print(f"Warning: MCP server '{name}' failed: {e}", file=sys.stderr)
    return tools, servers, pending_auth


# --- Agent ---


class Agent:
    def __init__(self, config, tool_registry, event_queue, input_queue):
        self.model = config.model
        self.api_key = config.api_key
        self.api_base_url = config.api_base_url
        self.system = config.system_prompt
        self.tools = tool_registry
        self.eq = event_queue
        self.iq = input_queue
        self.messages = []
        self.max_calls = config.max_tool_calls_per_turn
        self.max_output = config.tool_output_max_bytes
        self.auto_approve = config.auto_approve
        self.verbose = config.verbose
        self.log = config._log_fn

    def emit(self, event):
        event["ts"] = datetime.now(timezone.utc).isoformat()
        self.eq.put(event)
        if self.log and event["type"] != "assistant_text_delta":
            self.log(event)

    def run_turn(self, user_input):
        self.messages.append({"role": "user", "content": user_input})
        self.emit({"type": "user_message", "content": user_input})
        tool_count = 0
        while True:
            req = build_request(self.model, self.system, self.messages, self.tools)
            if self.verbose:
                self.emit({"type": "verbose", "data": json.dumps(req, indent=2)})
            resp = conn = None
            for attempt in range(5):
                try:
                    resp, conn = stream_request(self.api_key, req, self.api_base_url)
                    break
                except APIError as e:
                    if e.retryable and attempt < 4:
                        delay = e.retry_after or min(2**attempt, 30)
                        self.emit(
                            {
                                "type": "status",
                                "message": f"API error {e.status}, retrying in {delay:.0f}s...",
                            }
                        )
                        time.sleep(delay)
                        continue
                    self.emit({"type": "error", "message": str(e)})
                    return
                except Exception as e:
                    if attempt < 4:
                        delay = min(2**attempt, 30)
                        self.emit(
                            {
                                "type": "status",
                                "message": f"Network error, retrying in {delay:.0f}s...",
                            }
                        )
                        time.sleep(delay)
                        continue
                    self.emit({"type": "error", "message": f"Network error: {e}"})
                    return
            if resp is None or conn is None:
                return
            blocks, tool_uses, usage = [], [], {}
            try:
                for kind, payload in parse_sse_stream(resp):
                    if kind == "text_delta":
                        self.emit({"type": "assistant_text_delta", "text": payload})
                    elif kind == "text_final":
                        self.emit({"type": "assistant_text_final", "text": payload})
                    elif kind == "tool_use":
                        tool_uses.append(payload)
                        self.emit(
                            {
                                "type": "tool_call",
                                "tool_id": payload["id"],
                                "name": payload["name"],
                                "input": payload["input"],
                            }
                        )
                    elif kind == "block_done":
                        block = {k: v for k, v in payload.items() if k != "index"}
                        if block.get("type") == "text":
                            block["text"] = block.get("text", "").strip()
                        is_empty_text = block.get("type") == "text" and not block.get(
                            "text", ""
                        )
                        if not is_empty_text:
                            blocks.append(block)
                    elif kind == "usage":
                        usage = payload
            except Exception as e:
                self.emit({"type": "error", "message": f"Stream error: {e}"})
                return
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            content_blocks = blocks
            if content_blocks:
                self.messages.append({"role": "assistant", "content": content_blocks})
            if self.verbose:
                self.emit(
                    {
                        "type": "verbose",
                        "data": json.dumps(
                            {
                                "role": "assistant",
                                "content": content_blocks,
                                "usage": usage,
                            },
                            indent=2,
                        ),
                    }
                )
            if not tool_uses:
                self.emit({"type": "turn_complete", "usage": usage})
                return
            tool_results = []
            for tu in tool_uses:
                tool_count += 1
                if tool_count > self.max_calls:
                    tr = {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": "ERROR: maximum tool calls per turn exceeded",
                        "is_error": True,
                    }
                    tool_results.append(tr)
                    self.emit(
                        {
                            "type": "tool_result",
                            "tool_id": tu["id"],
                            "output": tr["content"],
                            "is_error": True,
                        }
                    )
                    continue
                output, is_error = self._dispatch(tu["name"], tu["input"])
                if len(output.encode("utf-8", errors="replace")) > self.max_output:
                    half = self.max_output // 2
                    output = (
                        output[:half] + "\n\n[... truncated ...]\n\n" + output[-half:]
                    )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": output,
                        "is_error": is_error,
                    }
                )
                if self.verbose:
                    self.emit({"type": "verbose", "data": output})
                self.emit(
                    {
                        "type": "tool_result",
                        "tool_id": tu["id"],
                        "output": output,
                        "is_error": is_error,
                    }
                )
            self.messages.append({"role": "user", "content": tool_results})
            if tool_count > self.max_calls:
                self.emit({"type": "turn_complete", "usage": usage})
                return

    def _dispatch(self, name, input_args):
        if name not in self.tools:
            return f"ERROR: unknown tool '{name}'", True
        entry = self.tools[name]
        if entry[0] == "builtin":
            _, fn, schema = entry
            if (
                name in ("write_file", "str_replace", "run_shell")
                and not self.auto_approve
            ):
                if not self._approve(name, input_args):
                    return "Tool call denied by user.", True
            try:
                r = fn(input_args)
                return r, r.startswith("ERROR:")
            except Exception as e:
                return f"ERROR: {e}", True
        elif entry[0] == "mcp":
            _, server, schema, real_name = entry
            if not self.auto_approve:
                if not self._approve(name, input_args):
                    return "Tool call denied by user.", True
            try:
                r = mcp_call_tool(server, real_name, input_args)
                return r, r.startswith("ERROR:")
            except Exception as e:
                return f"ERROR: MCP call failed: {e}", True
        return f"ERROR: unknown tool type for '{name}'", True

    def _approve(self, name, input_args):
        self.emit({"type": "approval_request", "name": name, "input": input_args})
        try:
            resp = self.iq.get(timeout=300)
            return (
                isinstance(resp, dict)
                and resp.get("type") == "approval"
                and resp.get("approved")
            )
        except queue.Empty:
            return False

    def worker_loop(self):
        while True:
            msg = self.iq.get()
            if msg is None:
                break
            if isinstance(msg, dict):
                if msg.get("type") == "switch_model":
                    self.model = msg["model"]
                    self.api_key = msg.get("api_key", "")
                    self.api_base_url = msg["api_base_url"]
                continue
            try:
                self.run_turn(msg)
            except Exception as e:
                self.emit({"type": "error", "message": f"Agent error: {e}"})


# --- TUI ---

_original_termios = None
_fd: int = 0  # stdin's well-known fd; overwritten by _set_cbreak()


def _set_cbreak():
    global _original_termios, _fd
    import termios
    import tty

    _fd = sys.stdin.fileno()
    _original_termios = termios.tcgetattr(_fd)
    tty.setcbreak(_fd)


def _restore_term():
    global _original_termios
    if _original_termios is not None:
        import termios

        try:
            termios.tcsetattr(_fd, termios.TCSADRAIN, _original_termios)
        except Exception:
            pass
        _original_termios = None
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()


def _summarize_args(args, max_len=120):
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 80:
            s = s[:77] + "..."
        parts.append(f'{k}="{s}"')
    r = ", ".join(parts)
    return r[: max_len - 3] + "..." if len(r) > max_len else r


def _git_branch(cwd):
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            cwd=cwd,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _out(s=""):
    sys.stdout.write(s + "\n")
    sys.stdout.flush()


def _out_inline(s):
    sys.stdout.write(s)
    sys.stdout.flush()


class TUI:
    def __init__(
        self,
        event_queue,
        input_queue,
        model="",
        cwd="",
        tool_count=0,
        token_threshold_warn=100000,
        token_threshold_danger=200000,
        models=None,
        mcp_servers=None,
        tool_reg=None,
        agent=None,
        pending_auth=None,
    ):
        self.eq, self.iq = event_queue, input_queue
        self.model, self.cwd, self.tool_count = model, cwd, tool_count
        self.models = models or {}
        self.mcp_servers = mcp_servers or []
        self._tool_reg = tool_reg if tool_reg is not None else {}
        self._agent = agent
        self._pending_auth = pending_auth if pending_auth is not None else []
        self._mcp_tools = {
            k: v for k, v in self._tool_reg.items() if v[0] == "mcp"
        }
        self._active_model_key = ""
        self.token_threshold_warn, self.token_threshold_danger = (
            token_threshold_warn,
            token_threshold_danger,
        )
        self.git_branch = _git_branch(cwd)
        self.tokens_in = self.tokens_out = 0
        self.ibuf, self.cpos = "", 0
        self.running = True
        self.approval = None
        self._history = []
        self._hist_idx = 0
        self._hist_stash = ""
        self._spinner = False
        self._spinner_frame = 0
        self._spinner_time = 0.0
        self._SPIN = "⠷⠯⠻⠽⠾"
        self._spin_line_active = False
        self._streaming_started = False
        self._drawn_rows = 1

    def _prompt_prefix(self):
        cwd_short = os.path.basename(self.cwd) or self.cwd
        # cwd in bold, # dim, branch in cyan
        if self.git_branch:
            prefix = (
                f"\033[1m{cwd_short}\033[2m#\033[0m\033[36m{self.git_branch}\033[0m"
            )
        else:
            prefix = f"\033[1m{cwd_short}\033[0m"
        return prefix

    def _caret(self):
        tok = self.tokens_in + self.tokens_out
        if tok >= self.token_threshold_danger:
            return "\033[31m❯\033[0m"  # red
        if tok >= self.token_threshold_warn:
            return "\033[33m❯\033[0m"  # yellow
        return "❯"

    def _banner(self):
        d, r = "\033[2m", "\033[0m"
        _out(f"""
{d}█▀▀ █▀█ █▀▀
█   █ █ █ █
▀▀▀ ▀▀▀ ▀▀▀{r}
{d}model:{r} {self.model}
{d}type{r} /help {d}for commands{r}
""")

    def _input_layout(self):
        try:
            w = os.get_terminal_size().columns
        except OSError:
            w = 80
        cwd_short = os.path.basename(self.cwd) or self.cwd
        plain_pfx = (
            f"{cwd_short}#{self.git_branch} ❯ "
            if self.git_branch
            else f"{cwd_short} ❯ "
        )
        styled_pfx = f"{self._prompt_prefix()} {self._caret()} "
        pfx_len = len(plain_pfx)
        first_cap, cont_cap = w - pfx_len - 1, max(w - 2, 1)
        rows, row_starts = [], [0]
        prefix, cap, line = styled_pfx, first_cap, ""
        for i, ch in enumerate(self.ibuf):
            if ch == "\n":
                rows.append((prefix, line))
                prefix, cap, line = "  ", cont_cap, ""
                row_starts.append(i + 1)
            elif len(line) >= cap:
                rows.append((prefix, line))
                prefix, cap, line = "  ", cont_cap, ch
                row_starts.append(i)
            else:
                line += ch
        rows.append((prefix, line))
        crow = len(rows) - 1
        for r in range(len(rows) - 1):
            if self.cpos < row_starts[r + 1]:
                crow = r
                break
        vis_pfx_w = pfx_len if crow == 0 else 2
        return rows, crow, vis_pfx_w + self.cpos - row_starts[crow]

    def _draw_input(self):
        rows, crow, ccol = self._input_layout()
        sys.stdout.write("\033[?25l")
        # Clear current line and draw all input rows
        sys.stdout.write("\r\033[2K")
        ghost = self._ghost_complete()
        for i, (prefix, text) in enumerate(rows):
            if i > 0:
                sys.stdout.write("\n\033[2K")
            sys.stdout.write(prefix + text)
            if ghost and i == len(rows) - 1:
                sys.stdout.write(f"\033[2m{ghost}\033[0m")
        # Move cursor to the right position
        up = len(rows) - 1 - crow
        if up > 0:
            sys.stdout.write(f"\033[{up}A")
        sys.stdout.write(f"\r\033[{ccol}C")
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        self._drawn_rows = len(rows)

    def _clear_input(self):
        n = self._drawn_rows
        sys.stdout.write("\r\033[2K")
        for _ in range(n - 1):
            sys.stdout.write("\033[1B\033[2K")
        if n > 1:
            sys.stdout.write(f"\033[{n - 1}A")
        sys.stdout.write("\r")
        sys.stdout.flush()

    def _clear_spinner(self):
        if self._spin_line_active:
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()
            self._spin_line_active = False

    def _start_spinner(self):
        self._spinner = True
        self._spinner_frame = 0
        self._spinner_time = time.monotonic()
        self._spin_line_active = True

    def _stop_spinner(self):
        if not self._spinner:
            return
        self._spinner = False
        self._clear_spinner()

    def _tick_spinner(self):
        if not self._spinner:
            return
        now = time.monotonic()
        if now - self._spinner_time < 0.08:
            return
        self._spinner_time = now
        self._spinner_frame = (self._spinner_frame + 1) % len(self._SPIN)
        ch = self._SPIN[self._spinner_frame]
        sys.stdout.write(f"\r\033[2K\033[2m{ch}\033[0m")
        sys.stdout.flush()
        self._spin_line_active = True

    def _handle_event(self, ev):
        t = ev.get("type")
        if t == "user_message":
            pass  # already printed by submit handler
        elif t == "assistant_text_delta":
            self._stop_spinner()
            text = ev.get("text", "")
            if not self._streaming_started:
                text = text.lstrip("\n")
                if not text:
                    return
                self._streaming_started = True
            _out_inline(text)
        elif t == "assistant_text_final":
            _out("")  # newline after streaming
        elif t == "tool_call":
            self._stop_spinner()
            s = _summarize_args(ev.get("input", {}))
            _out(f"\033[36;1m> {ev.get('name', '?')}\033[0m\033[2m({s})\033[0m")
        elif t == "tool_result":
            self._stop_spinner()
            o = ev.get("output", "")
            if ev.get("is_error"):
                _out(f"\033[31m< ERROR: {o[:200]}\033[0m")
            else:
                _out(
                    f"\033[2m< [{len(o.encode('utf-8', errors='replace'))} bytes]\033[0m"
                )
            self._streaming_started = False
            self._start_spinner()
        elif t == "verbose":
            for line in ev.get("data", "").split("\n"):
                _out(f"\033[2m  {line}\033[0m")
        elif t == "status":
            self._stop_spinner()
            _out(f"\033[33m~ {ev.get('message', '')}\033[0m")
        elif t == "error":
            self._stop_spinner()
            _out(f"\033[31m! {ev.get('message', '')}\033[0m")
        elif t == "turn_complete":
            self._stop_spinner()
            usage = ev.get("usage", {})
            self.tokens_in += usage.get("input_tokens", 0)
            self.tokens_out += usage.get("output_tokens", 0)
            _out("")
            self._draw_input()
        elif t == "approval_request":
            self._stop_spinner()
            name, inp = ev.get("name", "?"), ev.get("input", {})
            s = _summarize_args(inp)
            _out(f"\033[33m? Allow {name}({s})? [y/n]\033[0m")
            self.approval = ev

    def _handle_key(self, ch):
        if self.approval:
            if ch in (b"y", b"Y"):
                _out("\033[33m  approved\033[0m")
                self.iq.put({"type": "approval", "approved": True})
            elif ch in (b"n", b"N"):
                _out("\033[33m  denied\033[0m")
                self.iq.put({"type": "approval", "approved": False})
            else:
                return
            self.approval = None
            self._start_spinner()
            return
        if ch in (b"\r", b"\n"):
            text = self.ibuf.strip()
            if text:
                self._clear_input()
                self._history.append(text)
                self._hist_idx = len(self._history)
                self._hist_stash = ""
                self.ibuf, self.cpos = "", 0
                if text.startswith("/"):
                    ghost = self._ghost_complete()
                    if ghost:
                        text += ghost
                    _out(f"{self._prompt_prefix()} {self._caret()} {text}")
                    self._handle_slash(text)
                    self._draw_input()
                    return
                _out(f"{self._prompt_prefix()} {self._caret()} {text}")
                self._streaming_started = False
                self.iq.put(text)
                self._start_spinner()
                return
        elif ch in (b"\x7f", b"\x08"):
            if self.cpos > 0:
                self.ibuf = self.ibuf[: self.cpos - 1] + self.ibuf[self.cpos :]
                self.cpos -= 1
        elif ch == b"\x15":
            nl = self.ibuf.rfind("\n", 0, self.cpos)
            start = nl + 1 if nl >= 0 else 0
            if start == self.cpos and self.cpos > 0:
                self.ibuf = self.ibuf[: self.cpos - 1] + self.ibuf[self.cpos :]
                self.cpos -= 1
            else:
                self.ibuf = self.ibuf[:start] + self.ibuf[self.cpos :]
                self.cpos = start
        elif ch == b"\x09":  # Tab — accept ghost completion
            if self.ibuf.startswith("/"):
                ghost = self._ghost_complete()
                if ghost:
                    self.ibuf += ghost
                    self.cpos = len(self.ibuf)
        elif ch == b"\x01":
            self.cpos = 0
        elif ch == b"\x05":
            self.cpos = len(self.ibuf)
        elif ch == b"\x03":
            self.running = False
            return
        elif ch == b"\x1b":
            self._esc()
            self._draw_input()
            return
        elif ch and ch[0:1].isascii() and ch[0] >= 32:
            c = ch.decode("utf-8", errors="replace")
            self.ibuf = self.ibuf[: self.cpos] + c + self.ibuf[self.cpos :]
            self.cpos += 1
        self._draw_input()

    def _word_left(self):
        i = self.cpos
        if i > 0 and self.ibuf[i - 1] == "\n":
            return i - 1
        while i > 0 and not self.ibuf[i - 1].isalnum() and self.ibuf[i - 1] != "\n":
            i -= 1
        while i > 0 and self.ibuf[i - 1].isalnum():
            i -= 1
        return i

    def _word_right(self):
        i, n = self.cpos, len(self.ibuf)
        while i < n and not self.ibuf[i].isalnum():
            i += 1
        while i < n and self.ibuf[i].isalnum():
            i += 1
        return i

    def _hist_prev(self):
        if not self._history or self._hist_idx <= 0:
            return
        if self._hist_idx == len(self._history):
            self._hist_stash = self.ibuf
        self._hist_idx -= 1
        self.ibuf = self._history[self._hist_idx]
        self.cpos = len(self.ibuf)

    def _hist_next(self):
        if self._hist_idx >= len(self._history):
            return
        self._hist_idx += 1
        if self._hist_idx == len(self._history):
            self.ibuf = self._hist_stash
        else:
            self.ibuf = self._history[self._hist_idx]
        self.cpos = len(self.ibuf)

    _SLASH_CMDS = ["/help", "/clear", "/tokens", "/model", "/mcp", "/quit", "/exit"]

    def _ghost_complete(self):
        buf = self.ibuf.strip()
        if not buf.startswith("/") or " " in buf:
            return ""
        matches = [c for c in self._SLASH_CMDS if c.startswith(buf) and c != buf]
        if len(matches) == 1:
            return matches[0][len(buf) :]
        return ""

    def _handle_slash(self, cmd):
        d, r = "\033[2m", "\033[0m"
        parts = cmd.split()
        c = parts[0].lower()
        if c == "/help":
            _out(f"""
{d}Commands:{r}
  /help              show this message
  /clear             clear screen
  /tokens            show token usage
  /model [name]      show or switch model
  /mcp [name]        list MCP servers or tools
  /mcp auth <name>   authenticate MCP server
  /mcp revoke <name> revoke MCP auth token
  /quit              exit

{d}Shortcuts:{r}
  Opt+Enter          newline
  Opt+Left/Right     word jump
  Opt+Delete         delete word
  Cmd+Delete         delete line
  Ctrl+A/E           home / end
  Ctrl+C             exit
""")
        elif c == "/clear":
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        elif c == "/tokens":
            tok = self.tokens_in + self.tokens_out
            _out(
                f"  {d}input:{r} {self.tokens_in:,}  {d}output:{r} {self.tokens_out:,}  {d}total:{r} {tok:,}"
            )
        elif c == "/model":
            self._cmd_model(parts[1:])
        elif c == "/mcp":
            self._cmd_mcp(parts[1:])
        elif c in ("/quit", "/exit"):
            self.running = False
        else:
            _out(f"  {d}unknown command: {c} (try /help){r}")

    def _cmd_model(self, args):
        d, r = "\033[2m", "\033[0m"
        if not args:
            _out(f"  {d}active:{r} {self.model}")
            for name, m in self.models.items():
                marker = " *" if name == self._active_model_key else ""
                _out(f"  {d}{name}:{r} {m.get('model', name)}{marker}")
            return
        name = args[0]
        if name not in self.models:
            _out(
                f"  {d}unknown model: {name} (available: {', '.join(self.models.keys())}){r}"
            )
            return
        m = self.models[name]
        self.model = m.get("model", name)
        self._active_model_key = name
        self.iq.put(
            {
                "type": "switch_model",
                "model": self.model,
                "api_key": os.environ.get(m.get("api_key_env", ""), ""),
                "api_base_url": m.get("api_base_url", "https://api.anthropic.com"),
            }
        )
        _out(f"  {d}switched to{r} {self.model}")

    def _cmd_mcp(self, args):
        d, r = "\033[2m", "\033[0m"
        if not self.mcp_servers:
            _out(f"  {d}no MCP servers configured{r}")
            return
        if not args:
            for s in self.mcp_servers:
                sname = s.get("name", "?")
                if any(p.get("name") == sname for p in self._pending_auth):
                    _out(
                        f"  {d}{sname}{r} {s.get('url', '')} \033[33m(auth required: /mcp auth {sname})\033[0m"
                    )
                else:
                    n = sum(
                        1
                        for _, e in self._mcp_tools.items()
                        if e[1].get("name") == sname
                    )
                    _out(f"  {d}{sname}{r} {s.get('url', '')} {d}({n} tools){r}")
            return
        sub = args[0]
        if sub == "auth" and len(args) > 1:
            self._cmd_mcp_auth(args[1])
        elif sub == "revoke" and len(args) > 1:
            self._cmd_mcp_revoke(args[1])
        else:
            self._cmd_mcp_tools(sub)

    def _cmd_mcp_auth(self, name):
        d, r = "\033[2m", "\033[0m"
        pending = [p for p in self._pending_auth if p.get("name") == name]
        if not pending:
            _out(f"  {d}{name} does not need auth{r}")
            return
        try:
            pcfg = pending[0]
            pcfg["_oauth_interactive"] = True
            server = mcp_initialize(pcfg)
            server["name"] = name
            result = _mcp_post(server, "tools/list", {})
            mcp_tools = (
                (result or {}).get("result", {}).get("tools", [])
                if result and "error" not in result
                else []
            )
            multi = len(self.mcp_servers) > 1
            for t in mcp_tools:
                tname = f"{name}__{t['name']}" if multi else t["name"]
                entry = (
                    "mcp",
                    server,
                    {
                        "name": tname,
                        "description": t.get("description", ""),
                        "input_schema": t.get(
                            "inputSchema", {"type": "object", "properties": {}}
                        ),
                    },
                    t["name"],
                )
                self._mcp_tools[tname] = entry
                self._tool_reg[tname] = entry
                if self._agent is not None:
                    self._agent.tools[tname] = entry
            self._pending_auth = [
                p for p in self._pending_auth if p.get("name") != name
            ]
            self.tool_count = len(self._tool_reg)
            _out(f"  {d}authenticated. {len(mcp_tools)} tools loaded from {name}{r}")
        except Exception as e:
            _out(f"  \033[31mauth failed: {e}\033[0m")

    def _cmd_mcp_revoke(self, name):
        d, r = "\033[2m", "\033[0m"
        path = _mcp_token_path(name)
        if not os.path.exists(path):
            _out(f"  {d}no token found for {name}{r}")
            return
        os.remove(path)
        removed = [t for t, e in self._mcp_tools.items() if e[1].get("name") == name]
        for t in removed:
            self._mcp_tools.pop(t, None)
            self._tool_reg.pop(t, None)
            if self._agent is not None:
                self._agent.tools.pop(t, None)
        self.tool_count = len(self._tool_reg)
        srv = next((s for s in self.mcp_servers if s.get("name") == name), None)
        if srv and srv not in self._pending_auth:
            self._pending_auth.append(srv)
        _out(f"  {d}revoked {name}, {len(removed)} tools removed{r}")

    def _cmd_mcp_tools(self, name):
        d, r = "\033[2m", "\033[0m"
        found = False
        for tname, entry in self._mcp_tools.items():
            if entry[1].get("name") == name or len(self.mcp_servers) == 1:
                _out(f"  {d}{tname}:{r} {entry[2].get('description', '')[:80]}")
                found = True
        if not found:
            _out(f"  {d}no tools found for '{name}'{r}")

    def _esc(self):
        r, _, _ = select.select([sys.stdin], [], [], 0.05)
        if not r:
            return
        ch2 = os.read(_fd, 1)
        if ch2 in (b"\r", b"\n"):
            self.ibuf = self.ibuf[: self.cpos] + "\n" + self.ibuf[self.cpos :]
            self.cpos += 1
        elif ch2 == b"b":
            self.cpos = self._word_left()
        elif ch2 == b"f":
            self.cpos = self._word_right()
        elif ch2 == b"\x7f":
            wp = self._word_left()
            self.ibuf = self.ibuf[:wp] + self.ibuf[self.cpos :]
            self.cpos = wp
        elif ch2 == b"[":
            seq = b""
            while True:
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if not r:
                    break
                b = os.read(_fd, 1)
                seq += b
                if b and b[0] >= 0x40:
                    break
            if seq == b"A":
                self._hist_prev()
            elif seq == b"B":
                self._hist_next()
            elif seq == b"D" and self.cpos > 0:
                self.cpos -= 1
            elif seq == b"C" and self.cpos < len(self.ibuf):
                self.cpos += 1
            elif seq in (b"H", b"1~"):
                self.cpos = 0
            elif seq in (b"F", b"4~"):
                self.cpos = len(self.ibuf)
            elif seq == b"1;3D":
                self.cpos = self._word_left()
            elif seq == b"1;3C":
                self.cpos = self._word_right()

    def run(self):
        if not sys.stdin.isatty():
            self._run_headless()
            return
        _set_cbreak()
        atexit.register(_restore_term)
        self._banner()
        pending = self._pending_auth
        if pending:
            n = len(pending)
            names = ", ".join(p.get("name", "?") for p in pending)
            _out(
                f"\033[33m{n} MCP server{'s' if n > 1 else ''} need auth: {names}\033[0m"
            )
            _out("\033[2mrun /mcp to see details\033[0m\n")
        try:
            self._draw_input()
            while self.running:
                while True:
                    try:
                        ev = self.eq.get_nowait()
                        self._handle_event(ev)
                    except queue.Empty:
                        break
                r, _, _ = select.select([sys.stdin], [], [], 0.02)
                if r:
                    self._handle_key(os.read(_fd, 1))
                self._tick_spinner()
        except KeyboardInterrupt:
            pass
        finally:
            _restore_term()
            _out("\n\033[2mbye\033[0m")

    def _run_headless(self):
        """Line-oriented fallback for piped/scripted stdin. No raw mode, no
        redraw — just read a line, send it, print events until the turn
        completes. Approval prompts are answered from the next stdin line."""
        print(f"cog — {self.model} — {self.tool_count} tools")
        print("> ", end="", flush=True)
        while self.running:
            try:
                line = sys.stdin.readline()
            except KeyboardInterrupt:
                break
            if not line:  # EOF
                break
            line = line.rstrip("\n")
            if not line:
                print("> ", end="", flush=True)
                continue
            if line in ("/quit", "/exit"):
                break
            if line.startswith("/"):
                # Slash commands other than quit are ignored in headless mode;
                # they exist for the interactive TUI.
                print("  (slash commands are ignored in non-interactive mode)")
                print("> ", end="", flush=True)
                continue
            self.iq.put(line)
            while True:
                ev = self.eq.get()
                t = ev.get("type")
                if t == "assistant_text_delta":
                    print(ev.get("text", ""), end="", flush=True)
                elif t == "assistant_text_final":
                    print()
                elif t == "tool_call":
                    print(
                        f"> {ev.get('name', '?')}({_summarize_args(ev.get('input', {}))})"
                    )
                elif t == "tool_result":
                    o = ev.get("output", "")
                    if ev.get("is_error"):
                        print(f"< ERROR: {o[:200]}")
                    else:
                        print(f"< [{len(o.encode('utf-8', errors='replace'))} bytes]")
                elif t == "status":
                    print(f"~ {ev.get('message', '')}")
                elif t == "error":
                    print(f"! {ev.get('message', '')}")
                    break
                elif t == "approval_request":
                    name, inp = ev.get("name", "?"), ev.get("input", {})
                    print(
                        f"? Allow {name}({_summarize_args(inp)})? [y/N] ",
                        end="",
                        flush=True,
                    )
                    reply = sys.stdin.readline().strip().lower()
                    self.iq.put({"type": "approval", "approved": reply.startswith("y")})
                elif t == "turn_complete":
                    usage = ev.get("usage", {})
                    self.tokens_in += usage.get("input_tokens", 0)
                    self.tokens_out += usage.get("output_tokens", 0)
                    break
            print("> ", end="", flush=True)


# --- Config & Main ---


@dataclass
class Config:
    model: str = "claude-sonnet-4-20250514"
    api_key_env: str = "ANTHROPIC_API_KEY"
    api_base_url: str = "https://api.anthropic.com"
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    skills_dirs: list[str] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    max_tool_calls_per_turn: int = 10
    shell_timeout_seconds: int = 30
    tool_output_max_bytes: int = 32768
    log_dir: str = "~/.cog/logs"
    auto_approve: bool = False
    verbose: bool = False
    token_threshold_warn: int = 100000
    token_threshold_danger: int = 200000
    # Set at runtime, not from config file
    api_key: str = ""
    system_prompt: str = ""
    _log_fn: object = None


_SYSTEM = (
    "You are a coding agent working in the directory: {cwd}\n\n"
    "You have access to tools for reading, writing, and editing files, "
    "listing directories, and optionally running shell commands. Use them when helpful.\n\n"
    "Guidelines:\n"
    "- Take small, concrete steps. Read before writing.\n"
    "- Use str_replace for targeted edits. Use write_file for new files or complete rewrites.\n"
    "- Explain what you're doing briefly before each action.\n"
    "- If a tool call fails, read the error and adjust.\n"
    "- Do not invent file contents or tool outputs.\n"
    "- Keep responses concise unless the user asks for detail.\n"
)


def _expand_env(v) -> Any:
    if isinstance(v, str):
        return re.sub(r"\$\{([^}]+)\}", lambda m: os.environ.get(m.group(1), ""), v)
    if isinstance(v, dict):
        return {k: _expand_env(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_expand_env(i) for i in v]
    return v


def _find_local_config(cwd):
    d = os.path.abspath(cwd)
    while True:
        candidate = os.path.join(d, ".cog", "config.json")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _load_config(path, cwd="."):
    path = os.path.expanduser(path)
    raw: dict[str, Any] = {}
    if os.path.exists(path):
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:
            print(
                f"Warning: {path} is accessible by others (mode {oct(mode)}). "
                f"Run: chmod 600 {path}",
                file=sys.stderr,
            )
        with open(path) as f:
            raw = json.load(f)
    local = _find_local_config(cwd)
    if local:
        with open(local) as f:
            raw.update(json.load(f))
    raw = _expand_env(raw)
    known = {f.name for f in Config.__dataclass_fields__.values()}
    cfg = Config(**{k: v for k, v in raw.items() if k in known})
    cfg.log_dir = os.path.expanduser(cfg.log_dir)
    cfg.skills_dirs = [os.path.expanduser(p) for p in cfg.skills_dirs]
    _resolve_model(cfg)
    return cfg


def _resolve_model(cfg):
    """If cfg.model is a key in cfg.models, apply that model's settings."""
    if cfg.models and cfg.model in cfg.models:
        m = cfg.models[cfg.model]
        cfg.api_base_url = m.get("api_base_url", cfg.api_base_url)
        cfg.api_key_env = m.get("api_key_env", cfg.api_key_env)
        cfg.model = m.get("model", cfg.model)
    cfg.api_key = os.environ.get(cfg.api_key_env, "")


def _load_skills(dirs):
    skills = []
    for d in dirs:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        for entry in os.listdir(d):
            sf = os.path.join(d, entry, "SKILL.md")
            if not os.path.isfile(sf):
                continue
            with open(sf) as f:
                text = f.read()
            lines = text.split("\n")
            if not lines or lines[0].strip() != "---":
                skills.append({"name": entry, "text": text})
                continue
            name, i = entry, 1
            while i < len(lines) and lines[i].strip() != "---":
                if ":" in lines[i]:
                    k, v = lines[i].split(":", 1)
                    if k.strip() == "name":
                        name = v.strip()
                i += 1
            body = "\n".join(lines[i + 1 :]).strip()
            if body:
                skills.append({"name": name, "text": body})
    return skills


def main():
    if sys.platform not in ("darwin", "linux"):
        print(
            f"Error: unsupported platform '{sys.platform}'. Requires macOS or Linux.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if sys.version_info < (3, 9):
        print(f"Error: requires Python 3.9+, got {sys.version}", file=sys.stderr)
        raise SystemExit(1)
    ap = argparse.ArgumentParser(description="cog - minimal coding agent")
    ap.add_argument("--config", default="~/.cog/config.json")
    ap.add_argument("--cwd", default=".")
    ap.add_argument("--auto", action="store_true", help="auto-approve all tool calls")
    ap.add_argument("--verbose", action="store_true", help="show full API JSON")
    args = ap.parse_args()

    cwd = os.path.abspath(args.cwd)
    cfg = _load_config(args.config, cwd)
    if args.auto:
        cfg.auto_approve = True
    if args.verbose:
        cfg.verbose = True
    if not cfg.api_key and cfg.api_base_url == "https://api.anthropic.com":
        print(
            f"Error: API key not found. Run: export {cfg.api_key_env}=your-key",
            file=sys.stderr,
        )
        raise SystemExit(1)

    tools_configure(cwd=cwd, shell_timeout=cfg.shell_timeout_seconds)
    skills = _load_skills(cfg.skills_dirs)
    prompt = _SYSTEM.format(cwd=cwd)
    for s in skills:
        prompt += f'\n<skill name="{s["name"]}">\n{s["text"]}\n</skill>\n'

    tool_reg = get_tools()
    mcp_tools, _, pending_auth = mcp_discover_all(cfg.mcp_servers)
    tool_reg.update(mcp_tools)

    os.makedirs(cfg.log_dir, exist_ok=True)
    log_path = os.path.join(
        cfg.log_dir, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S") + ".jsonl"
    )
    log_f = open(log_path, "a")
    cfg.system_prompt = prompt
    cfg._log_fn = lambda ev: (log_f.write(json.dumps(ev) + "\n"), log_f.flush())

    eq, iq = queue.Queue(), queue.Queue()
    agent = Agent(cfg, tool_reg, eq, iq)
    threading.Thread(target=agent.worker_loop, daemon=True).start()
    tui = TUI(
        eq,
        iq,
        model=cfg.model,
        cwd=cwd,
        tool_count=len(tool_reg),
        token_threshold_warn=cfg.token_threshold_warn,
        token_threshold_danger=cfg.token_threshold_danger,
        models=cfg.models,
        mcp_servers=cfg.mcp_servers,
        tool_reg=tool_reg,
        agent=agent,
        pending_auth=pending_auth,
    )
    tui.run()
    iq.put(None)


if __name__ == "__main__":
    main()
