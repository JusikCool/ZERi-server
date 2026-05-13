"""ZERi-ai-model의 추론 스크립트를 subprocess로 실행하고 결과 CSV 경로를 돌려준다.

설계 원칙:
- ZERi-server는 PyTorch 의존성 본체를 들고 추론하지 않는다. 학부생 모델 환경
  (ZERi-ai-model 폴더, 자체 venv, FRED key, Kronos repo, GPU 옵션 등)을 그대로 활용.
- subprocess 호출이므로 ZERi-server의 import path가 더럽혀지지 않음.
- 결과 CSV가 만들어지면 risk_ingest_service / xai_ingest_service 가 적재.

지원 backend:
- "kronos": ZERi-ai-model/predict_kronos.py
   → model/saved/kronos/predict_kronos_returns.csv 생성 (50종목 × 30일 × 19분위수).
- "tft":    ZERi-ai-model/predict_with_xai.py
   → model/saved/predict_xai_returns.csv + model/saved/xai/raw/{TICKER}_*.csv 생성.
   (단, m3 ckpt 는 4종목 학습이라 50종목 정확도 보장 X. XAI 가 함께 나옴.)

환경 변수 (config.py):
- ZERI_AI_MODEL_PATH:   추론 스크립트 위치 (기본 ../ZERi-ai-model)
- ZERI_AI_MODEL_PYTHON: 추론용 파이썬 (기본 sys.executable). 학부생 venv 분리 시 지정.
- INFERENCE_TIMEOUT_SEC: 최대 실행 시간 (기본 4시간).

알아둘 함정:
- predict_kronos.py 는 Kronos 모델을 처음 호출하면 HuggingFace 에서 모델 다운로드 (~500MB).
- predict_with_xai.py 는 FRED_API_KEY 가 스크립트 내부 상수에 설정되어 있어야 동작.
- CPU 에서 50종목 × 30 sample 추론은 4~8시간 소요. 운영은 GPU 권장.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Literal

from app.core.config import get_settings
from app.core.error_codes import ErrorCode
from app.core.exceptions import AppException

__all__ = [
    "InferenceBackend",
    "get_ai_model_dir",
    "get_python_executable",
    "run_inference",
    "resolve_predict_csv_path",
]

InferenceBackend = Literal["kronos", "tft"]

_logger = logging.getLogger(__name__)

# 폴더 구조: <project_root>/ZERi-server + <project_root>/ZERi-ai-model 이라 가정.
_DEFAULT_RELATIVE_AI_MODEL = Path(__file__).resolve().parents[3] / "ZERi-ai-model"

_BACKEND_SCRIPT: dict[InferenceBackend, str] = {
    "kronos": "predict_kronos.py",
    "tft": "predict_with_xai.py",
}

# 스크립트 종료 후 결과 CSV가 만들어졌는지 확인하기 위한 상대 경로.
_BACKEND_PREDICT_CSV: dict[InferenceBackend, str] = {
    "kronos": "model/saved/kronos/predict_kronos_returns.csv",
    "tft": "model/saved/predict_xai_returns.csv",
}


def get_ai_model_dir() -> Path:
    settings = get_settings()
    if settings.zeri_ai_model_path:
        return Path(settings.zeri_ai_model_path).expanduser().resolve()
    return _DEFAULT_RELATIVE_AI_MODEL.resolve()


def get_python_executable() -> str:
    settings = get_settings()
    if settings.zeri_ai_model_python:
        return settings.zeri_ai_model_python
    return sys.executable


def resolve_predict_csv_path(backend: InferenceBackend) -> Path:
    """추론 종료 후 결과가 있어야 할 경로."""
    return get_ai_model_dir() / _BACKEND_PREDICT_CSV[backend]


async def run_inference(
    backend: InferenceBackend,
    *,
    timeout_sec: int | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict:
    """ZERi-ai-model 의 추론 스크립트를 동기 wait 로 호출.

    Returns:
        {
          "backend":     "kronos" | "tft",
          "returncode":  int,
          "duration_sec": float,
          "predict_csv": str (절대경로, 존재해야 200),
          "stdout_tail": str  (마지막 ~20줄, 디버깅용),
          "ai_model_dir": str,
        }

    Raises:
        AppException(INVALID_PARAMETER): 스크립트 미존재 / 결과 CSV 미생성 / 종료코드 ≠ 0.
        AppException(INTERNAL_ERROR):    타임아웃.
    """
    ai_dir = get_ai_model_dir()
    script = _BACKEND_SCRIPT[backend]
    script_path = ai_dir / script
    if not script_path.exists():
        raise AppException(
            ErrorCode.INVALID_PARAMETER,
            message=(
                f"추론 스크립트를 찾을 수 없습니다: {script_path}. "
                f"ZERI_AI_MODEL_PATH 환경변수가 올바른지 확인하세요."
            ),
        )

    settings = get_settings()
    timeout = timeout_sec or settings.inference_timeout_sec

    python_bin = get_python_executable()
    cmd = [python_bin, script]

    env = os.environ.copy()
    # ZERi-ai-model 폴더가 모듈 import root 이도록 (predict_kronos는 ./Kronos sys.path 조작).
    env.setdefault("PYTHONPATH", str(ai_dir))
    if extra_env:
        env.update(extra_env)

    _logger.info(
        "run_inference: backend=%s cmd=%s cwd=%s timeout=%ds",
        backend, cmd, ai_dir, timeout,
    )

    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(ai_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except FileNotFoundError as exc:
        raise AppException(
            ErrorCode.INVALID_PARAMETER,
            message=(
                f"파이썬 인터프리터를 실행할 수 없습니다: {python_bin}. "
                f"ZERI_AI_MODEL_PYTHON 환경변수 또는 PATH 를 확인하세요."
            ),
        ) from exc

    try:
        stdout_bytes, _ = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except TimeoutError as exc:
        proc.kill()
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001 — cleanup, 원인 보존이 더 중요
            pass
        raise AppException(
            ErrorCode.INTERNAL_ERROR,
            message=(
                f"추론 스크립트가 timeout({timeout}s) 안에 끝나지 않았습니다. "
                f"INFERENCE_TIMEOUT_SEC 환경변수로 늘리거나 종목 수를 줄이세요."
            ),
        ) from exc

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    duration = time.monotonic() - t0

    # 표준 출력 마지막 20줄만 보관 (로그 폭주 방지).
    stdout_tail = "\n".join(stdout.splitlines()[-20:])

    returncode = proc.returncode or 0
    predict_csv = resolve_predict_csv_path(backend)

    if returncode != 0:
        raise AppException(
            ErrorCode.INTERNAL_ERROR,
            message=(
                f"추론 스크립트가 실패했습니다 (returncode={returncode}). "
                f"마지막 로그:\n{stdout_tail}"
            ),
        )

    if not predict_csv.exists():
        raise AppException(
            ErrorCode.INTERNAL_ERROR,
            message=(
                f"추론 종료됐으나 결과 CSV가 없습니다: {predict_csv}. "
                f"마지막 로그:\n{stdout_tail}"
            ),
        )

    return {
        "backend": backend,
        "returncode": returncode,
        "duration_sec": duration,
        "predict_csv": str(predict_csv),
        "stdout_tail": stdout_tail,
        "ai_model_dir": str(ai_dir),
    }


