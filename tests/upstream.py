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
        # queue-mode model API (POST /jobs, GET /jobs/{id})
        self.job_requests: list = []
        self.job_gets: list[str] = []
        self.job_status = 202
        self.job_payload: object = {"job_id": "abc-123"}
        self.job_body: bytes | None = None
        self.job_get_status = 200
        self.job_get_payload: object = {"job_id": "abc-123", "status": "queued"}
        self.job_get_body: bytes | None = None
        self.job_headers: dict[str, str] = {}
        self._server: TestServer | None = None

    async def _handle(self, request: web.Request) -> web.Response:
        self.requests.append(json.loads(await request.read()))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.body is not None:
            return web.Response(status=self.status, body=self.body, headers=self.headers)
        return web.json_response(self.payload, status=self.status, headers=self.headers)

    async def _submit_job(self, request: web.Request) -> web.Response:
        self.job_requests.append(json.loads(await request.read()))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.job_body is not None:
            return web.Response(status=self.job_status, body=self.job_body, headers=self.job_headers)
        return web.json_response(self.job_payload, status=self.job_status, headers=self.job_headers)

    async def _get_job(self, request: web.Request) -> web.Response:
        self.job_gets.append(request.match_info["job_id"])
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.job_get_body is not None:
            return web.Response(status=self.job_get_status, body=self.job_get_body)
        return web.json_response(self.job_get_payload, status=self.job_get_status)

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/predict", self._handle)
        app.router.add_post("/jobs", self._submit_job)
        app.router.add_get("/jobs/{job_id:.*}", self._get_job)
        self._server = TestServer(app)
        await self._server.start_server()

    async def stop(self) -> None:
        if self._server:
            await self._server.close()

    @property
    def url(self) -> str:
        return str(self._server.make_url("")).rstrip("/")
