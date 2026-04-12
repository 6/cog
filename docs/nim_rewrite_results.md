# Nim rewrite: results

A faithful port of `cog.py` (1,450 LOC) to `cog.nim` (2,182 LOC). This
document records what the port actually bought us, where PLAN2.md was
wrong, and why we're keeping it anyway.

## What we shipped

| | Python | Nim |
|---|---|---|
| LOC | 1,450 | 2,182 |
| Binary | N/A (interpreter) | 1.3 MB native arm64 |
| Startup | ~60 ms | ~15 ms |
| Runtime deps | Python 3.9+ | libSystem + dlopen'd libssl |
| Build | — | `nim c -d:ssl cog.nim`, ~2 s |

All four planned phases landed and are verified end-to-end against
MiniMax (Anthropic-compatible API) and Context7 (MCP server):

- Streaming SSE from the LLM, tool dispatch, retry with backoff
- Full TUI: banner, character-by-char line editor, ghost completion,
  spinner, tool-call rendering, y/n approval, Ctrl+C and SIGTERM both
  restore the terminal cleanly
- MCP JSON-RPC over HTTP with SSE response reader, OAuth 2.1 with PKCE
  (interactive browser flow unverified — no accessible test server — but
  typechecks and the startup-time token-cache path works)
- Non-TTY fallback harness so pipes/scripts still work (`cog.py` crashes
  in that mode, so we're arguably more robust here)

## Honest reckoning: PLAN2.md vs reality

[PLAN2.md](./PLAN2.md), written before the port, floated two central
arguments for Nim over Python:

1. **"Best literal fit for native binary + zero external deps"** — true,
   and the main reason to keep it.
2. **"Good terminal primitives"** and the implication that Nim's
   `std/terminal` / `std/rdstdin` would help shrink the custom line
   editor — **false, in both directions**.

### LOC: +50%, not smaller

A port-to-Nim talk would usually promise parity or a small win. We got
a 50% *increase*. The concentrated sources:

- **~80 LOC inline SHA256.** Nim's stdlib has no SHA256. `std/checksums`
  (which exists in devel) only has md5/sha1, and isn't in 2.2.8 at all.
  `std/openssl` exists but would force a runtime OpenSSL dependency
  that breaks the "single binary" promise. We vendored a pure-Nim
  implementation from FIPS 180-4. Python gets SHA256 free via
  `hashlib`.
- **~60 LOC non-TTY fallback harness** that Python simply doesn't have
  (and arguably should).
- **~10 LOC flock(2) FFI** that Python gets via the `fcntl` module.
- **Per-exception-class try/except.** Nim's `except IOError | OSError`
  syntax exists but most of our tools need three or four classes each,
  so we end up with 3–4 line stanzas where Python uses one `except
  Exception as e:`.
- **Explicit type declarations** for records (`Config`, `ToolEntry`,
  `TuiState`, `AgentCtx`, `SseEvent`, `SseEventKind`, channel types).
  ~60 LOC of pure type boilerplate.
- **JsonNode access ceremony.** `ev{"foo", "bar"}.getStr("")` is ~2.5×
  the characters of `ev.get("foo", {}).get("bar", "")`. Spread over the
  SSE parser, event rendering, and MCP client, this adds up.
- **Manual `\r\n` bookkeeping** when writing to a raw-mode terminal —
  Python uses `sys.stdout.write` the same way but we needed an
  `outInline` helper that converts bare `\n` to `\r\n` for streamed
  output, because raw mode doesn't translate them.
- **`gcsafe` casts** around access to globals from threaded procs.
  ~15 LOC of compile-time type-system dance that Python skips entirely.
- **No f-strings.** Every format string is `&` concatenation or
  `strutils.%`, both wordier than `f"{a} {b}"`.

A second pass with aggressive refactoring (dedup the MCP tool-loading
between `mcpDiscoverAll` and the `add_mcp_tools` agent handler, a
`wrapErr` template for tool try/except stanzas, and `std/terminal`'s
`ansiStyleCode`/`styledEcho` instead of raw ANSI literals) would save
about **70 LOC**. That brings us to ~2,110. Still ~45% over Python.
The LOC tax is structural to the language choice, not a skill issue.

### Terminal stdlib: gave us almost nothing

This one is the bigger disappointment. The things we actually used from
`std/terminal`:

- `terminalWidth()` — one call, for input layout.
- That's it.

Everything else — raw-mode setup via `std/termios`, blocking `read(2)`
on a dedicated input thread, ANSI escape sequences written as literal
strings, escape-sequence parsing for arrow keys and Opt+Left/Opt+Right,
history with Up/Down, ghost completion on slash commands, multiline
input with cursor-tracks-across-newlines — **is a line-for-line
translation of the Python code**. Some procs even call the same POSIX
syscalls.

The PLAN2 suggestion to try `std/rdstdin` was investigated before the
port and ruled out: it wraps linenoise but doesn't expose multiline
input, Meta+B/F word movement, or in-place redraw. Good for REPLs, not
for this UX.

After the port, a fresh audit found one missed option:
`lib/wrappers/linenoise` — an unadvertised wrapper around antirez's
linenoise that compiles the bundled C file. It would replace ~400 LOC
of the editor *and* give you history, word-jump, and completion for
free. The showstopper: `linenoise.readLine(prompt)` is blocking and has
no event-loop integration, so we'd still need a separate thread, and
we'd lose in-place redraw (which we need for ghost completion, the
token-colored caret, and the streaming text layout). Net: not usable
for this specific UX, though a future simplified version of cog could
use it.

### What Nim *did* buy us

These matter and justify keeping the port:

- **Zero external packages.** Entire program builds from Nim's bundled
  stdlib. `otool -L cog` shows only `libSystem` and `Security.framework`
  plus the dlopen'd `libssl` path (baked in via `nim.cfg` rpath).
- **Native startup.** Subjectively instant compared to `python3 cog.py`.
- **Compile-time type checks.** Caught a handful of would-be runtime
  errors during the port. Not a huge deal in a small codebase, but real.
- **Good concurrency primitives.** `Channel[T]` is cleaner than Python's
  `queue.Queue` for the agent/TUI/input thread architecture.
- **Fast compile.** ~2 seconds for the whole program, 0.6 s for a
  `nim check`. Kept the edit-compile-test loop tight.

## Audit: is there more stdlib we could use?

We did a deep re-audit after the port, specifically looking for anything
we'd missed. Conclusions:

| Area | Checked | Verdict |
|---|---|---|
| `std/terminal` color helpers | `ansiStyleCode`, `styledEcho` | cosmetic ~20 LOC win, no structural change |
| `std/rdstdin` | rechecked | no multiline, no word-jump, no event-loop integration |
| `lib/wrappers/linenoise` | discovered post-port | great idea, killed by no event-loop integration |
| `std/asynchttpserver` for OAuth callback | checked | **net loss** — async ceremony costs more than our 25-line raw socket path |
| SHA256 anywhere in stdlib | exhaustively searched | not present in 2.2.8; `std/checksums` is devel-only |
| `std/posix` `fcntl(F_SETLK)` instead of `flock(2)` FFI | checked | more verbose than our 3-line FFI |
| `std/jsonutils` for JsonNode access | checked | targets typed round-trip, doesn't help loose access |
| `std/nativesockets` | checked, removed | `Port`/`getLocalAddr` already re-exported by `std/net` |

**Near-optimal.** The biggest remaining wins are internal refactors,
not stdlib-hunting.

## Should we keep it?

**Yes, if the goal is a native single-binary coding agent with a hard
"no package manager, no runtime deps, no supply-chain surface area"
philosophy.** Nim delivers on exactly that, and no other mainstream
language we evaluated matches that spec:

- **Deno + `node:readline`** would genuinely shrink the editor by
  200–400 LOC — Meta word-bindings, history, and `completer` support
  are built in. But Deno publishes its stdlib as external `@std/*`
  packages on JSR and `deno compile` bakes permission flags into the
  binary. It's not really "zero external deps" in the sense we care
  about.
- **Ruby + `Reline`** has `readmultiline` and is the most interesting
  option if we want a rich bundled editor. Loses the single-binary
  story.
- **Python + simpler UX** is the pragmatic alternative — but it means
  abandoning the custom line editor, which is where a lot of cog's
  differentiation lives.

**No, if the goal was smaller or simpler code.** The faithful port is
bigger and arguably no simpler. If LOC or editor-ergonomics were the
priority, Nim was the wrong pick.

## Artifacts

- Source: [`cog.nim`](../cog.nim)
- Build config: [`nim.cfg`](../nim.cfg), [`mise.toml`](../mise.toml)
- Original Python: [`cog.py`](../cog.py) (still runnable, still the
  authoritative reference for behavior)
- Planning doc: [`PLAN2.md`](./PLAN2.md)
