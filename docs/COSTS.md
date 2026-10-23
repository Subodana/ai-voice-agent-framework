# Dependencies and running costs

All usage is billed to the client's own accounts. There are no fees from me and no hidden subscriptions. Prices below are list-price estimates from October 2026; check the providers' pricing pages before you commit.

## Software dependencies (free, open source)

Python 3.11+, FastAPI, Uvicorn, Anthropic Python SDK, Twilio Python SDK, requests, pydantic, python-dotenv, tzdata, SQLite (built in), pytest.

## Pay-as-you-go services

| Service | What it's for | Approximate cost |
|---|---|---|
| Twilio phone number | Inbound and outbound number | ≈ $1–2 per month per US local number |
| Twilio voice minutes | The call itself | ≈ $0.01/min inbound, ≈ $0.014/min outbound (US) |
| Twilio ConversationRelay | Speech-to-text, text-to-speech, barge-in | ≈ $0.07/min |
| Anthropic Claude (`claude-opus-5-5`) | Conversation + summary | $4 / M input tokens, $20 / M output tokens, $0.20 / M cached input. Estimate ≈ $0.10–0.25 per 3-minute call |
| Hosting (Render / Railway / VPS) | Runs the app | ≈ $5–25 per month |
| Cal.com | Live calendar (optional) | Free plan is enough for one calendar |
| n8n / Make / Zapier | Webhook → CRM mapping (optional) | Free tiers available; n8n can be self-hosted for free |

**Example:** 500 calls a month at 3 minutes each (1,500 minutes) comes to roughly $120–130 for Twilio voice plus ConversationRelay, $50–125 for Claude, and $5–25 for hosting. That's about $175–280 a month, or **$0.35–0.56 per call**. ConversationRelay is the largest single line.

## Ways to cut cost

- `CLAUDE_MODEL=claude-haiku-5-5` is roughly 40x cheaper per token and faster. Test it on your call scripts before switching, because quality on complex qualification can differ.
- The system prompt and tools are cached per profile, so repeated calls pay the cached-input rate for most of the prompt.
- Twilio Answering Machine Detection is not enabled (it adds a per-call fee). Turn it on only if outbound volume justifies it.

## Not included

- Platform fees from Vapi or Retell. This stack doesn't use them, which saves about $0.05–0.10/min.
- SMS, call recording storage, and CRM licences (the client's own).
