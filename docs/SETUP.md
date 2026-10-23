# Setup guide

## 1. Requirements

- Python 3.11+ (tested on 3.12)
- An Anthropic API key (https://console.anthropic.com)
- For real phone calls: a Twilio account with one voice number, plus a public HTTPS URL (ngrok for testing, or a host such as Render, Railway or Fly.io)
- Optional: a Cal.com account (live calendar), and n8n/Make/Zapier or your CRM's webhook endpoint

## 2. Install and run locally

```bash
python -m venv .venv
.venv\Scripts\activate           # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env           # macOS/Linux: cp .env.example .env
python -m scripts.seed_demo
uvicorn app.main:app --port 8000
pytest -v                        # automated acceptance tests (no keys needed)
```

## 3. Credentials (`.env`)

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | Every conversation | `CLAUDE_MODEL` defaults to `claude-opus-5-5`; `CLAUDE_EFFORT=low` keeps replies fast |
| `ADMIN_TOKEN` | Anything reachable from the internet | Protects `/api/*`. Enter it in the console header |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Real calls | The auth token also turns on webhook signature checks |
| `TWILIO_FROM_NUMBER` | Outbound | A profile can override it with `outbound.caller_id` |
| `PUBLIC_BASE_URL` | Real calls | e.g. `https://abc123.ngrok-free.app` |
| `OUTBOUND_TEST_NUMBERS` | Real outbound | Comma-separated E.164 numbers. **Real dials to any other number are refused.** |
| `TTS_PROVIDER`, `TTS_VOICE` | Voice choice | ConversationRelay voice settings; leave the voice empty for the default |
| `*_WEBHOOK_SECRET` | CRM webhook signing | Each profile names its secret in `crm.secret_env` |
| `*_CALCOM_API_KEY` | Cal.com | Each profile names its key in `calendar.api_key_env` |
| `SELF_URL` | Mock CRM | The server's own address, used for webhook URLs that start with `/` |

Secrets never go in profile files. A test checks for this.

## 4. Connect a Twilio number

1. Buy a voice-capable number in the Twilio console.
2. Expose the app: `ngrok http 8000`, then set `PUBLIC_BASE_URL` to the https URL and restart.
3. In the number's **Voice configuration**:
   - *A call comes in* → Webhook `POST {PUBLIC_BASE_URL}/voice/incoming`
   - *Call status changes* → `POST {PUBLIC_BASE_URL}/voice/status`
4. Add the number to the profile's `phone_numbers` list. The dialled number decides which business answers, so one deployment can serve many clients.
5. Call the number from your own phone.

Outbound: put your own mobile in `OUTBOUND_TEST_NUMBERS`, add yourself as a lead with consent in the console, then send `POST /api/outbound/dial` with `{"profile_id": "...", "lead_id": N, "mode": "twilio"}`. The status callback finalises the call and schedules retries for no-answer and busy.

## 5. Calendar

- `calendar.provider: "local"` (default) uses the built-in calendar, which works from the profile's business hours. Bookings appear in the console's Bookings tab.
- `calendar.provider: "calcom"`: create an event type in Cal.com and copy its numeric ID into `calendar.event_type_id`. Create an API key (Settings → Developer → API keys) and put it in the env var named by `calendar.api_key_env`. Cal.com needs an attendee email, so the agent collects one before booking.

## 6. CRM

Every change is sent as a signed JSON event to `crm.webhook_url`:

```json
{"id": "evt_…", "type": "call.completed", "profile_id": "summit-solar", "company": "Summit Solar",
 "occurred_at": "2026-10-23T10:00:00+00:00", "data": {"outcome": "booked", "summary": "…", "lead": {…}, "transcript": […]}}
```

Event types: `lead.updated`, `lead.qualified`, `lead.disqualified`, `lead.opted_out`, `booking.created`, `task.created`, `call.completed`.
Headers: `X-Voice-Event` (the event type) and `X-Signature: sha256=<HMAC-SHA256 of the body using the profile's secret>`.

- **n8n / Make / Zapier:** create a webhook trigger, paste its URL into the profile, then map fields into HubSpot, GoHighLevel, Pipedrive, Sheets and so on. No code change is needed.
- **Your own API:** verify `X-Signature`, then upsert by `data.lead.phone`.
- **Built-in mock CRM:** `"/mock-crm/webhook"` (the default in the examples). Received events and their signature check appear in the console.

Delivery is retried with backoff (10 s, 20 s, 40 s … up to 1 h) and marked `dead` after `WEBHOOK_MAX_ATTEMPTS`. The console has a *Retry now* button.

## 7. Add a new business

1. Copy `profiles/summit-solar.json` to `profiles/<new-id>.json` and change `id`.
2. Edit identity, greeting, hours, FAQ, transfer targets, qualification questions/rules, calendar, CRM and outbound settings.
3. Restart, or edit in the console's *Profile config* tab, which validates before saving.

## 8. Deploy

Any host that runs a Python web service and supports WebSockets works: Render, Railway, Fly.io or a small VPS. The start command is `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Keep a persistent disk for `data/` (SQLite), set `ADMIN_TOKEN`, and run one instance.
