"""Pure timing maths for the booking race. All clock values are epoch float seconds."""

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


# 自動時鐘修正量的硬上限。量測爆掉時寧可不修正，也不要讓整場歪掉。
MAX_AUTO_CORRECTION_SECONDS = 2.0


def resolve_theta(
    theta_srv: float | None,
    uncertainty: float,
    theta_ntp: float | None,
    rtt_median: float,
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
        rtt_median (float): median round-trip time observed, seconds.

    Returns:
        tuple[float, float, str]: (theta, margin, source).
    """
    if theta_srv is not None:
        return theta_srv, uncertainty, "server"

    neutralise_rtt_compensation = rtt_median / 2
    if theta_ntp is not None:
        return theta_ntp, neutralise_rtt_compensation, "ntp"

    return 0.0, neutralise_rtt_compensation, "none"


def compute_send_time(
    nominal_epoch: float,
    theta: float,
    rtt_median: float,
    margin: float,
    manual_offset_ms: int,
) -> tuple[float, bool]:
    """Work out when to send so the request *arrives* just after the opening.

    The request needs half a round trip to reach the server, so sending at the
    nominal opening instant arrives late by that much.

    Args:
        nominal_epoch (float): the nominal opening instant, epoch seconds.
        theta (float): local clock minus server clock, seconds.
        rtt_median (float): median round-trip time observed, seconds.
        margin (float): deliberate lateness, seconds.
        manual_offset_ms (int): the user's own nudge, milliseconds.

    Returns:
        tuple[float, bool]: (send time epoch, whether the automatic
            correction passed the clamp).
    """
    automatic_correction = theta - rtt_median / 2 + margin
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
    rtt_median: float,
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
        rtt_median=rtt_median,
    )
    send_at, within_clamp = compute_send_time(
        nominal_epoch=nominal_epoch,
        theta=theta,
        rtt_median=rtt_median,
        margin=margin,
        manual_offset_ms=manual_offset_ms,
    )

    if not within_clamp and source == "server":
        theta, margin, source = resolve_theta(
            theta_srv=None,
            uncertainty=0.0,
            theta_ntp=theta_ntp,
            rtt_median=rtt_median,
        )
        send_at, within_clamp = compute_send_time(
            nominal_epoch=nominal_epoch,
            theta=theta,
            rtt_median=rtt_median,
            margin=margin,
            manual_offset_ms=manual_offset_ms,
        )

    return send_at, source, within_clamp
