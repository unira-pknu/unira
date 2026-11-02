"""FastAPI application factory and lifespan."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI

from .config import get_settings
from .deps import AppState
from .embeddings import Embedder
from .generation import load_system_prompt
from .reranking import CrossEncoderReranker
from .routers import admin as admin_router
from .routers import ask as ask_router
from .routers import popular as popular_router
from .supabase_client import ThreadLocalSupabase
from .warmup import keep_warm

logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_level)
    logger.info("Backend starting up (model=%s)…", settings.embedding_model)

    embedder = Embedder(
        model_name=settings.embedding_model,
        device=settings.embedding_device,
        expected_dimensions=settings.expected_dimensions,
    )
    reranker = None
    if settings.reranker_enabled:
        try:
            reranker = CrossEncoderReranker(
                model_name=settings.reranker_model,
                device=settings.reranker_device,
                quantize=settings.reranker_quantize,
            )
        except Exception:
            logger.exception("Reranker load failed; continuing without reranker")
    # One client per request thread -- a single shared client breaks under
    # concurrent requests (see supabase_client.py).
    supabase = ThreadLocalSupabase(
        settings.supabase_url, settings.supabase_service_role_key
    )
    openai_client = OpenAI(
        api_key=settings.openai_api_key,
        timeout=settings.openai_timeout_seconds,
    )
    system_prompt = load_system_prompt()

    app.state.app_state = AppState(
        settings=settings,
        embedder=embedder,
        reranker=reranker,
        supabase=supabase,
        openai=openai_client,
        system_prompt=system_prompt,
    )
    logger.info("Backend startup complete.")

    warmup_task = None
    if settings.warmup_interval_seconds > 0:
        warmup_task = asyncio.create_task(
            keep_warm(app.state.app_state, settings.warmup_interval_seconds)
        )

    try:
        yield
    finally:
        if warmup_task is not None:
            warmup_task.cancel()
        logger.info("Backend shutting down.")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Campus RAG Backend",
        description="RAG question-answering API for campus-rag.",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.api_docs_enabled else None,
        redoc_url="/redoc" if settings.api_docs_enabled else None,
        openapi_url="/openapi.json" if settings.api_docs_enabled else None,
    )

    cors_origins = list(settings.cors_origins)
    for origin in (
        "http://localhost:3000",
        "https://unira-pknu.vercel.app",
    ):
        if origin not in cors_origins:
            cors_origins.append(origin)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    app.include_router(ask_router.router)
    app.include_router(admin_router.router)
    app.include_router(popular_router.router)
    return app


app = create_app()
