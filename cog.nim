## cog - minimal coding agent. Nim port of cog.py, single file, stdlib only.

import std/[
  json, os, osproc, strutils, streams, httpclient, math,
  times, tables, parseopt, algorithm, terminal, termios, posix,
  options, exitprocs, random
]

# ------------------------------------------------------------------------------
# Types
# ------------------------------------------------------------------------------

type
  Config* = object
    model*: string
    apiKeyEnv*: string
    apiBaseUrl*: string
    models*: JsonNode      # name -> {api_base_url, api_key_env, model}
    skillsDirs*: seq[string]
    mcpServers*: JsonNode  # array of server defs
    maxToolCallsPerTurn*: int
    shellTimeoutSeconds*: int
    toolOutputMaxBytes*: int
    logDir*: string
    autoApprove*: bool
    verbose*: bool
    tokenThresholdWarn*: int
    tokenThresholdDanger*: int
    apiKey*: string        # runtime
    systemPrompt*: string  # runtime

  ToolKind* = enum tkBuiltin, tkMcp
  ToolFn* = proc(args: JsonNode): string {.gcsafe.}
  ToolEntry* = object
    kind*: ToolKind
    schema*: JsonNode
    # builtin
    fn*: ToolFn
    # mcp
    server*: JsonNode
    realName*: string

  APIError* = object of CatchableError
    status*: int
    body*: string
    retryAfter*: float   # negative = not set
    retryable*: bool

proc defaultConfig(): Config =
  Config(
    model: "claude-sonnet-4-20250514",
    apiKeyEnv: "ANTHROPIC_API_KEY",
    apiBaseUrl: "https://api.anthropic.com",
    models: newJObject(),
    skillsDirs: @[],
    mcpServers: newJArray(),
    maxToolCallsPerTurn: 10,
    shellTimeoutSeconds: 30,
    toolOutputMaxBytes: 32768,
    logDir: "~/.cog/logs",
    autoApprove: false,
    verbose: false,
    tokenThresholdWarn: 100_000,
    tokenThresholdDanger: 200_000,
  )

const SystemPromptTemplate = """You are a coding agent working in the directory: $1

You have access to tools for reading, writing, and editing files, listing directories, and optionally running shell commands. Use them when helpful.

Guidelines:
- Take small, concrete steps. Read before writing.
- Use str_replace for targeted edits. Use write_file for new files or complete rewrites.
- Explain what you're doing briefly before each action.
- If a tool call fails, read the error and adjust.
- Do not invent file contents or tool outputs.
- Keep responses concise unless the user asks for detail.
"""

# ------------------------------------------------------------------------------
# Tools
# ------------------------------------------------------------------------------

var gCwd = "."
var gShellTimeout = 30

proc toolsConfigure(cwd: string, shellTimeout: int) =
  gCwd = absolutePath(cwd)
  gShellTimeout = shellTimeout

proc resolvePath(path: string): string {.gcsafe.} =
  ## Resolve `path` under gCwd and reject if it escapes the working dir.
  ## Canonicalizes `..` and `.` without requiring the path to exist.
  {.cast(gcsafe).}:
    var p = path
    if not isAbsolute(p):
      p = gCwd / p
    result = normalizedPath(p)
    if fileExists(result) or dirExists(result):
      try: result = expandFilename(result)
      except OSError: discard
    if not (result == gCwd or result.startsWith(gCwd & "/")):
      raise newException(ValueError, "path escapes working directory: " & path)

proc toolReadFile(args: JsonNode): string {.gcsafe.} =
  try:
    let path = resolvePath(args["path"].getStr())
    if not fileExists(path):
      return "ERROR: file not found: " & path
    # Binary detection: look for NUL in first 8KB
    let f = open(path, fmRead)
    defer: f.close()
    var sample = newString(8192)
    let n = f.readBuffer(addr sample[0], 8192)
    sample.setLen(n)
    if '\0' in sample:
      return "ERROR: " & path & " appears to be a binary file"
    f.setFilePos(0)
    result = f.readAll()
  except ValueError as e:
    return "ERROR: " & e.msg
  except IOError as e:
    return "ERROR: " & e.msg
  except OSError as e:
    return "ERROR: " & e.msg

proc toolListDir(args: JsonNode): string {.gcsafe.} =
  try:
    let raw = if args.hasKey("path"): args["path"].getStr(".") else: "."
    let path = resolvePath(raw)
    if not dirExists(path):
      return "ERROR: directory not found: " & path
    var entries: seq[string] = @[]
    for kind, name in walkDir(path, relative = true):
      let marker = if kind == pcDir or kind == pcLinkToDir: "[dir] " else: "[file]"
      entries.add(marker & " " & name)
    sort(entries)
    if entries.len == 0: return "(empty directory)"
    result = entries.join("\n")
  except ValueError as e:
    return "ERROR: " & e.msg
  except OSError as e:
    return "ERROR: " & e.msg

proc toolWriteFile(args: JsonNode): string {.gcsafe.} =
  try:
    let path = resolvePath(args["path"].getStr())
    let content = args["content"].getStr()
    let parent = parentDir(path)
    if parent.len > 0 and not dirExists(parent):
      createDir(parent)
    writeFile(path, content)
    result = "OK: wrote " & $content.len & " bytes to " & path
  except ValueError as e:
    return "ERROR: " & e.msg
  except IOError as e:
    return "ERROR: " & e.msg
  except OSError as e:
    return "ERROR: " & e.msg

proc toolStrReplace(args: JsonNode): string {.gcsafe.} =
  try:
    let path = resolvePath(args["path"].getStr())
    let oldStr = args["old_str"].getStr()
    let newStr = args["new_str"].getStr()
    if not fileExists(path):
      return "ERROR: file not found: " & path
    let content = readFile(path)
    let count = content.count(oldStr)
    if count == 0:
      return "ERROR: old_str not found in file"
    if count > 1:
      return "ERROR: old_str matched " & $count & " times, must be unique. " &
             "Add more surrounding context to make it unique."
    writeFile(path, content.replace(oldStr, newStr))
    result = "OK: replacement made"
  except ValueError as e:
    return "ERROR: " & e.msg
  except IOError as e:
    return "ERROR: " & e.msg
  except OSError as e:
    return "ERROR: " & e.msg

proc runShellWithTimeout(cmd: string, timeoutSecs: int): tuple[output: string, timedOut: bool] {.gcsafe.} =
  ## Run shell command in gCwd with a timeout. Captures stdout+stderr together.
  var cwdCopy: string
  {.cast(gcsafe).}:
    cwdCopy = gCwd
  let p = startProcess("/bin/sh", args = ["-c", cmd], workingDir = cwdCopy,
                       options = {poStdErrToStdOut, poUsePath})
  let deadline = epochTime() + timeoutSecs.float
  while p.running and epochTime() <= deadline:
    sleep(50)
  if p.running:
    p.terminate()
    discard p.waitForExit()
    let captured = p.outputStream.readAll()
    p.close()
    return (captured, true)
  let code = p.waitForExit()
  let captured = p.outputStream.readAll() & "\n[exit code: " & $code & "]"
  p.close()
  return (captured.strip(leading = false), false)

proc toolRunShell(args: JsonNode): string {.gcsafe.} =
  var timeoutSecs: int
  {.cast(gcsafe).}:
    timeoutSecs = gShellTimeout
  try:
    let cmd = args["command"].getStr()
    let (captured, timedOut) = runShellWithTimeout(cmd, timeoutSecs)
    if timedOut:
      return "ERROR: command timed out after " & $timeoutSecs & "s"
    return captured
  except OSError as e:
    return "ERROR: " & e.msg
  except IOError as e:
    return "ERROR: " & e.msg

proc schema(name, desc: string, props: JsonNode, required: seq[string]): JsonNode =
  result = %* {
    "name": name,
    "description": desc,
    "input_schema": {
      "type": "object",
      "properties": props,
      "required": required
    }
  }

proc builtinTools(): OrderedTable[string, ToolEntry] =
  result = initOrderedTable[string, ToolEntry]()
  result["read_file"] = ToolEntry(kind: tkBuiltin, fn: toolReadFile, schema: schema(
    "read_file", "Read the contents of a file at the given path.",
    %*{"path": {"type": "string", "description": "File path to read (relative to cwd or absolute)"}},
    @["path"]))
  result["list_dir"] = ToolEntry(kind: tkBuiltin, fn: toolListDir, schema: schema(
    "list_dir", "List directory contents with type indicators ([dir] or [file]).",
    %*{"path": {"type": "string", "description": "Directory path (default: current directory)"}},
    @[]))
  result["write_file"] = ToolEntry(kind: tkBuiltin, fn: toolWriteFile, schema: schema(
    "write_file", "Write content to a file, creating parent directories if needed.",
    %*{
      "path": {"type": "string", "description": "File path to write"},
      "content": {"type": "string", "description": "Content to write to the file"}
    },
    @["path", "content"]))
  result["str_replace"] = ToolEntry(kind: tkBuiltin, fn: toolStrReplace, schema: schema(
    "str_replace",
    "Replace an exact string match in a file. The old_str must appear exactly once. " &
    "Include enough surrounding context lines in old_str to make the match unique.",
    %*{
      "path": {"type": "string", "description": "File path to edit"},
      "old_str": {"type": "string", "description": "Exact string to find (must appear exactly once)"},
      "new_str": {"type": "string", "description": "String to replace it with"}
    },
    @["path", "old_str", "new_str"]))
  result["run_shell"] = ToolEntry(kind: tkBuiltin, fn: toolRunShell, schema: schema(
    "run_shell", "Run a shell command and return combined stdout and stderr with exit code.",
    %*{"command": {"type": "string", "description": "Shell command to execute"}},
    @["command"]))

# ------------------------------------------------------------------------------
# Anthropic API + SSE parser
# ------------------------------------------------------------------------------

proc newAPIError(status: int, body: string, retryAfter: float = -1.0): ref APIError =
  result = newException(APIError, "API error " & $status & ": " & body)
  result.status = status
  result.body = body
  result.retryAfter = retryAfter
  result.retryable = status in [429, 500, 502, 503, 529]

proc buildRequest(model, system: string, messages: JsonNode,
                  tools: OrderedTable[string, ToolEntry], maxTokens: int = 4096): JsonNode =
  result = %* {
    "model": model,
    "max_tokens": maxTokens,
    "system": system,
    "messages": messages,
    "stream": true
  }
  if tools.len > 0:
    var toolArr = newJArray()
    for _, entry in tools:
      toolArr.add(entry.schema)
    result["tools"] = toolArr

proc streamRequest(apiKey: string, requestBody: JsonNode,
                   apiBaseUrl: string = "https://api.anthropic.com"):
                   tuple[client: HttpClient, resp: Response] =
  let client = newHttpClient(timeout = 120_000)
  client.headers = newHttpHeaders({
    "Content-Type": "application/json",
    "X-API-Key": apiKey,
    "anthropic-version": "2023-06-01"
  })
  let url = apiBaseUrl.strip(chars = {'/'}) & "/v1/messages"
  let resp = client.request(url, httpMethod = HttpPost, body = $requestBody)
  if resp.code != Http200:
    let body = resp.body  # OK to drain on error path
    var ra = -1.0
    if resp.headers.hasKey("retry-after"):
      try: ra = parseFloat(resp.headers["retry-after"])
      except ValueError: discard
    client.close()
    raise newAPIError(resp.code.int, body, ra)
  return (client, resp)

type
  SseEventKind* = enum
    seTextDelta, seTextFinal, seToolUse, seBlockDone, seStop, seUsage, seDone
  SseEvent* = object
    kind*: SseEventKind
    text*: string
    payload*: JsonNode  # for seToolUse, seBlockDone, seUsage

iterator parseSseStream(resp: Response): SseEvent =
  var eventType = ""
  var blockType = ""
  var blockId = ""
  var blockName = ""
  var blockIndex = -1
  var jsonAccum = ""
  var textAccum = ""
  var usage = %*{"input_tokens": 0, "output_tokens": 0}
  let stream = resp.bodyStream
  while not stream.atEnd:
    let raw =
      try: stream.readLine()
      except IOError: break
    let line = raw.strip(leading = false, trailing = true, chars = {'\r', '\n'})
    if line.startsWith("event: "):
      eventType = line[7 .. ^1]
      continue
    if not line.startsWith("data: "):
      continue
    let data =
      try: parseJson(line[6 .. ^1])
      except JsonParsingError: continue

    case eventType
    of "message_start":
      let u = data{"message", "usage"}
      if u != nil:
        usage["input_tokens"] = %u{"input_tokens"}.getInt(0)
    of "content_block_start":
      let cb = data{"content_block"}
      if cb != nil:
        blockIndex = data{"index"}.getInt(blockIndex + 1)
        blockType = cb{"type"}.getStr("")
        textAccum = ""
        jsonAccum = ""
        if blockType == "tool_use":
          blockId = cb{"id"}.getStr("")
          blockName = cb{"name"}.getStr("")
    of "content_block_delta":
      let delta = data{"delta"}
      if delta != nil:
        let dtype = delta{"type"}.getStr("")
        case dtype
        of "text_delta":
          let t = delta{"text"}.getStr("")
          textAccum.add(t)
          yield SseEvent(kind: seTextDelta, text: t)
        of "input_json_delta":
          jsonAccum.add(delta{"partial_json"}.getStr(""))
        of "thinking_delta":
          textAccum.add(delta{"thinking"}.getStr(""))
        else: discard
    of "content_block_stop":
      if blockType == "tool_use":
        var toolInput: JsonNode
        try:
          toolInput = if jsonAccum.len > 0: parseJson(jsonAccum) else: newJObject()
        except JsonParsingError:
          toolInput = %*{"_raw": jsonAccum}
        let done = %*{"type": "tool_use", "id": blockId, "name": blockName,
                      "input": toolInput, "index": blockIndex}
        yield SseEvent(kind: seBlockDone, payload: done)
        yield SseEvent(kind: seToolUse, payload: %*{
          "id": blockId, "name": blockName, "input": toolInput})
      elif blockType == "text":
        let done = %*{"type": "text", "text": textAccum, "index": blockIndex}
        yield SseEvent(kind: seBlockDone, payload: done)
        yield SseEvent(kind: seTextFinal, text: textAccum)
      elif blockType == "thinking":
        yield SseEvent(kind: seBlockDone, payload: %*{
          "type": "thinking", "thinking": textAccum, "index": blockIndex})
      else:
        yield SseEvent(kind: seBlockDone, payload: %*{"type": blockType, "index": blockIndex})
      blockType = ""
      blockId = ""
      blockName = ""
    of "message_delta":
      let outT = data{"usage", "output_tokens"}.getInt(0)
      usage["output_tokens"] = %outT
      let stopReason = data{"delta", "stop_reason"}.getStr("end_turn")
      yield SseEvent(kind: seStop, text: stopReason)
    of "message_stop":
      yield SseEvent(kind: seUsage, payload: usage.copy())
    else: discard
    eventType = ""

# ------------------------------------------------------------------------------
# SHA256 (pure Nim, inlined from Spike C)
# ------------------------------------------------------------------------------

type Sha256Ctx = object
  state: array[8, uint32]
  buf: array[64, byte]
  bufLen: int
  totalLen: uint64

const Sha256K: array[64, uint32] = [
  0x428a2f98'u32, 0x71374491'u32, 0xb5c0fbcf'u32, 0xe9b5dba5'u32,
  0x3956c25b'u32, 0x59f111f1'u32, 0x923f82a4'u32, 0xab1c5ed5'u32,
  0xd807aa98'u32, 0x12835b01'u32, 0x243185be'u32, 0x550c7dc3'u32,
  0x72be5d74'u32, 0x80deb1fe'u32, 0x9bdc06a7'u32, 0xc19bf174'u32,
  0xe49b69c1'u32, 0xefbe4786'u32, 0x0fc19dc6'u32, 0x240ca1cc'u32,
  0x2de92c6f'u32, 0x4a7484aa'u32, 0x5cb0a9dc'u32, 0x76f988da'u32,
  0x983e5152'u32, 0xa831c66d'u32, 0xb00327c8'u32, 0xbf597fc7'u32,
  0xc6e00bf3'u32, 0xd5a79147'u32, 0x06ca6351'u32, 0x14292967'u32,
  0x27b70a85'u32, 0x2e1b2138'u32, 0x4d2c6dfc'u32, 0x53380d13'u32,
  0x650a7354'u32, 0x766a0abb'u32, 0x81c2c92e'u32, 0x92722c85'u32,
  0xa2bfe8a1'u32, 0xa81a664b'u32, 0xc24b8b70'u32, 0xc76c51a3'u32,
  0xd192e819'u32, 0xd6990624'u32, 0xf40e3585'u32, 0x106aa070'u32,
  0x19a4c116'u32, 0x1e376c08'u32, 0x2748774c'u32, 0x34b0bcb5'u32,
  0x391c0cb3'u32, 0x4ed8aa4a'u32, 0x5b9cca4f'u32, 0x682e6ff3'u32,
  0x748f82ee'u32, 0x78a5636f'u32, 0x84c87814'u32, 0x8cc70208'u32,
  0x90befffa'u32, 0xa4506ceb'u32, 0xbef9a3f7'u32, 0xc67178f2'u32
]

proc rotr32(x: uint32, n: int): uint32 {.inline.} =
  (x shr n) or (x shl (32 - n))

proc sha256Init(c: var Sha256Ctx) =
  c.state = [
    0x6a09e667'u32, 0xbb67ae85'u32, 0x3c6ef372'u32, 0xa54ff53a'u32,
    0x510e527f'u32, 0x9b05688c'u32, 0x1f83d9ab'u32, 0x5be0cd19'u32
  ]
  c.bufLen = 0
  c.totalLen = 0

proc sha256ProcessBlock(c: var Sha256Ctx) =
  var w: array[64, uint32]
  for i in 0 ..< 16:
    w[i] = (uint32(c.buf[i*4]) shl 24) or (uint32(c.buf[i*4+1]) shl 16) or
           (uint32(c.buf[i*4+2]) shl 8) or uint32(c.buf[i*4+3])
  for i in 16 ..< 64:
    let s0 = rotr32(w[i-15], 7) xor rotr32(w[i-15], 18) xor (w[i-15] shr 3)
    let s1 = rotr32(w[i-2], 17) xor rotr32(w[i-2], 19) xor (w[i-2] shr 10)
    w[i] = w[i-16] + s0 + w[i-7] + s1
  var a = c.state[0]; var b = c.state[1]; var cc = c.state[2]; var d = c.state[3]
  var e = c.state[4]; var f = c.state[5]; var g = c.state[6]; var h = c.state[7]
  for i in 0 ..< 64:
    let S1 = rotr32(e, 6) xor rotr32(e, 11) xor rotr32(e, 25)
    let ch = (e and f) xor ((not e) and g)
    let t1 = h + S1 + ch + Sha256K[i] + w[i]
    let S0 = rotr32(a, 2) xor rotr32(a, 13) xor rotr32(a, 22)
    let mj = (a and b) xor (a and cc) xor (b and cc)
    let t2 = S0 + mj
    h = g; g = f; f = e; e = d + t1
    d = cc; cc = b; b = a; a = t1 + t2
  c.state[0] += a; c.state[1] += b; c.state[2] += cc; c.state[3] += d
  c.state[4] += e; c.state[5] += f; c.state[6] += g; c.state[7] += h

proc sha256Update(c: var Sha256Ctx, data: openArray[byte]) =
  c.totalLen += uint64(data.len)
  var i = 0
  while i < data.len:
    let take = min(64 - c.bufLen, data.len - i)
    for k in 0 ..< take:
      c.buf[c.bufLen + k] = data[i + k]
    c.bufLen += take
    i += take
    if c.bufLen == 64:
      sha256ProcessBlock(c)
      c.bufLen = 0

proc sha256Final(c: var Sha256Ctx): array[32, byte] =
  let bitLen = c.totalLen * 8
  c.buf[c.bufLen] = 0x80
  inc c.bufLen
  if c.bufLen > 56:
    while c.bufLen < 64:
      c.buf[c.bufLen] = 0; inc c.bufLen
    sha256ProcessBlock(c)
    c.bufLen = 0
  while c.bufLen < 56:
    c.buf[c.bufLen] = 0; inc c.bufLen
  for i in 0 ..< 8:
    c.buf[56 + i] = byte((bitLen shr ((7 - i) * 8)) and 0xff)
  sha256ProcessBlock(c)
  for i in 0 ..< 8:
    result[i*4]   = byte((c.state[i] shr 24) and 0xff)
    result[i*4+1] = byte((c.state[i] shr 16) and 0xff)
    result[i*4+2] = byte((c.state[i] shr 8) and 0xff)
    result[i*4+3] = byte(c.state[i] and 0xff)

proc sha256Bytes(s: string): array[32, byte] =
  var c: Sha256Ctx
  sha256Init(c)
  if s.len > 0:
    sha256Update(c, cast[ptr UncheckedArray[byte]](unsafeAddr s[0]).toOpenArray(0, s.len - 1))
  return sha256Final(c)

# ------------------------------------------------------------------------------
# MCP client + OAuth 2.1
# ------------------------------------------------------------------------------

import std/[base64, uri, net, browsers]

# BSD flock(2) — not part of POSIX but available on macOS/Linux. Nim's
# std/posix doesn't wrap it, so we bind it ourselves.
proc flock(fd: cint, op: cint): cint {.importc, header: "<sys/file.h>".}
const
  LOCK_SH* = cint(1)
  LOCK_EX* = cint(2)
  LOCK_UN* = cint(8)

type
  MCPError* = object of CatchableError
  MCPAuthRequired* = object of MCPError
    serverName*: string

proc newMCPAuthRequired(name: string): ref MCPAuthRequired =
  result = newException(MCPAuthRequired,
    "MCP server '" & name & "' requires auth (run /mcp auth " & name & ")")
  result.serverName = name

proc base64UrlEncode(data: openArray[byte]): string =
  ## base64url without padding, per RFC 4648 §5.
  result = encode(data)
  result = result.replace('+', '-').replace('/', '_')
  while result.len > 0 and result[^1] == '=':
    result.setLen(result.len - 1)

proc randomUrlSafe(nBytes: int): string =
  var buf = newSeq[byte](nBytes)
  for i in 0 ..< nBytes:
    buf[i] = byte(rand(255))
  return base64UrlEncode(buf)

proc mcpHttpGet(url: string, extraHeaders: openArray[(string, string)] = []):
                tuple[status: int, body: string] =
  let client = newHttpClient(timeout = 10_000)
  defer: client.close()
  var hdrs = @[("MCP-Protocol-Version", "2025-03-26")]
  for h in extraHeaders: hdrs.add(h)
  client.headers = newHttpHeaders(hdrs)
  try:
    let resp = client.request(url, httpMethod = HttpGet)
    return (resp.code.int, resp.body)
  except CatchableError as e:
    return (0, e.msg)

proc mcpHttpPostForm(url: string, data: Table[string, string]):
                     tuple[status: int, body: string] =
  let client = newHttpClient(timeout = 10_000)
  defer: client.close()
  client.headers = newHttpHeaders({"Content-Type": "application/x-www-form-urlencoded"})
  var parts: seq[string] = @[]
  for k, v in data.pairs:
    parts.add(encodeUrl(k) & "=" & encodeUrl(v))
  let body = parts.join("&")
  try:
    let resp = client.request(url, httpMethod = HttpPost, body = body)
    return (resp.code.int, resp.body)
  except CatchableError as e:
    return (0, e.msg)

proc mcpHttpPostJson(url: string, data: JsonNode):
                     tuple[status: int, body: string] =
  let client = newHttpClient(timeout = 10_000)
  defer: client.close()
  client.headers = newHttpHeaders({"Content-Type": "application/json"})
  try:
    let resp = client.request(url, httpMethod = HttpPost, body = $data)
    return (resp.code.int, resp.body)
  except CatchableError as e:
    return (0, e.msg)

proc mcpTokenPath(name: string): string =
  let d = expandTilde("~/.cog/tokens")
  createDir(d)
  return d / (name & ".json")

proc mcpLoadToken(name: string): JsonNode =
  let path = mcpTokenPath(name)
  if not fileExists(path): return nil
  try:
    let f = open(path, fmRead)
    defer: f.close()
    discard flock(f.getFileHandle().cint, LOCK_SH)
    let raw = f.readAll()
    if raw.len == 0: return nil
    let data = parseJson(raw)
    if data.hasKey("access_token") and data["access_token"].getStr().len > 0:
      return data
  except CatchableError:
    return nil
  return nil

proc mcpSaveToken(name: string, data: JsonNode) =
  let path = mcpTokenPath(name)
  let f = open(path, fmWrite)
  defer: f.close()
  discard flock(f.getFileHandle().cint, LOCK_EX)
  f.write($data)
  f.flushFile()

proc mcpParseWwwAuth(header: string): string =
  for part in header.split(','):
    let p = part.strip()
    if "resource_metadata=" in p:
      var v = p.split("resource_metadata=", maxsplit = 1)[1].strip()
      v = v.strip(chars = {'"'})
      return v
  return ""

proc mcpAuthBase(serverUrl: string): string =
  let p = parseUri(serverUrl)
  result = p.scheme & "://" & p.hostname
  if p.port.len > 0: result.add(":" & p.port)

proc mcpDiscoverAuthServer(serverUrl, wwwAuth: string):
                           tuple[meta: JsonNode, resource: string] =
  let base = mcpAuthBase(serverUrl)
  var resource = serverUrl
  var resMetaUrl = if wwwAuth.len > 0: mcpParseWwwAuth(wwwAuth) else: ""
  if resMetaUrl.len == 0:
    resMetaUrl = base & "/.well-known/oauth-protected-resource"
  var (status, body) = mcpHttpGet(resMetaUrl)
  if status == 200:
    try:
      let resMeta = parseJson(body)
      resource = resMeta{"resource"}.getStr(resource)
      if resMeta.hasKey("authorization_servers") and
         resMeta["authorization_servers"].kind == JArray and
         resMeta["authorization_servers"].len > 0:
        let asUrl = resMeta["authorization_servers"][0].getStr()
        let (s2, b2) = mcpHttpGet(asUrl & "/.well-known/oauth-authorization-server")
        if s2 == 200:
          return (parseJson(b2), resource)
    except CatchableError: discard
  # Fallback: AS metadata directly on base URL
  (status, body) = mcpHttpGet(base & "/.well-known/oauth-authorization-server")
  if status == 200:
    try: return (parseJson(body), resource)
    except CatchableError: discard
  # Last-resort defaults
  return (%*{
    "authorization_endpoint": base & "/authorize",
    "token_endpoint": base & "/token",
    "registration_endpoint": base & "/register"
  }, resource)

proc findFreePort(): int =
  let s = newSocket()
  try:
    s.bindAddr(Port(0), "127.0.0.1")
    let (_, port) = s.getLocalAddr()
    return port.int
  finally:
    s.close()

proc awaitOAuthCallback(port: int, timeoutSecs: int = 120): string =
  ## Minimal one-shot HTTP listener: bind, listen, accept one request, parse
  ## `code` query param, send an HTML response, close.
  let srv = newSocket()
  srv.setSockOpt(OptReuseAddr, true)
  srv.bindAddr(Port(port), "127.0.0.1")
  srv.listen(1)
  # Set a timeout on accept via select-equivalent: the stdlib Socket doesn't
  # expose SO_RCVTIMEO directly on accept, so we rely on the caller's timeout
  # window and just block on one connection.
  var client: Socket
  try:
    srv.accept(client)
  finally:
    srv.close()
  defer: client.close()
  var request = ""
  var line = ""
  while true:
    client.readLine(line, timeout = timeoutSecs * 1000)
    if line.len == 0: break
    request.add(line & "\r\n")
    if line == "": break
  # First line is e.g. "GET /callback?code=XYZ HTTP/1.1"
  let firstLine = request.split('\r', maxsplit = 1)[0]
  var code = ""
  let parts = firstLine.split(' ')
  if parts.len >= 2 and '?' in parts[1]:
    let query = parts[1].split('?', maxsplit = 1)[1]
    for kv in query.split('&'):
      let eq = kv.find('=')
      if eq > 0 and kv[0 ..< eq] == "code":
        code = decodeUrl(kv[eq + 1 .. ^1])
        break
  let html = "<html><body><h2>Authorization complete.</h2>" &
             "<p>You can close this tab.</p></body></html>"
  let response = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n" &
                 "Content-Length: " & $html.len & "\r\nConnection: close\r\n\r\n" & html
  client.send(response)
  return code

proc mcpOAuthFlow(server: JsonNode): string =
  let name = server{"name"}.getStr("mcp")
  let serverUrl = server["url"].getStr()

  # Cache hit path
  let cached = mcpLoadToken(name)
  if cached != nil and cached{"access_token"}.getStr().len > 0:
    return cached["access_token"].getStr()

  let wwwAuth = server{"_www_authenticate"}.getStr("")
  let (meta, resource) = mcpDiscoverAuthServer(serverUrl, wwwAuth)
  let authEp = meta{"authorization_endpoint"}.getStr()
  let tokenEp = meta{"token_endpoint"}.getStr()
  let regEp = meta{"registration_endpoint"}.getStr()

  let port = findFreePort()
  let redirectUri = "http://localhost:" & $port & "/callback"

  if regEp.len == 0:
    raise newException(MCPError, "No registration endpoint and no client_id configured")

  let regBody = %*{
    "client_name": "cog",
    "redirect_uris": [redirectUri],
    "grant_types": ["authorization_code"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none"
  }
  let (regStatus, regRespBody) = mcpHttpPostJson(regEp, regBody)
  if regStatus notin [200, 201]:
    raise newException(MCPError,
      "Dynamic client registration failed (" & $regStatus & "): " & regRespBody)
  let reg = parseJson(regRespBody)
  let clientId = reg["client_id"].getStr()

  # PKCE
  let verifier = randomUrlSafe(32)
  let challenge = base64UrlEncode(sha256Bytes(verifier))

  var params = initTable[string, string]()
  params["response_type"] = "code"
  params["client_id"] = clientId
  params["redirect_uri"] = redirectUri
  params["code_challenge"] = challenge
  params["code_challenge_method"] = "S256"
  params["resource"] = resource
  var query: seq[string] = @[]
  for k, v in params.pairs:
    query.add(encodeUrl(k) & "=" & encodeUrl(v))
  let authUrl = authEp & "?" & query.join("&")

  stderr.writeLine("Opening browser for " & name & " authorization...")
  try: openDefaultBrowser(authUrl)
  except CatchableError: discard

  let code = awaitOAuthCallback(port)
  if code.len == 0:
    raise newException(MCPError, "OAuth callback did not receive authorization code")

  var tokenForm = initTable[string, string]()
  tokenForm["grant_type"] = "authorization_code"
  tokenForm["code"] = code
  tokenForm["redirect_uri"] = redirectUri
  tokenForm["code_verifier"] = verifier
  tokenForm["client_id"] = clientId
  tokenForm["resource"] = resource
  let (tStatus, tBody) = mcpHttpPostForm(tokenEp, tokenForm)
  if tStatus != 200:
    raise newException(MCPError, "Token exchange failed (" & $tStatus & "): " & tBody)
  let tokenData = parseJson(tBody)
  tokenData["client_id"] = %clientId
  tokenData["resource"] = %resource
  mcpSaveToken(name, tokenData)
  return tokenData["access_token"].getStr()

proc mcpTryRefresh(server: JsonNode): string =
  let name = server{"name"}.getStr("mcp")
  let cached = mcpLoadToken(name)
  if cached == nil or cached{"refresh_token"}.getStr().len == 0:
    return ""
  let wwwAuth = server{"_www_authenticate"}.getStr("")
  let (meta, resource) = mcpDiscoverAuthServer(server["url"].getStr(), wwwAuth)
  let tokenEp = meta{"token_endpoint"}.getStr()
  if tokenEp.len == 0: return ""
  let useResource = cached{"resource"}.getStr(resource)
  var form = initTable[string, string]()
  form["grant_type"] = "refresh_token"
  form["refresh_token"] = cached["refresh_token"].getStr()
  form["client_id"] = cached{"client_id"}.getStr("")
  form["resource"] = useResource
  let (status, body) = mcpHttpPostForm(tokenEp, form)
  if status != 200: return ""
  try:
    let tokenData = parseJson(body)
    if not tokenData.hasKey("client_id"):
      tokenData["client_id"] = cached{"client_id"}
    if not tokenData.hasKey("resource"):
      tokenData["resource"] = %useResource
    if tokenData{"refresh_token"}.getStr().len == 0:
      tokenData["refresh_token"] = cached["refresh_token"]
    mcpSaveToken(name, tokenData)
    return tokenData["access_token"].getStr()
  except CatchableError:
    return ""

proc mcpReadSseStream(resp: Response, reqId: int): JsonNode =
  ## Read SSE line-by-line from an HTTP response, return the first data
  ## event matching our request id. Must consume the stream incrementally,
  ## since MCP servers may keep the connection open indefinitely.
  let stream = resp.bodyStream
  while not stream.atEnd:
    let raw =
      try: stream.readLine()
      except IOError: break
    let line = raw.strip(chars = {'\r', '\n'})
    if line.startsWith("data: "):
      try:
        let data = parseJson(line[6 .. ^1])
        if data.kind == JObject and data{"id"}.getInt(-1) == reqId:
          return data
      except JsonParsingError: continue
  return nil

proc mcpPost(server: JsonNode, meth: string, params: JsonNode = nil,
             isNotification: bool = false): JsonNode =
  var body = %*{"jsonrpc": "2.0", "method": meth}
  if params != nil:
    body["params"] = params
  var reqId = 0
  if not isNotification:
    let nextId = server{"_next_id"}.getInt(0) + 1
    server["_next_id"] = %nextId
    reqId = nextId
    body["id"] = %nextId

  var headers = @[
    ("Content-Type", "application/json"),
    ("Accept", "application/json, text/event-stream")
  ]
  let sessionId = server{"_session_id"}.getStr("")
  if sessionId.len > 0:
    headers.add(("Mcp-Session-Id", sessionId))
  let oauthToken = server{"_oauth_token"}.getStr("")
  if oauthToken.len > 0:
    headers.add(("Authorization", "Bearer " & oauthToken))
  if server.hasKey("headers") and server["headers"].kind == JObject:
    for k, v in server["headers"].pairs:
      headers.add((k, v.getStr()))

  let client = newHttpClient(timeout = 30_000)
  defer: client.close()
  client.headers = newHttpHeaders(headers)
  let resp = client.request(server["url"].getStr(), httpMethod = HttpPost, body = $body)

  if resp.code == Http401:
    let wwwAuth = if resp.headers.hasKey("www-authenticate"):
                    $resp.headers["www-authenticate"]
                  else: ""
    server["_www_authenticate"] = %wwwAuth
    if not server{"_refresh_attempted"}.getBool():
      server["_refresh_attempted"] = %true
      let newToken = mcpTryRefresh(server)
      if newToken.len > 0:
        server["_oauth_token"] = %newToken
        server["_refresh_attempted"] = %false
        return mcpPost(server, meth, params, isNotification)
    if server{"_oauth_interactive"}.getBool():
      server["_oauth_interactive"] = %false
      server["_refresh_attempted"] = %false
      let token = mcpOAuthFlow(server)
      server["_oauth_token"] = %token
      return mcpPost(server, meth, params, isNotification)
    raise newMCPAuthRequired(server{"name"}.getStr("mcp"))

  if isNotification:
    return nil

  let ct = if resp.headers.hasKey("content-type"): $resp.headers["content-type"] else: ""
  if resp.headers.hasKey("mcp-session-id"):
    server["_session_id"] = %($resp.headers["mcp-session-id"])
  var parsed: JsonNode
  if "text/event-stream" in ct:
    parsed = mcpReadSseStream(resp, reqId)
  else:
    try: parsed = parseJson(resp.body)
    except JsonParsingError: parsed = nil
  return parsed

proc mcpInitialize(cfg: JsonNode): JsonNode =
  ## cfg is one entry from config's mcp_servers array.
  let server = copy(cfg)
  server["_next_id"] = %0
  server["_session_id"] = %""
  server["_oauth_token"] = %""
  server["_refresh_attempted"] = %false
  let name = cfg{"name"}.getStr("mcp")
  let cached = mcpLoadToken(name)
  if cached != nil and cached{"access_token"}.getStr().len > 0:
    server["_oauth_token"] = cached["access_token"]
  let initResp = mcpPost(server, "initialize", %*{
    "protocolVersion": "2025-03-26",
    "capabilities": {"tools": {}},
    "clientInfo": {"name": "cog", "version": "0.1.0"}
  })
  if initResp != nil and initResp.hasKey("error"):
    raise newException(MCPError, "initialize failed: " & $initResp["error"])
  discard mcpPost(server, "notifications/initialized", nil, isNotification = true)
  return server

proc mcpCallTool(server: JsonNode, toolName: string, arguments: JsonNode): string {.gcsafe.} =
  {.cast(gcsafe).}:
    let resp =
      try: mcpPost(server, "tools/call", %*{"name": toolName, "arguments": arguments})
      except MCPError as e: return "ERROR: " & e.msg
      except CatchableError as e: return "ERROR: MCP call failed: " & e.msg
    if resp == nil: return "ERROR: no response from MCP server"
    if resp.hasKey("error"):
      let err = resp["error"]
      if err.kind == JObject and err.hasKey("message"):
        return "ERROR: " & err["message"].getStr()
      return "ERROR: " & $err
    var parts: seq[string] = @[]
    let content = resp{"result", "content"}
    if content != nil and content.kind == JArray:
      for item in content:
        if item{"type"}.getStr() == "text":
          parts.add(item{"text"}.getStr())
        else:
          parts.add($item)
    if parts.len == 0: return "(empty result)"
    return parts.join("\n")

proc mcpDiscoverAll(mcpConfigs: JsonNode):
                    tuple[tools: OrderedTable[string, ToolEntry],
                          pendingAuth: seq[string]] =
  result.tools = initOrderedTable[string, ToolEntry]()
  result.pendingAuth = @[]
  if mcpConfigs.isNil or mcpConfigs.kind != JArray or mcpConfigs.len == 0:
    return
  let multi = mcpConfigs.len > 1
  for cfg in mcpConfigs:
    let name = cfg{"name"}.getStr("mcp")
    try:
      let server = mcpInitialize(cfg)
      server["name"] = %name
      let listResp = mcpPost(server, "tools/list", %*{})
      if listResp == nil or listResp.hasKey("error"):
        continue
      let mcpTools = listResp{"result", "tools"}
      if mcpTools == nil or mcpTools.kind != JArray: continue
      for t in mcpTools:
        let realName = t{"name"}.getStr()
        let tname = if multi: name & "__" & realName else: realName
        let toolSchema = %*{
          "name": tname,
          "description": t{"description"}.getStr(""),
          "input_schema": t{"inputSchema"}
        }
        if toolSchema["input_schema"].isNil:
          toolSchema["input_schema"] = %*{"type": "object", "properties": {}}
        result.tools[tname] = ToolEntry(
          kind: tkMcp, server: server, schema: toolSchema, realName: realName)
    except MCPAuthRequired:
      result.pendingAuth.add(name)
    except CatchableError as e:
      stderr.writeLine("Warning: MCP server '" & name & "' failed: " & e.msg)

# ------------------------------------------------------------------------------
# Event queue and agent loop
# ------------------------------------------------------------------------------

type
  EventChan* = Channel[string]  # JSON-encoded events
  InputChan* = Channel[string]  # JSON-encoded messages to agent

proc emit(eq: ptr EventChan, ev: JsonNode, logFile: File = nil) =
  ev["ts"] = %($now().utc())
  if logFile != nil and ev{"type"}.getStr() != "assistant_text_delta":
    try:
      logFile.writeLine($ev)
      logFile.flushFile()
    except IOError:
      discard  # logging must not kill the agent
  eq[].send($ev)

type
  AgentCtx* = object
    model*: string
    apiKey*: string
    apiBaseUrl*: string
    system*: string
    tools*: OrderedTable[string, ToolEntry]
    messages*: JsonNode
    maxCalls*: int
    maxOutput*: int
    autoApprove*: bool
    verbose*: bool
    logFile*: File
    eq*: ptr EventChan
    iq*: ptr InputChan

proc waitForApproval(ctx: var AgentCtx, name: string, input: JsonNode): bool =
  emit(ctx.eq, %*{"type": "approval_request", "name": name, "input": input}, ctx.logFile)
  let raw = ctx.iq[].recv()
  let msg =
    try: parseJson(raw)
    except JsonParsingError: return false
  return msg{"type"}.getStr() == "approval" and msg{"approved"}.getBool()

proc dispatchTool(ctx: var AgentCtx, name: string, input: JsonNode):
                  tuple[output: string, isError: bool] =
  if name notin ctx.tools:
    return ("ERROR: unknown tool '" & name & "'", true)
  let entry = ctx.tools[name]
  if entry.kind == tkBuiltin:
    if name in ["write_file", "str_replace", "run_shell"] and not ctx.autoApprove:
      if not waitForApproval(ctx, name, input):
        return ("Tool call denied by user.", true)
    try:
      let r = entry.fn(input)
      return (r, r.startsWith("ERROR:"))
    except CatchableError as e:
      return ("ERROR: " & e.msg, true)
  else:
    if not ctx.autoApprove:
      if not waitForApproval(ctx, name, input):
        return ("Tool call denied by user.", true)
    let r = mcpCallTool(entry.server, entry.realName, input)
    return (r, r.startsWith("ERROR:"))

proc truncateOutput(s: string, maxBytes: int): string =
  if s.len <= maxBytes: return s
  let half = maxBytes div 2
  return s[0 ..< half] & "\n\n[... truncated ...]\n\n" & s[s.len - half .. ^1]

proc runTurn(ctx: var AgentCtx, userInput: string) =
  ctx.messages.add(%*{"role": "user", "content": userInput})
  emit(ctx.eq, %*{"type": "user_message", "content": userInput}, ctx.logFile)
  var toolCount = 0
  while true:
    let req = buildRequest(ctx.model, ctx.system, ctx.messages, ctx.tools)
    if ctx.verbose:
      emit(ctx.eq, %*{"type": "verbose", "data": req.pretty(2)}, ctx.logFile)
    var client: HttpClient = nil
    var resp: Response = nil
    var gotResp = false
    for attempt in 0 ..< 5:
      try:
        let (c, r) = streamRequest(ctx.apiKey, req, ctx.apiBaseUrl)
        client = c
        resp = r
        gotResp = true
        break
      except APIError as e:
        if e.retryable and attempt < 4:
          let delay =
            if e.retryAfter >= 0: e.retryAfter
            else: min(pow(2.0, attempt.float), 30.0)
          emit(ctx.eq, %*{"type": "status",
            "message": "API error " & $e.status & ", retrying in " &
                       formatFloat(delay, ffDecimal, 0) & "s..."}, ctx.logFile)
          sleep(int(delay * 1000))
          continue
        emit(ctx.eq, %*{"type": "error", "message": e.msg}, ctx.logFile)
        return
      except CatchableError as e:
        if attempt < 4:
          let delay = min(pow(2.0, attempt.float), 30.0)
          emit(ctx.eq, %*{"type": "status",
            "message": "Network error, retrying in " &
                       formatFloat(delay, ffDecimal, 0) & "s..."}, ctx.logFile)
          sleep(int(delay * 1000))
          continue
        emit(ctx.eq, %*{"type": "error", "message": "Network error: " & e.msg}, ctx.logFile)
        return
    if not gotResp:
      return

    var blocks = newJArray()
    var toolUses = newJArray()
    var usage = %*{"input_tokens": 0, "output_tokens": 0}
    try:
      for ev in parseSseStream(resp):
        case ev.kind
        of seTextDelta:
          emit(ctx.eq, %*{"type": "assistant_text_delta", "text": ev.text}, ctx.logFile)
        of seTextFinal:
          emit(ctx.eq, %*{"type": "assistant_text_final", "text": ev.text}, ctx.logFile)
        of seToolUse:
          toolUses.add(ev.payload)
          emit(ctx.eq, %*{"type": "tool_call",
            "tool_id": ev.payload{"id"}.getStr(),
            "name": ev.payload{"name"}.getStr(),
            "input": ev.payload{"input"}}, ctx.logFile)
        of seBlockDone:
          var blk = copy(ev.payload)
          blk.delete("index")
          if blk{"type"}.getStr() == "text":
            blk["text"] = %blk{"text"}.getStr().strip()
          let isEmptyText = blk{"type"}.getStr() == "text" and
                            blk{"text"}.getStr().len == 0
          if not isEmptyText:
            blocks.add(blk)
        of seUsage:
          usage = ev.payload
        of seStop, seDone: discard
    except CatchableError as e:
      emit(ctx.eq, %*{"type": "error", "message": "Stream error: " & e.msg}, ctx.logFile)
      if client != nil: client.close()
      return
    if client != nil: client.close()

    if blocks.len > 0:
      ctx.messages.add(%*{"role": "assistant", "content": blocks})
    if ctx.verbose:
      emit(ctx.eq, %*{"type": "verbose", "data": (%*{
        "role": "assistant", "content": blocks, "usage": usage}).pretty(2)}, ctx.logFile)
    if toolUses.len == 0:
      emit(ctx.eq, %*{"type": "turn_complete", "usage": usage}, ctx.logFile)
      return

    var toolResults = newJArray()
    for tu in toolUses:
      inc toolCount
      if toolCount > ctx.maxCalls:
        let content = "ERROR: maximum tool calls per turn exceeded"
        toolResults.add(%*{"type": "tool_result", "tool_use_id": tu["id"].getStr(),
                           "content": content, "is_error": true})
        emit(ctx.eq, %*{"type": "tool_result", "tool_id": tu["id"].getStr(),
                        "output": content, "is_error": true}, ctx.logFile)
        continue
      let (rawOut, isErr) = dispatchTool(ctx, tu["name"].getStr(), tu["input"])
      let output = truncateOutput(rawOut, ctx.maxOutput)
      toolResults.add(%*{"type": "tool_result", "tool_use_id": tu["id"].getStr(),
                         "content": output, "is_error": isErr})
      if ctx.verbose:
        emit(ctx.eq, %*{"type": "verbose", "data": output}, ctx.logFile)
      emit(ctx.eq, %*{"type": "tool_result", "tool_id": tu["id"].getStr(),
                      "output": output, "is_error": isErr}, ctx.logFile)
    ctx.messages.add(%*{"role": "user", "content": toolResults})
    if toolCount > ctx.maxCalls:
      emit(ctx.eq, %*{"type": "turn_complete", "usage": usage}, ctx.logFile)
      return

# Thread-shared agent state (written once before createThread, then read-only).
var gEqGlobal: ptr EventChan
var gIqGlobal: ptr InputChan
var gAgentConfig: Config
var gLogPath: string
var gToolRegGlobal: OrderedTable[string, ToolEntry]

proc agentWorker() {.thread.} =
  var ctx: AgentCtx
  {.cast(gcsafe).}:
    ctx = AgentCtx(
      model: gAgentConfig.model,
      apiKey: gAgentConfig.apiKey,
      apiBaseUrl: gAgentConfig.apiBaseUrl,
      system: gAgentConfig.systemPrompt,
      tools: gToolRegGlobal,
      messages: newJArray(),
      maxCalls: gAgentConfig.maxToolCallsPerTurn,
      maxOutput: gAgentConfig.toolOutputMaxBytes,
      autoApprove: gAgentConfig.autoApprove,
      verbose: gAgentConfig.verbose,
      eq: gEqGlobal,
      iq: gIqGlobal,
    )
    try:
      ctx.logFile = open(gLogPath, fmAppend)
    except IOError:
      ctx.logFile = nil
  while true:
    let raw = ctx.iq[].recv()
    if raw == "__SHUTDOWN__": break
    # Control messages are JSON objects; user input is wrapped.
    let msg =
      try: parseJson(raw)
      except JsonParsingError:
        # malformed — ignore
        continue
    let mtype = msg{"type"}.getStr("")
    case mtype
    of "user_input":
      try:
        runTurn(ctx, msg{"text"}.getStr())
      except CatchableError as e:
        emit(ctx.eq, %*{"type": "error", "message": "Agent error: " & e.msg}, ctx.logFile)
    of "switch_model":
      ctx.model = msg{"model"}.getStr(ctx.model)
      ctx.apiKey = msg{"api_key"}.getStr(ctx.apiKey)
      ctx.apiBaseUrl = msg{"api_base_url"}.getStr(ctx.apiBaseUrl)
    of "add_mcp_tools":
      let name = msg{"name"}.getStr()
      var serverCfg: JsonNode = nil
      {.cast(gcsafe).}:
        if gAgentConfig.mcpServers.kind == JArray:
          for s in gAgentConfig.mcpServers:
            if s{"name"}.getStr() == name:
              serverCfg = s; break
      if serverCfg == nil:
        emit(ctx.eq, %*{"type": "error", "message": "unknown MCP server: " & name},
             ctx.logFile)
        continue
      try:
        let server = mcpInitialize(serverCfg)
        server["name"] = %name
        let listResp = mcpPost(server, "tools/list", %*{})
        if listResp == nil or listResp.hasKey("error"):
          emit(ctx.eq, %*{"type": "error",
            "message": "tools/list failed for " & name}, ctx.logFile)
          continue
        let mcpTools = listResp{"result", "tools"}
        var count = 0
        if mcpTools != nil and mcpTools.kind == JArray:
          let multi = (ctx.tools.len > 0) and
                      ({.cast(gcsafe).}: gAgentConfig.mcpServers.len > 1)
          for t in mcpTools:
            let realName = t{"name"}.getStr()
            let tname = if multi: name & "__" & realName else: realName
            let toolSchema = %*{
              "name": tname,
              "description": t{"description"}.getStr(""),
              "input_schema": t{"inputSchema"}
            }
            if toolSchema["input_schema"].isNil:
              toolSchema["input_schema"] = %*{"type": "object", "properties": {}}
            ctx.tools[tname] = ToolEntry(
              kind: tkMcp, server: server, schema: toolSchema, realName: realName)
            inc count
        emit(ctx.eq, %*{"type": "status",
          "message": $count & " tools loaded from " & name}, ctx.logFile)
      except CatchableError as e:
        emit(ctx.eq, %*{"type": "error",
          "message": "MCP auth failed: " & e.msg}, ctx.logFile)
    of "revoke_mcp_tools":
      let name = msg{"name"}.getStr()
      var removed: seq[string] = @[]
      for k, entry in ctx.tools.pairs:
        if entry.kind == tkMcp and entry.server{"name"}.getStr() == name:
          removed.add(k)
      for k in removed:
        ctx.tools.del(k)
      emit(ctx.eq, %*{"type": "status",
        "message": $removed.len & " tools removed from " & name}, ctx.logFile)
    of "approval":
      # Approval replies are consumed by waitForApproval; if one arrives
      # outside that context, drop it.
      discard
    else: discard
  if ctx.logFile != nil:
    try: ctx.logFile.close() except IOError: discard

# ------------------------------------------------------------------------------
# Config loading + skills
# ------------------------------------------------------------------------------

proc expandEnvString(s: string): string =
  var i = 0
  while i < s.len:
    if i + 1 < s.len and s[i] == '$' and s[i+1] == '{':
      let close = s.find('}', i + 2)
      if close == -1:
        result.add(s[i]); inc i
      else:
        let key = s[i+2 ..< close]
        result.add(getEnv(key, ""))
        i = close + 1
    else:
      result.add(s[i]); inc i

proc expandEnvJson(v: JsonNode): JsonNode =
  case v.kind
  of JString: result = %expandEnvString(v.getStr())
  of JObject:
    result = newJObject()
    for k, val in v.pairs:
      result[k] = expandEnvJson(val)
  of JArray:
    result = newJArray()
    for item in v.items:
      result.add(expandEnvJson(item))
  else: result = v

proc findLocalConfig(cwd: string): string =
  var d = absolutePath(cwd)
  while true:
    let candidate = d / ".cog" / "config.json"
    if fileExists(candidate): return candidate
    let parent = parentDir(d)
    if parent == d or parent.len == 0: return ""
    d = parent

proc applyConfigJson(cfg: var Config, raw: JsonNode) =
  template assignStr(field, key) =
    if raw.hasKey(key): cfg.field = raw[key].getStr(cfg.field)
  template assignInt(field, key) =
    if raw.hasKey(key): cfg.field = raw[key].getInt(cfg.field)
  template assignBool(field, key) =
    if raw.hasKey(key): cfg.field = raw[key].getBool(cfg.field)
  assignStr(model, "model")
  assignStr(apiKeyEnv, "api_key_env")
  assignStr(apiBaseUrl, "api_base_url")
  if raw.hasKey("models") and raw["models"].kind == JObject: cfg.models = raw["models"]
  if raw.hasKey("skills_dirs") and raw["skills_dirs"].kind == JArray:
    cfg.skillsDirs = @[]
    for item in raw["skills_dirs"]: cfg.skillsDirs.add(item.getStr())
  if raw.hasKey("mcp_servers") and raw["mcp_servers"].kind == JArray:
    cfg.mcpServers = raw["mcp_servers"]
  assignInt(maxToolCallsPerTurn, "max_tool_calls_per_turn")
  assignInt(shellTimeoutSeconds, "shell_timeout_seconds")
  assignInt(toolOutputMaxBytes, "tool_output_max_bytes")
  assignStr(logDir, "log_dir")
  assignBool(autoApprove, "auto_approve")
  assignBool(verbose, "verbose")
  assignInt(tokenThresholdWarn, "token_threshold_warn")
  assignInt(tokenThresholdDanger, "token_threshold_danger")

proc resolveModel(cfg: var Config) =
  if cfg.models.kind == JObject and cfg.models.hasKey(cfg.model):
    let m = cfg.models[cfg.model]
    cfg.apiBaseUrl = m{"api_base_url"}.getStr(cfg.apiBaseUrl)
    cfg.apiKeyEnv = m{"api_key_env"}.getStr(cfg.apiKeyEnv)
    cfg.model = m{"model"}.getStr(cfg.model)
  cfg.apiKey = getEnv(cfg.apiKeyEnv, "")

proc loadConfig(path: string, cwd: string = "."): Config =
  result = defaultConfig()
  let expanded = expandTilde(path)
  if fileExists(expanded):
    try:
      let raw = expandEnvJson(parseJson(readFile(expanded)))
      if raw.kind == JObject: applyConfigJson(result, raw)
    except CatchableError as e:
      stderr.writeLine("Warning: failed to load " & expanded & ": " & e.msg)
  let local = findLocalConfig(cwd)
  if local.len > 0:
    try:
      let raw = expandEnvJson(parseJson(readFile(local)))
      if raw.kind == JObject: applyConfigJson(result, raw)
    except CatchableError as e:
      stderr.writeLine("Warning: failed to load " & local & ": " & e.msg)
  result.logDir = expandTilde(result.logDir)
  for i, d in result.skillsDirs:
    result.skillsDirs[i] = expandTilde(d)
  resolveModel(result)

proc loadSkills(dirs: seq[string]): seq[tuple[name, text: string]] =
  for rawDir in dirs:
    let d = expandTilde(rawDir)
    if not dirExists(d): continue
    for kind, entryPath in walkDir(d, relative = false):
      if kind != pcDir and kind != pcLinkToDir: continue
      let skillFile = entryPath / "SKILL.md"
      if not fileExists(skillFile): continue
      let text =
        try: readFile(skillFile)
        except IOError: continue
      let lines = text.splitLines()
      var name = extractFilename(entryPath)
      if lines.len == 0 or lines[0].strip() != "---":
        result.add((name, text))
        continue
      var i = 1
      while i < lines.len and lines[i].strip() != "---":
        if ':' in lines[i]:
          let parts = lines[i].split(':', maxsplit = 1)
          if parts[0].strip() == "name": name = parts[1].strip()
        inc i
      let body = lines[i + 1 .. ^1].join("\n").strip()
      if body.len > 0:
        result.add((name, body))

# ------------------------------------------------------------------------------
# TUI
# ------------------------------------------------------------------------------

type
  KeyChan* = Channel[char]

# Global channels — live for the process lifetime.
var gEq: EventChan
var gIq: InputChan
var gKeyChan: KeyChan

# Raw mode bookkeeping
var gOrigTermios: Termios
var gRawActive = false

proc enterRawMode() =
  if gRawActive: return
  if tcGetAttr(STDIN_FILENO, addr gOrigTermios) != 0: return
  var raw = gOrigTermios
  # cbreak-style: disable canonical + echo, keep Ctrl+C signal disabled.
  raw.c_lflag = raw.c_lflag and not Cflag(ICANON or ECHO or IEXTEN or ISIG)
  raw.c_iflag = raw.c_iflag and not Cflag(IXON or ICRNL or BRKINT or INPCK or ISTRIP)
  raw.c_cc[VMIN] = 1.char
  raw.c_cc[VTIME] = 0.char
  discard tcSetAttr(STDIN_FILENO, TCSANOW, addr raw)
  gRawActive = true

proc leaveRawMode() =
  if not gRawActive: return
  discard tcSetAttr(STDIN_FILENO, TCSANOW, addr gOrigTermios)
  gRawActive = false

proc restoreTerminal() {.noconv.} =
  leaveRawMode()
  stdout.write("\e[?25h")  # show cursor
  stdout.write("\e[0m")    # reset attrs
  stdout.flushFile()

proc signalExit(sig: cint) {.noconv.} =
  ## Restore the terminal before exiting on SIGINT/SIGTERM.
  restoreTerminal()
  quit(128 + sig.int)

proc inputThread() {.thread.} =
  ## Blocking byte-reader. Uses raw read(2) since getch() toggles termios
  ## per call and we've set raw mode ourselves.
  var b: char
  while true:
    let n = read(STDIN_FILENO, addr b, 1)
    if n <= 0:
      sleep(20)
      continue
    gKeyChan.send(b)

# TUI state
type
  TuiState = object
    model: string
    cwd: string
    toolCount: int
    models: JsonNode
    mcpServers: JsonNode
    pendingAuth: seq[string]
    activeModelKey: string
    tokenThresholdWarn: int
    tokenThresholdDanger: int
    gitBranch: string
    tokensIn: int
    tokensOut: int
    ibuf: string
    cpos: int
    running: bool
    approval: Option[JsonNode]
    history: seq[string]
    histIdx: int
    histStash: string
    spinner: bool
    spinnerFrame: int
    spinnerTime: float
    spinLineActive: bool
    streamingStarted: bool
    drawnRows: int

proc termWidth(): int =
  try: terminalWidth() except CatchableError: 80

proc gitBranch(cwd: string): string =
  try:
    let (output, code) = execCmdEx("git rev-parse --abbrev-ref HEAD",
                                   workingDir = cwd)
    if code == 0:
      return output.strip()
  except CatchableError:
    discard
  return ""

proc summarizeArgs(args: JsonNode, maxLen = 120): string =
  if args.isNil or args.kind != JObject or args.len == 0: return ""
  var parts: seq[string] = @[]
  for k, v in args.pairs:
    var s =
      case v.kind
      of JString: v.getStr()
      else: $v
    if s.len > 80: s = s[0 ..< 77] & "..."
    parts.add(k & "=\"" & s & "\"")
  let r = parts.join(", ")
  if r.len > maxLen: return r[0 ..< maxLen - 3] & "..."
  return r

proc outLn(s: string = "") =
  # In raw mode, bare '\n' doesn't return the cursor to column 0; need \r\n.
  stdout.write(s); stdout.write("\r\n"); stdout.flushFile()

proc outInline(s: string) =
  # Replace bare \n with \r\n for raw-mode correctness during streamed text.
  var converted = ""
  for ch in s:
    if ch == '\n' and (converted.len == 0 or converted[^1] != '\r'):
      converted.add('\r')
    converted.add(ch)
  stdout.write(converted); stdout.flushFile()

proc promptPrefix(t: TuiState): string =
  let short = extractFilename(t.cwd)
  let cwdShort = if short.len > 0: short else: t.cwd
  if t.gitBranch.len > 0:
    return "\e[1m" & cwdShort & "\e[2m#\e[0m\e[36m" & t.gitBranch & "\e[0m"
  return "\e[1m" & cwdShort & "\e[0m"

proc caret(t: TuiState): string =
  let tok = t.tokensIn + t.tokensOut
  if tok >= t.tokenThresholdDanger: return "\e[31m❯\e[0m"
  if tok >= t.tokenThresholdWarn: return "\e[33m❯\e[0m"
  return "❯"

proc banner(t: TuiState) =
  let d = "\e[2m"; let r = "\e[0m"
  outLn("")
  outLn(d & "█▀▀ █▀█ █▀▀" & r)
  outLn(d & "█   █ █ █ █" & r)
  outLn(d & "▀▀▀ ▀▀▀ ▀▀▀" & r)
  outLn(d & "model:" & r & " " & t.model)
  outLn(d & "type" & r & " /help " & d & "for commands" & r)
  outLn("")

const SlashCmds = ["/help", "/clear", "/tokens", "/model", "/mcp", "/quit", "/exit"]

proc ghostComplete(t: TuiState): string =
  let buf = t.ibuf.strip()
  if not buf.startsWith("/") or ' ' in buf: return ""
  var matches: seq[string] = @[]
  for c in SlashCmds:
    if c.startsWith(buf) and c != buf:
      matches.add(c)
  if matches.len == 1:
    return matches[0][buf.len .. ^1]
  return ""

proc plainPrefixLen(t: TuiState): int =
  ## Length of the unstyled prompt prefix "cwd#branch ❯ " for layout.
  let short = extractFilename(t.cwd)
  let cwdShort = if short.len > 0: short else: t.cwd
  result = cwdShort.len + 3  # " ❯ " = space, caret, space (counted as 3 cells)
  if t.gitBranch.len > 0:
    result += 1 + t.gitBranch.len  # '#' + branch

proc inputLayout(t: TuiState): tuple[rows: seq[(string, string)], crow: int, ccol: int] =
  let w = termWidth()
  let styledPfx = promptPrefix(t) & " " & caret(t) & " "
  let pfxLen = plainPrefixLen(t)
  let firstCap = max(w - pfxLen - 1, 1)
  let contCap = max(w - 2, 1)
  var rows: seq[(string, string)] = @[]
  var rowStarts = @[0]
  var prefix = styledPfx
  var cap = firstCap
  var line = ""
  for i, ch in t.ibuf:
    if ch == '\n':
      rows.add((prefix, line))
      prefix = "  "; cap = contCap; line = ""
      rowStarts.add(i + 1)
    elif line.len >= cap:
      rows.add((prefix, line))
      prefix = "  "; cap = contCap; line = $ch
      rowStarts.add(i)
    else:
      line.add(ch)
  rows.add((prefix, line))
  var crow = rows.len - 1
  for r in 0 ..< rows.len - 1:
    if t.cpos < rowStarts[r + 1]:
      crow = r; break
  let visPfxW = if crow == 0: pfxLen else: 2
  let ccol = visPfxW + t.cpos - rowStarts[crow]
  return (rows, crow, ccol)

proc drawInput(t: var TuiState) =
  let (rows, crow, ccol) = inputLayout(t)
  stdout.write("\e[?25l")
  stdout.write("\r\e[2K")
  let ghost = ghostComplete(t)
  for i, row in rows:
    let (prefix, text) = row
    if i > 0:
      stdout.write("\r\n\e[2K")
    stdout.write(prefix & text)
    if ghost.len > 0 and i == rows.len - 1:
      stdout.write("\e[2m" & ghost & "\e[0m")
  let up = rows.len - 1 - crow
  if up > 0:
    stdout.write("\e[" & $up & "A")
  stdout.write("\r")
  if ccol > 0:
    stdout.write("\e[" & $ccol & "C")
  stdout.write("\e[?25h")
  stdout.flushFile()
  t.drawnRows = rows.len

proc clearInput(t: TuiState) =
  let n = max(t.drawnRows, 1)
  stdout.write("\r\e[2K")
  for _ in 0 ..< n - 1:
    stdout.write("\e[1B\e[2K")
  if n > 1:
    stdout.write("\e[" & $(n - 1) & "A")
  stdout.write("\r")
  stdout.flushFile()

proc clearSpinner(t: var TuiState) =
  if t.spinLineActive:
    stdout.write("\r\e[2K")
    stdout.flushFile()
    t.spinLineActive = false

proc startSpinner(t: var TuiState) =
  t.spinner = true
  t.spinnerFrame = 0
  t.spinnerTime = epochTime()
  t.spinLineActive = true

proc stopSpinner(t: var TuiState) =
  if not t.spinner: return
  t.spinner = false
  clearSpinner(t)

const SpinFrames = ["⠷", "⠯", "⠻", "⠽", "⠾"]

proc tickSpinner(t: var TuiState) =
  if not t.spinner: return
  let now = epochTime()
  if now - t.spinnerTime < 0.08: return
  t.spinnerTime = now
  t.spinnerFrame = (t.spinnerFrame + 1) mod SpinFrames.len
  stdout.write("\r\e[2K\e[2m" & SpinFrames[t.spinnerFrame] & "\e[0m")
  stdout.flushFile()
  t.spinLineActive = true

proc byteLen(s: string): int {.inline.} = s.len

proc handleEvent(t: var TuiState, ev: JsonNode) =
  let typ = ev{"type"}.getStr()
  case typ
  of "user_message": discard  # already printed by submit
  of "assistant_text_delta":
    stopSpinner(t)
    var text = ev{"text"}.getStr()
    if not t.streamingStarted:
      text = text.strip(leading = true, trailing = false, chars = {'\n'})
      if text.len == 0: return
      t.streamingStarted = true
    outInline(text)
  of "assistant_text_final":
    outLn("")
  of "tool_call":
    stopSpinner(t)
    let s = summarizeArgs(ev{"input"})
    outLn("\e[36;1m> " & ev{"name"}.getStr("?") & "\e[0m\e[2m(" & s & ")\e[0m")
  of "tool_result":
    stopSpinner(t)
    let o = ev{"output"}.getStr()
    if ev{"is_error"}.getBool():
      let short = if o.len > 200: o[0 ..< 200] else: o
      outLn("\e[31m< ERROR: " & short & "\e[0m")
    else:
      outLn("\e[2m< [" & $byteLen(o) & " bytes]\e[0m")
    t.streamingStarted = false
    startSpinner(t)
  of "verbose":
    for line in ev{"data"}.getStr().split("\n"):
      outLn("\e[2m  " & line & "\e[0m")
  of "status":
    stopSpinner(t)
    outLn("\e[33m~ " & ev{"message"}.getStr() & "\e[0m")
  of "error":
    stopSpinner(t)
    outLn("\e[31m! " & ev{"message"}.getStr() & "\e[0m")
  of "turn_complete":
    stopSpinner(t)
    let usage = ev{"usage"}
    if usage != nil:
      t.tokensIn += usage{"input_tokens"}.getInt(0)
      t.tokensOut += usage{"output_tokens"}.getInt(0)
    outLn("")
    drawInput(t)
  of "approval_request":
    stopSpinner(t)
    let name = ev{"name"}.getStr("?")
    let s = summarizeArgs(ev{"input"})
    outLn("\e[33m? Allow " & name & "(" & s & ")? [y/n]\e[0m")
    t.approval = some(ev)
  else: discard

# Slash command handlers
proc cmdModel(t: var TuiState, args: seq[string]) =
  let d = "\e[2m"; let r = "\e[0m"
  if args.len == 0:
    outLn("  " & d & "active:" & r & " " & t.model)
    if t.models.kind == JObject:
      for name, m in t.models.pairs:
        let marker = if name == t.activeModelKey: " *" else: ""
        outLn("  " & d & name & ":" & r & " " & m{"model"}.getStr(name) & marker)
    return
  let name = args[0]
  if t.models.kind != JObject or not t.models.hasKey(name):
    var available: seq[string] = @[]
    if t.models.kind == JObject:
      for k, _ in t.models.pairs: available.add(k)
    outLn("  " & d & "unknown model: " & name & " (available: " & available.join(", ") & ")" & r)
    return
  let m = t.models[name]
  t.model = m{"model"}.getStr(name)
  t.activeModelKey = name
  let apiKeyEnv = m{"api_key_env"}.getStr("")
  let apiKey = if apiKeyEnv.len > 0: getEnv(apiKeyEnv, "") else: ""
  let baseUrl = m{"api_base_url"}.getStr("https://api.anthropic.com")
  gIq.send($(%*{
    "type": "switch_model",
    "model": t.model,
    "api_key": apiKey,
    "api_base_url": baseUrl
  }))
  outLn("  " & d & "switched to" & r & " " & t.model)

proc mcpServerConfig(t: TuiState, name: string): JsonNode =
  if t.mcpServers.isNil or t.mcpServers.kind != JArray: return nil
  for s in t.mcpServers:
    if s{"name"}.getStr() == name: return s
  return nil

proc cmdMcpAuth(t: var TuiState, name: string) =
  let d = "\e[2m"; let r = "\e[0m"
  if name notin t.pendingAuth:
    outLn("  " & d & name & " does not need auth" & r)
    return
  let cfg = mcpServerConfig(t, name)
  if cfg.isNil:
    outLn("  " & d & "unknown server: " & name & r)
    return
  # Leave raw mode so anything the browser/terminal emits doesn't corrupt state.
  leaveRawMode()
  outLn("  " & d & "opening browser for " & name & " authorization..." & r)
  let server = copy(cfg)
  server["_oauth_interactive"] = %true
  server["_next_id"] = %0
  server["_session_id"] = %""
  server["_oauth_token"] = %""
  server["_refresh_attempted"] = %false
  try:
    discard mcpOAuthFlow(server)
  except CatchableError as e:
    enterRawMode()
    outLn("  \e[31mauth failed: " & e.msg & "\e[0m")
    return
  enterRawMode()
  # Agent will load the token from disk and initialize the server.
  gIq.send($(%*{"type": "add_mcp_tools", "name": name}))
  t.pendingAuth.delete(t.pendingAuth.find(name))
  outLn("  " & d & "authenticated. tools will load shortly..." & r)

proc cmdMcpRevoke(t: var TuiState, name: string) =
  let d = "\e[2m"; let r = "\e[0m"
  let path = mcpTokenPath(name)
  if not fileExists(path):
    outLn("  " & d & "no token found for " & name & r)
    return
  try: removeFile(path)
  except OSError as e:
    outLn("  \e[31mfailed to remove token: " & e.msg & "\e[0m")
    return
  gIq.send($(%*{"type": "revoke_mcp_tools", "name": name}))
  if mcpServerConfig(t, name) != nil and name notin t.pendingAuth:
    t.pendingAuth.add(name)
  outLn("  " & d & "revoked " & name & r)

proc cmdMcp(t: var TuiState, args: seq[string]) =
  let d = "\e[2m"; let r = "\e[0m"
  if t.mcpServers.isNil or t.mcpServers.kind != JArray or t.mcpServers.len == 0:
    outLn("  " & d & "no MCP servers configured" & r)
    return
  if args.len == 0:
    for s in t.mcpServers:
      let sname = s{"name"}.getStr("?")
      if sname in t.pendingAuth:
        outLn("  " & d & sname & r & " " & s{"url"}.getStr("") &
              " \e[33m(auth required: /mcp auth " & sname & ")\e[0m")
      else:
        outLn("  " & d & sname & r & " " & s{"url"}.getStr(""))
    return
  let sub = args[0]
  if sub == "auth" and args.len > 1:
    cmdMcpAuth(t, args[1])
  elif sub == "revoke" and args.len > 1:
    cmdMcpRevoke(t, args[1])
  else:
    outLn("  " & d & "usage: /mcp [auth|revoke] <name>" & r)

proc handleSlash(t: var TuiState, cmd: string) =
  let d = "\e[2m"; let r = "\e[0m"
  let parts = cmd.splitWhitespace()
  if parts.len == 0: return
  let c = parts[0].toLowerAscii()
  case c
  of "/help":
    outLn("")
    outLn(d & "Commands:" & r)
    outLn("  /help              show this message")
    outLn("  /clear             clear screen")
    outLn("  /tokens            show token usage")
    outLn("  /model [name]      show or switch model")
    outLn("  /mcp [name]        list MCP servers or tools")
    outLn("  /quit              exit")
    outLn("")
    outLn(d & "Shortcuts:" & r)
    outLn("  Opt+Enter          newline")
    outLn("  Opt+Left/Right     word jump")
    outLn("  Opt+Delete         delete word")
    outLn("  Ctrl+U             delete line")
    outLn("  Ctrl+A/E           home / end")
    outLn("  Ctrl+C             exit")
    outLn("")
  of "/clear":
    stdout.write("\e[2J\e[H")
    stdout.flushFile()
  of "/tokens":
    let tok = t.tokensIn + t.tokensOut
    outLn("  " & d & "input:" & r & " " & $t.tokensIn &
          "  " & d & "output:" & r & " " & $t.tokensOut &
          "  " & d & "total:" & r & " " & $tok)
  of "/model":
    cmdModel(t, parts[1 .. ^1])
  of "/mcp":
    cmdMcp(t, parts[1 .. ^1])
  of "/quit", "/exit":
    t.running = false
  else:
    outLn("  " & d & "unknown command: " & c & " (try /help)" & r)

# Word movement
proc wordLeft(t: TuiState): int =
  var i = t.cpos
  if i > 0 and t.ibuf[i - 1] == '\n': return i - 1
  while i > 0 and not t.ibuf[i - 1].isAlphaNumeric() and t.ibuf[i - 1] != '\n':
    dec i
  while i > 0 and t.ibuf[i - 1].isAlphaNumeric():
    dec i
  return i

proc wordRight(t: TuiState): int =
  var i = t.cpos
  let n = t.ibuf.len
  while i < n and not t.ibuf[i].isAlphaNumeric():
    inc i
  while i < n and t.ibuf[i].isAlphaNumeric():
    inc i
  return i

# History
proc histPrev(t: var TuiState) =
  if t.history.len == 0 or t.histIdx <= 0: return
  if t.histIdx == t.history.len:
    t.histStash = t.ibuf
  dec t.histIdx
  t.ibuf = t.history[t.histIdx]
  t.cpos = t.ibuf.len

proc histNext(t: var TuiState) =
  if t.histIdx >= t.history.len: return
  inc t.histIdx
  if t.histIdx == t.history.len:
    t.ibuf = t.histStash
  else:
    t.ibuf = t.history[t.histIdx]
  t.cpos = t.ibuf.len

# Escape sequence reader — pulls additional bytes from keyChan with a
# short timeout (50ms, same as Python's select).
proc readEscByte(deadlineMs: int = 50): tuple[ok: bool, ch: char] =
  let deadline = epochTime() + deadlineMs.float / 1000.0
  while epochTime() < deadline:
    let (ok, ch) = gKeyChan.tryRecv()
    if ok: return (true, ch)
    sleep(5)
  return (false, '\0')

proc handleEsc(t: var TuiState) =
  let (ok, ch2) = readEscByte()
  if not ok: return
  case ch2
  of '\r', '\n':
    t.ibuf.insert("\n", t.cpos)
    inc t.cpos
  of 'b':
    t.cpos = wordLeft(t)
  of 'f':
    t.cpos = wordRight(t)
  of '\x7f':
    let wp = wordLeft(t)
    t.ibuf = t.ibuf[0 ..< wp] & t.ibuf[t.cpos .. ^1]
    t.cpos = wp
  of '[':
    var seq = ""
    while true:
      let (ok2, b) = readEscByte()
      if not ok2: break
      seq.add(b)
      if b.byte >= 0x40: break
    case seq
    of "A": histPrev(t)
    of "B": histNext(t)
    of "D":
      if t.cpos > 0: dec t.cpos
    of "C":
      if t.cpos < t.ibuf.len: inc t.cpos
    of "H", "1~": t.cpos = 0
    of "F", "4~": t.cpos = t.ibuf.len
    of "1;3D": t.cpos = wordLeft(t)
    of "1;3C": t.cpos = wordRight(t)
    else: discard
  else: discard

proc handleKey(t: var TuiState, ch: char) =
  if t.approval.isSome:
    if ch == 'y' or ch == 'Y':
      outLn("\e[33m  approved\e[0m")
      gIq.send($(%*{"type": "approval", "approved": true}))
      t.approval = none(JsonNode)
      startSpinner(t)
    elif ch == 'n' or ch == 'N':
      outLn("\e[33m  denied\e[0m")
      gIq.send($(%*{"type": "approval", "approved": false}))
      t.approval = none(JsonNode)
      startSpinner(t)
    return

  case ch
  of '\r', '\n':
    let text = t.ibuf.strip()
    if text.len > 0:
      clearInput(t)
      t.history.add(text)
      t.histIdx = t.history.len
      t.histStash = ""
      t.ibuf = ""; t.cpos = 0
      if text.startsWith("/"):
        var full = text
        let ghost = block:
          var saved = t
          saved.ibuf = text
          ghostComplete(saved)
        if ghost.len > 0: full = text & ghost
        outLn(promptPrefix(t) & " " & caret(t) & " " & full)
        handleSlash(t, full)
        drawInput(t)
        return
      outLn(promptPrefix(t) & " " & caret(t) & " " & text)
      t.streamingStarted = false
      gIq.send($(%*{"type": "user_input", "text": text}))
      startSpinner(t)
      return
  of '\x7f', '\x08':  # backspace / DEL
    if t.cpos > 0:
      t.ibuf = t.ibuf[0 ..< t.cpos - 1] & t.ibuf[t.cpos .. ^1]
      dec t.cpos
  of '\x15':  # Ctrl+U — delete to line start
    let nl = t.ibuf.rfind('\n', 0, t.cpos - 1)
    let start = if nl >= 0: nl + 1 else: 0
    if start == t.cpos and t.cpos > 0:
      t.ibuf = t.ibuf[0 ..< t.cpos - 1] & t.ibuf[t.cpos .. ^1]
      dec t.cpos
    else:
      t.ibuf = t.ibuf[0 ..< start] & t.ibuf[t.cpos .. ^1]
      t.cpos = start
  of '\x09':  # Tab — accept ghost
    if t.ibuf.startsWith("/"):
      let ghost = ghostComplete(t)
      if ghost.len > 0:
        t.ibuf.add(ghost)
        t.cpos = t.ibuf.len
  of '\x01': t.cpos = 0                           # Ctrl+A
  of '\x05': t.cpos = t.ibuf.len                  # Ctrl+E
  of '\x03': t.running = false; return            # Ctrl+C
  of '\x1b': handleEsc(t); drawInput(t); return   # Esc
  else:
    if ch.byte >= 32 and ch.byte < 127:
      t.ibuf.insert($ch, t.cpos)
      inc t.cpos
  drawInput(t)

proc tuiRun(t: var TuiState, pendingAuth: seq[string]) =
  banner(t)
  if pendingAuth.len > 0:
    let suffix = if pendingAuth.len > 1: "s" else: ""
    outLn("\e[33m" & $pendingAuth.len & " MCP server" & suffix &
          " need auth: " & pendingAuth.join(", ") & "\e[0m")
    outLn("\e[2mrun /mcp to see details\e[0m")
    outLn("")
  drawInput(t)
  while t.running:
    # Drain events (non-blocking)
    while true:
      let (ok, raw) = gEq.tryRecv()
      if not ok: break
      let ev =
        try: parseJson(raw)
        except JsonParsingError: continue
      handleEvent(t, ev)
    # Drain keys (non-blocking)
    var drew = false
    while true:
      let (ok, ch) = gKeyChan.tryRecv()
      if not ok: break
      handleKey(t, ch)
      drew = true
      if not t.running: break
    if drew: discard  # handleKey already redrew
    tickSpinner(t)
    sleep(15)

proc printUsage() =
  echo "cog - minimal coding agent"
  echo "Usage: cog [--config PATH] [--cwd DIR] [--auto] [--verbose]"

proc main() =
  var cfgPath = "~/.cog/config.json"
  var cwd = "."
  var autoFlag = false
  var verboseFlag = false

  var p = initOptParser(shortNoVal = {'h'}, longNoVal = @["auto", "verbose", "help"])
  while true:
    p.next()
    case p.kind
    of cmdEnd: break
    of cmdShortOption, cmdLongOption:
      case p.key
      of "config": cfgPath = p.val
      of "cwd": cwd = p.val
      of "auto": autoFlag = true
      of "verbose": verboseFlag = true
      of "help", "h": printUsage(); return
      else: discard
    of cmdArgument: discard

  cwd = absolutePath(cwd)
  var cfg = loadConfig(cfgPath, cwd)
  if autoFlag: cfg.autoApprove = true
  if verboseFlag: cfg.verbose = true
  if cfg.apiKey.len == 0 and cfg.apiBaseUrl == "https://api.anthropic.com":
    stderr.writeLine("Error: API key not found. Run: export " & cfg.apiKeyEnv & "=your-key")
    quit(1)

  toolsConfigure(cwd, cfg.shellTimeoutSeconds)
  let skills = loadSkills(cfg.skillsDirs)
  var prompt = SystemPromptTemplate % [cwd]
  for s in skills:
    prompt.add("\n<skill name=\"" & s.name & "\">\n" & s.text & "\n</skill>\n")
  cfg.systemPrompt = prompt

  var tools = builtinTools()
  let (mcpTools, pendingAuth) = mcpDiscoverAll(cfg.mcpServers)
  for k, v in mcpTools.pairs:
    tools[k] = v

  createDir(cfg.logDir)
  let logPath = cfg.logDir / (now().utc().format("yyyy-MM-dd'T'HH-mm-ss") & ".jsonl")

  gEq.open()
  gIq.open()
  gEqGlobal = addr gEq
  gIqGlobal = addr gIq
  gAgentConfig = cfg
  gLogPath = logPath
  gToolRegGlobal = tools

  let interactive = isatty(STDIN_FILENO) != 0
  gKeyChan.open()
  var agentThread: Thread[void]
  createThread(agentThread, agentWorker)
  var inputThr: Thread[void]
  if interactive:
    createThread(inputThr, inputThread)

  # Initial model key resolution for the TUI display
  var activeKey = ""
  if cfg.models.kind == JObject:
    for k, m in cfg.models.pairs:
      if m{"model"}.getStr(k) == cfg.model:
        activeKey = k; break

  var tui = TuiState(
    model: cfg.model,
    cwd: cwd,
    toolCount: tools.len,
    models: cfg.models,
    mcpServers: cfg.mcpServers,
    pendingAuth: pendingAuth,
    activeModelKey: activeKey,
    tokenThresholdWarn: cfg.tokenThresholdWarn,
    tokenThresholdDanger: cfg.tokenThresholdDanger,
    gitBranch: gitBranch(cwd),
    ibuf: "",
    cpos: 0,
    running: true,
    approval: none(JsonNode),
    history: @[],
    histIdx: 0,
  )

  if interactive:
    addExitProc(restoreTerminal)
    discard signal(SIGINT, signalExit)
    discard signal(SIGTERM, signalExit)
    discard signal(SIGHUP, signalExit)
    stdout.write("\e[?25l")
    enterRawMode()
    try:
      tuiRun(tui, pendingAuth)
    except CatchableError as e:
      leaveRawMode()
      stderr.writeLine("Fatal: " & e.msg)
    finally:
      restoreTerminal()
      outLn("\r\n\e[2mbye\e[0m")
  else:
    # Non-TTY fallback: line-oriented harness. Useful for scripting, tests,
    # and piped input. Approval prompts are answered from the next stdin line.
    stdout.writeLine("cog (nim) — " & cfg.model & " — " & $tools.len & " tools")
    stdout.write("> "); stdout.flushFile()
    while true:
      let line =
        try: stdin.readLine()
        except EOFError: break
      if line == "/quit" or line == "/exit": break
      if line.len == 0:
        stdout.write("> "); stdout.flushFile(); continue
      gIq.send($(%*{"type": "user_input", "text": line}))
      while true:
        let raw = gEq.recv()
        let ev =
          try: parseJson(raw)
          except JsonParsingError: continue
        let typ = ev{"type"}.getStr()
        case typ
        of "assistant_text_delta":
          stdout.write(ev{"text"}.getStr()); stdout.flushFile()
        of "assistant_text_final": stdout.write("\n")
        of "tool_call":
          stdout.writeLine("\n> " & ev{"name"}.getStr() & "(" & $ev{"input"} & ")")
        of "tool_result":
          let o = ev{"output"}.getStr()
          let short = if o.len > 200: o[0..199] & "…" else: o
          stdout.writeLine("< " & short)
        of "status":
          stdout.writeLine("~ " & ev{"message"}.getStr())
        of "error":
          stdout.writeLine("! " & ev{"message"}.getStr())
        of "approval_request":
          stdout.write("? approve " & ev{"name"}.getStr() & " [y/N] ")
          stdout.flushFile()
          let reply =
            try: stdin.readLine()
            except EOFError: "n"
          let approved = reply.toLowerAscii.startsWith("y")
          gIq.send($(%*{"type": "approval", "approved": approved}))
        of "turn_complete":
          stdout.write("> "); stdout.flushFile(); break
        else: discard
        if typ == "error": break

  gIq.send("__SHUTDOWN__")
  try: agentThread.joinThread() except CatchableError: discard
  # Input thread is blocked in read(); can't cleanly join. Just exit.
  gEq.close()
  gIq.close()
  gKeyChan.close()

when isMainModule:
  main()
