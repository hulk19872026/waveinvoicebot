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
             "first_name": str|null, "last_name": str|null, "address": ADDRESS|null} | null,
  "items": [{"product": str, "is_new": bool, "quantity": number,
             "unit_price": number|null, "description": str|null}],
  "new_product": {"name": str, "unit_price": number, "description": str|null} | null,
  "memo": str|null,
  "client_changes": {"name": str|null, "email": str|null, "phone": str|null, "address": ADDRESS|null} | null,
  "document": {"kind": "estimate"|"invoice"|null, "number": str|null, "convert": bool, "send": bool,
               "send_to": str|null, "duplicate": bool, "duplicate_for": str|null,
               "item_changes": [{"action": "change"|"add"|"remove", "product": str|null, "is_new": bool,
                                 "unit_price": number|null, "quantity": number|null,
                                 "description": str|null}] | null} | null
}
ADDRESS = {"line1": str, "line2": str|null, "city": str|null, "state": str|null, "zip": str|null,
           "country": str|null}  (state as the 2-letter code, e.g. "NY"; country as a 2-letter code, default "US")

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
- "new client ..." on its own (no items) -> action "new_client". Include the address if one is given.
- Changing an existing client's details ("change Brittany's email to b@x.com", "update Joe Smith phone 555-1234",
  "rename client Bob to Robert Jones", "change address to 12 Main St, Brooklyn NY 11201") -> action "update_client": "client" is the existing client,
  "client_changes" holds only the fields being changed (others null). If no client is named
  ("change email to b@x.com"), still use action "update_client" with "client": null.
- "new product/service ..." on its own -> action "new_product".
- Sending or converting an EXISTING estimate/invoice ("email estimate 478 to the client", "turn 478 into an
  invoice and send it", "convert the last estimate") -> action "document". "number" only if given
  (digits only); "convert" true to turn an estimate into an invoice; "send" true to email it.
- Changing prices/quantities/descriptions on an EXISTING estimate/invoice ("change the door strike price on
  invoice 12 to 500", "make labor 4 hours on estimate 478", "lower the price to 400 on the last invoice") ->
  action "document" with "item_changes" (one entry per item; only the fields being changed). Adding a line
  ("add 4 speaker cable at 115 to estimate 490") is an entry with "action": "add" (quantity defaults to 1;
  "is_new" true only if the user says it's a new product); removing one is "action": "remove";
  otherwise "action": "change". Spoken prices like "one fifteen" mean 115, "thirty five ninety nine" 35.99. Only when there is
  no CURRENT DRAFT, or the text names an existing number - otherwise it's a change to the draft.
- Copying one ("duplicate invoice 12", "same as estimate 478 for Bob Jones") -> action "document" with
  "duplicate": true, and "duplicate_for" = the other client's name if one is given.
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
        # Thinking counts toward max_tokens; long multi-item texts used to run out mid-JSON
        max_tokens=16000,
        output_config={"effort": "low"},  # simple extraction: keeps replies inside Twilio's 15s window
        system=SYSTEM,
        messages=[{"role": "user", "content": ctx}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text")
    start, end = raw.find("{"), raw.rfind("}")
    try:
        return json.loads(raw[start:end + 1])
    except ValueError:
        print(f"parse failed (stop_reason={msg.stop_reason}): {raw[:500]!r}", flush=True)
        return {"action": "unknown", "too_long": msg.stop_reason == "max_tokens"}
