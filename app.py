"""WhatsApp -> Wave invoice/estimate bot (Twilio webhook)."""
import os
import re
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
from flask import Flask, abort, request
from rapidfuzz import fuzz, process, utils
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()

from text_parser import check_key, parse  # noqa: E402
from wave_api import Wave, WaveError  # noqa: E402

app = Flask(__name__)
wave = Wave(os.environ["WAVE_TOKEN"], os.getenv("WAVE_BUSINESS_ID") or None,
            os.getenv("WAVE_INCOME_ACCOUNT_ID") or None)
ALLOWED = {n.strip() for n in os.getenv("ALLOWED_NUMBERS", "").split(",") if n.strip()}
VALIDATOR = RequestValidator(os.getenv("TWILIO_AUTH_TOKEN", ""))
CUR = os.getenv("CURRENCY_SYMBOL", "$")
MATCH_CUTOFF = 85

print(check_key(), flush=True)  # shows in Railway logs

PENDING = {}  # phone -> draft waiting for YES
LAST = {}  # phone -> last invoice/estimate created or converted, for SEND / CONVERT

HELP = (
    "🧾 *Wave Bot*\n"
    "• invoice Joe Smith 2 lawn mowing, 1 hedge trim\n"
    "• estimate for Sarah Lee: 10 hrs consulting at 80\n"
    "• invoice NEW client Mike Ross mike@x.com: 1 NEW product gutter clean at 120\n"
    "• new client Bob Jones, bob@x.com, 555-123-4567\n"
    "• new product Window Wash 60\n"
    "• change Bob Jones email to bob@new.com\n"
    "• YES to create · CANCEL to discard · REFRESH to reload lists\n"
    "• Send a change (\"make it 3 lawn mowings\") to edit the preview\n"
    "• SEND to email the last one to the client (or: send estimate 12 to bob@x.com)\n"
    "• CONVERT to turn the last estimate into an invoice (or: convert estimate 12)"
)


def money(v):
    # Wave can return amounts as formatted strings ("4,175.00"); never fail just to display one
    try:
        return f"{CUR}{Decimal(re.sub(r'[^0-9.-]', '', str(v))):,.2f}"
    except InvalidOperation:
        return f"{CUR}{v}"


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
def build_draft(parsed, phone=None):
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

    if action == "update_client":
        # No name given ("change email to ...") -> the client of the last invoice/estimate
        name = (parsed.get("client") or {}).get("name") or (LAST.get(phone) or {}).get("client")
        changes = {k: v for k, v in (parsed.get("client_changes") or {}).items() if v}
        if not name or not changes:
            return None, "Which client and what should change? e.g. \"change Brittany Spears email to b@x.com\""
        match, sugg = best_match(name, wave.customers)
        if not match:
            hint = f" Did you mean: {', '.join(sugg)}?" if sugg else ""
            return None, f"❓ I couldn't find client \"{name}\".{hint}"
        lines = [f"✏️ *UPDATE CLIENT* {match['name']}"]
        for k in ("name", "email", "phone"):
            if k in changes:
                lines.append(f"{k.title()}: {match.get(k) or '—'} → {changes[k]}")
        lines.append("\nReply *YES* to save in Wave or *CANCEL*.")
        return {"kind": "client_update", "client": match, "changes": changes, "parsed": parsed}, "\n".join(lines)

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


def execute(d, phone):
    """Create everything in Wave. Returns (reply_text, media_url)."""
    if d["kind"] == "client":
        c = d["client"]
        new = wave.create_customer(c["name"], c.get("email"), c.get("phone"), c.get("first_name"), c.get("last_name"))
        return f"✅ Client *{new['name']}* added to Wave.", None

    if d["kind"] == "send":
        doc, to = d["doc"], d["to"]
        wave.send_doc(doc, to)
        LAST[phone] = doc
        extra = "\nReply *CONVERT* to turn it into an invoice" if doc["kind"] == "estimate" else ""
        return (f"✅ {doc['kind'].title()} *#{doc['number']}* was sent to {to}\n"
                f"Client: {doc['client']} · Total: {money(doc['total'])}{extra}"), None

    if d["kind"] == "convert":
        doc = d["doc"]
        inv = wave.convert_estimate(doc)
        LAST[phone] = inv
        done = f"✅ Estimate *#{doc['number']}* converted to Invoice *#{inv['number']}*"
        if d.get("then_send"):
            wave.send_doc(inv, d["to"])
            return (f"{done}\n✅ Invoice *#{inv['number']}* was sent to {d['to']}\n"
                    f"Client: {inv['client']} · Total: {money(inv['total'])}\n\n🔗 {inv['view_url']}"), inv.get("pdf_url")
        return (f"{done}\nClient: {inv['client']}\nTotal: {money(inv['total'])}\n\n🔗 {inv['view_url']}\n\n"
                f"{next_steps(inv)}"), inv.get("pdf_url")

    if d["kind"] == "client_update":
        c = wave.update_customer(d["client"]["id"], **d["changes"])
        details = " · ".join(x for x in (c.get("email"), c.get("phone")) if x)
        return f"✅ Client *{c['name']}* updated in Wave.\n{details}", None

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
    LAST[phone] = doc
    reply = (f"✅ {d['kind'].title()} *#{doc['number']}* created as a draft in Wave\n"
             f"Client: {doc['client']}\nTotal: {money(doc['total'])}\n\n🔗 {doc['view_url']}\n\n{next_steps(doc)}")
    return reply, doc.get("pdf_url")


def next_steps(doc):
    to = doc.get("email")
    lines = [f"Reply *SEND* to email it to {to}" if to else "Reply *SEND to name@email.com* to email it"]
    if doc["kind"] == "estimate":
        lines.append("Reply *CONVERT* to turn it into an invoice")
    return "\n".join(lines)


EMAIL = r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
# "send", "send estimate 12 to bob@x.com", "convert it", "convert estimate 478 to an invoice and send it"
DOC_CMD_RE = re.compile(
    r"^(?:please\s+)?(?P<verb>convert|send|email)(?:\s+(?:it|the|this))?(?:\s+(?P<kind>estimate|quote|invoice))?"
    r"(?:\s*(?:number|no\.?)?\s*#?\s*(?P<num>\d+))?(?:\s+(?:to|into)\s+(?:an?\s+)?invoice)?"
    r"(?:\s*(?:,|and|&|then)\s*(?:then\s+)?(?P<send2>send|email)(?:\s+(?:it|the\s+invoice))?)?"
    rf"(?:\s+to\s+(?P<to>{EMAIL}))?\s*(?:please)?[.!]*$", re.I)


# "change email to x@y.com", "update phone number to 555-1234"
QUICK_EDIT_RE = re.compile(r"^(?:change|update|set|new)\s+(?:the\s+|their\s+|client'?s?\s+)?(email|phone)"
                           r"(?:\s+(?:address|number))?\s+(?:to\s+)?(\S.*)$", re.I)


def pick_doc(phone, kind, number):
    """The document a SEND/CONVERT refers to: by number if given, else the last one made here."""
    last = LAST.get(phone)
    if number:
        kind = kind or (last or {}).get("kind")
        if not kind:
            return None, f"Estimate or invoice? e.g. \"send estimate {number}\""
        doc = wave.find_doc(kind, number)
        return (doc, None) if doc else (None, f"❓ Couldn't find {kind} #{number} in Wave.")
    if last and (not kind or kind == last["kind"]):
        return wave.get_doc(last["kind"], last["id"]) or last, None
    return None, f"Which one? e.g. \"send {kind or 'estimate'} 12\""


def doc_action(phone, convert, send, kind=None, number=None, to=None):
    """Preview a SEND / CONVERT / CONVERT+SEND and hold it until YES."""
    if PENDING.get(phone) and not number:
        return "Something is waiting for your OK. Reply *YES* to confirm it or *CANCEL* first.", None
    if convert:
        if kind == "invoice":
            return "Only estimates can be converted. e.g. \"convert estimate 12\"", None
        kind = "estimate"
    doc, err = pick_doc(phone, kind, number)
    if err:
        return err, None
    if convert and doc["kind"] != "estimate":
        return "Only estimates can be converted. e.g. \"convert estimate 12\"", None
    to = to or doc.get("email")
    if send and not to:
        return (f"No email on file for {doc['client']}. Add one with \"change email to name@email.com\" "
                f"or say \"... to name@email.com\"."), None
    head = f"Client: {doc['client']} · Total: {money(doc['total'])}"
    if convert:
        PENDING[phone] = {"kind": "convert", "doc": doc, "then_send": send, "to": to}
        what = f"CONVERT ESTIMATE #{doc['number']} TO AN INVOICE" + (" AND EMAIL IT" if send else "")
        return (f"🔁 *{what}?*\n" + (f"To: {to}\n" if send else "") + f"{head}\n\n"
                "Reply *YES* to go ahead or *CANCEL*."), None
    PENDING[phone] = {"kind": "send", "doc": doc, "to": to}
    return (f"📧 *SEND {doc['kind'].upper()} #{doc['number']}?*\nTo: {to}\n{head}\n\n"
            "Reply *YES* to email it (PDF attached) or *CANCEL*."), None


def doc_command(phone, body):
    """Short SEND / CONVERT texts. Returns (reply, media) or None if the text isn't one."""
    m = DOC_CMD_RE.match(body.strip())
    if not m:
        return None
    kind = {"quote": "estimate"}.get((m["kind"] or "").lower(), (m["kind"] or "").lower() or None)
    convert = m["verb"].lower() == "convert"
    return doc_action(phone, convert, not convert or bool(m["send2"]), kind, m["num"], m["to"])


def handle(phone, body):
    cmd = body.strip().lower()
    if cmd in ("help", "?", "menu"):
        return HELP, None
    if cmd in ("cancel", "no", "stop", "discard"):
        PENDING.pop(phone, None)
        return "🗑️ Discarded.", None
    if cmd == "status":
        return check_key(), None
    if cmd == "refresh":
        wave.refresh()
        return f"🔄 Reloaded {len(wave.customers)} clients and {len(wave.products)} products.", None
    if cmd in ("yes", "y", "ok", "confirm", "create"):
        draft = PENDING.pop(phone, None)
        if not draft:
            return "Nothing waiting to create. Text HELP for examples.", None
        return execute(draft, phone)
    done = doc_command(phone, body)
    if done:
        return done

    m = QUICK_EDIT_RE.match(body.strip())
    if m and LAST.get(phone) and not PENDING.get(phone):
        # "change email to x@y.com" right after an invoice/estimate -> that client
        draft, reply = build_draft({"action": "update_client", "client": None,
                                    "client_changes": {m[1].lower(): m[2].strip()}}, phone)
        if draft:
            PENDING[phone] = draft
        return reply, None

    current = PENDING.get(phone, {}).get("parsed")
    parsed = parse(body, [c["name"] for c in wave.customers], [p["name"] for p in wave.products], current)
    if parsed.get("action") == "document":
        a = parsed.get("document") or {}
        return doc_action(phone, bool(a.get("convert")), bool(a.get("send")), a.get("kind"),
                          a.get("number"), a.get("send_to"))
    draft, reply = build_draft(parsed, phone)
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
