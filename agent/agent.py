import json
import queue
from datetime import datetime, timezone

from . import anthropic_api
from . import mcp_client as mcp


class Agent:
    def __init__(self, config, tool_registry, event_queue, input_queue):
        self.model = config["model"]
        self.api_key = config["api_key"]
        self.system = config["system_prompt"]
        self.tools = tool_registry
        self.event_queue = event_queue
        self.input_queue = input_queue
        self.messages = []
        self.max_tool_calls = config.get("max_tool_calls_per_turn", 10)
        self.max_output = config.get("tool_output_max_bytes", 32768)
        self.auto_approve = config.get("auto_approve", False)
        self.log = config.get("_log_fn")

    def emit(self, event):
        event["ts"] = datetime.now(timezone.utc).isoformat()
        self.event_queue.put(event)
        if self.log and event["type"] != "assistant_text_delta":
            self.log(event)

    def run_turn(self, user_input):
        self.messages.append({"role": "user", "content": user_input})
        self.emit({"type": "user_message", "content": user_input})
        tool_count = 0

        while True:
            req = anthropic_api.build_request(
                self.model, self.system, self.messages, self.tools,
            )
            try:
                resp, conn = anthropic_api.stream_request(self.api_key, req)
            except anthropic_api.APIError as e:
                self.emit({"type": "error", "message": str(e)})
                return
            except Exception as e:
                self.emit({"type": "error", "message": f"Network error: {e}"})
                return

            content_blocks = []
            tool_uses = []
            usage = {}
            full_text = ""

            try:
                for kind, payload in anthropic_api.parse_sse_stream(resp):
                    if kind == "text_delta":
                        self.emit({"type": "assistant_text_delta", "text": payload})
                    elif kind == "text_final":
                        full_text = payload
                        self.emit({"type": "assistant_text_final", "text": payload})
                    elif kind == "tool_use":
                        tool_uses.append(payload)
                        self.emit({
                            "type": "tool_call",
                            "tool_id": payload["id"],
                            "name": payload["name"],
                            "input": payload["input"],
                        })
                    elif kind == "usage":
                        usage = payload
                    elif kind == "stop":
                        pass
            except Exception as e:
                self.emit({"type": "error", "message": f"Stream error: {e}"})
                return
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

            if full_text:
                content_blocks.append({"type": "text", "text": full_text})
            for tu in tool_uses:
                content_blocks.append({
                    "type": "tool_use", "id": tu["id"],
                    "name": tu["name"], "input": tu["input"],
                })

            if content_blocks:
                self.messages.append({"role": "assistant", "content": content_blocks})

            if not tool_uses:
                self.emit({"type": "turn_complete", "usage": usage})
                return

            tool_results = []
            for tu in tool_uses:
                tool_count += 1
                if tool_count > self.max_tool_calls:
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": tu["id"],
                        "content": "ERROR: maximum tool calls per turn exceeded",
                        "is_error": True,
                    })
                    self.emit({
                        "type": "tool_result", "tool_id": tu["id"],
                        "output": "ERROR: maximum tool calls per turn exceeded",
                        "is_error": True,
                    })
                    continue

                output, is_error = self.dispatch_tool(tu["name"], tu["input"])
                if len(output.encode("utf-8", errors="replace")) > self.max_output:
                    output = output[:self.max_output] + "\n[truncated]"

                tool_results.append({
                    "type": "tool_result", "tool_use_id": tu["id"],
                    "content": output, "is_error": is_error,
                })
                self.emit({
                    "type": "tool_result", "tool_id": tu["id"],
                    "output": output, "is_error": is_error,
                })

            self.messages.append({"role": "user", "content": tool_results})

            if tool_count > self.max_tool_calls:
                self.emit({"type": "turn_complete", "usage": usage})
                return

    def dispatch_tool(self, name, input_args):
        if name not in self.tools:
            return f"ERROR: unknown tool '{name}'", True

        entry = self.tools[name]

        if entry[0] == "builtin":
            _, fn, schema = entry
            needs_approval = name in ("write_file", "str_replace", "run_shell")
            if needs_approval and not self.auto_approve:
                if not self._get_approval(name, input_args):
                    return "Tool call denied by user.", True
            try:
                result = fn(input_args)
                return result, result.startswith("ERROR:")
            except Exception as e:
                return f"ERROR: {e}", True

        elif entry[0] == "mcp":
            _, server, schema, real_name = entry
            if not self.auto_approve:
                if not self._get_approval(name, input_args):
                    return "Tool call denied by user.", True
            try:
                result = mcp.call_tool(server, real_name, input_args)
                return result, result.startswith("ERROR:")
            except Exception as e:
                return f"ERROR: MCP call failed: {e}", True

        return f"ERROR: unknown tool type for '{name}'", True

    def _get_approval(self, name, input_args):
        self.emit({
            "type": "approval_request", "name": name, "input": input_args,
        })
        try:
            response = self.input_queue.get(timeout=300)
            if isinstance(response, dict) and response.get("type") == "approval":
                return response.get("approved", False)
            return False
        except queue.Empty:
            return False

    def worker_loop(self):
        while True:
            user_input = self.input_queue.get()
            if user_input is None:
                break
            if isinstance(user_input, dict):
                continue
            try:
                self.run_turn(user_input)
            except Exception as e:
                self.emit({"type": "error", "message": f"Agent error: {e}"})
