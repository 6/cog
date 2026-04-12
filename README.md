# cog

```
█▀▀ █▀█ █▀▀
█   █ █ █ █
▀▀▀ ▀▀▀ ▀▀▀
```

Minimal terminal coding agent. **Zero external dependencies** — pure Python 3.9+ standard library, **single file** for portability. Drop `cog.py` anywhere a Python interpreter exists and run it.

Streams responses from the Anthropic Messages API (or compatible providers like OpenRouter, Minimax, Z.ai, Kimi, Ollama, and LM Studio), executes tools (file ops, shell, MCP), and loops until the task is complete.

## Install

cog is a single Python file with a shebang — you can run it from anywhere by dropping it on your `$PATH`.

**Symlink (tracks upstream changes):**

```sh
git clone https://github.com/<you>/cog ~/src/cog
chmod +x ~/src/cog/cog.py
ln -s ~/src/cog/cog.py ~/.local/bin/cog
```

**Copy (one-shot, no git):**

```sh
curl -o ~/.local/bin/cog https://raw.githubusercontent.com/<you>/cog/main/cog.py
chmod +x ~/.local/bin/cog
```

Either way, make sure `~/.local/bin` is on your `$PATH`. Then `cog` is available globally.

## Usage

```
cog
```

Requires `ANTHROPIC_API_KEY` set in your environment (or a different env var via `api_key_env` in config).

Flags: `--auto` to skip tool approval prompts, `--cwd PATH` to set working directory, `--verbose` for full API JSON.

## Config

Optional. Create `~/.config/cog/config.json` (respects `XDG_CONFIG_HOME`):

```json
{
  "model": "claude-sonnet-4-20250514",
  "api_base_url": "https://api.anthropic.com",
  "api_key_env": "ANTHROPIC_API_KEY",
  "auto_approve": false,
  "max_tool_calls_per_turn": 10,
  "shell_timeout_seconds": 30,
  "tool_output_max_bytes": 32768,
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

All fields are optional and fall back to defaults. `${ENV_VAR}` syntax is expanded in string values. Logs go to `~/.config/cog/logs/` and OAuth tokens to `~/.config/cog/tokens/`.

### Using with Minimax

Minimax provides an [Anthropic-compatible API](https://platform.minimax.io/docs/api-reference/text-anthropic-api). Set `api_base_url`, `model`, and `api_key_env` in your config:

```json
{
  "api_base_url": "https://api.minimax.io/anthropic",
  "model": "MiniMax-M2.7",
  "api_key_env": "MINIMAX_API_KEY"
}
```

Then set `MINIMAX_API_KEY` in your environment and run `cog`.

### Using with LM Studio

[LM Studio](https://lmstudio.ai) provides a local Anthropic-compatible endpoint. Load a model in LM Studio, then configure cog:

```json
{
  "api_base_url": "http://localhost:1234",
  "model": "ibm/granite-4-micro",
  "api_key_env": "LM_API_TOKEN"
}
```

Set `LM_API_TOKEN` in your environment (or omit `api_key_env` if LM Studio auth is disabled). The model name should match what you've loaded in LM Studio.

### Other providers

Any provider that implements the Anthropic Messages API works — change `api_base_url`, `model`, and `api_key_env`.

## Tools

- **read_file** / **write_file** / **str_replace** -- file operations
- **list_dir** -- directory listing
- **run_shell** -- shell commands
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

## Local development

cog itself has no runtime dependencies — `python3 cog.py` is all you need to run it. Dev tasks are wired up through [mise](https://mise.jdx.dev):

```
mise install       # installs the pinned dev tools (ruff, basedpyright)
mise run test      # runs the unittest suite under tests/
mise run lint      # ruff check (with E701/E702 ignored — terse one-liners are intentional)
mise run typecheck # basedpyright
```

For scripts and CI, cog falls back to a line-oriented mode when stdin is not a TTY:

```
echo "list files in docs" | python3 cog.py --auto
```

In this mode slash commands are ignored, approval prompts are answered from the next stdin line, and output is plain text with no ANSI.
