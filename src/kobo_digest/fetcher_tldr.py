from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
import html
import logging
import re
from typing import List, Optional, Tuple
from xml.etree import ElementTree as ET

from lxml import html as lxml_html
import requests

from .article_extractor import (
    USER_AGENT,
    clean_url,
    extract_article_xhtml,
    extract_domain,
    fetch_html,
    should_skip_url,
)
from .models import Article, Digest

logger = logging.getLogger(__name__)

TLDR_TECH_RSS_URL = "https://tldr.tech/api/rss/tech"
TLDR_TECH_ISSUE_URL = "https://tldr.tech/tech/{issue_date}"
ISSUE_URL_DATE_RE = re.compile(r"/tech/(\d{4}-\d{2}-\d{2})(?:/)?$")
READ_TIME_SUFFIX_RE = re.compile(r"\s*\(\d+\s+minute\s+read\)\s*$", re.IGNORECASE)
SPONSOR_MARKER_RE = re.compile(r"\(\s*(?:sponsor(?:ed)?|\d+\s+minute\s+form)\s*\)", re.IGNORECASE)
PROMO_URL_SUBSTRINGS = ("advertise.tldr.tech", "typeform.com", "jobs.ashbyhq.com/tldr")


@dataclass
class TldrItem:
    section: str
    title: str
    url: str
    summary: str


def strip_non_bmp_and_emoji(text: str) -> str:
    """Remove emojis/symbols that render as missing boxes on e-ink devices."""
    # Strip supplementary Unicode planes (most emojis) plus common BMP dingbats/variation selectors.
    cleaned = re.sub(r"[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0E\uFE0F\u200D]", "", text)
    return " ".join(cleaned.split()).strip()


def parse_rss_issue_links(rss_xml: str) -> List[Tuple[date, str]]:
    """Parse TLDR Tech RSS XML and return [(issue_date, issue_url)] sorted newest first."""
    root = ET.fromstring(rss_xml)
    issues: List[Tuple[date, str]] = []
    seen_dates = set()

    for item in root.findall(".//item"):
        link_el = item.find("link")
        if link_el is None or not link_el.text:
            continue
        url = link_el.text.strip()
        match = ISSUE_URL_DATE_RE.search(url)
        if not match:
            continue
        try:
            issue_date = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if issue_date not in seen_dates:
            seen_dates.add(issue_date)
            issues.append((issue_date, url))

    issues.sort(key=lambda pair: pair[0], reverse=True)
    return issues


def parse_tldr_issue_html(issue_html: str) -> List[TldrItem]:
    """Parse a TLDR Tech web issue page into non-sponsored TldrItem entries."""
    doc = lxml_html.fromstring(issue_html)
    items: List[TldrItem] = []

    for section_el in doc.xpath("//section"):
        raw_header = " ".join(section_el.xpath(".//header//text()")).strip()
        section_name = strip_non_bmp_and_emoji(raw_header)
        if not section_name:
            continue
        if section_name.lower().startswith("together with"):
            continue

        for art_el in section_el.xpath(".//article"):
            raw_title = " ".join(art_el.xpath(".//h3//text()")).strip()
            if not raw_title:
                continue
            if SPONSOR_MARKER_RE.search(raw_title):
                continue

            hrefs = art_el.xpath(".//a[@href]/@href")
            if not hrefs:
                continue
            url = clean_url(hrefs[0])
            if not url.startswith(("http://", "https://")):
                continue
            if any(marker in url.lower() for marker in PROMO_URL_SUBSTRINGS):
                continue

            raw_summary = " ".join(
                art_el.xpath('.//div[contains(@class,"newsletter-html")]//text()')
            ).strip()
            summary = " ".join(raw_summary.split())
            title = READ_TIME_SUFFIX_RE.sub("", raw_title).strip()
            title = strip_non_bmp_and_emoji(title) or raw_title

            items.append(
                TldrItem(
                    section=section_name,
                    title=title,
                    url=url,
                    summary=summary,
                )
            )

    return items


def _enrich_tldr_item(item: TldrItem) -> Article:
    """Fetch the full linked article for a TLDR item, falling back cleanly to the TLDR summary."""
    domain = extract_domain(item.url)
    summary_words = len(re.findall(r"\b\w+\b", item.summary))

    body_xhtml = ""
    word_count = summary_words

    if not should_skip_url(item.url):
        session = requests.Session()
        raw_html = fetch_html(session, item.url)
        if raw_html:
            extracted_xhtml, extracted_words = extract_article_xhtml(raw_html, item.url)
            if extracted_xhtml:
                body_xhtml = extracted_xhtml
                word_count = summary_words + extracted_words

    if not body_xhtml:
        # Keep the story with a clean note when the linked site is paywalled or unreachable.
        body_xhtml = (
            '<p class="fallback-note"><em>Full article could not be fetched '
            "(the source may be paywalled or require JavaScript). "
            "TLDR summary shown above.</em></p>"
        )

    return Article(
        title=item.title,
        url=item.url,
        body_xhtml=body_xhtml,
        source_name="TLDR Tech",
        section=item.section,
        domain=domain,
        word_count=word_count,
        summary=item.summary or None,
    )


def _resolve_issue_for_date(
    session: requests.Session,
    target_date: date,
    max_lookback_days: int = 3,
    exact_date: bool = False,
) -> Optional[Tuple[date, str]]:
    """Find the TLDR Tech issue URL for `target_date` (or the newest issue within lookback window)."""
    if exact_date:
        return target_date, TLDR_TECH_ISSUE_URL.format(issue_date=target_date.isoformat())

    try:
        resp = session.get(TLDR_TECH_RSS_URL, timeout=15, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        issues = parse_rss_issue_links(resp.text)
    except Exception as exc:
        logger.warning("Failed fetching TLDR RSS (%s); trying direct URL for %s", exc, target_date)
        return target_date, TLDR_TECH_ISSUE_URL.format(issue_date=target_date.isoformat())

    min_date = target_date - timedelta(days=max_lookback_days)
    for issue_date, issue_url in issues:
        if min_date <= issue_date <= target_date:
            return issue_date, issue_url

    logger.info(
        "No TLDR Tech issue found between %s and %s (latest in RSS: %s).",
        min_date,
        target_date,
        issues[0][0] if issues else "none",
    )
    return None


def fetch_tldr_digest(
    digest_date: Optional[date] = None,
    max_articles: int = 20,
    max_workers: int = 8,
    exact_date: bool = False,
) -> Optional[Digest]:
    """Fetch the latest TLDR Tech newsletter issue and enrich each link with full article text."""
    target_date = digest_date or date.today()
    session = requests.Session()

    resolved = _resolve_issue_for_date(session, target_date, exact_date=exact_date)
    if resolved is None:
        return None

    issue_date, issue_url = resolved
    logger.info("Fetching TLDR Tech issue for %s: %s", issue_date, issue_url)
    resp = session.get(issue_url, timeout=15, headers={"User-Agent": USER_AGENT})
    if resp.status_code == 404:
        logger.info("TLDR Tech issue %s returned 404 (likely weekend/holiday).", issue_url)
        return None
    resp.raise_for_status()

    tldr_items = parse_tldr_issue_html(resp.text)[:max_articles]
    if not tldr_items:
        logger.warning("Parsed 0 articles from TLDR Tech issue %s.", issue_url)
        return None

    logger.info("Parsed %d TLDR Tech items; fetching full linked articles...", len(tldr_items))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        articles = list(pool.map(_enrich_tldr_item, tldr_items))

    logger.info("Built TLDR Tech digest for %s with %d articles.", issue_date, len(articles))
    return Digest(
        source_code="TLDR",
        source_title="TLDR Tech",
        digest_date=issue_date,
        articles=articles,
    )
