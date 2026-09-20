# 搶場地延遲優化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓搶場地請求**抵達**預約伺服器的瞬間剛過開放時刻，並消除握手與準備工作造成的延遲。

**Architecture:** 四層由下而上獨立可測 ——（1）純時間數學（送出時刻計算、倒數）；
（2）時鐘量測（NTP 校時、用 HTTP `Date` header 對伺服器時鐘做二分逼近）；
（3）服務層（唯讀列表頁 URL、連線預熱、預組 URL + `asyncio.Event` 觸發、儀表結果物件）；
（4）`main.py` 編排。所有新增環節失敗時一律降級，絕不中斷搶場地。

**Tech Stack:** Python 3.11.4（pyenv-virtualenv `badminton_bot`）、aiohttp 3.11.13、
selenium 4.29.0、pytest 9.1.1。**不新增任何套件**（SNTP 以 `socket` + `struct` 自製）。

**Spec:** `docs/superpowers/specs/2026-09-06-booking-latency-optimization-design.md`

## Global Constraints

- **Live-site constraint（CLAUDE.md）**：測試一律離線。本計畫的所有測試都不得碰網路。
  服務物件用 `tests/test_sports_center_webservice.py` 既有的 `build_without_browser`
  （`object.__new__(cls)`）建立，`__init__` 不執行、Chrome 不啟動。
- 執行方式：`pyenv activate badminton_bot`，從 repo 根目錄跑 `python -m badminton_bot.main`；
  測試 `pytest`。
- **提示、log 訊息、行內註解一律用繁體中文；docstring 與識別字用英文。**
- **Commit 訊息使用 conventional-commit 前綴，且絕對不加 `Co-Authored-By` 或任何
  attribution trailer。** 一個功能一個分支。
- 所有時鐘運算走 epoch float（`time.time()`），持續時間用 `time.perf_counter()`。
- 不新增 `requirements.txt` 相依，`main.spec` / `build/` / `dist/` 是產出物，不要手改。
- 自動時鐘修正量硬上限 `MAX_AUTO_CORRECTION_SECONDS = 2.0`。
- 探測請求預算：一次執行最多 8 次探測，間隔隨機 12～25 秒。

---

## File Structure

**新增：**

| 檔案 | 責任 |
| --- | --- |
| `badminton_bot/utils/timing.py` | 純時間數學：等待迴圈、θ 取捨、送出時刻計算與 clamp |
| `badminton_bot/utils/ntp_client.py` | SNTP client，取得 θ_ntp |
| `badminton_bot/utils/server_clock.py` | 用 `Date` header 對伺服器時鐘二分逼近 |
| `tests/test_timing.py` | Task 1、2 的測試 |
| `tests/test_ntp_client.py` | Task 3 的測試 |
| `tests/test_server_clock.py` | Task 4、5 的測試 |

**修改：**

| 檔案 | 改動 |
| --- | --- |
| `badminton_bot/services/sports_center_webservice.py` | `target_qpid` 契約、列表頁/預熱 URL、格子解析、UA 常數、session 建構、預熱、`booking_courts` 改寫 |
| `badminton_bot/services/zhongzheng_sports_center_webservice.py` | `target_qpid = 1199`、列表頁 URL |
| `badminton_bot/services/zhongshan_sports_center_webservice.py` | `target_qpid = 84`、列表頁 URL（推論，須註明） |
| `badminton_bot/main.py` | `count_down` 改寫、完整編排、dev mode 降級、`return_exceptions=True` |
| `tests/test_main.py` | `count_down` 既有測試需續存 |
| `tests/test_sports_center_webservice.py` | 新 hook 與契約測試 |

---

### Task 1: 等待迴圈與倒數修正

把倒數從「滿載忙碌輪詢三分鐘」改成「遠時粗略 sleep、最後 5 秒才 busy-wait」，
並修掉兩個既有缺陷：倒數 log 條件實際上不成立、`timedelta.seconds` 遇負值會捲成 ~86400。

**Files:**
- Create: `badminton_bot/utils/timing.py`
- Modify: `badminton_bot/main.py`（`count_down`，約 line 160-183）
- Test: `tests/test_timing.py`

**Interfaces:**
- Consumes: 無
- Produces:
  - `sleep_then_spin(target_epoch: float, spin_window: float = 5.0, on_tick: Callable[[float], None] | None = None) -> None`
  - `SPIN_WINDOW_SECONDS: float = 5.0`

- [ ] **Step 1: Write the failing test**

`tests/test_timing.py`：

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_timing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'badminton_bot.utils.timing'`

- [ ] **Step 3: Write minimal implementation**

`badminton_bot/utils/timing.py`：

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_timing.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 改寫 main.count_down 使用它**

`badminton_bot/main.py` — 加入 import：

```python
from badminton_bot.utils.timing import sleep_then_spin
```

把整個 `count_down` 換成：

```python
def count_down(booking_date: datetime, offset: timedelta = timedelta()) -> None:
    """Count down to the target time (booking_date plus offset), while always
    reporting the seconds remaining to booking_date itself.

    Args:
        booking_date (datetime): specified date to book the court
        offset (timedelta, optional): shifts the wait target relative to
            booking_date. Defaults to timedelta().
    """
    count_down_target_time = booking_date + offset

    def _report(_remaining_to_target: float) -> None:
        # 一律回報距離 booking_date 的秒數，而不是距離提前量之後的等待目標。
        # 用 total_seconds()：timedelta.seconds 遇到負值會捲成 ~86400。
        delta_seconds = int((booking_date - datetime.now()).total_seconds())
        if delta_seconds < 10 or delta_seconds % 5 == 0:
            logging.info("倒數 %d 秒", delta_seconds)

    sleep_then_spin(
        target_epoch=count_down_target_time.timestamp(), on_tick=_report
    )
```

- [ ] **Step 6: 既有的 count_down 測試必須續存**

Run: `pytest tests/test_main.py -v`
Expected: PASS — `TestCountDown` 三個測試全過（提前返回、負 offset、等到近未來目標）

- [ ] **Step 7: Commit**

```bash
git add badminton_bot/utils/timing.py badminton_bot/main.py tests/test_timing.py
git commit -m "fix: 倒數改為遠時 sleep 近時 busy-wait，並修正倒數 log 與負值捲繞"
```

---

### Task 2: 送出時刻計算

實作「讓請求**抵達**伺服器時剛過開放時刻」的核心公式，以及 θ 來源的取捨與安全 clamp。

**Files:**
- Modify: `badminton_bot/utils/timing.py`
- Test: `tests/test_timing.py`

**Interfaces:**
- Consumes: Task 1 的 `badminton_bot/utils/timing`
- Produces:
  - `MAX_AUTO_CORRECTION_SECONDS: float = 2.0`
  - `resolve_theta(theta_srv: float | None, uncertainty: float, theta_ntp: float | None, rtt_median: float) -> tuple[float, float, str]` → `(theta, margin, source)`，`source` 為 `"server"` / `"ntp"` / `"none"`
  - `compute_send_time(nominal_epoch: float, theta: float, rtt_median: float, margin: float, manual_offset_ms: int) -> tuple[float, bool]` → `(送出時刻, 是否通過 clamp)`
  - `plan_send_time(nominal_epoch: float, theta_srv: float | None, uncertainty: float, theta_ntp: float | None, rtt_median: float, manual_offset_ms: int) -> tuple[float, str, bool]` → `(送出時刻, 採用來源, 是否通過 clamp)`

- [ ] **Step 1: Write the failing test**

附加到 `tests/test_timing.py`：

```python
from badminton_bot.utils.timing import (
    MAX_AUTO_CORRECTION_SECONDS,
    compute_send_time,
    plan_send_time,
    resolve_theta,
)

NOMINAL = 1_800_000_000.0
RTT = 0.024


class TestResolveTheta:
    def test_prefers_the_measured_server_offset(self):
        theta, margin, source = resolve_theta(
            theta_srv=0.180, uncertainty=0.025, theta_ntp=0.004, rtt_median=RTT
        )
        assert (theta, source) == (0.180, "server")
        assert margin == 0.025

    def test_server_margin_is_the_measurement_uncertainty(self):
        """送早是硬失敗、送晚只是可能輸，兩邊不對稱，所以偏晚一個不確定度。"""
        _, margin, _ = resolve_theta(
            theta_srv=0.0, uncertainty=0.031, theta_ntp=None, rtt_median=RTT
        )
        assert margin == 0.031

    def test_falls_back_to_ntp_and_neutralises_the_rtt_compensation(self):
        """只有 NTP 時我們不知道伺服器鐘差，不能冒著提早送出的風險。"""
        theta, margin, source = resolve_theta(
            theta_srv=None, uncertainty=0.0, theta_ntp=0.004, rtt_median=RTT
        )
        assert (theta, source) == (0.004, "ntp")
        assert margin == RTT / 2

    def test_falls_back_to_zero_when_every_measurement_failed(self):
        theta, margin, source = resolve_theta(
            theta_srv=None, uncertainty=0.0, theta_ntp=None, rtt_median=RTT
        )
        assert (theta, source) == (0.0, "none")
        assert margin == RTT / 2


class TestComputeSendTime:
    def test_fires_early_by_half_the_round_trip(self):
        """目標是請求抵達的瞬間對齊開放時刻，不是送出的瞬間。"""
        send_at, ok = compute_send_time(
            nominal_epoch=NOMINAL,
            theta=0.0,
            rtt_median=RTT,
            margin=0.0,
            manual_offset_ms=0,
        )
        assert ok is True
        assert send_at == NOMINAL - RTT / 2

    def test_a_local_clock_running_fast_delays_the_send(self):
        """θ 為正代表本機比伺服器快，必須晚一點送才對得上伺服器的開放時刻。"""
        send_at, _ = compute_send_time(
            nominal_epoch=NOMINAL,
            theta=0.300,
            rtt_median=0.0,
            margin=0.0,
            manual_offset_ms=0,
        )
        assert send_at == NOMINAL + 0.300

    def test_manual_offset_is_applied_on_top_in_milliseconds(self):
        send_at, _ = compute_send_time(
            nominal_epoch=NOMINAL,
            theta=0.0,
            rtt_median=0.0,
            margin=0.0,
            manual_offset_ms=-50,
        )
        assert send_at == NOMINAL - 0.050

    def test_rejects_an_absurd_automatic_correction(self):
        send_at, ok = compute_send_time(
            nominal_epoch=NOMINAL,
            theta=9.0,
            rtt_median=RTT,
            margin=0.0,
            manual_offset_ms=0,
        )
        assert ok is False
        assert send_at == NOMINAL

    def test_the_manual_offset_survives_a_clamped_correction(self):
        """clamp 擋掉的是量測值，不是使用者的明確指令。"""
        send_at, ok = compute_send_time(
            nominal_epoch=NOMINAL,
            theta=9.0,
            rtt_median=RTT,
            margin=0.0,
            manual_offset_ms=100,
        )
        assert ok is False
        assert send_at == NOMINAL + 0.100

    def test_a_correction_exactly_on_the_limit_is_accepted(self):
        _, ok = compute_send_time(
            nominal_epoch=NOMINAL,
            theta=MAX_AUTO_CORRECTION_SECONDS,
            rtt_median=0.0,
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
            rtt_median=RTT,
            manual_offset_ms=0,
        )
        assert (source, ok) == ("server", True)
        assert send_at == NOMINAL + 0.120 - RTT / 2 + 0.020

    def test_a_clamped_server_measurement_falls_back_to_ntp(self):
        """量測爆掉時退回 NTP，而不是讓整場歪掉。"""
        send_at, source, ok = plan_send_time(
            nominal_epoch=NOMINAL,
            theta_srv=30.0,
            uncertainty=0.020,
            theta_ntp=0.004,
            rtt_median=RTT,
            manual_offset_ms=0,
        )
        assert (source, ok) == ("ntp", True)
        assert send_at == NOMINAL + 0.004
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_timing.py -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_theta'`

- [ ] **Step 3: Write minimal implementation**

附加到 `badminton_bot/utils/timing.py`：

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_timing.py -v`
Expected: PASS（全部通過）

- [ ] **Step 5: Commit**

```bash
git add badminton_bot/utils/timing.py tests/test_timing.py
git commit -m "feat: 加入送出時刻計算，補償 RTT/2 並對時鐘修正量設安全上限"
```

---

### Task 3: SNTP client

取得 θ_ntp（本機時鐘 vs 標準時間）。**只碰國家時間伺服器，完全不碰預約網站。**

**Files:**
- Create: `badminton_bot/utils/ntp_client.py`
- Test: `tests/test_ntp_client.py`

**Interfaces:**
- Consumes: 無
- Produces:
  - `NTP_SERVER: str = "time.stdtime.gov.tw"`
  - `parse_ntp_response(packet: bytes, t1: float, t4: float) -> tuple[float, float]` → `(theta, delay)`
  - `pick_best_sample(samples: list[tuple[float, float]]) -> float | None`
  - `query_clock_offset(server: str = NTP_SERVER, sample_count: int = 3, timeout: float = 3.0) -> float | None`

- [ ] **Step 1: Write the failing test**

`tests/test_ntp_client.py`：

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ntp_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'badminton_bot.utils.ntp_client'`

- [ ] **Step 3: Write minimal implementation**

`badminton_bot/utils/ntp_client.py`：

```python
"""A minimal SNTP client, so the countdown can be anchored to standard time.

Hand-rolled on top of socket and struct rather than pulling in ntplib, to keep
requirements.txt and the PyInstaller bundle untouched.

This only ever talks to a national time server. It never touches the booking site.
"""

import logging
import socket
import struct

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
    import time

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ntp_client.py -v`
Expected: PASS（9 passed）

- [ ] **Step 5: Commit**

```bash
git add badminton_bot/utils/ntp_client.py tests/test_ntp_client.py
git commit -m "feat: 加入自製 SNTP client 校正本機時鐘"
```

---

### Task 4: 伺服器時鐘二分逼近的數學核心

`Date` header 只有秒級精度，但**下一次探測的送出相位是我們可以挑的** ——
挑得對就能讓「Date 跳秒的分界」落在目前區間的中點，每次探測把不確定度砍半。

定義 `θ = 本機 epoch − 伺服器 epoch`（正值代表本機快）。

**Files:**
- Create: `badminton_bot/utils/server_clock.py`
- Test: `tests/test_server_clock.py`

**Interfaces:**
- Consumes: 無
- Produces:
  - `INITIAL_INTERVAL: tuple[float, float] = (-2.0, 2.0)`
  - `parse_http_date(value: str) -> float`
  - `constrain(t0: float, t3: float, server_date_epoch: float) -> tuple[float, float]`
  - `intersect(current: tuple[float, float], new: tuple[float, float]) -> tuple[float, float] | None`
  - `next_probe_phase(interval: tuple[float, float], earliest: float) -> float`

- [ ] **Step 1: Write the failing test**

`tests/test_server_clock.py`：

```python
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
        assert parse_http_date("Thu, 04 Sep 2025 16:00:00 GMT") == 1_756_996_800.0

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_server_clock.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'badminton_bot.utils.server_clock'`

- [ ] **Step 3: Write minimal implementation**

`badminton_bot/utils/server_clock.py`：

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_server_clock.py -v`
Expected: PASS（全部通過，含 4 個參數化的收斂測試）

- [ ] **Step 5: Commit**

```bash
git add badminton_bot/utils/server_clock.py tests/test_server_clock.py
git commit -m "feat: 加入以 Date header 對伺服器時鐘二分逼近的數學核心"
```

---

### Task 5: 探測驅動迴圈

把 Task 4 的數學接上真實的 HTTP 請求：排程、隨機抖動、樣本有效性、矛盾退場。

**Files:**
- Modify: `badminton_bot/utils/server_clock.py`
- Test: `tests/test_server_clock.py`

**Interfaces:**
- Consumes: Task 4 的 `constrain` / `intersect` / `next_probe_phase` / `parse_http_date` / `INITIAL_INTERVAL`
- Produces:
  - `MIN_PROBE_GAP_SECONDS: float = 12.0`、`MAX_PROBE_GAP_SECONDS: float = 25.0`、`PROBE_BUDGET: int = 8`
  - `ClockMeasurement` dataclass：`theta: float | None`、`uncertainty: float`、`rtt_samples: list[float]`、`probe_count: int`、`discarded_count: int`、`rtt_median` property
  - `is_sample_usable(headers: Mapping[str, str]) -> tuple[bool, str]`
  - `async measure_server_clock(session, url: str, deadline_epoch: float, budget: int = PROBE_BUDGET, rng: random.Random | None = None, min_gap: float = MIN_PROBE_GAP_SECONDS, max_gap: float = MAX_PROBE_GAP_SECONDS) -> ClockMeasurement`
  - `MAX_USEFUL_UNCERTAINTY_SECONDS: float = 0.15`

- [ ] **Step 1: Write the failing test**

附加到 `tests/test_server_clock.py`：

```python
import asyncio
import random
from email.utils import formatdate

from badminton_bot.utils.server_clock import (
    MAX_USEFUL_UNCERTAINTY_SECONDS,
    ClockMeasurement,
    is_sample_usable,
    measure_server_clock,
)


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_server_clock.py -v`
Expected: FAIL — `ImportError: cannot import name 'ClockMeasurement'`

- [ ] **Step 3: Write minimal implementation**

附加到 `badminton_bot/utils/server_clock.py`。先補 import：

```python
import asyncio
import logging
import random
import statistics
import time
from dataclasses import dataclass, field
from typing import Mapping
```

然後：

```python
# 探測間隔隨機抖動：固定間隔比人類規律太多，規律性本身就是機器人簽名。
# 抖動不影響二分搜尋的正確性 —— 受約束的只有小數相位，落在第幾秒完全自由。
MIN_PROBE_GAP_SECONDS = 12.0
MAX_PROBE_GAP_SECONDS = 25.0

# 七到八次就收斂到 RTT 量級，再多打沒有資訊增益。
PROBE_BUDGET = 8

# 連續兩次證據矛盾就代表前提站不住腳，放棄量測比硬撐出一個錯的答案好。
MAX_CONTRADICTIONS = 2

# 區間已經窄到這個程度就停手。
MIN_USEFUL_WIDTH_SECONDS = 0.02

# 沒收斂到這個程度就不值得採用：margin 會等於不確定度，
# 用一個 ±0.5 秒的估計反而保證送晚半秒，比不修正還糟。
MAX_USEFUL_UNCERTAINTY_SECONDS = 0.15


@dataclass
class ClockMeasurement:
    """The outcome of a server-clock measurement run."""

    theta: float | None
    uncertainty: float
    rtt_samples: list[float] = field(default_factory=list)
    probe_count: int = 0
    discarded_count: int = 0

    @property
    def rtt_median(self) -> float:
        """Median round-trip time in seconds, or 0.0 with no samples."""
        if not self.rtt_samples:
            return 0.0

        return statistics.median(self.rtt_samples)


def is_sample_usable(headers: Mapping[str, str]) -> tuple[bool, str]:
    """Decide whether a response's Date can be trusted for clock measurement.

    A cached response carries a stale Date, which would drag the estimate off
    by however long it sat in the cache.

    Args:
        headers (Mapping[str, str]): the response headers.

    Returns:
        tuple[bool, str]: (usable, reason when not usable).
    """
    if "Date" not in headers:
        return False, "回應沒有 Date header"

    age = headers.get("Age")
    if age is not None:
        try:
            if int(age) > 0:
                return False, f"回應來自快取（Age={age}）"
        except ValueError:
            return False, f"無法解析的 Age header：{age!r}"

    x_cache = headers.get("X-Cache", "")
    if "HIT" in x_cache.upper():
        return False, f"回應來自快取（X-Cache={x_cache}）"

    return True, ""


def _log_infrastructure(headers: Mapping[str, str]) -> None:
    """Record what generated the Date, since a proxy's clock is not the app's."""
    logging.info(
        "伺服器資訊：Server=%s Via=%s X-Powered-By=%s",
        headers.get("Server", "（無）"),
        headers.get("Via", "（無）"),
        headers.get("X-Powered-By", "（無）"),
    )
    if headers.get("Via"):
        logging.warning(
            "偵測到反向代理，Date 可能來自代理而非應用伺服器，量到的鐘差僅供參考"
        )


async def measure_server_clock(
    session,
    url: str,
    deadline_epoch: float,
    budget: int = PROBE_BUDGET,
    rng: random.Random | None = None,
    min_gap: float = MIN_PROBE_GAP_SECONDS,
    max_gap: float = MAX_PROBE_GAP_SECONDS,
) -> ClockMeasurement:
    """Binary-search the booking server's clock offset via its Date header.

    Degrades to a theta of None on any failure — losing the booking to a
    measurement problem would be far worse than firing with an uncorrected clock.

    Args:
        session: an aiohttp-style session exposing get(url) as an async context manager.
        url (str): a read-only page to probe.
        deadline_epoch (float): stop before this instant.
        budget (int, optional): maximum probes. Defaults to PROBE_BUDGET.
        rng (random.Random | None, optional): source of interval jitter.
        min_gap (float, optional): shortest gap between probes, seconds.
        max_gap (float, optional): longest gap between probes, seconds. Tests
            pass tiny values here so the suite does not really wait minutes.

    Returns:
        ClockMeasurement: theta and its uncertainty, plus RTT samples.
    """
    rng = rng or random.Random()
    interval = INITIAL_INTERVAL
    rtt_samples: list[float] = []
    probe_count = 0
    discarded_count = 0
    contradictions = 0
    logged_infrastructure = False

    while probe_count < budget:
        gap = rng.uniform(min_gap, max_gap)
        send_at = next_probe_phase(interval=interval, earliest=time.time() + gap)
        if send_at >= deadline_epoch:
            break

        wait_for = send_at - time.time()
        if wait_for > 0:
            await asyncio.sleep(wait_for)

        try:
            t0 = time.time()
            async with session.get(url) as response:
                await response.text()
                t3 = time.time()
                headers = response.headers
        except Exception as error:
            logging.warning("時鐘探測請求失敗：%s", error)
            discarded_count += 1
            probe_count += 1
            continue

        probe_count += 1

        if not logged_infrastructure:
            _log_infrastructure(headers)
            logged_infrastructure = True

        usable, reason = is_sample_usable(headers)
        if not usable:
            logging.warning("捨棄時鐘探測樣本：%s", reason)
            discarded_count += 1
            continue

        try:
            server_date = parse_http_date(headers["Date"])
        except ValueError as error:
            logging.warning("捨棄時鐘探測樣本：%s", error)
            discarded_count += 1
            continue

        rtt_samples.append(t3 - t0)
        narrowed = intersect(interval, constrain(t0, t3, server_date))

        if narrowed is None:
            contradictions += 1
            logging.warning(
                "時鐘探測證據矛盾（第 %d 次），重新開始累積", contradictions
            )
            if contradictions >= MAX_CONTRADICTIONS:
                logging.warning("時鐘探測連續矛盾，放棄伺服器校時")
                return ClockMeasurement(
                    theta=None,
                    uncertainty=0.0,
                    rtt_samples=rtt_samples,
                    probe_count=probe_count,
                    discarded_count=discarded_count,
                )
            interval = constrain(t0, t3, server_date)
        else:
            interval = narrowed

        width = interval[1] - interval[0]
        logging.info(
            "時鐘探測 %d/%d：θ ∈ (%.3f, %.3f]，寬度 %.3f 秒",
            probe_count,
            budget,
            interval[0],
            interval[1],
            width,
        )

        if width <= max(2 * statistics.median(rtt_samples), MIN_USEFUL_WIDTH_SECONDS):
            break

    uncertainty = (interval[1] - interval[0]) / 2

    if (
        not rtt_samples
        or interval == INITIAL_INTERVAL
        or uncertainty > MAX_USEFUL_UNCERTAINTY_SECONDS
    ):
        if uncertainty > MAX_USEFUL_UNCERTAINTY_SECONDS and rtt_samples:
            logging.warning(
                "伺服器校時未收斂（±%.0f 毫秒），不予採用", uncertainty * 1000
            )
        return ClockMeasurement(
            theta=None,
            uncertainty=0.0,
            rtt_samples=rtt_samples,
            probe_count=probe_count,
            discarded_count=discarded_count,
        )

    return ClockMeasurement(
        theta=(interval[0] + interval[1]) / 2,
        uncertainty=uncertainty,
        rtt_samples=rtt_samples,
        probe_count=probe_count,
        discarded_count=discarded_count,
    )
```

**注意**：`asyncio.sleep` 的排程可能 overshoot 幾毫秒，相位因此不會落在完美的中點。
這**不會讓結果錯誤** —— `constrain` 用的是實際量到的 `t0` / `t3`，
只是收斂效率略降。不需要為此改用 busy-wait。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_server_clock.py -v`
Expected: PASS（全部通過）

> 測試一律傳 `min_gap=0.001, max_gap=0.002`，所以真實的 `asyncio.sleep` 只睡幾毫秒，
> 而 `time.time()` 在驅動迴圈與 `FakeSession` 兩邊仍是同一個時鐘 —— 相位邏輯照常成立，
> 整個收斂流程跑完不到一秒。**不要**改用假時鐘去 monkeypatch `asyncio.sleep`，
> 那會讓 `constrain` 讀到的 `t0` 與排程時刻對不上。

- [ ] **Step 5: Commit**

```bash
git add badminton_bot/utils/server_clock.py tests/test_server_clock.py
git commit -m "feat: 加入伺服器時鐘探測驅動迴圈，含隨機抖動與矛盾退場"
```

---

### Task 6: 服務層 —— `target_qpid` 契約與唯讀列表頁 URL

把寫死在 URL 字串裡的 QPid 提升為類別屬性（換場地變成改一行），
並新增探測、預熱、狀態偵察都要用到的唯讀列表頁 URL。

**Files:**
- Modify: `badminton_bot/services/sports_center_webservice.py`
- Modify: `badminton_bot/services/zhongzheng_sports_center_webservice.py`
- Modify: `badminton_bot/services/zhongshan_sports_center_webservice.py`
- Test: `tests/test_sports_center_webservice.py`

**Interfaces:**
- Consumes: 無
- Produces:
  - `SportsCenterWebService.target_qpid: int`（新增必要類別屬性）
  - `_generate_list_page_url(self, year: int, month: int, day: int) -> str`（新增抽象方法）
  - `_generate_warm_up_urls(self, year: int, month: int, day: int) -> tuple[str, ...]`（ABC 具體實作，回傳列表頁與登入頁）

- [ ] **Step 1: Write the failing test**

附加到 `tests/test_sports_center_webservice.py`（沿用該檔既有的 `build_without_browser`）：

```python
class TestTargetQpidContract:
    def test_both_centres_declare_their_target_court(self):
        assert ZhongzhengSportsCenterWebService.target_qpid == 1199
        assert ZhongshanSportsCenterWebService.target_qpid == 84

    def test_a_subclass_without_target_qpid_is_rejected_at_import_time(self):
        """__init_subclass__ 在類別定義時就擋下來，而不是等到執行期才炸。"""
        with pytest.raises(TypeError, match="target_qpid"):

            class Incomplete(SportsCenterWebService):
                sport_center_name = "測試中心"
                login_page_url = "https://example.invalid/login"
                booking_window_days = 7

    def test_the_booking_url_is_built_from_the_class_attribute(self):
        """換場地應該只要改 target_qpid 一行。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        url = service._generate_booking_url(year=2026, month=9, day=17, hour=20)
        assert f"QPid={ZhongzhengSportsCenterWebService.target_qpid}" in url


class TestListPageUrl:
    def test_zhongzheng_list_page_is_the_read_only_step_flag(self):
        """StepFlag=2 是列表頁，StepFlag=25 才會真的送出預約 —— 探測絕不能碰後者。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        url = service._generate_list_page_url(year=2026, month=9, day=17)
        assert url == (
            "https://bwd.xuanen.com.tw/wd27.aspx?module=net_booking"
            "&files=booking_place&StepFlag=2&PT=1&D=2026/09/17"
        )
        assert "StepFlag=25" not in url

    def test_zhongshan_list_page_is_the_read_only_step_flag(self):
        service = build_without_browser(ZhongshanSportsCenterWebService)
        url = service._generate_list_page_url(year=2026, month=9, day=17)
        assert url == (
            "https://scr.cyc.org.tw/tp01.aspx?module=net_booking"
            "&files=booking_place&StepFlag=2&PT=1&D=2026/09/17"
        )
        assert "StepFlag=25" not in url

    def test_the_date_is_zero_padded(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        url = service._generate_list_page_url(year=2026, month=1, day=5)
        assert "D=2026/01/05" in url


class TestWarmUpUrls:
    def test_warms_two_different_pages(self):
        """兩條熱連線供兩個預約請求各用一條；兩個不同頁面併發載入是一般瀏覽行為。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        urls = service._generate_warm_up_urls(year=2026, month=9, day=17)
        assert len(urls) == 2
        assert len(set(urls)) == 2

    def test_warm_up_never_touches_the_booking_action(self):
        for cls in (ZhongzhengSportsCenterWebService, ZhongshanSportsCenterWebService):
            service = build_without_browser(cls)
            for url in service._generate_warm_up_urls(year=2026, month=9, day=17):
                assert "StepFlag=25" not in url

    def test_includes_the_list_page_and_the_login_page(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        urls = service._generate_warm_up_urls(year=2026, month=9, day=17)
        assert service._generate_list_page_url(2026, 9, 17) in urls
        assert ZhongzhengSportsCenterWebService.login_page_url in urls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sports_center_webservice.py -v`
Expected: FAIL — `AttributeError: type object 'ZhongzhengSportsCenterWebService' has no attribute 'target_qpid'`

- [ ] **Step 3: Write minimal implementation**

`badminton_bot/services/sports_center_webservice.py` — 類別屬性宣告加一行、
`required_attrs` 加一項，並新增兩個方法：

```python
class SportsCenterWebService(ABC):
    sport_center_name: str
    login_page_url: str
    booking_window_days: int
    target_qpid: int
```

```python
        required_attrs = [
            "sport_center_name",
            "login_page_url",
            "booking_window_days",
            "target_qpid",
        ]
```

```python
    @abstractmethod
    def _generate_list_page_url(self, year: int, month: int, day: int) -> str:
        """唯讀的場地列表頁（StepFlag=2）。探測、預熱與狀態偵察都用這一頁。"""

    def _generate_warm_up_urls(
        self, year: int, month: int, day: int
    ) -> tuple[str, ...]:
        """Two read-only pages to open concurrently, leaving two hot connections.

        Two connections because the two booking requests each need one. Two
        *different* pages because a browser loading a page in parallel is
        ordinary traffic, whereas the same URL fetched twice at once is not.

        Returns:
            tuple[str, ...]: the list page and the login page.
        """
        return (
            self._generate_list_page_url(year=year, month=month, day=day),
            type(self).login_page_url,
        )
```

`zhongzheng_sports_center_webservice.py`：

```python
    booking_window_days = 7
    target_qpid = 1199  # 羽球 7-4。換場地只要改這一行。
```

```python
    def _generate_list_page_url(self, year: int, month: int, day: int) -> str:
        # StepFlag=2 是唯讀列表頁，StepFlag=25 才是真的送出預約
        return (
            f"https://bwd.xuanen.com.tw/wd27.aspx?module=net_booking"
            f"&files=booking_place&StepFlag=2&PT=1"
            f"&D={year}/{str(month).zfill(2)}/{str(day).zfill(2)}"
        )

    def _generate_booking_url(self, year: int, month: int, day: int, hour: int) -> str:
        # 產生搶場地 url。中正的 QTime 不補零
        return (
            f"https://bwd.xuanen.com.tw/wd27.aspx?module=net_booking"
            f"&files=booking_place&StepFlag=25&QPid={type(self).target_qpid}"
            f"&QTime={str(hour)}&PT=1"
            f"&D={year}/{str(month).zfill(2)}/{str(day).zfill(2)}"
        )
```

`zhongshan_sports_center_webservice.py`：

```python
    booking_window_days = 14
    target_qpid = 84
```

```python
    def _generate_list_page_url(self, year: int, month: int, day: int) -> str:
        # StepFlag=2 是唯讀列表頁，StepFlag=25 才是真的送出預約。
        # 注意：這個網址是依中正（同平台）的格式推論出來的，尚未在中山實地驗證。
        return (
            f"https://scr.cyc.org.tw/tp01.aspx?module=net_booking"
            f"&files=booking_place&StepFlag=2&PT=1"
            f"&D={year}/{str(month).zfill(2)}/{str(day).zfill(2)}"
        )

    def _generate_booking_url(self, year: int, month: int, day: int, hour: int) -> str:
        # 產生搶場地 url。中山的 QTime 要補零
        return (
            f"https://scr.cyc.org.tw/tp01.aspx?module=net_booking"
            f"&files=booking_place&StepFlag=25&QPid={type(self).target_qpid}"
            f"&QTime={str(hour).zfill(2)}&PT=1"
            f"&D={year}/{str(month).zfill(2)}/{str(day).zfill(2)}"
        )
```

- [ ] **Step 4: 把新屬性納入既有的契約測試**

`tests/test_sports_center_webservice.py` 既有的
`TestSubclassContract.test_shipped_services_declare_every_required_attribute`
把必要屬性寫死成三個，補上第四個：

```python
        for attr in (
            "sport_center_name",
            "login_page_url",
            "booking_window_days",
            "target_qpid",
        ):
            assert attr in service_cls.__dict__
```

同檔的 `test_subclass_missing_a_required_attribute_fails_at_definition_time`
不需要改：它省略的是 `booking_window_days`，而該項在 `required_attrs` 中排在
`target_qpid` 之前，所以拋出的訊息仍然是 `booking_window_days`。

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_sports_center_webservice.py -v`
Expected: PASS（含既有的 `_generate_booking_url` 測試 —— **URL 字串必須完全沒變**，
QPid 只是換了來源，中山補零、中正不補零的差異也必須保留）

- [ ] **Step 6: Run the whole suite**

Run: `pytest`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add badminton_bot/services tests/test_sports_center_webservice.py
git commit -m "refactor: QPid 提升為類別屬性並加入唯讀列表頁與預熱網址"
```

---

### Task 7: 目標格子狀態判讀

回答「我是輸幾毫秒，還是那格根本沒開放給我」—— 這是唯一能推翻整個方案前提的檢查。

**Files:**
- Modify: `badminton_bot/services/sports_center_webservice.py`
- Test: `tests/test_sports_center_webservice.py`

**Interfaces:**
- Consumes: Task 6 的 `target_qpid`
- Produces:
  - `SLOT_AVAILABLE: str = "可訂"`、`SLOT_TAKEN: str = "已訂"`、`SLOT_UNKNOWN: str = "未知"`
  - `parse_slot_state(self, html: str, hour: int) -> str`（ABC 具體實作，兩個中心共用）

**背景**：可預約的格子其 `onclick` 會寫出網站自己的呼叫 `Step3Action(QPid, QTime)`
（例：`Step3Action(1196, 6)`）；已被預約的格子沒有這個呼叫。
兩個中心是同一套 ASP.NET 平台，所以解析邏輯放在 ABC 共用。

**重要**：下面的 HTML fixture 是**依既有筆記合成的，不是真實網頁**。
因此解析必須保守，且 log 要留下足以事後修正的原始線索。

- [ ] **Step 1: Write the failing test**

附加到 `tests/test_sports_center_webservice.py`：

```python
from badminton_bot.services.sports_center_webservice import (
    SLOT_AVAILABLE,
    SLOT_TAKEN,
    SLOT_UNKNOWN,
)

# 合成的 fixture，依照既有筆記寫成：可預約的格子帶 Step3Action 呼叫，
# 已被預約的格子只有 place02 圖與 title。尚未與真實網頁核對過。
LIST_PAGE_HTML = """
<table>
  <tr>
    <td><a onclick="Step3Action(1199, 19)"><img src="img/place01.png"></a></td>
    <td><img src="img/place02.png" title="已被預約"></td>
    <td><a onclick="Step3Action(1199, 21)"><img src="img/place01.png"></a></td>
    <td><a onclick="Step3Action(1196, 20)"><img src="img/place01.png"></a></td>
  </tr>
</table>
"""

LIST_PAGE_HTML_PADDED_HOUR = """
<td><a onclick="Step3Action(84, 09)"><img src="img/place01.png"></a></td>
"""


class TestParseSlotState:
    def test_an_open_slot_is_reported_available(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        assert service.parse_slot_state(LIST_PAGE_HTML, hour=19) == SLOT_AVAILABLE

    def test_a_slot_without_its_click_handler_is_reported_taken(self):
        """20:00 那格的 Step3Action 呼叫不見了 —— 被別人訂走了。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        assert service.parse_slot_state(LIST_PAGE_HTML, hour=20) == SLOT_TAKEN

    def test_another_court_at_the_same_hour_does_not_count(self):
        """1196 的 20:00 還空著，但我們要的是 1199。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        assert service.parse_slot_state(LIST_PAGE_HTML, hour=20) == SLOT_TAKEN

    def test_a_page_that_never_mentions_our_court_is_unknown(self):
        """預約窗口還沒開時列表頁可能根本不渲染那一天 —— 那是未知，不是已訂。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        assert service.parse_slot_state("<html>查無資料</html>", hour=20) == SLOT_UNKNOWN

    def test_tolerates_a_zero_padded_hour(self):
        """中山的 QTime 補零、中正不補，解析不該依賴這個差異。"""
        service = build_without_browser(ZhongshanSportsCenterWebService)
        assert (
            service.parse_slot_state(LIST_PAGE_HTML_PADDED_HOUR, hour=9)
            == SLOT_AVAILABLE
        )

    def test_tolerates_whitespace_in_the_call(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        html = '<a onclick="Step3Action( 1199 , 20 )">'
        assert service.parse_slot_state(html, hour=20) == SLOT_AVAILABLE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sports_center_webservice.py -v`
Expected: FAIL — `ImportError: cannot import name 'SLOT_AVAILABLE'`

- [ ] **Step 3: Write minimal implementation**

`badminton_bot/services/sports_center_webservice.py` — 加 `import re`，並在模組層級：

```python
SLOT_AVAILABLE = "可訂"
SLOT_TAKEN = "已訂"
SLOT_UNKNOWN = "未知"
```

在 `SportsCenterWebService` 中新增：

```python
    def parse_slot_state(self, html: str, hour: int) -> str:
        """Read the target court's state for one hour off the read-only list page.

        A bookable cell carries the site's own click handler, Step3Action(QPid,
        QTime); a cell already taken does not. Both centres run the same ASP.NET
        platform, so one parser serves them both. The hour is matched with an
        optional leading zero because the two sites pad it differently.

        The distinction that matters is between "taken" and "unknown": before
        the booking window opens the page may not render that day at all, and
        reporting that as taken would be a lie.

        Args:
            html (str): the list page body.
            hour (int): the hour to look up.

        Returns:
            str: SLOT_AVAILABLE, SLOT_TAKEN or SLOT_UNKNOWN.
        """
        qpid = type(self).target_qpid
        bookable = re.compile(
            rf"Step3Action\(\s*{qpid}\s*,\s*0?{hour}\s*\)"
        )

        if bookable.search(html):
            return SLOT_AVAILABLE

        if str(qpid) in html:
            return SLOT_TAKEN

        return SLOT_UNKNOWN
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sports_center_webservice.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add badminton_bot/services/sports_center_webservice.py tests/test_sports_center_webservice.py
git commit -m "feat: 加入唯讀列表頁的目標場地狀態判讀"
```

---

### Task 8: 瀏覽器外觀 —— UA 常數、headers，與 Chrome 參數修正

目前 aiohttp 送出的 User-Agent 是 `Python/3.11 aiohttp/3.11.13`，網管掃 log 一眼就看得出來。
同時修掉 Selenium 那行根本沒生效的 UA 設定。

**Files:**
- Modify: `badminton_bot/services/sports_center_webservice.py`
- Test: `tests/test_sports_center_webservice.py`

**Interfaces:**
- Consumes: Task 6 的 `_generate_list_page_url`
- Produces:
  - `BROWSER_USER_AGENT: str`
  - `build_browser_headers(referer: str) -> dict[str, str]`（模組層級函式）

- [ ] **Step 1: Write the failing test**

附加到 `tests/test_sports_center_webservice.py`：

```python
from badminton_bot.services.sports_center_webservice import (
    BROWSER_USER_AGENT,
    build_browser_headers,
)


class TestBrowserHeaders:
    def test_the_user_agent_is_not_the_aiohttp_default(self):
        """預設的 Python/aiohttp UA 等於在 log 裡自報身分。"""
        assert "aiohttp" not in BROWSER_USER_AGENT
        assert "Python" not in BROWSER_USER_AGENT
        assert BROWSER_USER_AGENT.startswith("Mozilla/5.0")

    def test_headers_carry_the_referer_they_were_given(self):
        headers = build_browser_headers(referer="https://example.invalid/list")
        assert headers["Referer"] == "https://example.invalid/list"

    def test_headers_ask_for_traditional_chinese(self):
        headers = build_browser_headers(referer="https://example.invalid/list")
        assert headers["Accept-Language"].startswith("zh-TW")

    def test_headers_use_the_shared_user_agent(self):
        headers = build_browser_headers(referer="https://example.invalid/list")
        assert headers["User-Agent"] == BROWSER_USER_AGENT


class TestChromeOptions:
    def test_the_user_agent_argument_uses_the_flag_chrome_understands(self):
        """原本直接把 UA 字串當參數丟進去，Chrome 根本不會理它。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        arguments = service.get_default_chrome_options().arguments
        assert f"--user-agent={BROWSER_USER_AGENT}" in arguments

    def test_selenium_and_aiohttp_cannot_drift_apart(self):
        """兩邊共用同一個常數，否則登入與搶場地會用不同身分。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        arguments = service.get_default_chrome_options().arguments
        headers = build_browser_headers(referer="https://example.invalid/list")
        assert any(headers["User-Agent"] in argument for argument in arguments)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sports_center_webservice.py -v`
Expected: FAIL — `ImportError: cannot import name 'BROWSER_USER_AGENT'`

- [ ] **Step 3: Write minimal implementation**

`badminton_bot/services/sports_center_webservice.py` 模組層級：

```python
# Selenium 與 aiohttp 共用同一個 UA，否則登入與搶場地會用不同身分出現在對方的 log 裡。
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


def build_browser_headers(referer: str) -> dict[str, str]:
    """Assemble the headers an ordinary browser would send on this navigation.

    aiohttp's default User-Agent announces itself as a Python script. Note the
    trade-off: the TLS fingerprint and header order still say Python, so on a
    site with fingerprinting this is a claim that can be caught out. On a local
    sports centre's ASP.NET system that is very unlikely, and looking ordinary
    in the access log is worth more.

    Args:
        referer (str): the page this request would have been clicked from.

    Returns:
        dict[str, str]: headers for the aiohttp session.
    """
    return {
        "User-Agent": BROWSER_USER_AGENT,
        "Referer": referer,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }
```

`get_default_chrome_options` 裡把那一行換掉：

```python
        # 模擬真實瀏覽器。必須用 --user-agent= 這個旗標，
        # 直接丟裸字串進去 Chrome 不會當成 UA 設定。
        options.add_argument(f"--user-agent={BROWSER_USER_AGENT}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sports_center_webservice.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add badminton_bot/services/sports_center_webservice.py tests/test_sports_center_webservice.py
git commit -m "fix: 共用瀏覽器 UA 常數並修正 Chrome user-agent 參數寫法"
```

---

### Task 9: session 建構、連線預熱與觸發機制

消除開搶瞬間的 DNS/TCP/TLS 握手，並讓兩個請求在截止時刻之前就完全備妥。

**Files:**
- Modify: `badminton_bot/services/sports_center_webservice.py`
- Test: `tests/test_sports_center_webservice.py`

**Interfaces:**
- Consumes: Task 6 的 `_generate_warm_up_urls`、Task 8 的 `build_browser_headers`
- Produces:
  - `WarmUpResult` dataclass：`url`、`ok`、`connection`、`keep_alive`、`body`
  - `BookingAttempt` dataclass：`hour`、`url`、`sent_epoch`、`rtt`、`server_date`、`success`、`error`
  - `create_session(self, cookies: dict[str, str], referer: str) -> aiohttp.ClientSession`
  - `async warm_up(self, session, year, month, day) -> list[WarmUpResult]`
  - `async booking_courts(self, session, booking_url: str, hour: int, ready_event: asyncio.Event) -> BookingAttempt`

**注意**：`booking_courts` 的簽章改變了。舊簽章
（`session, year, month, day, hour`）的呼叫端只有 `main.py`，Task 10 會一併更新。

- [ ] **Step 1: Write the failing test**

附加到 `tests/test_sports_center_webservice.py`：

```python
import asyncio

from badminton_bot.services.sports_center_webservice import (
    BookingAttempt,
    WarmUpResult,
)


class FakeBookingResponse:
    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self.body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def text(self):
        return self.body


class RecordingSession:
    def __init__(self, body: str = "PT=1&X=1", headers=None):
        self.body = body
        self.headers = headers or {}
        self.requested_urls: list[str] = []
        self.request_epochs: list[float] = []

    def get(self, url, **kwargs):
        import time

        self.requested_urls.append(url)
        self.request_epochs.append(time.time())
        return FakeBookingResponse(self.body, self.headers)


class TestBookingCourts:
    def test_sends_the_url_it_was_handed(self):
        """URL 在截止時刻之前就組好，開搶那一刻不做字串運算。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession()

        async def scenario():
            ready = asyncio.Event()
            ready.set()
            return await service.booking_courts(
                session=session,
                booking_url="https://example.invalid/book",
                hour=20,
                ready_event=ready,
            )

        attempt = asyncio.run(scenario())
        assert session.requested_urls == ["https://example.invalid/book"]
        assert attempt.success is True
        assert attempt.hour == 20

    def test_waits_for_the_event_before_sending(self):
        """task 要在倒數結束前就掛好，時間到只做 event.set()。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession()

        async def scenario():
            ready = asyncio.Event()
            task = asyncio.create_task(
                service.booking_courts(
                    session=session,
                    booking_url="https://example.invalid/book",
                    hour=20,
                    ready_event=ready,
                )
            )
            await asyncio.sleep(0.05)
            assert session.requested_urls == [], "還沒放行就送出了"
            ready.set()
            return await task

        asyncio.run(scenario())
        assert session.requested_urls == ["https://example.invalid/book"]

    def test_reports_a_lost_race_without_raising(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession(body="PT=1&X=2")

        async def scenario():
            ready = asyncio.Event()
            ready.set()
            return await service.booking_courts(
                session=session,
                booking_url="https://example.invalid/book",
                hour=20,
                ready_event=ready,
            )

        attempt = asyncio.run(scenario())
        assert attempt.success is False
        assert attempt.error is None

    def test_an_unrecognised_response_is_recorded_not_raised(self):
        """一發失敗絕不能連坐另一發，所以例外要收在結果物件裡。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession(body="尚未開放預約")

        async def scenario():
            ready = asyncio.Event()
            ready.set()
            return await service.booking_courts(
                session=session,
                booking_url="https://example.invalid/book",
                hour=20,
                ready_event=ready,
            )

        attempt = asyncio.run(scenario())
        assert attempt.success is None
        assert attempt.error is not None

    def test_records_the_send_instant_and_round_trip(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession(headers={"Date": "Thu, 04 Sep 2025 16:00:00 GMT"})

        async def scenario():
            ready = asyncio.Event()
            ready.set()
            return await service.booking_courts(
                session=session,
                booking_url="https://example.invalid/book",
                hour=20,
                ready_event=ready,
            )

        attempt = asyncio.run(scenario())
        assert attempt.sent_epoch > 0
        assert attempt.rtt >= 0
        assert attempt.server_date == "Thu, 04 Sep 2025 16:00:00 GMT"


class TestWarmUp:
    def test_opens_both_pages_concurrently(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession(
            body="<html></html>", headers={"Connection": "keep-alive"}
        )

        results = asyncio.run(
            service.warm_up(session=session, year=2026, month=9, day=17)
        )
        assert len(results) == 2
        assert len(session.requested_urls) == 2
        assert all(result.ok for result in results)

    def test_reports_the_keep_alive_verdict(self):
        """若伺服器回 Connection: close，預熱就是白做，使用者必須看得見。"""
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession(
            body="<html></html>",
            headers={"Connection": "close", "Keep-Alive": "timeout=5"},
        )

        results = asyncio.run(
            service.warm_up(session=session, year=2026, month=9, day=17)
        )
        assert results[0].connection == "close"
        assert results[0].keep_alive == "timeout=5"

    def test_returns_the_list_page_body_for_slot_inspection(self):
        service = build_without_browser(ZhongzhengSportsCenterWebService)
        session = RecordingSession(body=LIST_PAGE_HTML)

        results = asyncio.run(
            service.warm_up(session=session, year=2026, month=9, day=17)
        )
        assert results[0].body == LIST_PAGE_HTML

    def test_a_failed_warm_up_degrades_instead_of_raising(self):
        """預熱失敗只是回到冷連線，不比現況差，絕不能中斷搶場地。"""

        class ExplodingSession:
            def get(self, url, **kwargs):
                raise OSError("模擬連線中斷")

        service = build_without_browser(ZhongzhengSportsCenterWebService)
        results = asyncio.run(
            service.warm_up(session=ExplodingSession(), year=2026, month=9, day=17)
        )
        assert all(result.ok is False for result in results)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sports_center_webservice.py -v`
Expected: FAIL — `ImportError: cannot import name 'BookingAttempt'`

- [ ] **Step 3: Write minimal implementation**

`badminton_bot/services/sports_center_webservice.py` — 補 import：

```python
import asyncio
import time
from dataclasses import dataclass
```

模組層級的 connector 設定與資料類別：

```python
# 連線池上限。只需要兩條熱連線，留餘裕給預熱與探測。
CONNECTION_LIMIT = 10

# DNS 快取存活秒數，讓開搶那一刻不必再解析一次。
DNS_CACHE_SECONDS = 300

# aiohttp 預設 15 秒，短於探測階段的隨機間隔（12~25 秒）。沿用預設值的話，
# 連線會在兩次探測之間被回收，每次探測都得重新握手 —— 量到的 RTT 會被
# 握手成本汙染，無法代表開搶時那條熱連線，連帶讓 RTT/2 的補償失準。
KEEPALIVE_TIMEOUT_SECONDS = 60


@dataclass
class WarmUpResult:
    """What one warm-up request revealed about the connection."""

    url: str
    ok: bool
    connection: str | None = None
    keep_alive: str | None = None
    body: str | None = None


@dataclass
class BookingAttempt:
    """One booking request, with the timings needed to tune the next run."""

    hour: int
    url: str
    sent_epoch: float
    rtt: float
    server_date: str | None
    success: bool | None
    error: str | None
```

在 `SportsCenterWebService` 中新增三個方法，並**取代**既有的 `booking_courts`：

```python
    def create_session(
        self, cookies: dict[str, str], referer: str
    ) -> aiohttp.ClientSession:
        """Build the session the race runs on.

        An explicit connector is what makes warming up worthwhile: the default
        one would drop the pooled connection between probes.

        Args:
            cookies (dict[str, str]): the authenticated cookies from Selenium.
            referer (str): the page the booking requests would be clicked from.

        Returns:
            aiohttp.ClientSession: a session with pooled, browser-looking requests.
        """
        connector = aiohttp.TCPConnector(
            limit=CONNECTION_LIMIT,
            ttl_dns_cache=DNS_CACHE_SECONDS,
            keepalive_timeout=KEEPALIVE_TIMEOUT_SECONDS,
            ssl=False,
        )

        return aiohttp.ClientSession(
            connector=connector,
            cookies=cookies,
            headers=build_browser_headers(referer=referer),
        )

    async def warm_up(
        self, session, year: int, month: int, day: int
    ) -> list[WarmUpResult]:
        """Open two read-only pages so two hot connections wait in the pool.

        Everything the booking request would otherwise pay for at the opening
        instant — DNS, the TCP handshake, the TLS handshake — happens here.

        Args:
            session: the aiohttp session.
            year (int): the target booking year.
            month (int): the target booking month.
            day (int): the target booking day.

        Returns:
            list[WarmUpResult]: one entry per warm-up URL, in URL order.
        """
        urls = self._generate_warm_up_urls(year=year, month=month, day=day)

        async def _open(url: str) -> WarmUpResult:
            try:
                async with session.get(url) as response:
                    body = await response.text()
                    return WarmUpResult(
                        url=url,
                        ok=True,
                        connection=response.headers.get("Connection"),
                        keep_alive=response.headers.get("Keep-Alive"),
                        body=body,
                    )
            except Exception as error:
                logging.warning("預熱請求失敗（%s）：%s", url, error)
                return WarmUpResult(url=url, ok=False)

        results = await asyncio.gather(*(_open(url) for url in urls))

        for result in results:
            if result.ok:
                logging.info(
                    "預熱完成：Connection=%s Keep-Alive=%s",
                    result.connection or "（無）",
                    result.keep_alive or "（無）",
                )
                if (result.connection or "").lower() == "close":
                    logging.warning(
                        "伺服器要求關閉連線，預熱無效，開搶時仍需重新握手"
                    )

        return list(results)

    async def booking_courts(
        self,
        session,
        booking_url: str,
        hour: int,
        ready_event: asyncio.Event,
    ) -> BookingAttempt:
        """Fire one pre-built booking request the moment the event is set.

        The URL is built and this coroutine is scheduled well before the
        deadline, so the only work left at the opening instant is the send.

        Never raises. An unrecognised response is recorded in the result rather
        than thrown, because one request blowing up must not take the other
        booking down with it.

        Args:
            session: the aiohttp session.
            booking_url (str): the URL assembled ahead of time.
            hour (int): the hour being booked, for logging.
            ready_event (asyncio.Event): released at the send instant.

        Returns:
            BookingAttempt: the outcome and its timings.
        """
        await ready_event.wait()

        sent_epoch = time.time()
        started = time.perf_counter()

        try:
            async with session.get(booking_url) as response:
                text = await response.text()
                rtt = time.perf_counter() - started
                server_date = response.headers.get("Date")
        except Exception as error:
            logging.error("%d 點的場地請求失敗：%s", hour, error)
            return BookingAttempt(
                hour=hour,
                url=booking_url,
                sent_epoch=sent_epoch,
                rtt=time.perf_counter() - started,
                server_date=None,
                success=None,
                error=str(error),
            )

        try:
            success = self._is_booking_success(text=text)
            error = None
        except RuntimeError as runtime_error:
            success = None
            error = str(runtime_error)
            logging.error("%d 點的場地回應無法判讀：%s", hour, runtime_error)

        if success is True:
            logging.info("%d ~ %d 點的場地預約成功", hour, hour + 1)
        elif success is False:
            logging.info("%d ~ %d 點的場地預約失敗", hour, hour + 1)

        return BookingAttempt(
            hour=hour,
            url=booking_url,
            sent_epoch=sent_epoch,
            rtt=rtt,
            server_date=server_date,
            success=success,
            error=error,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sports_center_webservice.py -v`
Expected: PASS

> `pytest tests/test_main.py` 此時會失敗，因為 `main.py` 還在用舊的 `booking_courts`
> 簽章。Task 10 會修好。

- [ ] **Step 5: Commit**

```bash
git add badminton_bot/services/sports_center_webservice.py tests/test_sports_center_webservice.py
git commit -m "feat: 加入連線預熱與預先掛載的搶場地觸發機制"
```

---

### Task 10: main.py 編排

把所有零件接起來，並修掉「一發例外連坐另一發」這個既有缺陷。

**Files:**
- Modify: `badminton_bot/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: Task 1-9 的全部產出
- Produces:
  - `PROBE_DEADLINE_OFFSET: timedelta = timedelta(seconds=-30)`
  - `WARM_UP_OFFSET: timedelta = timedelta(seconds=-10)`
  - `POST_CHECK_DELAY_SECONDS: float = 2.0`
  - `async run_booking_race(service, session, booking_periods, send_at_epoch) -> list[BookingAttempt]`

- [ ] **Step 1: Write the failing test**

附加到 `tests/test_main.py`：

```python
import asyncio
import time

from badminton_bot.main import run_booking_race
from badminton_bot.services.sports_center_webservice import BookingAttempt


class OneGoodOneBadSession:
    """Succeeds on the first URL and blows up on the second."""

    def __init__(self):
        self.requested_urls = []

    def get(self, url, **kwargs):
        self.requested_urls.append(url)
        if "QTime=21" in url:
            raise OSError("模擬第二發連線中斷")
        return FakeBookingResponse("PT=1&X=1")


class FakeBookingResponse:
    def __init__(self, body):
        self.body = body
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def text(self):
        return self.body


class TestRunBookingRace:
    def test_one_failing_request_does_not_take_down_the_other(self):
        """既有的 gather 會把例外往外傳，20 點那發炸掉會連 21 點一起收掉。"""
        from badminton_bot.services.zhongzheng_sports_center_webservice import (
            ZhongzhengSportsCenterWebService,
        )

        service = object.__new__(ZhongzhengSportsCenterWebService)
        session = OneGoodOneBadSession()
        periods = (
            datetime(2026, 9, 17, 20),
            datetime(2026, 9, 17, 21),
        )

        attempts = asyncio.run(
            run_booking_race(
                service=service,
                session=session,
                booking_periods=periods,
                send_at_epoch=time.time(),
            )
        )

        assert len(attempts) == 2
        assert attempts[0].success is True
        assert attempts[1].error is not None
        assert len(session.requested_urls) == 2

    def test_both_requests_go_out_after_the_send_instant(self):
        from badminton_bot.services.zhongzheng_sports_center_webservice import (
            ZhongzhengSportsCenterWebService,
        )

        service = object.__new__(ZhongzhengSportsCenterWebService)
        session = OneGoodOneBadSession()
        send_at = time.time() + 0.15
        periods = (datetime(2026, 9, 17, 20),)

        attempts = asyncio.run(
            run_booking_race(
                service=service,
                session=session,
                booking_periods=periods,
                send_at_epoch=send_at,
            )
        )

        assert attempts[0].sent_epoch >= send_at

    def test_returns_one_attempt_per_period(self):
        from badminton_bot.services.zhongzheng_sports_center_webservice import (
            ZhongzhengSportsCenterWebService,
        )

        service = object.__new__(ZhongzhengSportsCenterWebService)
        session = OneGoodOneBadSession()
        periods = (
            datetime(2026, 9, 17, 20),
            datetime(2026, 9, 17, 21),
        )

        attempts = asyncio.run(
            run_booking_race(
                service=service,
                session=session,
                booking_periods=periods,
                send_at_epoch=time.time(),
            )
        )
        assert [attempt.hour for attempt in attempts] == [20, 21]
        assert all(isinstance(attempt, BookingAttempt) for attempt in attempts)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_main.py -v`
Expected: FAIL — `ImportError: cannot import name 'run_booking_race'`

- [ ] **Step 3: Write the race runner**

`badminton_bot/main.py` — 補 import：

```python
import time

from badminton_bot.services.sports_center_webservice import BookingAttempt
from badminton_bot.utils.ntp_client import query_clock_offset
from badminton_bot.utils.server_clock import ClockMeasurement, measure_server_clock
from badminton_bot.utils.timing import plan_send_time, sleep_then_spin
```

模組層級的排程常數：

```python
# 開搶前這麼久登入，太早登入有 session 過期的風險
LOGIN_OFFSET = timedelta(minutes=-3)

# 時鐘探測的截止時刻。探測從登入完成後就開始，填滿原本閒置的那段時間
PROBE_DEADLINE_OFFSET = timedelta(seconds=-30)

# 預熱時機。夠做完預熱，又短到 keep-alive 一定守得住
WARM_UP_OFFSET = timedelta(seconds=-10)

# 搶完之後隔多久回頭看一次最終狀態
POST_CHECK_DELAY_SECONDS = 2.0
```

新增：

```python
async def run_booking_race(
    service, session, booking_periods, send_at_epoch: float
) -> list[BookingAttempt]:
    """Arm every booking request, then release them all at the send instant.

    Every URL is built and every task is scheduled before the deadline, so the
    only work left at the opening instant is releasing the event.

    Uses return_exceptions so one request failing cannot cancel the others —
    losing the 21:00 slot because the 20:00 response was unparsable would be a
    self-inflicted wound.

    Args:
        service: the sports centre web service.
        session: the aiohttp session.
        booking_periods: the datetimes to book.
        send_at_epoch (float): when to release the requests.

    Returns:
        list[BookingAttempt]: one attempt per period, in the order given.
    """
    ready_event = asyncio.Event()

    tasks = []
    for booking_date in booking_periods:
        booking_url = service._generate_booking_url(
            year=booking_date.year,
            month=booking_date.month,
            day=booking_date.day,
            hour=booking_date.hour,
        )
        logging.info("已備妥 %d 點的請求：%s", booking_date.hour, booking_url)
        tasks.append(
            asyncio.create_task(
                service.booking_courts(
                    session=session,
                    booking_url=booking_url,
                    hour=booking_date.hour,
                    ready_event=ready_event,
                )
            )
        )

    # 讓每個 task 都真的跑到 await ready_event.wait() 為止，
    # 否則「預先掛載」只是名義上的。
    await asyncio.sleep(0)

    sleep_then_spin(target_epoch=send_at_epoch)
    ready_event.set()

    results = await asyncio.gather(*tasks, return_exceptions=True)

    attempts: list[BookingAttempt] = []
    for booking_date, result in zip(booking_periods, results):
        if isinstance(result, BaseException):
            logging.error("%d 點的請求拋出未預期的例外：%s", booking_date.hour, result)
            attempts.append(
                BookingAttempt(
                    hour=booking_date.hour,
                    url="",
                    sent_epoch=0.0,
                    rtt=0.0,
                    server_date=None,
                    success=None,
                    error=str(result),
                )
            )
        else:
            attempts.append(result)

    return attempts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_main.py -v`
Expected: PASS

- [ ] **Step 5: Add the instrumentation summary**

`badminton_bot/main.py` 新增：

```python
def report_attempts(
    attempts: list[BookingAttempt], nominal_epoch: float, source: str
) -> None:
    """Log everything needed to tune the next run, so nobody has to guess again.

    Args:
        attempts (list[BookingAttempt]): the outcomes of the race.
        nominal_epoch (float): the nominal opening instant.
        source (str): which clock offset was applied.
    """
    logging.info("=== 搶場地結果（時鐘來源：%s）===", source)
    for attempt in attempts:
        offset_ms = (attempt.sent_epoch - nominal_epoch) * 1000
        logging.info(
            "%d 點：%s｜送出時刻相對開放時刻 %+.1f 毫秒｜RTT %.1f 毫秒｜"
            "伺服器 Date %s%s",
            attempt.hour,
            {True: "成功", False: "失敗", None: "無法判讀"}[attempt.success],
            offset_ms,
            attempt.rtt * 1000,
            attempt.server_date or "（無）",
            f"｜錯誤：{attempt.error}" if attempt.error else "",
        )
```

- [ ] **Step 6: Wire the whole flow into main()**

把 `main()` 裡從 `count_down(booking_date=upcoming_booking_date, offset=timedelta(minutes=-3))`
開始到 `await asyncio.gather(*tasks)` 為止的整段，換成：

```python
    # 非開發模式才校時。dev mode 打的仍是真實網站，每次測試都跑完整流程
    # 等於在非開搶時段多送十幾個請求，違反 live-site constraint。
    theta_ntp = None if dev_mode else query_clock_offset()

    nominal_epoch = upcoming_booking_date.timestamp()
    booking_target = booking_periods[0]

    # 倒數至開搶前的登入時間，太早登入有 session 過期的風險
    count_down(booking_date=upcoming_booking_date, offset=LOGIN_OFFSET)

    with webservice(username=national_id, password=password) as service:
        if not service.login_status:
            logging.error("登入失敗！")
            return

        cookies = service.get_cookies()
        referer = service._generate_list_page_url(
            year=booking_target.year,
            month=booking_target.month,
            day=booking_target.day,
        )

        async with service.create_session(cookies=cookies, referer=referer) as session:
            measurement = ClockMeasurement(theta=None, uncertainty=0.0)
            if not dev_mode:
                measurement = await measure_server_clock(
                    session=session,
                    url=referer,
                    deadline_epoch=(
                        upcoming_booking_date + PROBE_DEADLINE_OFFSET
                    ).timestamp(),
                )
                logging.info(
                    "伺服器校時：θ=%s 不確定度=±%.1f 毫秒 探測 %d 次 捨棄 %d 次",
                    f"{measurement.theta * 1000:+.1f} 毫秒"
                    if measurement.theta is not None
                    else "量測失敗",
                    measurement.uncertainty * 1000,
                    measurement.probe_count,
                    measurement.discarded_count,
                )
                if measurement.theta is not None and theta_ntp is not None:
                    logging.info(
                        "與 NTP 的差距 %.1f 毫秒（接近代表伺服器有做 NTP，可信度高）",
                        (measurement.theta - theta_ntp) * 1000,
                    )

            # 倒數至預熱時機
            count_down(booking_date=upcoming_booking_date, offset=WARM_UP_OFFSET)
            warm_up_results = await service.warm_up(
                session=session,
                year=booking_target.year,
                month=booking_target.month,
                day=booking_target.day,
            )

            # 前提驗證：那格在開放前就已經不是可訂狀態的話，所有優化的收益是零。
            # 只記錄，不對結果做任何分支。
            if warm_up_results and warm_up_results[0].body:
                for booking_date in booking_periods:
                    logging.info(
                        "開放前 %d 點的目標場地狀態：%s",
                        booking_date.hour,
                        service.parse_slot_state(
                            warm_up_results[0].body, hour=booking_date.hour
                        ),
                    )

            send_at_epoch, source, within_clamp = plan_send_time(
                nominal_epoch=nominal_epoch,
                theta_srv=measurement.theta,
                uncertainty=measurement.uncertainty,
                theta_ntp=theta_ntp,
                rtt_median=measurement.rtt_median,
                manual_offset_ms=0,
            )
            if not within_clamp:
                logging.warning(
                    "量到的時鐘修正量超出 ±%.1f 秒的上限，已拒絕套用",
                    2.0,
                )
            logging.info(
                "送出時刻相對名目開放時刻 %+.1f 毫秒（來源：%s）",
                (send_at_epoch - nominal_epoch) * 1000,
                source,
            )

            attempts = await run_booking_race(
                service=service,
                session=session,
                booking_periods=booking_periods,
                send_at_epoch=send_at_epoch,
            )
            report_attempts(
                attempts=attempts, nominal_epoch=nominal_epoch, source=source
            )

            # 事後偵察：這是「輸幾毫秒」與「根本沒開放給你」之間唯一的判別依據
            if not dev_mode:
                await asyncio.sleep(POST_CHECK_DELAY_SECONDS)
                try:
                    async with session.get(referer) as response:
                        body = await response.text()
                    for booking_date in booking_periods:
                        logging.info(
                            "開放後 %d 點的目標場地狀態：%s",
                            booking_date.hour,
                            service.parse_slot_state(body, hour=booking_date.hour),
                        )
                except Exception as error:
                    logging.warning("事後偵察失敗：%s", error)
```

**注意**：非 dev 模式下 `upcoming_booking_date` 已經含了使用者的手動偏移
（既有程式碼是 `UPCOMING_BOOKING_DATE + timedelta(milliseconds=offset_milliseconds)`），
所以 `plan_send_time` 的 `manual_offset_ms` 傳 0，避免重複套用。

- [ ] **Step 7: Run the whole suite**

Run: `pytest`
Expected: PASS（全部通過）

- [ ] **Step 8: 確認程式仍可啟動**

Run: `python -m badminton_bot.main`
Expected: 出現運動中心選單提示。**在輸入帳密之前按 Ctrl-C 中止 —— 不要跑到會碰網站的階段。**

- [ ] **Step 9: Commit**

```bash
git add badminton_bot/main.py tests/test_main.py
git commit -m "feat: 編排時鐘校正、連線預熱與儀表，並修正 gather 例外連坐"
```

---

## 完成後的驗證

離線測試全過只代表零件是對的。真正的驗證是**下週四那一次實跑**，讀 log 確認：

1. `伺服器校時：θ=...` —— 量到的鐘差是多少？與 NTP 差多少？
2. `預熱完成：Connection=...` —— keep-alive 有沒有守住？若是 `close`，預熱無效。
3. `送出時刻相對開放時刻 ...` —— 實際送出偏移多少？
4. `開放前/開放後 20 點的目標場地狀態` —— **若開放前就已經是「已訂」，
   代表整個方案的前提不成立，該場地根本不是靠速度搶得到的。**

依 CLAUDE.md 的 live-site constraint，**不要為了驗證而反覆實跑**。
一週一次，讀數據，再決定下一步。
