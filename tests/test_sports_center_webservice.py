"""Tests for the webservice subclasses.

Every test here builds an instance with ``object.__new__`` so that ``__init__``
never runs — that keeps Chrome from launching and, more importantly, keeps the
suite from touching the live booking sites. Only the pure parts (URL building,
response parsing, subclass contract) are exercised.
"""

from unittest.mock import Mock

import pytest
from selenium.common.exceptions import NoAlertPresentException, NoSuchElementException

from badminton_bot.services.sports_center_webservice import SportsCenterWebService
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
        urls = service._generate_warm_up_urls(year=2026, month=9, day=17)
        assert len(urls) == 2
        assert len(set(urls)) == 2

    def test_warm_up_never_touches_the_booking_action(self):
        for cls in (ZhongzhengSportsCenterWebService, ZhongshanSportsCenterWebService):
            service = build_without_browser(cls)
            for url in service._generate_warm_up_urls(year=2026, month=9, day=17):
                assert "StepFlag=25" not in url

    def test_includes_the_list_page_and_the_login_page(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        urls = service._generate_warm_up_urls(year=2026, month=9, day=17)
        assert service._generate_list_page_url(2026, 9, 17) in urls
        assert ZhongzhengSportsCenterWebService.login_page_url in urls
