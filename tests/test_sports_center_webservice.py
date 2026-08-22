"""Tests for the webservice subclasses.

Every test here builds an instance with ``object.__new__`` so that ``__init__``
never runs — that keeps Chrome from launching and, more importantly, keeps the
suite from touching the live booking sites. Only the pure parts (URL building,
response parsing, subclass contract) are exercised.
"""

import pytest

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
        for attr in ("sport_center_name", "login_page_url", "booking_window_days"):
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
