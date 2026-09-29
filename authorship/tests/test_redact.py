import pytest

from redact import redact, redact_obj

A40 = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"

POSITIVE = [
    ("anthropic", "key sk-ant-api03-%s end" % A40),
    ("openai", "OPENAI_API_KEY is sk-proj-%s" % A40),
    ("openai", "old style sk-%s" % A40[:32]),
    ("stripe", "sk_live_%s" % A40[:24]),
    ("stripe", "rk_test_%s" % A40[:24]),
    ("stripe_whsec", "whsec_%s" % A40[:32]),
    ("slack", "xoxb-123456789012-abcdefABCDEF"),
    ("slack_webhook", "https://hooks.slack.com/services/T000/B000/%s" % A40[:24]),
    ("github", "ghp_%s" % (A40[:36])),
    ("github", "github_pat_%s" % (A40 + "_x")),
    ("aws_access_key", "AKIAIOSFODNN7EXAMPLE"),
    ("aws_secret", "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    ("gcp_api_key", "AIzaSyD-%s" % A40[:31]),
    ("gcp_oauth", "ya29.a0AfH6SM%s" % A40[:20]),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"),
    ("pem", "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\nabc\n-----END RSA PRIVATE KEY-----"),
    ("bearer", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123"),
    ("assignment", "password=hunter2"),
    ("assignment", 'secret: "top secret value"'),
    ("assignment", "API_KEY='abc123'"),
]

NEGATIVE = [
    "sk-ant is a prefix we mention in docs",
    "task-management with sk-short",
    "use sk_live_ in the dashboard",
    "xoxb- tokens start like this",
    "the ghp_ prefix marks a GitHub token",
    "AKIA is the access key prefix",
    "aws_secret_access_key is read from the environment",
    "AIza keys are browser keys",
    "eyJ starts every base64 JSON",
    "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
    "the bearer of this note",
    "password reset flow for users",
    "the secret sauce is the sequence number",
    "whsec_ secrets sign Stripe webhooks",
    "https://hooks.slack.com/services/ is the webhook base",
    "ya29. is the OAuth access token prefix",
]


@pytest.mark.parametrize("kind,text", POSITIVE)
def test_positive(kind, text):
    out = redact(text)
    assert "[REDACTED:%s]" % kind in out, out


@pytest.mark.parametrize("text", NEGATIVE)
def test_negative(text):
    assert redact(text) == text


def test_every_pattern_has_a_positive_and_a_negative():
    from redact import _PATTERNS

    kinds = {k for k, _ in POSITIVE}
    assert {name for name, _ in _PATTERNS} | {"assignment"} <= kinds
    assert len(NEGATIVE) >= len(_PATTERNS)


def test_redact_obj_walks_structures():
    out = redact_obj({"a": ["password=x1"], "b": {"c": "AKIAIOSFODNN7EXAMPLE"}, "n": 3})
    assert out == {"a": ["password=[REDACTED:assignment]"], "b": {"c": "[REDACTED:aws_access_key]"}, "n": 3}
