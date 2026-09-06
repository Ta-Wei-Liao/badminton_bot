"""A minimal SNTP client, so the countdown can be anchored to standard time.

Hand-rolled on top of socket and struct rather than pulling in ntplib, to keep
requirements.txt and the PyInstaller bundle untouched.

This only ever talks to a national time server. It never touches the booking site.
"""

import logging
import socket
import struct
import time

NTP_SERVER = "time.stdtime.gov.tw"
NTP_PORT = 123
NTP_PACKET_SIZE = 48

# NTP 從 1900 起算，Unix epoch 從 1970 起算。
_NTP_EPOCH_DELTA = 2_208_988_800

# LI=0（無閏秒警告）、VN=3、Mode=3（client）。
_CLIENT_REQUEST = b"\x1b" + b"\x00" * 47


def _decode_timestamp(seconds: int, fraction: int) -> float:
    """Convert a 64-bit NTP timestamp into epoch seconds."""
    return seconds - _NTP_EPOCH_DELTA + fraction / 2**32


def parse_ntp_response(packet: bytes, t1: float, t4: float) -> tuple[float, float]:
    """Extract the clock offset and round-trip delay from an NTP reply.

    Args:
        packet (bytes): the raw 48-byte reply.
        t1 (float): local epoch time the request was sent.
        t4 (float): local epoch time the reply arrived.

    Raises:
        ValueError: if the packet is too short to hold the timestamps.

    Returns:
        tuple[float, float]: (theta, delay) in seconds, where theta is the
            local clock minus the server clock — positive means local is ahead.
    """
    if len(packet) < NTP_PACKET_SIZE:
        raise ValueError(f"NTP 回應長度不足：{len(packet)} 位元組")

    receive_seconds, receive_fraction, transmit_seconds, transmit_fraction = (
        struct.unpack("!IIII", packet[32:48])
    )
    t2 = _decode_timestamp(receive_seconds, receive_fraction)
    t3 = _decode_timestamp(transmit_seconds, transmit_fraction)

    server_minus_local = ((t2 - t1) + (t3 - t4)) / 2
    delay = (t4 - t1) - (t3 - t2)

    return -server_minus_local, delay


def pick_best_sample(samples: list[tuple[float, float]]) -> float | None:
    """Return the offset from the sample with the smallest round-trip delay.

    The least-delayed exchange is the one least distorted by queueing, which is
    the standard way to choose among NTP samples.

    Args:
        samples (list[tuple[float, float]]): (theta, delay) pairs.

    Returns:
        float | None: the chosen theta, or None if there are no samples.
    """
    if not samples:
        return None

    return min(samples, key=lambda sample: sample[1])[0]


def _collect_one_sample(server: str, timeout: float) -> tuple[float, float]:
    """Exchange one request/reply with the NTP server. Raises on any failure."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(timeout)
        t1 = time.time()
        client.sendto(_CLIENT_REQUEST, (server, NTP_PORT))
        packet, _ = client.recvfrom(256)
        t4 = time.time()

    return parse_ntp_response(packet=packet, t1=t1, t4=t4)


def query_clock_offset(
    server: str = NTP_SERVER, sample_count: int = 3, timeout: float = 3.0
) -> float | None:
    """Measure how far the local clock is from standard time.

    Degrades to None on any failure — losing the countdown to a time server
    outage would be far worse than running with an uncorrected clock.

    Args:
        server (str, optional): NTP host. Defaults to NTP_SERVER.
        sample_count (int, optional): exchanges to attempt. Defaults to 3.
        timeout (float, optional): per-exchange timeout in seconds. Defaults to 3.0.

    Returns:
        float | None: local clock minus standard time in seconds, or None.
    """
    samples: list[tuple[float, float]] = []

    for _ in range(sample_count):
        try:
            samples.append(_collect_one_sample(server=server, timeout=timeout))
        except (OSError, ValueError, struct.error) as error:
            logging.warning("NTP 取樣失敗：%s", error)

    theta = pick_best_sample(samples)
    if theta is None:
        logging.warning("NTP 校時全部失敗，改用未校正的本機時鐘")
    else:
        logging.info("NTP 校時完成：本機時鐘比標準時間快 %.1f 毫秒", theta * 1000)

    return theta
