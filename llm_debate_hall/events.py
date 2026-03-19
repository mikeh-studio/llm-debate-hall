from __future__ import annotations

import queue
import threading
from collections import defaultdict
from typing import Any


class EventBroker:
    def __init__(self) -> None:
        self._queues: dict[str, list[queue.Queue[dict[str, Any]]]] = defaultdict(list)
        self._lock = threading.Lock()

    async def subscribe(self, session_id: str) -> queue.Queue[dict[str, Any]]:
        event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._queues[session_id].append(event_queue)
        return event_queue

    async def unsubscribe(self, session_id: str, event_queue: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            if session_id not in self._queues:
                return
            self._queues[session_id] = [
                item for item in self._queues[session_id] if item is not event_queue
            ]
            if not self._queues[session_id]:
                self._queues.pop(session_id, None)

    async def publish(self, session_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._queues.get(session_id, []))
        for event_queue in targets:
            event_queue.put(event)
