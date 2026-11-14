"""Offline tests for the parts of the GUI that need no screen."""

import asyncio
import logging
import queue
import threading
from datetime import datetime, timedelta

from badminton_bot.booking_plan import TAIPEI
from badminton_bot.engine import GrabConfig, GrabRunner, set_logger
from badminton_bot.gui import (
    QueueLogHandler,
    format_remaining,
    is_dev_mode,
    results_grid,
    run_grab,
    stage_reporter,
)
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
