import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.schemas.common import ApiError, ApiErrorResponse, Meta

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup hooks (cache warmup, model registry load, etc.) go here.
    yield
    # Shutdown hooks.


app = FastAPI(
    title="BEFORE API",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# ---- middleware ---------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach a request_id to every request/response. Spec §0.2 envelope.meta.request_id."""
    request_id = request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex[:24]}"
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


# ---- exception handlers -------------------------------------------------

_HTTP_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_PARAMETER,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.DISCLAIMER_REQUIRED,
    429: ErrorCode.RATE_LIMIT_EXCEEDED,
}


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    return JSONResponse(
        status_code=exc.http_status,
        content=ApiErrorResponse(
            error=ApiError(code=exc.code.value, message=exc.message, details=exc.details),
            meta=Meta(request_id=_request_id(request)),
        ).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content=ApiErrorResponse(
            error=ApiError(
                code=ErrorCode.INVALID_PARAMETER.value,
                message="잘못된 파라미터입니다.",
                details={"errors": exc.errors()},
            ),
            meta=Meta(request_id=_request_id(request)),
        ).model_dump(),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    code = _HTTP_STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiErrorResponse(
            error=ApiError(code=code.value, message=str(exc.detail)),
            meta=Meta(request_id=_request_id(request)),
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content=ApiErrorResponse(
            error=ApiError(
                code=ErrorCode.INTERNAL_ERROR.value,
                message="서버 내부 오류가 발생했습니다.",
            ),
            meta=Meta(request_id=_request_id(request)),
        ).model_dump(),
    )


# ---- routes -------------------------------------------------------------


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(api_router)
