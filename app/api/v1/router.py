"""v1 API router aggregator. Endpoint modules are added in later phases.

Convention (matches the spec §8 screen-to-API matrix):

    /v1/auth/*      - F-AUTH (Phase 1)
    /v1/me/*        - F-ME / F-HOME / F-HISTORY (Phase 1+)
    /v1/tickers/*   - F-VETO (Phase 2)
    /v1/risk/*      - F-VERDICT (Phase 2)
    /v1/models/*    - F-MODEL (Phase 2)
"""

from fastapi import APIRouter

from app.api.v1.endpoints import auth, macro, prices, tickers

api_router = APIRouter(prefix="/v1")

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(prices.router, prefix="/prices", tags=["prices"])
api_router.include_router(tickers.router, prefix="/tickers", tags=["tickers"])
api_router.include_router(macro.router, prefix="/macro", tags=["macro"])

# 추후 단계에서 추가:
# from app.api.v1.endpoints import me, watchlist, risk, history, models
# api_router.include_router(me.router,   prefix="/me",   tags=["me"])
