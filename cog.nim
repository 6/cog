## cog - minimal coding agent. Nim port of cog.py, single file, stdlib only.

import std/[
  json, os, osproc, strutils, streams, httpclient, math,
  times, tables, parseopt, algorithm
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
    # MCP — wired up in phase 3
    return ("ERROR: MCP not implemented yet", true)

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
# CLI (temporary harness until TUI lands in Phase 2)
# ------------------------------------------------------------------------------

proc handleEvent(raw: string) =
  let ev =
    try: parseJson(raw)
    except JsonParsingError: return
  let t = ev{"type"}.getStr()
  case t
  of "assistant_text_delta":
    stdout.write(ev{"text"}.getStr())
    stdout.flushFile()
  of "assistant_text_final":
    stdout.write("\n")
  of "tool_call":
    stdout.writeLine("\n▶ " & ev{"name"}.getStr() & "(" & $ev{"input"} & ")")
  of "tool_result":
    let output = ev{"output"}.getStr()
    let short = if output.len > 200: output[0..199] & "…" else: output
    stdout.writeLine("◀ " & short)
  of "approval_request":
    stdout.write("\n? approve " & ev{"name"}.getStr() & " " & $ev{"input"} & " [y/N] ")
    stdout.flushFile()
  of "turn_complete":
    stdout.write("\n> ")
    stdout.flushFile()
  of "error":
    stdout.writeLine("\n[error] " & ev{"message"}.getStr())
    stdout.write("> "); stdout.flushFile()
  of "status":
    stdout.writeLine("\n[" & ev{"message"}.getStr() & "]")
  else: discard

# Global channels live for the process lifetime.
var gEq: EventChan
var gIq: InputChan

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

  let tools = builtinTools()

  createDir(cfg.logDir)
  let logPath = cfg.logDir / (now().utc().format("yyyy-MM-dd'T'HH-mm-ss") & ".jsonl")

  gEq.open()
  gIq.open()
  gEqGlobal = addr gEq
  gIqGlobal = addr gIq
  gAgentConfig = cfg
  gLogPath = logPath
  gToolRegGlobal = tools

  var agentThread: Thread[void]
  createThread(agentThread, agentWorker)

  stdout.writeLine("cog (nim) — " & cfg.model & " — " & $tools.len & " tools")
  stdout.write("> "); stdout.flushFile()

  while true:
    let line =
      try: stdin.readLine()
      except EOFError: break
    if line.len == 0:
      stdout.write("> "); stdout.flushFile()
      continue
    if line == "/quit" or line == "/exit":
      break

    gIq.send($(%*{"type": "user_input", "text": line}))

    # Drain events until we see turn_complete or error.
    while true:
      let raw = gEq.recv()
      handleEvent(raw)
      let ev =
        try: parseJson(raw)
        except JsonParsingError: continue
      let t = ev{"type"}.getStr()
      if t == "turn_complete" or t == "error": break
      if t == "approval_request":
        # Block for user input and forward the response.
        let reply =
          try: stdin.readLine()
          except EOFError: "n"
        let approved = reply.toLowerAscii.startsWith("y")
        gIq.send($(%*{"type": "approval", "approved": approved}))

  gIq.send("__SHUTDOWN__")
  agentThread.joinThread()
  gEq.close()
  gIq.close()

when isMainModule:
  main()
