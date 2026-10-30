"""In-process fake ompweb for tests/omp. Offline: all requests go through httpx.MockTransport.

The fake models only the endpoints the omp client/service/watch use. Each instance is one
server; ``factory()`` returns an httpx.AsyncClient factory that routes to it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from cgbot.omp.service import OmpService

COOKIE_VALUE = "omp_session=ok"


@dataclass
class FakeSession:
    id: str
    cwd: str
    running: bool = False
    streaming: bool = False
    queued_steering: list[str] = field(default_factory=list)
    queued_follow_up: list[str] = field(default_factory=list)
    last_text: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_ui: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FakeOmpweb:
    password: str = "pw-secret-123"
    auth: str = "cookie"  # "cookie" | "none" | "basic"
    logged_in: bool = False
    sessions: dict[str, FakeSession] = field(default_factory=dict)
    commands: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    requests: list[tuple[str, str]] = field(default_factory=list)
    fail_path: dict[str, httpx.Response | Exception] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=lambda: {"providers": []})
    dirs: dict[str, list[str]] = field(default_factory=dict)
    projects: list[dict[str, Any]] = field(default_factory=list)
    next_id: int = 1

    # ----- test controls -----------------------------------------------------------------
    def add_session(self, sid: str, cwd: str = "/work", **kwargs: Any) -> FakeSession:
        session = FakeSession(id=sid, cwd=cwd, **kwargs)
        self.sessions[sid] = session
        return session

    def expire(self) -> None:
        self.logged_in = False

    def factory(self) -> Callable[[], httpx.AsyncClient]:
        def make() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.MockTransport(self.handle),
                follow_redirects=False,
                timeout=5.0,
            )

        return make

    # ----- routing -----------------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if path in self.fail_path:
            outcome = self.fail_path[path]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if path == "/api/home":
            return self._home()
        if path == "/api/web-auth/session" and request.method == "POST":
            return self._login(request)
        if not self._authorized():
            return httpx.Response(401, json={"error": "unauthorized"})
        return self._route(request, path)

    def _home(self) -> httpx.Response:
        if self.auth == "none":
            return httpx.Response(200, json={"ok": True})
        if self.auth == "basic":
            return httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="omp"'})
        if self.logged_in:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(401, json={"error": "login"})

    def _authorized(self) -> bool:
        return self.auth == "none" or self.logged_in

    def _login(self, request: httpx.Request) -> httpx.Response:
        if self.auth == "none":
            return httpx.Response(404, json={"error": "not found"})
        body = json.loads(request.content or b"{}")
        if body.get("password") != self.password:
            return httpx.Response(401, json={"error": f"bad password {self.password}"})
        self.logged_in = True
        return httpx.Response(200, json={"ok": True}, headers={"Set-Cookie": COOKIE_VALUE})

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:  # noqa: C901
        method = request.method
        if path == "/api/sessions" and method == "GET":
            return self._sessions()
        if path == "/api/cwd/browse" and method == "GET":
            target = parse_qs(urlsplit(str(request.url)).query).get("path", [""])[0]
            names = self.dirs.get(target, [])
            return httpx.Response(
                200,
                json={
                    "path": target,
                    "parentPath": "/",
                    "directories": [{"name": n} for n in names],
                },
            )
        if path == "/api/projects" and method == "GET":
            return httpx.Response(200, json={"projects": self.projects})
        if path == "/api/cwd/validate" and method == "POST":
            return httpx.Response(200, json={"cwd": json.loads(request.content or b"{}")["cwd"]})
        if path == "/api/agent/new" and method == "POST":
            return self._new(json.loads(request.content or b"{}"))
        if path == "/api/provider-usage" and method == "GET":
            return httpx.Response(200, json=self.usage)
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["api", "sessions"]:
            return self._session_route(request, parts[2:])
        if len(parts) >= 3 and parts[:2] == ["api", "agent"] and method == "POST":
            return self._command(parts[2], json.loads(request.content or b"{}"))
        if len(parts) == 4 and parts[:2] == ["api", "agent"] and parts[3] == "events":
            return self._events(parts[2])
        return httpx.Response(404, json={"error": "no route"})

    def _sessions(self) -> httpx.Response:
        running = [s for s in self.sessions.values() if s.running]
        return httpx.Response(
            200,
            json={
                "sessions": [{"id": s.id, "cwd": s.cwd} for s in self.sessions.values()],
                "runningSessionIds": [s.id for s in running],
                "runningSessions": [{"id": s.id, "cwd": s.cwd} for s in running],
            },
        )

    def _new(self, body: dict[str, Any]) -> httpx.Response:
        sid = f"sess-{self.next_id}"
        self.next_id += 1
        session = self.add_session(sid, cwd=body.get("cwd", "/work"))
        if body.get("type") == "prompt":
            session.running = True
            session.streaming = True
        return httpx.Response(200, json={"sessionId": sid, "success": True})

    def _session_route(self, request: httpx.Request, rest: list[str]) -> httpx.Response:
        sid = rest[0]
        session = self.sessions.get(sid)
        if session is None:
            return httpx.Response(404, json={"error": "no session"})
        if rest[1:] == ["state"]:
            return httpx.Response(
                200, json={"running": session.running, "state": self._state(session)}
            )
        if rest[1:] == ["context"]:
            return self._context(session, request)
        if rest[1:] == ["subagents"]:
            return httpx.Response(200, json={"subagents": []})
        return httpx.Response(404, json={"error": "no route"})

    def _state(self, session: FakeSession) -> dict[str, Any]:
        return {
            "isStreaming": session.streaming,
            "isPromptRunning": False,
            "isBashRunning": False,
            "isCompacting": False,
            "queuedMessages": {
                "steering": list(session.queued_steering),
                "followUp": list(session.queued_follow_up),
            },
            "queuedMessageCount": len(session.queued_steering) + len(session.queued_follow_up),
            "pendingUi": list(session.pending_ui),
        }

    def _context(self, session: FakeSession, request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlsplit(str(request.url)).query)
        limit = int(query.get("limit", ["20"])[0])
        cursor_raw = query.get("cursor", [""])[0]
        offset = json.loads(cursor_raw)["offset"] if cursor_raw else 0
        messages = session.messages
        end = min(offset + limit, len(messages))
        page = messages[offset:end]
        has_more = end < len(messages)
        cursor = (
            {
                "firstEntryId": messages[offset]["id"],
                "lastEntryId": messages[end - 1]["id"],
                "offset": end,
            }
            if has_more and page
            else None
        )
        body = {"context": {"messages": page}, "hasMore": has_more, "cursor": cursor}
        return httpx.Response(200, json=body)

    def _command(self, sid: str, body: dict[str, Any]) -> httpx.Response:
        session = self.sessions.get(sid)
        if session is None:
            return httpx.Response(404, json={"error": "no session"})
        self.commands.append((sid, body))
        kind = body.get("type")
        if kind == "get_last_assistant_text":
            return httpx.Response(200, json={"success": True, "data": session.last_text})
        if kind == "abort_and_restore_queue":
            restored = {
                "steering": list(session.queued_steering),
                "followUp": list(session.queued_follow_up),
            }
            session.queued_steering.clear()
            session.queued_follow_up.clear()
            session.running = False
            session.streaming = False
            return httpx.Response(200, json={"success": True, "data": restored})
        if kind == "get_session_stats":
            return httpx.Response(
                200, json={"success": True, "data": {"tokens": {"total": 1234}, "context": 40}}
            )
        if kind == "compact":
            return httpx.Response(
                200, json={"success": True, "data": {"tokensBefore": 9000, "summary": "ok"}}
            )
        if kind == "prompt" and body.get("streamingBehavior") == "followUp":
            session.queued_follow_up.append(body.get("message", ""))
        return httpx.Response(200, json={"success": True, "data": None})

    def _events(self, sid: str) -> httpx.Response:
        session = self.sessions.get(sid)
        if session is None:
            return httpx.Response(404, json={"error": "no session"})
        lines = []
        for event in session.events:
            lines.append(f"data: {json.dumps(event, ensure_ascii=False)}\n\n")
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content="".join(lines).encode("utf-8"),
        )


def quiet_watchers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop OmpService from starting background watchers, so service calls are deterministic."""
    monkeypatch.setattr(OmpService, "_ensure_watch", lambda self, task: None)
