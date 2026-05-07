"""v1 API router aggregator. Endpoint modules are added in later phases.

Convention (matches the spec §8 screen-to-API matrix):

    /v1/auth/*      - F-AUTH (Phase 1)
    /v1/me/*        - F-ME / F-HOME / F-HISTORY (Phase 1+)
    /v1/tickers/*   - F-VETO (Phase 2)
    /v1/risk/*      - F-VERDICT (Phase 2)
    /v1/models/*    - F-MODEL (Phase 2)
"""

from fastapi import APIRouter

api_router = APIRouter(prefix="/v1")

# Endpoint routers will be wired here in subsequent phases, e.g.:
#
# from app.api.v1.endpoints import auth, me, watchlist, tickers, risk, history, models
# api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
# api_router.include_router(me.router,   prefix="/me",   tags=["me"])
