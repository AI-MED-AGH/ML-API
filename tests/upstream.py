import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestServer


class FakeUpstream:
    """A real local HTTP server standing in for a model container's POST /predict."""

    def __init__(self):
        self.requests: list = []  # parsed JSON bodies received
        self.status = 200
        self.payload = {"ok": True}
        self.body: bytes | None = None  # raw body overrides payload
        self.delay = 0.0
        self.headers: dict[str, str] = {}
        self._server: TestServer | None = None

    async def _handle(self, request: web.Request) -> web.Response:
        self.requests.append(json.loads(await request.read()))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.body is not None:
            return web.Response(status=self.status, body=self.body, headers=self.headers)
        return web.json_response(self.payload, status=self.status, headers=self.headers)

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/predict", self._handle)
        self._server = TestServer(app)
        await self._server.start_server()

    async def stop(self) -> None:
        if self._server:
            await self._server.close()

    @property
    def url(self) -> str:
        return str(self._server.make_url("")).rstrip("/")
