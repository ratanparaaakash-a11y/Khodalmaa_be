import asyncio
import json
import re
import unittest
from unittest.mock import patch

import live


class FakeSocket:
    def __init__(self, delay=0.0, block=None, fail=False):
        self.delay = delay
        self.block = block
        self.fail = fail
        self.messages = []
        self.closed_with = None

    async def send_text(self, text):
        if self.fail:
            raise RuntimeError("client disconnected")
        if self.block is not None:
            await self.block.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        self.messages.append(json.loads(text))

    async def close(self, code=1000):
        self.closed_with = code

    def data(self):
        return [m for m in self.messages if m.get("type") != "heartbeat"]


def old_frontend_accepts(message):
    """Python copy of the deployed frontend's machine-key check (bundle 356)."""
    if not isinstance(message, dict):
        return False
    for key in message:
        if key.startswith("__"):
            continue
        if not re.fullmatch(r"machine\d+", key, re.I):
            return False
    return True


async def settle(seconds=0.05):
    await asyncio.sleep(seconds)


class LiveChannelTests(unittest.TestCase):
    def test_publish_never_waits_for_a_stuck_viewer(self):
        async def exercise():
            channel = live.LiveChannel("test")
            stuck = FakeSocket(block=asyncio.Event())
            channel.attach(stuck, {"machine1": [0], "__full_snapshot": True})
            await settle()
            loop = asyncio.get_running_loop()
            started = loop.time()
            for value in range(50):
                channel.publish({"machine1": [value], "__full_snapshot": True})
            self.assertLess(loop.time() - started, 0.05)
            await channel.detach(stuck)
        asyncio.run(exercise())

    def test_slow_viewer_cannot_delay_fast_viewer_and_ends_on_latest(self):
        async def exercise():
            channel = live.LiveChannel("test")
            fast, slow = FakeSocket(), FakeSocket(delay=0.2)
            channel.attach(fast, {"machine1": [0], "__full_snapshot": True})
            channel.attach(slow, {"machine1": [0], "__full_snapshot": True})
            await settle()
            for value in range(1, 21):
                channel.publish({"machine1": [value], "__full_snapshot": True})
                await asyncio.sleep(0.01)
            await settle(0.6)
            self.assertEqual(fast.data()[-1]["machine1"], [20])
            self.assertEqual(slow.data()[-1]["machine1"], [20])
            # The slow viewer skipped stale frames instead of building a backlog.
            self.assertLess(len(slow.data()), len(fast.data()))
            await channel.detach(fast)
            await channel.detach(slow)
        asyncio.run(exercise())

    def test_stalled_viewer_is_closed_not_silently_dropped(self):
        async def exercise():
            channel = live.LiveChannel("test")
            stalled = FakeSocket(block=asyncio.Event())
            with patch.object(live, "SEND_TIMEOUT_SECONDS", 0.1):
                channel.attach(stalled, {"machine1": [0], "__full_snapshot": True})
                await settle(0.3)
            self.assertEqual(stalled.closed_with, live.STALLED_CLOSE_CODE)
            self.assertEqual(channel.sockets, [])
            self.assertEqual(len(channel), 0)
            self.assertEqual(channel.closed_stalled, 1)
        asyncio.run(exercise())

    def test_disconnected_viewer_is_removed(self):
        async def exercise():
            channel = live.LiveChannel("test")
            gone = FakeSocket(fail=True)
            channel.attach(gone, {"machine1": [0], "__full_snapshot": True})
            await settle()
            self.assertEqual(channel.sockets, [])
            self.assertEqual(channel.publish({"machine1": [1]}), 0)
        asyncio.run(exercise())

    def test_quiet_connection_gets_heartbeats_old_frontend_ignores(self):
        async def exercise():
            channel = live.LiveChannel("test")
            socket = FakeSocket()
            with patch.object(live, "HEARTBEAT_SECONDS", 0.05):
                channel.attach(socket, {"machine1": [0], "__full_snapshot": True})
                await settle(0.3)
                await channel.detach(socket)
            beats = [m for m in socket.messages if m.get("type") == "heartbeat"]
            self.assertGreaterEqual(len(beats), 3)
            self.assertTrue(all(set(b) == {"type", "t"} for b in beats))
            self.assertTrue(all(not old_frontend_accepts(b) for b in beats))
            self.assertTrue(old_frontend_accepts(socket.messages[0]))
        asyncio.run(exercise())

    def test_initial_snapshot_arrives_before_newer_update(self):
        async def exercise():
            channel = live.LiveChannel("test")
            release = asyncio.Event()
            socket = FakeSocket(block=release)
            channel.attach(socket, {"machine1": ["old"], "__full_snapshot": True})
            await settle()
            channel.publish({"machine1": ["new"], "__full_snapshot": True})
            self.assertEqual(socket.messages, [])
            release.set()
            await settle()
            self.assertEqual([m["machine1"] for m in socket.data()], [["old"], ["new"]])
            await channel.detach(socket)
        asyncio.run(exercise())

    def test_reattach_same_socket_replaces_previous_sender(self):
        async def exercise():
            channel = live.LiveChannel("test")
            socket = FakeSocket()
            channel.attach(socket, {"machine1": [1]})
            channel.attach(socket, {"machine1": [2]})
            await settle()
            self.assertEqual(channel.sockets, [socket])
            self.assertEqual(len(channel), 1)
            await channel.detach(socket)
            self.assertEqual(channel.sockets, [])
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
