from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from app.core.exceptions import AppException

_STATUS_CODE_MAP = {
    400: "BAD_REQUEST",
    401: "AUTH_FAILED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMIT",
    500: "INTERNAL_ERROR",
}


def _module_from_path(path: str) -> str:
    if "/chat" in path:
        return "chat_endpoint"
    if "/auth" in path:
        return "auth_endpoint"
    if "/kb" in path:
        return "kb_endpoint"
    if "/sessions" in path:
        return "session_endpoint"
    return "system"


def _get_trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "unknown")


def _get_session_id(request: Request) -> str:
    return getattr(request.state, "session_id", "unknown")


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "session_id": _get_session_id(request),
            "trace_id": _get_trace_id(request),
            "error_code": "VALIDATION_ERROR",
            "module": _module_from_path(str(request.url)),
            "reply": "请求格式有误，请检查您的输入格式。",
        },
    )


async def http_exception_handler(request: Request, exc):
    from fastapi import HTTPException
    if isinstance(exc, AppException):
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "session_id": _get_session_id(request),
                "trace_id": _get_trace_id(request),
                "error_code": exc.error_code,
                "module": exc.module,
                "reply": exc.message,
            },
        )
    if isinstance(exc, HTTPException):
        error_code = _STATUS_CODE_MAP.get(exc.status_code, f"HTTP_{exc.status_code}")
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "session_id": _get_session_id(request),
                "trace_id": _get_trace_id(request),
                "error_code": error_code,
                "module": _module_from_path(str(request.url)),
                "reply": str(exc.detail),
            },
        )
    return JSONResponse(status_code=500, content={"error_code": "INTERNAL_ERROR", "reply": "未知HTTP异常"})


async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "session_id": _get_session_id(request),
            "trace_id": _get_trace_id(request),
            "error_code": "INTERNAL_ERROR",
            "module": "system",
            "reply": "系统内部异常，请稍后再试。",
        },
    )


def register_exception_handlers(app):
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(AppException, http_exception_handler)
    from fastapi import HTTPException as FHTTPException
    app.add_exception_handler(FHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, global_exception_handler)