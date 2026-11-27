from __future__ import annotations

from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET
import zipfile

from kobo_digest.article_extractor import count_text_metrics
from kobo_digest.epub_builder import build_epub
from kobo_digest.fetcher_bbc_zh import (
    normalize_bbc_simp_url,
    parse_bbc_rss_items,
    strip_bbc_article_boilerplate,
)
from kobo_digest.fetcher_ruanyf import (
    parse_ruanyf_atom_weekly_entries,
    split_weekly_html_into_chapters,
)
from kobo_digest.models import Article, Digest

SAMPLE_BBC_RSS = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>測試標題</title>
    <link>https://www.bbc.com/zhongwen/articles/c123456789o/trad?at_medium=RSS&amp;at_campaign=rss</link>
    <pubDate>Fri, 27 Nov 2026 04:22:48 GMT</pubDate>
  </item>
  <item>
    <title>影片頁</title>
    <link>https://www.bbc.com/zhongwen/trad/media-12345</link>
    <pubDate>Fri, 27 Nov 2026 04:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""

SAMPLE_RUANYF_ATOM = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>科技爱好者周刊（第 421 期）：AI 的疯狂造富</title>
    <link href="http://www.ruanyifeng.com/blog/2026/11/weekly-issue-421.html" />
    <published>2026-11-27T00:10:30Z</published>
    <content type="html"><![CDATA[
      <h2>封面图</h2>
      <p><img src="https://example.com/cover.jpg" /></p>
      <h2>AI 的疯狂造富</h2>
      <p>人工智能行业今年出现了前所未有的资本聚集与估值飙升，许多初创公司在极短时间内完成了巨额融资，深刻改变了硅谷的创业节奏与人才流向。</p>
      <h2>科技动态</h2>
      <p>开源社区本周发布了多项面向本地推理优化的新工具，使普通消费级硬件能够更高效地运行中等规模语言模型，进一步降低了开发门槛。</p>
      <h2>往年回顾</h2>
      <p>去年的周刊链接列表。</p>
    ]]></content>
  </entry>
  <entry>
    <title>普通博客文章</title>
    <link href="http://www.ruanyifeng.com/blog/2026/11/other.html" />
    <published>2026-11-26T00:00:00Z</published>
    <content type="html">&lt;p&gt;not weekly&lt;/p&gt;</content>
  </entry>
</feed>
"""


def test_normalize_bbc_simp_url():
    raw = "https://www.bbc.com/zhongwen/articles/c3j4rw1r4p29o/trad?at_medium=RSS&at_campaign=rss"
    assert normalize_bbc_simp_url(raw) == "https://www.bbc.com/zhongwen/articles/c3j4rw1r4p29o/simp"


def test_parse_bbc_rss_filters_non_articles():
    items = parse_bbc_rss_items(SAMPLE_BBC_RSS)
    assert len(items) == 1
    assert items[0][1] == "https://www.bbc.com/zhongwen/articles/c123456789o/simp"
    assert items[0][2] == date(2026, 11, 27)


def test_strip_bbc_article_boilerplate():
    raw = (
        "<h1>测试标题</h1>"
        "<p><strong>Article Information</strong></p>"
        "<ul><li>Author, 记者</li><li>阅读时间: 3 分钟</li></ul>"
        "<p>正文第一段内容。</p>"
    )
    cleaned = strip_bbc_article_boilerplate(raw, "测试标题")
    assert "Article Information" not in cleaned
    assert "阅读时间" not in cleaned
    assert "正文第一段内容。" in cleaned


def test_parse_and_split_ruanyf_weekly():
    entries = parse_ruanyf_atom_weekly_entries(SAMPLE_RUANYF_ATOM)
    assert len(entries) == 1
    title, url, pub_date, content_html = entries[0]
    assert pub_date == date(2026, 11, 27)

    chapters = split_weekly_html_into_chapters(content_html, url, title)
    assert [c.title for c in chapters] == ["AI 的疯狂造富", "科技动态"]
    assert chapters[0].cjk_char_count >= 50


def test_cjk_read_minutes_and_zh_epub_output(tmp_path: Path):
    text = "人工智能正在快速改变软件工程的工作方式。" * 30  # ~600 CJK chars
    latin, cjk = count_text_metrics(text)
    assert latin == 0
    assert cjk > 500

    digest = Digest(
        source_code="BBC-ZH",
        source_title="BBC 中文",
        digest_date=date(2026, 11, 27),
        language="zh-Hans",
        articles=[
            Article(
                title="科技观察",
                url="https://www.bbc.com/zhongwen/articles/test/simp",
                body_xhtml=f"<p>{text}</p>",
                source_name="BBC 中文",
                domain="bbc.com",
                word_count=latin,
                cjk_char_count=cjk,
            )
        ],
    )
    assert digest.articles[0].read_minutes >= 2

    out = tmp_path / digest.filename
    build_epub(digest, out)
    with zipfile.ZipFile(out, "r") as zf:
        opf = zf.read("OEBPS/content.opf").decode("utf-8")
        assert "<dc:language>zh-Hans</dc:language>" in opf
        overview = zf.read("OEBPS/overview.xhtml").decode("utf-8")
        assert 'xml:lang="zh-Hans"' in overview
        assert "共 1 篇" in overview
        chapter = zf.read("OEBPS/article_01.xhtml").decode("utf-8")
        assert "返回目录" in chapter
        assert "Article 1 of" not in chapter  # pure Chinese chrome
        for name in zf.namelist():
            if name.endswith((".xml", ".xhtml", ".opf", ".ncx")):
                ET.fromstring(zf.read(name))
