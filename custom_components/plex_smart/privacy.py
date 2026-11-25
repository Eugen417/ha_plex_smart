"""Privacy helpers for redacting tokens, IPs, Plex URLs, and sensitive metadata from logs."""

from __future__ import annotations

import re
from typing import Any

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(X-Plex-Token\s*[:=]\s*)([^\s\"'&,;]+)"), r"\1***"),
    (re.compile(r"(?i)(\b(?:token|access_token|auth|key)=)([^&\s\"']+)"), r"\1***"),
    (re.compile(r"\btransient-[0-9a-fA-F-]+\b"), "transient-***"),
    (re.compile(r"(?i)(machineIdentifier=)([^&\s\"']+)"), r"\1***"),
    (re.compile(r"https?://[^\s/?\"']+"), "https://***"),
    (re.compile(r"[\w.-]+\.plex\.direct(?::\d+)?", re.IGNORECASE), "***.plex.direct"),
    (re.compile(r"/library/metadata/\d+"), "/library/metadata/***"),
    (re.compile(r"/playQueues/\d+"), "/playQueues/***"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "***.***.***.***"),
    (re.compile(r"\b(?:\d{1,3}-){3}\d{1,3}\b"), "***-***-***-***"),
)


def redact_sensitive_text(value: Any) -> str:
    """Strip tokens, IPs, hostnames, machine IDs, and metadata paths from text/exceptions."""
    text = str(value or "")
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text
