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
    cjk_char_count: int = 0
    summary: Optional[str] = None

    @property
    def read_minutes(self) -> int:
        if self.cjk_char_count > 0:
            # ~260 CJK chars/min (comfortable upper-intermediate learner pace)
            mins = (self.cjk_char_count / 260.0) + (self.word_count / 220.0)
            return max(1, round(mins))
        if self.word_count <= 0:
            return 1
        return max(1, round(self.word_count / 220))


@dataclass
class Digest:
    """A collection of articles for one source on one calendar day."""

    source_code: str  # e.g. "HN", "TLDR", "BBC-ZH", "RUANYF"
    source_title: str  # e.g. "Hacker News", "TLDR Tech", "BBC 中文", "阮一峰科技爱好者周刊"
    digest_date: date
    articles: List[Article] = field(default_factory=list)
    language: str = "en"  # BCP-47 language tag, e.g. "en", "zh-Hans"
    custom_title: Optional[str] = None

    @property
    def filename(self) -> str:
        return f"{self.digest_date.isoformat()}-{self.source_code}.epub"

    @property
    def title(self) -> str:
        if self.custom_title:
            return self.custom_title
        if self.language.startswith("zh"):
            return f"{self.source_title} — {self.digest_date.isoformat()}"
        return f"{self.source_title} Daily — {self.digest_date.isoformat()}"
