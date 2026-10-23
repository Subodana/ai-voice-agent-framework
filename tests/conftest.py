"""Test harness: a scripted fake Claude client, a fake HTTP poster for webhooks, and a fresh app per test.
No API key, phone line or network is needed."""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from app.agent import CallSummary
from app.config import ROOT, Settings
from app.main import create_app


def text(t):
    return NS(type="text", text=t)


def tool(name, args, id_="t"):
    return NS(type="tool_use", name=name, input=args, id=id_)


def resp(*blocks, stop=None):
    has_tool = any(b.type == "tool_use" for b in blocks)
    return NS(content=list(blocks), stop_reason=stop or ("tool_use" if has_tool else "end_turn"))


def last_tool_result(request) -> dict:
    """The JSON of the most recent tool_result the agent sent back to the model."""
    content = request["messages"][-1]["content"]
    return json.loads(next(b for b in reversed(content) if b.get("type") == "tool_result")["content"])


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for b in self.message.content:
            if b.type == "text":
                for word in b.text.split(" "):
                    yield NS(type="text", text=word + " ")

    def get_final_message(self):
        return self.message


class FakeClient:
    """Each script entry is a response, a callable(request) -> response, or an exception to raise."""

    def __init__(self, script=()):
        self.script = list(script)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create, stream=lambda **kw: FakeStream(self._create(**kw))))
        self.messages = NS(parse=self._parse)

    def _create(self, **kw):
        kw["messages"] = list(kw["messages"])  # snapshot: the session keeps appending to its list
        self.requests.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(kw) if callable(item) else item

    def _parse(self, **kw):
        return NS(parsed_output=CallSummary(summary="Caller handled by the AI agent.", sentiment="positive",
                                            follow_up_needed=False, follow_up_title=""))


class FakePost:
    def __init__(self):
        self.calls = []
        self.status = 200

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "body": json.loads(data), "headers": headers})
        return NS(status_code=self.status)


def tz_in_window(days=("mon", "tue", "wed", "thu", "fri", "sat"), hours=(10, 18)) -> str:
    """An IANA zone where it is currently inside the calling window, so tests don't depend on wall-clock time."""
    now = datetime.now(timezone.utc)
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    for offset in range(-12, 15):
        local = now.timestamp() + offset * 3600
        dt = datetime.fromtimestamp(local, timezone.utc)
        if names[dt.weekday()] in days and hours[0] <= dt.hour < hours[1]:
            return f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}" if offset else "Etc/GMT"
    raise RuntimeError("no zone found")


def tz_outside_window() -> str:
    return tz_in_window(days=("mon", "tue", "wed", "thu", "fri", "sat", "sun"), hours=(1, 6))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DENTAL_WEBHOOK_SECRET", "test-dental")
    monkeypatch.setenv("SOLAR_WEBHOOK_SECRET", "test-solar")
    profiles_dir = tmp_path / "profiles"
    shutil.copytree(ROOT / "profiles", profiles_dir)
    for f in profiles_dir.glob("*.json"):  # open for business during tests regardless of the clock
        data = json.loads(f.read_text())
        data["hours_override"] = "open"
        f.write_text(json.dumps(data))
    settings = Settings(sqlite_path=str(tmp_path / "test.db"), profiles_dir=str(profiles_dir), admin_token="",
                        outbound_test_numbers=["+15550009999"])
    fake, post = FakeClient(), FakePost()
    app = create_app(settings, anthropic_client=fake, http_post=post, start_worker=False)
    return NS(app=app, ctx=app.state.ctx, fake=fake, post=post, http=TestClient(app), settings=settings,
              profiles_dir=profiles_dir)
