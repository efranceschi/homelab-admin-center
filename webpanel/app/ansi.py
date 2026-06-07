"""Convert ANSI SGR escape sequences into safe HTML spans.

Ansible colours its output (warnings, recap, diff, errors) with ANSI escape
codes.  The web panel shows job logs in a dark ``<pre>``, so we translate the
SGR colour codes into ``<span>`` elements with inline styles and drop every
other control sequence.  Input is HTML-escaped first, so the result is safe to
mark ``|safe`` in templates and to send over the SSE stream.
"""
from __future__ import annotations

import html
import re

# Any escape sequence: CSI (``ESC [ ... <final>``), OSC, or a 2-char escape.
_ESC_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"          # CSI  (SGR is the subset ending in 'm')
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC  (e.g. window title)
    r"|\x1b[@-Z\\-_]"                       # two-character escapes
)

# 16-colour palette tuned for the dark log view (GitHub-like tones).
_FG = {
    30: "#6e7681", 31: "#f85149", 32: "#3fb950", 33: "#d29922",
    34: "#58a6ff", 35: "#bc8cff", 36: "#39c5cf", 37: "#d1d5da",
    90: "#8b949e", 91: "#ff7b72", 92: "#56d364", 93: "#e3b341",
    94: "#79c0ff", 95: "#d2a8ff", 96: "#56d4dd", 97: "#f0f6fc",
}
_BG = {
    40: "#0d1117", 41: "#f85149", 42: "#3fb950", 43: "#d29922",
    44: "#58a6ff", 45: "#bc8cff", 46: "#39c5cf", 47: "#d1d5da",
    100: "#161b22", 101: "#ff7b72", 102: "#56d364", 103: "#e3b341",
    104: "#79c0ff", 105: "#d2a8ff", 106: "#56d4dd", 107: "#f0f6fc",
}


class _State:
    """Tracks the active SGR attributes while scanning the text."""

    __slots__ = ("fg", "bg", "bold", "dim", "italic", "underline")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.fg = None
        self.bg = None
        self.bold = False
        self.dim = False
        self.italic = False
        self.underline = False

    def active(self) -> bool:
        return bool(
            self.fg or self.bg or self.bold
            or self.dim or self.italic or self.underline
        )

    def apply(self, codes: list[int]) -> None:
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                self.reset()
            elif c == 1:
                self.bold = True
            elif c == 2:
                self.dim = True
            elif c == 3:
                self.italic = True
            elif c == 4:
                self.underline = True
            elif c == 22:
                self.bold = self.dim = False
            elif c == 23:
                self.italic = False
            elif c == 24:
                self.underline = False
            elif c in _FG:
                self.fg = _FG[c]
            elif c in _BG:
                self.bg = _BG[c]
            elif c == 39:
                self.fg = None
            elif c == 49:
                self.bg = None
            elif c in (38, 48):
                # 256-colour / truecolour: consume params we don't render.
                if i + 1 < len(codes) and codes[i + 1] == 5:
                    i += 2
                elif i + 1 < len(codes) and codes[i + 1] == 2:
                    i += 4
            i += 1

    def span(self) -> str:
        styles = []
        fg = self.fg
        if self.bold and fg is None:
            fg = "#f0f6fc"  # bold without an explicit colour reads as bright white
        if fg:
            styles.append(f"color:{fg}")
        if self.bg:
            styles.append(f"background:{self.bg}")
        if self.bold:
            styles.append("font-weight:bold")
        if self.dim:
            styles.append("opacity:.7")
        if self.italic:
            styles.append("font-style:italic")
        if self.underline:
            styles.append("text-decoration:underline")
        return '<span style="' + ";".join(styles) + '">'


def strip_ansi(text: str) -> str:
    """Return ``text`` with every ANSI escape sequence removed (plain text).

    Used when parsing ansible output (e.g. the PLAY RECAP): colour codes from
    ``ANSIBLE_FORCE_COLOR`` would otherwise break field-matching regexes.
    """
    return _ESC_RE.sub("", text) if text else text


def ansi_to_html(text: str) -> str:
    """Render ANSI-coloured ``text`` as HTML-escaped markup with colour spans."""
    if not text:
        return ""
    state = _State()
    out: list[str] = []
    open_span = False
    pos = 0
    for m in _ESC_RE.finditer(text):
        chunk = text[pos:m.start()]
        if chunk:
            out.append(html.escape(chunk))
        pos = m.end()
        seq = m.group(0)
        # Only SGR sequences (``ESC [ ... m``) carry colour; drop the rest.
        if not (seq.startswith("\x1b[") and seq.endswith("m")):
            continue
        params = seq[2:-1]
        if params and any(ch not in "0123456789;" for ch in params):
            continue
        codes = [int(p) if p else 0 for p in params.split(";")] if params else [0]
        if open_span:
            out.append("</span>")
            open_span = False
        state.apply(codes)
        if state.active():
            out.append(state.span())
            open_span = True
    tail = text[pos:]
    if tail:
        out.append(html.escape(tail))
    if open_span:
        out.append("</span>")
    return "".join(out)
