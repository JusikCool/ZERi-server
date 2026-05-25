"""v1 API router aggregator. Endpoint modules are added in later phases.

Convention (matches the spec §8 screen-to-API matrix):

    /v1/auth/*      - F-AUTH (Phase 1)
    /v1/me/*        - F-ME / F-HOME / F-HISTORY (Phase 1+)
    /v1/tickers/*   - F-VETO (Phase 2)
    /v1/risk/*      - F-VERDICT (Phase 2)
    /v1/models/*    - F-MODEL (Phase 2)
"""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    auth,
    devices,
    history,
    llm,
    macro,
    me,
    notifications,
    prices,
    risk,
    tickers,
    watchlist,
)

api_router = APIRouter(prefix="/v1")

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(me.router, prefix="/me", tags=["me"])
# /me 하위 리소스 — me 라우터 다음에 등록.
api_router.include_router(watchlist.router, prefix="/me/watchlist", tags=["me"])
api_router.include_router(history.router, prefix="/me/history", tags=["me"])
api_router.include_router(devices.router, prefix="/me/devices", tags=["me"])
api_router.include_router(prices.router, prefix="/prices", tags=["prices"])
api_router.include_router(tickers.router, prefix="/tickers", tags=["tickers"])
api_router.include_router(macro.router, prefix="/macro", tags=["macro"])
api_router.include_router(risk.router, prefix="/risk", tags=["risk"])
# 푸시 알림 — notifications.py 의 path 가 /me/notifications/test, /notifications/send 풀로 적혀있어 prefix 없이 mount.
api_router.include_router(notifications.router, tags=["notifications"])
# LLM 디버그/연결 확인 — operator 전용.
api_router.include_router(llm.router, prefix="/llm", tags=["llm"])

# 추후: F-MODEL 2개, F-HISTORY 의 outcome 평가 cron (POST /v1/history/evaluate)
