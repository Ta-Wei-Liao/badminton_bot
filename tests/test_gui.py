"""Offline tests for the parts of the GUI that need no screen."""

import asyncio
import logging
import queue
import threading
from datetime import date, datetime, timedelta

from badminton_bot.booking_plan import TAIPEI
from badminton_bot.engine import GrabConfig, GrabRunner, set_logger
from badminton_bot.gui import (
    day_choices,
    QUIET_SECONDS,
    QueueLogHandler,
    format_remaining,
    in_quiet_window,
    is_dev_mode,
    results_grid,
    run_grab,
    stage_reporter,
    status_text,
)
from badminton_bot.utils.timing import MAX_AUTO_CORRECTION_SECONDS
from badminton_bot.services.sports_center_webservice import BookingAttempt
from tests.conftest import FakeService, make_targets


def make_config() -> GrabConfig:
    return GrabConfig(
        service_cls=FakeService,
        username="A123456789",
        password="s3cret",
        open_at=datetime.now(TAIPEI) + timedelta(seconds=2),
        targets=make_targets(),
    )


def drain(events: queue.Queue) -> list:
    items = []
    while not events.empty():
        items.append(events.get_nowait())
    return items


def make_attempt(court_name: str, hour: int, success: bool | None) -> BookingAttempt:
    return BookingAttempt(
        hour=hour,
        url="",
        sent_epoch=0.0,
        rtt=0.0,
        server_date=None,
        success=success,
        error=None,
        court_name=court_name,
        qpid=0,
    )


def test_dev_mode_needs_the_exact_flag():
    assert is_dev_mode({"BADMINTON_BOT_DEV": "1"})
    assert not is_dev_mode({})
    assert not is_dev_mode({"BADMINTON_BOT_DEV": "0"})


def test_results_grid_maps_each_outcome():
    attempts = [
        make_attempt("羽球7-1", 20, True),
        make_attempt("羽球7-1", 21, False),
        make_attempt("羽球7-4", 20, None),
    ]

    assert results_grid(attempts) == {
        ("羽球7-1", 20): "✅",
        ("羽球7-1", 21): "❌",
        ("羽球7-4", 20): "⚠️",
    }


def test_remaining_time_gets_coarser_the_further_away_it_is():
    assert format_remaining(2 * 3600 + 13 * 60 + 40) == "2 小時 13 分"
    assert format_remaining(3 * 60 + 5) == "3 分 5 秒"
    assert format_remaining(5.9) == "5 秒"
    assert format_remaining(-1) == "0 秒"


def test_queue_handler_never_receives_the_password(tmp_path, offline_engine):
    messages: queue.Queue[str] = queue.Queue()
    log_path = set_logger(tmp_path, extra_handlers=[QueueLogHandler(messages)])
    try:
        attempts = asyncio.run(GrabRunner(make_config()).run())
    finally:
        for handler in logging.getLogger().handlers:
            handler.flush()

    assert len(attempts) == 4
    received = []
    while not messages.empty():
        received.append(messages.get_nowait())
    # 確認 handler 真的有收到東西，否則「沒出現密碼」毫無意義
    assert received
    assert all("s3cret" not in message for message in received)
    assert "s3cret" not in log_path.read_text(encoding="utf-8")


def test_stage_reporter_only_enqueues():
    """背景執行緒絕不能碰 Tk：回呼裡只握著 queue，沒有任何視窗物件。"""
    events: queue.Queue = queue.Queue()
    report = stage_reporter(events)

    report("等待登入")

    assert drain(events) == [("stage", "等待登入")]
    assert [cell.cell_contents for cell in report.__closure__] == [events]


def test_run_grab_delivers_stages_and_outcome_through_the_queue(offline_engine):
    events: queue.Queue = queue.Queue()
    runner = GrabRunner(make_config(), on_stage=stage_reporter(events))

    worker = threading.Thread(target=run_grab, args=(runner, events))
    worker.start()
    worker.join(timeout=30)

    items = drain(events)
    assert items[0] == ("stage", "瀏覽器自我檢查")
    assert ("stage", "已送出，等待回應") in items
    kind, attempts = items[-1]
    assert kind == "done" and len(attempts) == 4


def test_run_grab_reports_a_cancel_as_cancelled(offline_engine):
    events: queue.Queue = queue.Queue()
    runner = GrabRunner(make_config(), on_stage=stage_reporter(events))
    runner.cancel()

    run_grab(runner, events)

    assert drain(events)[-1] == ("cancelled", None)


def test_run_grab_reports_even_a_base_exception():
    """背景執行緒不管怎麼死，都要回報結果，否則表單永遠鎖住。"""

    class Abort(BaseException):
        pass

    class AbortingRunner:
        async def run(self):
            raise Abort("模擬非 Exception 的錯誤")

    events: queue.Queue = queue.Queue()
    run_grab(AbortingRunner(), events)

    kind, payload = drain(events)[-1]
    assert kind == "unexpected"
    assert "模擬非 Exception 的錯誤" in payload


class TestQuietWindow:
    """實際送出時刻會因校時晚於名目開搶時刻，過了名目時刻仍要保持安靜到送出為止。"""

    def test_quiet_in_the_last_seconds_before_open(self):
        assert in_quiet_window(remaining=QUIET_SECONDS, fired=False)
        assert in_quiet_window(remaining=0.5, fired=False)

    def test_not_quiet_while_far_away(self):
        assert not in_quiet_window(remaining=QUIET_SECONDS + 1, fired=False)

    def test_stays_quiet_past_nominal_until_fired(self):
        assert in_quiet_window(remaining=0, fired=False)
        assert in_quiet_window(remaining=-2.5, fired=False)

    def test_not_quiet_once_fired(self):
        assert not in_quiet_window(remaining=-0.1, fired=True)
        assert not in_quiet_window(remaining=3, fired=True)

    def test_gives_up_after_the_largest_possible_correction(self):
        assert not in_quiet_window(remaining=-MAX_AUTO_CORRECTION_SECONDS - 1, fired=False)


class TestStatusText:
    def test_countdown_shows_the_time_left(self):
        assert status_text("倒數", remaining=65, fired=False) == "倒數 1 分 5 秒"

    def test_other_stages_show_the_time_left_in_brackets(self):
        assert status_text("校時中", remaining=65, fired=False) == "校時中（還有 1 分 5 秒）"

    def test_after_firing_shows_the_stage_alone(self):
        assert status_text("已送出，等待回應", remaining=-1, fired=True) == "已送出，等待回應"

    def test_a_pinned_status_survives_every_tick(self):
        """按下取消後，下一秒的更新不能把「取消中…」蓋掉。"""
        assert status_text("校時中", remaining=65, fired=False, pinned="取消中…") == "取消中…"
        assert status_text("倒數", remaining=3, fired=False, pinned="取消中…") == "取消中…"

    def test_nothing_to_show_without_a_stage(self):
        assert status_text("", remaining=65, fired=False) is None


def test_day_choices_label_today():
    now = datetime(2026, 11, 14, 18, 20, tzinfo=TAIPEI)
    choices = day_choices(now, 7)
    assert choices[0] == ("11/14（六）今天", date(2026, 11, 14))
    assert len(choices) == 8


def test_days_follow_the_clock():
    """表單開著跨過午夜，選單要跟著變，昨天不能再出現。"""
    before = day_choices(datetime(2026, 11, 14, 23, 59, tzinfo=TAIPEI), 7)
    after = day_choices(datetime(2026, 11, 15, 0, 1, tzinfo=TAIPEI), 7)
    assert date(2026, 11, 14) in [d for _, d in before]
    assert date(2026, 11, 14) not in [d for _, d in after]


def test_status_without_open_time_shows_the_stage():
    assert status_text("登入中", remaining=0.0, fired=False) == "登入中"


def test_run_grab_reports_a_refused_selection_as_an_error():
    """確認視窗開太久、時段在送出前開始了：引擎拒絕，使用者要看到原因，不是「未預期的錯誤」。"""

    class RefusingRunner:
        async def run(self):
            raise ValueError("19:00 的時段已經過了")

    events: queue.Queue = queue.Queue()
    run_grab(RefusingRunner(), events)

    assert drain(events)[-1] == ("error", "19:00 的時段已經過了")
