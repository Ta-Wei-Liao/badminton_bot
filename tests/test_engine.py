"""Tests for the grab engine. No network, no browser."""

import asyncio
import gc
import inspect
import logging
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from badminton_bot.booking_plan import TAIPEI, BookingTarget, GrabMode
from badminton_bot.engine import (
    CENTER_OPTIONS,
    LoginFailed,
    NTP_REFRESH_OFFSET,
    POST_CHECK_DELAY_SECONDS,
    POST_CHECK_TIMEOUT_SECONDS,
    PRE_CHECK_OFFSET,
    PRE_CHECK_TIMEOUT_SECONDS,
    GrabConfig,
    GrabRunner,
    count_down,
    report_slot_states,
    run_booking_race,
    run_with_deadline,
    set_logger,
)
from badminton_bot.services.sports_center_webservice import BookingAttempt, Court
from badminton_bot.services.zhongshan_sports_center_webservice import (
    ZhongshanSportsCenterWebService,
)
from badminton_bot.services.zhongzheng_sports_center_webservice import (
    ZhongzhengSportsCenterWebService,
)
from badminton_bot.utils.server_clock import ClockMeasurement
from badminton_bot.utils.timing import Cancelled
from tests.conftest import FakeResponse, FakeService, make_targets

COURT_1 = Court("羽球7-1", 1196)


def target(hour: int, court: Court = COURT_1) -> BookingTarget:
    return BookingTarget(court, datetime(2026, 9, 17, hour, tzinfo=TAIPEI))


class TestCenterOptions:
    def test_center_options_disable_zhongshan(self):
        assert [o.enabled for o in CENTER_OPTIONS] == [True, False]
        assert CENTER_OPTIONS[1].label == "中山運動中心（暫時停止）"

    def test_an_enabled_center_is_labelled_by_its_plain_name(self):
        assert CENTER_OPTIONS[0].service_cls is ZhongzhengSportsCenterWebService
        assert CENTER_OPTIONS[0].label == "中正運動中心"


class TestCountDown:
    def test_returns_immediately_when_the_target_has_already_passed(self):
        past = datetime.now(TAIPEI) - timedelta(seconds=5)
        started = time.time()
        count_down(booking_date=past)
        assert time.time() - started < 1

    def test_a_negative_offset_can_put_an_upcoming_target_in_the_past(self):
        """This is how the engine logs in three minutes early: offset moves the wait target back."""
        one_minute_away = datetime.now(TAIPEI) + timedelta(minutes=1)
        started = time.time()
        count_down(booking_date=one_minute_away, offset=timedelta(minutes=-3))
        assert time.time() - started < 1

    def test_waits_until_a_target_in_the_near_future(self):
        target_time = datetime.now(TAIPEI) + timedelta(milliseconds=200)
        count_down(booking_date=target_time)
        assert datetime.now(TAIPEI) >= target_time

    def test_a_cancel_event_interrupts_a_long_wait(self):
        ev = threading.Event()
        threading.Timer(0.1, ev.set).start()
        started = time.time()
        with pytest.raises(Cancelled):
            count_down(
                booking_date=datetime.now(TAIPEI) + timedelta(minutes=10),
                cancel_event=ev,
            )
        assert time.time() - started < 2


class OneGoodOneBadSession:
    """Succeeds on the first URL and blows up on the second."""

    def __init__(self):
        self.requested_urls = []

    def get(self, url, **kwargs):
        self.requested_urls.append(url)
        if "QTime=21" in url:
            raise OSError("模擬第二發連線中斷")
        return FakeResponse("PT=1&X=1")


class TestRunBookingRace:
    @staticmethod
    def _service():
        return object.__new__(ZhongzhengSportsCenterWebService)

    def test_one_failing_request_does_not_take_down_the_other(self):
        """gather 會把例外往外傳，20 點那發炸掉會連 21 點一起收掉。"""
        session = OneGoodOneBadSession()

        attempts = asyncio.run(
            run_booking_race(
                service=self._service(),
                session=session,
                targets=(target(20), target(21)),
                send_at_epoch=time.time(),
            )
        )

        assert len(attempts) == 2
        assert attempts[0].success is True
        assert attempts[1].error is not None
        assert (attempts[1].court_name, attempts[1].qpid) == ("羽球7-1", 1196)
        assert len(session.requested_urls) == 2

    def test_every_request_goes_out_after_the_send_instant(self):
        send_at = time.time() + 0.15

        attempts = asyncio.run(
            run_booking_race(
                service=self._service(),
                session=OneGoodOneBadSession(),
                targets=(target(20),),
                send_at_epoch=send_at,
            )
        )

        assert attempts[0].sent_epoch >= send_at

    def test_returns_one_attempt_per_target_in_order(self):
        court_4 = Court("羽球7-4", 1199)
        targets = (target(20), target(21), target(20, court_4))

        attempts = asyncio.run(
            run_booking_race(
                service=self._service(),
                session=OneGoodOneBadSession(),
                targets=targets,
                send_at_epoch=time.time(),
            )
        )

        assert [(a.qpid, a.hour) for a in attempts] == [
            (1196, 20),
            (1196, 21),
            (1199, 20),
        ]
        assert all(isinstance(a, BookingAttempt) for a in attempts)

    def test_on_fired_runs_before_any_request_leaves(self):
        session = OneGoodOneBadSession()
        seen_at_fire: list[int] = []

        asyncio.run(
            run_booking_race(
                service=self._service(),
                session=session,
                targets=(target(20), target(21)),
                send_at_epoch=time.time() + 0.05,
                on_fired=lambda: seen_at_fire.append(len(session.requested_urls)),
            )
        )

        assert seen_at_fire == [0]
        assert len(session.requested_urls) == 2


class TestRunBookingRaceOnReleased:
    def test_on_released_runs_only_after_every_request_left(self):
        session = OneGoodOneBadSession()
        seen_at_release: list[int] = []

        asyncio.run(
            run_booking_race(
                service=object.__new__(ZhongzhengSportsCenterWebService),
                session=session,
                targets=(target(20), target(21)),
                send_at_epoch=time.time() + 0.05,
                on_released=lambda: seen_at_release.append(len(session.requested_urls)),
            )
        )

        assert seen_at_release == [2]

    def test_a_raising_on_released_does_not_cancel_the_requests(self):
        def explode():
            raise RuntimeError("模擬回呼出錯")

        attempts = asyncio.run(
            run_booking_race(
                service=object.__new__(ZhongzhengSportsCenterWebService),
                session=OneGoodOneBadSession(),
                targets=(target(20),),
                send_at_epoch=time.time(),
                on_released=explode,
            )
        )

        assert attempts[0].success is True


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


class TestSetLogger:
    """每次執行都留下 log，密碼只存在 GrabConfig 裡、從不經過 logging。"""

    def test_creates_a_timestamped_log_file(self, tmp_path):
        log_path = set_logger(log_dir=tmp_path)
        assert log_path.exists()
        assert log_path.parent == tmp_path
        assert log_path.suffix == ".log"

    def test_log_records_reach_the_file(self, tmp_path):
        log_path = set_logger(log_dir=tmp_path)
        logging.info("測試訊息")
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert "測試訊息" in log_path.read_text(encoding="utf-8")

    def test_creates_the_directory_when_missing(self, tmp_path):
        log_path = set_logger(log_dir=tmp_path / "does" / "not" / "exist")
        assert log_path.exists()

    def test_repeated_calls_do_not_duplicate_handlers(self, tmp_path):
        set_logger(log_dir=tmp_path)
        first = len(logging.getLogger().handlers)
        set_logger(log_dir=tmp_path)
        assert len(logging.getLogger().handlers) == first

    def test_set_logger_skips_console_when_stderr_is_missing(self, tmp_path, monkeypatch):
        """打包成視窗程式後 sys.stderr 是 None，StreamHandler 一寫就炸。"""
        monkeypatch.setattr(sys, "stderr", None)
        set_logger(tmp_path)
        logging.info("x")
        assert not any(
            type(h) is logging.StreamHandler for h in logging.getLogger().handlers
        )

    def test_extra_handlers_receive_records(self, tmp_path):
        records: list[logging.LogRecord] = []

        class ListHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        set_logger(tmp_path, extra_handlers=(ListHandler(),))
        logging.info("給視窗看的訊息")
        assert any(r.getMessage() == "給視窗看的訊息" for r in records)

    @pytest.mark.parametrize("name", ["selenium", "urllib3"])
    def test_library_debug_output_never_reaches_the_file(self, tmp_path, name):
        """Selenium 的 DEBUG 會印出送給瀏覽器的指令，包含填進去的密碼。"""
        log_path = set_logger(tmp_path)
        # 就算之後有人把根 logger 調成 DEBUG 也一樣
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger(f"{name}.child").debug("password=secret")
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert "secret" not in log_path.read_text(encoding="utf-8")


class TestReportSlotStatesDistinguishesNotYetOpen:
    """週四 T-40s 時，要搶的那一天還沒進入預約窗口，頁面不會渲染它。

    「還空著 0 片」讀起來像全被掃光，但實際上是「這一天根本還看不到」。
    這兩件事必須在 log 裡分得開，否則開搶前那行會誤導判讀。
    """

    @staticmethod
    def _service():
        return object.__new__(ZhongzhengSportsCenterWebService)

    def test_says_the_window_is_not_open_yet(self, caplog):
        with caplog.at_level("INFO"):
            report_slot_states(
                service=self._service(),
                html="<html>查無資料</html>",
                targets=(target(20),),
                label="開放前",
            )

        assert "尚未進入預約窗口" in caplog.text
        assert "還空著 0 片" not in caplog.text

    def test_a_genuinely_swept_hour_is_not_mistaken_for_a_closed_window(self, caplog):
        """頁面有渲染我們的場地（別的時段可訂），所以 0 片是真的被掃光。"""
        html = '<a onclick="Step3Action(1196, 19)"></a>'
        with caplog.at_level("INFO"):
            report_slot_states(
                service=self._service(),
                html=html,
                targets=(target(20),),
                label="開放後",
            )

        assert "尚未進入預約窗口" not in caplog.text
        assert "還空著 0 片" in caplog.text

    def test_an_ordinary_census_is_unchanged(self, caplog):
        html = '<a onclick="Step3Action(1196, 20)"></a><a onclick="Step3Action(1199, 20)"></a>'
        with caplog.at_level("INFO"):
            report_slot_states(
                service=self._service(),
                html=html,
                targets=(target(20),),
                label="開放後",
            )

        assert "還空著 2 片" in caplog.text
        assert "1196" in caplog.text

    def test_each_target_reads_its_own_court(self, caplog):
        """只有 7-4 可訂：狀態必須照各自的 qpid 讀，不能全看第一片。"""
        html = '<a onclick="Step3Action(1199, 20)"></a>'
        with caplog.at_level("INFO"):
            report_slot_states(
                service=self._service(),
                html=html,
                targets=(target(20), target(20, Court("羽球7-4", 1199))),
                label="開放後",
            )

        lines = caplog.text.splitlines()
        court_1_line = next(line for line in lines if "羽球7-1" in line)
        court_4_line = next(line for line in lines if "羽球7-4" in line)
        assert "可訂" not in court_1_line
        assert "可訂" in court_4_line


class TestListPageTimeouts:
    def test_the_pre_check_finishes_before_the_ntp_refresh(self):
        """開放前偵察最慢也要在重新校時之前結束，否則會把後面的排程往後推。"""
        assert (
            PRE_CHECK_OFFSET + timedelta(seconds=PRE_CHECK_TIMEOUT_SECONDS)
            < NTP_REFRESH_OFFSET
        )

    def test_both_checks_outlast_the_session_timeout(self):
        """列表頁實測超過 5 秒；沿用 session 上限的話偵察注定逾時。"""
        from badminton_bot.services.sports_center_webservice import (
            SESSION_TIMEOUT_SECONDS,
        )

        assert PRE_CHECK_TIMEOUT_SECONDS > SESSION_TIMEOUT_SECONDS
        assert POST_CHECK_TIMEOUT_SECONDS > SESSION_TIMEOUT_SECONDS

    def test_the_post_check_waits_out_the_rush(self):
        """開搶當下實測回應要 5~27 秒；T+2s 去抓列表頁四週全數逾時。"""
        assert POST_CHECK_DELAY_SECONDS >= 30.0


def make_config(service_cls=FakeService, open_in=timedelta(seconds=2), targets=None):
    return GrabConfig(
        service_cls=service_cls,
        username="A123456789",
        password="s3cret",
        open_at=datetime.now(TAIPEI) + open_in,
        targets=make_targets() if targets is None else targets,
    )


class TestGrabRunner:
    def test_runner_fires_every_target_after_open_at(self, offline_engine):
        config = make_config()
        stages: list[str] = []

        attempts = asyncio.run(GrabRunner(config, on_stage=stages.append).run())

        (service,) = FakeService.instances
        assert len(service.session.booking_urls) == 4
        assert all(a.sent_epoch >= config.open_at.timestamp() for a in attempts)
        assert {(a.qpid, a.hour) for a in attempts} == {
            (1196, 20),
            (1196, 21),
            (1199, 20),
            (1199, 21),
        }
        assert all(a.success is True for a in attempts)
        assert offline_engine == [True]
        assert service.logged_out
        assert stages[0] == "瀏覽器自我檢查"
        assert stages[-1] == "完成"
        assert "已送出，等待回應" in stages

    def test_warm_up_opens_one_connection_per_request(self, offline_engine, monkeypatch):
        counts: list[int] = []

        async def record_warm_up(self, session, count):
            counts.append(count)
            return []

        monkeypatch.setattr(FakeService, "warm_up", record_warm_up)
        asyncio.run(GrabRunner(make_config()).run())
        assert counts == [4]

    def test_runner_rejects_a_disabled_center(self, offline_engine):
        runner = GrabRunner(make_config(service_cls=ZhongshanSportsCenterWebService))
        with pytest.raises(ValueError):
            asyncio.run(runner.run())
        assert FakeService.instances == []
        assert offline_engine == []

    def test_runner_rejects_thirteen_targets(self, offline_engine):
        courts = ZhongzhengSportsCenterWebService.courts
        targets = tuple(
            BookingTarget(court, datetime(2026, 9, 17, hour, tzinfo=TAIPEI))
            for court in courts
            for hour in (6, 7, 8)
        )[:13]
        assert len(targets) == 13

        with pytest.raises(ValueError):
            asyncio.run(GrabRunner(make_config(targets=targets)).run())
        assert FakeService.instances == []

    def test_cancel_before_login_never_opens_the_browser(self, offline_engine):
        runner = GrabRunner(make_config(open_in=timedelta(minutes=10)))
        results: list[bool] = []
        threading.Timer(0.2, lambda: results.append(runner.cancel())).start()

        started = time.time()
        with pytest.raises(Cancelled):
            asyncio.run(runner.run())

        assert time.time() - started < 3
        assert results == [True]
        assert FakeService.instances == []
        assert runner.fired is False

    def test_cancel_before_run_starts_raises_at_once(self, offline_engine):
        runner = GrabRunner(make_config(open_in=timedelta(minutes=10)))
        assert runner.cancel() is True

        with pytest.raises(Cancelled):
            asyncio.run(runner.run())
        assert FakeService.instances == []

    def test_cancel_after_login_still_logs_out(self, offline_engine, monkeypatch):
        """登入後才取消，一樣要走 with service 的登出流程。"""
        runner_box: list[GrabRunner] = []

        async def cancel_during_probe(*args, **kwargs):
            runner_box[0].cancel()
            await asyncio.sleep(5)

        from badminton_bot import engine

        monkeypatch.setattr(engine, "measure_server_clock", cancel_during_probe)
        runner = GrabRunner(make_config(open_in=timedelta(minutes=1)))
        runner_box.append(runner)

        with pytest.raises(Cancelled):
            asyncio.run(runner.run())

        (service,) = FakeService.instances
        assert service.logged_out
        assert service.session.booking_urls == []
        assert runner.fired is False

    def test_cancel_after_fire_is_refused(self, offline_engine):
        refusals: list[bool] = []
        runner_box: list[GrabRunner] = []

        def on_stage(stage: str) -> None:
            if stage == "已送出，等待回應":
                refusals.append(runner_box[0].cancel())

        runner = GrabRunner(make_config(), on_stage=on_stage)
        runner_box.append(runner)

        attempts = asyncio.run(runner.run())

        assert refusals == [False]
        assert runner.fired is True
        assert len(attempts) == 4
        assert all(a.success is True for a in attempts)

    def test_cancel_after_login_failed_does_not_touch_the_closed_loop(
        self, offline_engine, monkeypatch
    ):
        """登入失敗的同時按取消（或連按兩次），loop 已經關了，不能炸 RuntimeError。"""
        monkeypatch.setattr(FakeService, "login", lambda self: None)
        runner = GrabRunner(make_config(open_in=timedelta(minutes=1)))
        with pytest.raises(LoginFailed):
            asyncio.run(runner.run())

        assert runner.cancel() is True
        assert runner.cancel() is True

    def test_cancel_after_cancelled_does_not_touch_the_closed_loop(self, offline_engine):
        runner = GrabRunner(make_config(open_in=timedelta(minutes=10)))
        threading.Timer(0.2, runner.cancel).start()
        with pytest.raises(Cancelled):
            asyncio.run(runner.run())

        assert runner.cancel() is True

    def test_a_failing_stage_callback_never_blocks_the_release(self, offline_engine):
        """GUI 回呼在「已送出」時炸掉，請求還是要全部送出去。"""

        def on_stage(stage: str) -> None:
            if stage == "已送出，等待回應":
                raise RuntimeError("模擬 GUI 回呼出錯")

        runner = GrabRunner(make_config(), on_stage=on_stage)
        attempts = asyncio.run(runner.run())

        (service,) = FakeService.instances
        assert len(service.session.booking_urls) == 4
        assert all(a.success is True for a in attempts)
        assert runner.fired is True

    def test_every_stage_callback_failure_is_survived(self, offline_engine):
        def on_stage(stage: str) -> None:
            raise RuntimeError("每個階段都炸")

        attempts = asyncio.run(GrabRunner(make_config(), on_stage=on_stage).run())
        assert len(attempts) == 4

    def test_password_never_appears_in_config_repr(self):
        assert "s3cret" not in repr(make_config())

    def test_login_failure_raises_instead_of_returning_nothing(self, offline_engine, monkeypatch):
        """空的結果清單在視窗上看起來像「沒搶到」，登入失敗必須講清楚。"""
        monkeypatch.setattr(FakeService, "login", lambda self: None)

        with pytest.raises(LoginFailed, match="登入失敗，請確認身分證字號與密碼"):
            asyncio.run(GrabRunner(make_config(open_in=timedelta(minutes=1))).run())

        (service,) = FakeService.instances
        assert service.logged_out
        assert service.session.booking_urls == []


class TestCancelDuringCountdownUnwindsCleanly:
    """在同步倒數裡被取消時，殘留的 task.cancel 不能把 session 關到一半。"""

    @staticmethod
    def _skip_probe(monkeypatch):
        from badminton_bot import engine

        async def no_probe(*args, **kwargs):
            return ClockMeasurement(theta=None, uncertainty=0.0)

        monkeypatch.setattr(engine, "measure_server_clock", no_probe)

    def _cancel_during_pre_check(self) -> GrabRunner:
        # open_at 在一分鐘後：馬上登入，接著卡在 T-40s 的同步倒數裡
        runner = GrabRunner(make_config(open_in=timedelta(minutes=1)))
        threading.Timer(0.3, runner.cancel).start()
        with pytest.raises(Cancelled):
            asyncio.run(runner.run())
        return runner

    def test_the_session_finishes_closing(self, offline_engine, monkeypatch):
        self._skip_probe(monkeypatch)
        self._cancel_during_pre_check()

        (service,) = FakeService.instances
        assert service.session.closed
        assert service.logged_out

    def test_a_real_aiohttp_session_is_closed_without_warnings(
        self, offline_engine, monkeypatch, caplog
    ):
        import aiohttp

        self._skip_probe(monkeypatch)
        sessions: list[aiohttp.ClientSession] = []

        def real_session(self, cookies, referer):
            self.handshake_timing = None
            session = aiohttp.ClientSession()
            sessions.append(session)
            return session

        monkeypatch.setattr(FakeService, "create_session", real_session)

        with caplog.at_level(logging.WARNING):
            import warnings

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self._cancel_during_pre_check()
                gc.collect()

        (session,) = sessions
        assert session.closed
        assert not [w for w in caught if issubclass(w.category, ResourceWarning)]
        assert "Unclosed" not in caplog.text


class TestCancelInsideTheSpinWindow:
    """倒數最後幾秒在 busy-wait，cancel_event 只在粗睡時看；取消被接受就不能再往下走。"""

    def test_cancel_in_the_last_seconds_before_login_never_opens_the_browser(
        self, offline_engine
    ):
        from badminton_bot.engine import LOGIN_OFFSET

        # 登入時刻在 2 秒後：0.5 秒時按取消，此時倒數已進入最後 5 秒的 busy-wait
        runner = GrabRunner(make_config(open_in=-LOGIN_OFFSET + timedelta(seconds=2)))
        results: list[bool] = []
        threading.Timer(0.5, lambda: results.append(runner.cancel())).start()

        with pytest.raises(Cancelled):
            asyncio.run(runner.run())

        assert results == [True]
        assert FakeService.instances == []
        assert runner.fired is False


class TestLogoutFailureNeverHidesTheOutcome:
    """搶完之後登出失敗，只能記一筆警告，不能把結果蓋掉。"""

    @staticmethod
    def _failing_logout(monkeypatch):
        def broken_logout(self):
            raise RuntimeError("模擬登出按鈕不見了")

        monkeypatch.setattr(FakeService, "logout", broken_logout)

    def test_attempts_are_still_returned(self, offline_engine, monkeypatch, caplog):
        self._failing_logout(monkeypatch)

        with caplog.at_level(logging.WARNING):
            attempts = asyncio.run(GrabRunner(make_config()).run())

        assert len(attempts) == 4
        assert all(a.success is True for a in attempts)
        assert "模擬登出按鈕不見了" in caplog.text

    def test_a_cancel_is_still_reported_as_cancelled(self, offline_engine, monkeypatch):
        self._failing_logout(monkeypatch)
        runner_box: list[GrabRunner] = []

        async def cancel_during_probe(*args, **kwargs):
            runner_box[0].cancel()
            await asyncio.sleep(5)

        from badminton_bot import engine

        monkeypatch.setattr(engine, "measure_server_clock", cancel_during_probe)
        runner = GrabRunner(make_config(open_in=timedelta(minutes=1)))
        runner_box.append(runner)

        with pytest.raises(Cancelled):
            asyncio.run(runner.run())


class TestLogDirFallback:
    """「文件」資料夾寫不進去（macOS 權限、Windows 受控資料夾存取）時，程式仍要能啟動。"""

    @staticmethod
    def _read_only_dir(tmp_path):
        import os

        locked = tmp_path / "Documents"
        locked.mkdir()
        locked.chmod(0o500)
        if os.access(locked, os.W_OK):
            pytest.skip("以 root 執行時唯讀資料夾仍可寫入")
        return locked

    def test_falls_back_to_the_next_writable_dir(self, tmp_path):
        locked = self._read_only_dir(tmp_path)
        fallback = tmp_path / "fallback"
        try:
            log_path = set_logger(
                locked / "badminton_bot_logs", fallback_dirs=(fallback,)
            )
        finally:
            locked.chmod(0o700)
        for handler in logging.getLogger().handlers:
            handler.flush()

        assert log_path.parent == fallback
        # 換了位置要講出來，使用者才知道 log 去哪裡找
        text = log_path.read_text(encoding="utf-8")
        assert str(locked / "badminton_bot_logs") in text
        assert f"log 改存在 {fallback}" in text

    def test_skips_every_unwritable_fallback(self, tmp_path):
        locked = self._read_only_dir(tmp_path)
        last = tmp_path / "last"
        try:
            log_path = set_logger(
                locked / "a", fallback_dirs=(locked / "b", last)
            )
        finally:
            locked.chmod(0o700)

        assert log_path.parent == last

    def test_raises_when_nothing_is_writable(self, tmp_path):
        locked = self._read_only_dir(tmp_path)
        try:
            with pytest.raises(OSError):
                set_logger(locked / "a", fallback_dirs=(locked / "b",))
        finally:
            locked.chmod(0o700)

    def test_the_default_fallbacks_end_in_the_temp_dir(self):
        import tempfile

        from badminton_bot.engine import LOG_FALLBACK_DIRS

        assert LOG_FALLBACK_DIRS[-1].parent == Path(tempfile.gettempdir())


def make_immediate_config(targets=None):
    day = (datetime.now(TAIPEI) + timedelta(days=2)).date()
    return GrabConfig(
        service_cls=FakeService,
        username="A123456789",
        password="s3cret",
        open_at=None,
        targets=make_targets(day=day) if targets is None else targets,
        mode=GrabMode.IMMEDIATE,
    )


class TestImmediateMode:
    def test_default_mode_is_midnight(self):
        assert make_config().mode is GrabMode.MIDNIGHT

    def test_fires_at_once_and_logs_out(self, offline_engine):
        stages: list[str] = []
        started = time.time()
        attempts = asyncio.run(
            GrabRunner(make_immediate_config(), on_stage=stages.append).run()
        )
        assert time.time() - started < 2
        (service,) = FakeService.instances
        assert len(service.session.booking_urls) == 4
        assert all(a.success is True for a in attempts)
        assert service.logged_out
        assert stages == ["瀏覽器自我檢查", "登入中", "送出中", "已送出，等待回應", "完成"]

    def test_skips_every_timing_and_reconnaissance_step(self, offline_engine, monkeypatch):
        from badminton_bot import engine

        def forbidden(*args, **kwargs):
            raise AssertionError("立即預約不該呼叫這一步")

        monkeypatch.setattr(engine, "query_clock_offset", forbidden)
        monkeypatch.setattr(engine, "measure_server_clock", forbidden)
        monkeypatch.setattr(engine, "count_down", forbidden)
        monkeypatch.setattr(FakeService, "warm_up", forbidden)
        monkeypatch.setattr(FakeService, "fetch_list_page", forbidden)
        asyncio.run(GrabRunner(make_immediate_config()).run())
        (service,) = FakeService.instances
        assert len(service.session.booking_urls) == 4

    def test_rejects_a_started_hour(self, offline_engine):
        today = datetime.now(TAIPEI).date()
        targets = make_targets(hours=(6,), day=today - timedelta(days=1))
        with pytest.raises(ValueError):
            asyncio.run(GrabRunner(make_immediate_config(targets=targets)).run())
        assert FakeService.instances == []

    def test_cancel_during_login_sends_nothing(self, offline_engine, monkeypatch):
        runner_box = []

        def slow_login(self):
            runner_box[0].cancel()
            self.logged_in = True

        monkeypatch.setattr(FakeService, "login", slow_login)
        runner = GrabRunner(make_immediate_config())
        runner_box.append(runner)
        with pytest.raises(Cancelled):
            asyncio.run(runner.run())
        (service,) = FakeService.instances
        assert service.session.booking_urls == []
        assert service.logged_out
        assert runner.fired is False


class TestImmediateRecheckBeforeSending:
    def test_a_slot_that_starts_during_login_is_not_sent(self, offline_engine, monkeypatch):
        """登入可能要十幾秒，期間跨過整點的時段不能送出，但照樣要登出。"""
        from badminton_bot import engine

        real_validate = engine.validate_immediate
        calls: list[int] = []

        def validate_then_expire(targets, now, window_days):
            calls.append(1)
            if len(calls) > 1:
                raise ValueError("19:00 的時段已經過了")
            real_validate(targets, now, window_days)

        monkeypatch.setattr(engine, "validate_immediate", validate_then_expire)
        with pytest.raises(ValueError, match="已經過了"):
            asyncio.run(GrabRunner(make_immediate_config()).run())
        (service,) = FakeService.instances
        assert service.session.booking_urls == []
        assert service.logged_out
        assert len(calls) == 2
