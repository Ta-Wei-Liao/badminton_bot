"""Tests for the SNTP client. No sockets are opened."""

import struct

import pytest

from badminton_bot.utils import ntp_client
from badminton_bot.utils.ntp_client import (
    parse_ntp_response,
    pick_best_sample,
    query_clock_offset,
)

NTP_EPOCH_DELTA = 2_208_988_800


def build_packet(server_receive_epoch: float, server_transmit_epoch: float) -> bytes:
    """Assemble the 48-byte reply an NTP server would send. Test helper only."""

    def _encode(epoch: float) -> bytes:
        ntp_seconds = int(epoch) + NTP_EPOCH_DELTA
        fraction = int((epoch % 1) * (2**32))
        return struct.pack("!II", ntp_seconds, fraction)

    header_and_ignored_fields = b"\x1c" + b"\x00" * 23
    originate_timestamp = b"\x00" * 8
    return (
        header_and_ignored_fields
        + originate_timestamp
        + _encode(server_receive_epoch)
        + _encode(server_transmit_epoch)
    )


class TestParseNtpResponse:
    def test_a_perfectly_synced_clock_has_no_offset(self):
        t1, t4 = 1_800_000_000.000, 1_800_000_000.040
        packet = build_packet(
            server_receive_epoch=1_800_000_000.019,
            server_transmit_epoch=1_800_000_000.021,
        )
        theta, delay = parse_ntp_response(packet=packet, t1=t1, t4=t4)
        assert theta == pytest.approx(0.0, abs=0.002)
        assert delay == pytest.approx(0.038, abs=0.002)

    def test_a_local_clock_running_fast_yields_a_positive_theta(self):
        """theta 的定義是本機減伺服器，本機快就是正的。"""
        t1, t4 = 1_800_000_000.500, 1_800_000_000.540
        packet = build_packet(
            server_receive_epoch=1_800_000_000.019,
            server_transmit_epoch=1_800_000_000.021,
        )
        theta, _ = parse_ntp_response(packet=packet, t1=t1, t4=t4)
        assert theta == pytest.approx(0.5, abs=0.002)

    def test_a_local_clock_running_slow_yields_a_negative_theta(self):
        t1, t4 = 1_799_999_999.700, 1_799_999_999.740
        packet = build_packet(
            server_receive_epoch=1_799_999_999.919,
            server_transmit_epoch=1_799_999_999.921,
        )
        theta, _ = parse_ntp_response(packet=packet, t1=t1, t4=t4)
        assert theta == pytest.approx(-0.2, abs=0.002)

    def test_rejects_a_truncated_packet(self):
        with pytest.raises(ValueError):
            parse_ntp_response(packet=b"\x00" * 20, t1=0.0, t4=0.1)


class TestPickBestSample:
    def test_takes_the_offset_from_the_lowest_delay_sample(self):
        """延遲最小的樣本受排隊影響最少，是 NTP 的標準做法。"""
        samples = [(0.30, 0.200), (0.11, 0.020), (0.25, 0.150)]
        assert pick_best_sample(samples) == 0.11

    def test_returns_none_when_every_sample_failed(self):
        assert pick_best_sample([]) is None


class TestQueryClockOffset:
    def test_returns_the_best_offset_across_samples(self, monkeypatch):
        monkeypatch.setattr(
            ntp_client,
            "_collect_one_sample",
            lambda server, timeout: (0.11, 0.020),
        )
        assert query_clock_offset(sample_count=3) == 0.11

    def test_a_total_failure_degrades_instead_of_raising(self, monkeypatch):
        """NTP 掛掉絕不能中斷搶場地。"""

        def _always_fails(server, timeout):
            raise OSError("模擬逾時")

        monkeypatch.setattr(ntp_client, "_collect_one_sample", _always_fails)
        assert query_clock_offset(sample_count=3) is None

    def test_survives_a_partial_failure(self, monkeypatch):
        results = iter([OSError("模擬逾時"), (0.11, 0.020), (0.40, 0.300)])

        def _flaky(server, timeout):
            outcome = next(results)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(ntp_client, "_collect_one_sample", _flaky)
        assert query_clock_offset(sample_count=3) == 0.11
