from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import logging
from typing import Any, Dict, List, Optional

import requests

from .article_extractor import (
    extract_article_xhtml,
    extract_domain,
    fetch_html,
    sanitize_fragment_to_xhtml,
    should_skip_url,
)
from .models import Article, Digest

logger = logging.getLogger(__name__)

HN_API_BASE = "https://hacker-news.firebaseio.com/v0"
HN_ITEM_WEB_URL = "https://news.ycombinator.com/item?id={item_id}"


def _fetch_hn_item(session: requests.Session, item_id: int) -> Optional[Dict[str, Any]]:
    try:
        resp = session.get(f"{HN_API_BASE}/item/{item_id}.json", timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict) or data.get("deleted") or data.get("dead"):
            return None
        return data
    except Exception as exc:
        logger.debug("Failed fetching HN item %s: %s", item_id, exc)
        return None


def _process_hn_story(item: Dict[str, Any]) -> Optional[Article]:
    item_id = item.get("id")
    title = (item.get("title") or "").strip()
    if not item_id or not title:
        return None
    if item.get("type") != "story":
        return None

    comments_url = HN_ITEM_WEB_URL.format(item_id=item_id)
    url = (item.get("url") or "").strip()
    score = item.get("score")
    comment_count = item.get("descendants", 0)
    author = item.get("by")

    # Handle Ask HN / Show HN / text-only posts directly.
    if not url:
        text_html = (item.get("text") or "").strip()
        if not text_html:
            return None
        xhtml, word_count = sanitize_fragment_to_xhtml(text_html)
        if not xhtml:
            return None
        return Article(
            title=title,
            url=comments_url,
            body_xhtml=xhtml,
            source_name="Hacker News",
            author=author,
            domain="news.ycombinator.com",
            score=score,
            comment_count=comment_count,
            comments_url=comments_url,
            word_count=word_count,
        )

    if should_skip_url(url):
        logger.debug("Skipping non-article URL: %s", url)
        return None

    session = requests.Session()
    raw_html = fetch_html(session, url)
    if not raw_html:
        return None

    xhtml, word_count = extract_article_xhtml(raw_html, url)
    if not xhtml:
        logger.debug("Could not extract enough readable text from %s", url)
        return None

    return Article(
        title=title,
        url=url,
        body_xhtml=xhtml,
        source_name="Hacker News",
        author=author,
        domain=extract_domain(url),
        score=score,
        comment_count=comment_count,
        comments_url=comments_url,
        word_count=word_count,
    )


def fetch_hn_digest(
    digest_date: Optional[date] = None,
    max_articles: int = 15,
    candidate_pool: int = 40,
    max_workers: int = 8,
) -> Digest:
    """Fetch top Hacker News stories and extract readable full articles into a Digest."""
    target_date = digest_date or date.today()
    session = requests.Session()

    logger.info("Fetching Hacker News top stories...")
    resp = session.get(f"{HN_API_BASE}/topstories.json", timeout=15)
    resp.raise_for_status()
    story_ids: List[int] = resp.json()[: max(candidate_pool, max_articles * 2)]

    # Fetch HN item metadata concurrently, preserving HN front-page rank order.
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        items = list(pool.map(lambda sid: _fetch_hn_item(session, sid), story_ids))

    valid_items = [item for item in items if item is not None]
    logger.info("Fetched %d candidate HN items; extracting articles...", len(valid_items))

    # Extract external articles concurrently while preserving original ranking.
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(_process_hn_story, valid_items))

    articles: List[Article] = []
    for article in results:
        if article is not None:
            articles.append(article)
            if len(articles) >= max_articles:
                break

    logger.info("Collected %d readable Hacker News articles.", len(articles))
    return Digest(
        source_code="HN",
        source_title="Hacker News",
        digest_date=target_date,
        articles=articles,
    )
