# Walkthrough recording script (about 6–8 minutes)

Preparation: `python -m scripts.seed_demo`, start the server, open the console in Chrome, and turn on *Speak agent replies aloud*. Use a headset so the mic doesn't pick up the agent.

1. **Framework (30 s).** Show `profiles/` with two files, then switch the *Business* dropdown. "Same code; everything business-specific is in this config."
2. **Inbound enquiry → booking, dental (90 s).**
   - "Hi, do you take insurance?" → the answer comes from the FAQ, with no promise of coverage.
   - "I'd like to book a new patient exam." Give a name and an email.
   - **Interrupt:** while it reads out times, press the mic and say "Actually, do you have anything Tuesday?" Point out the struck-through text it never said.
   - Accept a time. Show **Bookings**, **Calls** (summary, outcome `booked`) and **CRM webhooks** (`booking.created`, signature valid).
3. **Invalid input (30 s).** Give the email as "maria at example": the agent asks again.
4. **Integration failure (45 s).** Tick *Simulate calendar outage* and ask to book. The agent can't book right now and promises a callback. Show the high-priority task. Tick *Simulate CRM outage*, make a short call, show `pending` events, untick, *Retry now* → `sent`.
5. **Escalation (30 s).** "I'm in terrible pain and my face is swelling" → transfer to Front desk.
6. **Outbound qualification, solar (90 s).** *Outbound* → Sam Carter → *Dial lead*. The agent says it's an AI assistant. Answer the four questions, get qualified, book. Show the qualification answers in **Leads** and `lead.qualified` in the CRM tab.
7. **Opt-out (45 s).** Add a consented lead, dial, and say "Please stop calling me." Show the **Do-not-call** entry. Dial again: blocked. Click *Run campaign* to show no-consent, DNC and outside-hours leads blocked, with reasons in the **Dial log**.
8. **Real phone (60 s, optional but strongest).** Call the Twilio number from your mobile. Have a short conversation, talk over the agent once, and ask for a person.
9. **Close (30 s).** Run `pytest -v` (all green), then show the docs folder and `.env.example` with no secrets.
