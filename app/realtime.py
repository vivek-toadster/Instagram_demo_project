"""In-process WebSocket connection management for private chat."""

from collections import defaultdict
import asyncio

from fastapi import WebSocket


class ChatConnectionManager:
    """Track active chat sockets by the two-person conversation room."""

    def __init__(self) -> None:
        self._connections: dict[tuple[int, int], set[WebSocket]] = defaultdict(set)
        self._loops: dict[tuple[int, int], asyncio.AbstractEventLoop] = {}

    async def connect(self, room: tuple[int, int], websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections[room].add(websocket)
        self._loops[room] = asyncio.get_running_loop()

    def disconnect(self, room: tuple[int, int], websocket: WebSocket) -> None:
        connections = self._connections.get(room)
        if not connections:
            return
        connections.discard(websocket)
        if not connections:
            self._connections.pop(room, None)
            self._loops.pop(room, None)

    def publish_from_sync(self, room: tuple[int, int], payload: dict) -> None:
        """Schedule a broadcast from a synchronous HTTP request handler."""
        loop = self._loops.get(room)
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.send_to_room(room, payload), loop)

    async def send_to_room(self, room: tuple[int, int], payload: dict) -> None:
        """Send a payload to every open tab in one private conversation."""
        stale: list[WebSocket] = []
        for websocket in tuple(self._connections.get(room, ())):
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.disconnect(user_id, websocket)


chat_manager = ChatConnectionManager()
