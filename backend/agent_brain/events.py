"""In-process pub/sub so the dashboard can watch a troubleshooting session live."""

import asyncio

_subscribers = {}


def subscribe(session_id):
    queue = asyncio.Queue(maxsize=500)
    _subscribers.setdefault(session_id, set()).add(queue)
    return queue


def unsubscribe(session_id, queue):
    queues = _subscribers.get(session_id)
    if queues:
        queues.discard(queue)
        if not queues:
            _subscribers.pop(session_id, None)


def publish(session_id, event):
    for queue in list(_subscribers.get(session_id, ())):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass
