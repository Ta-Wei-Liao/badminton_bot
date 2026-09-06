"""Tests for the pure timing helpers. No network, no browser."""

import time

from badminton_bot.utils.timing import sleep_then_spin


class TestSleepThenSpin:
    def test_returns_immediately_when_the_target_has_passed(self):
        started = time.perf_counter()
        sleep_then_spin(target_epoch=time.time() - 5)
        assert time.perf_counter() - started < 0.5

    def test_waits_until_a_target_in_the_near_future(self):
        target = time.time() + 0.2
        sleep_then_spin(target_epoch=target)
        assert time.time() >= target

    def test_busy_waits_precisely_inside_the_spin_window(self):
        """開搶那一刻靠 busy-wait 取得毫秒精度，不能被 sleep 的排程延遲拖過頭。"""
        target = time.time() + 0.15
        sleep_then_spin(target_epoch=target, spin_window=1.0)
        overshoot = time.time() - target
        assert 0 <= overshoot < 0.02

    def test_sleeps_instead_of_spinning_while_far_from_the_target(self):
        """遠離目標時要讓出 CPU，否則三分鐘倒數會把一顆核心燒滿。"""
        target = time.time() + 0.3
        started_cpu = time.process_time()
        sleep_then_spin(target_epoch=target, spin_window=0.05)
        cpu_used = time.process_time() - started_cpu
        assert cpu_used < 0.15

    def test_on_tick_fires_once_per_whole_second(self):
        """既有的倒數 log 用 microsecond == 0 判斷，實際上幾乎不成立，訊息印不出來。"""
        seen: list[int] = []
        target = time.time() + 2.1
        sleep_then_spin(
            target_epoch=target,
            spin_window=0.05,
            on_tick=lambda remaining: seen.append(int(remaining)),
        )
        assert seen == [2, 1, 0]
