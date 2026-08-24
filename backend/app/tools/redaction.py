from __future__ import annotations

import re


_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|pwd|secret|cookie|authorization)"
    r"(\s*[:=]\s*)([^\s,;|]+)"
)
_FLAG = re.compile(
    r"(?i)(--?(?:api[_-]?key|access[_-]?token|token|password|passwd|secret|cookie)\s+)([^\s;|]+)"
)
_BEARER = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_URI_CREDENTIALS = re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@")
_JSON_SECRET = re.compile(
    r'(?i)(["\'](?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|cookie|authorization)["\']\s*:\s*)'
    r'(["\'])(.*?)(\2)'
)
_TABLE_SECRET = re.compile(
    r"(?im)^(\s*[A-Za-z0-9_.-]*(?:API[_-]?KEY|TOKEN|PASSWORD|PASSWD|SECRET|COOKIE|AUTHORIZATION)[A-Za-z0-9_.-]*\s+)(\S.*)$"
)
_PRIVATE_KEY = re.compile(
    r"(?is)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)


def redact_secrets(value: str) -> str:
    """Best-effort local redaction for command, stdout, stderr and audit fields."""

    if not value:
        return value
    redacted = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}***REDACTED***", value)
    redacted = _FLAG.sub(lambda match: f"{match.group(1)}***REDACTED***", redacted)
    redacted = _BEARER.sub(lambda match: f"{match.group(1)}***REDACTED***", redacted)
    redacted = _URI_CREDENTIALS.sub(lambda match: f"{match.group(1)}***:***@", redacted)
    redacted = _JSON_SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}***REDACTED***{match.group(4)}", redacted)
    redacted = _TABLE_SECRET.sub(lambda match: f"{match.group(1)}***REDACTED***", redacted)
    redacted = _PRIVATE_KEY.sub("***PRIVATE KEY REDACTED***", redacted)
    return redacted
