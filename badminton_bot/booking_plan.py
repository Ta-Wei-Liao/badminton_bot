"""Pure booking-plan logic: open time, court x hour expansion, limits, summary."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum

from .services.sports_center_webservice import Court

TAIPEI = timezone(timedelta(hours=8))
BOOKABLE_HOURS = tuple(range(6, 22))
MAX_REQUESTS = 12
TOO_CLOSE = timedelta(minutes=4)

_WEEKDAYS = "一二三四五六日"

IMMEDIATE_REMINDER = "立即預約會真的訂下場地；請勿短時間內反覆送出，頻繁登入可能導致帳號被停權。"


class GrabMode(Enum):
    """How a run picks its send instant; the value is the radio-button label."""

    MIDNIGHT = "搶午夜"
    IMMEDIATE = "立即預約"


def next_open_at(now: datetime) -> datetime:
    """Next Taipei midnight strictly after ``now`` (which must be aware)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(TAIPEI)
    return datetime.combine(local.date() + timedelta(days=1), time(), tzinfo=TAIPEI)


def booking_day(open_at: datetime, window_days: int) -> date:
    return open_at.astimezone(TAIPEI).date() + timedelta(days=window_days)


@dataclass(frozen=True)
class BookingTarget:
    court: Court
    start: datetime

    @property
    def hour(self) -> int:
        return self.start.hour


def expand_targets(
    courts: Sequence[Court], hours: Sequence[int], day: date
) -> tuple[BookingTarget, ...]:
    return tuple(
        BookingTarget(court, datetime.combine(day, time(hour), tzinfo=TAIPEI))
        for court in courts
        for hour in hours
    )


def validate_targets(targets: Sequence[BookingTarget]) -> None:
    if not targets:
        raise ValueError("請至少選一片場地和一個時段")
    if len(targets) > MAX_REQUESTS:
        raise ValueError(f"最多只能同時搶 {MAX_REQUESTS} 格，目前選了 {len(targets)} 格")


@dataclass(frozen=True)
class SelectionSummary:
    text: str
    can_start: bool
    warning: str | None


def _fmt(d: date | datetime) -> str:
    return f"{d.month:02d}/{d.day:02d}（{_WEEKDAYS[d.weekday()]}）"


def describe_selection(court_count: int, hour_count: int) -> str:
    """「已選 N 片 × M 時段 ＝ K 個請求（上限 12）」."""
    count = court_count * hour_count
    return f"已選 {court_count} 片 × {hour_count} 時段 ＝ {count} 個請求（上限 {MAX_REQUESTS}）"


def describe_schedule(open_at: datetime, day: date) -> str:
    """「將於 11/19（四）00:00 開搶，預約 11/26（四）的場地」; seconds only when non-zero."""
    local = open_at.astimezone(TAIPEI)
    clock = f"{local:%H:%M:%S}" if local.second else f"{local:%H:%M}"
    return f"將於 {_fmt(local)}{clock} 開搶，預約 {_fmt(day)}的場地"


def summarize(
    now: datetime,
    courts: Sequence[Court],
    hours: Sequence[int],
    window_days: int,
    has_credentials: bool,
) -> SelectionSummary:
    open_at = next_open_at(now)
    count = len(courts) * len(hours)
    warning = "距離開搶太近，校時可能不準" if open_at - now < TOO_CLOSE else None
    if not has_credentials:
        return SelectionSummary("未填帳密", False, warning)
    if count == 0:
        return SelectionSummary("未選場地或時段", False, warning)
    if count > MAX_REQUESTS:
        return SelectionSummary(
            f"已選 {len(courts)} 片 × {len(hours)} 時段 ＝ {count} 個請求，超過上限 {MAX_REQUESTS}",
            False,
            warning,
        )
    text = (
        f"{describe_selection(len(courts), len(hours))}"
        f"｜{describe_schedule(open_at, booking_day(open_at, window_days))}"
    )
    return SelectionSummary(text, True, warning)


def immediate_days(now: datetime, window_days: int) -> tuple[date, ...]:
    """Days that are already open for booking: Taipei today through today + window_days."""
    today = now.astimezone(TAIPEI).date()
    return tuple(today + timedelta(days=offset) for offset in range(window_days + 1))


def bookable_hours(day: date, now: datetime) -> tuple[int, ...]:
    """The hours of day whose slot has not started yet at now."""
    return tuple(
        hour
        for hour in BOOKABLE_HOURS
        if datetime.combine(day, time(hour), tzinfo=TAIPEI) > now
    )


def describe_day(day: date, today: date) -> str:
    """「11/14（六）今天」 for today, 「11/16（一）」 otherwise."""
    return f"{_fmt(day)}今天" if day == today else _fmt(day)


def validate_immediate(
    targets: Sequence[BookingTarget], now: datetime, window_days: int
) -> None:
    """Refuse an immediate booking the site could not accept right now.

    Raises:
        ValueError: no targets, too many, a day outside the open window, or a
            slot that has already started.
    """
    validate_targets(targets)
    days = immediate_days(now, window_days)
    for booking_target in targets:
        day = booking_target.start.date()
        if day not in days:
            raise ValueError(f"{_fmt(day)}不在可預約的日期範圍內")
        if booking_target.start <= now:
            raise ValueError(f"{booking_target.hour:02d}:00 的時段已經過了")


def summarize_immediate(
    now: datetime,
    courts: Sequence[Court],
    hours: Sequence[int],
    day: date,
    window_days: int,
    has_credentials: bool,
) -> SelectionSummary:
    """The summary line for 立即預約; never carries the too-close warning."""
    count = len(courts) * len(hours)
    if not has_credentials:
        return SelectionSummary("未填帳密", False, None)
    if count == 0:
        return SelectionSummary("未選場地或時段", False, None)
    if count > MAX_REQUESTS:
        return SelectionSummary(
            f"已選 {len(courts)} 片 × {len(hours)} 時段 ＝ {count} 個請求，超過上限 {MAX_REQUESTS}",
            False,
            None,
        )
    try:
        validate_immediate(expand_targets(courts, hours, day), now, window_days)
    except ValueError as error:
        return SelectionSummary(str(error), False, None)
    text = f"{describe_selection(len(courts), len(hours))}｜立即預約 {_fmt(day)}的場地"
    return SelectionSummary(text, True, None)


def describe_confirmation(targets: Sequence[BookingTarget]) -> str:
    """The confirm-dialog body: the day, one line per booking, and the reminder."""
    lines = [f"將立即預約 {_fmt(targets[0].start.date())}的以下場地："]
    lines += [
        f"{t.court.name} {t.hour:02d}:00–{t.hour + 1:02d}:00" for t in targets
    ]
    lines += ["", "按下「確定」會真的預約這些場地。", IMMEDIATE_REMINDER]
    return "\n".join(lines)
