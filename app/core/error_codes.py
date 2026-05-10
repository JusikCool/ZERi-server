from enum import Enum


class ErrorCode(str, Enum):
    INVALID_PARAMETER = "INVALID_PARAMETER"
    UNAUTHORIZED = "UNAUTHORIZED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    EMAIL_DUPLICATE = "EMAIL_DUPLICATE"
    TICKER_NOT_FOUND = "TICKER_NOT_FOUND"
    MACRO_NOT_FOUND = "MACRO_NOT_FOUND"
    PREDICTION_NOT_READY = "PREDICTION_NOT_READY"
    WATCHLIST_LIMIT_EXCEEDED = "WATCHLIST_LIMIT_EXCEEDED"
    WATCHLIST_DUPLICATE = "WATCHLIST_DUPLICATE"
    DISCLAIMER_REQUIRED = "DISCLAIMER_REQUIRED"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_PARAMETER: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.TOKEN_EXPIRED: 401,
    ErrorCode.INVALID_CREDENTIALS: 401,
    ErrorCode.EMAIL_DUPLICATE: 409,
    ErrorCode.TICKER_NOT_FOUND: 404,
    ErrorCode.MACRO_NOT_FOUND: 404,
    ErrorCode.PREDICTION_NOT_READY: 503,
    ErrorCode.WATCHLIST_LIMIT_EXCEEDED: 422,
    ErrorCode.WATCHLIST_DUPLICATE: 409,
    ErrorCode.DISCLAIMER_REQUIRED: 403,
    ErrorCode.RATE_LIMIT_EXCEEDED: 429,
    ErrorCode.INTERNAL_ERROR: 500,
}


DEFAULT_ERROR_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.INVALID_PARAMETER: "잘못된 파라미터입니다.",
    ErrorCode.UNAUTHORIZED: "인증이 필요합니다.",
    ErrorCode.TOKEN_EXPIRED: "토큰이 만료되었습니다.",
    ErrorCode.INVALID_CREDENTIALS: "이메일 또는 비밀번호가 올바르지 않습니다.",
    ErrorCode.EMAIL_DUPLICATE: "이미 사용 중인 이메일입니다.",
    ErrorCode.TICKER_NOT_FOUND: "해당 종목을 찾을 수 없습니다.",
    ErrorCode.MACRO_NOT_FOUND: "해당 거시지표를 찾을 수 없습니다.",
    ErrorCode.PREDICTION_NOT_READY: "예측 데이터 준비 중입니다.",
    ErrorCode.WATCHLIST_LIMIT_EXCEEDED: "워치리스트는 최대 5개까지 등록 가능합니다.",
    ErrorCode.WATCHLIST_DUPLICATE: "이미 워치리스트에 추가된 종목입니다.",
    ErrorCode.DISCLAIMER_REQUIRED: "면책 동의가 필요합니다.",
    ErrorCode.RATE_LIMIT_EXCEEDED: "요청이 너무 많습니다. 잠시 후 다시 시도하세요.",
    ErrorCode.INTERNAL_ERROR: "서버 내부 오류가 발생했습니다.",
}
