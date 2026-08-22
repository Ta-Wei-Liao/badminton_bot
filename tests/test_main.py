"""Tests for the orchestration helpers in main. No network, no browser."""

from datetime import datetime, timedelta

import pytest

from badminton_bot.main import WEBSERVICE_MAPPING, count_down, webservice_factory
from badminton_bot.services.zhongshan_sports_center_webservice import (
    ZhongshanSportsCenterWebService,
)
from badminton_bot.services.zhongzheng_sports_center_webservice import (
    ZhongzhengSportsCenterWebService,
)


class TestWebserviceFactory:
    def test_resolves_every_registered_court_no(self):
        assert webservice_factory(court_no=0) is ZhongshanSportsCenterWebService
        assert webservice_factory(court_no=1) is ZhongzhengSportsCenterWebService

    def test_rejects_an_unregistered_court_no(self):
        with pytest.raises(ValueError):
            webservice_factory(court_no=99)

    def test_the_menu_mapping_has_no_gaps(self):
        """main builds the numbered menu straight from this dict."""
        assert sorted(WEBSERVICE_MAPPING) == list(range(len(WEBSERVICE_MAPPING)))


class TestCountDown:
    def test_returns_immediately_when_the_target_has_already_passed(self):
        past = datetime.now() - timedelta(seconds=5)
        started = datetime.now()
        count_down(booking_date=past)
        assert (datetime.now() - started) < timedelta(seconds=1)

    def test_a_negative_offset_can_put_an_upcoming_target_in_the_past(self):
        """This is how main logs in three minutes early: offset moves the wait target back."""
        one_minute_away = datetime.now() + timedelta(minutes=1)
        started = datetime.now()
        count_down(booking_date=one_minute_away, offset=timedelta(minutes=-3))
        assert (datetime.now() - started) < timedelta(seconds=1)

    def test_waits_until_a_target_in_the_near_future(self):
        target = datetime.now() + timedelta(milliseconds=200)
        count_down(booking_date=target)
        assert datetime.now() >= target
