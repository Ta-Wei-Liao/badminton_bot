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
