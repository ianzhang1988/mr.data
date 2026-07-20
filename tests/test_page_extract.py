import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mr_data.online.page_extract import PageExtractor


class _SimpleHTMLHandler(BaseHTTPRequestHandler):
    def do_GET(self):
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
    text = extractor.extract(local_html_server)
    assert text is not None
    assert "Important Article" in text
    assert "main content" in text
    assert "Navigation" not in text
    assert "Footer" not in text


def test_page_extractor_invalid_url():
    extractor = PageExtractor()
    assert extractor.extract("") is None
    assert extractor.extract("not-a-url") is None
