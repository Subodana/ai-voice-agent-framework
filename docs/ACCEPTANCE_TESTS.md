# Acceptance tests: how to reproduce

There are two layers:
- **Automated:** `pytest -v` runs every scenario below with a scripted fake LLM. It needs no keys, phone or network, so the results are repeatable.
- **Manual, live:** the same scenarios in the console (real Claude) and on a real Twilio number (approved test phones only).

Before the manual steps, run `python -m scripts.seed_demo` and start the server.

| # | Acceptance test | Automated test | Manual steps |
|---|---|---|---|
| 1 | Two distinct business profiles on the same framework | `test_two_profiles_same_framework`, `test_profile_editing_is_validated` | Switch *Business* between Bright Smile Dental and Summit Solar, start an inbound call on each, and compare greeting, FAQ answers and transfer targets. Open *Profile config* and save an invalid timezone to see it rejected. |
| 2a | Inbound enquiry → booking + CRM update + summary | `test_inbound_enquiry_to_booking` | Dental, Inbound: "Hi, I'd like to book a new patient exam." Give a name and email and accept a time. Check the **Bookings** tab, **Calls** (outcome `booked` + summary) and **CRM webhooks** (`booking.created`, `call.completed`, signature valid). |
| 2b | Outbound eligible-lead qualification + CRM + summary | `test_outbound_qualification_to_booking`, `test_outbound_disqualified_lead` | Solar, Outbound, Sam Carter, *Dial lead*. Answer: owner yes, bill $180, partial shade, within three months. Book. **Leads** shows `qualified` with the answers. The CRM gets `lead.qualified` and `call.completed`. Repeat with "I rent" to get `not_qualified`. |
| 3a | Interruption handling | `test_interruption_is_passed_to_the_model`, `test_twilio_inbound_conversation_relay` | While the agent is speaking a long answer, press the mic or send text. Its speech stops, the unheard part is struck through, and the next reply follows what you said. On a phone, talk over the agent (ConversationRelay barge-in). |
| 3b | Invalid input recovery | `test_invalid_input_recovery` | Give an invalid email ("maria at example") or ask for a time that wasn't offered. The agent explains and asks again, and no booking is made. Send an empty message three times: reprompt, then an offer of a person, then a callback task and hang-up. |
| 3c | Failed integration fallback | `test_calendar_outage_falls_back_to_callback`, `test_crm_outage_is_retried_then_delivered`, `test_ai_failure_hands_off_to_a_human` | Tick **Simulate calendar outage** and try to book. The agent says it can't book right now and a high-priority callback task is created. Tick **Simulate CRM outage** and make a call: events stay `pending` with an error. Untick it and click *Retry now* (or wait): they become `sent`. |
| 3d | Human escalation | `test_human_escalation_open_and_closed` | Dental: "I have terrible pain and my face is swelling" → transfer to Front desk. Set `hours_override` to `closed` in *Profile config* and ask for a person → no transfer, a callback task instead. |
| 3e | Opt-out suppression | `test_opt_out_during_call_suppresses_future_dials`, `test_outbound_eligibility_checks` | During an outbound call say "Please stop calling me." The number is added to **Do-not-call** at once (source: keyword) and the call ends with outcome `opted_out`. Dial the same lead again: blocked. *Run campaign*: Riley (no consent), Jordan (DNC) and Casey (outside the calling window) are blocked with reasons in the **Dial log**. |
| 4 | Walkthrough, source, setup guide, costs, access | n/a | See [DEMO_SCRIPT.md](DEMO_SCRIPT.md), [SETUP.md](SETUP.md) and [COSTS.md](COSTS.md). Source is this repository. |
| 5 | Reproducible, no hardcoded secrets, no misleading promises | `test_no_secrets_in_profiles` | Secrets are only in `.env`. The prompts forbid inventing prices, savings and coverage, and the FAQs say so explicitly. |

## Live phone checks (approved test numbers only)

1. Inbound: call the Twilio number and run steps 2a, 3a and 3d by voice.
2. Outbound: put your own mobile in `OUTBOUND_TEST_NUMBERS`, add yourself as a consented lead, and dial with `"mode": "twilio"`. A lead whose number is not on the list is refused with "not an approved test number".
3. Let one outbound call ring out. The outcome is `no_answer` and the next retry time appears in **Leads**.
