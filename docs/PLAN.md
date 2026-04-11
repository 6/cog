# Minimal Python Coding Agent — Implementation Plan

## Overview

Build a small, auditable, zero-dependency terminal coding agent in Python. The user types requests, the agent streams responses from the Anthropic Messages API, and executes tools (local file operations, shell commands, MCP server calls) in a loop until the task is complete. Everything is logged to an append-only JSONL trace.

**Hard constraints:**

- Python 3.9+ (runs on macOS Xcode CLI Tools stock Python, Ubuntu 24.04, GitHub Actions)
- Zero external dependencies — stdlib only
- Under 1000 lines of code (excluding blank lines and comments)
- Single interactive terminal session
- Unix-first, but Windows-compatible via ANSI escape codes

**Explicit non-goals for v1:**

- Multi-provider support (no OpenAI, Gemini, etc.)
- Provider abstraction layer
- stdio MCP transport
- Async/asyncio
- Browser UI, IDE integration, or plugin system
- Persistent multi-session database
- Autonomous background agents
- Complex approval policy engine
- Full MCP compliance

---

## File Structure and LOC Budget

```
agent/
  main.py           ~60 LOC    entrypoint, config, arg parsing, skill loading
  agent.py          ~200 LOC   conversation loop, event emission, tool dispatch
  anthropic_api.py  ~160 LOC   request building, SSE stream parsing
  mcp_client.py     ~150 LOC   JSON-RPC client, tool discovery, tool invocation
  tools.py          ~120 LOC   built-in tool implementations
  tui.py            ~250 LOC   ANSI TUI — status bar, transcript, input
```

**Total: ~940 LOC**

Every file should be readable end-to-end by one person in a sitting. No file should exceed 250 LOC. If a file grows past its budget, the scope is wrong — cut features, don't add files.

---

## 1. Config and Startup (`main.py`, ~60 LOC)

### Config format

Single JSON file. Default path: `~/.agent/config.json`. Override with `--config` flag.

```json
{
  "model": "claude-sonnet-4-20250514",
  "api_key_env": "ANTHROPIC_API_KEY",
  "system_prompt": "You are a coding agent. Use tools when helpful. Prefer small, safe, concrete steps. Explain actions briefly. Keep responses concise.",
  "skills_dirs": ["~/.agent/skills"],
  "mcp_servers": [
    {
      "name": "local-tools",
      "url": "http://127.0.0.1:8001/mcp",
      "headers": {"Authorization": "Bearer ${MCP_LOCAL_TOKEN}"}
    }
  ],
  "shell_enabled": false,
  "max_tool_calls_per_turn": 10,
  "shell_timeout_seconds": 30,
  "tool_output_max_bytes": 32768,
  "log_dir": "~/.agent/logs"
}
```

**Config loading rules:**

1. Read and parse the JSON file
2. Expand `~` in all path fields
3. Expand `${ENV_VAR}` references in string values (used for MCP headers)
4. Read the API key from the env var named in `api_key_env`
5. Apply defaults for any missing fields
6. Validate: API key must be present, model must be set

### CLI args

Minimal. Only:

- `--config PATH` — config file path
- `--cwd PATH` — working directory (default: current dir)
- `--shell` — enable shell tool (overrides config)

Parse with `argparse`. No subcommands.

### Skill loading

Skills follow the official directory-based format. Each skill is a directory containing a `SKILL.md` file with YAML-like frontmatter.

**Frontmatter parser** (~15 lines): Read the file. If it starts with `---`, read lines until the next `---`. For each line in that block, split on the first `:` to get key/value. Strip whitespace. Only extract `name` and `description`. Everything else is ignored.

```
---
name: python-editing
description: Guidance for editing Python projects safely
---

When editing Python files:
- prefer small patches
- run tests if present
```

The markdown body (everything after the closing `---`) is the skill text.

**Loading:** Walk each configured `skills_dirs` directory. For each subdirectory containing a `SKILL.md`, parse it. Collect all skills into a list of `{name, description, text}` dicts. All loaded skills are injected into the system prompt — no search, no ranking, no selection in v1.

### Startup sequence

1. Parse CLI args
2. Load and validate config
3. Load skills from configured directories
4. Discover MCP tools from configured servers (see §5)
5. Register built-in tools (see §4)
6. Create JSONL trace file
7. Start TUI (see §6)

---

## 2. Anthropic API Client (`anthropic_api.py`, ~160 LOC)

### Request building

Build Anthropic Messages API requests as plain dicts. The function signature should be something like:

```python
def build_request(model, system, messages, tools, max_tokens=4096):
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "tools": tools,
        "stream": True
    }
```

**System prompt construction:** Concatenate the base system prompt with all loaded skill texts. Each skill should be wrapped in a clear delimiter:

```
<skill name="python-editing">
When editing Python files:
- prefer small patches
- run tests if present
</skill>
```

**Tool schemas:** Both built-in tools and MCP tools must be converted to Anthropic's tool format:

```json
{
  "name": "read_file",
  "description": "Read the contents of a file at the given path.",
  "input_schema": {
    "type": "object",
    "properties": {
      "path": {"type": "string", "description": "File path to read"}
    },
    "required": ["path"]
  }
}
```

### HTTP client

Use `http.client.HTTPSConnection` directly. No `urllib.request` — `http.client` gives more control over streaming.

```python
import http.client
import json

def stream_request(api_key, request_body):
    conn = http.client.HTTPSConnection("api.anthropic.com")
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": api_key,
        "anthropic-version": "2023-06-01"
    }
    conn.request("POST", "/v1/messages", json.dumps(request_body), headers)
    response = conn.getresponse()
    if response.status != 200:
        body = response.read().decode()
        raise Exception(f"API error {response.status}: {body}")
    return response  # caller reads SSE lines from this
```

### SSE stream parser

Anthropic's streaming uses Server-Sent Events. The parser reads lines from the HTTP response and yields parsed events.

**Lines to handle:**

- `event: <event_type>` — sets the event type for the next data line
- `data: <json>` — the payload
- blank lines — event boundary (ignored, we emit on each data line)

**Events to parse (the minimal set):**

| Event | What to do |
|---|---|
| `message_start` | Extract `message.id`, `message.usage` for tracking |
| `content_block_start` | Note block index and type (`text` or `tool_use`). For `tool_use`, extract `name` and `id` |
| `content_block_delta` | For `text_delta`: yield the text fragment for streaming display. For `input_json_delta`: accumulate the `partial_json` string |
| `content_block_stop` | For `tool_use` blocks: `json.loads()` the accumulated input string to get the tool arguments. Yield the complete tool call |
| `message_delta` | Extract `stop_reason` and final `usage` |
| `message_stop` | End of message |
| `ping` | Ignore |
| Anything else | Log it, ignore it |

**Critical implementation detail:** Tool input arrives as incremental JSON string fragments via `input_json_delta`. You must accumulate these fragments as raw strings and only `json.loads()` at `content_block_stop`. Do not try to parse partial JSON.

**Parser structure:**

```python
def parse_sse_stream(response):
    """Yields (event_type, parsed_data) tuples."""
    event_type = None
    for line in read_lines(response):
        line = line.decode("utf-8").rstrip("\r\n")
        if line.startswith("event: "):
            event_type = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
            yield (event_type, data)
            event_type = None
        # blank lines and comments (":") are ignored
```

**Line reading:** `http.client` responses support `.readline()` but behavior can be tricky with chunked transfer encoding. Use a wrapper that reads from the response and splits on `\n`. The response object from `http.client` is a `http.client.HTTPResponse` which is a `BufferedIOBase` — calling `.readline()` on it works for reading SSE lines.

---

## 3. Agent Loop (`agent.py`, ~200 LOC)

### Core loop

The agent loop is the heart of the system. It manages conversation state and runs tool calls.

```
1. User submits input
2. Append {"role": "user", "content": user_text} to messages
3. Build request (system + messages + tools)
4. Stream response, emitting events to TUI and trace
5. Collect all content blocks from the response
6. If any content block is tool_use:
   a. For each tool_use block, execute the tool
   b. Append the assistant message (all content blocks) to messages
   c. Append a tool_result message with results for each tool call
   d. Go to step 3
7. If no tool_use (text only): turn is complete, wait for next user input
```

### Message format

Maintain the conversation as a list of Anthropic-format messages:

```python
messages = [
    {"role": "user", "content": "Fix the failing test in tests/test_parser.py"},
    {"role": "assistant", "content": [
        {"type": "text", "text": "I'll read the test file first."},
        {"type": "tool_use", "id": "toolu_01abc", "name": "read_file", "input": {"path": "tests/test_parser.py"}}
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_01abc", "content": "...file contents..."}
    ]}
]
```

**Important:** When the assistant response contains tool_use blocks, append the full assistant message (including both text and tool_use blocks) to messages. Then append a single user message containing all tool_result blocks.

### Event model

The entire system runs on a single stream of event dicts. Both the TUI and the JSONL trace consume the same events. This is the most important architectural decision — it keeps the system simple.

**Event types:**

```python
{"type": "session_start", "ts": ..., "model": ..., "cwd": ...}
{"type": "user_message", "ts": ..., "content": "..."}
{"type": "assistant_text_delta", "ts": ..., "text": "..."}
{"type": "assistant_text_final", "ts": ..., "text": "...full text..."}
{"type": "tool_call", "ts": ..., "tool_id": "...", "name": "...", "input": {...}}
{"type": "tool_result", "ts": ..., "tool_id": "...", "output": "...", "is_error": false}
{"type": "status", "ts": ..., "message": "..."}
{"type": "error", "ts": ..., "message": "..."}
{"type": "turn_complete", "ts": ..., "usage": {"input_tokens": ..., "output_tokens": ...}}
{"type": "session_end", "ts": ...}
```

All events include `"ts"` (ISO 8601 timestamp).

### Guardrails

- **Max tool calls per turn:** Default 10. Count tool executions per user-initiated turn. If exceeded, append an error message and stop the loop.
- **Tool output truncation:** If a tool returns more than `tool_output_max_bytes` (default 32KB), truncate with a clear `[truncated]` marker.
- **Shell timeout:** `subprocess.run()` with `timeout` parameter. Default 30 seconds.
- **Request timeout:** Set a reasonable timeout on the HTTP connection. 120 seconds for the initial response, then the stream can run longer.

### Tool dispatch

When a tool_use block is received:

1. Look up the tool name in the registry
2. If it's a built-in tool, call the Python function directly
3. If it's an MCP tool (prefixed with the server name when there are multiple servers), call the MCP server
4. If the tool is not found, return an error result
5. For write/shell tools when approval is configured, emit an approval request event and wait

**Approval model (simple):** Read-only tools (`read_file`, `list_dir`) auto-execute. Write tools (`write_file`, `str_replace`), shell, and MCP tools prompt for approval via the TUI. The user presses `y` or `n`. This can be disabled globally in config with an `"auto_approve": true` field.

### Threading model

- **Main thread:** Owns the TUI. Runs the curses/ANSI event loop, polls for events from the agent, redraws the screen.
- **Worker thread:** Runs the agent loop (HTTP streaming, tool execution). Pushes events onto a `queue.Queue`.
- Communication is one-way: worker → main via the queue. User input goes main → worker via a separate small queue or threading.Event.

```python
import threading
import queue

event_queue = queue.Queue()
input_queue = queue.Queue()

def agent_worker():
    while True:
        user_input = input_queue.get()
        if user_input is None:
            break
        run_turn(user_input, event_queue)

thread = threading.Thread(target=agent_worker, daemon=True)
thread.start()
```

---

## 4. Built-in Tools (`tools.py`, ~120 LOC)

Five built-in tools. Each is a simple function that takes a dict of arguments and returns a string result (or error string).

### `read_file`

```python
def read_file(path):
    """Read the contents of a file at the given path."""
    # Resolve relative to cwd
    # Return file contents as string
    # On error (not found, permission denied, binary): return error message
```

**Schema:**
```json
{
  "properties": {
    "path": {"type": "string", "description": "File path to read (relative to cwd or absolute)"}
  },
  "required": ["path"]
}
```

### `write_file`

```python
def write_file(path, content):
    """Write content to a file, creating parent directories if needed."""
    # Create parent dirs with os.makedirs(exist_ok=True)
    # Write the content
    # Return confirmation with byte count
```

**Schema:**
```json
{
  "properties": {
    "path": {"type": "string", "description": "File path to write"},
    "content": {"type": "string", "description": "Content to write to the file"}
  },
  "required": ["path", "content"]
}
```

### `str_replace`

This is the critical editing tool. Exact-match search and replace.

```python
def str_replace(path, old_str, new_str):
    """Replace an exact string match in a file. Fails if old_str is not found or matches more than once."""
    content = Path(path).read_text()
    count = content.count(old_str)
    if count == 0:
        return "ERROR: old_str not found in file"
    if count > 1:
        return f"ERROR: old_str matched {count} times, must be unique. Add more surrounding context to make it unique."
    new_content = content.replace(old_str, new_str, 1)
    Path(path).write_text(new_content)
    return "OK: replacement made"
```

**Schema:**
```json
{
  "properties": {
    "path": {"type": "string", "description": "File path to edit"},
    "old_str": {"type": "string", "description": "Exact string to find (must appear exactly once)"},
    "new_str": {"type": "string", "description": "String to replace it with"}
  },
  "required": ["path", "old_str", "new_str"]
}
```

**Design note:** The model is instructed (in the tool description) that `old_str` must be a unique exact match. If it fails, the model should retry with more context lines around the target. Claude is good at this pattern.

### `list_dir`

```python
def list_dir(path="."):
    """List directory contents, one entry per line, with type indicators."""
    # Return entries like:
    #   [dir]  src/
    #   [file] README.md
    # Non-recursive. Return error if path is not a directory.
```

**Schema:**
```json
{
  "properties": {
    "path": {"type": "string", "description": "Directory path (default: current directory)"}
  },
  "required": []
}
```

### `run_shell`

```python
def run_shell(command):
    """Run a shell command and return stdout+stderr."""
    # Only available if shell_enabled is True in config
    # subprocess.run(command, shell=True, capture_output=True, text=True, timeout=shell_timeout, cwd=cwd)
    # Combine stdout + stderr in output
    # Include exit code in result
```

**Schema:**
```json
{
  "properties": {
    "command": {"type": "string", "description": "Shell command to execute"}
  },
  "required": ["command"]
}
```

**Safety:** If `shell_enabled` is false in config and the model tries to use `run_shell`, return an error: `"Shell is disabled. Enable with --shell flag or shell_enabled in config."` Do not include `run_shell` in the tools list sent to the API if shell is disabled.

### Tool registry

Simple dict mapping tool names to `(function, schema)` tuples. MCP tools are added to the same registry after discovery.

```python
TOOLS = {
    "read_file": (read_file_fn, read_file_schema),
    "write_file": (write_file_fn, write_file_schema),
    "str_replace": (str_replace_fn, str_replace_schema),
    "list_dir": (list_dir_fn, list_dir_schema),
    "run_shell": (run_shell_fn, run_shell_schema),  # only if enabled
}
```

---

## 5. MCP Client (`mcp_client.py`, ~150 LOC)

### Scope

Tools-only MCP client over Streamable HTTP. Pin to a single MCP spec revision (2025-03-26). Support only `initialize`, `tools/list`, and `tools/call`. No GET streams, no resumability, no prompts, no resources, no roots, no sampling, no OAuth.

### Transport

MCP Streamable HTTP uses JSON-RPC 2.0 over HTTP POST.

**Request format:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/list",
  "params": {}
}
```

**Response handling:** The server may respond with either:
- `Content-Type: application/json` — a single JSON-RPC response
- `Content-Type: text/event-stream` — an SSE stream of JSON-RPC messages

For tool invocations you'll mostly get plain JSON responses. But the client must handle both. For SSE responses, read events the same way as the Anthropic parser (look for `data:` lines), parse JSON, and collect the result.

### Session management

After `initialize`, if the server returns a `Mcp-Session-Id` header, include it in all subsequent requests. Store one session ID per server.

### Connection lifecycle

For each configured MCP server:

1. **Initialize:** POST `initialize` with client capabilities (just `{"tools": {}}`)
2. **Receive server capabilities:** Note if the server supports tools
3. **Send initialized notification:** POST `notifications/initialized` (no id field, it's a notification)
4. **List tools:** POST `tools/list`, parse the tool schemas
5. **Register tools:** Add each tool to the agent's tool registry with its schema

### Tool invocation

When the agent needs to call an MCP tool:

```python
def call_mcp_tool(server, tool_name, arguments):
    """POST tools/call to the MCP server."""
    # Build JSON-RPC request
    # Include Mcp-Session-Id header if present
    # Parse response (JSON or SSE)
    # Return the tool result content
```

### Tool naming

When only one MCP server is configured, use tool names as-is from the server. When multiple servers are configured, prefix with server name: `servername__toolname` (double underscore separator). The agent strips the prefix before sending to the MCP server.

### Error handling

- Connection failures: log error, mark server as unavailable, exclude its tools
- Tool call failures: return error string as tool result (the model will see it and adapt)
- Timeout: 30 second default for MCP calls
- Do not retry automatically in v1

### Headers

For each server, send:
```
Content-Type: application/json
Accept: application/json, text/event-stream
Mcp-Session-Id: <if present>
```

Plus any custom headers from the server's config (used for auth tokens).

---

## 6. TUI (`tui.py`, ~250 LOC)

### Why ANSI, not curses

The `curses` module is not available on Windows Python. ANSI escape codes work on macOS, Linux, and Windows 10+ natively. This lets the agent run on stock Python everywhere without external dependencies.

### Windows compatibility shim

On Windows, ANSI support must be enabled via `ctypes`:

```python
import sys, os

def enable_ansi():
    if os.name == 'nt':
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
```

### Terminal raw mode

Need raw/cbreak mode for keypress handling without waiting for Enter.

```python
if os.name == 'nt':
    import msvcrt
    def read_key():
        ch = msvcrt.getwch()
        return ch
else:
    import tty, termios
    def read_key():
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)  # cbreak, not full raw — allows Ctrl+C
            return sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
```

### Screen layout

Three regions, top to bottom:

```
┌─────────────────────────────────────────────┐
│ model: claude-sonnet │ cwd: /project │ tools: 5 │   ← Status bar (row 0)
├─────────────────────────────────────────────┤
│                                             │
│ You: Fix the failing test                   │   ← Transcript
│                                             │      (rows 1 to height-3)
│ Claude: I'll read the test file first.      │
│ ▶ read_file(path="tests/test_parser.py")    │
│ ◀ [842 bytes]                               │
│ Claude: The test expects...                 │
│                                             │
├─────────────────────────────────────────────┤
│ > type your message here_                   │   ← Input line (last 2 rows)
└─────────────────────────────────────────────┘
```

### ANSI escape code reference

The TUI needs these escape sequences:

| Purpose | Sequence |
|---|---|
| Enter alternate screen | `\033[?1049h` |
| Exit alternate screen | `\033[?1049l` |
| Move cursor | `\033[{row};{col}H` |
| Clear entire line | `\033[2K` |
| Clear screen | `\033[2J` |
| Set scroll region | `\033[{top};{bottom}r` |
| Scroll up one line | `\033[S` |
| Bold text | `\033[1m` |
| Dim text | `\033[2m` |
| Reset formatting | `\033[0m` |
| Color (e.g. cyan) | `\033[36m` |
| Hide cursor | `\033[?25l` |
| Show cursor | `\033[?25h` |

### Scroll region

The key technique: set the terminal's scroll region to the transcript area only. When new lines are added and the transcript is full, the terminal scrolls automatically within that region. The status bar and input line stay fixed.

```python
def set_scroll_region(top, bottom):
    sys.stdout.write(f"\033[{top};{bottom}r")

# On startup and resize:
height, width = os.get_terminal_size()
set_scroll_region(2, height - 2)  # transcript area
```

### Transcript rendering

The transcript is an append-only list of rendered lines. When new events arrive (from the queue), convert them to display lines and append:

- **user_message:** `"You: {text}"` in bold
- **assistant_text_delta:** Append to current assistant line, no newline until complete
- **assistant_text_final:** Finalize the line
- **tool_call:** `"▶ {name}({args_summary})"` in cyan
- **tool_result:** `"◀ [{byte_count} bytes]"` in dim, or `"◀ ERROR: {msg}"` in red
- **error:** `"✖ {message}"` in red
- **status:** `"⋯ {message}"` in dim

Word-wrap long lines to terminal width. Track total lines for scroll offset.

### Scrolling

Keep a simple integer `scroll_offset`. Default is 0 (showing the bottom). `PgUp` increases offset, `PgDn` decreases it (min 0). When new content arrives and `scroll_offset == 0`, auto-scroll. If user has scrolled up, don't auto-scroll (just increment total lines).

### Input handling

Single-line input with basic editing:

- Printable characters: insert at cursor position
- Backspace: delete character before cursor
- Enter: submit the line, clear the input
- Ctrl+C: clean exit (restore terminal, exit alternate screen)
- Ctrl+L: full redraw
- PgUp/PgDn: scroll transcript (these arrive as escape sequences: `\033[5~` and `\033[6~`)

**Escape sequence detection:** After reading `\033`, read the next character. If `[`, read until a letter to get the full sequence. This handles arrow keys, PgUp/PgDn, etc.

### Resize handling

On Unix, handle `SIGWINCH`:

```python
import signal
def on_resize(signum, frame):
    global needs_redraw
    needs_redraw = True
signal.signal(signal.SIGWINCH, on_resize)
```

On Windows, poll `os.get_terminal_size()` periodically (every 500ms in the main loop).

On resize: recalculate scroll region, re-render status bar and input, redraw visible transcript lines.

### Main TUI loop

```python
def run_tui(event_queue, input_queue):
    enable_ansi()
    enter_alternate_screen()
    set_raw_mode()
    try:
        draw_full_screen()
        while True:
            # 1. Drain events from agent
            while not event_queue.empty():
                event = event_queue.get_nowait()
                append_to_transcript(event)
                log_event(event)  # JSONL
            # 2. Check for keypress (non-blocking or short timeout)
            if key_available():
                handle_keypress(read_key())
            # 3. Redraw if needed
            if needs_redraw:
                draw_full_screen()
                needs_redraw = False
            time.sleep(0.02)  # ~50fps cap
    finally:
        restore_terminal()
        exit_alternate_screen()
```

---

## 7. JSONL Tracing

### File location

One file per session: `{log_dir}/{timestamp}.jsonl`

Example: `~/.agent/logs/2025-01-15T10-30-00.jsonl`

### Format

One JSON object per line. Same event objects that drive the TUI:

```jsonl
{"type":"session_start","ts":"2025-01-15T10:30:00Z","model":"claude-sonnet-4-20250514","cwd":"/project"}
{"type":"user_message","ts":"2025-01-15T10:30:05Z","content":"Fix the failing test"}
{"type":"assistant_text_delta","ts":"2025-01-15T10:30:06Z","text":"I'll "}
{"type":"tool_call","ts":"2025-01-15T10:30:07Z","tool_id":"toolu_01abc","name":"read_file","input":{"path":"tests/test_parser.py"}}
{"type":"tool_result","ts":"2025-01-15T10:30:07Z","tool_id":"toolu_01abc","output":"...","is_error":false}
{"type":"turn_complete","ts":"2025-01-15T10:30:15Z","usage":{"input_tokens":1200,"output_tokens":350}}
```

### Writer

Trivial — open the file in append mode, `json.dumps` each event, write + newline + flush.

```python
def make_logger(log_path):
    f = open(log_path, "a")
    def log(event):
        f.write(json.dumps(event) + "\n")
        f.flush()
    return log
```

The logger is called from the TUI main loop, same place events are consumed from the queue. This ensures the trace and transcript are always in sync.

**Do not log `assistant_text_delta` events** — they are too noisy. Instead, log `assistant_text_final` with the complete text. The deltas are only for real-time streaming display.

---

## 8. System Prompt

Keep it short and operational. Do not overengineer prompt layers.

```
You are a coding agent working in the directory: {cwd}

You have access to tools for reading, writing, and editing files, listing directories, and optionally running shell commands. Use them when helpful.

Guidelines:
- Take small, concrete steps. Read before writing.
- Use str_replace for targeted edits. Use write_file for new files or complete rewrites.
- Explain what you're doing briefly before each action.
- If a tool call fails, read the error and adjust.
- Do not invent file contents or tool outputs.
- Keep responses concise unless the user asks for detail.

{skills_text}
```

Where `{skills_text}` is the concatenated skill contents wrapped in `<skill>` tags.

---

## 9. Implementation Order

Build in this order. Each milestone should be testable independently.

### Milestone 1 — Headless core (no TUI)

Build everything except `tui.py`. Use a simple `input()`/`print()` REPL loop instead.

**Test:** Run the agent, type a request, see it stream text and execute tools via print statements.

Files: `main.py`, `agent.py`, `anthropic_api.py`, `tools.py`

### Milestone 2 — MCP client

Add MCP server connection, tool discovery, and tool calling.

**Test:** Configure a local MCP server, verify its tools appear in the tool list and can be called.

Files: `mcp_client.py`, updates to `agent.py`

### Milestone 3 — ANSI TUI

Replace the print-based REPL with the full ANSI TUI.

**Test:** Launch the agent, see the three-region layout, type a message, watch streaming text and tool calls in the transcript, scroll up and down.

Files: `tui.py`, updates to `main.py`

### Milestone 4 — Hardening

- Terminal resize handling
- Tool output truncation
- Better error display
- Approval prompts for write/shell/MCP tools
- Edge cases: empty responses, malformed JSON from API, tool not found, network errors
- Clean exit on Ctrl+C (restore terminal state)

---

## 10. Key Design Decisions Summary

| Decision | Choice | Rationale |
|---|---|---|
| Python version | 3.9+ | Runs on stock macOS Xcode CLI Tools (3.9.6) |
| Config format | JSON | No deps. `tomllib` requires 3.11+ |
| TUI library | ANSI escape codes | Works on macOS, Linux, Windows. No curses (unavailable on Windows) |
| HTTP client | `http.client` | stdlib, supports streaming line reads |
| Async model | threading + queue | Simpler than asyncio, curses/ANSI owns main thread |
| Editing tool | `str_replace` (exact match) | ~10 LOC, no diff parsing, Claude handles this format well |
| MCP scope | tools-only, Streamable HTTP | Minimal subset, no GET streams or non-tool features |
| Skills selection | Load all from configured dirs | No search/ranking complexity in v1 |
| Tracing | Append-only JSONL | Same events drive TUI and log |
| Provider support | Anthropic only | No abstraction layer needed |

