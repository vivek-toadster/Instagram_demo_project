"""Redis Pub/Sub and local WebSocket connection management for private chat."""

import asyncio
import json
import logging
from collections import defaultdict

import redis.asyncio as redis
from fastapi import WebSocket

from app.config import REDIS_URL

logger = logging.getLogger(__name__)


class ChatConnectionManager:
    """Fan chat events through Redis, then to sockets connected to this process."""

    CHANNEL = "luma:chat:messages"

    def __init__(self) -> None:
        self._connections: dict[tuple[int, int], set[WebSocket]] = defaultdict(set)
        self._loops: dict[tuple[int, int], asyncio.AbstractEventLoop] = {}
        self._redis: redis.Redis | None = None
        self._pubsub = None
        self._listener_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        """Connect to Redis and start one subscriber for this app process."""
        self._loop = asyncio.get_running_loop()
        if not REDIS_URL:
            logger.info("Redis Pub/Sub disabled because REDIS_URL is not configured")
            return
        try:
            client = redis.from_url(REDIS_URL, decode_responses=True)
            await client.ping()
            pubsub = client.pubsub()
            await pubsub.subscribe(self.CHANNEL)
            self._redis = client
            self._pubsub = pubsub
            self._listener_task = asyncio.create_task(self._listen())
            logger.info("Redis Pub/Sub enabled for private chat")
        except Exception:
            logger.exception("Redis unavailable; using local WebSocket delivery")
            await self._close_redis()

    async def stop(self) -> None:
        """Stop the subscriber and close the Redis connection."""
        if self._listener_task:
            self._listener_task.cancel()
            await asyncio.gather(self._listener_task, return_exceptions=True)
            self._listener_task = None
        await self._close_redis()
        self._loop = None

    async def _close_redis(self) -> None:
        if self._pubsub:
            await self._pubsub.close()
        if self._redis:
            await self._redis.aclose()
        self._pubsub = None
        self._redis = None

    async def _listen(self) -> None:
        """Forward events published by any app process to local browser sockets."""
        try:
            async for item in self._pubsub.listen():
                if item.get("type") != "message":
                    continue
                event = json.loads(item["data"])
                room = tuple(event["room"])
                await self.send_to_room(room, event["payload"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Redis chat subscriber stopped")

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
        """Schedule a Pub/Sub publish from a synchronous HTTP request handler."""
        loop = self._loops.get(room) or self._loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.publish(room, payload), loop)

    async def publish(self, room: tuple[int, int], payload: dict) -> None:
        """Publish globally when Redis is available, otherwise deliver locally."""
        if self._redis:
            event = json.dumps({"room": list(room), "payload": payload})
            try:
                await self._redis.publish(self.CHANNEL, event)
                return
            except Exception:
                logger.exception("Redis publish failed; delivering chat event locally")
        await self.send_to_room(room, payload)

    async def send_to_room(self, room: tuple[int, int], payload: dict) -> None:
        """Send a payload to every open tab in one private conversation."""
        stale: list[WebSocket] = []
        for websocket in tuple(self._connections.get(room, ())):
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            self.disconnect(room, websocket)


chat_manager = ChatConnectionManager()
