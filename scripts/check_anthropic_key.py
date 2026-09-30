"""Standalone Anthropic auth check. Independent of the bot's code.

Usage:
    python scripts/check_anthropic_key.py              # uses ANTHROPIC_API_KEY from the environment
    railway run python scripts/check_anthropic_key.py  # uses the exact value stored in Railway

Never prints the key: only its shape and a SHA-256 fingerprint, so two copies can be compared.
"""
import hashlib
import json
import os
import platform
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://api.anthropic.com/v1/models?limit=1"

raw = os.environ.get("ANTHROPIC_API_KEY")
print(f"python {platform.python_version()} | endpoint {ENDPOINT}")
if raw is None:
    sys.exit("ANTHROPIC_API_KEY is not set in this environment")

key = raw.strip().strip("'\"").strip()
odd = [f"U+{ord(c):04X}" for c in raw if ord(c) > 126 or ord(c) < 33]
print(f"length raw/clean: {len(raw)}/{len(key)} | prefix ok: {key.startswith('sk-ant-api03-')} | "
      f"ends AA: {key.endswith('AA')} | odd/invisible chars: {odd or 'none'}")
print(f"sha256 fingerprint: {hashlib.sha256(key.encode()).hexdigest()[:16]}")
for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_WORKSPACE_ID",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    if os.environ.get(var) or os.environ.get(var.lower()):
        print(f"note: {var} is set and may affect requests")

req = urllib.request.Request(ENDPOINT, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        print(f"RESULT: {r.status} OK - Anthropic accepts this key")
except urllib.error.HTTPError as e:
    body = e.read().decode(errors="replace")
    try:
        body = json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        pass
    print(f"RESULT: {e.code} - {body} | request-id: {e.headers.get('request-id')}")
