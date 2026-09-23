"""Pydantic request/response models for the public API."""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class QueryFilters(BaseModel):
    source: Optional[str] = Field(None, description="website | commit_kb | local")
    content_type: Optional[str] = Field(None, description="e.g. publication, blog, op-ed, person")
    author: Optional[str] = Field(None, max_length=100)
    year: Optional[str] = Field(None, pattern=r"^\d{4}$")
    category: Optional[str] = Field(None, max_length=100)


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=1000)
    mode: Literal["normal", "short", "detailed", "search"] = "normal"
    top_k: int = Field(5, ge=1, le=10)
    filters: QueryFilters = Field(default_factory=QueryFilters)

    @field_validator("query")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 2:
            raise ValueError("query is too short")
        return v


class Citation(BaseModel):
    n: int
    document_id: str = ""
    title: str
    url: str = ""
    source: str = ""
    source_label: str = ""
    access: str = "public"
    content_type: str = ""
    content_type_label: str = ""
    authors: List[str] = []
    author_urls: List[str] = []
    date: str = ""
    publisher: str = ""
    section: str = ""
    page_number: Optional[int] = None
    excerpt: str = ""


class QueryResponse(BaseModel):
    answer: str
    citations: List[Citation]
    sources: List[Citation]            # same records; kept for clients expecting "sources"
    confidence: str
    grounded: bool
    metadata: Dict


class ErrorResponse(BaseModel):
    error: str
    message: str
    request_id: str
