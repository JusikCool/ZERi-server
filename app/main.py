import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.core.rate_limit import limiter, rate_limit_exceeded_handler
from app.core.request_context import set_request_id
from app.schemas.common import ApiError, ApiErrorResponse, Meta

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: 만료된 refresh 토큰 sweep — DB 누적 방지.
    # 주: 운영에서는 cron/별도 잡으로 옮기는 게 정석. 단일 인스턴스 dev에선 startup 1회로 충분.
    try:
        from app.db.session import SessionLocal
        from app.services.auth_service import sweep_expired_refresh_tokens

        async with SessionLocal() as session:
            removed = await sweep_expired_refresh_tokens(session)
        if removed:
            import logging

            logging.getLogger(__name__).info(
                "startup: swept %d expired refresh tokens", removed
            )
    except Exception:  # noqa: BLE001 — startup은 sweep 실패로 죽이면 안 됨
        import logging

        logging.getLogger(__name__).exception("startup: refresh token sweep failed")
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

# Rate limiter 등록 — 라우트의 @limiter.limit 데코레이터로 적용.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# ---- middleware ---------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def strip_trailing_slash(request: Request, call_next):
    """들어오는 path 의 trailing slash 를 떼고 라우팅.

    FastAPI 기본 `redirect_slashes=True` 가 307 redirect 로 처리하지만:
    - POST/PATCH 의 body 가 redirect 후 손실되는 케이스
    - CORS preflight 가 redirect 따라가지 않아 차단되는 케이스
    이 두 가지를 회피하려면 redirect 가 아닌 path-rewrite 가 필요.

    예: `GET /v1/me/watchlist/` → `GET /v1/me/watchlist` 로 그대로 처리.
    루트(`/`) 와 path param 안의 `/` 는 영향 없음.
    """
    path = request.url.path
    if path != "/" and path.endswith("/"):
        stripped = path.rstrip("/")
        request.scope["path"] = stripped
        request.scope["raw_path"] = stripped.encode("utf-8")
    return await call_next(request)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach a request_id to every request/response. Spec §0.2 envelope.meta.request_id.

    ContextVar에 set → schemas.common.Meta가 default_factory로 자동 주입.
    이렇게 해야 성공 응답(라우터가 직접 만드는 ApiResponse)에도 request_id가 채워짐.

    auth 응답은 추가로 Cache-Control: no-store — 토큰이 프록시/브라우저 캐시에 남는 것 방지.
    """
    request_id = request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex[:24]}"
    request.state.request_id = request_id
    set_request_id(request_id)
    try:
        response = await call_next(request)
    finally:
        # task 종료 후 다른 task로 누수 방지 — 이 부분은 ContextVar가 task별 격리되니
        # 실제로는 불필요하지만, 명시적으로 cleanup하는 게 코드 의도가 명확.
        set_request_id(None)
    response.headers["X-Request-Id"] = request_id
    if request.url.path.startswith("/v1/auth/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
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
