"""Tests for the orchestration helpers in main. No network, no browser."""

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from badminton_bot.main import WEBSERVICE_MAPPING, count_down, webservice_factory, run_booking_race
from badminton_bot.services.sports_center_webservice import BookingAttempt
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


class OneGoodOneBadSession:
    """Succeeds on the first URL and blows up on the second."""

    def __init__(self):
        self.requested_urls = []

    def get(self, url, **kwargs):
        self.requested_urls.append(url)
        if "QTime=21" in url:
            raise OSError("模擬第二發連線中斷")
        return FakeBookingResponse("PT=1&X=1")


class FakeBookingResponse:
    def __init__(self, body):
        self.body = body
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def text(self):
        return self.body


class TestRunBookingRace:
    def test_one_failing_request_does_not_take_down_the_other(self):
        """既有的 gather 會把例外往外傳，20 點那發炸掉會連 21 點一起收掉。"""
        from badminton_bot.services.zhongzheng_sports_center_webservice import (
            ZhongzhengSportsCenterWebService,
        )

        service = object.__new__(ZhongzhengSportsCenterWebService)
        session = OneGoodOneBadSession()
        periods = (
            datetime(2026, 9, 17, 20),
            datetime(2026, 9, 17, 21),
        )

        attempts = asyncio.run(
            run_booking_race(
                service=service,
                session=session,
                booking_periods=periods,
                send_at_epoch=time.time(),
            )
        )

        assert len(attempts) == 2
        assert attempts[0].success is True
        assert attempts[1].error is not None
        assert len(session.requested_urls) == 2

    def test_both_requests_go_out_after_the_send_instant(self):
        from badminton_bot.services.zhongzheng_sports_center_webservice import (
            ZhongzhengSportsCenterWebService,
        )

        service = object.__new__(ZhongzhengSportsCenterWebService)
        session = OneGoodOneBadSession()
        send_at = time.time() + 0.15
        periods = (datetime(2026, 9, 17, 20),)

        attempts = asyncio.run(
            run_booking_race(
                service=service,
                session=session,
                booking_periods=periods,
                send_at_epoch=send_at,
            )
        )

        assert attempts[0].sent_epoch >= send_at

    def test_returns_one_attempt_per_period(self):
        from badminton_bot.services.zhongzheng_sports_center_webservice import (
            ZhongzhengSportsCenterWebService,
        )

        service = object.__new__(ZhongzhengSportsCenterWebService)
        session = OneGoodOneBadSession()
        periods = (
            datetime(2026, 9, 17, 20),
            datetime(2026, 9, 17, 21),
        )

        attempts = asyncio.run(
            run_booking_race(
                service=service,
                session=session,
                booking_periods=periods,
                send_at_epoch=time.time(),
            )
        )
        assert [attempt.hour for attempt in attempts] == [20, 21]
        assert all(isinstance(attempt, BookingAttempt) for attempt in attempts)
