from __future__ import annotations

import html
import logging
import re
from typing import List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from lxml import etree, html as lxml_html
import requests
import trafilatura

logger = logging.getLogger(__name__)

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

TRACKING_QUERY_PREFIXES = ("utm_", "at_")
TRACKING_QUERY_KEYS = {"reflink", "fbclid", "gclid", "mc_cid", "mc_eid"}

MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MB cap per article page
MIN_TEXT_CHARS = 220
CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
LATIN_WORD_RE = re.compile(r"\b[A-Za-z0-9_]+\b")


def count_text_metrics(xhtml_or_text: str) -> Tuple[int, int]:
    """Return (latin_word_count, cjk_char_count) from HTML/XHTML or plain text."""
    plain = re.sub(r"<[^>]+>", " ", xhtml_or_text)
    cjk_count = len(CJK_CHAR_RE.findall(plain))
    latin_count = len(LATIN_WORD_RE.findall(plain))
    return latin_count, cjk_count


def clean_url(url: str) -> str:
    """Unescape HTML entities in URLs and strip common tracking query parameters."""
    unescaped = html.unescape(url).strip()
    parsed = urlparse(unescaped)
    if not parsed.query:
        return unescaped
    filtered = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith(TRACKING_QUERY_PREFIXES)
        and k.lower() not in TRACKING_QUERY_KEYS
    ]
    return urlunparse(parsed._replace(query=urlencode(filtered, doseq=True)))


def extract_domain(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def should_skip_url(url: str) -> bool:
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

        new_children: List[etree._Element] = []
        for child in list(el):
            el.remove(child)
            if not isinstance(child.tag, str):
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
        # Drop empty <p> elements left behind after stripping <img>/<script>.
        if tag == "p" and len(el) == 0 and not (el.text and el.text.strip()):
            return None
        return el

    cleaned_root = _clean(root)
    if cleaned_root is None:
        return "", 0

    plain_text = " ".join("".join(cleaned_root.itertext()).split())
    word_count = len(re.findall(r"\b\w+\b", plain_text))

    parts: List[str] = []
    if cleaned_root.text and cleaned_root.text.strip():
        parts.append(f"<p>{html.escape(cleaned_root.text.strip())}</p>")
    for child in cleaned_root:
        parts.append(etree.tostring(child, method="xml", encoding="unicode"))
    xhtml = "\n".join(parts).strip()
    return xhtml, word_count


def fetch_html(session: requests.Session, url: str, timeout: int = 12) -> Optional[str]:
    """Fetch a web page with size cap and content-type check."""
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
            if (
                content_type
                and "html" not in content_type
                and "text/plain" not in content_type
                and "xml" not in content_type
            ):
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


def extract_article_xhtml(raw_html: str, url: str, min_chars: int = MIN_TEXT_CHARS) -> Tuple[str, int]:
    """Extract main readable article body via trafilatura and serialize as strict XHTML."""
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
    if len(" ".join(text_only.split())) < min_chars:
        return "", 0
    return xhtml, word_count
