"""Pure timing maths for the booking race. All clock values are epoch float seconds.

One distinction matters more than it looks. The send-time formula compensates
for the time a request spends in flight, and the obvious source for that is
half the observed round trip. That is only valid when the round trip is mostly
network. Against a slow origin it is not: a booking site was measured taking
~5 seconds to answer on an already-warmed connection, virtually all of it
application processing. Halving that and firing 2.5 seconds early would be a
hard failure, because the server rejects a slot that has not opened yet. So
callers pass a one-way flight time estimated from the TCP/TLS handshake, which
contains no application processing at all.
"""

import time
from typing import Callable

# 最後這段時間改用 busy-wait 取得毫秒精度。留 5 秒而非 2 秒，
# 是為了容忍 macOS time.sleep 的排程 overshoot。
SPIN_WINDOW_SECONDS = 5.0

# 粗略等待時每次最多睡這麼久，讓 on_tick 有機會每秒回報一次。
_COARSE_SLEEP_SECONDS = 0.2


def sleep_then_spin(
    target_epoch: float,
    spin_window: float = SPIN_WINDOW_SECONDS,
    on_tick: Callable[[float], None] | None = None,
) -> None:
    """Block until the wall clock reaches target_epoch.

    Sleeps coarsely while far from the target and busy-waits only inside
    spin_window, so a three-minute countdown does not peg a core while still
    firing with millisecond precision.

    Args:
        target_epoch (float): the epoch second to wait for.
        spin_window (float, optional): how long before the target to switch
            from sleeping to busy-waiting. Defaults to SPIN_WINDOW_SECONDS.
        on_tick (Callable[[float], None] | None, optional): called once per
            whole second of remaining time, with the seconds remaining.
    """
    last_reported: int | None = None

    while True:
        remaining = target_epoch - time.time()
        if remaining <= 0:
            return

        if on_tick is not None:
            whole_seconds = int(remaining)
            if whole_seconds != last_reported:
                last_reported = whole_seconds
                on_tick(remaining)

        if remaining > spin_window:
            time.sleep(min(_COARSE_SLEEP_SECONDS, remaining - spin_window))


# 自動時鐘修正量的硬上限。這只是擋住荒謬值的最後一道防線，不是品質把關 ——
# 實測遇過本機時鐘慢 2.65 秒的機器，原本 ±2 秒的上限把「量對的」修正量也丟掉了，
# 而時鐘真的差很多的人正是最需要補償的人。真正的品質把關是
# server_clock.MAX_USEFUL_UNCERTAINTY_SECONDS（沒收斂就不採用）。
MAX_AUTO_CORRECTION_SECONDS = 30.0


def resolve_theta(
    theta_srv: float | None,
    uncertainty: float,
    theta_ntp: float | None,
    one_way_seconds: float,
) -> tuple[float, float, str]:
    """Pick which clock offset to trust and the safety margin that goes with it.

    Priority is the measured booking-server offset, then NTP, then nothing.

    The margin exists because the two failure directions are not symmetric:
    firing early is a hard failure (the server rejects an unopened slot),
    while firing late merely risks losing the race. With a measured server
    offset the margin is the measurement uncertainty. Without one we cannot
    trust our knowledge of the server's clock enough to risk firing early,
    so the margin cancels the RTT/2 compensation instead.

    Args:
        theta_srv (float | None): measured local-minus-server offset, seconds.
        uncertainty (float): half-width of the measured interval, seconds.
        theta_ntp (float | None): local-minus-standard-time offset, seconds.
        one_way_seconds (float): estimated one-way network flight time,
            seconds. This is NOT half the response time — see the module
            note on why that distinction matters.

    Returns:
        tuple[float, float, str]: (theta, margin, source).
    """
    if theta_srv is not None:
        return theta_srv, uncertainty, "server"

    neutralise_flight_compensation = one_way_seconds
    if theta_ntp is not None:
        return theta_ntp, neutralise_flight_compensation, "ntp"

    return 0.0, neutralise_flight_compensation, "none"


def compute_send_time(
    nominal_epoch: float,
    theta: float,
    one_way_seconds: float,
    margin: float,
    manual_offset_ms: int,
) -> tuple[float, bool]:
    """Work out when to send so the request *arrives* just after the opening.

    The request needs half a round trip to reach the server, so sending at the
    nominal opening instant arrives late by that much.

    Args:
        nominal_epoch (float): the nominal opening instant, epoch seconds.
        theta (float): local clock minus server clock, seconds.
        one_way_seconds (float): estimated one-way network flight time,
            seconds. This is NOT half the response time — see the module
            note on why that distinction matters.
        margin (float): deliberate lateness, seconds.
        manual_offset_ms (int): the user's own nudge, milliseconds.

    Returns:
        tuple[float, bool]: (send time epoch, whether the automatic
            correction passed the clamp).
    """
    automatic_correction = theta - one_way_seconds + margin
    within_clamp = abs(automatic_correction) <= MAX_AUTO_CORRECTION_SECONDS
    if not within_clamp:
        automatic_correction = 0.0

    return (
        nominal_epoch + automatic_correction + manual_offset_ms / 1000,
        within_clamp,
    )


def plan_send_time(
    nominal_epoch: float,
    theta_srv: float | None,
    uncertainty: float,
    theta_ntp: float | None,
    one_way_seconds: float,
    manual_offset_ms: int,
) -> tuple[float, str, bool]:
    """Resolve the clock offset, clamp it, and return the send time.

    A measured server offset that fails the clamp falls back to NTP rather
    than to no correction at all.

    Returns:
        tuple[float, str, bool]: (send time epoch, source used, clamp passed).
    """
    theta, margin, source = resolve_theta(
        theta_srv=theta_srv,
        uncertainty=uncertainty,
        theta_ntp=theta_ntp,
        one_way_seconds=one_way_seconds,
    )
    send_at, within_clamp = compute_send_time(
        nominal_epoch=nominal_epoch,
        theta=theta,
        one_way_seconds=one_way_seconds,
        margin=margin,
        manual_offset_ms=manual_offset_ms,
    )

    if not within_clamp and source == "server":
        theta, margin, source = resolve_theta(
            theta_srv=None,
            uncertainty=0.0,
            theta_ntp=theta_ntp,
            one_way_seconds=one_way_seconds,
        )
        send_at, within_clamp = compute_send_time(
            nominal_epoch=nominal_epoch,
            theta=theta,
            one_way_seconds=one_way_seconds,
            margin=margin,
            manual_offset_ms=manual_offset_ms,
        )

    if not within_clamp:
        # 修正量被拒絕就等於完全沒校正，回報來源必須反映這件事，
        # 否則 log 會說「來源：ntp」而那個值其實已經被丟掉了。
        source = "none"

    return send_at, source, within_clamp
