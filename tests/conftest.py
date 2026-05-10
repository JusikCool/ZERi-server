"""테스트용 공통 fixture.

- 단일 PG 컨테이너에 별도 schema(`test_<uuid>`)를 만들어 격리.
- 테스트마다 깨끗한 DB. 끝나면 schema 삭제.
- 운영 DB에 영향 없게 search_path를 test schema로 고정.

로컬 실행: docker compose exec api pytest
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# 테스트 DB URL은 운영용과 다른 schema 사용. 같은 PG 인스턴스, 격리된 namespace.
_BASE_DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://before:before@db:5432/before"
)


def _schema_for_run() -> str:
    return f"test_{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture
async def db_schema() -> AsyncIterator[str]:
    """격리 schema 생성 → yield → cleanup."""
    schema = _schema_for_run()
    engine = create_async_engine(_BASE_DB_URL, isolation_level="AUTOCOMMIT")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    await engine.dispose()
    try:
        yield schema
    finally:
        engine = create_async_engine(_BASE_DB_URL, isolation_level="AUTOCOMMIT")
        async with engine.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_schema: str) -> AsyncIterator[AsyncSession]:
    """격리된 schema에 모든 모델 테이블 생성 후 세션 제공.

    asyncpg는 server_settings를 URL 쿼리가 아닌 connect_args로 받음.
    """
    from app.db.models import Base

    engine = create_async_engine(
        _BASE_DB_URL,
        future=True,
        connect_args={"server_settings": {"search_path": db_schema}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """get_db override한 in-process ASGI 테스트 클라이언트.

    rate limit은 의도적으로 완화 — 테스트가 자연스럽게 한도에 부딪히지 않게.
    """
    from app.api.deps import get_db
    from app.core.rate_limit import limiter
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    limiter.enabled = False  # 테스트 중엔 rate limit off
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()
        limiter.enabled = True
