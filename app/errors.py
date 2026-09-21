"""One error shape for every non-2xx response.

    {
      "error": {"code": "job_limit_reached", "message": "...", "retry_after": 30,
                "details": {...}},
      "detail": "...",            # = error.message; kept for the built-in upload page
      "request_id": "req_..."
    }

`code` is stable and meant for programs (the portal maps it to UI copy); `message` is for
people. `Retry-After` and `X-Request-ID` are also sent as headers, so a proxy can act on
them without parsing the body. The full list of codes is ERROR_CODES below.

Per-domain outcomes (found, fetch_failed, invalid_input, ...) are NOT errors: they are
200 results with a status and error_reason.
"""

import logging
import re
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("email_extractor")

# code -> (http status, when)
ERROR_CODES: dict[str, tuple[int, str]] = {
    "invalid_json": (400, "the body is not valid JSON"),
    "acting_user_required": (400, "a service key must say who it acts for (X-Acting-User)"),
    "invalid_acting_user": (400, "X-Acting-User is not an email address"),
    "missing_api_key": (401, "no X-API-Key header"),
    "invalid_api_key": (401, "the key is wrong or revoked"),
    "acting_user_not_allowed": (403, "X-Acting-User sent with a key that is not a service key"),
    "job_not_found": (404, "no such job, or it belongs to someone else"),
    "not_found": (404, "no such route"),
    "method_not_allowed": (405, "wrong HTTP method for this route"),
    "job_not_paused": (409, "resume on a job that is not paused"),
    "job_already_finished": (409, "cancel on a job that is done, failed or cancelled"),
    "file_too_large": (413, "the upload or body is over the size limit"),
    "too_many_rows": (413, "over 50,000 rows or items"),
    "unsupported_file_type": (415, "not a CSV, TSV, TXT or XLSX file"),
    "validation_error": (422, "a field is missing or has the wrong type; see details"),
    "empty_file": (422, "the file has no data"),
    "no_rows": (422, "nothing to process"),
    "no_website_column": (422, "no website column detected; pass column="),
    "unknown_column": (422, "column= names a column that is not in the file"),
    "invalid_webhook_url": (422, "webhook_url is not a public https URL"),
    "invalid_file": (422, "the file could not be read"),
    "rate_limited": (429, "too many requests for this user; see retry_after"),
    "job_limit_reached": (429, "too many unfinished jobs for this user"),
    "invalid_request": (400, "the request could not be read"),
    "forbidden": (403, "the key may not do this"),
    "acting_user_revoked": (403, "the person named in X-Acting-User was removed"),
    "conflict": (409, "the request conflicts with the current state"),
    "internal_error": (500, "a bug; quote the request_id"),
    "provider_quota_exhausted": (503, "the fetch provider account is out of credit or "
                                      "rejecting the key; jobs are paused, not failed"),
    "service_unavailable": (503, "the database or queue is unreachable"),
}

_GENERIC_BY_STATUS = {
    400: "invalid_request", 401: "invalid_api_key", 403: "forbidden", 404: "not_found",
    405: "method_not_allowed", 409: "conflict", 413: "file_too_large",
    415: "unsupported_file_type", 422: "validation_error", 429: "rate_limited",
    500: "internal_error", 503: "service_unavailable",
}

_REQUEST_ID_OK = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class ApiError(HTTPException):
    def __init__(self, code: str, message: str, *, details: dict | list | None = None,
                 retry_after: int | None = None, status: int | None = None) -> None:
        status = status or ERROR_CODES[code][0]
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
        super().__init__(status_code=status, detail=message, headers=headers)
        self.code = code
        self.message = message
        self.details = details
        self.retry_after = retry_after


def error_body(code: str, message: str, request_id: str | None, *,
               details=None, retry_after: int | None = None) -> dict:
    return {
        "error": {"code": code, "message": message, "retry_after": retry_after,
                  "details": details},
        "detail": message,
        "request_id": request_id,
    }


def _rid(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _response(request: Request, status: int, body: dict,
              headers: dict | None = None) -> JSONResponse:
    headers = dict(headers or {})
    if body.get("request_id"):
        headers["X-Request-ID"] = body["request_id"]
    return JSONResponse(body, status_code=status, headers=headers)


class RequestId:
    """Accept the caller's X-Request-ID (the portal sends one) or mint one; echo it."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode(
            "latin-1")
        rid = incoming if _REQUEST_ID_OK.match(incoming) else f"req_{uuid.uuid4().hex[:16]}"
        scope.setdefault("state", {})["request_id"] = rid

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                headers = [h for h in message.get("headers", []) if h[0] != b"x-request-id"]
                headers.append((b"x-request-id", rid.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_id)


def _is_outage(exc: BaseException) -> bool:
    """A dependency being down, as opposed to a bug. Bare OSError is deliberately not
    here: FileNotFoundError or a stray TimeoutError is a bug, and calling it an outage
    made the portal retry it."""
    from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    import socket

    # socket.gaierror: the database or Redis host does not resolve (asyncpg raises it
    # unwrapped). Unlike the rest of OSError it can only mean a dependency is missing.
    if isinstance(exc, (OperationalError, InterfaceError, PoolTimeout, ConnectionError,
                        socket.gaierror)):
        return True
    try:
        import asyncpg

        if isinstance(exc, (asyncpg.exceptions.CannotConnectNowError,
                            asyncpg.exceptions.TooManyConnectionsError,
                            asyncpg.exceptions.ConnectionDoesNotExistError,
                            asyncpg.exceptions.InterfaceError)):
            return True
    except ImportError:  # pragma: no cover
        pass
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    try:
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeout
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, (RedisConnectionError, RedisTimeout))


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        body = error_body(exc.code, exc.message, _rid(request), details=exc.details,
                          retry_after=exc.retry_after)
        return _response(request, exc.status_code, body, exc.headers)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        # Framework-raised: unknown route, wrong method.
        code = _GENERIC_BY_STATUS.get(exc.status_code, "error")
        message = exc.detail if isinstance(exc.detail, str) else code.replace("_", " ")
        return _response(request, exc.status_code,
                         error_body(code, message, _rid(request)), exc.headers)

    from starlette.exceptions import HTTPException as StarletteHTTPException

    app.add_exception_handler(StarletteHTTPException, _http_error)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        details = [
            {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "msg": e["msg"]}
            for e in exc.errors()
        ]
        first = details[0] if details else {"field": "", "msg": "invalid request"}
        message = f"{first['field']}: {first['msg']}" if first["field"] else first["msg"]
        return _response(request, 422, error_body("validation_error", message,
                                                  _rid(request), details=details))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        rid = _rid(request)
        if _is_outage(exc):
            log.error("service_unavailable", extra={"request_id": rid,
                                                    "err": type(exc).__name__})
            return _response(request, 503, error_body(
                "service_unavailable", "the database or queue is unreachable; try again",
                rid, retry_after=10), {"Retry-After": "10"})
        log.exception("internal_error", extra={"request_id": rid})
        return _response(request, 500, error_body(
            "internal_error", "unexpected error; quote the request_id when reporting it",
            rid))
