"""Progress reporting for long scans.

A full shop codebase takes minutes to parse, and the report is only printed at
the very end, so without this the tool looks hung. Everything here goes to
stderr so that redirecting stdout to a file still captures a clean report.

Behaviour adapts to the destination: an interactive terminal gets a single line
rewritten in place, while a log file or pipe gets one line every few seconds so
the output stays readable after the fact.
"""

from __future__ import annotations

import sys
import time
from typing import Optional, TextIO

_LABEL_WIDTH = 58


class Progress:
    """Stage announcements and a throttled counter."""

    def __init__(
        self,
        stream: Optional[TextIO] = None,
        enabled: bool = True,
        min_interval: float = 2.0,
    ) -> None:
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled
        self.min_interval = min_interval
        self._tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._last_emit = 0.0
        self._line_open = False
        self._start = time.monotonic()

    # -- public API --------------------------------------------------------

    def stage(self, message: str) -> None:
        """Announce a phase of the run. Always emitted."""
        if not self.enabled:
            return
        self._close_line()
        self._write(f"[{self._elapsed()}] {message}\n")

    def step(self, current: int, total: int, label: str = "") -> None:
        """Report progress through ``total`` items. Throttled."""
        if not self.enabled:
            return
        final = current >= total
        now = time.monotonic()
        interval = 0.1 if self._tty else self.min_interval
        if not final and now - self._last_emit < interval:
            return
        self._last_emit = now
        text = f"[{self._elapsed()}] {current}/{total}"
        if label:
            text += f"  {_shorten(label)}"
        if self._tty and not final:
            self._write("\r" + text)
            self._line_open = True
        else:
            self._close_line()
            self._write(text + "\n")

    def warn(self, message: str) -> None:
        if not self.enabled:
            return
        self._close_line()
        self._write(f"[{self._elapsed()}] ! {message}\n")

    def done(self, message: str = "") -> None:
        if not self.enabled:
            return
        self._close_line()
        if message:
            self._write(f"[{self._elapsed()}] {message}\n")

    # -- internals ---------------------------------------------------------

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            # A closed or broken stderr must never take the analysis down.
            self.enabled = False

    def _close_line(self) -> None:
        if self._line_open:
            self._write("\n")
            self._line_open = False

    def _elapsed(self) -> str:
        seconds = int(time.monotonic() - self._start)
        return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _shorten(label: str) -> str:
    """Keep the tail of a path, which is the part that identifies it."""
    if len(label) <= _LABEL_WIDTH:
        return label
    return "..." + label[-(_LABEL_WIDTH - 3):]


class NullProgress(Progress):
    """A progress reporter that never emits anything."""

    def __init__(self) -> None:
        super().__init__(stream=None, enabled=False)


def resolve(progress: Optional[Progress]) -> Progress:
    """Callers may pass ``None``; this saves them a guard at every call site."""
    return progress if progress is not None else NullProgress()
