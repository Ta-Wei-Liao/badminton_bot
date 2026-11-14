"""The grab engine: login, clock measurement, warm-up and the booking race.

Knows nothing about any UI. It never calls input(); progress is reported
through an on_stage callback and the logging module, and a run can be
cancelled from another thread right up to the instant the requests go out.
"""

import asyncio
import logging
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from badminton_bot.booking_plan import BookingTarget, validate_targets
from badminton_bot.services.sports_center_webservice import (
    SLOT_UNKNOWN,
    BookingAttempt,
    SportsCenterWebService,
    check_browser,
)
from badminton_bot.services.zhongshan_sports_center_webservice import (
    ZhongshanSportsCenterWebService,
)
from badminton_bot.services.zhongzheng_sports_center_webservice import (
    ZhongzhengSportsCenterWebService,
)
from badminton_bot.utils.errors import describe_error
from badminton_bot.utils.keep_awake import keep_awake
from badminton_bot.utils.ntp_client import query_clock_offset
from badminton_bot.utils.server_clock import (
    ClockMeasurement,
    measure_server_clock,
    seed_interval,
)
from badminton_bot.utils.timing import (
    MAX_AUTO_CORRECTION_SECONDS,
    Cancelled,
    plan_send_time,
    sleep_then_spin,
)

# 每次執行的 log 都留一份，之後才有辦法回頭看實際搶場地當下發生了什麼。
# 放在「文件」底下，打包後的使用者也找得到，不會跟著程式資料夾被覆蓋掉
LOG_DIR = Path.home() / "Documents" / "badminton_bot_logs"

# 「文件」寫不進去時（macOS 拒絕存取「文件」、Windows 受控資料夾存取）依序改用這些，
# 否則程式根本無法啟動
LOG_FALLBACK_DIRS = (
    Path.home() / "badminton_bot_logs",
    Path(tempfile.gettempdir()) / "badminton_bot_logs",
)

# 這些套件在 DEBUG 會記下送出的請求內容（Selenium 連密碼都會記），只留 WARNING 以上
QUIET_LIBRARY_LOGGERS = ("selenium", "urllib3", "asyncio")

# 開搶前這麼久登入，太早登入有 session 過期的風險
LOGIN_OFFSET = timedelta(minutes=-3)

# 時鐘探測的截止時刻。探測從登入完成後就開始，填滿原本閒置的那段時間
PROBE_DEADLINE_OFFSET = timedelta(seconds=-30)

# 開放前查看場地狀態的時機。列表頁要 4~5 秒，所以不能擠在最後十秒裡
PRE_CHECK_OFFSET = timedelta(seconds=-40)

# 重新校時的時機。實測本機時鐘十幾分鐘就能漂 1.3 秒，
# 一開始量的值到了開搶那一刻早就過期了
NTP_REFRESH_OFFSET = timedelta(seconds=-20)

# 預熱時機。抓的是靜態資源，很快就做完
WARM_UP_OFFSET = timedelta(seconds=-10)

# 搶完之後隔多久回頭看一次最終狀態。偵察要的是最終誰搶到，不必搶在尖峰裡看：
# 實測開搶當下伺服器回應要 5~27 秒（甚至 30 秒逾時），等尖峰退了再抓，請求數不變
POST_CHECK_DELAY_SECONDS = 45.0

# 列表頁的請求上限。實測開搶前就要 5 秒以上，原本沿用 session 的 5 秒上限，
# 四週的開放前／後偵察全數逾時。逾時只是少一筆偵察資料，不影響搶場地。
# 開放前那一發從 T-40s 開始，15 秒內結束才不會拖到 T-20s 的重新校時
PRE_CHECK_TIMEOUT_SECONDS = 15.0
# 開放後伺服器正被擠爆，搶場地的請求實測要 5~27 秒才回，給足時間
POST_CHECK_TIMEOUT_SECONDS = 30.0

# run_with_deadline 比請求本身的上限多留一點，讓請求自己的逾時先觸發、留下 log
LIST_PAGE_DEADLINE_SLACK_SECONDS = 1.0

# 預熱的時間預算。預熱本身約 200 毫秒，而它整個位在 T-10 秒的窗口裡，
# 留 6 秒已經很寬鬆，超過就代表出事了，寧可冷連線也不要拖掉開搶。
WARM_UP_BUDGET_SECONDS = 6.0

# 最後這麼久才進 busy-wait。忙碌等待期間 event loop 完全停擺，
# 伺服器送來的 FIN 不會被處理，死掉的連線就會留在連線池裡等著被用。
FINAL_SPIN_SECONDS = 0.3

STAGE_BROWSER_CHECK = "瀏覽器自我檢查"
STAGE_WAITING_LOGIN = "等待登入"
STAGE_LOGGING_IN = "登入中"
STAGE_CLOCK = "校時中"
STAGE_PRE_CHECK = "開放前檢查"
STAGE_WARM_UP = "預熱連線"
STAGE_COUNTDOWN = "倒數"
STAGE_FIRED = "已送出，等待回應"
STAGE_POST_CHECK = "事後檢查"
STAGE_DONE = "完成"


class LoginFailed(RuntimeError):
    """The booking site rejected the credentials."""


@dataclass(frozen=True)
class CenterOption:
    """One sports centre as offered to the user, and whether it may be run."""

    service_cls: type[SportsCenterWebService]
    enabled: bool

    @property
    def label(self) -> str:
        name = self.service_cls.sport_center_name
        return name if self.enabled else f"{name}（暫時停止）"


CENTER_OPTIONS: tuple[CenterOption, ...] = (
    CenterOption(ZhongzhengSportsCenterWebService, True),
    CenterOption(ZhongshanSportsCenterWebService, False),
)


def is_center_enabled(service_cls: type[SportsCenterWebService]) -> bool:
    """Whether service_cls (or a centre it specialises) is offered and enabled."""
    for option in CENTER_OPTIONS:
        if issubclass(service_cls, option.service_cls):
            return option.enabled

    return False


@dataclass(frozen=True)
class GrabConfig:
    """Everything one grab needs. The password is kept out of repr on purpose."""

    service_cls: type[SportsCenterWebService]
    username: str
    password: str = field(repr=False)
    open_at: datetime
    targets: tuple[BookingTarget, ...]


def report_slot_states(
    service, html: str | None, targets: Sequence[BookingTarget], label: str
) -> None:
    """Log each target court's state and how many courts survived, at one instant.

    The court census is the more informative half. `X=2` cannot tell "someone
    beat me by 20 milliseconds" apart from "this hour was swept clean", and
    that answer decides whether shaving milliseconds is worth anything at all.

    Args:
        service: the sports centre web service.
        html (str | None): the list page body, or None if it could not be read.
        targets (Sequence[BookingTarget]): the court/hour pairs being booked.
        label (str): "開放前" or "開放後", for the log line.
    """
    if html is None:
        logging.warning("%s無法取得場地列表頁，略過狀態檢查", label)
        return

    for booking_target in targets:
        hour = booking_target.hour
        available = service.parse_available_courts(html, hour=hour)
        state = service.parse_slot_state(html, hour=hour, qpid=booking_target.court.qpid)

        # 開搶前要的那一天還沒進入預約窗口，頁面根本不會渲染它 —— 此時
        # 「還空著 0 片」讀起來像全被掃光，但真相是這一天還看不到。
        # 頁面完全沒提到我們的場地（連別的時段都沒有），就是這種情況。
        if state == SLOT_UNKNOWN and not available:
            logging.info(
                "%s %s %d 點：該日期尚未進入預約窗口，頁面看不到這一天（開搶前的正常狀態）",
                label,
                booking_target.court.name,
                hour,
            )
            continue

        logging.info(
            "%s %s %d 點：目標場地 %s｜該時段還空著 %d 片%s",
            label,
            booking_target.court.name,
            hour,
            state,
            len(available),
            f"（{available}）" if available else "",
        )


async def run_with_deadline(awaitable, budget_seconds: float, label: str, fallback):
    """Await something with a hard ceiling, degrading to fallback instead of aborting.

    A stalled request raises nothing, so try/except alone cannot protect the
    schedule — only a timeout can.

    Args:
        awaitable: the coroutine to run.
        budget_seconds (float): hard ceiling. A non-positive budget skips the work.
        label (str): name used in the warning log.
        fallback: value returned when the work times out, fails, or is skipped.

    Returns:
        The awaitable's result, or fallback.
    """
    if budget_seconds <= 0:
        logging.warning("%s 的時間預算已用盡，直接跳過", label)
        # 收下的 coroutine 沒被 await 就得自己關掉，否則會留下 never awaited 警告。
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()

        return fallback

    try:
        return await asyncio.wait_for(awaitable, timeout=budget_seconds)
    except asyncio.TimeoutError:
        logging.warning("%s 超過 %.1f 秒預算，已放棄，繼續搶場地", label, budget_seconds)
    except Exception as error:
        # CancelledError 繼承自 BaseException，不會被這裡吞掉，仍會往外傳。
        logging.warning("%s 失敗：%s，已放棄，繼續搶場地", label, error)

    return fallback


async def run_booking_race(
    service,
    session,
    targets: Sequence[BookingTarget],
    send_at_epoch: float,
    on_fired: Callable[[], None] | None = None,
    on_released: Callable[[], None] | None = None,
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
        targets (Sequence[BookingTarget]): the court/hour pairs to book.
        send_at_epoch (float): when to release the requests.
        on_fired (Callable[[], None] | None, optional): called right before the
            requests are released. Raising from it releases nothing.
        on_released (Callable[[], None] | None, optional): called after the
            requests have left. Queued behind them on the event loop, so its
            latency can never delay the send, and an exception from it is only
            logged.

    Returns:
        list[BookingAttempt]: one attempt per target, in the order given.
    """
    ready_event = asyncio.Event()

    tasks = []
    for booking_target in targets:
        start = booking_target.start
        booking_url = service._generate_booking_url(
            year=start.year,
            month=start.month,
            day=start.day,
            hour=start.hour,
            qpid=booking_target.court.qpid,
        )
        logging.info(
            "已備妥 %s %d 點的請求：%s", booking_target.court.name, start.hour, booking_url
        )
        tasks.append(
            asyncio.create_task(
                service.booking_courts(
                    session=session,
                    booking_url=booking_url,
                    hour=start.hour,
                    ready_event=ready_event,
                    court=booking_target.court,
                )
            )
        )

    last_reported: int | None = None

    def _report_countdown(remaining: float) -> None:
        nonlocal last_reported
        whole_seconds = int(remaining)
        if whole_seconds == last_reported:
            return

        last_reported = whole_seconds
        logging.info("倒數 %d 秒", whole_seconds)

    try:
        # 讓每個 task 都真的跑到 await ready_event.wait() 為止，
        # 否則「預先掛載」只是名義上的。
        await asyncio.sleep(0)

        # 讓出控制權直到最後一刻，event loop 才有機會處理伺服器送來的 FIN，
        # 把已經死掉的連線踢出連線池；忙碌等待只保留在最後 0.3 秒。
        coarse_until = send_at_epoch - FINAL_SPIN_SECONDS
        while (remaining := coarse_until - time.time()) > 0:
            _report_countdown(send_at_epoch - time.time())
            await asyncio.sleep(min(0.05, remaining))

        sleep_then_spin(
            target_epoch=send_at_epoch,
            spin_window=FINAL_SPIN_SECONDS,
            on_tick=_report_countdown,
        )
        if on_fired is not None:
            on_fired()
    except BaseException:
        # 還沒放行就被取消：掛著的請求一個都不能送出去，也不能留成孤兒 task
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    ready_event.set()

    if on_released is not None:
        # set() 已經把每個請求的喚醒排進 loop；call_soon 排在它們後面，
        # 所以回呼最快也是在全部請求送出之後才跑，再慢也拖不到送出時刻
        def _safe_on_released() -> None:
            try:
                on_released()
            except Exception as error:
                logging.warning("送出後的回呼失敗：%s", describe_error(error))

        asyncio.get_running_loop().call_soon(_safe_on_released)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    attempts: list[BookingAttempt] = []
    for booking_target, result in zip(targets, results):
        if isinstance(result, BaseException):
            logging.error(
                "%s %d 點的請求拋出未預期的例外：%s",
                booking_target.court.name,
                booking_target.hour,
                result,
            )
            attempts.append(
                BookingAttempt(
                    hour=booking_target.hour,
                    url="",
                    sent_epoch=0.0,
                    rtt=0.0,
                    server_date=None,
                    success=None,
                    error=str(result),
                    court_name=booking_target.court.name,
                    qpid=booking_target.court.qpid,
                )
            )
        else:
            attempts.append(result)

    return attempts


def report_attempts(
    attempts: list[BookingAttempt], reference_epoch: float, source: str
) -> None:
    """Log everything needed to tune the next run, so nobody has to guess again.

    Args:
        attempts (list[BookingAttempt]): the outcomes of the race.
        reference_epoch (float): the nominal opening instant to measure the
            send times against.
        source (str): which clock offset was applied.
    """
    logging.info("=== 搶場地結果（時鐘來源：%s）===", source)
    for attempt in attempts:
        # 例外造出來的合成紀錄沒有送出時刻，拿 0.0 去減會印出 -1.8e12 毫秒。
        offset_text = (
            "（無送出紀錄）"
            if attempt.sent_epoch == 0.0
            else f"{(attempt.sent_epoch - reference_epoch) * 1000:+.1f} 毫秒"
        )
        logging.info(
            "%s %d 點：%s｜送出時刻相對名目開放時刻 %s｜RTT %.1f 毫秒｜"
            "伺服器 Date %s%s",
            attempt.court_name,
            attempt.hour,
            {True: "成功", False: "失敗", None: "無法判讀"}[attempt.success],
            offset_text,
            attempt.rtt * 1000,
            attempt.server_date or "（無）",
            f"｜錯誤：{attempt.error}" if attempt.error else "",
        )


class GrabRunner:
    """Runs one grab on the schedule, cancellable from another thread until it fires.

    run() is meant to be driven by its own event loop in a background thread;
    cancel() and fired are safe to use from any thread.
    """

    def __init__(
        self, config: GrabConfig, on_stage: Callable[[str], None] = lambda s: None
    ) -> None:
        self._config = config
        self._on_stage = on_stage
        self._cancel_event = threading.Event()
        # 保護 _fired 與 loop/task 的登記：取消與放行只能有一個贏
        self._lock = threading.Lock()
        self._fired = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        # 只在 event loop 的執行緒上讀寫：取消是否已經送進 run() 了
        self._cancel_delivered = False

    @property
    def fired(self) -> bool:
        with self._lock:
            return self._fired

    def cancel(self) -> bool:
        """Ask the run to stop. Refused, and a no-op, once the requests are out.

        Returns:
            bool: True if the cancellation was accepted.
        """
        with self._lock:
            if self._fired:
                return False

            self._cancel_event.set()
            if self._loop is not None:
                # 同步的倒數由 cancel_event 打斷；正在 await 的階段則靠 task.cancel。
                # 不直接排 task.cancel：loop 卡在同步倒數時它會被延後到收尾的 await，
                # 把 session 關到一半。交給 loop 執行緒判斷取消是否已經送達。
                self._loop.call_soon_threadsafe(self._deliver_cancel)

            return True

    def _deliver_cancel(self) -> None:
        """Runs on the loop thread: cancel the task unless Cancelled is already unwinding it."""
        if self._cancel_delivered or self._task is None or self._task.done():
            return

        self._cancel_delivered = True
        self._task.cancel()

    def _mark_fired(self) -> None:
        with self._lock:
            # 取消搶先一步：就算倒數已經走完，也一發都不送
            if self._cancel_event.is_set():
                self._cancel_delivered = True
                raise Cancelled()

            self._fired = True

    def _stage(self, stage: str) -> None:
        """Report a stage; a broken UI callback must never break the grab."""
        try:
            self._on_stage(stage)
        except Exception as error:
            logging.warning("回報階段「%s」失敗：%s", stage, describe_error(error))

    def _count_down(self, offset: timedelta) -> None:
        try:
            count_down(
                booking_date=self._config.open_at,
                offset=offset,
                cancel_event=self._cancel_event,
            )
        except Cancelled:
            self._cancel_delivered = True
            raise

        # 最後幾秒的 busy-wait 不看 cancel_event：倒數走完時取消可能已被接受，
        # 不擋在這裡的話，下一步（例如開 Chrome 登入）會照樣執行
        if self._cancel_event.is_set():
            self._cancel_delivered = True
            raise Cancelled()

    async def run(self) -> list[BookingAttempt]:
        """Run the whole grab and return one attempt per target.

        Raises:
            ValueError: the centre is disabled or the targets are invalid.
            LoginFailed: the site rejected the credentials.
            Cancelled: cancel() was accepted before the requests went out.
        """
        config = self._config
        if not is_center_enabled(config.service_cls):
            raise ValueError(f"{config.service_cls.sport_center_name}目前暫時停止使用")
        validate_targets(config.targets)

        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()

        try:
            if self._cancel_event.is_set():
                # 還沒開始就被取消了，那時候沒有 task 可以 cancel
                self._cancel_delivered = True
                raise Cancelled()

            self._stage(STAGE_BROWSER_CHECK)
            check_browser()
            with keep_awake():
                return await self._run_schedule()
        except asyncio.CancelledError:
            raise Cancelled() from None
        finally:
            # asyncio.run 結束就會關掉 loop；之後再按取消不能去碰已關閉的 loop
            with self._lock:
                self._loop = None
                self._task = None

    async def _run_schedule(self) -> list[BookingAttempt]:
        config = self._config

        self._stage(STAGE_WAITING_LOGIN)
        theta_ntp = query_clock_offset()

        # 倒數至開搶前的登入時間，太早登入有 session 過期的風險
        self._count_down(LOGIN_OFFSET)

        self._stage(STAGE_LOGGING_IN)
        attempts: list[BookingAttempt] | None = None
        with config.service_cls(
            username=config.username, password=config.password
        ) as service:
            if service.login_status:
                attempts = await self._grab(service, theta_ntp)

        # 離開 with 之後才拋：登出、關 Chrome 照常完成
        if attempts is None:
            logging.error("登入失敗！")
            raise LoginFailed("登入失敗，請確認身分證字號與密碼")

        self._stage(STAGE_DONE)
        return attempts

    async def _grab(
        self, service: SportsCenterWebService, theta_ntp: float | None
    ) -> list[BookingAttempt]:
        """Everything between a successful login and the post-race check."""
        config = self._config
        targets = config.targets
        # 全部 target 都是同一天，偵察與 referer 都看第一個
        booking_day = targets[0].start
        nominal_epoch = config.open_at.timestamp()

        cookies = service.get_cookies()
        referer = service._generate_list_page_url(
            year=booking_day.year, month=booking_day.month, day=booking_day.day
        )

        async with service.create_session(cookies=cookies, referer=referer) as session:
            self._stage(STAGE_CLOCK)
            probe_deadline_epoch = (config.open_at + PROBE_DEADLINE_OFFSET).timestamp()
            measurement = await run_with_deadline(
                measure_server_clock(
                    session=session,
                    url=referer,
                    deadline_epoch=probe_deadline_epoch,
                    # 用 NTP 把搜尋區間平移到對的位置。本機時鐘差幾秒時，
                    # 預設的 (-2, 2) 會讓真值落在區間外，第一次探測就必然矛盾。
                    initial_interval=seed_interval(theta_ntp),
                ),
                budget_seconds=probe_deadline_epoch - time.time(),
                label="伺服器時鐘探測",
                fallback=ClockMeasurement(theta=None, uncertainty=0.0),
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
            if measurement.rtt_samples:
                logging.info(
                    "探測 RTT：min %.1f／median %.1f／max %.1f 毫秒",
                    min(measurement.rtt_samples) * 1000,
                    measurement.rtt_median * 1000,
                    max(measurement.rtt_samples) * 1000,
                )
            if measurement.theta is not None and theta_ntp is not None:
                logging.info(
                    "與 NTP 的差距 %.1f 毫秒（接近代表伺服器有做 NTP，可信度高）",
                    (measurement.theta - theta_ntp) * 1000,
                )

            # 開放前查看場地狀態。列表頁要 4~5 秒，所以提前到 T-40s 單獨做，
            # 不跟預熱擠在最後十秒。只記錄，不對結果做任何分支。
            self._stage(STAGE_PRE_CHECK)
            self._count_down(PRE_CHECK_OFFSET)
            report_slot_states(
                service=service,
                html=await run_with_deadline(
                    service.fetch_list_page(
                        session=session,
                        year=booking_day.year,
                        month=booking_day.month,
                        day=booking_day.day,
                        timeout_seconds=PRE_CHECK_TIMEOUT_SECONDS,
                    ),
                    budget_seconds=PRE_CHECK_TIMEOUT_SECONDS
                    + LIST_PAGE_DEADLINE_SLACK_SECONDS,
                    label="開放前場地檢查",
                    fallback=None,
                ),
                targets=targets,
                label="開放前",
            )

            # 重新校時。一開始量的值可能已經是好幾小時前的事，
            # 而實測這台機器十幾分鐘就能漂 1.3 秒。
            self._count_down(NTP_REFRESH_OFFSET)
            refreshed_theta_ntp = query_clock_offset()
            if refreshed_theta_ntp is not None and theta_ntp is not None:
                logging.info(
                    "本機時鐘在這段期間漂移了 %.1f 毫秒",
                    (refreshed_theta_ntp - theta_ntp) * 1000,
                )
            if refreshed_theta_ntp is not None:
                theta_ntp = refreshed_theta_ntp

            if theta_ntp is None:
                logging.warning(
                    "NTP 校時失敗，本次完全不做時鐘校正 —— "
                    "送出時刻的準確度取決於本機時鐘"
                )

            # 倒數至預熱時機，每個請求各預熱一條連線
            self._stage(STAGE_WARM_UP)
            self._count_down(WARM_UP_OFFSET)
            await run_with_deadline(
                service.warm_up(session=session, count=len(targets)),
                budget_seconds=WARM_UP_BUDGET_SECONDS,
                label="連線預熱",
                fallback=[],
            )

            # 補償用的是握手量到的飛行時間，不是回應時間 —— 這個站的回應時間
            # 幾乎都是伺服器處理，拿它的一半去補償會把請求提早好幾秒送出。
            one_way_seconds = service.handshake_timing.one_way_estimate
            logging.info(
                "單程飛行時間估計 %.1f 毫秒（取自 %d 次握手；回應時間中位數 %.1f 毫秒）",
                one_way_seconds * 1000,
                len(service.handshake_timing.handshake_samples),
                measurement.rtt_median * 1000,
            )

            send_at_epoch, source, within_clamp = plan_send_time(
                nominal_epoch=nominal_epoch,
                theta_srv=measurement.theta,
                uncertainty=measurement.uncertainty,
                theta_ntp=theta_ntp,
                one_way_seconds=one_way_seconds,
                manual_offset_ms=0,
            )
            if not within_clamp:
                logging.warning(
                    "量到的時鐘修正量超出 ±%.0f 秒的上限，已拒絕套用，本次不做任何校正",
                    MAX_AUTO_CORRECTION_SECONDS,
                )
            logging.info(
                "送出時刻相對名目開放時刻 %+.1f 毫秒（來源：%s）",
                (send_at_epoch - nominal_epoch) * 1000,
                source,
            )

            self._stage(STAGE_COUNTDOWN)
            attempts = await run_booking_race(
                service=service,
                session=session,
                targets=targets,
                send_at_epoch=send_at_epoch,
                on_fired=self._mark_fired,
                on_released=lambda: self._stage(STAGE_FIRED),
            )
            report_attempts(
                attempts=attempts, reference_epoch=nominal_epoch, source=source
            )

            # 事後偵察：這是「輸幾毫秒」與「根本沒開放給你」之間唯一的判別依據
            self._stage(STAGE_POST_CHECK)
            await asyncio.sleep(POST_CHECK_DELAY_SECONDS)
            report_slot_states(
                service=service,
                html=await run_with_deadline(
                    service.fetch_list_page(
                        session=session,
                        year=booking_day.year,
                        month=booking_day.month,
                        day=booking_day.day,
                        timeout_seconds=POST_CHECK_TIMEOUT_SECONDS,
                    ),
                    budget_seconds=POST_CHECK_TIMEOUT_SECONDS
                    + LIST_PAGE_DEADLINE_SLACK_SECONDS,
                    label="事後偵察",
                    fallback=None,
                ),
                targets=targets,
                label="開放後",
            )

        return attempts


def _open_log_file(
    log_dirs: Sequence[Path],
) -> tuple[logging.FileHandler, list[tuple[Path, OSError]]]:
    """Open a timestamped log file in the first directory that accepts one.

    Returns:
        tuple: the file handler, and every directory skipped with its error.

    Raises:
        OSError: no directory was writable; the last directory's error.
    """
    file_name = f"{datetime.now().strftime('%Y-%m-%dT%H%M%S')}.log"
    skipped: list[tuple[Path, OSError]] = []
    for log_dir in log_dirs:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            return logging.FileHandler(log_dir / file_name, encoding="utf-8"), skipped
        except OSError as error:
            skipped.append((log_dir, error))

    raise skipped[-1][1]


def set_logger(
    log_dir: Path = LOG_DIR,
    extra_handlers: Sequence[logging.Handler] = (),
    fallback_dirs: Sequence[Path] = LOG_FALLBACK_DIRS,
) -> Path:
    """Send log records to the console (when there is one), a timestamped file,
    and any extra handlers.

    The password never passes through logging, so it cannot reach the file.

    Args:
        log_dir (Path, optional): directory for log files. Created if missing.
        extra_handlers (Sequence[logging.Handler], optional): further handlers,
            such as the GUI's queue handler.
        fallback_dirs (Sequence[Path], optional): tried in order when log_dir
            cannot be written to.

    Returns:
        Path: the file this run is logging to, which may be in a fallback dir.

    Raises:
        OSError: neither log_dir nor any fallback directory is writable.
    """
    file_handler, skipped = _open_log_file([log_dir, *fallback_dirs])
    log_path = Path(file_handler.baseFilename)

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    # Selenium 的 DEBUG 會印出送給瀏覽器的每個指令，包含填進去的密碼；
    # 把第三方套件固定在 WARNING，就算根 logger 之後被調成 DEBUG 也不會寫進 log 檔
    for library in QUIET_LIBRARY_LOGGERS:
        logging.getLogger(library).setLevel(logging.WARNING)

    # 重複呼叫不該疊加 handler，否則同一行會被印很多次
    for existing in list(root_logger.handlers):
        root_logger.removeHandler(existing)
        existing.close()

    handlers: list[logging.Handler] = []
    # 打包成視窗程式（沒有主控台）時 sys.stderr 是 None，StreamHandler 一寫就會炸
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    handlers.append(file_handler)
    handlers.extend(extra_handlers)

    for handler in handlers:
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)

    for skipped_dir, error in skipped:
        logging.warning("無法寫入 log 資料夾 %s（%s）", skipped_dir, error)
    if skipped:
        logging.warning("log 改存在 %s", log_path.parent)

    return log_path


def count_down(
    booking_date: datetime,
    offset: timedelta = timedelta(),
    cancel_event: threading.Event | None = None,
) -> None:
    """Count down to the target time (booking_date plus offset), while always
    reporting the seconds remaining to booking_date itself.

    Args:
        booking_date (datetime): the opening instant; timezone-aware.
        offset (timedelta, optional): shifts the wait target relative to
            booking_date. Defaults to timedelta().
        cancel_event (threading.Event | None, optional): interrupts the wait.

    Raises:
        Cancelled: cancel_event was set while waiting.
    """
    count_down_target_time = booking_date + offset

    def _report(_remaining_to_target: float) -> None:
        # 一律回報距離 booking_date 的秒數，而不是距離提前量之後的等待目標。
        # 用 total_seconds()：timedelta.seconds 遇到負值會捲成 ~86400。
        delta_seconds = int(
            (booking_date - datetime.now(booking_date.tzinfo)).total_seconds()
        )

        # booking_date 可能在好幾小時之後，一路每 5 秒印一次會累積大量 log，
        # 真正要看的最後幾秒反而被沖掉。離得越遠，回報越稀疏。
        if delta_seconds > 3600:
            cadence = 600
        elif delta_seconds > 300:
            cadence = 60
        elif delta_seconds >= 10:
            cadence = 5
        else:
            cadence = 1

        if delta_seconds % cadence == 0:
            logging.info("倒數 %d 秒", delta_seconds)

    sleep_then_spin(
        target_epoch=count_down_target_time.timestamp(),
        on_tick=_report,
        cancel_event=cancel_event,
    )
