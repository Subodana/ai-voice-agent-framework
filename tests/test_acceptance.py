"""One test (or group) per acceptance criterion in the brief. Run: pytest -v"""

import json

import anthropic
import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

from app.agent import build_system_prompt, build_tools
from app.crm import sign, verify
from conftest import FakeClient, last_tool_result, resp, text, tool, tz_in_window, tz_outside_window


def events(env, kind=None):
    env.ctx.crm.flush()
    rows = [c["body"] for c in env.post.calls]
    return [r for r in rows if kind is None or r["type"] == kind]


def book_first_slot(name, email=None):
    """Script step: book the first slot the calendar returned in the previous tool result."""
    def step(request):
        slot = last_tool_result(request)["slots"][0]["start"]
        args = {"start": slot, "name": name, **({"email": email} if email else {})}
        return resp(tool("book_appointment", args, "book"))
    return step


# --- 1. Two distinct business profiles on the same framework ------------------------------------
def test_two_profiles_same_framework(env):
    dental, solar = env.ctx.profiles.get("bright-smile-dental"), env.ctx.profiles.get("summit-solar")
    assert dental.company_name != solar.company_name
    assert "Bright Smile Dental" in build_system_prompt(dental) and "Summit Solar" in build_system_prompt(solar)
    transfer = lambda p: next(t for t in build_tools(p) if t["name"] == "transfer_to_human")
    assert transfer(dental)["input_schema"]["properties"]["target"]["enum"] == ["front_desk", "office_manager"]
    assert transfer(solar)["input_schema"]["properties"]["target"]["enum"] == ["solar_specialist", "service_team"]
    # Inbound numbers route to the right profile
    assert env.ctx.profiles.for_number("+1 555 020 0001", "bright-smile-dental").id == "summit-solar"


def test_profile_editing_is_validated(env):
    good = env.http.get("/api/profiles").json()[0]
    bad = {**good, "timezone": "Mars/Olympus"}
    assert env.http.put(f"/api/profiles/{good['id']}", json=bad).status_code == 422
    good["greeting"] = "Hello from the edited profile."
    assert env.http.put(f"/api/profiles/{good['id']}", json=good).status_code == 200
    saved = json.loads((env.profiles_dir / f"{good['id']}.json").read_text())
    assert saved["greeting"] == "Hello from the edited profile."


# --- 2a. Inbound enquiry -> booking, with CRM webhook + summary --------------------------------
def test_inbound_enquiry_to_booking(env):
    env.fake.script = [
        resp(text("Of course. Can I take your name?"), tool("save_lead_details", {"reason": "new patient exam"}, "a")),
        resp(text("Thanks! Can I take your name?")),
        resp(tool("save_lead_details", {"name": "Maria Lopez", "email": "maria@example.com"}, "b"),
             tool("check_availability", {}, "c")),
        lambda req: resp(text(f"I have {last_tool_result(req)['slots'][0]['spoken']}. Does that work?")),
        lambda req: resp(tool("check_availability", {}, "d")),
        book_first_slot("Maria Lopez", "maria@example.com"),
        resp(text("You're booked. See you then!"), tool("end_call", {}, "e")),
    ]
    agent, profile = env.ctx.agent, env.ctx.profiles.get("bright-smile-dental")
    session, opening = agent.start_call("CA-IN-1", profile, "inbound", "+15550101111")
    assert "Bright Smile Dental" in opening

    agent.respond(session, "Hi, I'd like to book a new patient exam")
    r = agent.respond(session, "Maria Lopez, maria@example.com")
    assert "Does that work" in r.text
    r = agent.respond(session, "Yes that works")
    assert r.action == "hangup"

    bookings = env.ctx.store.bookings("bright-smile-dental")
    assert len(bookings) == 1 and bookings[0]["lead_name"] == "Maria Lopez"
    record = agent.finalize("CA-IN-1")
    assert record["outcome"] == "booked" and record["summary"]
    lead = env.ctx.store.find_lead(profile.id, "+15550101111")
    assert lead["status"] == "booked" and lead["email"] == "maria@example.com"

    types = [e["type"] for e in events(env)]
    assert "booking.created" in types and "call.completed" in types
    done = events(env, "call.completed")[0]
    assert done["data"]["outcome"] == "booked" and done["data"]["summary"]
    assert env.post.calls[0]["headers"]["X-Signature"].startswith("sha256=")


# --- 2b. Outbound eligible-lead qualification ---------------------------------------------------
def add_lead(env, phone, consent="express_written", tz=None, profile="summit-solar", name="Sam Carter"):
    return env.http.post("/api/leads", json={"profile_id": profile, "phone": phone, "name": name,
                                             "consent_status": consent, "timezone": tz or tz_in_window()}).json()


def test_outbound_qualification_to_booking(env):
    lead = add_lead(env, "+15550102222")
    env.fake.script = [
        resp(text("Great. Do you own the home?")),
        resp(tool("record_qualification", {"answers": {"homeowner": "yes", "monthly_bill": "about $180"}}, "q1")),
        resp(text("And how much shade does your roof get?")),
        resp(tool("record_qualification", {"answers": {"roof_shade": "partial", "timeframe": "within three months"}}, "q2")),
        lambda req: resp(tool("check_availability", {}, "c")) if last_tool_result(req)["qualified"] else None,
        lambda req: resp(text(f"You qualify for a free assessment. How about {last_tool_result(req)['slots'][0]['spoken']}?")),
        lambda req: resp(tool("check_availability", {}, "c2")),
        book_first_slot("Sam Carter"),
        resp(text("Booked. Thanks Sam, goodbye!"), tool("end_call", {}, "e")),
    ]
    dial = env.http.post("/api/outbound/dial", json={"profile_id": "summit-solar", "lead_id": lead["id"]}).json()
    assert dial["allowed"] and "AI assistant" in dial["reply"]
    agent = env.ctx.agent
    session = agent.get(dial["call_id"])

    agent.respond(session, "Sure, go ahead")
    agent.respond(session, "Yes I own it, the bill is about 180 dollars a month")
    r = agent.respond(session, "Partial shade, and I want it within three months")
    assert "qualify" in r.text
    r = agent.respond(session, "That works")
    assert r.action == "hangup"

    record = agent.finalize(dial["call_id"])
    assert record["outcome"] == "booked" and record["direction"] == "outbound"
    stored = env.ctx.store.get_lead(lead["id"])
    assert stored["qualified"] == 1 and stored["qualification"]["answers"]["monthly_bill"] == "about $180"
    assert {"lead.qualified", "booking.created", "call.completed"} <= {e["type"] for e in events(env)}


def test_outbound_disqualified_lead(env):
    lead = add_lead(env, "+15550102223")
    env.fake.script = [
        resp(tool("record_qualification", {"answers": {"homeowner": "no", "monthly_bill": "250",
                                                       "roof_shade": "none", "timeframe": "within a year"}}, "q")),
        resp(text("Thanks for your time, it doesn't sound like the right fit right now. Goodbye!"), tool("end_call", {}, "e")),
    ]
    dial = env.ctx.dialer.dial(env.ctx.profiles.get("summit-solar"), lead["id"])
    env.ctx.agent.respond(env.ctx.agent.get(dial["call_id"]), "I rent, bill is 250, no shade, within a year")
    assert env.ctx.agent.finalize(dial["call_id"])["outcome"] == "not_qualified"
    assert env.ctx.store.get_lead(lead["id"])["qualification"]["failed"] == ["not the homeowner"]


# --- 3. Interruption, invalid input, integration failure, escalation, opt-out ---------------------
def test_interruption_is_passed_to_the_model(env):
    env.fake.script = [resp(text("Sure - what would you like to know?"))]
    agent = env.ctx.agent
    session, _ = agent.start_call("CA-INT", env.ctx.profiles.get("bright-smile-dental"), "inbound", "+15550103333")
    agent.interrupted(session, "Thanks for calling Bright Smile")
    agent.respond(session, "Sorry, quick question about parking")
    sent = env.fake.requests[-1]["messages"][-1]["content"]
    assert "interrupted" in sent[0]["text"] and "Thanks for calling Bright Smile" in sent[0]["text"]
    assert session.transcript[0]["interrupted_after"] == "Thanks for calling Bright Smile"
    assert session.events[0]["type"] == "interruption"


def test_invalid_input_recovery(env):
    agent = env.ctx.agent
    env.fake.script = [
        resp(tool("save_lead_details", {"email": "maria at example"}, "a")),
        resp(text("Sorry, could you spell that email for me?")),
        resp(tool("book_appointment", {"start": "2030-01-01T09:00:00-05:00", "name": "Maria"}, "b")),
        resp(text("Let me check what's actually free.")),
        resp(tool("check_availability", {"date": "next tuesday"}, "c")),
        resp(text("Which date would you like?")),
    ]
    session, _ = agent.start_call("CA-BAD", env.ctx.profiles.get("bright-smile-dental"), "inbound", "+15550104444")
    agent.respond(session, "it's maria at example")
    assert "valid email" in env.fake.requests[1]["messages"][-1]["content"][0]["content"]
    agent.respond(session, "book me january first 2030 at 9")  # a time the calendar never offered
    assert "wasn't offered" in env.fake.requests[3]["messages"][-1]["content"][0]["content"]
    agent.respond(session, "next tuesday")
    assert "not a valid date" in env.fake.requests[5]["messages"][-1]["content"][0]["content"]
    assert env.ctx.store.bookings() == []
    assert [e["type"] for e in session.events].count("invalid_input") == 3

    # Silence: reprompt, offer a person, then hang up with a callback task
    assert "didn't catch" in agent.respond(session, "").text
    assert "person" in agent.respond(session, "  ").text
    assert agent.respond(session, "").action == "hangup"
    assert env.ctx.store.tasks()[0]["title"].startswith("Call dropped")


def test_calendar_outage_falls_back_to_callback(env):
    env.ctx.faults.set("calendar", True)
    env.fake.script = [
        resp(tool("check_availability", {}, "c")),
        lambda req: resp(text("I'm sorry, our booking system isn't responding. The team will call you back to confirm a time."))
        if last_tool_result(req)["error"] == "calendar_unavailable" else None,
    ]
    agent = env.ctx.agent
    session, _ = agent.start_call("CA-CAL", env.ctx.profiles.get("bright-smile-dental"), "inbound", "+15550105555")
    r = agent.respond(session, "Can I book a cleaning?")
    assert "call you back" in r.text
    task = env.ctx.store.tasks()[0]
    assert task["priority"] == "high" and "calendar was unavailable" in task["title"]
    assert any(e["type"] == "integration_failure" for e in session.events)


def test_crm_outage_is_retried_then_delivered(env):
    env.ctx.faults.set("crm", True)
    profile = env.ctx.profiles.get("summit-solar")
    event_id = env.ctx.crm.emit(profile, "lead.updated", {"lead": {"phone": "+1555"}})
    env.ctx.crm.flush()
    row = env.ctx.store.outbox()[0]
    assert row["status"] == "pending" and row["attempts"] == 1 and "simulated outage" in row["last_error"]
    assert env.post.calls == []

    env.ctx.faults.set("crm", False)
    env.ctx.crm.retry(event_id)  # the console's Retry button; the worker also retries on its own schedule
    env.ctx.crm.flush()
    assert env.ctx.store.outbox()[0]["status"] == "sent" and len(env.post.calls) == 1

    # A receiver that keeps failing ends up 'dead' after the max attempts, not retried forever
    env.post.status = 500
    env.ctx.crm.emit(profile, "lead.updated", {})
    for _ in range(env.settings.webhook_max_attempts):
        env.ctx.crm.retry(env.ctx.store.outbox()[0]["id"])
        env.ctx.crm.flush()
    assert env.ctx.store.outbox()[0]["status"] == "dead"


def test_ai_failure_hands_off_to_a_human(env):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    env.fake.script = [anthropic.APIConnectionError(request=req)]
    agent = env.ctx.agent
    session, _ = agent.start_call("CA-AI", env.ctx.profiles.get("bright-smile-dental"), "inbound", "+15550106666")
    r = agent.respond(session, "hello")
    assert r.action == "transfer" and r.transfer_phone == "+15550100002"


def test_human_escalation_open_and_closed(env):
    agent, profiles = env.ctx.agent, env.ctx.profiles
    env.fake.script = [resp(text("Putting you through to the front desk now."),
                            tool("transfer_to_human", {"target": "front_desk", "reason": "severe toothache"}, "t"))]
    session, _ = agent.start_call("CA-ESC", profiles.get("bright-smile-dental"), "inbound", "+15550107777")
    r = agent.respond(session, "I have terrible pain and my face is swelling")
    assert r.action == "transfer" and r.transfer_phone == "+15550100002"
    assert agent.finalize("CA-ESC")["outcome"] == "transferred"

    # After hours: no transfer, a high-priority callback task instead
    closed = profiles.get("bright-smile-dental").model_copy(update={"hours_override": "closed"})
    env.fake.script = [resp(tool("transfer_to_human", {"target": "front_desk", "reason": "wants a person"}, "t")),
                       resp(text("The front desk is closed right now, but I've arranged a callback first thing."))]
    session, _ = agent.start_call("CA-ESC2", closed, "inbound", "+15550107778")
    r = agent.respond(session, "Can I talk to a real person?")
    assert r.action == "continue" and "callback" in r.text
    assert env.ctx.store.tasks()[0]["priority"] == "high"


def test_opt_out_during_call_suppresses_future_dials(env):
    lead = add_lead(env, "+15550108888")
    profile = env.ctx.profiles.get("summit-solar")
    env.fake.script = [resp(text("Understood, we won't call you again. Goodbye."), tool("end_call", {}, "e"))]
    dial = env.ctx.dialer.dial(profile, lead["id"])
    session = env.ctx.agent.get(dial["call_id"])
    env.ctx.agent.respond(session, "Please stop calling me")

    # Enforced in code before the model even replied, and the model was told
    assert env.ctx.store.is_dnc("+15550108888", "summit-solar")
    assert "do-not-call" in env.fake.requests[0]["messages"][-1]["content"][0]["text"]
    assert env.ctx.agent.finalize(dial["call_id"])["outcome"] == "opted_out"
    assert "lead.opted_out" in [e["type"] for e in events(env)]

    again = env.ctx.dialer.dial(profile, lead["id"])
    assert not again["allowed"] and any("do-not-call" in r for r in again["reasons"])


def test_outbound_eligibility_checks(env):
    profile = env.ctx.profiles.get("summit-solar")
    dial = lambda lead, mode="simulate": env.ctx.dialer.dial(profile, lead["id"], mode)

    no_consent = add_lead(env, "+15550109001", consent="none")
    assert any("consent" in r for r in dial(no_consent)["reasons"])

    late = add_lead(env, "+15550109002", tz=tz_outside_window())
    assert any("calling window" in r for r in dial(late)["reasons"])

    env.http.post("/api/dnc", json={"phone": "+1 (555) 010-9003", "reason": "national DNC registry"})
    listed = add_lead(env, "+15550109003")
    assert any("do-not-call" in r for r in dial(listed)["reasons"])

    real = add_lead(env, "+15550109004")  # a real dial to a number not on the approved test list
    assert any("approved test number" in r for r in dial(real, "twilio")["reasons"])

    # No answer twice -> retry is scheduled, not immediate; after max attempts the lead is never dialled
    ok = add_lead(env, "+15550109005")
    first = dial(ok)
    assert first["allowed"]
    env.ctx.agent.finalize(first["call_id"], "no-answer")
    assert env.ctx.store.get_lead(ok["id"])["status"] == "no_answer"
    assert any("retry not due" in r for r in dial(ok)["reasons"])

    log = env.http.get("/api/dashboard").json()["dial_log"]
    assert sum(1 for d in log if not d["allowed"]) == 5


# --- Telephony: Twilio ConversationRelay routes --------------------------------------------------
def test_twilio_inbound_conversation_relay(env):
    env.fake.script = [resp(text("We're at 123 Main Street with free parking."))]
    r = env.http.post("/voice/incoming", data={"CallSid": "CA-TW", "From": "+15550110000", "To": "+15550100001"})
    assert "<ConversationRelay" in r.text and "Bright Smile Dental" in r.text

    from app.telephony import relay_token
    token = relay_token("dev", "CA-TW")
    with env.http.websocket_connect(f"/relay?call=CA-TW&token={token}") as ws:
        ws.send_json({"type": "setup", "callSid": "CA-TW"})
        ws.send_json({"type": "prompt", "voicePrompt": "Where are you located?", "last": True})
        tokens = []
        while True:
            msg = ws.receive_json()
            tokens.append(msg["token"])
            if msg["last"]:
                break
    assert "123 Main Street" in "".join(tokens)

    with pytest.raises(WebSocketDisconnect):  # unauthenticated relay sockets are rejected
        with env.http.websocket_connect("/relay?call=CA-TW&token=wrong"):
            pass

    handoff = json.dumps({"action": "transfer", "phone": "+15550100002"})
    r = env.http.post("/voice/relay-ended", data={"CallSid": "CA-TW", "HandoffData": handoff})
    assert "<Dial>+15550100002</Dial>" in r.text


def test_mock_crm_verifies_signatures(env):
    body = json.dumps({"profile_id": "summit-solar", "type": "x"}).encode()
    assert verify(env.ctx.profiles, body, sign("test-solar", body))
    assert not verify(env.ctx.profiles, body, sign("wrong", body))


def test_no_secrets_in_profiles(env):
    for f in env.profiles_dir.glob("*.json"):
        raw = f.read_text().lower()
        assert "sk-ant" not in raw and "cal_live" not in raw and "change-me" not in raw
