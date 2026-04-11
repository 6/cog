import os
import subprocess
from pathlib import Path

_cwd = "."
_shell_enabled = False
_shell_timeout = 30


def configure(cwd=".", shell_enabled=False, shell_timeout=30):
    global _cwd, _shell_enabled, _shell_timeout
    _cwd = os.path.abspath(cwd)
    _shell_enabled = shell_enabled
    _shell_timeout = shell_timeout


def _resolve(path):
    p = Path(path)
    if not p.is_absolute():
        p = Path(_cwd) / p
    return str(p.resolve())


def read_file(args):
    path = _resolve(args["path"])
    try:
        with open(path, "rb") as f:
            sample = f.read(8192)
        if b"\x00" in sample:
            return f"ERROR: {path} appears to be a binary file"
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except PermissionError:
        return f"ERROR: permission denied: {path}"
    except Exception as e:
        return f"ERROR: {e}"


def list_dir(args):
    path = _resolve(args.get("path", "."))
    try:
        entries = sorted(os.listdir(path))
    except FileNotFoundError:
        return f"ERROR: directory not found: {path}"
    except PermissionError:
        return f"ERROR: permission denied: {path}"
    lines = []
    for name in entries:
        full = os.path.join(path, name)
        kind = "[dir] " if os.path.isdir(full) else "[file]"
        lines.append(f"{kind} {name}")
    return "\n".join(lines) if lines else "(empty directory)"


def write_file(args):
    path = _resolve(args["path"])
    content = args["content"]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"OK: wrote {len(content.encode('utf-8'))} bytes to {path}"
    except Exception as e:
        return f"ERROR: {e}"


def str_replace(args):
    path = _resolve(args["path"])
    old_str = args["old_str"]
    new_str = args["new_str"]
    try:
        content = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except Exception as e:
        return f"ERROR: {e}"
    count = content.count(old_str)
    if count == 0:
        return "ERROR: old_str not found in file"
    if count > 1:
        return (
            f"ERROR: old_str matched {count} times, must be unique. "
            "Add more surrounding context to make it unique."
        )
    new_content = content.replace(old_str, new_str, 1)
    Path(path).write_text(new_content, encoding="utf-8")
    return "OK: replacement made"


def run_shell(args):
    if not _shell_enabled:
        return "ERROR: Shell is disabled. Enable with --shell flag or shell_enabled in config."
    command = args["command"]
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=_shell_timeout, cwd=_cwd,
        )
        out = ""
        if r.stdout:
            out += r.stdout
        if r.stderr:
            out += r.stderr
        out += f"\n[exit code: {r.returncode}]"
        return out.strip()
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {_shell_timeout}s"
    except Exception as e:
        return f"ERROR: {e}"


_TOOL_DEFS = {
    "read_file": (read_file, {
        "name": "read_file",
        "description": "Read the contents of a file at the given path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read (relative to cwd or absolute)"}
            },
            "required": ["path"],
        },
    }),
    "list_dir": (list_dir, {
        "name": "list_dir",
        "description": "List directory contents with type indicators ([dir] or [file]).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: current directory)"}
            },
            "required": [],
        },
    }),
    "write_file": (write_file, {
        "name": "write_file",
        "description": "Write content to a file, creating parent directories if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write"},
                "content": {"type": "string", "description": "Content to write to the file"},
            },
            "required": ["path", "content"],
        },
    }),
    "str_replace": (str_replace, {
        "name": "str_replace",
        "description": (
            "Replace an exact string match in a file. The old_str must appear exactly once. "
            "If it matches zero or more than one time, the call fails. "
            "Include enough surrounding context lines in old_str to make the match unique."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to edit"},
                "old_str": {"type": "string", "description": "Exact string to find (must appear exactly once)"},
                "new_str": {"type": "string", "description": "String to replace it with"},
            },
            "required": ["path", "old_str", "new_str"],
        },
    }),
    "run_shell": (run_shell, {
        "name": "run_shell",
        "description": "Run a shell command and return combined stdout and stderr with exit code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"}
            },
            "required": ["command"],
        },
    }),
}


def get_tools(shell_enabled=False):
    tools = {}
    for name, (fn, schema) in _TOOL_DEFS.items():
        if name == "run_shell" and not shell_enabled:
            continue
        tools[name] = ("builtin", fn, schema)
    return tools
