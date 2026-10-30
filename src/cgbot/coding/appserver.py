"""Codex app-server JSON-RPC client over one websocket (protocol of codex-cli 0.166.0).

Method names and payload shapes come from the generated schema (``codex app-server
generate-json-schema``), not from DESIGN.md §13.6 which is partly stale. See
``docs/reference/codex-appserver-notes.md`` for the verified behaviour.

Design:
- one reader task owns the socket: responses are matched to requests by ``id``, server
  requests (approvals, user-input, elicitations) are answered inline, notifications are
  normalised into :class:`CodexEvent` and queued for :meth:`CodexAppServerClient.events`;
- a connection failure fails every pending request with :class:`CodexConnectionError` and ends
  the event stream;
- the bearer token is only sent in the handshake header and never appears in logs or errors.

Log lines carry method names, ids and counts only, never message bodies or tokens.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from cgbot.coding.contracts import CodexEvent, SandboxEndpoint, ThreadInfo

log = logging.getLogger(__name__)

MAX_FRAME_BYTES = 16 * 1024 * 1024
DEFAULT_OPEN_TIMEOUT = 10.0
DEFAULT_REQUEST_TIMEOUT = 30.0
CLIENT_INFO = {"name": "cgbot", "version": "0.1.0"}
THREAD_LIST_PAGE = 100

# Codex turn statuses that end a turn; ``inProgress`` is not terminal and is not emitted.
_TERMINAL_TURN_STATUS = frozenset({"completed", "interrupted", "failed"})

_JSONRPC_METHOD_NOT_FOUND = -32601


class CodexError(Exception):
    """Base class for errors raised by this client."""


class CodexConnectionError(CodexError):
    """The websocket could not be opened, or was lost before the request completed."""


class CodexRpcError(CodexError):
    """The app-server answered a request with a JSON-RPC error."""

    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method} failed ({code}): {message}")
        self.method = method
        self.code = code
        self.message = message


class CodexTimeoutError(CodexError):
    """The app-server did not answer a request in time."""


class CodexProtocolError(CodexError):
    """A frame or response did not have the shape the schema promises."""


def _answer_user_input(params: dict[str, Any]) -> dict[str, Any]:
    # YOLO: nobody is at the keyboard. Every question is answered with no answer, so the model
    # sees that the user did not reply instead of the turn hanging on a request.
    questions = params.get("questions") or []
    return {
        "answers": {
            str(q["id"]): {"answers": []} for q in questions if isinstance(q, dict) and "id" in q
        }
    }


# Server -> client requests that cgbot answers. Anything else gets a JSON-RPC method-not-found.
_SERVER_REQUEST_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "item/commandExecution/requestApproval": lambda _p: {"decision": "accept"},
    "item/fileChange/requestApproval": lambda _p: {"decision": "accept"},
    "execCommandApproval": lambda _p: {"decision": "approved"},
    "applyPatchApproval": lambda _p: {"decision": "approved"},
    # Grants nothing: the turn proceeds under the configured sandbox policy.
    "item/permissions/requestApproval": lambda _p: {"permissions": {}, "scope": "turn"},
    "item/tool/requestUserInput": _answer_user_input,
    "mcpServer/elicitation/request": lambda _p: {"action": "decline"},
    # No dynamic tools are registered by cgbot, so a call here is an explicit failure.
    "item/tool/call": lambda _p: {
        "contentItems": [{"type": "inputText", "text": "dynamic tools are not available"}],
        "success": False,
    },
}


def _thread_info(thread: dict[str, Any], *, archived: bool) -> ThreadInfo:
    title = thread.get("name")
    return ThreadInfo(
        thread_id=str(thread["id"]),
        title=title if isinstance(title, str) and title else None,
        updated_at=float(thread["updatedAt"]),
        archived=archived,
    )


def _last_agent_text(items: Any) -> str | None:
    if not isinstance(items, list):
        return None
    for item in reversed(items):
        if isinstance(item, dict) and item.get("type") == "agentMessage":
            text = item.get("text")
            if isinstance(text, str):
                return text
    return None


class CodexAppServerClient:
    """One initialised connection to one app-server. Build it with :func:`connect_codex`."""

    def __init__(
        self,
        ws: ClientConnection,
        *,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self._ws = ws
        self._timeout = request_timeout
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._events: asyncio.Queue[CodexEvent | None] = asyncio.Queue()
        self._turn_text: dict[str, str] = {}
        self._closed = False
        self._reader = asyncio.create_task(self._read_loop(), name="codex-appserver-reader")

    # ------------------------------------------------------------------ transport

    async def _send(self, message: dict[str, Any]) -> None:
        try:
            await self._ws.send(json.dumps(message, separators=(",", ":")))
        except ConnectionClosed as exc:
            raise CodexConnectionError("app-server connection is closed") from exc

    async def _read_loop(self) -> None:
        reason: BaseException | None = None
        try:
            async for raw in self._ws:
                await self._dispatch(raw)
        except ConnectionClosed:
            pass
        except (CodexProtocolError, WebSocketException) as exc:
            reason = exc
            log.warning("codex app-server connection failed: %s", type(exc).__name__)
            await self._ws.close()
        finally:
            self._closed = True
            error = CodexConnectionError("app-server connection closed")
            if reason is not None:
                error.__cause__ = reason
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(error)
            self._pending.clear()
            self._events.put_nowait(None)

    async def _dispatch(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raise CodexProtocolError("binary frame from app-server")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodexProtocolError("malformed JSON frame") from exc
        if not isinstance(msg, dict):
            raise CodexProtocolError("frame is not a JSON object")

        method = msg.get("method")
        if isinstance(method, str) and "id" in msg:
            await self._answer_server_request(msg["id"], method, msg.get("params"))
        elif isinstance(method, str):
            self._on_notification(method, msg.get("params"))
        elif "id" in msg:
            fut = self._pending.get(msg["id"]) if isinstance(msg["id"], int) else None
            if fut is not None and not fut.done():
                fut.set_result(msg)
        else:
            raise CodexProtocolError("frame has neither method nor id")

    async def _answer_server_request(self, rid: Any, method: str, params: Any) -> None:
        handler = _SERVER_REQUEST_HANDLERS.get(method)
        if handler is None:
            log.warning("unhandled app-server request %s; answering method-not-found", method)
            await self._send(
                {
                    "id": rid,
                    "error": {
                        "code": _JSONRPC_METHOD_NOT_FOUND,
                        "message": f"cgbot does not answer {method}",
                    },
                }
            )
            return
        result = handler(params if isinstance(params, dict) else {})
        await self._send({"id": rid, "result": result})

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise CodexConnectionError("app-server connection is closed")
        rid = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"id": rid, "method": method, "params": params})
            try:
                async with asyncio.timeout(self._timeout):
                    msg = await fut
            except TimeoutError as exc:
                raise CodexTimeoutError(f"{method} timed out after {self._timeout}s") from exc
        finally:
            self._pending.pop(rid, None)

        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise CodexRpcError(
                method,
                int(err.get("code", 0)),
                str(err.get("message", "unknown error")),
            )
        result = msg.get("result")
        if not isinstance(result, dict):
            raise CodexProtocolError(f"{method} returned a non-object result")
        return result

    # ------------------------------------------------------------------ notifications

    def _on_notification(self, method: str, params: Any) -> None:
        if not isinstance(params, dict):
            return
        event = self._normalise(method, params)
        if event is not None:
            self._events.put_nowait(event)

    def _normalise(self, method: str, params: dict[str, Any]) -> CodexEvent | None:
        thread_id = params.get("threadId")
        thread_id = thread_id if isinstance(thread_id, str) else None
        if method == "turn/started":
            turn = params.get("turn") or {}
            return CodexEvent("turn_started", thread_id, turn.get("id"), params=params)
        if method == "turn/completed":
            turn = params.get("turn") or {}
            turn_id = turn.get("id")
            status = turn.get("status")
            if status not in _TERMINAL_TURN_STATUS:
                log.warning("turn/completed with non-terminal status %r", status)
                return None
            cached = self._turn_text.pop(turn_id, None) if isinstance(turn_id, str) else None
            text = cached or _last_agent_text(turn.get("items"))
            return CodexEvent(
                "turn_completed", thread_id, turn_id, status=status, text=text, params=params
            )
        if method == "item/completed":
            item = params.get("item") or {}
            turn_id = params.get("turnId")
            text = None
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                text = item["text"]
                if isinstance(turn_id, str):
                    self._turn_text[turn_id] = text
            return CodexEvent("item_completed", thread_id, turn_id, text=text, params=params)
        if method == "thread/tokenUsage/updated":
            return CodexEvent("token_usage", thread_id, params.get("turnId"), params=params)
        if method == "thread/status/changed":
            status = (params.get("status") or {}).get("type")
            return CodexEvent(
                "thread_status",
                thread_id,
                None,
                status=status if isinstance(status, str) else None,
                params=params,
            )
        if method == "error":
            error = params.get("error") or {}
            message = error.get("message")
            return CodexEvent(
                "error",
                thread_id,
                params.get("turnId"),
                text=message if isinstance(message, str) else None,
                params=params,
            )
        return None

    async def events(self) -> AsyncIterator[CodexEvent]:
        """Normalised notifications in arrival order. Ends when the connection closes.

        Intended for a single consumer: events are taken off one shared queue.
        """
        while True:
            event = await self._events.get()
            if event is None:
                # Re-post the sentinel so a second loop over events() also ends.
                self._events.put_nowait(None)
                return
            yield event

    # ------------------------------------------------------------------ CodexClient API

    async def thread_start(self, *, title: str | None = None) -> ThreadInfo:
        result = await self._request("thread/start", {})
        info = _thread_info(result["thread"], archived=False)
        if title is not None:
            await self.thread_rename(info.thread_id, title)
        return ThreadInfo(
            thread_id=info.thread_id,
            title=title if title is not None else info.title,
            updated_at=info.updated_at,
            archived=False,
        )

    async def thread_resume(self, thread_id: str) -> ThreadInfo:
        result = await self._request("thread/resume", {"threadId": thread_id})
        return _thread_info(result["thread"], archived=False)

    async def _list_threads(self, *, archived: bool) -> list[ThreadInfo]:
        threads: list[ThreadInfo] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"archived": archived, "limit": THREAD_LIST_PAGE}
            if cursor is not None:
                params["cursor"] = cursor
            result = await self._request("thread/list", params)
            threads.extend(_thread_info(t, archived=archived) for t in result["data"])
            cursor = result.get("nextCursor")
            if not cursor:
                return threads

    async def thread_list(self, *, include_archived: bool = False) -> list[ThreadInfo]:
        # The app-server filter is exclusive: archived=true returns only archived threads,
        # archived=false only active ones. Including both takes two listings.
        threads = await self._list_threads(archived=False)
        if include_archived:
            threads.extend(await self._list_threads(archived=True))
        return threads

    async def thread_read(
        self, thread_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> tuple[list[dict[str, object]], str | None]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "limit": limit,
            "sortDirection": "asc",
            "itemsView": "full",
        }
        if cursor is not None:
            params["cursor"] = cursor
        result = await self._request("thread/turns/list", params)
        turns = [t for t in result["data"] if isinstance(t, dict)]
        next_cursor = result.get("nextCursor")
        return turns, next_cursor if isinstance(next_cursor, str) and next_cursor else None

    async def thread_rename(self, thread_id: str, title: str) -> None:
        await self._request("thread/name/set", {"threadId": thread_id, "name": title})

    async def thread_archive(self, thread_id: str) -> None:
        await self._request("thread/archive", {"threadId": thread_id})

    async def thread_delete(self, thread_id: str) -> None:
        await self._request("thread/delete", {"threadId": thread_id})

    async def thread_fork(self, thread_id: str) -> ThreadInfo:
        result = await self._request("thread/fork", {"threadId": thread_id})
        return _thread_info(result["thread"], archived=False)

    async def thread_compact(self, thread_id: str) -> None:
        await self._request("thread/compact/start", {"threadId": thread_id})

    async def turn_start(self, thread_id: str, text: str) -> str:
        result = await self._request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": text}]},
        )
        turn_id = (result.get("turn") or {}).get("id")
        if not isinstance(turn_id, str):
            raise CodexProtocolError("turn/start returned no turn id")
        return turn_id

    async def turn_steer(self, thread_id: str, turn_id: str, text: str) -> None:
        await self._request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": text}],
            },
        )

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> None:
        await self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def close(self) -> None:
        if self._closed and self._reader.done():
            return
        self._closed = True
        await self._ws.close()
        self._reader.cancel()
        try:
            await self._reader
        except asyncio.CancelledError:
            pass
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CodexConnectionError("app-server connection closed"))
        self._pending.clear()


async def connect_codex(
    endpoint: SandboxEndpoint,
    *,
    open_timeout: float = DEFAULT_OPEN_TIMEOUT,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> CodexAppServerClient:
    """Open the websocket with the bearer token, run ``initialize`` and return a ready client.

    This is a :data:`cgbot.coding.contracts.CodexConnector`. The token is sent only in the
    handshake header; failures never include it.
    """
    try:
        ws = await connect(
            endpoint.url,
            additional_headers=Headers({"Authorization": f"Bearer {endpoint.ws_token}"}),
            open_timeout=open_timeout,
            max_size=MAX_FRAME_BYTES,
        )
    except InvalidStatus as exc:
        raise CodexConnectionError(
            f"app-server rejected the websocket handshake (HTTP {exc.response.status_code})"
        ) from exc
    except (OSError, TimeoutError, WebSocketException) as exc:
        raise CodexConnectionError(f"cannot reach app-server: {type(exc).__name__}") from exc

    client = CodexAppServerClient(ws, request_timeout=request_timeout)
    try:
        await client._request(
            "initialize",
            {"clientInfo": CLIENT_INFO, "capabilities": {"experimentalApi": False}},
        )
        await client._send({"method": "initialized"})
    except BaseException:
        await client.close()
        raise
    return client
