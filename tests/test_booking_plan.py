from datetime import date, datetime, timedelta, timezone

import pytest

from badminton_bot.booking_plan import (
    BOOKABLE_HOURS,
    MAX_REQUESTS,
    TAIPEI,
    BookingTarget,
    booking_day,
    describe_schedule,
    expand_targets,
    next_open_at,
    summarize,
    validate_targets,
)
from badminton_bot.services.sports_center_webservice import Court

T = TAIPEI


def test_constants():
    assert BOOKABLE_HOURS == tuple(range(6, 22))
    assert MAX_REQUESTS == 12


def test_wednesday_evening_targets_thursday_midnight():
    assert next_open_at(datetime(2026, 11, 18, 21, 0, tzinfo=T)) == datetime(2026, 11, 19, tzinfo=T)


def test_just_after_midnight_targets_the_next_midnight():
    assert next_open_at(datetime(2026, 11, 19, 0, 0, 30, tzinfo=T)) == datetime(2026, 11, 20, tzinfo=T)


def test_exactly_midnight_targets_the_next_one():
    assert next_open_at(datetime(2026, 11, 19, tzinfo=T)) == datetime(2026, 11, 20, tzinfo=T)


def test_open_at_ignores_the_machine_timezone():
    now_utc = datetime(2026, 11, 18, 15, 59, tzinfo=timezone.utc)  # 台北 23:59
    assert next_open_at(now_utc) == datetime(2026, 11, 19, tzinfo=T)


def test_crosses_month_and_year():
    assert next_open_at(datetime(2026, 12, 31, 22, tzinfo=T)) == datetime(2027, 1, 1, tzinfo=T)


def test_booking_day_is_a_week_later_for_zhongzheng():
    assert booking_day(datetime(2026, 11, 19, tzinfo=T), 7) == date(2026, 11, 26)


def test_expand_is_the_full_cartesian_product():
    ts = expand_targets([Court("a", 1), Court("b", 2)], [20, 21], date(2026, 11, 26))
    assert [(t.court.qpid, t.hour) for t in ts] == [(1, 20), (1, 21), (2, 20), (2, 21)]
    assert ts[0].start == datetime(2026, 11, 26, 20, tzinfo=T)


def _targets(n):
    c = Court("a", 1)
    return tuple(BookingTarget(c, datetime(2026, 11, 26, 6, tzinfo=T)) for _ in range(n))


def test_twelve_is_allowed_thirteen_is_not():
    courts = [Court(str(i), i) for i in range(3)]
    validate_targets(expand_targets(courts, [6, 7, 8, 9], date(2026, 11, 26)))
    with pytest.raises(ValueError, match="12"):
        validate_targets(_targets(13))


def test_empty_selection_is_rejected():
    with pytest.raises(ValueError, match="請至少選"):
        validate_targets(())


NOW = datetime(2026, 11, 18, 21, 0, tzinfo=T)  # 週三
COURTS = [Court("a", 1), Court("b", 2)]


def test_summary_blocks_start_without_credentials():
    s = summarize(NOW, COURTS, [20, 21, 22], 7, has_credentials=False)
    assert not s.can_start
    assert "未填帳密" in s.text


def test_summary_blocks_empty_and_over_limit():
    s = summarize(NOW, [], [20], 7, True)
    assert not s.can_start and "未選場地或時段" in s.text
    s = summarize(NOW, [Court(str(i), i) for i in range(5)], [6, 7, 8], 7, True)
    assert not s.can_start and "超過上限 12" in s.text


def test_summary_warns_when_too_close():
    s = summarize(datetime(2026, 11, 18, 23, 57, tzinfo=T), COURTS, [20], 7, True)
    assert s.warning == "距離開搶太近，校時可能不準"
    assert s.can_start


def test_summary_no_warning_when_far():
    s = summarize(NOW, COURTS, [20], 7, True)
    assert s.warning is None


def test_summary_text_names_both_dates():
    s = summarize(NOW, COURTS, [20, 21, 22], 7, True)
    assert s.can_start
    assert "已選 2 片 × 3 時段 ＝ 6 個請求（上限 12）" in s.text
    assert "11/19（四）00:00" in s.text
    assert "11/26（四）" in s.text


def test_describe_schedule_names_open_time_and_booking_day():
    assert (
        describe_schedule(datetime(2026, 11, 19, tzinfo=T), date(2026, 11, 26))
        == "將於 11/19（四）00:00 開搶，預約 11/26（四）的場地"
    )
    # 開發模式的開搶時刻可以帶秒數
    assert describe_schedule(
        datetime(2026, 11, 14, 13, 5, 30, tzinfo=T), date(2026, 11, 14)
    ).startswith("將於 11/14（六）13:05:30 開搶")
