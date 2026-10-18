"""Tests for the webservice subclasses.

Every test here builds an instance with ``object.__new__`` so that ``__init__``
never runs — that keeps Chrome from launching and, more importantly, keeps the
suite from touching the live booking sites. Only the pure parts (URL building,
response parsing, subclass contract) are exercised.
"""

import asyncio
from unittest.mock import Mock

import pytest
from selenium.common.exceptions import NoAlertPresentException, NoSuchElementException

from badminton_bot.services.sports_center_webservice import (
    BROWSER_USER_AGENT,
    CONNECT_TIMEOUT_SECONDS,
    SESSION_TIMEOUT_SECONDS,
    SLOT_AVAILABLE,
    SLOT_TAKEN,
    SLOT_UNKNOWN,
    BookingAttempt,
    SportsCenterWebService,
    WarmUpResult,
    build_browser_headers,
)
from badminton_bot.services.zhongshan_sports_center_webservice import (
    ZhongshanSportsCenterWebService,
)
from badminton_bot.services.zhongzheng_sports_center_webservice import (
    ZhongzhengSportsCenterWebService,
)

SERVICE_CLASSES = [ZhongshanSportsCenterWebService, ZhongzhengSportsCenterWebService]


def build_without_browser(service_cls: type[SportsCenterWebService]):
    """Instantiate a service without running __init__ (i.e. without opening Chrome)."""
    return object.__new__(service_cls)


class TestSubclassContract:
    @pytest.mark.parametrize("service_cls", SERVICE_CLASSES)
    def test_shipped_services_declare_every_required_attribute(self, service_cls):
        for attr in (
            "sport_center_name",
            "login_page_url",
            "booking_window_days",
            "target_qpid",
        ):
            assert attr in service_cls.__dict__

    def test_subclass_missing_a_required_attribute_fails_at_definition_time(self):
        with pytest.raises(TypeError, match="booking_window_days"):

            class Incomplete(SportsCenterWebService):
                sport_center_name = "測試運動中心"
                login_page_url = "https://example.invalid/login"
                # booking_window_days deliberately omitted

    def test_booking_window_days_differ_per_center(self):
        assert ZhongshanSportsCenterWebService.booking_window_days == 14
        assert ZhongzhengSportsCenterWebService.booking_window_days == 7


class TestGenerateBookingUrl:
    def test_zhongshan_zero_pads_the_hour(self):
        url = build_without_browser(ZhongshanSportsCenterWebService)._generate_booking_url(
            year=2025, month=4, day=12, hour=9
        )
        assert "QTime=09" in url
        assert "D=2025/04/12" in url
        assert url.startswith("https://scr.cyc.org.tw/tp01.aspx?")

    def test_zhongzheng_does_not_zero_pad_the_hour(self):
        url = build_without_browser(ZhongzhengSportsCenterWebService)._generate_booking_url(
            year=2025, month=4, day=12, hour=9
        )
        assert "QTime=9" in url
        assert "QTime=09" not in url
        assert "D=2025/04/12" in url
        assert url.startswith("https://bwd.xuanen.com.tw/wd27.aspx?")

    @pytest.mark.parametrize(
        ("service_cls", "expected_qpid"),
        [(ZhongshanSportsCenterWebService, "QPid=84"), (ZhongzhengSportsCenterWebService, "QPid=1199")],
    )
    def test_each_center_targets_its_own_venue_id(self, service_cls, expected_qpid):
        url = build_without_browser(service_cls)._generate_booking_url(
            year=2025, month=4, day=12, hour=20
        )
        assert expected_qpid in url

    @pytest.mark.parametrize("service_cls", SERVICE_CLASSES)
    def test_month_and_day_are_always_zero_padded(self, service_cls):
        url = build_without_browser(service_cls)._generate_booking_url(
            year=2025, month=12, day=5, hour=20
        )
        assert "D=2025/12/05" in url


class TestIsBookingSuccess:
    @pytest.mark.parametrize("service_cls", SERVICE_CLASSES)
    def test_x_equals_1_means_booked(self, service_cls):
        service = build_without_browser(service_cls)
        assert service._is_booking_success(text="...&PT=1&X=1&...") is True

    @pytest.mark.parametrize("service_cls", SERVICE_CLASSES)
    def test_x_equals_2_means_taken(self, service_cls):
        service = build_without_browser(service_cls)
        assert service._is_booking_success(text="...&PT=1&X=2&...") is False

    @pytest.mark.parametrize("service_cls", SERVICE_CLASSES)
    def test_unrecognised_response_raises(self, service_cls):
        service = build_without_browser(service_cls)
        with pytest.raises(RuntimeError):
            service._is_booking_success(text="<html>維護中</html>")

    @pytest.mark.parametrize("service_cls", SERVICE_CLASSES)
    def test_failure_marker_wins_when_both_markers_appear(self, service_cls):
        """The failure branch is checked first, so a page containing both is a failure."""
        service = build_without_browser(service_cls)
        assert service._is_booking_success(text="PT=1&X=2 ... PT=1&X=1") is False


class _FakeElement:
    """A page element that records whether it was clicked / typed into."""

    def __init__(self, text: str = "測試使用者") -> None:
        self.text = text
        self.clicked = False
        self.keys: list[str] = []

    def is_displayed(self) -> bool:
        return True

    def click(self) -> None:
        self.clicked = True

    def send_keys(self, value: str) -> None:
        self.keys.append(value)


class _FakeAlert:
    """A native JS alert that records whether it was accepted."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.accepted = False

    def accept(self) -> None:
        self.accepted = True


class _FakeSwitchTo:
    def __init__(self, driver: "_FakeDriver") -> None:
        self._driver = driver

    @property
    def alert(self) -> _FakeAlert:
        return self._driver._poll_for_alert()


class _FakeDriver:
    """Driver whose alerts only show up after a few polls.

    登入頁的兩個 alert 是網頁載入後才跳出來的（實測約 0.5 ~ 0.9 秒），
    所以 driver.get() 回來的當下還沒有 alert 可以切換。
    """

    def __init__(
        self,
        alert_texts: tuple[str, ...],
        polls_before_each_alert: int = 1,
        delayed_selectors: dict[str, int] | None = None,
    ) -> None:
        self.alerts = [_FakeAlert(text) for text in alert_texts]
        self._polls_before_each_alert = polls_before_each_alert
        self._polls = 0
        self.switch_to = _FakeSwitchTo(self)
        self.executed_scripts: list[str] = []
        # {selector: 還要再被找幾次才會出現}，用來模擬延遲渲染的元素
        self._delayed_selectors = dict(delayed_selectors or {})
        self.elements: dict[str, _FakeElement] = {}

    def _poll_for_alert(self) -> _FakeAlert:
        pending = [alert for alert in self.alerts if not alert.accepted]
        if not pending:
            raise NoAlertPresentException("no such alert")

        self._polls += 1
        if self._polls <= self._polls_before_each_alert:
            raise NoAlertPresentException("no such alert")

        self._polls = 0
        return pending[0]

    def find_element(self, by, value):
        remaining = self._delayed_selectors.get(value, 0)
        if remaining > 0:
            self._delayed_selectors[value] = remaining - 1
            raise NoSuchElementException(f"no such element: {value}")

        return self.elements.setdefault(value, _FakeElement())

    def quit(self):
        pass

    def execute_script(self, script, *args):
        self.executed_scripts.append(script)


class TestLoginWaitsForTheAlerts:
    """登入頁的 alert 是延遲跳出的，login() 必須真的輪詢等待，不能只看一次。"""

    @staticmethod
    def _build(service_cls, **kwargs):
        service = build_without_browser(service_cls)
        service._driver = _FakeDriver(
            alert_texts=("這只是提醒！！！", "報名運動課程請使用個人帳密報名繳費"), **kwargs
        )
        service._SportsCenterWebService__is_login = False
        service._SportsCenterWebService__username = "A123456789"
        service._SportsCenterWebService__password = "secret"
        return service

    @pytest.mark.parametrize("service_cls", SERVICE_CLASSES)
    def test_login_accepts_both_delayed_alerts(self, service_cls):
        service = self._build(service_cls)

        service.login()

        assert [alert.accepted for alert in service._driver.alerts] == [True, True]
        assert service.login_status is True
        assert "DoSubmit()" in service._driver.executed_scripts

    @pytest.mark.parametrize("service_cls", SERVICE_CLASSES)
    def test_login_still_works_when_the_alert_is_slow(self, service_cls):
        """就算 alert 慢了好幾次輪詢才出現，也要等到它。"""
        service = self._build(service_cls, polls_before_each_alert=4)

        service.login()

        assert [alert.accepted for alert in service._driver.alerts] == [True, True]
        assert service.login_status is True


class TestLoginWaitsForDelayedElements:
    """兩個 alert 關掉之後才渲染出來的元素，也必須等，不能立刻就找。"""

    @staticmethod
    def _build(service_cls, delayed_selectors):
        service = build_without_browser(service_cls)
        service._driver = _FakeDriver(
            alert_texts=("這只是提醒！！！", "報名運動課程請使用個人帳密報名繳費"),
            delayed_selectors=delayed_selectors,
        )
        service._SportsCenterWebService__is_login = False
        service._SportsCenterWebService__username = "A123456789"
        service._SportsCenterWebService__password = "secret"
        return service

    @pytest.mark.parametrize("service_cls", SERVICE_CLASSES)
    def test_login_waits_for_the_delayed_confirm_popup(self, service_cls):
        """詐騙提醒的 SweetAlert2 視窗在 alert 關掉後才出現（實測差約 50 毫秒）。"""
        service = self._build(service_cls, {"swal2-actions": 3})

        service.login()

        assert service._driver.elements["swal2-actions"].clicked is True
        assert service.login_status is True

    @pytest.mark.parametrize("service_cls", SERVICE_CLASSES)
    def test_login_waits_for_the_welcome_message_after_submit(self, service_cls):
        """DoSubmit() 之後頁面要重新導向，歡迎訊息不會馬上就在 DOM 裡。"""
        service = self._build(service_cls, {"//span[@id='lab_Name']": 3})

        service.login()

        assert service.login_status is True
        assert "DoSubmit()" in service._driver.executed_scripts


class TestTargetQpidContract:
    def test_both_centres_declare_their_target_court(self):
        assert ZhongzhengSportsCenterWebService.target_qpid == 1199
        assert ZhongshanSportsCenterWebService.target_qpid == 84

    def test_a_subclass_without_target_qpid_is_rejected_at_import_time(self):
        """__init_subclass__ 在類別定義時就擋下來，而不是等到執行期才炸。"""
        with pytest.raises(TypeError, match="target_qpid"):

            class Incomplete(SportsCenterWebService):
                sport_center_name = "測試中心"
                login_page_url = "https://example.invalid/login"
                booking_window_days = 7

    def test_the_booking_url_is_built_from_the_class_attribute(self):
        """換場地應該只要改 target_qpid 一行。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        url = service._generate_booking_url(year=2026, month=9, day=17, hour=20)
        assert f"QPid={ZhongzhengSportsCenterWebService.target_qpid}" in url


class TestListPageUrl:
    def test_zhongzheng_list_page_is_the_read_only_step_flag(self):
        """StepFlag=2 是列表頁，StepFlag=25 才會真的送出預約 —— 探測絕不能碰後者。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        url = service._generate_list_page_url(year=2026, month=9, day=17)
        assert url == (
            "https://bwd.xuanen.com.tw/wd27.aspx?module=net_booking"
            "&files=booking_place&StepFlag=2&PT=1&D=2026/09/17"
        )
        assert "StepFlag=25" not in url

    def test_zhongshan_list_page_is_the_read_only_step_flag(self):
        service = build_without_browser(ZhongshanSportsCenterWebService)
        url = service._generate_list_page_url(year=2026, month=9, day=17)
        assert url == (
            "https://scr.cyc.org.tw/tp01.aspx?module=net_booking"
            "&files=booking_place&StepFlag=2&PT=1&D=2026/09/17"
        )
        assert "StepFlag=25" not in url

    def test_the_date_is_zero_padded(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        url = service._generate_list_page_url(year=2026, month=1, day=5)
        assert "D=2026/01/05" in url


class TestWarmUpUrls:
    def test_warms_two_different_pages(self):
        """兩條熱連線供兩個預約請求各用一條；兩個不同頁面併發載入是一般瀏覽行為。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        urls = service._generate_warm_up_urls()
        assert len(urls) == 2
        assert len(set(urls)) == 2

    def test_warm_up_never_touches_the_booking_action(self):
        for cls in (ZhongzhengSportsCenterWebService, ZhongshanSportsCenterWebService):
            service = build_without_browser(cls)
            for url in service._generate_warm_up_urls():
                assert "StepFlag=25" not in url



# 合成的 fixture，依照既有筆記寫成：可預約的格子帶 Step3Action 呼叫，
# 已被預約的格子只有 place02 圖與 title。尚未與真實網頁核對過。
LIST_PAGE_HTML = """
<table>
  <tr>
    <td><a onclick="Step3Action(1199, 19)"><img src="img/place01.png"></a></td>
    <td><img src="img/place02.png" title="已被預約"></td>
    <td><a onclick="Step3Action(1199, 21)"><img src="img/place01.png"></a></td>
    <td><a onclick="Step3Action(1196, 20)"><img src="img/place01.png"></a></td>
  </tr>
</table>
"""

LIST_PAGE_HTML_PADDED_HOUR = """
<td><a onclick="Step3Action(84, 09)"><img src="img/place01.png"></a></td>
"""


class TestParseSlotState:
    def test_an_open_slot_is_reported_available(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        assert service.parse_slot_state(LIST_PAGE_HTML, hour=19) == SLOT_AVAILABLE

    def test_a_slot_without_its_click_handler_is_reported_taken(self):
        """20:00 那格的 Step3Action 呼叫不見了 —— 被別人訂走了。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        assert service.parse_slot_state(LIST_PAGE_HTML, hour=20) == SLOT_TAKEN

    def test_another_court_at_the_same_hour_does_not_count(self):
        """1196 的 20:00 還空著，但我們要的是 1199。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        assert service.parse_slot_state(LIST_PAGE_HTML, hour=20) == SLOT_TAKEN

    def test_a_page_that_never_mentions_our_court_is_unknown(self):
        """預約窗口還沒開時列表頁可能根本不渲染那一天 —— 那是未知，不是已訂。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        assert service.parse_slot_state("<html>查無資料</html>", hour=20) == SLOT_UNKNOWN

    def test_tolerates_a_zero_padded_hour(self):
        """中山的 QTime 補零、中正不補，解析不該依賴這個差異。"""
        service = build_without_browser(ZhongshanSportsCenterWebService)
        assert (
            service.parse_slot_state(LIST_PAGE_HTML_PADDED_HOUR, hour=9)
            == SLOT_AVAILABLE
        )

    def test_tolerates_whitespace_in_the_call(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        html = '<a onclick="Step3Action( 1199 , 20 )">'
        assert service.parse_slot_state(html, hour=20) == SLOT_AVAILABLE

    def test_incidental_substring_in_unrelated_content_does_not_trigger_taken(self):
        """中山的 QPid=84 太短，很容易在圖檔名裡撞到（place0841.png）。
        那不代表我們的場地被預約了 —— 只是巧合的字串。不該謊報成已訂。"""
        service = build_without_browser(ZhongshanSportsCenterWebService)
        html = '<img src="img/place0841.png">'
        assert service.parse_slot_state(html, hour=20) == SLOT_UNKNOWN

    def test_our_court_at_another_hour_reads_as_taken(self):
        """我們的場地確實出現在頁面上（Step3Action 呼叫），只是不在我們查詢的時段。
        那表示那個時段已經被訂走了。"""
        service = build_without_browser(ZhongshanSportsCenterWebService)
        html = '<a onclick="Step3Action(84, 19)"><img src="img/place01.png"></a>'
        assert service.parse_slot_state(html, hour=20) == SLOT_TAKEN


class TestBrowserHeaders:
    def test_the_user_agent_is_not_the_aiohttp_default(self):
        """預設的 Python/aiohttp UA 等於在 log 裡自報身分。"""
        assert "aiohttp" not in BROWSER_USER_AGENT
        assert "Python" not in BROWSER_USER_AGENT
        assert BROWSER_USER_AGENT.startswith("Mozilla/5.0")

    def test_headers_carry_the_referer_they_were_given(self):
        headers = build_browser_headers(referer="https://example.invalid/list")
        assert headers["Referer"] == "https://example.invalid/list"

    def test_headers_ask_for_traditional_chinese(self):
        headers = build_browser_headers(referer="https://example.invalid/list")
        assert headers["Accept-Language"].startswith("zh-TW")

    def test_headers_use_the_shared_user_agent(self):
        headers = build_browser_headers(referer="https://example.invalid/list")
        assert headers["User-Agent"] == BROWSER_USER_AGENT


class TestChromeOptions:
    def test_the_user_agent_argument_uses_the_flag_chrome_understands(self):
        """原本直接把 UA 字串當參數丟進去，Chrome 根本不會理它。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        arguments = service.get_default_chrome_options().arguments
        assert f"--user-agent={BROWSER_USER_AGENT}" in arguments

    def test_selenium_and_aiohttp_cannot_drift_apart(self):
        """兩邊共用同一個常數，否則登入與搶場地會用不同身分。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        arguments = service.get_default_chrome_options().arguments
        headers = build_browser_headers(referer="https://example.invalid/list")
        assert any(headers["User-Agent"] in argument for argument in arguments)


class TestCreateSession:
    """開搶前的每一發請求都在關鍵路徑上，卡住不會拋例外，只有 timeout 攔得到。"""

    def test_every_request_is_bounded_by_default(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)

        async def scenario():
            # 只是建立物件，不會連線；連線是 session.get 才發生的事。
            session = service.create_session(
                cookies={}, referer="https://example.invalid/list"
            )
            try:
                assert session.timeout.total == SESSION_TIMEOUT_SECONDS
                assert session.timeout.connect == CONNECT_TIMEOUT_SECONDS
            finally:
                await session.close()

        asyncio.run(scenario())


class FakeBookingResponse:
    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self.body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def text(self):
        return self.body

    async def read(self):
        return self.body.encode("utf-8")


class RecordingSession:
    def __init__(self, body: str = "PT=1&X=1", headers=None):
        self.body = body
        self.headers = headers or {}
        self.requested_urls: list[str] = []
        self.request_epochs: list[float] = []

    def get(self, url, **kwargs):
        import time

        self.requested_urls.append(url)
        self.request_epochs.append(time.time())
        return FakeBookingResponse(self.body, self.headers)


class TestBookingCourts:
    def test_sends_the_url_it_was_handed(self):
        """URL 在截止時刻之前就組好，開搶那一刻不做字串運算。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession()

        async def scenario():
            ready = asyncio.Event()
            ready.set()
            return await service.booking_courts(
                session=session,
                booking_url="https://example.invalid/book",
                hour=20,
                ready_event=ready,
            )

        attempt = asyncio.run(scenario())
        assert session.requested_urls == ["https://example.invalid/book"]
        assert attempt.success is True
        assert attempt.hour == 20

    def test_waits_for_the_event_before_sending(self):
        """task 要在倒數結束前就掛好，時間到只做 event.set()。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession()

        async def scenario():
            ready = asyncio.Event()
            task = asyncio.create_task(
                service.booking_courts(
                    session=session,
                    booking_url="https://example.invalid/book",
                    hour=20,
                    ready_event=ready,
                )
            )
            await asyncio.sleep(0.05)
            assert session.requested_urls == [], "還沒放行就送出了"
            ready.set()
            return await task

        asyncio.run(scenario())
        assert session.requested_urls == ["https://example.invalid/book"]

    def test_reports_a_lost_race_without_raising(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession(body="PT=1&X=2")

        async def scenario():
            ready = asyncio.Event()
            ready.set()
            return await service.booking_courts(
                session=session,
                booking_url="https://example.invalid/book",
                hour=20,
                ready_event=ready,
            )

        attempt = asyncio.run(scenario())
        assert attempt.success is False
        assert attempt.error is None

    def test_an_unrecognised_response_is_recorded_not_raised(self):
        """一發失敗絕不能連坐另一發，所以例外要收在結果物件裡。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession(body="尚未開放預約")

        async def scenario():
            ready = asyncio.Event()
            ready.set()
            return await service.booking_courts(
                session=session,
                booking_url="https://example.invalid/book",
                hour=20,
                ready_event=ready,
            )

        attempt = asyncio.run(scenario())
        assert attempt.success is None
        assert attempt.error is not None

    def test_records_the_send_instant_and_round_trip(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession(headers={"Date": "Thu, 04 Sep 2025 16:00:00 GMT"})

        async def scenario():
            ready = asyncio.Event()
            ready.set()
            return await service.booking_courts(
                session=session,
                booking_url="https://example.invalid/book",
                hour=20,
                ready_event=ready,
            )

        attempt = asyncio.run(scenario())
        assert attempt.sent_epoch > 0
        assert attempt.rtt >= 0
        assert attempt.server_date == "Thu, 04 Sep 2025 16:00:00 GMT"


class TestWarmUp:
    def test_opens_both_pages_concurrently(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession(
            body="<html></html>", headers={"Connection": "keep-alive"}
        )

        results = asyncio.run(
            service.warm_up(session=session)
        )
        assert len(results) == 2
        assert len(session.requested_urls) == 2
        assert all(result.ok for result in results)

    def test_reports_the_keep_alive_verdict(self):
        """若伺服器回 Connection: close，預熱就是白做，使用者必須看得見。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession(
            body="<html></html>",
            headers={"Connection": "close", "Keep-Alive": "timeout=5"},
        )

        results = asyncio.run(
            service.warm_up(session=session)
        )
        assert results[0].connection == "close"
        assert results[0].keep_alive == "timeout=5"

    def test_the_list_page_is_fetched_separately_now(self):
        """預熱抓靜態資源，列表頁由 fetch_list_page 單獨處理 —— 它慢，不能卡在最後十秒。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession(body=LIST_PAGE_HTML)

        body = asyncio.run(
            service.fetch_list_page(session=session, year=2026, month=9, day=17)
        )
        assert body == LIST_PAGE_HTML
        assert "StepFlag=2&" in session.requested_urls[0]

    def test_a_failed_list_page_fetch_returns_none(self):
        class ExplodingSession:
            def get(self, url, **kwargs):
                raise OSError("模擬連線中斷")

        service = build_without_browser(ZhongzhengSportsCenterWebService)
        body = asyncio.run(
            service.fetch_list_page(
                session=ExplodingSession(), year=2026, month=9, day=17
            )
        )
        assert body is None

    def test_a_failed_warm_up_degrades_instead_of_raising(self):
        """預熱失敗只是回到冷連線，不比現況差，絕不能中斷搶場地。"""

        class ExplodingSession:
            def get(self, url, **kwargs):
                raise OSError("模擬連線中斷")

        service = build_without_browser(ZhongzhengSportsCenterWebService)
        results = asyncio.run(
            service.warm_up(session=ExplodingSession())
        )
        assert all(result.ok is False for result in results)


from badminton_bot.services.sports_center_webservice import (
    MAX_ONE_WAY_SECONDS,
    HandshakeTiming,
    build_handshake_tracer,
)


class TestHandshakeTiming:
    """回應時間包含伺服器處理，握手不包含 —— 這是唯一能分離出飛行時間的量法。

    實測那個站在已預熱的連線上仍要 5.3 秒才回應，幾乎全是應用層處理。
    照回應時間去補償會把請求提早 2.5 秒送出，那是硬失敗。
    """

    def test_no_handshake_means_no_compensation(self):
        """連線全部重用、沒有新握手時，寧可不補償也不要亂猜。"""
        assert HandshakeTiming().one_way_estimate == 0.0

    def test_estimates_one_way_as_a_quarter_of_the_handshake(self):
        """TCP 一個往返 + TLS 一個往返 = 兩個往返 = 四段單程。"""
        timing = HandshakeTiming()
        timing.record(0.080)
        assert timing.one_way_estimate == pytest.approx(0.020)

    def test_uses_the_median_of_several_handshakes(self):
        timing = HandshakeTiming()
        for seconds in (0.080, 0.084, 0.600):
            timing.record(seconds)
        assert timing.one_way_estimate == pytest.approx(0.021)

    def test_caps_an_implausible_estimate(self):
        """台灣主機的單程飛行不可能是這個數量級，量到就是摻了別的東西。"""
        timing = HandshakeTiming()
        timing.record(20.0)
        assert timing.one_way_estimate == MAX_ONE_WAY_SECONDS

    def test_ignores_a_non_positive_sample(self):
        timing = HandshakeTiming()
        timing.record(0.0)
        timing.record(-1.0)
        assert timing.one_way_estimate == 0.0


class TestHandshakeTracerWiring:
    """握手是否真的被記錄下來只有連上網才驗得到，但「掛在哪個訊號上」可以離線驗。

    掛錯訊號名稱是這段最可能的失敗方式，而它會安靜地讓飛行時間永遠是 0。
    """

    def test_subscribes_to_the_signals_that_bracket_a_handshake(self):
        timing = HandshakeTiming()
        trace = build_handshake_tracer(timing)
        assert len(trace.on_connection_create_start) == 1
        assert len(trace.on_connection_create_end) == 1
        assert len(trace.on_dns_resolvehost_start) == 1
        assert len(trace.on_dns_resolvehost_end) == 1

    def test_the_callbacks_record_a_handshake_with_dns_subtracted(self):
        import asyncio
        from types import SimpleNamespace

        timing = HandshakeTiming()
        trace = build_handshake_tracer(timing)
        context = SimpleNamespace()

        async def drive():
            await trace.on_connection_create_start[0](None, context, None)
            await trace.on_dns_resolvehost_start[0](None, context, None)
            await trace.on_dns_resolvehost_end[0](None, context, None)
            await trace.on_connection_create_end[0](None, context, None)

        asyncio.run(drive())
        assert len(timing.handshake_samples) == 1
        assert timing.handshake_samples[0] >= 0

    def test_a_connection_end_without_a_start_is_ignored(self):
        import asyncio
        from types import SimpleNamespace

        timing = HandshakeTiming()
        trace = build_handshake_tracer(timing)

        asyncio.run(trace.on_connection_create_end[0](None, SimpleNamespace(), None))
        assert timing.handshake_samples == []


from badminton_bot.services.sports_center_webservice import SLOT_UNKNOWN as _SU


class TestParseAvailableCourts:
    """事後偵察要回答的是「這到底是不是毫秒級的搶奪」。

    不寫死場地清單 —— 我們只確定 7-1=1196、7-2=1197，其餘是推論。
    直接從頁面抓出還掛著 Step3Action 的 qpid，資料自己會說話。
    """

    def test_lists_every_court_still_bookable_at_that_hour(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        html = """
        <td><a onclick="Step3Action(1196, 20)"></a></td>
        <td><a onclick="Step3Action(1199, 20)"></a></td>
        <td><img src="img/place02.png" title="已被預約"></td>
        """
        assert service.parse_available_courts(html, hour=20) == [1196, 1199]

    def test_ignores_other_hours(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        html = '<a onclick="Step3Action(1196, 19)"></a><a onclick="Step3Action(1199, 20)"></a>'
        assert service.parse_available_courts(html, hour=20) == [1199]

    def test_tolerates_zero_padded_hours(self):
        service = build_without_browser(ZhongshanSportsCenterWebService)
        html = '<a onclick="Step3Action(84, 09)"></a>'
        assert service.parse_available_courts(html, hour=9) == [84]

    def test_a_fully_booked_hour_is_an_empty_list(self):
        """空清單代表全被掃光 —— 那才是真正的競速。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        assert service.parse_available_courts("<html>查無資料</html>", hour=20) == []

    def test_deduplicates_and_sorts(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        html = '<a onclick="Step3Action(1199,20)"></a><a onclick="Step3Action(1196,20)"></a><a onclick="Step3Action(1199,20)"></a>'
        assert service.parse_available_courts(html, hour=20) == [1196, 1199]


class TestStaticAssetWarmUp:
    """預熱要的是連線熱著，不是頁面內容。

    實測抓列表頁要 4~5 秒，從 T-10s 開始會跑到 T-4.6s 才結束，差點被預算砍掉。
    靜態資源由 Cloudflare 邊緣直接回，約 50 毫秒。
    """

    def test_warm_up_urls_are_two_distinct_static_assets(self):
        for cls in (ZhongzhengSportsCenterWebService, ZhongshanSportsCenterWebService):
            service = build_without_browser(cls)
            urls = service._generate_warm_up_urls()
            assert len(urls) == 2
            assert len(set(urls)) == 2

    def test_warm_up_never_touches_the_booking_action(self):
        for cls in (ZhongzhengSportsCenterWebService, ZhongshanSportsCenterWebService):
            service = build_without_browser(cls)
            for url in service._generate_warm_up_urls():
                assert "StepFlag=25" not in url

    def test_warm_up_urls_are_not_the_slow_list_page(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        for url in service._generate_warm_up_urls():
            assert "module=net_booking" not in url


class TestWarmUpReadsBinaryAssets:
    """實測抓到的 bug：預熱改抓 PNG 之後仍用 response.text()，解 UTF-8 直接爆炸。

    連線有建立起來，但 ok=False 讓 Connection / Keep-Alive 的診斷跟著消失。
    """

    def test_a_binary_asset_does_not_fail_the_warm_up(self):
        class BinaryResponse:
            headers = {"Connection": "keep-alive"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def read(self):
                return b"\x89PNG\r\n\x1a\n\xde\xad\xbe\xef"

            async def text(self):
                raise UnicodeDecodeError(
                    "utf-8", b"\xde", 0, 1, "invalid continuation byte"
                )

        class BinarySession:
            def __init__(self):
                self.requested_urls = []

            def get(self, url, **kwargs):
                self.requested_urls.append(url)
                return BinaryResponse()

        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = BinarySession()
        results = asyncio.run(service.warm_up(session=session))

        assert all(result.ok for result in results)
        assert all(result.connection == "keep-alive" for result in results)
        assert len(session.requested_urls) == 2
