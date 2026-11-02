"""Application settings loaded from environment variables / .env."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, List

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Supabase
    supabase_url: str = Field(validation_alias=AliasChoices("SUPABASE_URL"))
    supabase_service_role_key: str

    # OpenAI
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"
    openai_timeout_seconds: float = 30.0
    openai_temperature: float = 0.1
    openai_max_tokens: int = 700

    # Embedding (dimension MUST match rag_chunks.embedding column type)
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_device: str = "cpu"
    expected_dimensions: int = 384

    # Retrieval
    rag_top_k: int = 10
    rag_first_stage_k: int = 30
    rag_min_similarity: float = 0.30
    rag_max_chunks_per_url: int = 2
    # How many retrieval RPCs (4 vector + 4 lexical) may run at once; 1 runs
    # them one after another. Was 1 until 2026-10-13: on 09-29, concurrency
    # doubled lexical statement timeouts, but that was before the inactive
    # sources were deleted (014). Re-measured 10-13 on 12 questions x 3 rounds:
    # 0 timeouts at 1/4/8, median 1.82s -> 0.24s at 4 (8 was no faster, worse
    # tail). Production does these 8 calls in ~12.7s sequentially (HF is far
    # from the Seoul DB), so this is most of the search time there.
    rag_rpc_concurrency: int = Field(default=4, ge=1, le=8)
    # NOTE: tried a tighter per-URL cap (1) specifically for lexical-only
    # hits, on the theory that one broad keyword-rich document (e.g. a
    # 2000+ chunk "student life guide" ebook) was crowding out other
    # candidates on lexical coincidence alone. Measured on the 100-case
    # eval: made things worse (33 -> 38 answerable failures, 0 improved /
    # 5 regressed vs. no cap) — that "guide" document turned out to
    # genuinely cover many different administrative topics, so multiple
    # chunks of it are often legitimately relevant, not noise. Left at
    # None (disabled); the two-tier cap machinery in retrieval.py still
    # accepts a value if a future, better-targeted case for it turns up.
    rag_max_lexical_chunks_per_url: int | None = None
    # NOTE: also tried lowering these to 0.20/0.10 when hybrid (BM25 +
    # vector) search shipped, on the theory that lexical hits now carry
    # their own relevance signal via `similarity`. Measured regression on
    # the 100-case eval (33 -> 40 answerable failures): priority_score is
    # doing real work filtering out plausible-sounding noise (e.g. an
    # unrelated "한국어연수과정" PDF outranking the actual grade-appeal
    # regulation once its weight was cut), so keep the original weights.
    rag_priority_weight: float = 0.30
    rag_dataset_priority_weight: float = 0.15
    rag_source_kind_weight: float = 0.10
    # Pull in the best chunks of a notice a rare query word names in its
    # title (see title_anchor.py). Switchable for before/after evals.
    rag_title_anchor_enabled: bool = True
    reranker_enabled: bool = True
    reranker_model: str = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    reranker_device: str = "cpu"
    reranker_weight: float = 0.80
    # int8 dynamic quantization of the reranker's Linear layers (CPU only).
    # Measured 2026-10-02 on 20 real candidate sets: 3.84s -> 2.54s per set
    # (1.5x), but the top-1 changed in 40% of sets, so it's off until a
    # regression eval says answers don't get worse.
    reranker_quantize: bool = False
    rpc_names: Annotated[List[str], NoDecode] = [
        "match_rule_documents",
        "match_pknu_notice_documents",
        "match_pknu_student_life_documents",
        "match_rag_documents",
    ]
    # Chunks average ~870 chars; at 500 the answer model missed facts past
    # the cut (2026-10-19: 멘토 장학금 at char 590). 1000 + title anchor:
    # google_compare 8 -> 14/31, regression 0.401 -> 0.419 (within run noise).
    max_chars_per_chunk: int = 1000
    source_excerpt_max_chars: int = Field(default=320, ge=80, le=2000)

    # Server
    # NoDecode disables pydantic-settings' default JSON-decode for complex types,
    # so a plain "a,b,c" string from .env reaches our validator instead of
    # blowing up in json.loads. JSON list form (["a","b"]) is still accepted
    # by the validator below.
    cors_origins: Annotated[List[str], NoDecode] = ["http://localhost:3000"]
    # /docs, /redoc, /openapi.json. 운영 주소(학교 서버·HF)는 공개라 기본은 끈다.
    # 로컬에서 API를 둘러볼 때만 API_DOCS_ENABLED=true.
    api_docs_enabled: bool = False
    # 관리자 대시보드(/admin/*)에 접근할 수 있는 계정 이메일 (콤마 구분).
    # 비어 있으면 아무도 접근할 수 없다(기본 거부). 대소문자 무시.
    admin_emails: Annotated[List[str], NoDecode] = []
    # 한 계정이 하루(한국 시간 자정 기준)에 /ask를 부를 수 있는 횟수. 분당 20회
    # 제한만으로는 하루 수만 건(OpenAI 요금)까지 갈 수 있어서 둔다. 0이면 끔.
    # 관리자(admin_emails)는 평가·점검용으로 제외.
    ask_daily_limit: int = 100
    # 질문이 없을 때도 이 간격(초)마다 GPT 없이 검색 1회를 돌려 DB를 깨워 둔다
    # (app/warmup.py). 시작 직후에도 1회. 0이면 끔.
    warmup_interval_seconds: float = 600
    log_level: str = "INFO"
    port: int = 8000

    @field_validator("supabase_url")
    @classmethod
    def _validate_supabase_api_url(cls, value: str):
        if value.startswith(("postgres://", "postgresql://")):
            raise ValueError(
                "SUPABASE_URL must be the Supabase API URL "
                "(https://<project-ref>.supabase.co). Put PostgreSQL connection "
                "strings in DATABASE_URL instead."
            )
        return value

    @field_validator("cors_origins", "rpc_names", "admin_emails", mode="before")
    @classmethod
    def _split_csv(cls, value, info):
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            field_label = info.field_name.upper()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"{field_label} looks like JSON but failed to parse: {exc}"
                    ) from exc
                if not isinstance(parsed, list):
                    raise ValueError(f"{field_label} JSON must be an array of strings")
                return [str(item).strip() for item in parsed if str(item).strip()]
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
