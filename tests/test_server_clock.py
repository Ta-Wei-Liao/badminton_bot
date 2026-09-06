"""Tests for the server-clock binary search. No network."""

import math

import pytest

from badminton_bot.utils.server_clock import (
    INITIAL_INTERVAL,
    constrain,
    intersect,
    next_probe_phase,
    parse_http_date,
)


class TestParseHttpDate:
    def test_parses_an_rfc_7231_date(self):
        assert parse_http_date("Thu, 04 Sep 2025 16:00:00 GMT") == 1_757_001_600.0

    def test_rejects_rubbish(self):
        with pytest.raises(ValueError):
            parse_http_date("not a date at all")


class TestConstrain:
    def test_the_interval_is_one_second_plus_the_round_trip_wide(self):
        """Date 只有秒級精度，而它在送出到收到之間某一刻才蓋章。"""
        low, high = constrain(t0=1000.40, t3=1000.50, server_date_epoch=1000.0)
        assert (low, high) == pytest.approx((-0.6, 0.5))
        assert high - low == pytest.approx(1.1)

    def test_contains_a_synced_clock(self):
        low, high = constrain(t0=1000.40, t3=1000.50, server_date_epoch=1000.0)
        assert low < 0.0 <= high

    def test_contains_a_local_clock_running_fast(self):
        """本機快 0.3 秒：本機 1000.40 時伺服器才 1000.10，所以 Date 仍是 1000。"""
        low, high = constrain(t0=1000.40, t3=1000.50, server_date_epoch=1000.0)
        assert low < 0.3 <= high


class TestIntersect:
    def test_narrows_to_the_overlap(self):
        assert intersect((-0.6, 0.5), (0.1, 0.9)) == pytest.approx((0.1, 0.5))

    def test_returns_none_when_the_evidence_contradicts(self):
        """交集為空代表某個前提破了：時鐘被 step、RTT 暴衝，或讀到快取的 Date。"""
        assert intersect((-0.6, -0.2), (0.1, 0.9)) is None

    def test_touching_intervals_do_not_overlap(self):
        assert intersect((0.0, 0.5), (0.5, 0.9)) is None


class TestNextProbePhase:
    def test_the_send_time_puts_the_date_flip_on_the_midpoint(self):
        """t − mid 必須是整數，回應才會恰好把區間切在中點。"""
        interval = (0.10, 0.50)
        midpoint = 0.30
        send_at = next_probe_phase(interval=interval, earliest=1000.0)
        assert (send_at - midpoint) == pytest.approx(round(send_at - midpoint))

    def test_never_schedules_a_probe_in_the_past(self):
        send_at = next_probe_phase(interval=(0.10, 0.50), earliest=1000.0)
        assert send_at >= 1000.0

    def test_respects_a_later_earliest_bound(self):
        send_at = next_probe_phase(interval=(0.10, 0.50), earliest=1234.9)
        assert send_at >= 1234.9


class TestConvergence:
    """把整個二分流程跑完，確認它真的收斂而且收斂到真值。"""

    @staticmethod
    def simulate(theta_true: float, rtt: float, probe_count: int):
        interval = INITIAL_INTERVAL
        clock = 1_000_000.0
        widths = []

        for _ in range(probe_count):
            clock = next_probe_phase(interval=interval, earliest=clock + 1.0)
            t0 = clock
            t3 = clock + rtt
            stamped_at_local = clock + rtt / 2
            server_date = math.floor(stamped_at_local - theta_true)

            narrowed = intersect(interval, constrain(t0, t3, server_date))
            assert narrowed is not None, "模擬中不該出現矛盾證據"
            interval = narrowed
            widths.append(interval[1] - interval[0])

        return interval, widths

    @pytest.mark.parametrize("theta_true", [0.0, 0.187, -0.412, 0.913])
    def test_eight_probes_bracket_the_true_offset(self, theta_true):
        interval, _ = self.simulate(theta_true=theta_true, rtt=0.024, probe_count=8)
        assert interval[0] < theta_true <= interval[1]

    def test_eight_probes_converge_to_round_trip_scale(self):
        _, widths = self.simulate(theta_true=0.187, rtt=0.024, probe_count=8)
        assert widths[-1] < 0.15

    def test_each_probe_narrows_the_interval(self):
        _, widths = self.simulate(theta_true=0.187, rtt=0.024, probe_count=8)
        assert widths == sorted(widths, reverse=True)

    def test_further_probes_hit_the_round_trip_floor(self):
        """精度下限是 RTT，超過就沒有資訊增益了 —— 這是請求數量的天花板。"""
        _, widths = self.simulate(theta_true=0.187, rtt=0.024, probe_count=16)
        assert widths[-1] >= 0.024
