# WhatsApp → Wave Invoice & Estimate Bot

Text WhatsApp, get a preview, reply **YES**, and a draft invoice or estimate appears in your Wave account.

## What it does
- **Invoices & estimates** from plain texts: `invoice Joe Smith 2 lawn mowing, 1 hedge trim`
- **Matches your existing clients and products** in Wave (typos OK: "joe smth" → Joe Smith). Uses your Wave prices and default sales taxes unless you give a price.
- **Only creates new clients/products when you say "new"**: `estimate NEW client Mike Ross mike@x.com: 1 NEW product gutter clean at 120`
- **Adds clients**: `new client Bob Jones, bob@x.com, 555-123-4567`
- **Adds products**: `new product Window Wash 60`
- **Preview first**: shows line items and subtotal. Text a change ("make it 3 mowings", "add a hedge trim") or reply YES / CANCEL.
- After creation it replies with the invoice/estimate number, total, a Wave link, and attaches the PDF when Wave provides one.
- Other commands: `HELP`, `REFRESH` (reload clients/products after editing them in Wave).

Everything is created as a **draft**, so nothing is sent to a client until you send it from Wave.

## Setup (about 30 minutes)

### 1. Wave API token
1. Go to https://developer.waveapps.com → sign in → **Manage Applications** → **Create an application**.
2. Open the app and click **Create token** (full access token). Copy it into `WAVE_TOKEN`.

### 2. Claude API key
Get one at https://console.anthropic.com → `ANTHROPIC_API_KEY`. Create the key inside a workspace; if your key isn't scoped to one, also set `ANTHROPIC_WORKSPACE_ID`. (Claude reads your texts and turns them into line items. Cost is a fraction of a cent per message.)

### 3. Twilio WhatsApp
1. Create a Twilio account → **Messaging → Try it out → Send a WhatsApp message**.
2. Join the sandbox from your phone (text the join code it shows).
3. Copy your **Auth Token** into `TWILIO_AUTH_TOKEN`.
4. Put your own number in `ALLOWED_NUMBERS` as `whatsapp:+15551234567` so nobody else can use your bot.

For permanent use (not the sandbox), register a WhatsApp sender in Twilio.

### 4. Run it
```bash
cp .env.example .env        # fill in the values
pip install -r requirements.txt
python app.py               # runs on port 5000
```
Expose it publicly while testing: `ngrok http 5000`.

In Twilio's sandbox settings, set **"When a message comes in"** to
`https://YOUR-URL/whatsapp` (method POST).

Text **HELP** to your Twilio WhatsApp number. 🎉

### 5. Host it 24/7 (optional)
Deploy the folder to Render, Railway, or Fly.io. Start command:
```
gunicorn app:app --workers 1 --bind 0.0.0.0:$PORT
```
Add your `.env` values as environment variables there, then point Twilio at the new URL.
Keep **1 worker**: drafts waiting for YES are held in memory.

## Files
| File | Purpose |
|---|---|
| `app.py` | WhatsApp webhook, matching, previews, confirmation flow |
| `wave_api.py` | Wave GraphQL calls (clients, products, invoices, estimates) |
| `text_parser.py` | Uses Claude to understand your texts |

## Notes
- **Estimates:** built to mirror `invoiceCreate` using Wave's `estimateCreate` mutation. If Wave returns a field error, adjust the field names in `create_estimate()` in `wave_api.py` to match the Estimate section of Wave's API Reference.
- **Match strictness:** change `MATCH_CUTOFF` in `app.py` (default 85; lower = more forgiving).
- **Multiple Wave businesses:** set `WAVE_BUSINESS_ID`, otherwise the first business is used.
- Test with a throwaway client first, then delete the test invoice in Wave.
