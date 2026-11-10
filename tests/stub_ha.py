"""A stand-in for Home Assistant's WebSocket API.

Just enough of it to drive the dashboard in a test: the auth handshake,
the compressed entity subscription, and the history query.  The shapes
are the ones Home Assistant really uses - in particular the diff, which
carries only the fields that changed and has to be merged into the state
the dashboard already holds, and the snapshot, which arrives under 'a'.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

HA_VERSION = "2025.2.5"


def _now() -> float:
    return time.time()


class StubHomeAssistant:
    """A Home Assistant that says what the test tells it to say."""

    def __init__(
        self,
        token: str = "stub-token",
        states: dict[str, dict] | None = None,
        history: dict[str, list] | None = None,
    ) -> None:
        self.token = token
        self.states: dict[str, dict] = dict(states or {})
        self.history: dict[str, list] = dict(history or {})
        self._server = None
        self._socket = None
        self._ready = False
        self.port = 0

    # ── the life of the server ──────────────────────────────────────

    async def start(self) -> int:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # ── the conversation ────────────────────────────────────────────

    async def _handler(self, websocket: Any) -> None:
        self._socket = websocket
        self._ready = False
        try:
            if not await self._handshake(websocket):
                return
            await self._loop(websocket)
        except ConnectionClosed:
            # The browser closed the page.  That is not a failure.
            pass
        finally:
            self._socket = None
            self._ready = False

    async def _handshake(self, websocket: Any) -> bool:
        await websocket.send(
            json.dumps({"type": "auth_required", "ha_version": HA_VERSION})
        )
        message = json.loads(await websocket.recv())
        if message.get("type") != "auth" or message.get("access_token") != self.token:
            await websocket.send(
                json.dumps({"type": "auth_invalid", "message": "Invalid access token"})
            )
            await websocket.close()
            return False
        await websocket.send(json.dumps({"type": "auth_ok", "ha_version": HA_VERSION}))
        return True

    async def _loop(self, websocket: Any) -> None:
        async for raw in websocket:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await self._command(websocket, message)

    async def _command(self, websocket: Any, message: dict) -> None:
        kind = message.get("type")
        if kind == "subscribe_entities":
            await websocket.send(
                json.dumps(
                    {
                        "id": message.get("id"),
                        "type": "result",
                        "success": True,
                        "result": None,
                    }
                )
            )
            # The subscription is answered with the state of everything it
            # asked for, all at once, under 'a'.
            await websocket.send(
                json.dumps({"type": "event", "event": {"a": self._snapshot()}})
            )
            self._ready = True
        elif kind == "history/history_during_period":
            entities = message.get("entity_ids", [])
            result = {
                entity: list(self.history.get(entity, [])) for entity in entities
            }
            await websocket.send(
                json.dumps(
                    {
                        "id": message.get("id"),
                        "type": "result",
                        "success": True,
                        "result": result,
                    }
                )
            )

    # ── the state ───────────────────────────────────────────────────

    def _snapshot(self) -> dict:
        now = _now()
        return {
            entity: {
                "s": state.get("s", "unknown"),
                "a": dict(state.get("a", {})),
                "c": state.get("c", "unknown"),
                "lc": now,
            }
            for entity, state in self.states.items()
        }

    async def push_diff(
        self,
        entity_id: str,
        state: str | None = None,
        attributes: dict | None = None,
    ) -> None:
        """Send a change the way Home Assistant sends one: as a diff.

        Only the fields that changed are in it, so the dashboard has to
        merge it into the state it already holds rather than replace it.
        """
        # Wait for the subscription to have been answered, so that the
        # change cannot arrive before the state it changes.
        for _ in range(200):
            if self._ready:
                break
            await asyncio.sleep(0.05)
        if self._socket is None:
            raise RuntimeError("nobody is connected")
        if not self._ready:
            raise RuntimeError("the subscription was never answered")
        change: dict[str, Any] = {}
        if state is not None:
            change["s"] = state
            self.states.setdefault(entity_id, {})["s"] = state
        if attributes:
            change["a"] = attributes
            self.states.setdefault(entity_id, {}).setdefault("a", {}).update(attributes)
        await self._socket.send(
            json.dumps(
                {"type": "event", "event": {"c": {entity_id: {"+": change}}}}
            )
        )
