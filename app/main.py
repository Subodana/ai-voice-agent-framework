"""HTTP entry points: Twilio voice routes, browser call simulator, admin API and console."""

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError

from .agent import VoiceAgent
from .config import Settings, settings as default_settings
from .crm import WebhookCRM, verify
from .faults import Faults
from .profiles import ProfileStore
from .scheduling import build_calendars
from .store import Store, now_iso
from .telephony import Dialer, build_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).resolve().parent.parent / "static"


class SimStart(BaseModel):
    profile_id: str
    phone: str


class SimTurn(BaseModel):
    call_id: str
    text: str
    interrupted_heard: str | None = None  # what the caller heard before barging in


class CallRef(BaseModel):
    call_id: str
    status: str | None = None


class DialRequest(BaseModel):
    profile_id: str
    lead_id: int | None = None  # omit to run a campaign pass over the profile's leads
    mode: Literal["simulate", "twilio"] = "simulate"


class LeadIn(BaseModel):
    profile_id: str
    phone: str
    name: str = ""
    email: str = ""
    consent_status: str = "none"
    consent_source: str = ""
    timezone: str = ""


class DNCIn(BaseModel):
    phone: str
    profile_id: str = "*"
    reason: str = "manual"


class FaultIn(BaseModel):
    name: str
    on: bool


def create_app(settings: Settings = default_settings, anthropic_client=None, http_post=None,
               twilio_client=None, start_worker: bool = True) -> FastAPI:
    store = Store(settings.sqlite_path)
    profiles = ProfileStore(settings.profiles_dir)
    faults = Faults()
    crm = WebhookCRM(store, profiles, faults, settings.webhook_max_attempts, post=http_post,
                     self_url=settings.self_url)
    agent = VoiceAgent(store, profiles, build_calendars(store, faults), crm, settings, client=anthropic_client)
    dialer = Dialer(store, profiles, agent, settings, twilio_client)

    @asynccontextmanager
    async def lifespan(_):
        if start_worker:
            crm.start()
        yield

    app = FastAPI(title="Voice Agent Framework", lifespan=lifespan)
    app.state.ctx = SimpleNamespace(store=store, profiles=profiles, faults=faults, crm=crm, agent=agent, dialer=dialer)
    app.include_router(build_router(agent, profiles, settings))

    def admin(x_admin_token: str = Header(default="")):
        if settings.admin_token and x_admin_token != settings.admin_token:
            raise HTTPException(401, "Admin token required")

    def profile_or_404(profile_id: str):
        try:
            return profiles.get(profile_id)
        except KeyError:
            raise HTTPException(404, f"Unknown profile {profile_id}")

    guarded = [Depends(admin)]

    # --- Browser simulator: the same engine as the phone line, without a phone number --------
    @app.post("/api/sim/start", dependencies=guarded)
    def sim_start(body: SimStart):
        call_id = f"SIM-IN-{uuid.uuid4().hex[:8]}"
        _, opening = agent.start_call(call_id, profile_or_404(body.profile_id), "inbound", body.phone)
        return {"call_id": call_id, "reply": opening}

    @app.post("/api/sim/turn", dependencies=guarded)
    def sim_turn(body: SimTurn):
        session = agent.get(body.call_id)
        if not session or session.ended:
            raise HTTPException(404, "Call not found or already ended")
        if body.interrupted_heard is not None:
            agent.interrupted(session, body.interrupted_heard)
        reply = agent.respond(session, body.text)
        target = session.profile.transfer_targets.get(reply.transfer_target or "")
        return {"reply": reply.text, "action": reply.action, "transfer_to": target.name if target else None}

    @app.post("/api/sim/end", dependencies=guarded)
    def sim_end(body: CallRef):
        return {"call": agent.finalize(body.call_id, body.status)}

    # --- Outbound ----------------------------------------------------------------------------
    @app.post("/api/outbound/dial", dependencies=guarded)
    def outbound_dial(body: DialRequest):
        profile = profile_or_404(body.profile_id)
        try:
            if body.lead_id is None:
                return {"results": dialer.run(profile, body.mode)}
            return dialer.dial(profile, body.lead_id, body.mode)
        except KeyError as e:
            raise HTTPException(404, str(e))

    # --- Admin: leads, do-not-call, profiles, integrations -------------------------------------
    @app.post("/api/leads", dependencies=guarded)
    def add_lead(body: LeadIn):
        profile_or_404(body.profile_id)
        data = {k: v for k, v in body.model_dump(exclude={"profile_id", "phone"}).items() if v}
        if body.consent_status != "none":
            data["consent_at"] = now_iso()
        return store.upsert_lead(body.profile_id, body.phone, status="new", attempts=0, **data)

    @app.post("/api/dnc", dependencies=guarded)
    def add_dnc(body: DNCIn):
        store.add_dnc(body.phone, body.profile_id, body.reason, "admin console")
        return {"ok": True}

    @app.delete("/api/dnc", dependencies=guarded)
    def remove_dnc(body: DNCIn):
        store.remove_dnc(body.phone, body.profile_id)
        return {"ok": True}

    @app.get("/api/profiles", dependencies=guarded)
    def list_profiles():
        return [p.model_dump() for p in profiles.profiles.values()]

    @app.put("/api/profiles/{profile_id}", dependencies=guarded)
    def save_profile(profile_id: str, body: dict):
        if body.get("id") != profile_id:
            raise HTTPException(400, "Profile id in body must match the URL")
        try:
            return profiles.save(body).model_dump()
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False))

    @app.post("/api/faults", dependencies=guarded)
    def set_fault(body: FaultIn):
        try:
            faults.set(body.name, body.on)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"active": sorted(faults.active)}

    @app.post("/api/outbox/{event_id}/retry", dependencies=guarded)
    def retry_event(event_id: int):
        crm.retry(event_id)
        return {"ok": True}

    @app.get("/api/dashboard", dependencies=guarded)
    def dashboard(profile_id: str | None = None):
        return {
            "calls": store.calls(profile_id), "leads": store.leads(profile_id), "tasks": store.tasks(profile_id),
            "bookings": store.bookings(profile_id), "outbox": store.outbox(profile_id), "dnc": store.dnc_list(),
            "dial_log": store.dial_log(profile_id), "crm_inbox": store.inbox(), "faults": sorted(faults.active),
        }

    # --- Built-in mock CRM: stands in for n8n/Make/your CRM so the webhook flow is visible end to end --
    @app.post("/mock-crm/webhook")
    async def mock_crm(request: Request):
        body = await request.body()
        store.record_inbox(request.headers.get("X-Voice-Event", ""), body.decode(),
                           verify(profiles, body, request.headers.get("X-Signature", "")))
        return {"received": True}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health():
        return {"ok": True, "model": settings.claude_model, "profiles": list(profiles.profiles)}

    return app


app = create_app()
