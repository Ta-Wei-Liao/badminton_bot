"""Shared offline fakes for exercising GrabRunner end to end. No network, no browser."""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

from badminton_bot.booking_plan import TAIPEI, expand_targets
from badminton_bot.services.sports_center_webservice import HandshakeTiming
from badminton_bot.services.zhongzheng_sports_center_webservice import (
    ZhongzhengSportsCenterWebService,
)


class FakeResponse:
    def __init__(self, body: str):
        self.body = body
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def text(self):
        return self.body

    async def read(self):
        return self.body.encode("utf-8")


class FakeSession:
    """Records every URL; booking requests succeed, everything else is an empty page."""

    def __init__(self):
        self.requested_urls: list[str] = []
        self.closed = False

    def get(self, url, **kwargs):
        self.requested_urls.append(url)
        if "StepFlag=25" in url:
            return FakeResponse("PT=1&X=1")
        return FakeResponse("<html></html>")

    @property
    def booking_urls(self) -> list[str]:
        return [url for url in self.requested_urls if "StepFlag=25" in url]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        # 真的 aiohttp session 關閉時會 await；讓出一次控制權，
        # 才測得出「關到一半被殘留的取消打斷」這種情況
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.closed = True
        return False


class FakeService(ZhongzhengSportsCenterWebService):
    """Zhongzheng's URLs and parsing, with Selenium replaced by bookkeeping."""

    # __init_subclass__ 要求每個子類別自己宣告這四個屬性
    sport_center_name = ZhongzhengSportsCenterWebService.sport_center_name
    login_page_url = ZhongzhengSportsCenterWebService.login_page_url
    booking_window_days = ZhongzhengSportsCenterWebService.booking_window_days
    courts = ZhongzhengSportsCenterWebService.courts

    instances: list["FakeService"] = []

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.logged_in = False
        self.logged_out = False
        self.session = FakeSession()
        FakeService.instances.append(self)

    def login(self) -> None:
        self.logged_in = True

    def logout(self) -> None:
        self.logged_out = True

    @property
    def login_status(self) -> bool:
        return self.logged_in

    def get_cookies(self):
        return {}

    def create_session(self, cookies, referer):
        self.handshake_timing = HandshakeTiming()
        return self.session


@pytest.fixture
def offline_engine(monkeypatch):
    """Swap every side effect GrabRunner has for an offline stand-in.

    Returns the list check_browser calls are recorded into.
    """
    from badminton_bot import engine

    browser_checks: list[bool] = []

    @contextmanager
    def fake_keep_awake():
        yield

    monkeypatch.setattr(engine, "check_browser", lambda: browser_checks.append(True))
    monkeypatch.setattr(engine, "keep_awake", fake_keep_awake)
    monkeypatch.setattr(engine, "query_clock_offset", lambda *a, **k: None)
    monkeypatch.setattr(engine, "POST_CHECK_DELAY_SECONDS", 0.0)
    FakeService.instances.clear()
    yield browser_checks
    FakeService.instances.clear()


def make_targets(qpids=(1196, 1199), hours=(20, 21), day=None):
    """Targets for the given Zhongzheng courts x hours, a week from today."""
    courts = [c for c in ZhongzhengSportsCenterWebService.courts if c.qpid in qpids]
    day = day or (datetime.now(TAIPEI) + timedelta(days=7)).date()
    return expand_targets(courts, hours, day)
