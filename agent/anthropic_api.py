import http.client
import json
import ssl


def build_request(model, system, messages, tools, max_tokens=4096):
    req = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "stream": True,
    }
    if tools:
        req["tools"] = [t for _, _, t in tools.values()]
    return req


def stream_request(api_key, request_body):
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection("api.anthropic.com", timeout=120, context=ctx)
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": api_key,
        "anthropic-version": "2023-06-01",
    }
    conn.request("POST", "/v1/messages", json.dumps(request_body), headers)
    resp = conn.getresponse()
    if resp.status != 200:
        body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        raise APIError(resp.status, body)
    return resp, conn


class APIError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"API error {status}: {body}")


def read_lines(response):
    while True:
        line = response.readline()
        if not line:
            break
        yield line


def parse_sse_stream(response):
    event_type = None
    block_type = None
    block_id = None
    block_name = None
    json_accum = ""
    full_text = ""
    usage = {"input_tokens": 0, "output_tokens": 0}

    for raw_line in read_lines(response):
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")

        if line.startswith("event: "):
            event_type = line[7:]
            continue

        if not line.startswith("data: "):
            continue

        data = json.loads(line[6:])

        if event_type == "message_start":
            msg = data.get("message", {})
            u = msg.get("usage", {})
            usage["input_tokens"] = u.get("input_tokens", 0)

        elif event_type == "content_block_start":
            cb = data.get("content_block", {})
            block_type = cb.get("type")
            if block_type == "tool_use":
                block_id = cb.get("id")
                block_name = cb.get("name")
                json_accum = ""
            elif block_type == "text":
                pass

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
                yield ("tool_use", {
                    "id": block_id,
                    "name": block_name,
                    "input": tool_input,
                })
            elif block_type == "text":
                yield ("text_final", full_text)
            block_type = None
            block_id = None
            block_name = None
            json_accum = ""

        elif event_type == "message_delta":
            u = data.get("usage", {})
            usage["output_tokens"] = u.get("output_tokens", 0)
            yield ("stop", data.get("delta", {}).get("stop_reason", "end_turn"))

        elif event_type == "message_stop":
            yield ("usage", dict(usage))

        elif event_type == "ping":
            pass

        event_type = None
