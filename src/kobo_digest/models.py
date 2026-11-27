from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional


@dataclass
class Article:
    """A single cleaned article ready to be rendered as an EPUB chapter."""

    title: str
    url: str
    body_xhtml: str
    source_name: str
    section: Optional[str] = None
    author: Optional[str] = None
    domain: Optional[str] = None
    score: Optional[int] = None
    comment_count: Optional[int] = None
    comments_url: Optional[str] = None
    word_count: int = 0
    summary: Optional[str] = None

    @property
    def read_minutes(self) -> int:
        if self.word_count <= 0:
            return 1
        return max(1, round(self.word_count / 220))


@dataclass
class Digest:
    """A collection of articles for one source on one calendar day."""

    source_code: str  # e.g. "HN", "TLDR"
    source_title: str  # e.g. "Hacker News", "TLDR Tech"
    digest_date: date
    articles: List[Article] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return f"{self.digest_date.isoformat()}-{self.source_code}.epub"

    @property
    def title(self) -> str:
        return f"{self.source_title} Daily — {self.digest_date.isoformat()}"
