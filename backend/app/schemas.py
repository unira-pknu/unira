"""Request/response Pydantic models for the public API."""

from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4000)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    # Prior turns of the same conversation, oldest first. Capped at 20 turns
    # (10 exchanges) so a long-running chat can't unboundedly grow the prompt
    # sent to OpenAI on every request.
    chat_history: List[ChatMessage] = Field(default_factory=list, max_length=20)
    # true: text/event-stream with step updates and answer text as it is
    # written (the chat screen). false (default): one JSON AskResponse, which
    # eval and comparison scripts rely on.
    stream: bool = False



class Attachment(BaseModel):
    name: str
    url: str


class Source(BaseModel):
    title: str
    uri: str
    content: str
    similarity: float
    priority_score: float = 0.0
    dataset_priority: float = 0.0
    final_score: float = 0.0
    rerank_score: float = 0.0
    rerank_final_score: float = 0.0
    attachments: List[Attachment] = Field(default_factory=list)


class AskResponse(BaseModel):
    answer: str
    sources: List[Source]
    # False when retrieval found nothing or the model gave the "no info"
    # refusal (system prompt rule 5). Defaults to True so older clients and
    # tests that ignore the field keep working.
    answerable: bool = True
