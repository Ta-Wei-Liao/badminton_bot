"""Error descriptions end up in log files that friends send to the author."""

import re
from pathlib import Path

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from badminton_bot.utils.errors import describe_error

SECRET = "SECRETCOOKIE"


def cookie_carrying_error(cls=aiohttp.TooManyRedirects):
    """aiohttp 的回應錯誤會帶著整組請求標頭，包含登入 cookie。"""
    url = URL("https://example.invalid/book")
    headers = CIMultiDictProxy(CIMultiDict({"Cookie": f"ASP.NET_SessionId={SECRET}"}))
    info = aiohttp.RequestInfo(url, "GET", headers, url)
    return cls(info, (), status=302, message="")


class TestDescribeError:
    def test_repr_would_leak_the_cookie(self):
        """確認測試本身有意義：repr 真的會洩漏。"""
        assert SECRET in repr(cookie_carrying_error())

    def test_never_includes_request_headers(self):
        for cls in (aiohttp.TooManyRedirects, aiohttp.ClientResponseError):
            assert SECRET not in describe_error(cookie_carrying_error(cls))

    def test_names_the_error_type(self):
        assert describe_error(OSError("模擬連線中斷")) == "OSError: 模擬連線中斷"

    def test_an_error_without_a_message_is_just_its_type(self):
        assert describe_error(TimeoutError()) == "TimeoutError"


def test_no_module_formats_errors_with_repr():
    """%r 和 repr() 會把 aiohttp 錯誤裡的 cookie 帶進 log 或畫面，一律改用 describe_error。"""
    package = Path(__file__).resolve().parent.parent / "badminton_bot"
    offenders = [
        f"{path.name}:{number}"
        for path in package.rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"%r|repr\(\s*(error|e|exc)\s*\)", line)
    ]
    assert offenders == []
