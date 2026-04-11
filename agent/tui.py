import atexit
import os
import select
import signal
import sys
import time

_original_termios = None
_in_alt_screen = False
_fd = None


def enable_ansi():
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


def _write(s):
    sys.stdout.write(s)


def _flush():
    sys.stdout.flush()


def enter_alt_screen():
    global _in_alt_screen
    _write("\033[?1049h\033[2J\033[H")
    _flush()
    _in_alt_screen = True


def exit_alt_screen():
    global _in_alt_screen
    if _in_alt_screen:
        _write("\033[?1049l")
        _flush()
        _in_alt_screen = False


def set_cbreak():
    global _original_termios, _fd
    if os.name == "nt":
        return
    import termios
    import tty
    _fd = sys.stdin.fileno()
    _original_termios = termios.tcgetattr(_fd)
    tty.setcbreak(_fd)


def restore_terminal():
    global _original_termios
    if _original_termios is not None:
        import termios
        try:
            termios.tcsetattr(_fd, termios.TCSADRAIN, _original_termios)
        except Exception:
            pass
        _original_termios = None
    _write("\033[?25h")
    exit_alt_screen()
    _flush()


def _cleanup():
    restore_terminal()


def get_size():
    try:
        cols, rows = os.get_terminal_size()
        return max(rows, 5), max(cols, 20)
    except OSError:
        return 24, 80


def move(row, col):
    _write(f"\033[{row};{col}H")


def clear_line():
    _write("\033[2K")


def set_scroll_region(top, bottom):
    _write(f"\033[{top};{bottom}r")


def wrap_text(text, width):
    lines = []
    for raw_line in text.split("\n"):
        if not raw_line:
            lines.append("")
            continue
        while len(raw_line) > width:
            lines.append(raw_line[:width])
            raw_line = raw_line[width:]
        lines.append(raw_line)
    return lines


class TUI:
    def __init__(self, event_queue, input_queue, model="", cwd="", tool_count=0):
        self.event_queue = event_queue
        self.input_queue = input_queue
        self.model = model
        self.cwd = cwd
        self.tool_count = tool_count
        self.transcript_lines = []
        self.input_buffer = ""
        self.cursor_pos = 0
        self.scroll_offset = 0
        self.needs_redraw = True
        self.running = True
        self.height = 24
        self.width = 80
        self.current_text = ""
        self.awaiting_approval = None
        self.input_enabled = True

    def _status_text(self):
        cwd_short = os.path.basename(self.cwd) or self.cwd
        return f" model: {self.model} | cwd: {cwd_short} | tools: {self.tool_count} "

    def draw_status_bar(self):
        move(1, 1)
        clear_line()
        status = self._status_text()
        status = status[:self.width].ljust(self.width)
        _write(f"\033[7m{status}\033[0m")

    def draw_input_line(self):
        move(self.height - 1, 1)
        clear_line()
        move(self.height, 1)
        clear_line()
        if self.awaiting_approval:
            name = self.awaiting_approval.get("name", "?")
            inp = self.awaiting_approval.get("input", {})
            summary = _summarize_args(inp)
            prompt = f"Allow {name}({summary})? [y/n] "
            _write(f"\033[33m{prompt[:self.width]}\033[0m")
        else:
            prefix = "> "
            visible = self.input_buffer[:self.width - 3]
            _write(prefix + visible)
            col = len(prefix) + min(self.cursor_pos, self.width - 3)
            move(self.height, col + 1)

    def draw_transcript(self):
        top = 2
        bottom = self.height - 2
        visible_count = bottom - top + 1
        if visible_count <= 0:
            return

        total = len(self.transcript_lines)
        start = max(0, total - visible_count - self.scroll_offset)
        end = start + visible_count

        for i in range(visible_count):
            row = top + i
            move(row, 1)
            clear_line()
            idx = start + i
            if 0 <= idx < total:
                line = self.transcript_lines[idx]
                _write(line[:self.width])

    def full_redraw(self):
        self.height, self.width = get_size()
        _write("\033[2J")
        set_scroll_region(2, self.height - 2)
        self.draw_status_bar()
        self.draw_transcript()
        self.draw_input_line()
        _flush()
        self.needs_redraw = False

    def append_transcript(self, styled_lines):
        self.transcript_lines.extend(styled_lines)
        if self.scroll_offset == 0:
            self.needs_redraw = True

    def handle_event(self, event):
        etype = event.get("type")

        if etype == "user_message":
            text = event.get("content", "")
            lines = wrap_text(f"\033[1mYou:\033[0m {text}", self.width)
            self.append_transcript([""])
            self.append_transcript(lines)
            self.input_enabled = False

        elif etype == "assistant_text_delta":
            self.current_text += event.get("text", "")
            self._update_streaming_text()

        elif etype == "assistant_text_final":
            self.current_text = ""

        elif etype == "tool_call":
            name = event.get("name", "?")
            inp = event.get("input", {})
            summary = _summarize_args(inp)
            line = f"\033[36m> {name}({summary})\033[0m"
            self.append_transcript(wrap_text(line, self.width))

        elif etype == "tool_result":
            output = event.get("output", "")
            is_err = event.get("is_error", False)
            byte_count = len(output.encode("utf-8", errors="replace"))
            if is_err:
                line = f"\033[31m< ERROR: {output[:200]}\033[0m"
            else:
                line = f"\033[2m< [{byte_count} bytes]\033[0m"
            self.append_transcript(wrap_text(line, self.width))

        elif etype == "error":
            msg = event.get("message", "unknown error")
            line = f"\033[31m! {msg}\033[0m"
            self.append_transcript(wrap_text(line, self.width))
            self.input_enabled = True

        elif etype == "status":
            msg = event.get("message", "")
            self.append_transcript([f"\033[2m~ {msg}\033[0m"])

        elif etype == "turn_complete":
            self.input_enabled = True

        elif etype == "approval_request":
            self.awaiting_approval = event
            self.needs_redraw = True

    def _update_streaming_text(self):
        tag = "\033[1mClaude:\033[0m "
        lines = wrap_text(tag + self.current_text, self.width)
        marker = "___STREAM___"
        cleaned = [l for l in self.transcript_lines if not l.startswith(marker)]
        tagged = [marker + l for l in lines]
        self.transcript_lines = cleaned + tagged
        if self.scroll_offset == 0:
            self.needs_redraw = True

    def finalize_streaming_text(self):
        marker = "___STREAM___"
        self.transcript_lines = [
            l[len(marker):] if l.startswith(marker) else l
            for l in self.transcript_lines
        ]

    def handle_key(self, ch):
        if self.awaiting_approval:
            if ch in (b"y", b"Y"):
                self.input_queue.put({"type": "approval", "approved": True})
                self.awaiting_approval = None
                self.needs_redraw = True
            elif ch in (b"n", b"N"):
                self.input_queue.put({"type": "approval", "approved": False})
                self.awaiting_approval = None
                self.needs_redraw = True
            return

        if ch == b"\r" or ch == b"\n":
            text = self.input_buffer.strip()
            if text:
                self.input_buffer = ""
                self.cursor_pos = 0
                self.input_queue.put(text)
            self.needs_redraw = True

        elif ch == b"\x7f" or ch == b"\x08":
            if self.cursor_pos > 0:
                self.input_buffer = (
                    self.input_buffer[:self.cursor_pos - 1]
                    + self.input_buffer[self.cursor_pos:]
                )
                self.cursor_pos -= 1
                self.needs_redraw = True

        elif ch == b"\x03":
            self.running = False

        elif ch == b"\x0c":
            self.needs_redraw = True

        elif ch == b"\x1b":
            self._handle_escape()

        elif ch and ch[0:1].isascii() and ch[0] >= 32:
            char = ch.decode("utf-8", errors="replace")
            self.input_buffer = (
                self.input_buffer[:self.cursor_pos]
                + char
                + self.input_buffer[self.cursor_pos:]
            )
            self.cursor_pos += 1
            self.needs_redraw = True

    def _handle_escape(self):
        if os.name == "nt":
            return
        r, _, _ = select.select([sys.stdin], [], [], 0.05)
        if not r:
            return
        ch2 = os.read(_fd, 1)
        if ch2 != b"[":
            return
        seq = b""
        while True:
            r, _, _ = select.select([sys.stdin], [], [], 0.05)
            if not r:
                break
            b = os.read(_fd, 1)
            seq += b
            if b and b[0] >= 0x40:
                break
        if seq == b"5~":
            visible = self.height - 4
            self.scroll_offset = min(
                self.scroll_offset + visible,
                max(0, len(self.transcript_lines) - visible),
            )
            self.needs_redraw = True
        elif seq == b"6~":
            visible = self.height - 4
            self.scroll_offset = max(0, self.scroll_offset - visible)
            self.needs_redraw = True

    def run(self):
        enable_ansi()
        set_cbreak()
        atexit.register(_cleanup)
        enter_alt_screen()

        if os.name != "nt":
            def on_resize(signum, frame):
                self.needs_redraw = True
            signal.signal(signal.SIGWINCH, on_resize)

        try:
            self.full_redraw()
            while self.running:
                drained = False
                while True:
                    try:
                        event = self.event_queue.get_nowait()
                        self.handle_event(event)
                        if event.get("type") == "assistant_text_final":
                            self.finalize_streaming_text()
                        drained = True
                    except Exception:
                        break

                if os.name == "nt":
                    import msvcrt
                    if msvcrt.kbhit():
                        ch = msvcrt.getch()
                        self.handle_key(ch)
                else:
                    r, _, _ = select.select([sys.stdin], [], [], 0.02)
                    if r:
                        ch = os.read(_fd, 1)
                        self.handle_key(ch)

                if self.needs_redraw:
                    self.full_redraw()

        finally:
            restore_terminal()


def _summarize_args(args, max_len=60):
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 30:
            s = s[:27] + "..."
        parts.append(f'{k}="{s}"')
    result = ", ".join(parts)
    if len(result) > max_len:
        result = result[:max_len - 3] + "..."
    return result
