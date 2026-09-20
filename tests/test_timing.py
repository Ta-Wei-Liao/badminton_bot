"""Tests for the pure timing helpers. No network, no browser."""

import time

import pytest

from badminton_bot.utils.timing import (
    MAX_AUTO_CORRECTION_SECONDS,
    compute_send_time,
    plan_send_time,
    resolve_theta,
    sleep_then_spin,
)


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


NOMINAL = 1_800_000_000.0
ONE_WAY = 0.012  # 單程飛行時間（不是回應時間）


class TestResolveTheta:
    def test_prefers_the_measured_server_offset(self):
        theta, margin, source = resolve_theta(
            theta_srv=0.180, uncertainty=0.025, theta_ntp=0.004, one_way_seconds=ONE_WAY
        )
        assert (theta, source) == (0.180, "server")
        assert margin == 0.025

    def test_server_margin_is_the_measurement_uncertainty(self):
        """送早是硬失敗、送晚只是可能輸，兩邊不對稱，所以偏晚一個不確定度。"""
        _, margin, _ = resolve_theta(
            theta_srv=0.0, uncertainty=0.031, theta_ntp=None, one_way_seconds=ONE_WAY
        )
        assert margin == 0.031

    def test_falls_back_to_ntp_and_neutralises_the_rtt_compensation(self):
        """只有 NTP 時我們不知道伺服器鐘差，不能冒著提早送出的風險。"""
        theta, margin, source = resolve_theta(
            theta_srv=None, uncertainty=0.0, theta_ntp=0.004, one_way_seconds=ONE_WAY
        )
        assert (theta, source) == (0.004, "ntp")
        assert margin == ONE_WAY

    def test_falls_back_to_zero_when_every_measurement_failed(self):
        theta, margin, source = resolve_theta(
            theta_srv=None, uncertainty=0.0, theta_ntp=None, one_way_seconds=ONE_WAY
        )
        assert (theta, source) == (0.0, "none")
        assert margin == ONE_WAY


class TestComputeSendTime:
    def test_fires_early_by_half_the_round_trip(self):
        """目標是請求抵達的瞬間對齊開放時刻，不是送出的瞬間。"""
        send_at, ok = compute_send_time(
            nominal_epoch=NOMINAL,
            theta=0.0,
            one_way_seconds=ONE_WAY,
            margin=0.0,
            manual_offset_ms=0,
        )
        assert ok is True
        assert send_at == pytest.approx(NOMINAL - ONE_WAY, abs=1e-6)

    def test_a_local_clock_running_fast_delays_the_send(self):
        """θ 為正代表本機比伺服器快，必須晚一點送才對得上伺服器的開放時刻。"""
        send_at, _ = compute_send_time(
            nominal_epoch=NOMINAL,
            theta=0.300,
            one_way_seconds=0.0,
            margin=0.0,
            manual_offset_ms=0,
        )
        assert send_at == NOMINAL + 0.300

    def test_manual_offset_is_applied_on_top_in_milliseconds(self):
        send_at, _ = compute_send_time(
            nominal_epoch=NOMINAL,
            theta=0.0,
            one_way_seconds=0.0,
            margin=0.0,
            manual_offset_ms=-50,
        )
        assert send_at == NOMINAL - 0.050

    def test_rejects_an_absurd_automatic_correction(self):
        send_at, ok = compute_send_time(
            nominal_epoch=NOMINAL,
            theta=120.0,
            one_way_seconds=ONE_WAY,
            margin=0.0,
            manual_offset_ms=0,
        )
        assert ok is False
        assert send_at == NOMINAL

    def test_the_manual_offset_survives_a_clamped_correction(self):
        """clamp 擋掉的是量測值，不是使用者的明確指令。"""
        send_at, ok = compute_send_time(
            nominal_epoch=NOMINAL,
            theta=120.0,
            one_way_seconds=ONE_WAY,
            margin=0.0,
            manual_offset_ms=100,
        )
        assert ok is False
        assert send_at == NOMINAL + 0.100

    def test_a_correction_exactly_on_the_limit_is_accepted(self):
        _, ok = compute_send_time(
            nominal_epoch=NOMINAL,
            theta=MAX_AUTO_CORRECTION_SECONDS,
            one_way_seconds=0.0,
            margin=0.0,
            manual_offset_ms=0,
        )
        assert ok is True


class TestPlanSendTime:
    def test_uses_the_measured_offset_when_it_is_sane(self):
        send_at, source, ok = plan_send_time(
            nominal_epoch=NOMINAL,
            theta_srv=0.120,
            uncertainty=0.020,
            theta_ntp=0.001,
            one_way_seconds=ONE_WAY,
            manual_offset_ms=0,
        )
        assert (source, ok) == ("server", True)
        assert send_at == pytest.approx(NOMINAL + 0.120 - ONE_WAY + 0.020, abs=1e-6)

    def test_a_clamped_server_measurement_falls_back_to_ntp(self):
        """量測爆掉時退回 NTP，而不是讓整場歪掉。"""
        send_at, source, ok = plan_send_time(
            nominal_epoch=NOMINAL,
            theta_srv=30.0,
            uncertainty=0.020,
            theta_ntp=0.004,
            one_way_seconds=ONE_WAY,
            manual_offset_ms=0,
        )
        assert (source, ok) == ("ntp", True)
        assert send_at == NOMINAL + 0.004


class TestClampWidenedForGenuinelyBadClocks:
    """實測發現的洞：本機時鐘慢 2.65 秒時，量對的修正量被 ±2 秒的 clamp 丟掉。

    clamp 的角色是擋住荒謬值，不是擋住「時鐘真的差很多」—— 而時鐘真的差很多的人，
    正是最需要這個補償的人。品質把關交給 MAX_USEFUL_UNCERTAINTY_SECONDS。
    """

    def test_a_two_and_a_half_second_offset_is_now_applied(self):
        send_at, ok = compute_send_time(
            nominal_epoch=NOMINAL,
            theta=-2.652,
            one_way_seconds=ONE_WAY,
            margin=0.0,
            manual_offset_ms=0,
        )
        assert ok is True
        assert send_at == pytest.approx(NOMINAL - 2.652 - ONE_WAY, abs=1e-6)

    def test_an_absurd_offset_is_still_rejected(self):
        _, ok = compute_send_time(
            nominal_epoch=NOMINAL,
            theta=120.0,
            one_way_seconds=ONE_WAY,
            margin=0.0,
            manual_offset_ms=0,
        )
        assert ok is False


class TestClampedCorrectionReportsNoSource:
    """被 clamp 掉時回報 source="ntp" 會誤導 —— 那個值其實已經被丟棄了。"""

    def test_source_is_none_when_the_correction_was_discarded(self):
        send_at, source, ok = plan_send_time(
            nominal_epoch=NOMINAL,
            theta_srv=None,
            uncertainty=0.0,
            theta_ntp=900.0,
            one_way_seconds=ONE_WAY,
            manual_offset_ms=0,
        )
        assert ok is False
        assert source == "none"
        assert send_at == NOMINAL

    def test_a_usable_source_is_still_reported(self):
        _, source, ok = plan_send_time(
            nominal_epoch=NOMINAL,
            theta_srv=None,
            uncertainty=0.0,
            theta_ntp=-2.652,
            one_way_seconds=ONE_WAY,
            manual_offset_ms=0,
        )
        assert (source, ok) == ("ntp", True)
