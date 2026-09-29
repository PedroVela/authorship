"""Secret redaction, applied before anything is hashed or stored.

Standard library only: this module runs on the hook path.
"""
import re

# Order matters: more specific shapes first, so an Anthropic key is not
# reported as a generic OpenAI key.
_PATTERNS = [
    ("pem", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
    ("anthropic", r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
    ("openai", r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"),
    ("stripe", r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("stripe_whsec", r"\bwhsec_[A-Za-z0-9]{24,}"),
    ("slack", r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    ("slack_webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]{20,}"),
    ("github", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    ("aws_access_key", r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b"),
    ("aws_secret", r"(?i:\baws_secret_access_key\b)\s*[=:]\s*[\"']?[A-Za-z0-9/+=]{40}[\"']?"),
    ("gcp_api_key", r"\bAIza[0-9A-Za-z_\-]{35}"),
    ("gcp_oauth", r"\bya29\.[0-9A-Za-z_\-]{20,}"),
    ("jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    ("bearer", r"(?<=\b[Bb]earer )[A-Za-z0-9_\-\.=~+/]{20,}"),
]

# Assignments keep the key and lose the value: `password=hunter2` becomes
# `password=[REDACTED:assignment]`. Requires `=` or `:` so prose such as
# "password reset flow" is left alone.
_ASSIGN = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|secret|client_secret|api[_-]?key|access[_-]?token|auth[_-]?token|private[_-]?key)\b"
    r"[\"']?\s*[:=]\s*)(\"[^\"\n]{1,}\"|'[^'\n]{1,}'|[^\s,;\"'&]{1,})"
)

SECRET_RE = re.compile("|".join("(?P<%s>%s)" % (name, pat) for name, pat in _PATTERNS))


def _sub(m):
    return "[REDACTED:%s]" % m.lastgroup


def _sub_assign(m):
    value = m.group(2)
    if value.startswith("[REDACTED:"):
        return m.group(0)
    return m.group(1) + "[REDACTED:assignment]"


def redact(text):
    """Return text with secrets replaced by [REDACTED:<kind>] markers."""
    if not text or not isinstance(text, str):
        return text
    text = SECRET_RE.sub(_sub, text)
    return _ASSIGN.sub(_sub_assign, text)


def redact_obj(obj):
    """Redact every string inside a JSON-like structure, keys included."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {redact(k) if isinstance(k, str) else k: redact_obj(v) for k, v in obj.items()}
    return obj
