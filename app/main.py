from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.collectors.manager import SyncManager
from app.collectors.web_collector import WebSiteCollector
from app.config import DB_PATH, ensure_directories
from app.database import DatabaseManager
from app.services.scheduler import SiteSchedulerService

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
TEMPLATES_DIR = APP_DIR / "templates"


def create_app(
    *,
    db_path: Optional[Path] = None,
    enable_scheduler: bool = True,
    web_collector: Optional[WebSiteCollector] = None,
) -> FastAPI:
    ensure_directories()
    resolved_db_path = Path(db_path) if db_path else DB_PATH

    db = DatabaseManager(db_path=resolved_db_path)
    collector = web_collector or WebSiteCollector()
    sync_manager = SyncManager(
        db=db,
        web_collector=collector,
    )
    scheduler = SiteSchedulerService(db=db, sync_manager=sync_manager)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
        if enable_scheduler:
            scheduler.start()
        try:
            yield
        finally:
            if enable_scheduler:
                scheduler.stop()

    application = FastAPI(
        title="سامانه جمع‌آوری و آرشیو هوشمند مطالب وب‌سایت‌ها",
        version="1.0.0",
        lifespan=lifespan,
    )

    application.state.db = db
    application.state.sync_manager = sync_manager
    application.state.scheduler = scheduler

    application.include_router(api_router)
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @application.get("/", include_in_schema=False)
    async def serve_index() -> FileResponse:
        return FileResponse(
            str(TEMPLATES_DIR / "index.html"),
            headers={"Cache-Control": "no-cache"},
        )

    @application.get("/healthz", include_in_schema=False)
    async def healthcheck() -> dict:
        return {
            "status": "ok",
            "app": "website-content-collector",
            "pid": os.getpid(),
            "scheduler_running": scheduler.is_running,
        }

    return application


app = create_app()
