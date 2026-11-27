from __future__ import annotations

from datetime import datetime, timezone
import html
from pathlib import Path
from typing import List
import uuid
import zipfile

from .models import Article, Digest

EINK_CSS = """\
body {
  font-family: Georgia, "Noto Serif", serif;
  line-height: 1.55;
  margin: 0.8em 1em;
}
h1, h2, h3, h4 {
  font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
  line-height: 1.25;
  margin-top: 1.2em;
  margin-bottom: 0.4em;
  page-break-after: avoid;
}
h1.article-title {
  font-size: 1.45em;
  margin-top: 0;
  margin-bottom: 0.3em;
}
.meta {
  font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
  font-size: 0.82em;
  color: #444;
  border-bottom: 1px solid #bbb;
  padding-bottom: 0.55em;
  margin-bottom: 1.1em;
}
.meta a {
  color: #333;
  text-decoration: underline;
}
.summary-box {
  border-left: 3px solid #444;
  background-color: #f4f4f4;
  padding: 0.65em 0.85em;
  margin: 0 0 1.15em 0;
  font-size: 0.94em;
}
.summary-label {
  font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
  font-weight: bold;
  font-size: 0.82em;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 0.3em;
}
.fallback-note {
  color: #555;
  font-size: 0.9em;
}
p {
  margin-top: 0;
  margin-bottom: 0.8em;
}
blockquote {
  border-left: 3px solid #888;
  margin: 0.9em 0;
  padding-left: 0.9em;
  font-style: italic;
}
pre, code {
  font-family: "Courier New", Courier, monospace;
  font-size: 0.86em;
}
pre {
  white-space: pre-wrap;
  word-wrap: break-word;
  border: 1px solid #ccc;
  padding: 0.55em;
  margin: 0.8em 0;
}
ul, ol {
  margin: 0.6em 0 0.8em 1.4em;
  padding: 0;
}
li {
  margin-bottom: 0.35em;
}
table {
  border-collapse: collapse;
  width: 100%;
  margin: 0.8em 0;
  font-size: 0.9em;
}
th, td {
  border: 1px solid #aaa;
  padding: 0.35em 0.5em;
  text-align: left;
}
.toc-list {
  list-style: none;
  margin-left: 0;
  padding-left: 0;
}
.toc-item {
  margin-bottom: 0.75em;
  padding-bottom: 0.55em;
  border-bottom: 1px solid #ddd;
}
.toc-title {
  font-weight: bold;
  font-size: 1.02em;
}
.toc-sub {
  font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
  font-size: 0.8em;
  color: #555;
  margin-top: 0.2em;
}
.article-footer {
  margin-top: 1.6em;
  padding-top: 0.6em;
  border-top: 1px solid #ccc;
  font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
  font-size: 0.8em;
  color: #555;
}
"""


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _article_meta_line(article: Article) -> str:
    bits: List[str] = []
    if article.section:
        bits.append(_esc(article.section))
    if article.domain:
        bits.append(_esc(article.domain))
    if article.score is not None:
        bits.append(f"{article.score} pts")
    if article.comment_count is not None:
        bits.append(f"{article.comment_count} comments")
    if article.author:
        bits.append(f"by {_esc(article.author)}")
    bits.append(f"~{article.read_minutes} min read")
    return " · ".join(bits)


def _render_overview_xhtml(digest: Digest) -> str:
    items_html: List[str] = []
    for idx, article in enumerate(digest.articles, start=1):
        href = f"article_{idx:02d}.xhtml"
        items_html.append(
            f'<li class="toc-item">'
            f'<div class="toc-title"><a href="{href}">{idx}. {_esc(article.title)}</a></div>'
            f'<div class="toc-sub">{_article_meta_line(article)}</div>'
            f"</li>"
        )
    list_markup = "\n".join(items_html)
    total_mins = sum(a.read_minutes for a in digest.articles)
    return f"""\
<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{_esc(digest.title)}</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
  <h1 class="article-title">{_esc(digest.title)}</h1>
  <div class="meta">{len(digest.articles)} articles · ~{total_mins} min total</div>
  <ul class="toc-list">
{list_markup}
  </ul>
</body>
</html>
"""


def _render_article_xhtml(idx: int, total: int, article: Article) -> str:
    meta = _article_meta_line(article)
    links: List[str] = [f'<a href="{_esc(article.url)}">Original article</a>']
    if article.comments_url and article.comments_url != article.url:
        links.append(f'<a href="{_esc(article.comments_url)}">Discussion</a>')
    links.append('<a href="overview.xhtml">Back to contents</a>')
    footer_links = " · ".join(links)

    summary_block = ""
    if article.summary:
        summary_block = (
            '  <div class="summary-box">\n'
            '    <div class="summary-label">TLDR Summary</div>\n'
            f"    <p>{_esc(article.summary)}</p>\n"
            "  </div>\n"
        )

    return f"""\
<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{_esc(article.title)}</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
  <h1 class="article-title">{_esc(article.title)}</h1>
  <div class="meta">Article {idx} of {total} · {meta}</div>
{summary_block}  <div class="article-body">
{article.body_xhtml}
  </div>
  <div class="article-footer">{footer_links}</div>
</body>
</html>
"""


def _render_nav_xhtml(digest: Digest) -> str:
    nav_items = ['<li><a href="overview.xhtml">Front Page Contents</a></li>']
    for idx, article in enumerate(digest.articles, start=1):
        nav_items.append(
            f'<li><a href="article_{idx:02d}.xhtml">{idx}. {_esc(article.title)}</a></li>'
        )
    items_markup = "\n      ".join(nav_items)
    return f"""\
<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Table of Contents</title>
</head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>{_esc(digest.title)}</h1>
    <ol>
      {items_markup}
    </ol>
  </nav>
</body>
</html>
"""


def _render_toc_ncx(book_id: str, digest: Digest) -> str:
    nav_points: List[str] = [
        f"""    <navPoint id="navpoint-1" playOrder="1">
      <navLabel><text>Front Page Contents</text></navLabel>
      <content src="overview.xhtml"/>
    </navPoint>"""
    ]
    for idx, article in enumerate(digest.articles, start=1):
        order = idx + 1
        nav_points.append(
            f"""    <navPoint id="navpoint-{order}" playOrder="{order}">
      <navLabel><text>{idx}. {_esc(article.title)}</text></navLabel>
      <content src="article_{idx:02d}.xhtml"/>
    </navPoint>"""
        )
    nav_map = "\n".join(nav_points)
    return f"""\
<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{_esc(book_id)}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{_esc(digest.title)}</text></docTitle>
  <navMap>
{nav_map}
  </navMap>
</ncx>
"""


def _render_content_opf(book_id: str, digest: Digest) -> str:
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_items = [
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '    <item id="css" href="style.css" media-type="text/css"/>',
        '    <item id="overview" href="overview.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine_items = ['    <itemref idref="overview"/>']
    for idx in range(1, len(digest.articles) + 1):
        manifest_items.append(
            f'    <item id="art_{idx:02d}" href="article_{idx:02d}.xhtml" media-type="application/xhtml+xml"/>'
        )
        spine_items.append(f'    <itemref idref="art_{idx:02d}"/>')

    manifest_xml = "\n".join(manifest_items)
    spine_xml = "\n".join(spine_items)
    return f"""\
<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{_esc(book_id)}</dc:identifier>
    <dc:title>{_esc(digest.title)}</dc:title>
    <dc:creator>{_esc(digest.source_title)} Digest</dc:creator>
    <dc:language>en</dc:language>
    <dc:date>{digest.digest_date.isoformat()}</dc:date>
    <meta property="dcterms:modified">{modified}</meta>
  </metadata>
  <manifest>
{manifest_xml}
  </manifest>
  <spine toc="ncx">
{spine_xml}
  </spine>
</package>
"""


CONTAINER_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def build_epub(digest: Digest, output_path: Path) -> Path:
    """Write a clean, valid EPUB 3 (with EPUB 2 NCX fallback) to output_path."""
    if not digest.articles:
        raise ValueError(f"Cannot build EPUB for {digest.title}: zero articles collected.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(".epub.tmp")
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'kobo-digest:{digest.filename}')}"

    with zipfile.ZipFile(tmp_path, "w") as zf:
        # EPUB spec requires `mimetype` first and uncompressed (ZIP_STORED).
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", CONTAINER_XML, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/style.css", EINK_CSS, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", _render_content_opf(book_id, digest), compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/toc.ncx", _render_toc_ncx(book_id, digest), compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", _render_nav_xhtml(digest), compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/overview.xhtml", _render_overview_xhtml(digest), compress_type=zipfile.ZIP_DEFLATED)

        total = len(digest.articles)
        for idx, article in enumerate(digest.articles, start=1):
            chapter_xhtml = _render_article_xhtml(idx, total, article)
            zf.writestr(
                f"OEBPS/article_{idx:02d}.xhtml",
                chapter_xhtml,
                compress_type=zipfile.ZIP_DEFLATED,
            )

    tmp_path.replace(output_path)
    return output_path
