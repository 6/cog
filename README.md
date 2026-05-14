# cog

```
█▀▀ █▀█ █▀▀
█   █ █ █ █
▀▀▀ ▀▀▀ ▀▀▀
```

A small terminal coding agent. **Zero external dependencies** (just the Python 3.9+ standard library) and ships as a **single file** you can drop anywhere there's a Python interpreter.

Talks to the Anthropic Messages API, or anything else that speaks it: Minimax, Kimi, GLM (Z.ai), OpenRouter, Ollama, LM Studio, and so on.

## Install

cog is a single Python file with a shebang. Drop it on your `$PATH`:

```sh
curl -fsSL https://raw.githubusercontent.com/6/cog/main/cog.py -o ~/.local/bin/cog && chmod +x ~/.local/bin/cog
```

Make sure `~/.local/bin` is on your `$PATH`. Then `cog` is available globally.

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

### Other providers

cog speaks the Anthropic Messages API, so anything else that speaks it works too. Point `api_base_url` at the provider's endpoint, set `model` to whatever they call the model, and `api_key_env` to the env var holding your key. For example, to use Kimi:

```json
{
  "api_base_url": "https://api.moonshot.ai/anthropic",
  "model": "kimi-k2.6",
  "api_key_env": "MOONSHOT_API_KEY"
}
```

A few that work today (model names move fast, check the provider's docs for the current flagship):

| Provider | `api_base_url` | Example `model` |
|---|---|---|
| [Minimax](https://platform.minimax.io/docs/api-reference/text-anthropic-api) | `https://api.minimax.io/anthropic` | `MiniMax-M2.7` |
| [Kimi (Moonshot)](https://platform.moonshot.ai) | `https://api.moonshot.ai/anthropic` | `kimi-k2.6` |
| [GLM (Z.ai)](https://docs.z.ai/scenario-example/develop-tools/claude) | `https://api.z.ai/api/anthropic` | `glm-5.1` |
| [LM Studio](https://lmstudio.ai) (local) | `http://localhost:1234` | whatever you loaded |
| [Ollama](https://ollama.com) (local) | `http://localhost:11434` | whatever you pulled |

For local endpoints with auth disabled you can omit `api_key_env`.

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

cog itself has no runtime dependencies, so `python3 cog.py` is all you need to run it. Dev tasks are wired up through [mise](https://mise.jdx.dev):

```
mise install       # installs the pinned dev tools (ruff, basedpyright)
mise run test      # runs the unittest suite under tests/
mise run lint      # ruff check (with E701/E702 ignored, terse one-liners are intentional)
mise run typecheck # basedpyright
```

For scripts and CI, cog falls back to a line-oriented mode when stdin is not a TTY:

```
echo "list files in docs" | python3 cog.py --auto
```

In this mode slash commands are ignored, approval prompts are answered from the next stdin line, and output is plain text with no ANSI.
