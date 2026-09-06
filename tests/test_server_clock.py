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

import asyncio
import random
from email.utils import formatdate

from badminton_bot.utils.server_clock import (
    MAX_USEFUL_UNCERTAINTY_SECONDS,
    ClockMeasurement,
    is_sample_usable,
    measure_server_clock,
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


class FakeResponse:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def text(self):
        return "<html></html>"


class FakeSession:
    """Answers every GET with a Date generated from a fixed clock skew."""

    def __init__(self, theta_true: float, rtt: float = 0.0, extra_headers=None):
        self.theta_true = theta_true
        self.rtt = rtt
        self.extra_headers = extra_headers or {}
        self.requested_urls: list[str] = []

    def get(self, url, **kwargs):
        import time

        self.requested_urls.append(url)
        server_now = time.time() - self.theta_true
        headers = {"Date": formatdate(timeval=server_now, usegmt=True)}
        headers.update(self.extra_headers)
        return FakeResponse(headers)


class TestIsSampleUsable:
    def test_accepts_a_freshly_generated_response(self):
        usable, _ = is_sample_usable({"Date": "Thu, 04 Sep 2025 16:00:00 GMT"})
        assert usable is True

    def test_rejects_a_response_with_no_date(self):
        usable, reason = is_sample_usable({})
        assert usable is False
        assert "Date" in reason

    def test_rejects_a_cached_response(self):
        """Age > 0 代表這個 Date 是舊的，拿來校時會把我們帶偏。"""
        usable, _ = is_sample_usable(
            {"Date": "Thu, 04 Sep 2025 16:00:00 GMT", "Age": "37"}
        )
        assert usable is False

    def test_accepts_an_age_of_zero(self):
        usable, _ = is_sample_usable(
            {"Date": "Thu, 04 Sep 2025 16:00:00 GMT", "Age": "0"}
        )
        assert usable is True

    def test_rejects_a_proxy_cache_hit(self):
        usable, _ = is_sample_usable(
            {"Date": "Thu, 04 Sep 2025 16:00:00 GMT", "X-Cache": "HIT from edge"}
        )
        assert usable is False


class TestMeasureServerClock:
    def test_measures_an_offset_it_was_never_told(self):
        session = FakeSession(theta_true=0.35)
        measurement = asyncio.run(
            measure_server_clock(
                session=session,
                url="https://example.invalid/list",
                deadline_epoch=__import__("time").time() + 3600,
                budget=8,
                rng=random.Random(0),
                min_gap=0.001,
                max_gap=0.002,
            )
        )
        assert measurement.theta is not None
        assert abs(measurement.theta - 0.35) < MAX_USEFUL_UNCERTAINTY_SECONDS
        # 收斂到夠窄就提前收手，所以次數是上限而不是定值
        assert 4 <= measurement.probe_count <= 8

    def test_stops_at_the_probe_budget(self):
        session = FakeSession(theta_true=0.0)
        measurement = asyncio.run(
            measure_server_clock(
                session=session,
                url="https://example.invalid/list",
                deadline_epoch=__import__("time").time() + 3600,
                budget=3,
                rng=random.Random(0),
                min_gap=0.001,
                max_gap=0.002,
            )
        )
        assert measurement.probe_count <= 3
        assert len(session.requested_urls) <= 3

    def test_a_deadline_already_past_yields_no_measurement(self):
        session = FakeSession(theta_true=0.0)
        measurement = asyncio.run(
            measure_server_clock(
                session=session,
                url="https://example.invalid/list",
                deadline_epoch=__import__("time").time() - 1,
                budget=8,
                rng=random.Random(0),
                min_gap=0.001,
                max_gap=0.002,
            )
        )
        assert measurement.theta is None
        assert session.requested_urls == []

    def test_cached_responses_are_discarded_not_trusted(self):
        session = FakeSession(theta_true=0.0, extra_headers={"Age": "99"})
        measurement = asyncio.run(
            measure_server_clock(
                session=session,
                url="https://example.invalid/list",
                deadline_epoch=__import__("time").time() + 3600,
                budget=4,
                rng=random.Random(0),
                min_gap=0.001,
                max_gap=0.002,
            )
        )
        assert measurement.theta is None
        assert measurement.discarded_count == 4

    def test_a_request_error_degrades_instead_of_raising(self):
        class ExplodingSession:
            requested_urls: list[str] = []

            def get(self, url, **kwargs):
                raise OSError("模擬連線中斷")

        measurement = asyncio.run(
            measure_server_clock(
                session=ExplodingSession(),
                url="https://example.invalid/list",
                deadline_epoch=__import__("time").time() + 3600,
                budget=3,
                rng=random.Random(0),
                min_gap=0.001,
                max_gap=0.002,
            )
        )
        assert measurement.theta is None


class TestClockMeasurement:
    def test_rtt_median_of_no_samples_is_zero(self):
        assert ClockMeasurement(theta=None, uncertainty=0.0).rtt_median == 0.0

    def test_rtt_median_ignores_an_outlier(self):
        measurement = ClockMeasurement(
            theta=0.0, uncertainty=0.0, rtt_samples=[0.020, 0.022, 0.900]
        )
        assert measurement.rtt_median == 0.022
