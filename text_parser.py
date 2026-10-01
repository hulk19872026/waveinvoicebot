"""Turns a WhatsApp text into structured JSON using Claude."""
import hashlib
import json
import os
import re

import anthropic


def _env(name):
    """Env var with stray whitespace/quotes (common copy-paste slips) removed."""
    return (os.getenv(name) or "").strip().strip("'\"").strip() or None


API_KEY = _env("ANTHROPIC_API_KEY")
# Keys not scoped to a workspace need the workspace ID sent with every request.
_ws = _env("ANTHROPIC_WORKSPACE_ID")
_client = anthropic.Anthropic(api_key=API_KEY,
                              default_headers={"anthropic-workspace-id": _ws} if _ws else None)

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")


def check_key():
    """One-line startup diagnosis of the Anthropic key (never prints the key itself)."""
    if not API_KEY:
        return "ANTHROPIC_API_KEY is missing"
    shape = (f"length {len(API_KEY)} (expected ~108), "
             f"starts with sk-ant-api03-: {API_KEY.startswith('sk-ant-api03-')}, "
             f"ends with AA: {API_KEY.endswith('AA')}, "
             f"has spaces inside: {any(ch.isspace() for ch in API_KEY)}, "
             f"sha256 {hashlib.sha256(API_KEY.encode()).hexdigest()[:12]}, "
             f"deployment {(os.getenv('RAILWAY_DEPLOYMENT_ID') or '?')[:8]}")
    try:
        _client.models.list(limit=1)
    except anthropic.APIStatusError as e:
        return f"Anthropic key REJECTED {e.status_code} ({shape})"
    except Exception as e:  # network etc.
        return f"Anthropic key check failed: {e} ({shape})"
    try:  # the same kind of call a text makes
        _client.messages.create(model=MODEL, max_tokens=1, messages=[{"role": "user", "content": "hi"}])
        return f"Anthropic key OK, test message with {MODEL} OK ({shape})"
    except anthropic.APIStatusError as e:
        return f"Anthropic key OK but test message with {MODEL} FAILED {e.status_code}: {e.message}"
    except Exception as e:  # network etc.
        return f"Anthropic test message failed: {e}"


SYSTEM = """You turn short WhatsApp texts from a small-business owner into JSON for their Wave accounting bot.
Return ONLY a JSON object, no prose, no code fences.

Schema:
{
  "action": "invoice" | "estimate" | "new_client" | "update_client" | "new_product" | "document" | "help" | "unknown",
  "client": {"name": str, "is_new": bool, "email": str|null, "phone": str|null,
             "first_name": str|null, "last_name": str|null} | null,
  "items": [{"product": str, "is_new": bool, "quantity": number,
             "unit_price": number|null, "description": str|null}],
  "new_product": {"name": str, "unit_price": number, "description": str|null} | null,
  "memo": str|null,
  "client_changes": {"name": str|null, "email": str|null, "phone": str|null} | null,
  "document": {"kind": "estimate"|"invoice"|null, "number": str|null, "convert": bool, "send": bool,
               "send_to": str|null} | null
}

Rules:
- Use the EXACT spelling from the known client/product lists when the text clearly refers to one
  (handle typos, nicknames, partial names).
- Set is_new=true ONLY if the user explicitly says it's new (e.g. "new client", "new product", "new item").
  Otherwise is_new=false even if you can't find a match.
- quantity defaults to 1. unit_price is null unless the user states a price (then use it, it overrides the list price).
- "hours", "hrs", "x", "qty" all indicate quantity.
- If a CURRENT DRAFT is given and the text is a change request ("make it 3", "add a hedge trim",
  "change client to Bob", "change the door strike description to ..."), return the FULL updated draft with the
  change applied, keeping the same action. Item descriptions in the draft are what will print on the document:
  keep them unless asked to change them; to change one, set that item's "description" to the new text
  ("add X to the description" means append X to the existing text).
- "new client ..." on its own (no items) -> action "new_client".
- Changing an existing client's details ("change Brittany's email to b@x.com", "update Joe Smith phone 555-1234",
  "rename client Bob to Robert Jones") -> action "update_client": "client" is the existing client,
  "client_changes" holds only the fields being changed (others null). If no client is named
  ("change email to b@x.com"), still use action "update_client" with "client": null.
- "new product/service ..." on its own -> action "new_product".
- Sending or converting an EXISTING estimate/invoice ("email estimate 478 to the client", "turn 478 into an
  invoice and send it", "convert the last estimate") -> action "document". "number" only if given
  (digits only); "convert" true to turn an estimate into an invoice; "send" true to email it.
"""


def parse(text, client_names, product_names, current_draft=None):
    ctx = (
        f"KNOWN CLIENTS: {json.dumps(client_names)}\n"
        f"KNOWN PRODUCTS: {json.dumps(product_names)}\n"
    )
    if current_draft:
        ctx += f"CURRENT DRAFT: {json.dumps(current_draft)}\n"
    ctx += f"\nTEXT: {text}"

    msg = _client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=SYSTEM,
        messages=[{"role": "user", "content": ctx}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text")
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"action": "unknown"}
