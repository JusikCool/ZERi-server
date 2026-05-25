"""ORM model registry. Importing this package binds every model to `Base.metadata`,
which is what Alembic's autogenerate uses as the source of truth.
"""

from app.db.base import Base
from app.db.models.analysis_history import AnalysisHistory
from app.db.models.backtest_result import BacktestResult
from app.db.models.device_token import DeviceToken
from app.db.models.disclaimer_ack import DisclaimerAck
from app.db.models.llm_explanation import LLMExplanation
from app.db.models.macro_indicator import MacroIndicator
from app.db.models.marketing_consent import MarketingConsent
from app.db.models.prediction import Prediction
from app.db.models.prediction_evaluation import PredictionEvaluation
from app.db.models.price import Price
from app.db.models.refresh_token import RefreshToken
from app.db.models.risk_grade import RiskGrade
from app.db.models.ticker import Ticker
from app.db.models.user import User
from app.db.models.watchlist import Watchlist
from app.db.models.xai_explanation import XaiExplanation

__all__ = [
    "Base",
    "User",
    "Ticker",
    "Price",
    "MacroIndicator",
    "Prediction",
    "RiskGrade",
    "XaiExplanation",
    "Watchlist",
    "AnalysisHistory",
    "DisclaimerAck",
    "PredictionEvaluation",
    "BacktestResult",
    "RefreshToken",
    "MarketingConsent",
    "DeviceToken",
    "LLMExplanation",
]
