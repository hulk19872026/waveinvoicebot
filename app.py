"""WhatsApp -> Wave invoice/estimate bot (Twilio webhook)."""
import os
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
from flask import Flask, abort, request
from rapidfuzz import fuzz, process, utils
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()

from text_parser import parse  # noqa: E402
from wave_api import Wave, WaveError  # noqa: E402

app = Flask(__name__)
wave = Wave(os.environ["WAVE_TOKEN"], os.getenv("WAVE_BUSINESS_ID") or None,
            os.getenv("WAVE_INCOME_ACCOUNT_ID") or None)
ALLOWED = {n.strip() for n in os.getenv("ALLOWED_NUMBERS", "").split(",") if n.strip()}
VALIDATOR = RequestValidator(os.getenv("TWILIO_AUTH_TOKEN", ""))
CUR = os.getenv("CURRENCY_SYMBOL", "$")
MATCH_CUTOFF = 85

PENDING = {}  # phone -> draft waiting for YES

HELP = (
    "🧾 *Wave Bot*\n"
    "• invoice Joe Smith 2 lawn mowing, 1 hedge trim\n"
    "• estimate for Sarah Lee: 10 hrs consulting at 80\n"
    "• invoice NEW client Mike Ross mike@x.com: 1 NEW product gutter clean at 120\n"
    "• new client Bob Jones, bob@x.com, 555-123-4567\n"
    "• new product Window Wash 60\n"
    "• YES to create · CANCEL to discard · REFRESH to reload lists\n"
    "• Send a change (\"make it 3 lawn mowings\") to edit the preview"
)


def money(v):
    return f"{CUR}{Decimal(str(v)):,.2f}"


def dec(v, default=None):
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError):
        return default


def best_match(name, records):
    names = [r["name"] for r in records]
    hit = process.extractOne(name, names, scorer=fuzz.WRatio, processor=utils.default_process, score_cutoff=MATCH_CUTOFF)
    if hit:
        return records[hit[2]], []
    sugg = [h[0] for h in process.extract(name, names, scorer=fuzz.WRatio, processor=utils.default_process, limit=3)]
    return None, sugg


# ---------------- resolving parsed text against Wave ----------------
def resolve_client(c):
    if not c or not c.get("name"):
        return None, "Which client is this for?"
    if c.get("is_new"):
        existing, _ = best_match(c["name"], wave.customers)
        warn = f"\n⚠️ Heads up: similar client already exists: {existing['name']}" if existing else ""
        return {"new": True, **c}, warn or None
    match, sugg = best_match(c["name"], wave.customers)
    if match:
        return {"new": False, "id": match["id"], "name": match["name"]}, None
    hint = f" Did you mean: {', '.join(sugg)}?" if sugg else ""
    return None, f"❓ I couldn't find client \"{c['name']}\".{hint}\nOr say \"new client {c['name']}\"."


def resolve_items(items):
    out, errors = [], []
    for it in items or []:
        qty = dec(it.get("quantity"), Decimal(1))
        price = dec(it.get("unit_price"))
        if it.get("is_new"):
            if price is None:
                errors.append(f"💲 What's the price for new product \"{it['product']}\"?")
                continue
            out.append({"new": True, "name": it["product"], "product_id": None, "quantity": qty,
                        "unit_price": price, "description": it.get("description"), "tax_ids": []})
            continue
        match, sugg = best_match(it["product"], wave.products)
        if not match:
            hint = f" Did you mean: {', '.join(sugg)}?" if sugg else ""
            errors.append(f"❓ Product \"{it['product']}\" not found.{hint} Or say \"new product {it['product']} at <price>\".")
            continue
        out.append({"new": False, "name": match["name"], "product_id": match["id"], "quantity": qty,
                    "unit_price": price if price is not None else dec(match["unitPrice"], Decimal(0)),
                    "description": it.get("description") or match.get("description"),
                    "tax_ids": [t["id"] for t in (match.get("defaultSalesTaxes") or [])]})
    if not out and not errors:
        errors.append("What items should go on it?")
    return out, errors


# ---------------- previews ----------------
def doc_preview(d):
    lines = [f"📄 *{d['kind'].upper()} PREVIEW*",
             f"Client: {d['client']['name']}{' 🆕' if d['client']['new'] else ''}", "──────────"]
    subtotal = Decimal(0)
    for it in d["items"]:
        line = it["quantity"] * it["unit_price"]
        subtotal += line
        tag = " 🆕" if it["new"] else ""
        tax = " +tax" if it["tax_ids"] else ""
        lines.append(f"{it['quantity'].normalize():f} × {it['name']}{tag} @ {money(it['unit_price'])} = {money(line)}{tax}")
    lines += ["──────────", f"*Subtotal: {money(subtotal)}*"]
    if d.get("memo"):
        lines.append(f"Memo: {d['memo']}")
    if d.get("warning"):
        lines.append(d["warning"])
    lines.append("\nReply *YES* to create in Wave, *CANCEL*, or text a change.")
    return "\n".join(lines)


def client_preview(c):
    parts = [f"👤 *NEW CLIENT PREVIEW*", f"Name: {c['name']}"]
    for k, label in (("email", "Email"), ("phone", "Phone")):
        if c.get(k):
            parts.append(f"{label}: {c[k]}")
    if c.get("warning"):
        parts.append(c["warning"])
    parts.append("\nReply *YES* to add to Wave or *CANCEL*.")
    return "\n".join(parts)


# ---------------- actions ----------------
def build_draft(parsed):
    action = parsed.get("action")

    if action in ("invoice", "estimate"):
        client, cmsg = resolve_client(parsed.get("client"))
        items, ierrs = resolve_items(parsed.get("items"))
        errs = ([cmsg] if cmsg and client is None else []) + ierrs
        if errs:
            return None, "\n".join(errs)
        draft = {"kind": action, "client": client, "items": items,
                 "memo": parsed.get("memo"), "warning": cmsg, "parsed": parsed}
        return draft, doc_preview(draft)

    if action == "new_client":
        c = parsed.get("client") or {}
        if not c.get("name"):
            return None, "What's the new client's name?"
        existing, _ = best_match(c["name"], wave.customers)
        c["warning"] = f"⚠️ Similar client already exists: {existing['name']}" if existing else None
        return {"kind": "client", "client": c, "parsed": parsed}, client_preview(c)

    if action == "new_product":
        p = parsed.get("new_product") or {}
        if not p.get("name") or p.get("unit_price") is None:
            return None, "Give me a name and price, e.g. \"new product Window Wash 60\"."
        draft = {"kind": "product", "product": p, "parsed": parsed}
        return draft, (f"📦 *NEW PRODUCT PREVIEW*\n{p['name']} @ {money(p['unit_price'])}"
                       "\n\nReply *YES* to add to Wave or *CANCEL*.")

    if action == "help":
        return None, HELP
    return None, "🤔 Didn't catch that. Text HELP for examples."


def execute(d):
    """Create everything in Wave. Returns (reply_text, media_url)."""
    if d["kind"] == "client":
        c = d["client"]
        new = wave.create_customer(c["name"], c.get("email"), c.get("phone"), c.get("first_name"), c.get("last_name"))
        return f"✅ Client *{new['name']}* added to Wave.", None

    if d["kind"] == "product":
        p = d["product"]
        new = wave.create_product(p["name"], dec(p["unit_price"]), p.get("description"))
        return f"✅ Product *{new['name']}* added at {money(new['unitPrice'])}.", None

    # invoice / estimate
    c = d["client"]
    if c["new"]:
        created = wave.create_customer(c["name"], c.get("email"), c.get("phone"), c.get("first_name"), c.get("last_name"))
        customer_id = created["id"]
    else:
        customer_id = c["id"]

    for it in d["items"]:
        if it["new"]:
            it["product_id"] = wave.create_product(it["name"], it["unit_price"], it.get("description"))["id"]

    create = wave.create_invoice if d["kind"] == "invoice" else wave.create_estimate
    doc = create(customer_id, d["items"], d.get("memo"))
    reply = (f"✅ {d['kind'].title()} *#{doc['number']}* created as a draft in Wave\n"
             f"Client: {c['name']}\nTotal: {money(doc['total'])}\n\n🔗 {doc['view_url']}")
    return reply, doc.get("pdf_url")


def handle(phone, body):
    cmd = body.strip().lower()
    if cmd in ("help", "?", "menu"):
        return HELP, None
    if cmd in ("cancel", "no", "stop", "discard"):
        PENDING.pop(phone, None)
        return "🗑️ Discarded.", None
    if cmd == "refresh":
        wave.refresh()
        return f"🔄 Reloaded {len(wave.customers)} clients and {len(wave.products)} products.", None
    if cmd in ("yes", "y", "ok", "confirm", "create", "send"):
        draft = PENDING.pop(phone, None)
        if not draft:
            return "Nothing waiting to create. Text HELP for examples.", None
        return execute(draft)

    current = PENDING.get(phone, {}).get("parsed")
    parsed = parse(body, [c["name"] for c in wave.customers], [p["name"] for p in wave.products], current)
    draft, reply = build_draft(parsed)
    if draft:
        PENDING[phone] = draft
    return reply, None


# ---------------- webhook ----------------
@app.post("/whatsapp")
def whatsapp():
    if os.getenv("TWILIO_AUTH_TOKEN"):
        sig = request.headers.get("X-Twilio-Signature", "")
        url = request.url.replace("http://", "https://", 1) if request.headers.get("X-Forwarded-Proto") == "https" else request.url
        if not VALIDATOR.validate(url, request.form, sig):
            abort(403)

    phone = request.form.get("From", "")
    resp = MessagingResponse()
    if ALLOWED and phone not in ALLOWED:
        return str(resp)  # silently ignore strangers

    try:
        text, media = handle(phone, request.form.get("Body", ""))
    except WaveError as e:
        text, media = f"⚠️ Wave said: {e}", None
    except Exception as e:  # keep the chat alive on unexpected errors
        app.logger.exception("handler error")
        text, media = f"⚠️ Something went wrong: {e}", None

    msg = resp.message(text[:1590])  # WhatsApp message limit
    if media:
        msg.media(media)
    return str(resp)


@app.get("/")
def health():
    return f"OK – {len(wave.customers)} clients, {len(wave.products)} products loaded."


if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", 5000)), debug=False)
