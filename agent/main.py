import argparse
import json
import os
import queue
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import mcp_client
from .agent import Agent
from .tools import configure, get_tools
from .tui import TUI

DEFAULTS = {
    "model": "claude-sonnet-4-20250514",
    "api_key_env": "ANTHROPIC_API_KEY",
    "system_prompt": (
        "You are a coding agent. Use tools when helpful. "
        "Prefer small, safe, concrete steps. Explain actions briefly. Keep responses concise."
    ),
    "skills_dirs": [],
    "mcp_servers": [],
    "shell_enabled": False,
    "max_tool_calls_per_turn": 10,
    "shell_timeout_seconds": 30,
    "tool_output_max_bytes": 32768,
    "log_dir": "~/.agent/logs",
    "auto_approve": False,
}

BASE_SYSTEM = (
    "You are a coding agent working in the directory: {cwd}\n\n"
    "You have access to tools for reading, writing, and editing files, "
    "listing directories, and optionally running shell commands. Use them when helpful.\n\n"
    "Guidelines:\n"
    "- Take small, concrete steps. Read before writing.\n"
    "- Use str_replace for targeted edits. Use write_file for new files or complete rewrites.\n"
    "- Explain what you're doing briefly before each action.\n"
    "- If a tool call fails, read the error and adjust.\n"
    "- Do not invent file contents or tool outputs.\n"
    "- Keep responses concise unless the user asks for detail.\n"
)


def _expand_env(value):
    if isinstance(value, str):
        return re.sub(r"\$\{([^}]+)\}", lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(path):
    cfg = dict(DEFAULTS)
    path = os.path.expanduser(path)
    if os.path.exists(path):
        with open(path) as f:
            user_cfg = json.load(f)
        cfg.update(user_cfg)
    cfg = _expand_env(cfg)
    for key in ("log_dir", "skills_dirs"):
        v = cfg[key]
        if isinstance(v, str):
            cfg[key] = os.path.expanduser(v)
        elif isinstance(v, list):
            cfg[key] = [os.path.expanduser(p) for p in v]
    cfg["api_key"] = os.environ.get(cfg["api_key_env"], "")
    return cfg


def load_skills(dirs):
    skills = []
    for d in dirs:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        for entry in os.listdir(d):
            skill_file = os.path.join(d, entry, "SKILL.md")
            if not os.path.isfile(skill_file):
                continue
            with open(skill_file) as f:
                text = f.read()
            name, desc, body = _parse_frontmatter(text)
            if body:
                skills.append({"name": name or entry, "description": desc, "text": body})
    return skills


def _parse_frontmatter(text):
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, None, text
    name = desc = None
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        line = lines[i]
        if ":" in line:
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if k == "name":
                name = v
            elif k == "description":
                desc = v
        i += 1
    body = "\n".join(lines[i + 1:]).strip()
    return name, desc, body


def build_system_prompt(base, skills, cwd):
    prompt = base.format(cwd=cwd)
    for s in skills:
        prompt += f'\n<skill name="{s["name"]}">\n{s["text"]}\n</skill>\n'
    return prompt


def make_logger(log_dir):
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    path = os.path.join(log_dir, f"{ts}.jsonl")
    f = open(path, "a")
    def log(event):
        f.write(json.dumps(event) + "\n")
        f.flush()
    return log


def main():
    parser = argparse.ArgumentParser(description="Minimal coding agent")
    parser.add_argument("--config", default="~/.agent/config.json")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--shell", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    cwd = os.path.abspath(args.cwd)

    if args.shell:
        cfg["shell_enabled"] = True

    if not cfg["api_key"]:
        print(f"Error: set {cfg['api_key_env']} environment variable", file=__import__("sys").stderr)
        raise SystemExit(1)

    configure(cwd=cwd, shell_enabled=cfg["shell_enabled"], shell_timeout=cfg.get("shell_timeout_seconds", 30))
    skills = load_skills(cfg.get("skills_dirs", []))
    system_prompt = build_system_prompt(BASE_SYSTEM, skills, cwd)

    tool_registry = get_tools(cfg["shell_enabled"])
    mcp_tools, _servers = mcp_client.discover_all(cfg.get("mcp_servers", []))
    tool_registry.update(mcp_tools)

    log_fn = make_logger(os.path.expanduser(cfg["log_dir"]))
    cfg["system_prompt"] = system_prompt
    cfg["_log_fn"] = log_fn

    event_queue = queue.Queue()
    input_queue = queue.Queue()

    agent = Agent(cfg, tool_registry, event_queue, input_queue)
    worker = threading.Thread(target=agent.worker_loop, daemon=True)
    worker.start()

    tui = TUI(
        event_queue, input_queue,
        model=cfg["model"], cwd=cwd, tool_count=len(tool_registry),
    )
    tui.run()
    input_queue.put(None)
