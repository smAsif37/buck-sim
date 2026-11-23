"""Helper: run the real ASGI app on a real socket (uvicorn in a thread) for tests."""

from __future__ import annotations

import threading
import time

import uvicorn

from buck_sim.api import create_app
from buck_sim.worker import Worker


class LiveServer:
    def __init__(self, settings, ai_client=None):
        self.app = create_app(settings, ai_client=ai_client)
        config = uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.worker = Worker(settings, self.app.state.store, self.app.state.engine, "test-worker")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)
