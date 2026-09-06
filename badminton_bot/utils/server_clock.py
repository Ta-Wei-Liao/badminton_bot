"""Measure the booking server's clock through its HTTP Date header.

Date only has one-second resolution, so a single probe pins the offset no
better than a one-second-wide interval. But the *phase* of the next probe is
ours to choose: send at an instant whose second-boundary lands on the midpoint
of what we already know, and the reply halves the interval. Seven or eight
probes converge to about one round-trip time, and further probes carry no
information at all — the request count has a mathematical ceiling, not an
arbitrary one.

Throughout, theta is the local clock minus the server clock, in seconds.
Positive means the local clock is ahead.
"""

import asyncio
import logging
import math
import random
import statistics
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Mapping

# 開場假設：本機與伺服器相差不超過兩秒。
INITIAL_INTERVAL = (-2.0, 2.0)


def parse_http_date(value: str) -> float:
    """Convert an RFC 7231 HTTP-date into epoch seconds.

    Args:
        value (str): the raw Date header value.

    Raises:
        ValueError: if the value is not a parsable HTTP-date.

    Returns:
        float: epoch seconds.
    """
    parsed = parsedate_to_datetime(value)
    if parsed is None:
        raise ValueError(f"無法解析的 Date header：{value!r}")

    return parsed.timestamp()


def constrain(
    t0: float, t3: float, server_date_epoch: float
) -> tuple[float, float]:
    """Derive the theta interval implied by one probe.

    The Date was stamped at some instant between t0 and t3, and at that instant
    the server's clock read somewhere in [D, D+1). Those two facts bound theta
    to an interval one second plus one round trip wide.

    Args:
        t0 (float): local epoch time the request was sent.
        t3 (float): local epoch time the reply arrived.
        server_date_epoch (float): the Date header, as epoch seconds.

    Returns:
        tuple[float, float]: the interval (low, high], exclusive at the low end.
    """
    return (t0 - server_date_epoch - 1.0, t3 - server_date_epoch)


def intersect(
    current: tuple[float, float], new: tuple[float, float]
) -> tuple[float, float] | None:
    """Narrow what we know by combining it with a fresh constraint.

    Args:
        current (tuple[float, float]): the interval accumulated so far.
        new (tuple[float, float]): the interval from the latest probe.

    Returns:
        tuple[float, float] | None: the overlap, or None if the two contradict
            each other — which means an assumption broke (the server's clock
            was stepped, a round trip spiked, or we read a cached Date).
    """
    low = max(current[0], new[0])
    high = min(current[1], new[1])
    if high <= low:
        return None

    return (low, high)


def next_probe_phase(interval: tuple[float, float], earliest: float) -> float:
    """Choose when to send the next probe so it halves the interval.

    If the send time t satisfies t - midpoint == some integer n, then the
    server's clock reads exactly n at the moment theta equals the midpoint.
    The reply's Date therefore lands on one side or the other of the midpoint,
    splitting the interval in half.

    Only the sub-second phase is constrained, so which second the probe falls
    in stays completely free — that is what lets the probes be spaced at
    random human-looking intervals.

    Args:
        interval (tuple[float, float]): what we know about theta so far.
        earliest (float): the earliest acceptable send time, epoch seconds.

    Returns:
        float: the local epoch time to send the next probe.
    """
    midpoint = (interval[0] + interval[1]) / 2

    return math.ceil(earliest - midpoint) + midpoint


# 探測間隔隨機抖動：固定間隔比人類規律太多，規律性本身就是機器人簽名。
# 抖動不影響二分搜尋的正確性 —— 受約束的只有小數相位，落在第幾秒完全自由。
MIN_PROBE_GAP_SECONDS = 12.0
MAX_PROBE_GAP_SECONDS = 25.0

# 七到八次就收斂到 RTT 量級，再多打沒有資訊增益。
PROBE_BUDGET = 8

# 連續兩次證據矛盾就代表前提站不住腳，放棄量測比硬撐出一個錯的答案好。
MAX_CONTRADICTIONS = 2

# 區間已經窄到這個程度就停手。
MIN_USEFUL_WIDTH_SECONDS = 0.02

# 沒收斂到這個程度就不值得採用：margin 會等於不確定度，
# 用一個 ±0.5 秒的估計反而保證送晚半秒，比不修正還糟。
MAX_USEFUL_UNCERTAINTY_SECONDS = 0.15


@dataclass
class ClockMeasurement:
    """The outcome of a server-clock measurement run."""

    theta: float | None
    uncertainty: float
    rtt_samples: list[float] = field(default_factory=list)
    probe_count: int = 0
    discarded_count: int = 0

    @property
    def rtt_median(self) -> float:
        """Median round-trip time in seconds, or 0.0 with no samples."""
        if not self.rtt_samples:
            return 0.0

        return statistics.median(self.rtt_samples)


def is_sample_usable(headers: Mapping[str, str]) -> tuple[bool, str]:
    """Decide whether a response's Date can be trusted for clock measurement.

    A cached response carries a stale Date, which would drag the estimate off
    by however long it sat in the cache.

    Args:
        headers (Mapping[str, str]): the response headers.

    Returns:
        tuple[bool, str]: (usable, reason when not usable).
    """
    if "Date" not in headers:
        return False, "回應沒有 Date header"

    age = headers.get("Age")
    if age is not None:
        try:
            if int(age) > 0:
                return False, f"回應來自快取（Age={age}）"
        except ValueError:
            return False, f"無法解析的 Age header：{age!r}"

    x_cache = headers.get("X-Cache", "")
    if "HIT" in x_cache.upper():
        return False, f"回應來自快取（X-Cache={x_cache}）"

    return True, ""


def _log_infrastructure(headers: Mapping[str, str]) -> None:
    """Record what generated the Date, since a proxy's clock is not the app's."""
    logging.info(
        "伺服器資訊：Server=%s Via=%s X-Powered-By=%s",
        headers.get("Server", "（無）"),
        headers.get("Via", "（無）"),
        headers.get("X-Powered-By", "（無）"),
    )
    if headers.get("Via"):
        logging.warning(
            "偵測到反向代理，Date 可能來自代理而非應用伺服器，量到的鐘差僅供參考"
        )


async def measure_server_clock(
    session,
    url: str,
    deadline_epoch: float,
    budget: int = PROBE_BUDGET,
    rng: random.Random | None = None,
    min_gap: float = MIN_PROBE_GAP_SECONDS,
    max_gap: float = MAX_PROBE_GAP_SECONDS,
) -> ClockMeasurement:
    """Binary-search the booking server's clock offset via its Date header.

    Degrades to a theta of None on any failure — losing the booking to a
    measurement problem would be far worse than firing with an uncorrected clock.

    Args:
        session: an aiohttp-style session exposing get(url) as an async context manager.
        url (str): a read-only page to probe.
        deadline_epoch (float): stop before this instant.
        budget (int, optional): maximum probes. Defaults to PROBE_BUDGET.
        rng (random.Random | None, optional): source of interval jitter.
        min_gap (float, optional): shortest gap between probes, seconds.
        max_gap (float, optional): longest gap between probes, seconds. Tests
            pass tiny values here so the suite does not really wait minutes.

    Returns:
        ClockMeasurement: theta and its uncertainty, plus RTT samples.
    """
    rng = rng or random.Random()
    interval = INITIAL_INTERVAL
    rtt_samples: list[float] = []
    probe_count = 0
    discarded_count = 0
    contradictions = 0
    logged_infrastructure = False

    while probe_count < budget:
        gap = rng.uniform(min_gap, max_gap)
        send_at = next_probe_phase(interval=interval, earliest=time.time() + gap)
        if send_at >= deadline_epoch:
            break

        wait_for = send_at - time.time()
        if wait_for > 0:
            await asyncio.sleep(wait_for)

        try:
            t0 = time.time()
            async with session.get(url) as response:
                await response.text()
                t3 = time.time()
                headers = response.headers
        except Exception as error:
            logging.warning("時鐘探測請求失敗：%s", error)
            discarded_count += 1
            probe_count += 1
            continue

        probe_count += 1

        if not logged_infrastructure:
            _log_infrastructure(headers)
            logged_infrastructure = True

        usable, reason = is_sample_usable(headers)
        if not usable:
            logging.warning("捨棄時鐘探測樣本：%s", reason)
            discarded_count += 1
            continue

        try:
            server_date = parse_http_date(headers["Date"])
        except ValueError as error:
            logging.warning("捨棄時鐘探測樣本：%s", error)
            discarded_count += 1
            continue

        rtt_samples.append(t3 - t0)
        narrowed = intersect(interval, constrain(t0, t3, server_date))

        if narrowed is None:
            contradictions += 1
            logging.warning(
                "時鐘探測證據矛盾（第 %d 次），重新開始累積", contradictions
            )
            if contradictions >= MAX_CONTRADICTIONS:
                logging.warning("時鐘探測連續矛盾，放棄伺服器校時")
                return ClockMeasurement(
                    theta=None,
                    uncertainty=0.0,
                    rtt_samples=rtt_samples,
                    probe_count=probe_count,
                    discarded_count=discarded_count,
                )
            interval = constrain(t0, t3, server_date)
        else:
            interval = narrowed

        width = interval[1] - interval[0]
        logging.info(
            "時鐘探測 %d/%d：θ ∈ (%.3f, %.3f]，寬度 %.3f 秒",
            probe_count,
            budget,
            interval[0],
            interval[1],
            width,
        )

        if width <= max(2 * statistics.median(rtt_samples), MIN_USEFUL_WIDTH_SECONDS):
            break

    uncertainty = (interval[1] - interval[0]) / 2

    if (
        not rtt_samples
        or interval == INITIAL_INTERVAL
        or uncertainty > MAX_USEFUL_UNCERTAINTY_SECONDS
    ):
        if uncertainty > MAX_USEFUL_UNCERTAINTY_SECONDS and rtt_samples:
            logging.warning(
                "伺服器校時未收斂（±%.0f 毫秒），不予採用", uncertainty * 1000
            )
        return ClockMeasurement(
            theta=None,
            uncertainty=0.0,
            rtt_samples=rtt_samples,
            probe_count=probe_count,
            discarded_count=discarded_count,
        )

    return ClockMeasurement(
        theta=(interval[0] + interval[1]) / 2,
        uncertainty=uncertainty,
        rtt_samples=rtt_samples,
        probe_count=probe_count,
        discarded_count=discarded_count,
    )
