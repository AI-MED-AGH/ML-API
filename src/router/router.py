import logging

from src.common.uvicorn_server import UvicornServer

logger = logging.getLogger(__name__)


class MLRouter:
    service_name: str = "MLRouter"
    api_version: str = "1.0.0"

    def __init__(self):
        self._server = UvicornServer(self)

    def initialize(self):
        pass

    def run(self) -> None:
        self._server.run(host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    MLRouter().run()
