"""Live WebSocket fan-out for machine data.

Design rules (each one fixes a freeze seen in production):

* A producer never waits for a viewer. ``publish`` serializes the payload once
  and hands it to every viewer without awaiting any network write.
* Every viewer has its own sender task holding only the *latest* payload. A
  slow phone skips intermediate frames and still always receives the newest
  state; it can never delay other viewers or fall behind.
* A viewer whose send stalls is closed, never silently dropped. The browser
  sees the close and reconnects at once instead of showing "Live connected"
  with frozen numbers.
* Every viewer gets a heartbeat when nothing else was sent recently, so the
  browser can tell a quiet session from a dead connection.

The heartbeat is ``{"type": "heartbeat", "t": <ms>}``. Older frontends ignore
it (it has no machine keys), so it is safe to roll out before the frontend.
"""

import asyncio
import json
import time
from typing import Dict, List, Optional

HEARTBEAT_SECONDS = 5.0
SEND_TIMEOUT_SECONDS = 15.0
CLOSE_TIMEOUT_SECONDS = 2.0
STALLED_CLOSE_CODE = 1011


def encode(payload) -> str:
    # Same wire format Starlette's send_json uses.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def heartbeat_text() -> str:
    return '{"type":"heartbeat","t":%d}' % int(time.time() * 1000)


class _Viewer:
    __slots__ = ("websocket", "pending", "wake", "task")

    def __init__(self, websocket, initial_text: Optional[str]):
        self.websocket = websocket
        self.pending: Optional[str] = initial_text
        self.wake = asyncio.Event()
        if initial_text is not None:
            self.wake.set()
        self.task: Optional[asyncio.Task] = None


class LiveChannel:
    def __init__(self, name: str):
        self.name = name
        self.sockets: List = []
        self._viewers: Dict[int, _Viewer] = {}
        self.published = 0
        self.closed_stalled = 0

    def __len__(self):
        return len(self._viewers)

    def attach(self, websocket, initial_payload=None) -> None:
        """Register a viewer and queue its first full snapshot.

        Runs synchronously, so no publish can slip in between the snapshot
        being built and the viewer being registered.
        """
        self.detach_nowait(websocket)
        viewer = _Viewer(websocket, None if initial_payload is None else encode(initial_payload))
        self._viewers[id(websocket)] = viewer
        self.sockets.append(websocket)
        viewer.task = asyncio.ensure_future(self._pump(viewer))

    def publish(self, payload) -> int:
        """Hand the newest payload to every viewer. Never awaits the network."""
        text = encode(payload)
        for viewer in list(self._viewers.values()):
            viewer.pending = text
            viewer.wake.set()
        self.published += 1
        return len(self._viewers)

    def detach_nowait(self, websocket) -> Optional[_Viewer]:
        viewer = self._viewers.pop(id(websocket), None)
        if websocket in self.sockets:
            self.sockets.remove(websocket)
        if viewer and viewer.task and not viewer.task.done() and viewer.task is not asyncio.current_task():
            viewer.task.cancel()
        return viewer

    async def detach(self, websocket) -> None:
        viewer = self.detach_nowait(websocket)
        if viewer and viewer.task and viewer.task is not asyncio.current_task():
            try:
                await viewer.task
            except (asyncio.CancelledError, Exception):
                pass

    async def _pump(self, viewer: _Viewer) -> None:
        websocket = viewer.websocket
        stalled = False
        try:
            while True:
                try:
                    await asyncio.wait_for(viewer.wake.wait(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    text = heartbeat_text()
                else:
                    viewer.wake.clear()
                    text, viewer.pending = viewer.pending, None
                    if text is None:
                        continue
                try:
                    await asyncio.wait_for(websocket.send_text(text), timeout=SEND_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    stalled = True
                    raise
        except asyncio.CancelledError:
            raise
        except Exception:
            # Disconnected or stalled: close it so the browser reconnects
            # immediately instead of waiting forever on a dead connection.
            if self._viewers.get(id(websocket)) is viewer:
                self.detach_nowait(websocket)
            if stalled:
                self.closed_stalled += 1
                print(f"{self.name}: closed a stalled viewer", flush=True)
            try:
                await asyncio.wait_for(websocket.close(code=STALLED_CLOSE_CODE), timeout=CLOSE_TIMEOUT_SECONDS)
            except Exception:
                pass

    def stats(self) -> dict:
        return {"viewers": len(self._viewers), "published": self.published,
                "closed_stalled": self.closed_stalled}
