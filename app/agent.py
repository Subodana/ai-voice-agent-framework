"""Conversation engine: one Claude-driven `CallSession` per call, shared by every transport
(Twilio ConversationRelay, Twilio outbound, and the browser simulator).

Business logic that must be reliable - opt-outs, slot validation, qualification scoring,
transfer rules, outcome status - is deterministic code. Claude handles the conversation and
decides when to call these tools.
"""

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Literal

import anthropic
from pydantic import BaseModel

from .compliance import detects_opt_out
from .faults import IntegrationError
from .profiles import Profile, ProfileStore
from .store import Store, now_iso

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
MAX_UNCLEAR = 2  # failed-to-hear turns before offering a person / callback


def build_tools(profile: Profile) -> list[dict]:
    targets = list(profile.transfer_targets)
    qids = [q.id for q in profile.qualification.questions]
    return [
        {"name": "save_lead_details",
         "description": "Save or update the caller's details in the CRM as soon as you learn them. Pass only "
                        "fields you have. Spell back names and emails before saving.",
         "input_schema": {"type": "object", "properties": {
             "name": {"type": "string"}, "email": {"type": "string"},
             "reason": {"type": "string", "description": "Why they called / what they need, in their words"},
             "fields": {"type": "object", "description": f"Other lead fields: {', '.join(profile.lead_fields)}",
                        "additionalProperties": {"type": "string"}}}}},
        {"name": "check_availability",
         "description": "Get open appointment times. Offer at most two or three of the returned times. "
                        "Never offer a time that this tool did not return.",
         "input_schema": {"type": "object", "properties": {
             "date": {"type": "string", "description": "Optional YYYY-MM-DD the caller asked for"}}}},
        {"name": "book_appointment",
         "description": "Book a time the caller accepted. `start` must be copied exactly from check_availability. "
                        "Confirm name (and email if you have it) first.",
         "input_schema": {"type": "object", "properties": {
             "start": {"type": "string"}, "name": {"type": "string"}, "email": {"type": "string"}},
             "required": ["start", "name"]}},
        {"name": "record_qualification",
         "description": "Record answers to the qualification questions as you get them. Returns whether the "
                        "lead qualifies once all answers are in, and the next step.",
         "input_schema": {"type": "object", "properties": {
             "answers": {"type": "object", "description": f"Keys: {', '.join(qids) or 'none configured'}",
                         "additionalProperties": {"type": "string"}}}, "required": ["answers"]}},
        {"name": "transfer_to_human",
         "description": "Transfer the call to a person: when the caller asks for one, on an escalation trigger, "
                        "after repeated misunderstanding, or for anything you cannot handle.",
         "input_schema": {"type": "object", "properties": {
             "target": {"type": "string", "enum": targets}, "reason": {"type": "string"}},
             "required": ["target", "reason"]}},
        {"name": "record_opt_out",
         "description": "The caller does not want further calls. Adds them to the do-not-call list immediately.",
         "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}}},
        {"name": "create_callback_task",
         "description": "Create a follow-up task for the team (callback, question you couldn't answer, message).",
         "input_schema": {"type": "object", "properties": {
             "title": {"type": "string"}, "details": {"type": "string"},
             "priority": {"type": "string", "enum": ["low", "medium", "high"]}}, "required": ["title"]}},
        {"name": "end_call",
         "description": "Hang up after you have said goodbye.",
         "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}}},
    ]


def build_system_prompt(p: Profile) -> str:
    faq = "\n".join(f"- {q}: {a}" for q, a in p.faq.items())
    services = "\n".join(f"- {s}" for s in p.services)
    questions = "\n".join(f"- {q.id}: {q.question}" + (f" (options: {', '.join(q.choices)})" if q.choices else "")
                          for q in p.qualification.questions) or "- none"
    targets = "\n".join(f"- {k}: {t.name}" for k, t in p.transfer_targets.items())
    triggers = "\n".join(f"- {t}" for t in p.escalation_triggers) or "- none"
    return f"""You are {p.agent_name}, an AI voice assistant for {p.company_name} ({p.industry}). You are on a live phone call; everything you write is spoken aloud.

INBOUND calls - goal: {p.inbound_goal}
Answer questions from the facts below, capture the caller's details with save_lead_details, and book with check_availability then book_appointment when they want an appointment.

OUTBOUND calls - you are calling a lead who gave consent. You already introduced yourself as an AI assistant and asked if now is a good time. If it is not, offer a better time and create_callback_task. Otherwise ask the qualification questions one at a time, record answers with record_qualification, and follow its next step: book if qualified, otherwise thank them politely.
Qualification questions:
{questions}

Always:
- One to three short sentences per reply, one question at a time. Plain spoken English: no lists, markdown, emojis or URLs.
- If asked, say plainly that you are an AI assistant.
- Never invent prices, availability, savings, medical/legal/financial advice, or promises. If you don't know, say a team member will follow up and create_callback_task.
- Read back names, emails and appointment times to confirm them. Speech recognition makes mistakes: if an answer doesn't make sense, politely ask again or ask them to spell it.
- If you fail to understand the caller twice in a row, offer to transfer them or take a message.
- If a tool returns an error, follow its instruction; never pretend an action succeeded.
- If the caller asks not to be called or contacted again, call record_opt_out, confirm they won't be called again, say goodbye and call end_call.
- If the caller interrupts you, stop and respond to what they just said; don't repeat everything you were saying.
- Transfer with transfer_to_human for: the caller asking for a person, and these triggers:
{triggers}
- {p.emergency_instructions or "If anyone is in danger, tell them to hang up and call emergency services."}
- Before ending, summarise the next step in one sentence, say goodbye, then call end_call.
{p.persona}

Company facts
Company: {p.company_name}
Hours: {p.hours_text()} ({p.timezone})
Appointment type: {p.calendar.appointment_type}, {p.calendar.duration_minutes} minutes
Services:
{services}
FAQ:
{faq}
Transfer targets:
{targets}
"""


class CallSummary(BaseModel):
    summary: str
    sentiment: Literal["positive", "neutral", "negative"]
    follow_up_needed: bool
    follow_up_title: str


@dataclass
class AgentReply:
    text: str
    action: Literal["continue", "transfer", "hangup"] = "continue"
    transfer_target: str | None = None
    transfer_phone: str | None = None


@dataclass
class CallSession:
    call_id: str
    profile: Profile
    direction: Literal["inbound", "outbound"]
    phone: str
    lead_id: int | None = None
    started_at: str = field(default_factory=now_iso)
    messages: list = field(default_factory=list)
    transcript: list = field(default_factory=list)
    events: list = field(default_factory=list)
    offered_slots: dict = field(default_factory=dict)  # iso start -> datetime
    qual_answers: dict = field(default_factory=dict)
    qualified: bool | None = None
    booking_id: int | None = None
    transferred_to: str | None = None
    opted_out: bool = False
    tasks_created: int = 0
    unclear_turns: int = 0
    pending_notes: list = field(default_factory=list)  # system notes prepended to the next caller turn
    ended: bool = False
    finalized: bool = False

    def say(self, role: str, text: str, **extra) -> None:
        self.transcript.append({"role": role, "text": text, "at": now_iso(), **extra})

    def event(self, kind: str, **detail) -> None:
        self.events.append({"type": kind, "at": now_iso(), **detail})


class VoiceAgent:
    def __init__(self, store: Store, profiles: ProfileStore, calendars: dict, crm, settings,
                 client: anthropic.Anthropic | None = None):
        self.store, self.profiles, self.calendars, self.crm = store, profiles, calendars, crm
        self.settings = settings
        self._client = client
        self.sessions: dict[str, CallSession] = {}
        self._lock = threading.Lock()

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:  # created on first use so the console starts without an API key
            self._client = anthropic.Anthropic()
        return self._client

    # --- lifecycle --------------------------------------------------------------
    def start_call(self, call_id: str, profile: Profile, direction: str, phone: str,
                   lead_id: int | None = None) -> tuple[CallSession, str]:
        lead = self.store.get_lead(lead_id) if lead_id else self.store.find_lead(profile.id, phone)
        session = CallSession(call_id=call_id, profile=profile, direction=direction, phone=phone,
                              lead_id=lead["id"] if lead else None)
        first = (lead.get("name") or "").split(" ")[0] if lead else ""
        if direction == "outbound":
            opening = profile.outbound.opening_line.format(
                first_name=first or "there", agent_name=profile.agent_name, company_name=profile.company_name)
        elif first:
            opening = f"Hi {first}, thanks for calling {profile.company_name}. This is {profile.agent_name}. How can I help today?"
        else:
            opening = profile.greeting

        local = profile.now().strftime("%A %d %B %Y, %H:%M")
        known = json.dumps({k: v for k, v in lead.items() if v and k not in ("profile_id",)}, default=str) if lead else "none"
        # Per-call context lives in the first user turn so the system prompt stays cacheable per profile.
        session.messages = [
            {"role": "user", "content": f"[Call connected. Direction: {direction}. Phone: {phone}. "
                                        f"Local time: {local}. Business is {'OPEN' if profile.is_open() else 'CLOSED'} now. "
                                        f"CRM record: {known}]"},
            {"role": "assistant", "content": opening},
        ]
        session.say("agent", opening)
        with self._lock:
            self.sessions[call_id] = session
        return session, opening

    def get(self, call_id: str) -> CallSession | None:
        return self.sessions.get(call_id)

    # --- turn handling ----------------------------------------------------------------
    def interrupted(self, session: CallSession, heard: str) -> None:
        """The caller barged in. Record what they actually heard and tell the model on the next turn.
        History stays append-only: the earlier assistant turn is never edited."""
        session.event("interruption", heard=heard)
        if session.transcript and session.transcript[-1]["role"] == "agent":
            session.transcript[-1]["interrupted_after"] = heard
        session.pending_notes.append(f"The caller interrupted you; they only heard: \"{heard}\". "
                                     "Do not assume they heard the rest.")

    def no_input(self, session: CallSession) -> AgentReply:
        """Silence or unintelligible audio: reprompt, then offer help, then end with a callback task."""
        session.unclear_turns += 1
        session.event("no_input", count=session.unclear_turns)
        if session.unclear_turns > MAX_UNCLEAR:
            self._task(session, "Call dropped / caller could not be heard", "Line was silent or unclear; call back.")
            text = "I'm sorry, I can't hear you clearly. We'll call you back shortly. Goodbye."
            session.say("agent", text)
            session.ended = True
            return AgentReply(text=text, action="hangup")
        text = ("Sorry, I didn't catch that. Could you say that again?" if session.unclear_turns == 1 else
                "I'm still having trouble hearing you. You can say 'person' to speak to a member of the team.")
        session.say("agent", text)
        return AgentReply(text=text)

    def respond(self, session: CallSession, caller_text: str,
                on_text: Callable[[str], None] | None = None) -> AgentReply:
        caller_text = caller_text.strip()
        if not caller_text:
            return self.no_input(session)
        session.unclear_turns = 0
        session.say("caller", caller_text)

        # Opt-out is enforced in code, not left to the model.
        if detects_opt_out(caller_text) and not session.opted_out:
            self._opt_out(session, "caller asked not to be contacted", source="keyword")
            session.pending_notes.append("The caller asked not to be contacted. They are now on the do-not-call "
                                         "list. Confirm that briefly, say goodbye, and call end_call.")
        content = [{"type": "text", "text": f"[Phone system note: {n}]"} for n in session.pending_notes]
        content.append({"type": "text", "text": caller_text})
        session.pending_notes = []
        session.messages.append({"role": "user", "content": content})

        reply = AgentReply(text="")
        spoken: list[str] = []
        tools = build_tools(session.profile)
        system = [{"type": "text", "text": build_system_prompt(session.profile),
                   "cache_control": {"type": "ephemeral"}}]

        for _ in range(6):  # cap tool round-trips per caller turn
            try:
                response = self._complete(dict(
                    model=self.settings.claude_model, max_tokens=16000, system=system, tools=tools,
                    messages=session.messages, output_config={"effort": self.settings.claude_effort},
                    betas=[FALLBACK_BETA], fallbacks="default"), on_text)
            except anthropic.APIError as e:
                log.exception("Claude call failed for %s", session.call_id)
                return self._failsafe(session, f"AI error: {e.__class__.__name__}")
            if response.stop_reason == "refusal":
                return self._failsafe(session, "model declined to continue")

            # Append the full content unchanged - history stays append-only.
            session.messages.append({"role": "assistant", "content": response.content})
            spoken += [b.text for b in response.content if b.type == "text" and b.text.strip()]
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break
            results = [self._run_tool(session, t, reply) for t in tool_uses]
            session.messages.append({"role": "user", "content": results})
            if reply.action != "continue":
                break

        reply.text = " ".join(spoken).strip() or {
            "transfer": "Let me put you through now.", "hangup": "Thanks for your time. Goodbye."
        }.get(reply.action, "Sorry, could you say that again?")
        session.say("agent", reply.text)
        if reply.action != "continue":
            session.ended = True
        return reply

    def _complete(self, kwargs: dict, on_text):
        if on_text is None:
            return self.client.beta.messages.create(**kwargs)
        # Stream so the phone line can start speaking before the whole reply is generated.
        with self.client.beta.messages.stream(**kwargs) as stream:
            for event in stream:
                if event.type == "text":
                    on_text(event.text)
            return stream.get_final_message()

    def _failsafe(self, session: CallSession, reason: str) -> AgentReply:
        """The AI itself failed: hand the caller to a person if possible, otherwise take a callback."""
        session.event("ai_failure", reason=reason)
        p = session.profile
        target = next(iter(p.transfer_targets), None)
        if target and (p.is_open() or not p.transfer_targets[target].business_hours_only):
            text = "I'm sorry, I'm having a technical problem. Let me put you through to a member of the team."
            session.transferred_to = target
            session.say("agent", text)
            session.ended = True
            return AgentReply(text=text, action="transfer", transfer_target=target,
                              transfer_phone=p.transfer_targets[target].phone)
        self._task(session, "Call back - AI assistant failed mid-call", reason, "high")
        text = "I'm sorry, I'm having a technical problem. Someone from the team will call you back shortly. Goodbye."
        session.say("agent", text)
        session.ended = True
        return AgentReply(text=text, action="hangup")

    # --- tools --------------------------------------------------------------------------
    def _run_tool(self, session: CallSession, block, reply: AgentReply) -> dict:
        args = block.input if isinstance(block.input, dict) else {}
        session.event("tool", name=block.name, args=args)
        try:
            result = self._dispatch(session, block.name, args, reply)
            return {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result, default=str)}
        except ValueError as e:  # invalid input from the caller/model: tell the model so it can re-ask
            session.event("invalid_input", tool=block.name, error=str(e))
            return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": json.dumps({"error": str(e), "instruction": "Explain briefly and ask the caller again."})}
        except Exception as e:
            log.exception("Tool %s failed", block.name)
            return {"type": "tool_result", "tool_use_id": block.id, "content": f"Error: {e}", "is_error": True}

    def _dispatch(self, session: CallSession, name: str, args: dict, reply: AgentReply):
        p = session.profile
        if name == "save_lead_details":
            lead = self._save_lead(session, name=args.get("name"), email=args.get("email"),
                                   reason=args.get("reason"), **(args.get("fields") or {}))
            return {"saved": True, "lead_id": lead["id"]}

        if name == "check_availability":
            day = None
            if args.get("date"):
                try:
                    day = datetime.strptime(args["date"], "%Y-%m-%d").date()
                except ValueError:
                    raise ValueError(f"'{args['date']}' is not a valid date; use YYYY-MM-DD")
            try:
                slots = self.calendars[p.calendar.provider].available_slots(p, day)
            except IntegrationError as e:
                return self._calendar_down(session, str(e))
            session.offered_slots.update({s.isoformat(): s for s in slots})
            if not slots:
                return {"slots": [], "instruction": "No times on that day; offer to check another day."}
            return {"slots": [{"start": s.isoformat(), "spoken": s.strftime("%A %d %B at %I:%M %p").replace(" 0", " ")}
                              for s in slots]}

        if name == "book_appointment":
            start = session.offered_slots.get(args.get("start", ""))
            if not start:
                raise ValueError("That time wasn't offered by check_availability. Check availability again.")
            email = (args.get("email") or "").strip()
            if email and not EMAIL.match(email):
                raise ValueError(f"'{email}' doesn't look like a valid email address")
            lead = self._save_lead(session, name=args["name"], email=email or None)
            try:
                result = self.calendars[p.calendar.provider].book(
                    p, start, {"name": args["name"], "email": email or lead.get("email"), "phone": session.phone,
                               "call_id": session.call_id})
            except IntegrationError as e:
                return self._calendar_down(session, str(e))
            booking = self.store.add_booking(
                profile_id=p.id, lead_id=lead["id"], call_id=session.call_id, start=result["start"].isoformat(),
                end=result["end"].isoformat(), appointment_type=p.calendar.appointment_type,
                provider=p.calendar.provider, external_id=result["external_id"])
            session.booking_id = booking["id"]
            self.store.upsert_lead(p.id, session.phone, status="booked")
            self.crm.emit(p, "booking.created", {"booking": booking, "lead": self.store.get_lead(lead["id"])})
            return {"booked": True, "when": start.strftime("%A %d %B at %I:%M %p"),
                    "instruction": "Confirm the day and time back to the caller."}

        if name == "record_qualification":
            return self._qualify(session, args.get("answers") or {})

        if name == "transfer_to_human":
            target = p.transfer_targets.get(args["target"])
            if not target:
                raise ValueError(f"Unknown transfer target {args['target']}")
            if target.business_hours_only and not p.is_open():
                self._task(session, f"Callback requested for {target.name}", args.get("reason", ""), "high")
                return {"status": "closed", "instruction": f"{target.name} is closed right now. Say so, tell the "
                        "caller a callback has been arranged for when they open, and confirm their number."}
            reply.action, reply.transfer_target, reply.transfer_phone = "transfer", args["target"], target.phone
            session.transferred_to = args["target"]
            session.event("transfer", target=args["target"], reason=args.get("reason"))
            return {"status": "transferring", "to": target.name,
                    "instruction": "Tell the caller in one sentence that you're putting them through."}

        if name == "record_opt_out":
            if not session.opted_out:
                self._opt_out(session, args.get("reason") or "caller request", source="agent")
            return {"opted_out": True}

        if name == "create_callback_task":
            task = self._task(session, args["title"], args.get("details", ""), args.get("priority", "medium"))
            return {"created": True, "task_id": task["id"]}

        if name == "end_call":
            reply.action = "hangup"
            return {"status": "ending call"}
        raise ValueError(f"Unknown tool {name}")

    # --- helpers ----------------------------------------------------------------------
    def _save_lead(self, session: CallSession, **fields) -> dict:
        email = fields.get("email")
        if email and not EMAIL.match(email.strip()):
            raise ValueError(f"'{email}' doesn't look like a valid email address")
        lead = self.store.upsert_lead(session.profile.id, session.phone, **fields)
        session.lead_id = lead["id"]
        self.crm.emit(session.profile, "lead.updated", {"lead": lead, "call_id": session.call_id})
        return lead

    def _task(self, session: CallSession, title: str, details: str = "", priority: str = "medium") -> dict:
        task = self.store.add_task(session.profile.id, session.lead_id, session.call_id, title, details, priority)
        session.tasks_created += 1
        self.crm.emit(session.profile, "task.created", {"task": task, "phone": session.phone})
        return task

    def _opt_out(self, session: CallSession, reason: str, source: str) -> None:
        p = session.profile
        self.store.add_dnc(session.phone, p.id, reason=reason, source=f"call {session.call_id} ({source})")
        lead = self.store.upsert_lead(p.id, session.phone, status="opted_out")
        session.lead_id, session.opted_out = lead["id"], True
        session.event("opt_out", reason=reason, source=source)
        self.crm.emit(p, "lead.opted_out", {"lead": lead, "reason": reason, "call_id": session.call_id})

    def _calendar_down(self, session: CallSession, error: str) -> dict:
        p = session.profile
        session.event("integration_failure", integration="calendar", error=error)
        self._task(session, "Book appointment manually - calendar was unavailable during the call",
                   f"Caller {session.phone} wanted a {p.calendar.appointment_type}. Error: {error}", "high")
        link = f" They can also book online at {p.calendar.booking_link}." if p.calendar.booking_link else ""
        return {"error": "calendar_unavailable",
                "instruction": "Apologise: the booking system isn't responding right now. A team member will call "
                               f"them back to confirm a time.{link} Do not say the appointment is booked."}

    def _qualify(self, session: CallSession, answers: dict) -> dict:
        p, q = session.profile, session.profile.qualification
        known = {x.id: x for x in q.questions}
        session.qual_answers.update({k: str(v) for k, v in answers.items() if k in known})
        missing = [x.id for x in q.questions if x.id not in session.qual_answers]
        if missing:
            return {"recorded": True, "still_needed": missing}

        failed = [r.reason for r in q.rules if not _rule_passes(r, session.qual_answers.get(r.field), known.get(r.field))]
        session.qualified = not failed
        record = {"answers": session.qual_answers, "qualified": session.qualified, "failed": failed}
        lead = self.store.upsert_lead(p.id, session.phone, qualified=int(session.qualified), qualification=record,
                                      status="qualified" if session.qualified else "not_qualified")
        session.lead_id = lead["id"]
        session.event("qualification", **record)
        self.crm.emit(p, "lead.qualified" if session.qualified else "lead.disqualified",
                      {"lead": lead, "call_id": session.call_id})
        if not session.qualified:
            return {"qualified": False, "instruction": "Thank them politely; say this isn't the right fit right now "
                                                       "without listing reasons, then end the call."}
        if q.on_qualified == "transfer":
            return {"qualified": True, "instruction": "Offer to transfer them to a specialist now."}
        return {"qualified": True, "instruction": "Offer to book: call check_availability."}

    # --- after the call ------------------------------------------------------------------
    def finalize(self, call_id: str, telephony_status: str | None = None) -> dict | None:
        with self._lock:
            session = self.sessions.get(call_id)
            if not session or session.finalized:
                return None
            session.finalized = True

        record = {"call_id": call_id, "profile_id": session.profile.id, "direction": session.direction,
                  "lead_id": session.lead_id, "phone": session.phone, "started_at": session.started_at,
                  "ended_at": now_iso(), "transcript": session.transcript, "events": session.events,
                  "transferred_to": session.transferred_to, "booking_id": session.booking_id,
                  "outcome": outcome_for(session, telephony_status)}
        summary = self._summarize(session) if record["outcome"] not in ("no_answer", "busy", "failed") else None
        if summary:
            record.update(summary=summary.summary, sentiment=summary.sentiment)
            if summary.follow_up_needed and session.tasks_created == 0 and not session.opted_out:
                self._task(session, summary.follow_up_title, summary.summary)
        else:
            record["summary"] = fallback_summary(session, record["outcome"])
        self.store.log_call(record)
        if session.direction == "outbound" and session.lead_id:
            # Retry is already scheduled by the dialer; a connected call closes the lead for auto-dialling.
            lead = self.store.get_lead(session.lead_id)
            if record["outcome"] in ("no_answer", "busy", "failed") or lead["status"] in ("new", "no_answer", "busy", "failed"):
                self.store.upsert_lead(session.profile.id, session.phone, status=record["outcome"])
        lead = self.store.get_lead(session.lead_id) if session.lead_id else None
        self.crm.emit(session.profile, "call.completed", {
            **{k: record[k] for k in ("call_id", "direction", "phone", "started_at", "ended_at", "outcome",
                                      "summary", "transferred_to", "booking_id")},
            "sentiment": record.get("sentiment"), "lead": lead,
            "transcript": [{"role": t["role"], "text": t["text"]} for t in session.transcript]})
        with self._lock:
            self.sessions.pop(call_id, None)
        return record

    def _summarize(self, session: CallSession) -> CallSummary | None:
        if len(session.transcript) < 2:
            return None
        transcript = "\n".join(f"{t['role'].upper()}: {t['text']}" for t in session.transcript)
        try:
            response = self.client.messages.parse(
                model=self.settings.claude_model, max_tokens=4000, output_config={"effort": "low"},
                messages=[{"role": "user", "content":
                           f"Summarise this {session.direction} call for {session.profile.company_name}'s CRM in 2-3 "
                           f"sentences a busy team member can act on. Events: "
                           f"{json.dumps([e['type'] for e in session.events])}.\n\n<transcript>\n{transcript}\n</transcript>"}],
                output_format=CallSummary)
            return response.parsed_output
        except (anthropic.APIError, ValueError):
            log.exception("Summary failed for %s", session.call_id)
            return None


def outcome_for(session: CallSession, telephony_status: str | None = None) -> str:
    if telephony_status in ("no-answer", "busy", "failed") and len(session.transcript) <= 1:
        return {"no-answer": "no_answer"}.get(telephony_status, telephony_status)
    if session.opted_out:
        return "opted_out"
    if session.booking_id:
        return "booked"
    if session.transferred_to:
        return "transferred"
    if session.qualified is True:
        return "qualified"
    if session.qualified is False:
        return "not_qualified"
    if any(e["type"] == "ai_failure" for e in session.events):
        return "failed"
    if session.tasks_created:
        return "callback_requested"
    return "info_provided" if len(session.transcript) > 1 else "no_conversation"


def fallback_summary(session: CallSession, outcome: str) -> str:
    """Used when the summary model is unavailable - the call record is never lost."""
    said = [t["text"] for t in session.transcript if t["role"] == "caller"]
    return f"{session.direction.title()} call, outcome: {outcome}. Caller said: " + (" / ".join(said)[:400] or "nothing")


def _rule_passes(rule, raw: str | None, question) -> bool:
    if raw is None:
        return False
    text = raw.strip().lower()
    if question and question.type == "yes_no":
        value = text in ("yes", "y", "true", "yeah", "yep", "correct")
        target = rule.value if isinstance(rule.value, bool) else str(rule.value).lower() in ("yes", "true")
    elif question and question.type == "number" or rule.op in ("gte", "lte"):
        nums = re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))
        if not nums:
            return False
        value, target = float(nums[0]), float(rule.value)
    else:
        value = text
        target = [str(v).lower() for v in rule.value] if isinstance(rule.value, list) else str(rule.value).lower()
    return {"eq": lambda: value == target, "neq": lambda: value != target,
            "gte": lambda: value >= target, "lte": lambda: value <= target,
            "in": lambda: value in target, "not_in": lambda: value not in target}[rule.op]()
