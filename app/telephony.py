"""Twilio transport: inbound and outbound calls over ConversationRelay, plus the outbound dialer.

ConversationRelay (Twilio-native) handles speech-to-text, text-to-speech and barge-in detection;
this module streams Claude's reply tokens to it over a WebSocket and turns its `interrupt`
events into context for the next turn.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from .agent import AgentReply, VoiceAgent
from .compliance import check_eligibility
from .profiles import Profile, ProfileStore
from .store import Store, now_iso

log = logging.getLogger(__name__)


def twiml(*verbs: str) -> Response:
    body = '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(verbs) + "</Response>"
    return Response(content=body, media_type="application/xml")


class Dialer:
    """Outbound calls. Every attempt passes the compliance checks first and is written to the dial log."""

    def __init__(self, store: Store, profiles: ProfileStore, agent: VoiceAgent, settings, twilio_client=None):
        self.store, self.profiles, self.agent, self.settings = store, profiles, agent, settings
        self._twilio = twilio_client

    @property
    def twilio(self):
        if self._twilio is None:
            from twilio.rest import Client
            self._twilio = Client(self.settings.twilio_account_sid, self.settings.twilio_auth_token)
        return self._twilio

    def dial(self, profile: Profile, lead_id: int, mode: str = "simulate") -> dict:
        lead = self.store.get_lead(lead_id)
        if not lead or lead["profile_id"] != profile.id:
            raise KeyError("lead not found for this profile")
        reasons = check_eligibility(self.store, profile, lead, mode, self.settings.outbound_test_numbers)
        caller_id = profile.outbound.caller_id or self.settings.twilio_from_number
        if mode == "twilio" and not (self.settings.twilio_account_sid and caller_id and self.settings.public_base_url):
            reasons.append("Twilio not configured (TWILIO_ACCOUNT_SID, caller ID, PUBLIC_BASE_URL)")
        self.store.log_dial(profile.id, lead_id, lead["phone"], not reasons, reasons, mode)
        if reasons:
            return {"lead_id": lead_id, "phone": lead["phone"], "allowed": False, "reasons": reasons}

        attempt = (lead["attempts"] or 0) + 1
        waits = profile.outbound.retry_after_minutes or [60]
        retry_at = datetime.now(timezone.utc) + timedelta(minutes=waits[min(attempt, len(waits)) - 1])
        self.store.upsert_lead(profile.id, lead["phone"], attempts=attempt, last_attempt_at=now_iso(),
                               next_attempt_at=retry_at.isoformat(timespec="seconds"))

        if mode == "simulate":
            call_id = f"SIM-OUT-{uuid.uuid4().hex[:8]}"
            _, opening = self.agent.start_call(call_id, profile, "outbound", lead["phone"], lead_id)
            return {"lead_id": lead_id, "phone": lead["phone"], "allowed": True, "call_id": call_id, "reply": opening}

        base = self.settings.public_base_url
        call = self.twilio.calls.create(
            to=lead["phone"], from_=caller_id, url=f"{base}/voice/outbound", method="POST",
            status_callback=f"{base}/voice/status", status_callback_method="POST",
            status_callback_event=["completed"])
        # Session exists before the callee answers so a no-answer/busy status can still be logged.
        self.agent.start_call(call.sid, profile, "outbound", lead["phone"], lead_id)
        return {"lead_id": lead_id, "phone": lead["phone"], "allowed": True, "call_id": call.sid}

    def run(self, profile: Profile, mode: str = "simulate", limit: int = 10) -> list[dict]:
        """Campaign pass: try every lead of this profile; ineligible ones are skipped with reasons."""
        results = []
        for lead in self.store.leads(profile.id)[:limit]:
            results.append(self.dial(profile, lead["id"], mode))
        return results


def relay_token(secret: str, call_id: str) -> str:
    return hmac.new(secret.encode(), call_id.encode(), hashlib.sha256).hexdigest()[:32]


def build_router(agent: VoiceAgent, profiles: ProfileStore, settings) -> APIRouter:
    router = APIRouter()
    secret = settings.twilio_auth_token or settings.admin_token or "dev"

    async def twilio_form(request: Request) -> dict:
        form = dict(await request.form())
        if settings.twilio_auth_token:
            from twilio.request_validator import RequestValidator
            url = settings.public_base_url + request.url.path + (f"?{request.url.query}" if request.url.query else "")
            if not RequestValidator(settings.twilio_auth_token).validate(
                    url, form, request.headers.get("X-Twilio-Signature", "")):
                raise HTTPException(403, "Invalid Twilio signature")
        return form

    def relay_twiml(call_id: str, opening: str, profile: Profile) -> Response:
        ws_url = settings.public_base_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url += f"/relay?call={call_id}&token={relay_token(secret, call_id)}"
        attrs = {"url": ws_url, "welcomeGreeting": opening, "dtmfDetection": "true"}
        if settings.tts_provider:
            attrs["ttsProvider"] = settings.tts_provider
        if settings.tts_voice:
            attrs["voice"] = settings.tts_voice
        relay = "<ConversationRelay " + " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items()) + " />"
        return twiml(f'<Connect action="{settings.public_base_url}/voice/relay-ended">{relay}</Connect>')

    @router.post("/voice/incoming")
    def incoming(form: dict = Depends(twilio_form)):
        profile = profiles.for_number(form.get("To", ""), settings.default_profile)
        _, opening = agent.start_call(form["CallSid"], profile, "inbound", form.get("From", "unknown"))
        return relay_twiml(form["CallSid"], opening, profile)

    @router.post("/voice/outbound")
    def outbound(form: dict = Depends(twilio_form)):
        session = agent.get(form["CallSid"])
        if not session:
            return twiml("<Hangup/>")
        return relay_twiml(session.call_id, session.transcript[0]["text"], session.profile)

    @router.post("/voice/relay-ended")
    def relay_ended(form: dict = Depends(twilio_form)):
        try:
            handoff = json.loads(form.get("HandoffData") or "{}")
        except ValueError:
            handoff = {}
        if handoff.get("action") == "transfer" and handoff.get("phone"):
            return twiml(f"<Dial>{escape(handoff['phone'])}</Dial>")
        return twiml("<Hangup/>")

    @router.post("/voice/status")
    def status(form: dict = Depends(twilio_form)):
        if form.get("CallStatus") in {"completed", "busy", "failed", "no-answer", "canceled"}:
            agent.finalize(form["CallSid"], form.get("CallStatus"))
        return Response(status_code=204)

    @router.websocket("/relay")
    async def relay(ws: WebSocket):
        call_id = ws.query_params.get("call", "")
        if not hmac.compare_digest(relay_token(secret, call_id), ws.query_params.get("token", "")):
            await ws.close(code=1008)
            return
        await ws.accept()
        session = agent.get(call_id)
        loop = asyncio.get_running_loop()
        outq: asyncio.Queue = asyncio.Queue()
        turn = {"id": 0, "muted": -1}  # tokens of an interrupted turn are dropped, not spoken
        lock = asyncio.Lock()

        async def sender():
            while True:
                msg = await outq.get()
                await ws.send_json(msg)

        def push(msg: dict) -> None:
            loop.call_soon_threadsafe(outq.put_nowait, msg)

        async def handle(text: str):
            async with lock:
                turn["id"] += 1
                me = turn["id"]

                streamed = []

                def on_text(token: str):
                    streamed.append(token)
                    if turn["muted"] != me:
                        push({"type": "text", "token": token, "last": False})

                reply: AgentReply = await asyncio.to_thread(agent.respond, session, text, on_text)
                if not streamed and turn["muted"] != me:  # canned replies (no-input, failsafe) aren't streamed
                    push({"type": "text", "token": reply.text, "last": False})
                push({"type": "text", "token": "", "last": True})
                if reply.action != "continue":
                    await asyncio.sleep(min(2 + len(reply.text) / 15, 10))  # let the goodbye finish playing
                    handoff = {"action": reply.action, "phone": reply.transfer_phone, "target": reply.transfer_target}
                    push({"type": "end", "handoffData": json.dumps(handoff)})

        send_task = asyncio.create_task(sender())
        try:
            while True:
                msg = await ws.receive_json()
                kind = msg.get("type")
                if kind == "setup":
                    session = session or agent.get(msg.get("callSid", ""))
                    if not session:
                        await ws.close()
                        return
                elif kind == "prompt" and msg.get("last", True):
                    asyncio.create_task(handle(msg.get("voicePrompt", "")))
                elif kind == "interrupt":
                    turn["muted"] = turn["id"]
                    agent.interrupted(session, msg.get("utteranceUntilInterrupted", ""))
                elif kind == "dtmf" and msg.get("digit") == "0":
                    asyncio.create_task(handle("I'd like to speak to a person, please."))
                elif kind == "error":
                    log.error("ConversationRelay error on %s: %s", call_id, msg.get("description"))
        except WebSocketDisconnect:
            pass
        finally:
            send_task.cancel()
            if session:
                agent.finalize(session.call_id)  # idempotent; the status callback may also call it

    return router
