"""Streaming normalizer for mixed logfmt / multi-line stack-trace logs.

The collector uploads logs in fixed-size byte chunks, so a single logical
record may be split across two chunks, and indented stack-trace lines must
be merged back into the record they belong to instead of being mistaken
for new records.

Usage::

    norm = StreamNormalizer()
    for chunk in chunks:
        for record in norm.feed(chunk):
            handle(record)
    for record in norm.close():
        handle(record)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Union

# ---------------------------------------------------------------------------
# Timestamp / level handling
# ---------------------------------------------------------------------------

TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:[.,]\d{1,6})?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)\s+"
)

LEVEL_ALIASES = {
    "trace": "TRACE",
    "debug": "DEBUG",
    "info": "INFO",
    "information": "INFO",
    "notice": "NOTICE",
    "warn": "WARN",
    "warning": "WARN",
    "err": "ERROR",
    "error": "ERROR",
    "crit": "CRITICAL",
    "critical": "CRITICAL",
    "fatal": "FATAL",
    "panic": "FATAL",
    "emerg": "EMERGENCY",
    "emergency": "EMERGENCY",
}

UNKNOWN_LEVEL = "UNKNOWN"

LEVEL_KEYS = ("level", "lvl", "severity")
TRACE_ID_KEYS = ("trace_id", "traceid", "trace-id", "trace")
MESSAGE_KEYS = ("msg", "message")


def normalize_level(raw: Optional[str]) -> str:
    """Map a raw level string to a canonical upper-case level.

    Unknown levels return ``UNKNOWN``; the raw value is preserved by the
    caller in the record fields.
    """
    if raw is None:
        return UNKNOWN_LEVEL
    return LEVEL_ALIASES.get(str(raw).strip().lower(), UNKNOWN_LEVEL)


# ---------------------------------------------------------------------------
# logfmt parsing
# ---------------------------------------------------------------------------

_ESCAPE_MAP = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _parse_quoted(text: str, pos: int) -> "tuple[str, int]":
    """Parse a double-quoted value starting just after the opening quote."""
    out: List[str] = []
    i = pos
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt in _ESCAPE_MAP:
                out.append(_ESCAPE_MAP[nxt])
                i += 2
                continue
            # Unknown escape: keep the backslash literally.
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    # Unterminated quote (e.g. truncated tail): keep what we have.
    return "".join(out), i


def parse_logfmt(text: str) -> "Dict[str, Union[str, None, List[Optional[str]]]]":
    """Parse a logfmt fragment into an ordered dict.

    - Supports ``key=value``, ``key="quoted value"`` and bare ``key`` flags.
    - Escaped quotes (``\\"``) and backslashes inside quoted values are
      unescaped.
    - Duplicate keys are preserved: the value becomes a list in first-seen
      order.
    """
    fields: "Dict[str, Union[str, None, List[Optional[str]]]]" = {}
    i = 0
    n = len(text)
    while i < n:
        while i < n and text[i] == " ":
            i += 1
        if i >= n:
            break
        start = i
        while i < n and text[i] not in " =":
            i += 1
        key = text[start:i]
        while i < n and text[i] == " ":
            i += 1
        value: Optional[str] = None
        if i < n and text[i] == "=":
            i += 1
            if i < n and text[i] == '"':
                value, i = _parse_quoted(text, i + 1)
            else:
                start = i
                while i < n and text[i] != " ":
                    i += 1
                value = text[start:i]
        if not key:
            continue
        if key in fields:
            existing = fields[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                fields[key] = [existing, value]
        else:
            fields[key] = value
    return fields


def looks_like_logfmt(text: str) -> bool:
    """Heuristic: does this line start a new (timestamp-less) logfmt record?"""
    if not text or text[0] in " \t":
        return False
    first = text.split(" ", 1)[0]
    if "=" not in first:
        return False
    key = first.split("=", 1)[0]
    return bool(key) and re.fullmatch(r"[A-Za-z_][\w.\-]*", key) is not None


# ---------------------------------------------------------------------------
# Record model
# ---------------------------------------------------------------------------


@dataclass
class LogRecord:
    """One normalized log record."""

    timestamp: Optional[str] = None
    level: str = UNKNOWN_LEVEL
    trace_id: Optional[str] = None
    msg: str = ""
    fields: Dict[str, object] = field(default_factory=dict)
    stack: List[str] = field(default_factory=list)
    origin: str = "record"  # "record" | "pending"
    truncated: bool = False
    stack_truncated: bool = False

    def to_dict(self) -> Dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "trace_id": self.trace_id,
            "msg": self.msg,
            "fields": dict(self.fields),
            "stack": list(self.stack),
            "origin": self.origin,
            "truncated": self.truncated,
            "stack_truncated": self.stack_truncated,
        }

    def to_logfmt(self) -> str:
        """Serialize back to a canonical single-line logfmt string."""
        parts: List[str] = []
        if self.timestamp:
            parts.append(self.timestamp)
        parts.append(f"level={self.level.lower()}")
        if self.trace_id:
            parts.append(f"trace_id={self.trace_id}")
        parts.append(f'msg="{_quote(self.msg)}"')
        for key, value in self.fields.items():
            if isinstance(value, list):
                for item in value:
                    parts.append(f"{key}={_format_value(item)}")
            else:
                parts.append(f"{key}={_format_value(value)}")
        if self.stack:
            parts.append(f'stack="{_quote(chr(10).join(self.stack))}"')
        return " ".join(parts)


def _quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_value(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if text == "" or any(c in text for c in ' ="\\'):
        return f'"{_quote(text)}"'
    return text


# ---------------------------------------------------------------------------
# Streaming normalizer
# ---------------------------------------------------------------------------


class StreamNormalizer:
    """Incrementally normalize a mixed logfmt / stack-trace byte stream.

    Parameters
    ----------
    max_stack_lines:
        Optional cap on continuation lines merged into one record. Extra
        lines are dropped and the record is flagged ``stack_truncated``.
    """

    def __init__(self, max_stack_lines: Optional[int] = None) -> None:
        self.max_stack_lines = max_stack_lines
        self._carry = ""  # incomplete line bytes across chunk boundaries
        self._current: Optional[LogRecord] = None
        self._pending: List[str] = []  # ownerless continuation lines
        self._closed = False

    # -- public API --------------------------------------------------------

    def feed(self, data: Union[str, bytes]) -> List[LogRecord]:
        """Feed a chunk; return records completed by this chunk."""
        if self._closed:
            raise ValueError("cannot feed a closed normalizer")
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        text = self._carry + data
        lines = text.split("\n")
        self._carry = lines.pop()  # last piece may be incomplete
        out: List[LogRecord] = []
        for line in lines:
            out.extend(self._handle_line(line.rstrip("\r"), truncated=False))
        return out

    def close(self) -> List[LogRecord]:
        """Flush the incomplete tail, pending buffer and current record."""
        self._closed = True
        out: List[LogRecord] = []
        if self._carry:
            tail, self._carry = self._carry, ""
            out.extend(self._handle_line(tail.rstrip("\r"), truncated=True))
        if self._pending:
            out.append(self._make_pending_record())
        if self._current is not None:
            out.append(self._current)
            self._current = None
        return out

    # -- internals ----------------------------------------------------------

    def _handle_line(self, line: str, truncated: bool) -> List[LogRecord]:
        if line == "":
            return []
        if self._is_record_start(line):
            return self._start_record(line, truncated)
        self._append_continuation(line, truncated)
        return []

    @staticmethod
    def _is_record_start(line: str) -> bool:
        if TIMESTAMP_RE.match(line):
            return True
        return looks_like_logfmt(line)

    def _start_record(self, line: str, truncated: bool) -> List[LogRecord]:
        out: List[LogRecord] = []
        # A new record header proves the pending lines can never be
        # attributed to a future record, so release them first.
        if self._pending:
            out.append(self._make_pending_record())
        if self._current is not None:
            out.append(self._current)
        self._current = self._build_record(line, truncated)
        return out

    def _append_continuation(self, line: str, truncated: bool) -> None:
        if self._current is None:
            # No owner yet: hold in the pending buffer, never drop.
            self._pending.append(line)
            return
        record = self._current
        if self.max_stack_lines is not None and len(record.stack) >= self.max_stack_lines:
            record.stack_truncated = True
        else:
            record.stack.append(line)
        if truncated:
            record.truncated = True

    def _make_pending_record(self) -> LogRecord:
        lines, self._pending = self._pending, []
        return LogRecord(
            msg=lines[0] if lines else "",
            stack=lines[1:],
            origin="pending",
        )

    def _build_record(self, line: str, truncated: bool) -> LogRecord:
        rest = line
        timestamp: Optional[str] = None
        match = TIMESTAMP_RE.match(rest)
        if match:
            timestamp = match.group(1)
            rest = rest[match.end():]

        fields = parse_logfmt(rest)

        raw_level = _pop_first(fields, LEVEL_KEYS)
        level = normalize_level(raw_level if isinstance(raw_level, str) else None)
        if raw_level is not None and level == UNKNOWN_LEVEL:
            fields["level_raw"] = raw_level  # preserve unrecognized level

        trace_id = _pop_first(fields, TRACE_ID_KEYS)
        if isinstance(trace_id, list):
            trace_id = trace_id[0] if trace_id else None

        msg = _pop_first(fields, MESSAGE_KEYS)
        if isinstance(msg, list):
            msg = " ".join(str(part) for part in msg if part is not None)

        return LogRecord(
            timestamp=timestamp,
            level=level,
            trace_id=str(trace_id) if trace_id is not None else None,
            msg=str(msg) if msg is not None else "",
            fields=fields,
            truncated=truncated,
        )


def _pop_first(fields: Dict[str, object], keys: "tuple[str, ...]") -> object:
    for key in keys:
        if key in fields:
            return fields.pop(key)
    return None
