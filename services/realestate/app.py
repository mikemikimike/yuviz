"""Real Estate Service — projects, site visits, leads and callbacks behind the real-estate agent's tools.

Run: uvicorn services.realestate.app:app --port 8710
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import core, db
from .routers import admin, tools

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.get_pool()
    yield
    await db.close_pool()


app = FastAPI(title="Voice AI Platform — Real Estate Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("ADMIN_UI_ORIGINS", "http://localhost:3000").split(",")
                   if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tools.router)
app.include_router(admin.tenant_scoped_router)


@app.exception_handler(core.NotConfigured)
async def not_configured_handler(request: Request, exc: core.NotConfigured) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "real estate is not set up for this tenant"})


@app.exception_handler(LookupError)
async def not_found_handler(request: Request, exc: LookupError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "not found"})


@app.exception_handler(asyncpg.ForeignKeyViolationError)
async def fk_violation_handler(request: Request, exc: asyncpg.ForeignKeyViolationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": "request references an id that does not exist"})


@app.get("/health")
async def health():
    return {"status": "ok"}
