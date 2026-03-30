# 📉 ZERi - Backend API

Adaptive AI 기반 금융 하방 리스크 예측 및 시각화 시스템의 백엔드 API 서버입니다.

FastAPI를 기반으로 구축되었으며, Temporal Fusion Transformer(TFT) 모델 추론, XAI(설명 가능한 AI) 데이터 반환, 모델 검증 지표를 제공합니다.

## 🛠 Tech Stack

- **Framework**: FastAPI (Python 3.10+)
- **AI/ML**: PyTorch, PyTorch Forecasting (TFT Model)
- **Data**: Pandas, NumPy, yfinance, FRED API
- **Database**: PostgreSQL (Partitioning & Indexing)
- **Infrastructure**: Docker, AWS S3/CloudFront (Static Asset Serving)
- **Caching**: Redis (LRU Strategy)

## 🚀 Architecture & Scalability

대규모 시계열 데이터 처리와 수천 개 종목 확장을 고려하여 **'데이터 전송 최소화'**와 **'서버 부하 분산'**에 초점을 맞춘 아키텍처를 채택했습니다.

### 1. Static Pre-generation (S3/CDN)

- 과거 주가 및 예측 히스토리는 불변(Immutable) 데이터입니다.
- 배치 작업(Batch Job) 완료 후 종목별 전체 히스토리 데이터를 JSON 파일(예: `AAPL_history.json`)로 생성하여 **AWS S3**에 업로드합니다.
- 프론트엔드는 CDN(CloudFront)을 통해 정적 파일을 직접 호출함으로써 백엔드 I/O와 DB 부하를 원천 차단합니다.

### 2. Downsampling & Lazy Loading

- 조회 기간에 따라 데이터 밀도를 동적으로 조절하여 페이로드 크기를 최적화합니다.
  - **~6개월**: 일봉(Daily) 데이터 제공
  - **6개월~2년**: 주봉(Weekly) 데이터 제공
  - **2년 이상**: 월봉(Monthly) 데이터 제공
- 차트 초기 로딩 시 기본 구간을 '최근 6개월'로 제한하여 렌더링 성능을 보장합니다.

### 3. Redis LRU Caching

- 동적 연산이 필요한 API 응답에 대해 Redis를 사용하되, 메모리 한도를 설정하고 자주 조회되는 **활성 종목(Active Tickers)**만 유지하는 LRU 정책을 적용합니다.

### 4. PostgreSQL Table Partitioning

- 데이터 비대화를 방지하기 위해 연도별 또는 종목 심볼 기준 파티셔닝을 적용합니다.
- 예측 결과는 행(Row)으로 늘리지 않고 `FLOAT[]` 타입으로 압축 저장하여 스캔 효율을 극대화합니다.

## 🗄 Database Design

### Optimized Schema

```
-- 연도별 범위 파티셔닝 적용
CREATE TABLE stock_history (
    ticker VARCHAR(10) NOT NULL,
    base_date DATE NOT NULL,
    open_price NUMERIC(12, 2),
    high_price NUMERIC(12, 2),
    low_price NUMERIC(12, 2),
    close_price NUMERIC(12, 2),
    volume BIGINT,
    -- 다중 분위수 예측값을 FLOAT 배열로 압축 저장
    pred_1d_ago FLOAT[], -- [q_0.1, q_0.25, q_0.5]
    pred_5d_ago FLOAT[],
    pred_10d_ago FLOAT[],
    PRIMARY KEY (ticker, base_date)
) PARTITION BY RANGE (base_date);

-- 복합 인덱스: Ticker 선별 후 Date 범위 스캔 최적화
CREATE INDEX idx_stock_ticker_date ON stock_history (ticker, base_date DESC);
```

## 📋 API Specification

### Base URL

```
http://{host}:{port}/api/v1
```

### 1. 추론 (Prediction) API

특정 종목의 향후 10일간 다중 분위수 기반 하방 리스크 예측값을 반환합니다.

- **URL**: `/risk/predict/{ticker}`
- **Method**: `GET`
- **Response (200 OK)**:

```
{
  "ticker": "AAPL",
  "base_date": "2026-03-30",
  "target_dates": ["2026-03-31", "2026-04-01", "..."],
  "predictions": {
    "q_0.1": [140.5, 139.2, "..."],
    "q_0.25": [145.0, 144.1, "..."],
    "q_0.5": [150.2, 149.8, "..."]
  },
  "current_vix": 22.4,
  "realized_volatility": 0.045
}
```

### 2. 설명 가능성 (XAI) API

TFT 모델의 Variable Selection Network(VSN)를 기반으로 하락 예측의 주요 원인을 설명합니다.

- **URL**: `/risk/xai/{ticker}`
- **Method**: `GET`
- **Query Params**: `top_k` (Default: 5)
- **Implementation Note**: 시스템 안정성을 위해 템플릿 기반(Template-based) 텍스트 구성을 사용합니다.
- **Response (200 OK)**:

```
{
  "ticker": "AAPL",
  "timestamp": "2026-03-30T15:00:00Z",
  "variable_importance": [
    {"feature": "US_Fed_Rate", "weight": 0.45},
    {"feature": "VIX", "weight": 0.25}
  ],
  "attention_summary": "미국 기준금리(45%)와 시장 공포 지수(25%)가 하방 압력의 주원인으로 분석되었습니다."
}
```

### 3. 검증 및 성능 (Metrics) API

모델 사후 검증 지표(Violation Rate, Kupiec POF Test 등)를 반환합니다.

- **URL**: `/risk/metrics/{ticker}`
- **Method**: `GET`
- **Response (200 OK)**:

```
{
  "ticker": "AAPL",
  "window_days": 252,
  "violation_rate": 0.042,
  "kupiec_pof_test": {
    "statistic": 1.25,
    "p_value": 0.068,
    "is_valid": true
  }
}
```

### 4. 과거 이력 조회 (History) API

과거 OHLCV와 시점별 모델 예측 밴드 데이터를 반환합니다. 캔들스틱 차트 렌더링에 사용됩니다.

- **URL**: `/risk/history/{ticker}`
- **Method**: `GET`
- **Query Params**: `start_date`, `end_date`
- **Response (200 OK)**:

```
{
  "ticker": "AAPL",
  "history": [
    {
      "date": "2026-03-25",
      "open": 154.0, "high": 156.5, "low": 153.0, "close": 155.0,
      "pred_1d_ago": {"q_0.1": 145.0, "q_0.25": 148.0, "q_0.5": 152.0},
      "pred_5d_ago": {...},
      "pred_10d_ago": {...}
    }
  ]
}
```

### 5. 파이프라인 동기화 (Pipeline Sync) API

데이터 수집 및 전처리를 수동 가동합니다. 스케줄러 복구용으로 사용됩니다.

- **URL**: `/pipeline/sync`
- **Method**: `POST`
- **Header**: `X-Admin-Token` (Internal API Key)
- **Response (202 Accepted)**:

```
{
  "message": "Data pipeline synchronization started successfully.",
  "status": "processing"
}
```

## ⚠️ Error Responses

| Status Code | Error Code             | Description              | Example Body                                          |
| ----------- | ---------------------- | ------------------------ | ----------------------------------------------------- |
| **404**     | `TICKER_NOT_FOUND`     | 지원하지 않는 티커 조회  | `{"error": "TICKER_NOT_FOUND", "message": "..."}`     |
| **422**     | `INVALID_PARAMETER`    | 파라미터 제약 조건 위반  | `{"error": "INVALID_PARAMETER", "message": "..."}`    |
| **503**     | `PREDICTION_NOT_READY` | 당일 배치 작업 지연/오류 | `{"error": "PREDICTION_NOT_READY", "message": "..."}` |
| **401**     | `UNAUTHORIZED_ACCESS`  | 관리자 토큰 불일치/누락  | `{"error": "UNAUTHORIZED_ACCESS", "message": "..."}`  |



