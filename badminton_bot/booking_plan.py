"""Pure booking-plan logic: open time, court x hour expansion, limits, summary."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .services.sports_center_webservice import Court

TAIPEI = timezone(timedelta(hours=8))
BOOKABLE_HOURS = tuple(range(6, 22))
MAX_REQUESTS = 12
TOO_CLOSE = timedelta(minutes=4)

_WEEKDAYS = "一二三四五六日"


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
        f"已選 {len(courts)} 片 × {len(hours)} 時段 ＝ {count} 個請求（上限 {MAX_REQUESTS}）"
        f"｜將於 {_fmt(open_at)}00:00 開搶，預約 {_fmt(booking_day(open_at, window_days))}的場地"
    )
    return SelectionSummary(text, True, warning)
