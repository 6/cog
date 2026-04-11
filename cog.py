#!/usr/bin/env python3
"""cog - minimal coding agent. Stdlib-only, Python 3.9+."""

import argparse
import atexit
import http.client
import json
import os
import queue
import re
import select
import signal
import ssl
import subprocess
import sys
import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

_cwd = "."
_shell_enabled = False
_shell_timeout = 30


def tools_configure(cwd=".", shell_enabled=False, shell_timeout=30):
    global _cwd, _shell_enabled, _shell_timeout
    _cwd = os.path.abspath(cwd)
    _shell_enabled = shell_enabled
    _shell_timeout = shell_timeout


def _resolve(path):
    p = Path(path)
    if not p.is_absolute():
        p = Path(_cwd) / p
    return str(p.resolve())


def tool_read_file(args):
    path = _resolve(args["path"])
    try:
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
    path = _resolve(args.get("path", "."))
    try:
        entries = sorted(os.listdir(path))
    except FileNotFoundError:
        return f"ERROR: directory not found: {path}"
    except PermissionError:
        return f"ERROR: permission denied: {path}"
    lines = []
    for name in entries:
        full = os.path.join(path, name)
        kind = "[dir] " if os.path.isdir(full) else "[file]"
        lines.append(f"{kind} {name}")
    return "\n".join(lines) if lines else "(empty directory)"


def tool_write_file(args):
    path = _resolve(args["path"])
    content = args["content"]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"OK: wrote {len(content.encode('utf-8'))} bytes to {path}"
    except Exception as e:
        return f"ERROR: {e}"


def tool_str_replace(args):
    path = _resolve(args["path"])
    old_str, new_str = args["old_str"], args["new_str"]
    try:
        content = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except Exception as e:
        return f"ERROR: {e}"
    count = content.count(old_str)
    if count == 0:
        return "ERROR: old_str not found in file"
    if count > 1:
        return (f"ERROR: old_str matched {count} times, must be unique. "
                "Add more surrounding context to make it unique.")
    Path(path).write_text(content.replace(old_str, new_str, 1), encoding="utf-8")
    return "OK: replacement made"


def tool_run_shell(args):
    if not _shell_enabled:
        return "ERROR: Shell is disabled. Enable with --shell flag or shell_enabled in config."
    try:
        r = subprocess.run(
            args["command"], shell=True, capture_output=True, text=True,
            timeout=_shell_timeout, cwd=_cwd,
        )
        out = (r.stdout or "") + (r.stderr or "") + f"\n[exit code: {r.returncode}]"
        return out.strip()
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {_shell_timeout}s"
    except Exception as e:
        return f"ERROR: {e}"


def _schema(name, desc, props, required):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": required}}


_TOOL_DEFS = {
    "read_file": (tool_read_file, _schema(
        "read_file", "Read the contents of a file at the given path.",
        {"path": {"type": "string", "description": "File path to read (relative to cwd or absolute)"}},
        ["path"])),
    "list_dir": (tool_list_dir, _schema(
        "list_dir", "List directory contents with type indicators ([dir] or [file]).",
        {"path": {"type": "string", "description": "Directory path (default: current directory)"}},
        [])),
    "write_file": (tool_write_file, _schema(
        "write_file", "Write content to a file, creating parent directories if needed.",
        {"path": {"type": "string", "description": "File path to write"},
         "content": {"type": "string", "description": "Content to write to the file"}},
        ["path", "content"])),
    "str_replace": (tool_str_replace, _schema(
        "str_replace",
        "Replace an exact string match in a file. The old_str must appear exactly once. "
        "Include enough surrounding context lines in old_str to make the match unique.",
        {"path": {"type": "string", "description": "File path to edit"},
         "old_str": {"type": "string", "description": "Exact string to find (must appear exactly once)"},
         "new_str": {"type": "string", "description": "String to replace it with"}},
        ["path", "old_str", "new_str"])),
    "run_shell": (tool_run_shell, _schema(
        "run_shell", "Run a shell command and return combined stdout and stderr with exit code.",
        {"command": {"type": "string", "description": "Shell command to execute"}},
        ["command"])),
}


def get_tools(shell_enabled=False):
    tools = {}
    for name, (fn, schema) in _TOOL_DEFS.items():
        if name == "run_shell" and not shell_enabled:
            continue
        tools[name] = ("builtin", fn, schema)
    return tools


# ---------------------------------------------------------------------------
# Anthropic API
# ---------------------------------------------------------------------------

class APIError(Exception):
    def __init__(self, status, body):
        self.status, self.body = status, body
        super().__init__(f"API error {status}: {body}")


def build_request(model, system, messages, tools, max_tokens=4096):
    req = {"model": model, "max_tokens": max_tokens, "system": system,
           "messages": messages, "stream": True}
    if tools:
        req["tools"] = [t for _, _, t in tools.values()]
    return req


def stream_request(api_key, request_body, api_base_url="https://api.anthropic.com"):
    parsed = urllib.parse.urlparse(api_base_url)
    host = parsed.hostname
    port = parsed.port
    base_path = (parsed.path or "").rstrip("/")
    if parsed.scheme == "https":
        conn = http.client.HTTPSConnection(host, port or 443, timeout=120,
                                           context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, port or 80, timeout=120)
    headers = {"Content-Type": "application/json", "X-API-Key": api_key,
               "anthropic-version": "2023-06-01"}
    conn.request("POST", f"{base_path}/v1/messages", json.dumps(request_body), headers)
    resp = conn.getresponse()
    if resp.status != 200:
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        raise APIError(resp.status, body)
    return resp, conn


def parse_sse_stream(response):
    event_type = block_type = block_id = block_name = None
    json_accum = full_text = ""
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
            block_type = cb.get("type")
            if block_type == "tool_use":
                block_id, block_name, json_accum = cb.get("id"), cb.get("name"), ""
        elif event_type == "content_block_delta":
            delta = data.get("delta", {})
            dtype = delta.get("type")
            if dtype == "text_delta":
                text = delta.get("text", "")
                full_text += text
                yield ("text_delta", text)
            elif dtype == "input_json_delta":
                json_accum += delta.get("partial_json", "")
        elif event_type == "content_block_stop":
            if block_type == "tool_use":
                try:
                    tool_input = json.loads(json_accum) if json_accum else {}
                except json.JSONDecodeError:
                    tool_input = {"_raw": json_accum}
                yield ("tool_use", {"id": block_id, "name": block_name, "input": tool_input})
            elif block_type == "text":
                yield ("text_final", full_text)
            block_type = block_id = block_name = None
            json_accum = ""
        elif event_type == "message_delta":
            usage["output_tokens"] = data.get("usage", {}).get("output_tokens", 0)
            yield ("stop", data.get("delta", {}).get("stop_reason", "end_turn"))
        elif event_type == "message_stop":
            yield ("usage", dict(usage))
        event_type = None


# ---------------------------------------------------------------------------
# MCP Client
# ---------------------------------------------------------------------------

class MCPError(Exception):
    pass


def _mcp_connect(parsed_url):
    if parsed_url.scheme == "https":
        return http.client.HTTPSConnection(parsed_url.hostname, parsed_url.port or 443, timeout=30)
    return http.client.HTTPConnection(parsed_url.hostname, parsed_url.port or 80, timeout=30)


def _mcp_post(server, method, params=None, is_notification=False):
    parsed = urllib.parse.urlparse(server["url"])
    body = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if not is_notification:
        server["_next_id"] = server.get("_next_id", 0) + 1
        body["id"] = server["_next_id"]
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if server.get("_session_id"):
        headers["Mcp-Session-Id"] = server["_session_id"]
    for k, v in server.get("headers", {}).items():
        headers[k] = v
    conn = _mcp_connect(parsed)
    conn.request("POST", parsed.path or "/", json.dumps(body), headers)
    resp = conn.getresponse()
    if is_notification:
        resp.read(); conn.close(); return None
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


def mcp_initialize(cfg):
    server = dict(cfg, _next_id=0, _session_id=None)
    result = _mcp_post(server, "initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
        "clientInfo": {"name": "cog", "version": "0.1.0"}})
    if result and "error" in result:
        raise MCPError(f"initialize failed: {result['error']}")
    _mcp_post(server, "notifications/initialized", is_notification=True)
    return server


def mcp_call_tool(server, tool_name, arguments):
    result = _mcp_post(server, "tools/call", {"name": tool_name, "arguments": arguments})
    if not result:
        return "ERROR: no response from MCP server"
    if "error" in result:
        err = result["error"]
        return f"ERROR: {err.get('message', err) if isinstance(err, dict) else err}"
    parts = []
    for item in result.get("result", {}).get("content", []):
        parts.append(item.get("text", "") if item.get("type") == "text" else json.dumps(item))
    return "\n".join(parts) if parts else "(empty result)"


def mcp_discover_all(mcp_configs):
    if not mcp_configs:
        return {}, []
    servers, tools, multi = [], {}, len(mcp_configs) > 1
    for cfg in mcp_configs:
        name = cfg.get("name", "mcp")
        try:
            server = mcp_initialize(cfg)
            server["name"] = name
            result = _mcp_post(server, "tools/list", {})
            mcp_tools = (result or {}).get("result", {}).get("tools", []) if result and "error" not in result else []
            servers.append(server)
            for t in mcp_tools:
                tname = f"{name}__{t['name']}" if multi else t["name"]
                tools[tname] = ("mcp", server,
                    {"name": tname, "description": t.get("description", ""),
                     "input_schema": t.get("inputSchema", {"type": "object", "properties": {}})},
                    t["name"])
        except Exception as e:
            print(f"Warning: MCP server '{name}' failed: {e}", file=sys.stderr)
    return tools, servers


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

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
            try:
                resp, conn = stream_request(self.api_key, req, self.api_base_url)
            except APIError as e:
                self.emit({"type": "error", "message": str(e)}); return
            except Exception as e:
                self.emit({"type": "error", "message": f"Network error: {e}"}); return
            content_blocks, tool_uses, usage, full_text = [], [], {}, ""
            try:
                for kind, payload in parse_sse_stream(resp):
                    if kind == "text_delta":
                        self.emit({"type": "assistant_text_delta", "text": payload})
                    elif kind == "text_final":
                        full_text = payload
                        self.emit({"type": "assistant_text_final", "text": payload})
                    elif kind == "tool_use":
                        tool_uses.append(payload)
                        self.emit({"type": "tool_call", "tool_id": payload["id"],
                                   "name": payload["name"], "input": payload["input"]})
                    elif kind == "usage":
                        usage = payload
            except Exception as e:
                self.emit({"type": "error", "message": f"Stream error: {e}"}); return
            finally:
                try: conn.close()
                except Exception: pass
            if full_text:
                content_blocks.append({"type": "text", "text": full_text})
            for tu in tool_uses:
                content_blocks.append({"type": "tool_use", "id": tu["id"],
                                       "name": tu["name"], "input": tu["input"]})
            if content_blocks:
                self.messages.append({"role": "assistant", "content": content_blocks})
            if not tool_uses:
                self.emit({"type": "turn_complete", "usage": usage}); return
            tool_results = []
            for tu in tool_uses:
                tool_count += 1
                if tool_count > self.max_calls:
                    tr = {"type": "tool_result", "tool_use_id": tu["id"],
                          "content": "ERROR: maximum tool calls per turn exceeded", "is_error": True}
                    tool_results.append(tr)
                    self.emit({"type": "tool_result", "tool_id": tu["id"],
                               "output": tr["content"], "is_error": True})
                    continue
                output, is_error = self._dispatch(tu["name"], tu["input"])
                if len(output.encode("utf-8", errors="replace")) > self.max_output:
                    output = output[:self.max_output] + "\n[truncated]"
                tool_results.append({"type": "tool_result", "tool_use_id": tu["id"],
                                     "content": output, "is_error": is_error})
                self.emit({"type": "tool_result", "tool_id": tu["id"],
                           "output": output, "is_error": is_error})
            self.messages.append({"role": "user", "content": tool_results})
            if tool_count > self.max_calls:
                self.emit({"type": "turn_complete", "usage": usage}); return

    def _dispatch(self, name, input_args):
        if name not in self.tools:
            return f"ERROR: unknown tool '{name}'", True
        entry = self.tools[name]
        if entry[0] == "builtin":
            _, fn, schema = entry
            if name in ("write_file", "str_replace", "run_shell") and not self.auto_approve:
                if not self._approve(name, input_args):
                    return "Tool call denied by user.", True
            try:
                r = fn(input_args); return r, r.startswith("ERROR:")
            except Exception as e:
                return f"ERROR: {e}", True
        elif entry[0] == "mcp":
            _, server, schema, real_name = entry
            if not self.auto_approve:
                if not self._approve(name, input_args):
                    return "Tool call denied by user.", True
            try:
                r = mcp_call_tool(server, real_name, input_args); return r, r.startswith("ERROR:")
            except Exception as e:
                return f"ERROR: MCP call failed: {e}", True
        return f"ERROR: unknown tool type for '{name}'", True

    def _approve(self, name, input_args):
        self.emit({"type": "approval_request", "name": name, "input": input_args})
        try:
            resp = self.iq.get(timeout=300)
            return isinstance(resp, dict) and resp.get("type") == "approval" and resp.get("approved")
        except queue.Empty:
            return False

    def worker_loop(self):
        while True:
            msg = self.iq.get()
            if msg is None:
                break
            if isinstance(msg, dict):
                continue
            try:
                self.run_turn(msg)
            except Exception as e:
                self.emit({"type": "error", "message": f"Agent error: {e}"})


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------

_original_termios = None
_fd = None


def _twrite(s):
    sys.stdout.write(s)

def _tflush():
    sys.stdout.flush()

def _enter_alt():
    _twrite("\033[2J\033[H"); _tflush()

def _exit_alt():
    pass

def _set_cbreak():
    global _original_termios, _fd
    if os.name == "nt": return
    import termios, tty
    _fd = sys.stdin.fileno()
    _original_termios = termios.tcgetattr(_fd)
    tty.setcbreak(_fd)

def _restore_term():
    global _original_termios
    if _original_termios is not None:
        import termios
        try: termios.tcsetattr(_fd, termios.TCSADRAIN, _original_termios)
        except Exception: pass
        _original_termios = None
    _twrite("\033[?25h\033[r"); _tflush()

def _get_size():
    try:
        c, r = os.get_terminal_size(); return max(r, 5), max(c, 20)
    except OSError:
        return 24, 80

def _wrap(text, width):
    lines = []
    for raw in text.split("\n"):
        if not raw: lines.append(""); continue
        while len(raw) > width:
            lines.append(raw[:width]); raw = raw[width:]
        lines.append(raw)
    return lines

def _summarize_args(args, max_len=60):
    if not args: return ""
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 30: s = s[:27] + "..."
        parts.append(f'{k}="{s}"')
    r = ", ".join(parts)
    return r[:max_len - 3] + "..." if len(r) > max_len else r


class TUI:
    def __init__(self, event_queue, input_queue, model="", cwd="", tool_count=0):
        self.eq, self.iq = event_queue, input_queue
        self.model, self.cwd, self.tool_count = model, cwd, tool_count
        self.lines, self.ibuf, self.cpos = [], "", 0
        self.scroll, self.redraw, self.running = 0, True, True
        self.height, self.width = 24, 80
        self.cur_text, self.approval = "", None

    def _rule(self, row):
        _twrite(f"\033[{row};1H\033[2K\033[36m{'─' * self.width}\033[0m")

    def _input_layout(self):
        w = self.width
        first_cap = w - 3
        cont_cap = max(w - 2, 1)
        rows = []
        row_starts = [0]
        prefix = "❯ "
        cap = first_cap
        line = ""
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
        max_rows = max((self.height - 4) // 2, 1)
        if len(rows) > max_rows:
            rows = rows[:max_rows]
        crow = len(rows) - 1
        for r in range(len(rows) - 1):
            if self.cpos < row_starts[r + 1]:
                crow = r; break
        offset = self.cpos - row_starts[crow]
        ccol = 3 + offset
        return rows, crow, ccol

    def _draw_status(self):
        cwd_short = os.path.basename(self.cwd) or self.cwd
        d = "\033[2m"
        r = "\033[0m"
        s = f" {d}model:{r} {self.model} {d}|{r} {d}cwd:{r} {cwd_short} {d}|{r} {d}tools:{r} {self.tool_count} "
        _twrite(f"\033[{self.height};1H\033[2K{s}")

    def _draw_input(self, input_rows, base_row):
        for i, (prefix, text) in enumerate(input_rows):
            row = base_row + i
            _twrite(f"\033[{row};1H\033[2K{prefix}{text[:self.width - len(prefix)]}")
        if self.approval:
            name, inp = self.approval.get("name", "?"), self.approval.get("input", {})
            _twrite(f"\033[{base_row};1H\033[2K")
            _twrite(f"\033[33mAllow {name}({_summarize_args(inp)})? [y/n] \033[0m")

    def _draw_transcript(self, t_bottom):
        top = 1
        vis = t_bottom - top + 1
        if vis <= 0: return
        start = max(0, len(self.lines) - vis - self.scroll)
        for i in range(vis):
            _twrite(f"\033[{top+i};1H\033[2K")
            idx = start + i
            if 0 <= idx < len(self.lines):
                _twrite(self.lines[idx][:self.width])

    def _full_redraw(self):
        self.height, self.width = _get_size()
        input_rows, crow, ccol = self._input_layout()
        n_input = len(input_rows)
        # Layout: transcript | sep | input (n rows) | sep | status
        # status = height, bottom sep = height-1, input = height-2-n+1..height-2
        input_base = self.height - 1 - n_input
        sep_top = input_base - 1
        t_bottom = sep_top - 1
        _twrite("\033[?25l")
        _twrite(f"\033[1;{self.height}r")
        _twrite("\033[2J")
        self._draw_transcript(t_bottom)
        self._rule(sep_top)
        self._draw_input(input_rows, input_base)
        self._rule(self.height - 1)
        self._draw_status()
        _twrite(f"\033[{input_base + crow};{ccol}H")
        _twrite("\033[?25h")
        _tflush(); self.redraw = False

    def _append(self, styled):
        self.lines.extend(styled)
        if self.scroll == 0: self.redraw = True

    def _handle_event(self, ev):
        t = ev.get("type")
        if t == "user_message":
            self._append([""])
            self._append(_wrap(f"\033[1mYou:\033[0m {ev.get('content','')}", self.width))
        elif t == "assistant_text_delta":
            self.cur_text += ev.get("text", "")
            tag = "\033[1mClaude:\033[0m "
            new = _wrap(tag + self.cur_text, self.width)
            mk = "___S___"
            self.lines = [l for l in self.lines if not l.startswith(mk)] + [mk + l for l in new]
            if self.scroll == 0: self.redraw = True
        elif t == "assistant_text_final":
            mk = "___S___"
            self.lines = [l[len(mk):] if l.startswith(mk) else l for l in self.lines]
            self.cur_text = ""
        elif t == "tool_call":
            s = _summarize_args(ev.get("input", {}))
            self._append(_wrap(f"\033[36m> {ev.get('name','?')}({s})\033[0m", self.width))
        elif t == "tool_result":
            o = ev.get("output", "")
            if ev.get("is_error"):
                self._append(_wrap(f"\033[31m< ERROR: {o[:200]}\033[0m", self.width))
            else:
                self._append([f"\033[2m< [{len(o.encode('utf-8',errors='replace'))} bytes]\033[0m"])
        elif t == "error":
            self._append(_wrap(f"\033[31m! {ev.get('message','')}\033[0m", self.width))
        elif t == "approval_request":
            self.approval = ev; self.redraw = True

    def _handle_key(self, ch):
        if self.approval:
            if ch in (b"y", b"Y"):
                self.iq.put({"type": "approval", "approved": True})
            elif ch in (b"n", b"N"):
                self.iq.put({"type": "approval", "approved": False})
            else:
                return
            self.approval = None; self.redraw = True; return
        if ch in (b"\r", b"\n"):
            text = self.ibuf.strip()
            if text: self.ibuf = ""; self.cpos = 0; self.iq.put(text)
            self.redraw = True
        elif ch in (b"\x7f", b"\x08"):
            if self.cpos > 0:
                self.ibuf = self.ibuf[:self.cpos-1] + self.ibuf[self.cpos:]
                self.cpos -= 1; self.redraw = True
        elif ch == b"\x15":
            nl = self.ibuf.rfind("\n", 0, self.cpos)
            start = nl + 1 if nl >= 0 else 0
            if start == self.cpos and self.cpos > 0:
                # Already at line start — delete the newline to join with previous line
                self.ibuf = self.ibuf[:self.cpos-1] + self.ibuf[self.cpos:]
                self.cpos -= 1
            else:
                self.ibuf = self.ibuf[:start] + self.ibuf[self.cpos:]
                self.cpos = start
            self.redraw = True
        elif ch == b"\x01":
            self.cpos = 0; self.redraw = True
        elif ch == b"\x05":
            self.cpos = len(self.ibuf); self.redraw = True
        elif ch == b"\x03": self.running = False
        elif ch == b"\x0c": self.redraw = True
        elif ch == b"\x1b": self._esc()
        elif ch and ch[0:1].isascii() and ch[0] >= 32:
            c = ch.decode("utf-8", errors="replace")
            self.ibuf = self.ibuf[:self.cpos] + c + self.ibuf[self.cpos:]
            self.cpos += 1; self.redraw = True

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

    def _esc(self):
        if os.name == "nt": return
        r, _, _ = select.select([sys.stdin], [], [], 0.05)
        if not r: return
        ch2 = os.read(_fd, 1)
        if ch2 in (b"\r", b"\n"):
            self.ibuf = self.ibuf[:self.cpos] + "\n" + self.ibuf[self.cpos:]
            self.cpos += 1; self.redraw = True; return
        elif ch2 == b"b":
            self.cpos = self._word_left(); self.redraw = True; return
        elif ch2 == b"f":
            self.cpos = self._word_right(); self.redraw = True; return
        elif ch2 == b"\x7f":
            wp = self._word_left()
            self.ibuf = self.ibuf[:wp] + self.ibuf[self.cpos:]
            self.cpos = wp; self.redraw = True; return
        if ch2 != b"[": return
        seq = b""
        while True:
            r, _, _ = select.select([sys.stdin], [], [], 0.05)
            if not r: break
            b = os.read(_fd, 1); seq += b
            if b and b[0] >= 0x40: break
        if seq == b"D":
            if self.cpos > 0: self.cpos -= 1; self.redraw = True
        elif seq == b"C":
            if self.cpos < len(self.ibuf): self.cpos += 1; self.redraw = True
        elif seq == b"H" or seq == b"1~":
            self.cpos = 0; self.redraw = True
        elif seq == b"F" or seq == b"4~":
            self.cpos = len(self.ibuf); self.redraw = True
        elif seq == b"1;3D":
            self.cpos = self._word_left(); self.redraw = True
        elif seq == b"1;3C":
            self.cpos = self._word_right(); self.redraw = True
        elif seq == b"5~":
            vis = self.height - 5
            self.scroll = min(self.scroll + vis, max(0, len(self.lines) - vis))
            self.redraw = True
        elif seq == b"6~":
            vis = self.height - 5
            self.scroll = max(0, self.scroll - vis); self.redraw = True

    def run(self):
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-11), 7)
            except Exception: pass
        _set_cbreak(); atexit.register(_restore_term); _enter_alt()
        if os.name != "nt":
            signal.signal(signal.SIGWINCH, lambda *_: setattr(self, "redraw", True))
        try:
            self._full_redraw()
            while self.running:
                while True:
                    try: self._handle_event(self.eq.get_nowait())
                    except Exception: break
                if os.name == "nt":
                    import msvcrt
                    if msvcrt.kbhit(): self._handle_key(msvcrt.getch())
                else:
                    r, _, _ = select.select([sys.stdin], [], [], 0.02)
                    if r: self._handle_key(os.read(_fd, 1))
                if self.redraw: self._full_redraw()
        finally:
            _restore_term()


# ---------------------------------------------------------------------------
# Config & Main
# ---------------------------------------------------------------------------

@dataclass
class Config:
    model: str = "claude-sonnet-4-20250514"
    api_key_env: str = "ANTHROPIC_API_KEY"
    api_base_url: str = "https://api.anthropic.com"
    skills_dirs: list = field(default_factory=list)
    mcp_servers: list = field(default_factory=list)
    shell_enabled: bool = False
    max_tool_calls_per_turn: int = 10
    shell_timeout_seconds: int = 30
    tool_output_max_bytes: int = 32768
    log_dir: str = "~/.cog/logs"
    auto_approve: bool = False
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


def _expand_env(v):
    if isinstance(v, str):
        return re.sub(r"\$\{([^}]+)\}", lambda m: os.environ.get(m.group(1), ""), v)
    if isinstance(v, dict): return {k: _expand_env(val) for k, val in v.items()}
    if isinstance(v, list): return [_expand_env(i) for i in v]
    return v


def _load_config(path):
    path = os.path.expanduser(path)
    raw = {}
    if os.path.exists(path):
        if os.name != "nt":
            mode = os.stat(path).st_mode & 0o777
            if mode & 0o077:
                print(f"Warning: {path} is accessible by others (mode {oct(mode)}). "
                      f"Run: chmod 600 {path}", file=sys.stderr)
        with open(path) as f: raw = json.load(f)
    raw = _expand_env(raw)
    known = {f.name for f in Config.__dataclass_fields__.values()}
    cfg = Config(**{k: v for k, v in raw.items() if k in known})
    cfg.log_dir = os.path.expanduser(cfg.log_dir)
    cfg.skills_dirs = [os.path.expanduser(p) for p in cfg.skills_dirs]
    cfg.api_key = os.environ.get(cfg.api_key_env, "")
    return cfg


def _load_skills(dirs):
    skills = []
    for d in dirs:
        d = os.path.expanduser(d)
        if not os.path.isdir(d): continue
        for entry in os.listdir(d):
            sf = os.path.join(d, entry, "SKILL.md")
            if not os.path.isfile(sf): continue
            with open(sf) as f: text = f.read()
            lines = text.split("\n")
            if not lines or lines[0].strip() != "---":
                skills.append({"name": entry, "text": text}); continue
            name, i = entry, 1
            while i < len(lines) and lines[i].strip() != "---":
                if ":" in lines[i]:
                    k, v = lines[i].split(":", 1)
                    if k.strip() == "name": name = v.strip()
                i += 1
            body = "\n".join(lines[i+1:]).strip()
            if body: skills.append({"name": name, "text": body})
    return skills


def main():
    ap = argparse.ArgumentParser(description="cog - minimal coding agent")
    ap.add_argument("--config", default="~/.cog/config.json")
    ap.add_argument("--cwd", default=".")
    ap.add_argument("--shell", action="store_true")
    args = ap.parse_args()

    cfg = _load_config(args.config)
    cwd = os.path.abspath(args.cwd)
    if args.shell: cfg.shell_enabled = True
    if not cfg.api_key:
        print(f"Error: set {cfg.api_key_env} environment variable", file=sys.stderr)
        raise SystemExit(1)

    tools_configure(cwd=cwd, shell_enabled=cfg.shell_enabled,
                    shell_timeout=cfg.shell_timeout_seconds)
    skills = _load_skills(cfg.skills_dirs)
    prompt = _SYSTEM.format(cwd=cwd)
    for s in skills:
        prompt += f'\n<skill name="{s["name"]}">\n{s["text"]}\n</skill>\n'

    tool_reg = get_tools(cfg.shell_enabled)
    mcp_tools, _ = mcp_discover_all(cfg.mcp_servers)
    tool_reg.update(mcp_tools)

    os.makedirs(cfg.log_dir, exist_ok=True)
    log_path = os.path.join(cfg.log_dir, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S") + ".jsonl")
    log_f = open(log_path, "a")
    cfg.system_prompt = prompt
    cfg._log_fn = lambda ev: (log_f.write(json.dumps(ev) + "\n"), log_f.flush())

    eq, iq = queue.Queue(), queue.Queue()
    agent = Agent(cfg, tool_reg, eq, iq)
    threading.Thread(target=agent.worker_loop, daemon=True).start()
    TUI(eq, iq, model=cfg.model, cwd=cwd, tool_count=len(tool_reg)).run()
    iq.put(None)


if __name__ == "__main__":
    main()
