Start by verifying the three risks that could kill the project before writing any real code.

**Week 0: Spike three things (~2 hours)**

Write three tiny standalone Nim programs, 20-30 lines each:

**Spike 1 — SSE streaming from Anthropic:**
```nim
# Can httpclient read response body line-by-line during streaming?
import httpclient, json
let client = newHttpClient(timeout = 30000)
client.headers = newHttpHeaders({"Content-Type": "application/json", "X-API-Key": getEnv("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"})
let body = $(%*{"model": "claude-sonnet-4-20250514", "max_tokens": 100, "stream": true, "messages": [{"role": "user", "content": "Say hello"}]})
let resp = client.post("https://api.anthropic.com/v1/messages", body)
# Try reading lines — does this work incrementally or buffer everything?
for line in resp.bodyStream.lines:
  echo line
```

If `httpclient` buffers the whole response, drop to raw `net.Socket` with TLS. Write that fallback spike too — it's ~30 lines with `newContext()` and manual HTTP request writing. You need to know which path you're taking before building anything else.

**Spike 2 — `getch()` and escape sequences:**
```nim
import terminal
while true:
  let ch = getch()
  echo "got: ", ord(ch), " ", repr(ch)
  if ch == 'q': break
```

Press arrow keys, Opt+Left, PgUp, Ctrl+C. Verify that `getch()` returns `\033` as a raw byte and lets you read the subsequent `[A` etc. in follow-up `getch()` calls. If it interprets or blocks on escape sequences, you have a problem.

**Spike 3 — SHA256 via OpenSSL FFI:**
```nim
proc SHA256(d: cstring, n: csize_t, md: ptr array[32, byte]): ptr array[32, byte] {.importc, header: "<openssl/sha.h>".}
var hash: array[32, byte]
let input = "test verifier string"
discard SHA256(input.cstring, input.len.csize_t, addr hash)
echo hash  # should be 32 bytes of non-zero data
```

Compile with `nim c -d:ssl spike3.nim`. If this works, OAuth PKCE is solved. If not, figure out the right OpenSSL include path for your platform before going further.

**If all three spikes pass, proceed. If any fails, solve it before writing production code.**

---

**Phase 1: Headless agent loop (~400 LOC, 2-3 days)**

Build a working agent with `stdin.readLine()` / `echo` instead of a TUI. This is the same approach as the Python implementation plan — prove the loop works first.

Single file: `cog.nim`

Build in this order within the file:

1. **Config loading** (~60 LOC) — `Config` object, read `~/.cog/config.json`, expand env vars, walk up for local `.cog/config.json`. Port directly from Python.

2. **Tool implementations** (~100 LOC) — `read_file`, `write_file`, `str_replace`, `list_dir`, `run_shell`. Port line by line from Python. Use `osproc.execCmdEx` for shell with a timeout thread.

3. **Anthropic SSE client** (~100 LOC) — request building, streaming parser. Use whichever approach worked in spike 1. Same event types as Python: `text_delta`, `text_final`, `tool_use`, `stop`, `usage`.

4. **Agent loop** (~120 LOC) — conversation state, tool dispatch, retry logic, guardrails. Port directly from Python's `Agent` class. For now, run synchronously in the main thread.

5. **JSONL tracing** (~10 LOC) — open file, write events, flush.

6. **Skill loading** (~20 LOC) — walk directories, parse frontmatter, inject into system prompt.

Test: `nim c -d:release -d:ssl cog.nim && ./cog`. Type "list the files in this directory", watch it call `list_dir` and respond. Try `str_replace` on a test file. Verify retry logic by temporarily using a bad API key.

---

**Phase 2: TUI (~350 LOC, 3-4 days)**

Replace the `readLine`/`echo` loop with the real terminal UI. This is where Nim pays off.

**Step 1: Three-thread architecture**

```
Main thread:  TUI rendering, polls keyChan + eventChan
Agent thread: HTTP streaming, tool execution → sends to eventChan  
Input thread: blocking getch() loop → sends to keyChan
```

Set up the channels and threads first, wire them to the existing agent loop. The agent's `emit` function now sends to `eventChan` instead of printing.

**Step 2: Screen setup/teardown**

```nim
import terminal
# Enter
stdout.hideCursor()
stdout.write("\033[?1049h")  # alternate screen — only raw ANSI you need
# Exit (atexit + signal handler)
stdout.write("\033[?1049l")
stdout.showCursor()
resetAttributes(stdout)
```

Write a `withTerminal` template or use `defer` to guarantee cleanup on exit/crash.

**Step 3: Status bar + input line rendering**

Use `terminal` module: `setCursorPos`, `eraseLine`, `setForegroundColor`, `setStyle`, `resetAttributes`. Port the prompt format from Python — cwd, git branch, caret color based on token count.

**Step 4: Event rendering in transcript area**

Port `_handle_event` from Python. Same logic — streamed text deltas print inline, tool calls show `▶ name(args)`, tool results show `◀ [N bytes]`, errors in red. Use `terminal` colors instead of raw ANSI.

**Step 5: Input handling**

Port the key handling from Python. In Nim this is cleaner because `getch()` handles raw mode internally:

- Printable chars → insert at cursor
- Backspace → delete  
- Enter → submit
- Ctrl+C → clean exit
- Escape sequences → parse same as Python (read `\033`, `[`, then accumulate)

Port: history (Up/Down), word navigation (Opt+Left/Right), Opt+Delete, Ctrl+A/E, ghost completion on slash commands.

**Step 6: Scroll**

Same as Python — integer `scrollOffset`, PgUp increments, PgDn decrements, auto-scroll when at bottom.

**Step 7: Slash commands**

Port `/help`, `/clear`, `/tokens`, `/model`, `/quit` from Python. Leave `/mcp` commands for phase 3.

**Step 8: Approval flow**

Single-keypress `y`/`n` via `getch()` when a write/shell tool call arrives. Agent thread blocks on channel receive.

Test: Full interactive session — type a request, watch streaming, see tool calls, scroll up and down, use history, try `/tokens`.

---

**Phase 3: MCP client + OAuth (~300 LOC, 2-3 days)**

Put this in `cog_mcp.nim`, imported by `cog.nim`.

**Step 1: MCP client core (~80 LOC)**

JSON-RPC 2.0 over HTTP POST. Port `_mcp_post`, `mcp_initialize`, `mcp_call_tool`, `mcp_discover_all` from Python. Use `httpclient` — MCP tool calls are normal request/response, not streaming.

**Step 2: OAuth 2.1 (~180 LOC)**

Port the full OAuth flow from Python:
- Metadata discovery
- Dynamic client registration
- PKCE with SHA256 (using the FFI approach from spike 3)
- Callback server — use `net.Socket` directly: bind, listen, accept one connection, read GET, extract `code` param, send HTML response, close. ~25 lines, no async needed.
- `openDefaultBrowser()` from Nim's `browsers` module
- Token caching to `~/.cog/tokens/{name}.json`
- Token refresh on 401

**Step 3: Wire `/mcp` commands**

Port `/mcp`, `/mcp auth`, `/mcp revoke` slash commands from Python.

Test: Configure an MCP server, run `/mcp auth name`, complete browser flow, verify tools appear, call an MCP tool from the agent.

---

**Phase 4: Hardening (1-2 days)**

- Resize handling: `terminalSize()` on each frame, redraw if changed
- Clean exit on Ctrl+C and SIGTERM (restore terminal)
- Multi-model switching: `/model` command, channel message to agent thread
- Edge cases: empty API response, malformed JSON, tool not found, network timeout
- Tool output truncation
- Verbose mode (`--verbose` flag)

---

**File layout when done:**

```
cog/
  cog.nim         ~1100 LOC   everything except MCP
  cog_mcp.nim     ~300 LOC    MCP client + OAuth 2.1
```

Compile: `nim c -d:release -d:ssl cog.nim` → single `cog` binary, ~1-2MB.

---

**Practical tips for the port:**

Don't try to write idiomatic Nim from day one. Start by translating Python line-by-line into Nim — it'll look ugly but it'll work. Refactor to idiomatic Nim after things pass tests.

Use `JsonNode` liberally. Resist the urge to type everything early. Your Python code uses dicts for events, messages, and config — `JsonNode` is the same thing in Nim. Type the hot paths later.

Compile and test after every 50 lines. Nim's compiler catches a lot but error messages can be cryptic for beginners. Small increments keep you sane.

Keep the Python version running alongside. When something behaves weird in Nim, run the same scenario in Python to see what the correct behavior should be.


# Further context/discussion if helpful:

Yes — widening the net changes the picture a bit.

Given your current code, the real pain is not “building a TUI” in the full-screen sense. It is building a **good line editor** inside a normal scrolling terminal: word movement, word delete, history, completion, approvals, redraws, and streamed output without clobbering the prompt. Your code is already firmly in that model rather than a pane-based screen takeover. 

My updated take is:

* **Best literal fit for “native binary + zero external deps”**: **Nim**
* **Best fit for “delete the most editor/TUI LOC”**: **Deno**, specifically by leaning on `node:readline`
* **Best overlooked option**: **Ruby**, because `Reline` is surprisingly capable and ships in the standard library
* **Best stay-put option**: **Python**, but only if you simplify the UX instead of trying to perfect the custom editor

The most important correction to my earlier answer is this: **Deno is stronger than I gave it credit for on the exact keybindings you care about.**

### Deno is better than it looks for your editor problem

Deno itself gives you raw terminal mode via `Deno.stdin.setRaw(true)`. It also exposes `node:readline`, and Deno’s Node-compat docs show that `node:readline` is available, including `createInterface`, `emitKeypressEvents`, `cursorTo`, `moveCursor`, `history`, and `completer` support. ([Deno][1])

And Node’s official `readline` docs are unusually relevant to your exact UX requirements. They explicitly document built-in TTY keybindings including:

* `Meta+B` / `Meta+F` for word-left / word-right
* `Meta+D` or `Meta+Delete` for delete-word-right
* `Ctrl+W` for delete backward to a word boundary
* `Ctrl+U`, `Ctrl+K`, `Ctrl+A`, `Ctrl+E`
* built-in history and `completer` support for tab completion. ([Node.js][2])

That means if your terminal maps macOS Option to Meta, **Deno + node:readline gets surprisingly close to your desired prompt UX without hand-parsing escape sequences**. For your current “normal scrollback + smart prompt” design, that is a big deal. ([Deno][3])

The catch is your supply-chain rule. Deno’s “standard library” is published as modular `@std` packages on JSR, not baked into the runtime itself, so it is not a literal zero-external-deps story in the same way as Python or Nim stdlib. And `deno compile` bakes runtime permission flags into the produced binary, which matters for a coding agent that needs file, env, network, and subprocess access. ([Deno][4])

So I would phrase Deno like this: **best LOC reducer, but only if you are willing to redefine “zero deps” to include first-party Deno/Node surfaces.** ([Deno][4])

### Nim has the best literal stdlib story

Nim’s official docs back up your intuition here. The stdlib distinguishes pure libraries from impure ones, and the terminal pieces are quite solid: `std/terminal` gives cursor motion, erase, cursor show/hide, `getch()`, and `terminalSize()`, and on Unix it uses ANSI sequences directly rather than depending on another terminal framework. `std/rdstdin` wraps `linenoise` on Unix and gives default keybindings out of the box. ([Nim Programming Language][5])

So yes: **Nim has a better built-in terminal toolbox than Python stdlib**, and arguably a cleaner literal-stdlib story than Deno for this project. It also compiles to native executables and the Nim site explicitly markets native dependency-free executables. ([Nim Programming Language][5])

But I would temper the expectation on LOC savings. `rdstdin` is fundamentally a **read-a-line** abstraction with default bindings. That is great if you are happy to center the UX around a prompt. It is less obviously a huge win if you still want your own ghost completions, approval mode, inline streamed redraw behavior, and custom command UX. Nim will absolutely shrink some of the raw terminal glue, but it does **not** give you a magically richer terminal editor than Node/Ruby do. ([Nim Programming Language][6])

So for Nim my verdict is:

* strongest fit for your “zero transitive deps / single binary” principle
* good terminal primitives
* probably **less** editor-LOC reduction than Deno-with-readline or Ruby-with-Reline

### The missed candidate is Ruby

Ruby is the one I should have raised sooner.

Ruby’s standard library is large, and `Reline` is not a toy. The official docs show completion hooks, Emacs and Vi editing modes, screen-size access, normal `readline`, and importantly `readmultiline`. That is a much richer built-in line editor story than Python stdlib, and closer to what you actually need for this app. ([Ruby Documentation][7])

This matters because your friend’s Python `readline` summary is basically right: Python’s `readline` enhances `input()`, but `input()` still reads one line. The Python docs say `input()` reads a line and, if `readline` is loaded, uses it for editing/history. That is helpful, but it does not solve your multiline composition problem by itself. ([Python documentation][8])

Ruby `Reline`, by contrast, officially exposes `readmultiline`, plus completion and editing mode hooks. That makes Ruby the most credible “I want a bundled editor, not just raw TTY primitives” option I’ve seen in this search. ([Ruby Documentation][9])

The downside is distribution. Ruby does not give you the same neat native-binary story as Nim, and I would not pick it if “single standalone binary” is central.

### Have we found anything else truly comparable to Nim?

Not many, honestly.

The only other niche candidate I found worth naming is **Free Pascal**. Its runtime docs describe cross-platform keyboard/mouse/text-screen units, and the Free Pascal docs mention FreeVision as a text-mode application framework. That is real, and it is closer to old-school built-in TUI tooling than most modern languages offer. ([Free Pascal][10])

But I would not recommend it for this project unless you want to lean fully into retro text-mode tooling. For a modern HTTP/MCP/JSON/streaming agent, it feels like the wrong center of gravity. ([Free Pascal][10])

So the honest answer is: **there are not many modern languages with a truly rich bundled terminal editor/TUI story.** The shortlist is basically:

* **Nim** for native binary + solid terminal stdlib
* **Ruby** for richest bundled line editor
* **Deno** if Node-compat/readline counts as acceptable
* **Python** if you simplify instead of fighting stdlib limitations

### On your adoption assumption

One practical note: stock modern macOS is not the safe “Python is already there” target it used to be. Apple removed Python 2.7 from macOS 12.3, and GitHub Actions macOS runners do currently include multiple Python versions — but that is CI image policy, not the same as end-user macOS having Python preinstalled. ([Apple Developer][11])

So Python still has a real adoption advantage in **developer** environments and CI, but less of an advantage on **random user Macs** than your message suggests. ([GitHub][12])

### My actual recommendation now

For **your exact app**, I would rank the choices like this:

1. **Deno** if the true goal is “cut the editor code aggressively.”
   The presence of `node:readline` with Meta word motions/deletes and completer/history support makes it a much stronger fit than I initially thought. ([Deno][3])

2. **Nim** if the true goal is “ship a native binary with a hard zero-deps philosophy.”
   Best philosophical match, decent terminal stdlib, but probably not the absolute best LOC minimizer for the editor layer. ([Nim Programming Language][5])

3. **Ruby** if the true goal is “find the nicest bundled editor/runtime story.”
   `Reline` is the most interesting missed option here. ([Ruby Documentation][7])

4. **Python** only if you stop trying to make the prompt editor ambitious.
   Python stdlib does not appear to have another hidden terminal gem that changes this equation. `readline` helps, but it does not give you your current UX whole. ([Python documentation][8])

The concise version of my answer is: **you were right to keep looking past Python, and the two most important discoveries are “Nim is legit” and “Ruby/Reline is the main missed option,” but Deno may still be the best rewrite target if LOC is the top priority.**

If helpful, I can turn this into a very concrete matrix for your exact feature list: multiline, ghost completion, slash commands, y/n approvals, streaming redraw, MCP/OAuth, single-binary shipping, and expected LOC in each language.

[1]: https://docs.deno.com/api/deno/~/Deno.stdin.setRaw "Deno.stdin.setRaw - Deno documentation"
[2]: https://nodejs.org/api/readline.html "Readline | Node.js v25.9.0 Documentation"
[3]: https://docs.deno.com/api/node/all_symbols "All Symbols - Node documentation"
[4]: https://docs.deno.com/runtime/reference/std/?utm_source=chatgpt.com "Deno Standard Library (@std)"
[5]: https://nim-lang.org/docs/lib.html "Nim Standard Library"
[6]: https://nim-lang.org/docs/rdstdin.html "std/rdstdin"
[7]: https://docs.ruby-lang.org/en/master/standard_library_md.html "standard_library - Documentation for Ruby 4.1"
[8]: https://docs.python.org/3/library/readline.html?utm_source=chatgpt.com "GNU readline interface"
[9]: https://ruby-doc.org/3.2.3/stdlibs/reline/Reline/Core.html "class Reline::Core - reline: Ruby Standard Library Documentation"
[10]: https://www.freepascal.org/docs-html/rtl/index.html?utm_source=chatgpt.com "Reference for package 'rtl'"
[11]: https://developer.apple.com/documentation/macos-release-notes/macos-12_3-release-notes?utm_source=chatgpt.com "macOS Monterey 12.3 Release Notes"
[12]: https://github.com/actions/runner-images/blob/main/images/macos/macos-15-Readme.md "runner-images/images/macos/macos-15-Readme.md at main · actions/runner-images · GitHub"
