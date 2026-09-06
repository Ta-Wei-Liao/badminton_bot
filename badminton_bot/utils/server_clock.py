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

import math
from email.utils import parsedate_to_datetime

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
