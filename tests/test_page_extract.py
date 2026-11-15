import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import capture_events, open_log_propagation

from mr_data.online.page_extract import PageExtractor


class _SimpleHTMLHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/landing")
            self.end_headers()
            return
        if self.path == "/no-charset":
            body = (
                "<!DOCTYPE html><html><head><title>No Charset Page</title></head>"
                "<body><article><h1>重要文章标题</h1>"
                "<p>这是页面的主要内容，应当被正确提取。</p></article></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            # 故意不带 charset 参数，模拟老站行为
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"""
<!DOCTYPE html>
<html>
<head><title>Test Page</title></head>
<body>
<nav>Navigation</nav>
<article>
<h1>Important Article</h1>
<p>This is the main content of the page. It should be extracted.</p>
<p>Second paragraph with more details.</p>
</article>
<footer>Footer</footer>
</body>
</html>
"""
        )

    def log_message(self, format, *args):
        pass


@pytest.fixture
def local_html_server():
    server = HTTPServer(("127.0.0.1", 0), _SimpleHTMLHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()


def test_page_extractor_extracts_article(local_html_server):
    extractor = PageExtractor(max_length=500)
    page = extractor.extract(local_html_server)
    assert page is not None
    assert "Important Article" in page.text
    assert "main content" in page.text
    assert "Navigation" not in page.text
    assert "Footer" not in page.text
    assert page.url.rstrip("/") == local_html_server


def test_page_extractor_no_charset_falls_back_to_apparent_encoding(local_html_server):
    extractor = PageExtractor(max_length=500)
    page = extractor.extract(f"{local_html_server}/no-charset")
    assert page is not None
    assert "重要文章标题" in page.text
    assert "主要内容" in page.text


def test_page_extractor_follows_redirect(local_html_server):
    extractor = PageExtractor(max_length=500)
    page = extractor.extract(f"{local_html_server}/redirect")
    assert page is not None
    assert "Important Article" in page.text
    assert page.url == f"{local_html_server}/landing"


def test_page_extractor_invalid_url():
    extractor = PageExtractor()
    assert extractor.extract("") is None
    assert extractor.extract("not-a-url") is None


def test_page_extractor_truncation_logs_event(local_html_server, caplog, monkeypatch):
    """正文超长截断记录 web.extract_truncated（截断调查改进项 A4/C1），

    截尾带省略标记，truncated_chars 为最终返回文本长度（含标记）。"""
    open_log_propagation(caplog, monkeypatch)
    extractor = PageExtractor(max_length=50)

    page = extractor.extract(local_html_server)

    assert page is not None
    assert page.text.endswith("[内容过长，已省略后续部分]")
    truncated = capture_events(caplog, "web.extract_truncated")
    assert len(truncated) == 1
    details = truncated[0].details
    assert details["url"] == page.url
    assert details["original_chars"] > 50
    assert details["truncated_chars"] == len(page.text)
    assert details["truncated_chars"] < details["original_chars"]


def test_page_extractor_default_max_length_is_archive_cap():
    """C1：默认上限为 100,000 字符归档级上限（Tika 100K / Claude Code 100KB 锚点）。"""
    from mr_data.config import settings

    assert settings.web_extract_max_length == 100000
    assert PageExtractor().max_length == 100000
