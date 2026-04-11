# cog

Minimal terminal coding agent. Python 3.9+, stdlib only, single file.

Streams responses from the Anthropic Messages API (or compatible providers like Minimax), executes tools (file ops, shell, MCP), and loops until the task is complete.

## Usage

```
python3 cog.py
```

Requires `ANTHROPIC_API_KEY` set in your environment (or a different env var via `api_key_env` in config).

Add `--shell` to enable shell command execution. `--cwd PATH` to set the working directory.

## Config

Optional. Create `~/.cog/config.json`:

```json
{
  "model": "claude-sonnet-4-20250514",
  "api_base_url": "https://api.anthropic.com",
  "api_key_env": "ANTHROPIC_API_KEY",
  "shell_enabled": false,
  "auto_approve": false,
  "max_tool_calls_per_turn": 10,
  "shell_timeout_seconds": 30,
  "tool_output_max_bytes": 32768,
  "log_dir": "~/.cog/logs",
  "skills_dirs": [],
  "mcp_servers": [
    {
      "name": "local",
      "url": "http://127.0.0.1:8001/mcp",
      "headers": {"Authorization": "Bearer ${MCP_TOKEN}"}
    }
  ]
}
```

All fields are optional and fall back to defaults shown above. `${ENV_VAR}` syntax is expanded in string values.

### Using with Minimax

Minimax provides an [Anthropic-compatible API](https://platform.minimax.io/docs/api-reference/text-anthropic-api). Set `api_base_url`, `model`, and `api_key_env` in your config:

```json
{
  "api_base_url": "https://api.minimax.io/anthropic",
  "model": "MiniMax-M2.7",
  "api_key_env": "MINIMAX_API_KEY"
}
```

Then set `MINIMAX_API_KEY` in your environment and run `python3 cog.py`.

The same approach works for any provider that implements the Anthropic Messages API — change `api_base_url`, `model`, and `api_key_env`.

## Tools

- **read_file** / **write_file** / **str_replace** -- file operations
- **list_dir** -- directory listing
- **run_shell** -- shell commands (requires `--shell` or `shell_enabled`)
- **MCP tools** -- discovered from configured MCP servers

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| Enter | Submit |
| Opt+Enter | Newline |
| Opt+Left/Right | Word jump |
| Opt+Delete | Delete word |
| Cmd+Delete | Delete line |
| Ctrl+A / Ctrl+E | Home / End |
| Ctrl+C | Exit |
