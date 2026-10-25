"""Decorative startup banner for miniAgent.

Optional presentation layer: it holds no agent state, prints nothing on
import, and every failure degrades silently. Delete this file plus the
single `show_banner(...)` call in `agent.py` and nothing else changes.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from typing import IO, Callable, Iterator

ART = r"""
    _       ____   _____   _   _    _____
   / \    / ___| | ____| | \ | | |_   _|
  / _ \   | |  _  |  _|   |  \| |   | |
 / ___ \  | |_| | | |___  | |\  |   | |
/_/   \_\  \____| |_____| |_| \_|   |_|
""".strip("\n")

ART_LINES: tuple[str, ...] = tuple(ART.splitlines())
ART_WIDTH = max(len(line) for line in ART_LINES)

GRADIENT: tuple[tuple[int, int, int], ...] = (
    (34, 211, 238),  # cyan
    (129, 140, 248),  # indigo
    (192, 132, 252),  # violet
)

RESET = "\x1b[0m"
DIM = "\x1b[2m"

FRAME_PAD = 2
FRAME_BORDER = 2
MIN_FRAME_WIDTH = ART_WIDTH + FRAME_PAD * 2 + FRAME_BORDER

TOP_LEFT, TOP_RIGHT = "\u256d", "\u256e"
BOTTOM_LEFT, BOTTOM_RIGHT = "\u2570", "\u256f"
HORIZONTAL, VERTICAL = "\u2500", "\u2502"
ANIMATION_BUDGET = 0.30
MIN_DELAY = 0.0005

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def render_banner(*, color: bool, frame: bool = True) -> list[str]:
    """Build the banner lines. Pure: no IO, no environment access."""
    rows = len(ART_LINES)
    if color:
        painted: list[str] = []
        for y, line in enumerate(ART_LINES):
            cells: list[str] = []
            for x, ch in enumerate(line):
                inked = 0.7 * (x / max(ART_WIDTH - 1, 1)) + 0.3 * (y / max(rows - 1, 1))
                cells.append(_fg(_sample_gradient(inked)))
                cells.append(ch)
            painted.append("".join(cells) + RESET)
    else:
        painted = list(ART_LINES)

    if not frame:
        return painted

    inner = ART_WIDTH + FRAME_PAD * 2
    rule = HORIZONTAL * inner
    if color:
        top = f"{DIM}{TOP_LEFT}{rule}{TOP_RIGHT}{RESET}"
        bottom = f"{DIM}{BOTTOM_LEFT}{rule}{BOTTOM_RIGHT}{RESET}"
        left = right = f"{DIM}{VERTICAL}{RESET}"
    else:
        top = bottom = f"+{'-' * inner}+"
        left = right = "|"

    lines = [top]
    lead = " " * FRAME_PAD
    for y, raw in enumerate(ART_LINES):
        tail = " " * (ART_WIDTH - len(raw) + FRAME_PAD)
        lines.append(f"{left}{lead}{painted[y]}{tail}{right}")
    lines.append(bottom)
    return lines


def show_banner(
    *,
    enabled: bool = True,
    stream: IO[str] | None = None,
    animate: bool = True,
    color: bool | None = None,
    width: int | None = None,
) -> bool:
    """Draw the banner once on stdout. Returns True when something was drawn."""
    if not enabled:
        return False

    out = stream if stream is not None else sys.stdout
    try:
        if not _is_tty(out):
            return False
        columns = width if width is not None else shutil.get_terminal_size().columns
        if columns < ART_WIDTH:
            return False
        frame = columns >= MIN_FRAME_WIDTH

        with _vt_mode(out) as vt_ok:
            if color is None:
                use_color = vt_ok and _color_allowed()
            else:
                use_color = color
            try:
                lines = render_banner(color=use_color, frame=frame)
                delay = _delay_for(lines) if animate and use_color else 0.0
                if delay > 0:
                    _type_out(out, lines, delay)
                else:
                    out.write("\n".join(lines) + "\n")
                    out.flush()
            finally:
                # 必须在 VT 模式恢复之前复位颜色
                if use_color:
                    out.write(RESET)
                    out.flush()
        return True
    except Exception:  # 装饰层不得影响启动
        return False


def _sample_gradient(t: float) -> tuple[int, int, int]:
    t = min(max(t, 0.0), 1.0)
    span = len(GRADIENT) - 1
    pos = t * span
    index = min(int(pos), span - 1)
    frac = pos - index
    start, end = GRADIENT[index], GRADIENT[index + 1]
    return (
        round(start[0] + (end[0] - start[0]) * frac),
        round(start[1] + (end[1] - start[1]) * frac),
        round(start[2] + (end[2] - start[2]) * frac),
    )


def _fg(rgb: tuple[int, int, int]) -> str:
    return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def _tokens(line: str) -> Iterator[tuple[str, bool]]:
    """Split a line into (text, is_escape) pieces so animation paces glyphs only."""
    pos = 0
    for match in _ANSI_RE.finditer(line):
        for ch in line[pos : match.start()]:
            yield ch, False
        yield match.group(), True
        pos = match.end()
    for ch in line[pos:]:
        yield ch, False


def _delay_for(lines: list[str]) -> float:
    visible = sum(1 for line in lines for _, is_escape in _tokens(line) if not is_escape)
    if visible <= 0:
        return 0.0
    delay = ANIMATION_BUDGET / visible
    return delay if delay >= MIN_DELAY else 0.0


def _type_out(out: IO[str], lines: list[str], delay: float) -> None:
    for line in lines:
        for text, is_escape in _tokens(line):
            out.write(text)
            if not is_escape:
                out.flush()
                time.sleep(delay)
        out.write("\n")
    out.flush()


def _is_tty(stream: IO[str]) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _color_allowed() -> bool:
    if "NO_COLOR" in os.environ:
        return False
    return os.environ.get("TERM", "").lower() != "dumb"


@contextmanager
def _vt_mode(stream: IO[str]) -> Iterator[bool]:
    """Yield whether ANSI sequences are usable; restore console mode afterwards."""
    supported, restore = True, None
    if os.name == "nt":
        supported, restore = _ensure_vt()
    try:
        yield supported
    finally:
        if restore is not None:
            try:
                restore()
            except Exception:
                pass


def _ensure_vt() -> tuple[bool, Callable[[], None] | None]:
    try:
        import ctypes
    except ImportError:
        return False, None

    kernel32 = getattr(ctypes, "windll", None)
    if kernel32 is None:
        return False, None
    kernel32 = kernel32.kernel32

    enable_vt = 0x0004
    try:
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return True, None  # 不是控制台（已重定向），无需启用
        original = mode.value
        if original & enable_vt:
            return True, None  # Windows Terminal 默认已开
        if not kernel32.SetConsoleMode(handle, original | enable_vt):
            return False, None
        return True, lambda: kernel32.SetConsoleMode(handle, original)
    except (OSError, AttributeError, ValueError):
        return False, None
