"""XAI(변수 중요도) CSV → xai_explanations 테이블 적재.

TFT의 `interpret_output()` 결과를 학부생 모델 환경에서 CSV로 dump 받아 적재한다.

입력 CSV 형식 (xai_csv):
    ticker, feature, weight, label
    AAPL,   realized_volatility_30d, 0.34, 최근 30일 실현 변동성
    AAPL,   beta,                    0.22, 베타
    ...

ticker 단위로 group → features 배열로 묶어서 xai_explanations 한 행으로 저장.

UPSERT 정책:
- xai_explanations 는 prediction_id UNIQUE — 같은 (ticker, base_date) 재실행 시
  prediction_id가 이미 있을 수 있음. ON CONFLICT(prediction_id) DO UPDATE 로 덮어씀.
- prediction_id 매핑은 호출자(sync/baseline)가 ingest_predictions_from_csv 결과에서 전달.

XAI는 선택적(optional). 파일이 없거나 path=None 이면 0건 적재로 정상 종료.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException
from app.db.models import XaiExplanation

__all__ = ["ingest_xai_from_csv"]


def _read_xai_csv(path: Path) -> dict[str, list[dict]]:
    """xai CSV → {ticker: [{feature, weight, label}, ...]}.

    weight는 float 캐스팅. 잘못된 행은 INVALID_PARAMETER.
    """
    required = {"ticker", "feature", "weight"}
    out: dict[str, list[dict]] = defaultdict(list)

    try:
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
                missing = required - set(reader.fieldnames or [])
                raise AppException(
                    ErrorCode.INVALID_PARAMETER,
                    message=f"XAI CSV 필수 컬럼 누락: {sorted(missing)}",
                )
            for row in reader:
                tk = row["ticker"]
                try:
                    weight = float(row["weight"])
                except (TypeError, ValueError) as exc:
                    raise AppException(
                        ErrorCode.INVALID_PARAMETER,
                        message=f"weight 가 숫자가 아닙니다: ticker={tk}, weight={row['weight']!r}",
                    ) from exc
                out[tk].append(
                    {
                        "feature": row["feature"],
                        "weight": weight,
                        # label은 선택. 비면 feature 이름 그대로.
                        "label": row.get("label") or row["feature"],
                    }
                )
    except FileNotFoundError as exc:
        raise AppException(
            ErrorCode.INVALID_PARAMETER,
            message=f"XAI CSV 파일을 찾을 수 없습니다: {path}",
        ) from exc

    return dict(out)


async def ingest_xai_from_csv(
    session: AsyncSession,
    *,
    xai_csv_path: str | None,
    prediction_id_by_ticker: dict[str, int],
    base_date: date,
) -> int:
    """XAI CSV → xai_explanations UPSERT.

    Args:
        xai_csv_path: 경로. None 이면 0 반환 (적재 스킵).
        prediction_id_by_ticker: ingest_predictions_from_csv 결과의 prediction_id 매핑.
        base_date: 같은 sync/baseline run의 base_date.

    Returns:
        적재된 ticker 수.
    """
    if xai_csv_path is None:
        return 0

    grouped = _read_xai_csv(Path(xai_csv_path))
    if not grouped:
        return 0

    payloads = []
    for ticker, features in grouped.items():
        prediction_id = prediction_id_by_ticker.get(ticker)
        if prediction_id is None:
            # predictions 테이블에 없는 ticker — sync/baseline에서 스킵된 것. XAI도 스킵.
            continue
        payloads.append(
            {
                "prediction_id": prediction_id,
                "ticker": ticker,
                "base_date": base_date,
                "features": features,
            }
        )

    if not payloads:
        return 0

    stmt = pg_insert(XaiExplanation).values(payloads)
    stmt = stmt.on_conflict_do_update(
        index_elements=["prediction_id"],
        set_={
            "features": stmt.excluded.features,
            "base_date": stmt.excluded.base_date,
            "ticker": stmt.excluded.ticker,
        },
    )
    await session.execute(stmt)
    await session.commit()
    return len(payloads)
