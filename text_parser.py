"""Turns a WhatsApp text into structured JSON using Claude."""
import json
import os
import re

import anthropic

# Keys not scoped to a workspace need the workspace ID sent with every request.
_ws = os.getenv("ANTHROPIC_WORKSPACE_ID")
_client = anthropic.Anthropic(default_headers={"anthropic-workspace-id": _ws} if _ws else None)
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

SYSTEM = """You turn short WhatsApp texts from a small-business owner into JSON for their Wave accounting bot.
Return ONLY a JSON object, no prose, no code fences.

Schema:
{
  "action": "invoice" | "estimate" | "new_client" | "new_product" | "help" | "unknown",
  "client": {"name": str, "is_new": bool, "email": str|null, "phone": str|null,
             "first_name": str|null, "last_name": str|null} | null,
  "items": [{"product": str, "is_new": bool, "quantity": number,
             "unit_price": number|null, "description": str|null}],
  "new_product": {"name": str, "unit_price": number, "description": str|null} | null,
  "memo": str|null
}

Rules:
- Use the EXACT spelling from the known client/product lists when the text clearly refers to one
  (handle typos, nicknames, partial names).
- Set is_new=true ONLY if the user explicitly says it's new (e.g. "new client", "new product", "new item").
  Otherwise is_new=false even if you can't find a match.
- quantity defaults to 1. unit_price is null unless the user states a price (then use it, it overrides the list price).
- "hours", "hrs", "x", "qty" all indicate quantity.
- If a CURRENT DRAFT is given and the text is a change request ("make it 3", "add a hedge trim",
  "change client to Bob"), return the FULL updated draft with the change applied, keeping the same action.
- "new client ..." on its own (no items) -> action "new_client".
- "new product/service ..." on its own -> action "new_product".
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
