"""요청 컨텍스트 전파.

ContextVar로 미들웨어 → ApiResponse default factory까지 request_id를 전달.
- async 안전: ContextVar는 task별 격리됨
- 미들웨어 외부에서 호출되면 None 반환 (테스트나 startup 작업)
"""

from contextvars import ContextVar

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_request_id(request_id: str | None) -> None:
    _request_id_var.set(request_id)


def get_request_id() -> str | None:
    return _request_id_var.get()
