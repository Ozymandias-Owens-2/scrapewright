"""A server that sends UTF-8 under a bare `text/html` must still decode."""
from scrapewright.http import _decoded


class _Resp:
    def __init__(self, body: bytes, content_type: str):
        self.content = body
        self.headers = {"content-type": content_type}
        self.encoding = "ISO-8859-1"          # what requests defaults to


def test_bare_text_html_that_is_utf8_decodes_as_utf8():
    r = _decoded(_Resp("£51.77".encode("utf-8"), "text/html"))
    assert r.content.decode(r.encoding) == "£51.77"


def test_an_explicit_charset_is_obeyed():
    r = _decoded(_Resp("café".encode("latin-1"), "text/html; charset=ISO-8859-1"))
    assert r.encoding == "ISO-8859-1"


def test_bytes_that_are_not_utf8_are_left_alone():
    r = _decoded(_Resp("café".encode("latin-1"), "text/html"))
    assert r.encoding == "ISO-8859-1"
