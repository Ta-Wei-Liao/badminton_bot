"""Tests for the orchestration helpers in main. No network, no browser."""

import asyncio
import inspect
import logging
import time
from datetime import datetime, timedelta

import pytest

from badminton_bot.main import (
    WEBSERVICE_MAPPING,
    count_down,
    run_booking_race,
    run_with_deadline,
    webservice_factory,
)
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


class TestRunWithDeadline:
    """卡住的請求不會拋任何例外，try/except 攔不到，只有時間預算攔得到。"""

    def test_returns_the_real_result_when_the_work_finishes_in_time(self):
        async def work():
            return "完成"

        assert (
            asyncio.run(
                run_with_deadline(
                    work(), budget_seconds=1.0, label="測試", fallback="退回值"
                )
            )
            == "完成"
        )

    def test_falls_back_and_warns_when_the_work_hangs(self, caplog):
        async def hangs_forever():
            # 永遠不會被 set 的 event，模擬一個連上之後就不回應的請求。
            await asyncio.Event().wait()

        with caplog.at_level(logging.WARNING):
            result = asyncio.run(
                run_with_deadline(
                    hangs_forever(),
                    budget_seconds=0.05,
                    label="會卡住的階段",
                    fallback="退回值",
                )
            )

        assert result == "退回值"
        assert "會卡住的階段" in caplog.text

    def test_falls_back_when_the_work_raises(self, caplog):
        async def explodes():
            raise OSError("模擬連線中斷")

        with caplog.at_level(logging.WARNING):
            result = asyncio.run(
                run_with_deadline(
                    explodes(), budget_seconds=1.0, label="會炸掉的階段", fallback=[]
                )
            )

        assert result == []
        assert "會炸掉的階段" in caplog.text

    def test_a_non_positive_budget_skips_the_work_entirely(self, caplog):
        started = False

        async def work():
            nonlocal started
            started = True
            return "完成"

        coroutine = work()
        with caplog.at_level(logging.WARNING):
            result = asyncio.run(
                run_with_deadline(
                    coroutine, budget_seconds=0.0, label="來不及的階段", fallback=None
                )
            )

        assert result is None
        assert started is False
        # 沒 await 就得自己關掉，否則會留下 coroutine was never awaited 警告。
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
        assert "來不及的階段" in caplog.text
