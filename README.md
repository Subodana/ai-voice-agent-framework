# Voice Agent Framework: reusable inbound and outbound AI calling

One codebase serves any number of businesses. Each business is a JSON **profile**: its identity, prompts, FAQs, hours, qualification rules, calendar, transfer targets and CRM webhook. Adding a client means adding a profile. The core code doesn't change.

```
 Phone ──► Twilio number ──► /voice/incoming ─┐            ┌─► Calendar (built-in │ Cal.com)
 Lead  ◄── Twilio REST  ◄── Dialer ───────────┤            ├─► CRM webhook outbox (signed, retried)
          (consent, DNC, calling-window       ▼            ├─► Tasks / callbacks
           checks before every dial)    ConversationRelay  ├─► Do-not-call list
                                        (STT, TTS, barge-in)│
                                              │ WebSocket   │
                                              ▼             │
                                     VoiceAgent (Claude + tools) ──► Call log, summary, outcome
 Browser simulator ──► /api/sim/* ───────────┘   (same engine, no phone needed)
```

## What's included

| Brief requirement | Where |
|---|---|
| Inbound receptionist, FAQ, lead intake, booking | [app/agent.py](app/agent.py) tools: `save_lead_details`, `check_availability`, `book_appointment` |
| Outbound qualification and follow-up | [app/telephony.py](app/telephony.py) `Dialer` + the `record_qualification` tool; rules are scored by code, not the LLM |
| Reusable client configuration | [profiles/](profiles/) (two examples), validated by [app/profiles.py](app/profiles.py), editable in the console |
| Turn-taking and interruptions | Twilio ConversationRelay barge-in. `interrupt` events record what the caller actually heard and pass it to the model |
| Misunderstood or invalid input | Tool inputs are validated (email, date, offered slots only). Silence: reprompt, then offer a person, then a callback task |
| Human transfer and escalation | `transfer_to_human` with per-profile targets and triggers. After hours it creates a callback task. If the AI fails, the call is handed off |
| Outcomes and summaries | Outcome status is set by code (`booked`, `qualified`, `not_qualified`, `transferred`, `opted_out`, `callback_requested`, `no_answer` …). Claude writes the summary, with a fallback if it fails |
| Calendar integration | [app/scheduling.py](app/scheduling.py): built-in calendar and Cal.com API v2 |
| CRM/webhook integration | [app/crm.py](app/crm.py): HMAC-signed webhook with a durable outbox, exponential backoff and a dead-letter state |
| Opt-out / do-not-call | Opt-out phrases are caught in code mid-call and added to the DNC list at once. The outbound dialer checks DNC, consent, calling window, attempts and an approved-test-number list, and logs every decision |
| Admin configuration | Console at `/`: simulator, leads, bookings, tasks, webhook outbox (with retry), DNC list, dial log, profile editor, outage switches |
| No hardcoded secrets | Profiles reference env-var names only. All secrets are in `.env` (see [.env.example](.env.example)) |

## Quick start (no phone number needed)

```bash
python -m venv .venv
.venv\Scripts\activate                 # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env                 # add ANTHROPIC_API_KEY (and ADMIN_TOKEN if exposed publicly)
python -m scripts.seed_demo            # demo leads: eligible, no-consent, DNC, outside-hours
uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000 in Chrome or Edge. Pick a business, then:
- **Inbound:** click *Call the business*, then talk with the mic or type.
- **Outbound:** switch to *Outbound*, pick a lead, then *Dial lead*. *Run campaign* shows which leads are blocked and why.

If you run on another port, set `SELF_URL` to match. The built-in mock CRM webhook uses it.

Run the automated acceptance tests (no API key or network needed): `pytest -v`

## Documentation

- [docs/SETUP.md](docs/SETUP.md): install, credentials, Twilio, Cal.com, connecting a real CRM, deployment
- [docs/ACCEPTANCE_TESTS.md](docs/ACCEPTANCE_TESTS.md): reproducible steps for every acceptance test
- [docs/COSTS.md](docs/COSTS.md): dependencies and running costs
- [docs/SWAPPING_PROVIDERS.md](docs/SWAPPING_PROVIDERS.md): change calendar, CRM, LLM, voice or telephony
- [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md): script for the recorded walkthrough

## Project layout

```
app/agent.py        Conversation engine: Claude, tools, interruption/no-input/failure handling, summaries
app/telephony.py    Twilio ConversationRelay routes + WebSocket, outbound Dialer
app/compliance.py   Opt-out detection, outbound eligibility checks
app/scheduling.py   Calendar providers (built-in, Cal.com)
app/crm.py          Signed webhook delivery with retrying outbox
app/profiles.py     Profile schema, validation, routing by dialled number, business hours
app/store.py        SQLite: leads, calls, bookings, tasks, DNC, outbox, dial log
app/main.py         Admin API, simulator API, mock CRM receiver
profiles/*.json     One file per business
static/index.html   Admin console + browser call simulator
scripts/seed_demo.py  Demo leads
tests/              Acceptance tests with a scripted fake LLM
```

## Known limitations

- The Twilio ConversationRelay routes and WebSocket protocol are covered by automated tests that simulate Twilio's messages. Verify them on a real Twilio number with approved test phones before going live.
- The Cal.com adapter follows Cal.com API v2. Confirm it against your Cal.com account before relying on it. The built-in calendar works with no setup.
- Live call state is held in memory and SQLite is single-server. Use one instance, or move to Postgres/Redis to scale out.
- No voicemail detection or SMS confirmations yet. Twilio AMD and SMS can be added at extra per-use cost.
- The compliance features are engineering controls, not legal advice or certification. The business is responsible for consent records and local calling rules (e.g. TCPA and state laws).
