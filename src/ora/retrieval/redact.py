"""Conservative receipt redaction; original provider bytes are never persisted here."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTION_VERSION = "allowlist-and-redact-v1"
REDACTED = "[REDACTED]"
SECRET_NAME = re.compile(
    r"(?:^|[_-])(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"bearer|password|passwd|secret|session|cookie|client[_-]?secret|signature|sig|key|token)(?:$|[_-])",
    re.IGNORECASE,
)
_SECRET_SUFFIXES = (
    "apikey", "accesstoken", "refreshtoken", "clientsecret", "authorization",
    "password", "bearertoken", "sessiontoken", "privatekey", "signature",
)
INLINE_SECRET = re.compile(
    r"(?i)\b(bearer\s+)[^\s,;]+|\b(api[_-]?key|access[_-]?token|token|secret|password)"
    r"\s*([=:])\s*([^\s&;,]+)"
)


def sensitive_name(value: str) -> bool:
    """Catch separator and common camel-case credential field names."""
    compact = re.sub(r"[^a-z0-9]", "", value.casefold())
    return bool(SECRET_NAME.search(value)) or compact.endswith(_SECRET_SUFFIXES)


def redact_url(value: str) -> tuple[str, bool]:
    """Remove URL credentials, fragments, and secret-looking query values."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return REDACTED, True
    if not parsed.scheme or not parsed.netloc:
        return REDACTED, True
    changed = bool(parsed.fragment or parsed.username or parsed.password)
    host = parsed.hostname or ""
    if not host:
        return REDACTED, True
    if ":" in host:
        host = f"[{host}]"
    try:
        if parsed.port is not None:
            host += f":{parsed.port}"
    except ValueError:
        return REDACTED, True
    pairs = []
    for name, item in parse_qsl(parsed.query, keep_blank_values=True):
        if sensitive_name(name):
            pairs.append((name, REDACTED))
            changed = True
        else:
            clean, was_changed = redact_text(item)
            pairs.append((name, clean))
            changed |= was_changed
    result = urlunsplit((parsed.scheme, host, parsed.path, urlencode(pairs), ""))
    return result, changed


def redact_text(value: str) -> tuple[str, bool]:
    """Remove common inline credentials without altering ordinary research wording."""
    if value.startswith(("https://", "http://")) and not any(c.isspace() for c in value):
        return redact_url(value)

    def replacement(match: re.Match[str]) -> str:
        if match.group(1):
            return match.group(1) + REDACTED
        return match.group(2) + match.group(3) + REDACTED

    clean, count = INLINE_SECRET.subn(replacement, value)
    return clean, count > 0


def redact(value: object, path: str = "$") -> tuple[object, list[str]]:
    """Return a sanitized JSON-like value and the paths changed by the transformation."""
    if isinstance(value, dict):
        output: dict[str, object] = {}
        paths: list[str] = []
        for key, item in value.items():
            name = str(key)
            child = f"{path}.{name}"
            if sensitive_name(name):
                output[name] = REDACTED
                paths.append(child)
            else:
                output[name], changed = redact(item, child)
                paths.extend(changed)
        return output, paths
    if isinstance(value, list):
        output = []
        paths = []
        for index, item in enumerate(value):
            clean, changed = redact(item, f"{path}[{index}]")
            output.append(clean)
            paths.extend(changed)
        return output, paths
    if isinstance(value, str):
        clean, changed = redact_text(value)
        return clean, [path] if changed else []
    return value, []
