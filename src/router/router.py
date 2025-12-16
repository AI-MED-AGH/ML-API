import logging

import aiohttp
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.common.model_routes import ModelRoutes, UnknownModelRoute
from src.common.uvicorn_server import UvicornServer

logger = logging.getLogger(__name__)


class PredictRequest(BaseModel):
    model_data: dict
    model: str


class MLRouter:
    service_name: str = "MLRouter"
    api_version: str = "1.0.0"

    def __init__(self):
        self._server = UvicornServer(self)

        self._server.add_route(
            "/predict",
            self.predict,
            ["POST"],
        )

        self._routes = ModelRoutes()

    def initialize(self):
        pass

    async def predict(self, request: Request) -> JSONResponse:
        body = await request.json()
        pred_request = PredictRequest.model_validate(body)

        try:
            route = await self._routes.get_route(pred_request.model)
        except UnknownModelRoute:
            raise

        logger.debug(f"Forwarding predict to {pred_request.model} on {route}")

        try:
            async with aiohttp.ClientSession(route) as client:
                async with client.post(request.url.path, json=pred_request.model_data) as response:
                    return JSONResponse(
                        content=await response.json(),
                        status_code=response.status,
                )
        except Exception as err:
            # For safety reason, do not return exception details
            logger.exception(err)
            return JSONResponse(
                content={
                    "error": "Unexpected error"
                },
                status_code=500,
            )

    def run(self) -> None:
        self._server.run(host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    MLRouter().run()
