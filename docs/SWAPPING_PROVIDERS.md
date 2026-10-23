# Swapping providers

Every integration sits behind a small interface, and a profile picks a provider by name.

## Calendar (`app/scheduling.py`)

Implement two methods and register the class in `build_calendars()`:

```python
class GoogleCalendar:
    name = "google"
    def available_slots(self, profile, day=None, limit=6) -> list[datetime]: ...   # tz-aware datetimes
    def book(self, profile, start: datetime, attendee: dict) -> dict: ...           # {"external_id", "start", "end"}
```

Raise `IntegrationError` for outages, which triggers the callback-task fallback. Raise `ValueError` for "that time is taken", and the agent asks the caller again. Then set `"calendar": {"provider": "google", ...}` in the profile. Calendly, Acuity, GoHighLevel calendars and Microsoft Bookings follow the same pattern.

## CRM (`app/crm.py`)

- **No code:** point `crm.webhook_url` at n8n, Make, Zapier, a GoHighLevel inbound webhook or a HubSpot workflow webhook, and map the fields there.
- **Direct API:** subclass `WebhookCRM` and override `_deliver` so it maps the event to the CRM's API (e.g. HubSpot contacts, notes and tasks). The outbox, retries and dead-letter handling stay the same.

## LLM (`app/agent.py`)

`CLAUDE_MODEL` selects the Claude model (e.g. `claude-haiku-5-5` for lower cost and latency). The tools are plain JSON Schema, so another provider means rewriting only `VoiceAgent._complete()` and the tool-result format.

## Voice and telephony (`app/telephony.py`)

- **Voice:** `TTS_PROVIDER` and `TTS_VOICE` are passed to ConversationRelay (e.g. ElevenLabs, Google or Amazon voices that Twilio supports).
- **Telephony / voice platform:** the engine is transport-agnostic. `VoiceAgent.start_call()`, `respond()`, `interrupted()` and `finalize()` are all a transport needs. Vapi or Retell can call these through their "custom LLM" WebSocket option. A LiveKit or Telnyx transport would replace only `telephony.py`.

## Storage (`app/store.py`)

SQLite with plain SQL. Moving to Postgres means changing the connection and the `?` placeholders. The rest of the app only uses `Store` methods.
