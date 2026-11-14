from datetime import date, datetime, timedelta, timezone

import pytest

from badminton_bot.booking_plan import (
    BOOKABLE_HOURS,
    IMMEDIATE_REMINDER,
    MAX_REQUESTS,
    TAIPEI,
    BookingTarget,
    GrabMode,
    bookable_hours,
    booking_day,
    describe_confirmation,
    describe_day,
    describe_schedule,
    expand_targets,
    immediate_days,
    next_open_at,
    summarize,
    summarize_immediate,
    validate_immediate,
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


IMMEDIATE_NOW = datetime(2026, 11, 14, 18, 20, tzinfo=T)  # 週六 18:20
C72 = Court("羽球7-2", 1197)


class TestImmediateRules:
    def test_mode_labels(self):
        assert [m.value for m in GrabMode] == ["搶午夜", "立即預約"]

    def test_days_run_from_today_through_the_window(self):
        days = immediate_days(IMMEDIATE_NOW, 7)
        assert days[0] == date(2026, 11, 14)
        assert days[-1] == date(2026, 11, 21)
        assert len(days) == 8

    def test_days_use_taipei_time(self):
        now_utc = datetime(2026, 11, 14, 16, 30, tzinfo=timezone.utc)  # 台北 11/15 00:30
        assert immediate_days(now_utc, 7)[0] == date(2026, 11, 15)

    def test_today_offers_only_hours_that_have_not_started(self):
        assert bookable_hours(date(2026, 11, 14), IMMEDIATE_NOW) == (19, 20, 21)

    def test_an_hour_starting_exactly_now_is_gone(self):
        assert 19 not in bookable_hours(date(2026, 11, 14), datetime(2026, 11, 14, 19, 0, tzinfo=T))

    def test_future_days_offer_every_hour(self):
        assert bookable_hours(date(2026, 11, 15), IMMEDIATE_NOW) == BOOKABLE_HOURS

    def test_past_days_offer_nothing(self):
        assert bookable_hours(date(2026, 11, 13), IMMEDIATE_NOW) == ()

    def test_describe_day(self):
        assert describe_day(date(2026, 11, 14), today=date(2026, 11, 14)) == "11/14（六）今天"
        assert describe_day(date(2026, 11, 16), today=date(2026, 11, 14)) == "11/16（一）"

    def test_validate_accepts_an_open_future_slot(self):
        validate_immediate(expand_targets([C72], [6], date(2026, 11, 16)), IMMEDIATE_NOW, 7)

    def test_validate_rejects_a_started_hour(self):
        with pytest.raises(ValueError, match="已經過了"):
            validate_immediate(expand_targets([C72], [18], date(2026, 11, 14)), IMMEDIATE_NOW, 7)

    def test_validate_rejects_days_outside_the_window(self):
        for day in (date(2026, 11, 13), date(2026, 11, 22)):
            with pytest.raises(ValueError, match="不在可預約"):
                validate_immediate(expand_targets([C72], [20], day), IMMEDIATE_NOW, 7)

    def test_validate_keeps_the_request_cap(self):
        courts = [Court(f"c{i}", i) for i in range(5)]
        with pytest.raises(ValueError):
            validate_immediate(expand_targets(courts, [6, 7, 8], date(2026, 11, 16)), IMMEDIATE_NOW, 7)

    def test_summary_text(self):
        result = summarize_immediate(IMMEDIATE_NOW, [C72], [6], date(2026, 11, 16), 7, True)
        assert result.can_start
        assert result.warning is None
        assert result.text == "已選 1 片 × 1 時段 ＝ 1 個請求（上限 12）｜立即預約 11/16（一）的場地"

    def test_summary_refuses_without_credentials_or_selection(self):
        assert summarize_immediate(IMMEDIATE_NOW, [C72], [6], date(2026, 11, 16), 7, False).text == "未填帳密"
        assert summarize_immediate(IMMEDIATE_NOW, [], [6], date(2026, 11, 16), 7, True).can_start is False

    def test_summary_refuses_a_started_hour(self):
        result = summarize_immediate(IMMEDIATE_NOW, [C72], [18], date(2026, 11, 14), 7, True)
        assert result.can_start is False

    def test_confirmation_lists_every_booking_and_warns(self):
        text = describe_confirmation(expand_targets([C72], [6, 7], date(2026, 11, 16)))
        assert "11/16（一）" in text
        assert "羽球7-2 06:00–07:00" in text
        assert "羽球7-2 07:00–08:00" in text
        assert "會真的預約" in text
        assert IMMEDIATE_REMINDER in text
