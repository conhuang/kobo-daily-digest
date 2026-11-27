from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import html
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from lxml import etree, html as lxml_html
import requests
import trafilatura

from .models import Article, Digest

logger = logging.getLogger(__name__)

HN_API_BASE = "https://hacker-news.firebaseio.com/v0"
HN_ITEM_WEB_URL = "https://news.ycombinator.com/item?id={item_id}"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

SKIP_EXTENSIONS = (
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".mp4",
    ".mp3",
    ".webm",
    ".mov",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".dmg",
    ".exe",
)

SKIP_DOMAINS = {
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "m.youtube.com",
    "vimeo.com",
    "twitter.com",
    "x.com",
}

ALLOWED_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "pre",
    "code",
    "ul",
    "ol",
    "li",
    "dl",
    "dt",
    "dd",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "del",
    "sub",
    "sup",
    "br",
    "hr",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
    "a",
    "figure",
    "figcaption",
    "section",
    "article",
    "div",
    "span",
}

DROP_TREE_TAGS = {
    "script",
    "style",
    "noscript",
    "iframe",
    "object",
    "embed",
    "video",
    "audio",
    "canvas",
    "svg",
    "form",
    "input",
    "button",
    "select",
    "textarea",
    "img",
    "picture",
    "source",
    "nav",
    "header",
    "footer",
}

MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MB cap per article page
MIN_TEXT_CHARS = 220


def sanitize_fragment_to_xhtml(raw_html: str) -> Tuple[str, int]:
    """Convert an arbitrary HTML fragment into strict, e-ink-friendly XHTML.

    Returns (xhtml_string, word_count).
    """
    if not raw_html or not raw_html.strip():
        return "", 0

    try:
        root = lxml_html.fragment_fromstring(raw_html, create_parent="div")
    except Exception:
        escaped = html.escape(raw_html)
        words = len(re.findall(r"\w+", raw_html))
        return f"<p>{escaped}</p>", words

    def _clean(el: etree._Element) -> Optional[etree._Element]:
        if not isinstance(el.tag, str):
            return None
        tag = el.tag.lower()
        if tag in DROP_TREE_TAGS:
            return None

        # Strip disallowed attributes; keep only safe ones.
        attrs = dict(el.attrib)
        el.attrib.clear()
        if tag == "a":
            href = attrs.get("href", "").strip()
            if href.startswith(("http://", "https://")):
                el.set("href", href)
        elif tag in ("td", "th"):
            for attr_name in ("colspan", "rowspan"):
                val = attrs.get(attr_name, "").strip()
                if val.isdigit():
                    el.set(attr_name, val)

        # Recurse on children first.
        new_children: List[etree._Element] = []
        for child in list(el):
            el.remove(child)
            if not isinstance(child.tag, str):
                # Preserve tail text of comments/processing instructions.
                if child.tail:
                    if new_children:
                        new_children[-1].tail = (new_children[-1].tail or "") + child.tail
                    else:
                        el.text = (el.text or "") + child.tail
                continue

            child_tag = child.tag.lower()
            if child_tag in DROP_TREE_TAGS:
                if child.tail:
                    if new_children:
                        new_children[-1].tail = (new_children[-1].tail or "") + child.tail
                    else:
                        el.text = (el.text or "") + child.tail
                continue

            cleaned = _clean(child)
            if cleaned is None:
                continue

            if cleaned.tag not in ALLOWED_TAGS:
                # Unwrap unknown tags while keeping their text and children.
                if cleaned.text:
                    if new_children:
                        new_children[-1].tail = (new_children[-1].tail or "") + cleaned.text
                    else:
                        el.text = (el.text or "") + cleaned.text
                for grand in list(cleaned):
                    cleaned.remove(grand)
                    new_children.append(grand)
                if cleaned.tail:
                    if new_children:
                        new_children[-1].tail = (new_children[-1].tail or "") + cleaned.tail
                    else:
                        el.text = (el.text or "") + cleaned.tail
            else:
                new_children.append(cleaned)

        el.tag = tag
        for child in new_children:
            el.append(child)
        return el

    cleaned_root = _clean(root)
    if cleaned_root is None:
        return "", 0

    plain_text = " ".join("".join(cleaned_root.itertext()).split())
    word_count = len(re.findall(r"\b\w+\b", plain_text))

    # Serialize inner contents of the wrapper <div> as strict XML.
    parts: List[str] = []
    if cleaned_root.text and cleaned_root.text.strip():
        parts.append(f"<p>{html.escape(cleaned_root.text.strip())}</p>")
    for child in cleaned_root:
        parts.append(etree.tostring(child, method="xml", encoding="unicode"))
    xhtml = "\n".join(parts).strip()
    return xhtml, word_count


def _extract_domain(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _should_skip_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return True
    host = (parsed.hostname or "").lower()
    if host in SKIP_DOMAINS:
        return True
    path_lower = parsed.path.lower()
    if any(path_lower.endswith(ext) for ext in SKIP_EXTENSIONS):
        return True
    return False


def _fetch_html(session: requests.Session, url: str, timeout: int = 12) -> Optional[str]:
    try:
        with session.get(
            url,
            timeout=timeout,
            stream=True,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        ) as resp:
            if resp.status_code != 200:
                logger.debug("HTTP %s for %s", resp.status_code, url)
                return None
            content_type = resp.headers.get("Content-Type", "").lower()
            if content_type and "html" not in content_type and "text/plain" not in content_type and "xml" not in content_type:
                logger.debug("Skipping non-HTML content-type %s for %s", content_type, url)
                return None

            chunks: List[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=32768):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    logger.debug("Page exceeded %d bytes: %s", MAX_RESPONSE_BYTES, url)
                    return None
                chunks.append(chunk)
            raw = b"".join(chunks)
            encoding = resp.encoding or resp.apparent_encoding or "utf-8"
            return raw.decode(encoding, errors="replace")
    except Exception as exc:
        logger.debug("Failed fetching %s: %s", url, exc)
        return None


def _extract_article_xhtml(raw_html: str, url: str) -> Tuple[str, int]:
    """Extract main readable article body using trafilatura, serialized as strict XHTML."""
    extracted = trafilatura.extract(
        raw_html,
        url=url,
        output_format="html",
        include_formatting=True,
        include_links=True,
        include_tables=True,
        include_images=False,
        include_comments=False,
        favor_precision=False,
    )
    if not extracted:
        return "", 0

    xhtml, word_count = sanitize_fragment_to_xhtml(extracted)
    text_only = re.sub(r"<[^>]+>", " ", xhtml)
    if len(" ".join(text_only.split())) < MIN_TEXT_CHARS:
        return "", 0
    return xhtml, word_count


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

    if _should_skip_url(url):
        logger.debug("Skipping non-article URL: %s", url)
        return None

    session = requests.Session()
    raw_html = _fetch_html(session, url)
    if not raw_html:
        return None

    xhtml, word_count = _extract_article_xhtml(raw_html, url)
    if not xhtml:
        logger.debug("Could not extract enough readable text from %s", url)
        return None

    return Article(
        title=title,
        url=url,
        body_xhtml=xhtml,
        source_name="Hacker News",
        author=author,
        domain=_extract_domain(url),
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
