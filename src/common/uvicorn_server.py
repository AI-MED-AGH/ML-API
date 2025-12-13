import logging
from contextlib import asynccontextmanager
from typing import Optional, Callable, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.common.response_types import ErrorResponse

logger = logging.getLogger(__name__)


class UvicornServer:
    def __init__(self, service):
        self.service = service
        self._app: Optional[FastAPI] = None
        self._custom_routes: list = []

    def add_route(
        self,
        path: str,
        endpoint: Callable,
        methods: Optional[list] = None,
        response_model: Optional[BaseModel] = None,
        tags: Optional[list] = None,
        summary: Optional[str] = None,
        description: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """
        Add a custom route to the API.

        This method allows you to add additional endpoints beyond /predict.
        Routes must be added before accessing the `app` property or calling `run()`.

        Args:
            path: The URL path for the endpoint (e.g., "/analyze", "/batch")
            endpoint: The async function to handle requests
            methods: HTTP methods (default: ["GET"])
            response_model: Optional Pydantic model for response validation
            tags: OpenAPI tags for documentation
            summary: Short summary for OpenAPI docs
            description: Detailed description for OpenAPI docs
            **kwargs: Additional arguments passed to FastAPI's add_api_route

        Example:
            class MyController(MLController):
                def __init__(self):
                    super().__init__()
                    self.add_route("/batch", self.batch_predict, methods=["POST"])

                async def batch_predict(self, request: Request):
                    data = await request.json()
                    results = [await self.predict(item) for item in data["items"]]
                    return {"results": results}
        """
        if self._app is not None:
            raise RuntimeError(
                "Cannot add routes after the app has been created. "
                "Add routes in __init__ before accessing .app or calling .run()"
            )

        self._custom_routes.append({
            "path": path,
            "endpoint": endpoint,
            "methods": methods or ["GET"],
            "response_model": response_model,
            "tags": tags or ["Custom"],
            "summary": summary,
            "description": description,
            **kwargs,
        })

    def _create_app(self) -> FastAPI:
        """Create and configure the FastAPI application."""

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            logger.info(f"Initializing router...")
            try:
                self.service.initialize()
            except Exception as e:
                logger.error(f"Failed to initialize: {e}")
                raise
            logger.info(f"Router initialized successfully")
            yield
            logger.info(f"Shutting down...")

        app = FastAPI(
            title="ML-Api router",
            description="A router redirecting request from end user to the MLController servers.",
            version=self.service.api_version,
            lifespan=lifespan,
            docs_url="/docs",
            redoc_url="/redoc",
        )

        @app.exception_handler(Exception)
        async def global_exception_handler(request: Request, exc: Exception):
            logger.exception(f"Unhandled exception: {exc}")
            return JSONResponse(
                status_code=500,
                content=ErrorResponse(
                    error=str(exc),
                    error_type=type(exc).__name__,
                ).model_dump(),
            )

        # Root endpoint
        @app.get("/", tags=["Info"])
        async def root():
            """API information endpoint."""
            return {
                "name": self.service.service_name,
                "version": self.service.api_version,
                "endpoint": "/predict",
            }

        # Register custom routes
        for route_config in self._custom_routes:
            endpoint = route_config.pop("endpoint")
            path = route_config.pop("path")
            methods = route_config.pop("methods", ["GET"])

            app.add_api_route(
                path,
                endpoint,
                methods=methods,
                **route_config,
            )
            logger.debug(f"Registered custom route: {methods} {path}")

        return app

    @property
    def app(self) -> FastAPI:
        """Get or create the FastAPI application instance."""
        if self._app is None:
            self._app = self._create_app()
        return self._app

    def run(
        self,
        host: str,
        port: int,
        reload: bool = False,
        **uvicorn_kwargs,
    ) -> None:
        """
        Run the FastAPI server with Uvicorn.

        Args:
            host: Host to bind to
            port: Port to bind to
            reload: Enable auto-reload
            **uvicorn_kwargs: Additional arguments passed to uvicorn.run
        """
        import uvicorn

        print(f"Starting router...")
        print(f"API docs available at: http://{host}:{port}/docs")
        print(f"Predict endpoint: POST http://{host}:{port}/predict")

        uvicorn.run(
            self.app,
            host=host,
            port=port,
            reload=reload,
            **uvicorn_kwargs,
        )
