from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from api.job_queue import SQLiteJobWorker, init_db
from api.routes.apple import router as apple_router

worker = SQLiteJobWorker()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
    worker.start()
    try:
        yield
    finally:
        worker.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="APPLE API",
        version="0.1.0",
        description="APPLE ANSYS ZIP runner API. SQLite queue 기반 비동기 실행을 사용합니다.",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )
    app.include_router(apple_router)
    return app


app = create_app()
