from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
import logging
import re
from typing import List, Optional, Tuple
from xml.etree import ElementTree as ET

from lxml import etree, html as lxml_html
import requests

from .article_extractor import (
    USER_AGENT,
    clean_url,
    count_text_metrics,
    extract_article_xhtml,
    fetch_html,
    sanitize_fragment_to_xhtml,
)
from .models import Article, Digest

logger = logging.getLogger(__name__)

BBC_ZH_RSS_URL = "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml"
BBC_TRAD_SUFFIX_RE = re.compile(r"/trad(?=[/?#]|$)")
BBC_MIN_CJK_CHARS = 120


def normalize_bbc_simp_url(url: str) -> str:
    """Convert BBC Chinese RSS URLs (which often point to /trad) to /simp and strip tracking params."""
    cleaned = clean_url(url)
    return BBC_TRAD_SUFFIX_RE.sub("/simp", cleaned)


def parse_bbc_rss_items(rss_xml: bytes | str) -> List[Tuple[str, str, Optional[date]]]:
    """Parse BBC Chinese RSS into [(title, simp_url, pub_date)]."""
    root = ET.fromstring(rss_xml)
    items: List[Tuple[str, str, Optional[date]]] = []
    seen_urls = set()

    for item in root.findall(".//item"):
        raw_link = (item.findtext("link") or "").strip()
        if not raw_link or "/articles/" not in raw_link:
            # Skip live/video hub links that aren't readable articles.
            continue
        simp_url = normalize_bbc_simp_url(raw_link)
        if simp_url in seen_urls:
            continue
        seen_urls.add(simp_url)

        title = (item.findtext("title") or "").strip()
        pub_date: Optional[date] = None
        raw_pub = (item.findtext("pubDate") or "").strip()
        if raw_pub:
            try:
                pub_date = parsedate_to_datetime(raw_pub).date()
            except Exception:
                pub_date = None
        items.append((title, simp_url, pub_date))

    return items


def strip_bbc_article_boilerplate(body_xhtml: str, article_title: str) -> str:
    """Remove duplicate leading <h1> and BBC's 'Article Information' metadata list."""
    if not body_xhtml:
        return body_xhtml
    try:
        wrapper = lxml_html.fragment_fromstring(body_xhtml, create_parent="div")
    except Exception:
        return body_xhtml

    children = list(wrapper)
    drop_next_ul = False
    for child in children:
        tag = child.tag.lower() if isinstance(child.tag, str) else ""
        text = " ".join("".join(child.itertext()).split()).strip()

        if tag == "h1" and (text == article_title or article_title in text):
            wrapper.remove(child)
            continue

        if text.lower() == "article information":
            wrapper.remove(child)
            drop_next_ul = True
            continue

        if drop_next_ul and tag == "ul":
            wrapper.remove(child)
            drop_next_ul = False
            continue

        # Also drop standalone "Author, ..." / "阅读时间" metadata lists if header wasn't present.
        if tag == "ul" and ("Author," in text or "Published" in text or "阅读时间" in text) and len(text) < 220:
            wrapper.remove(child)
            continue

        drop_next_ul = False

    parts = [etree.tostring(c, method="xml", encoding="unicode") for c in wrapper]
    return "\n".join(parts).strip()


def _fetch_bbc_article(entry: Tuple[str, str, Optional[date]]) -> Optional[Article]:
    fallback_title, simp_url, _ = entry
    session = requests.Session()
    raw_html = fetch_html(session, simp_url, timeout=15)
    if not raw_html:
        return None

    # Prefer the Simplified Chinese <h1> from the /simp page over the RSS feed title.
    title = fallback_title
    try:
        doc = lxml_html.fromstring(raw_html)
        h1_text = " ".join(doc.xpath("//h1//text()")).strip()
        if h1_text:
            title = h1_text
    except Exception:
        pass

    body_xhtml, _ = extract_article_xhtml(raw_html, simp_url, min_chars=BBC_MIN_CJK_CHARS)
    if not body_xhtml:
        return None

    body_xhtml = strip_bbc_article_boilerplate(body_xhtml, title)
    body_xhtml, _ = sanitize_fragment_to_xhtml(body_xhtml)
    latin_words, cjk_chars = count_text_metrics(body_xhtml)
    if cjk_chars < BBC_MIN_CJK_CHARS:
        return None

    return Article(
        title=title,
        url=simp_url,
        body_xhtml=body_xhtml,
        source_name="BBC 中文",
        domain="bbc.com",
        word_count=latin_words,
        cjk_char_count=cjk_chars,
    )


def fetch_bbc_zh_digest(
    digest_date: Optional[date] = None,
    max_articles: int = 10,
    max_age_days: int = 2,
    max_workers: int = 6,
) -> Optional[Digest]:
    """Fetch recent BBC 中文 (Simplified Chinese) news articles into a pure-Chinese Digest."""
    target_date = digest_date or date.today()
    session = requests.Session()

    logger.info("Fetching BBC 中文 RSS feed...")
    resp = session.get(BBC_ZH_RSS_URL, timeout=15, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()

    all_items = parse_bbc_rss_items(resp.content)
    cutoff = target_date - timedelta(days=max_age_days)
    recent_items = [
        item for item in all_items if item[2] is None or item[2] >= cutoff
    ]
    candidates = recent_items[: max(max_articles * 2, 18)]
    if not candidates:
        candidates = all_items[: max(max_articles * 2, 18)]

    logger.info("Fetching %d BBC 中文 (/simp) candidate articles...", len(candidates))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(_fetch_bbc_article, candidates))

    articles: List[Article] = []
    for art in results:
        if art is not None:
            articles.append(art)
            if len(articles) >= max_articles:
                break

    if not articles:
        return None

    logger.info("Collected %d BBC 中文 Simplified articles.", len(articles))
    return Digest(
        source_code="BBC-ZH",
        source_title="BBC 中文",
        digest_date=target_date,
        articles=articles,
        language="zh-Hans",
    )
