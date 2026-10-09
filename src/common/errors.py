import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.common.response_types import ErrorResponse

logger = logging.getLogger(__name__)


class ApiError(Exception):
    status_code = 500
    error_type = "InternalError"

    def __init__(self, message: str, *, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.message = message
        self.headers = headers or {}


class Unauthorized(ApiError):
    status_code = 401
    error_type = "Unauthorized"


class ModelNotFound(ApiError):
    status_code = 404
    error_type = "ModelNotFound"


class RequestInvalid(ApiError):
    status_code = 422
    error_type = "ValidationError"


class WrongMode(ApiError):
    status_code = 400
    error_type = "WrongMode"


class PayloadTooLarge(ApiError):
    status_code = 413
    error_type = "PayloadTooLarge"


class ModelUnavailable(ApiError):
    status_code = 503
    error_type = "ModelUnavailable"


class UpstreamError(ApiError):
    status_code = 502
    error_type = "UpstreamError"


class UpstreamTimeout(ApiError):
    status_code = 504
    error_type = "UpstreamTimeout"


class RedisUnavailable(ApiError):
    status_code = 503
    error_type = "AuthBackendUnavailable"


def error_response(
    status: int, error: str, error_type: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = ErrorResponse(error=error, error_type=error_type, details={}).model_dump()
    return JSONResponse(body, status_code=status, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return error_response(exc.status_code, exc.message, exc.error_type, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return error_response(422, "Invalid request", "ValidationError")

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return error_response(exc.status_code, str(exc.detail), "HttpError")

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        logger.exception("Unhandled error")
        return error_response(500, "Internal error", "InternalError")
