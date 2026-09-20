"""Main entry point to execute the program"""

import asyncio
import logging
from pathlib import Path
import time
from datetime import datetime, timedelta

from badminton_bot.services.sports_center_webservice import BookingAttempt, SportsCenterWebService
from badminton_bot.services.zhongshan_sports_center_webservice import (
    ZhongshanSportsCenterWebService,
)
from badminton_bot.services.zhongzheng_sports_center_webservice import (
    ZhongzhengSportsCenterWebService,
)
from badminton_bot.utils.input_helper import (
    cast_court_no_to_int_and_check_is_valid,
    check_if_target_datetime_is_outdated,
    get_valid_input,
    parse_input_booking_periods_str,
    transform_yes_no_input,
)
from badminton_bot.utils.ntp_client import query_clock_offset
from badminton_bot.utils.server_clock import (
    ClockMeasurement,
    measure_server_clock,
    seed_interval,
)
from badminton_bot.utils.timing import (
    MAX_AUTO_CORRECTION_SECONDS,
    plan_send_time,
    sleep_then_spin,
)

# 每次執行的 log 都留一份，之後才有辦法回頭看實際搶場地當下發生了什麼
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

BOOKING_WEEKDAY = 4  # 填上星期幾搶場地
UPCOMING_BOOKING_DATE = (
    datetime.today()
    + timedelta(days=((BOOKING_WEEKDAY - datetime.today().isoweekday()) % 7))
).replace(hour=0, minute=0, second=0, microsecond=0)  # 這次搶場地的時間
WEBSERVICE_MAPPING: dict[int, SportsCenterWebService] = {
    0: ZhongshanSportsCenterWebService,
    1: ZhongzhengSportsCenterWebService,
}

# 開搶前這麼久登入，太早登入有 session 過期的風險
LOGIN_OFFSET = timedelta(minutes=-3)

# 時鐘探測的截止時刻。探測從登入完成後就開始，填滿原本閒置的那段時間
PROBE_DEADLINE_OFFSET = timedelta(seconds=-30)

# 開放前查看場地狀態的時機。列表頁要 4~5 秒，所以不能擠在最後十秒裡
PRE_CHECK_OFFSET = timedelta(seconds=-40)

# 重新校時的時機。實測本機時鐘十幾分鐘就能漂 1.3 秒，
# 確認畫面當下量的值到了開搶那一刻早就過期了
NTP_REFRESH_OFFSET = timedelta(seconds=-20)

# 預熱時機。抓的是靜態資源，很快就做完
WARM_UP_OFFSET = timedelta(seconds=-10)

# 搶完之後隔多久回頭看一次最終狀態
POST_CHECK_DELAY_SECONDS = 2.0

# 列表頁實測要 4~5 秒，預算給寬一點；逾時只是少一筆偵察資料，不影響搶場地
LIST_PAGE_BUDGET_SECONDS = 12.0

# 預熱的時間預算。預熱本身約 200 毫秒，而它整個位在 T-10 秒的窗口裡，
# 留 6 秒已經很寬鬆，超過就代表出事了，寧可冷連線也不要拖掉開搶。
WARM_UP_BUDGET_SECONDS = 6.0

# 最後這麼久才進 busy-wait。忙碌等待期間 event loop 完全停擺，
# 伺服器送來的 FIN 不會被處理，死掉的連線就會留在連線池裡等著被用。
FINAL_SPIN_SECONDS = 0.3


def report_slot_states(service, html: str | None, booking_periods, label: str) -> None:
    """Log the target court's state and how many courts survived, at one instant.

    The court census is the more informative half. `X=2` cannot tell "someone
    beat me by 20 milliseconds" apart from "this hour was swept clean", and
    that answer decides whether shaving milliseconds is worth anything at all.

    Args:
        service: the sports centre web service.
        html (str | None): the list page body, or None if it could not be read.
        booking_periods: the datetimes being booked.
        label (str): "開放前" or "開放後", for the log line.
    """
    if html is None:
        logging.warning("%s無法取得場地列表頁，略過狀態檢查", label)
        return

    for booking_date in booking_periods:
        available = service.parse_available_courts(html, hour=booking_date.hour)
        logging.info(
            "%s %d 點：目標場地 %s｜該時段還空著 %d 片%s",
            label,
            booking_date.hour,
            service.parse_slot_state(html, hour=booking_date.hour),
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

    last_reported: int | None = None

    def _report_countdown(remaining: float) -> None:
        nonlocal last_reported
        whole_seconds = int(remaining)
        if whole_seconds == last_reported:
            return

        last_reported = whole_seconds
        logging.info("倒數 %d 秒", whole_seconds)

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


def report_attempts(
    attempts: list[BookingAttempt], reference_epoch: float, source: str
) -> None:
    """Log everything needed to tune the next run, so nobody has to guess again.

    Args:
        attempts (list[BookingAttempt]): the outcomes of the race.
        reference_epoch (float): the nominal opening instant to measure the
            send times against — deliberately the un-nudged one, so a user who
            asked for +200 ms sees +200 ms rather than zero.
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
            "%d 點：%s｜送出時刻相對名目開放時刻 %s｜RTT %.1f 毫秒｜"
            "伺服器 Date %s%s",
            attempt.hour,
            {True: "成功", False: "失敗", None: "無法判讀"}[attempt.success],
            offset_text,
            attempt.rtt * 1000,
            attempt.server_date or "（無）",
            f"｜錯誤：{attempt.error}" if attempt.error else "",
        )


async def main():
    """搶球場主程式的進入點，倒數計時後搶球場"""
    log_path = set_logger()

    logging.info("本次執行的 log 會留在 %s", log_path)

    courts_list_message = ""
    for court_no, court_service in WEBSERVICE_MAPPING.items():
        courts_list_message += f"{court_service.sport_center_name} -> {court_no}\n"

    input_court_no = get_valid_input(
        prompt=f"\n{courts_list_message}請輸入編號指定要預約的運動中心，運動中心編號清單如上：",
        transform_func=lambda x: cast_court_no_to_int_and_check_is_valid(
            input_court_no=x, mapping_dict=WEBSERVICE_MAPPING
        ),
        error_hint="請輸入正確的運動中心編號",
    )
    national_id = get_valid_input(
        prompt="請輸入你的身分證字號：", transform_func=lambda x: x
    )
    password = get_valid_input(prompt="請輸入密碼：", transform_func=lambda x: x)
    dev_mode = get_valid_input(
        prompt="是否要進入開發測試模式？ Y/N：",
        transform_func=transform_yes_no_input,
        error_hint="請輸入 Y/N 決定是否要進入開發測試模式",
    )

    webservice = webservice_factory(court_no=input_court_no)
    if dev_mode:
        upcoming_booking_date = get_valid_input(
            prompt="\n指定開搶時間(輸入格式為 YYYY-mm-ddTHH:MM:SS.fff，例： 2025-04-12T15:00:00.000)\n：",
            transform_func=lambda x: check_if_target_datetime_is_outdated(
                target_datetime=datetime.strptime(x, "%Y-%m-%dT%H:%M:%S.%f")
            ),
            error_hint="輸入日期不正確，請重新輸入",
        )
        booking_periods = get_valid_input(
            prompt=(
                "\n指定想要預約的時段"
                "(輸入格式為 YYYY-mm-ddTHH:MM:SS，可輸入多個時段，用 , 分隔，不要有空格，"
                "例：2025-04-12T15:00:00,2025-04-12T16:00:00)\n："
            ),
            transform_func=parse_input_booking_periods_str,
            error_hint="輸入日期不正確，請重新輸入",
        )
    else:
        # 時鐘校正已經自動化，不再需要人工猜一個毫秒偏移
        upcoming_booking_date = UPCOMING_BOOKING_DATE
        booking_periods = (
            (
                UPCOMING_BOOKING_DATE + timedelta(days=webservice.booking_window_days)
            ).replace(hour=20),
            (
                UPCOMING_BOOKING_DATE + timedelta(days=webservice.booking_window_days)
            ).replace(hour=21),
        )

    is_booking_info_confirmed = get_valid_input(
        prompt=(
            f"\n身分證號碼：{national_id}\n"
            f"密碼：{password}\n"
            f"預定開搶時間：{upcoming_booking_date}\n"
            f"預計預約時段：{' & '.join(date.strftime('%Y-%m-%d %H:%M%:%S') for date in booking_periods)}\n"
            f"請確認以上搶球場資訊是否正確？ Y/N："
        ),
        transform_func=transform_yes_no_input,
        error_hint="請輸入 Y/N 確認預約資訊是否正確：",
    )

    if not is_booking_info_confirmed:
        logging.info("預約資訊不正確，終止程式。")
        return
    else:
        logging.info("預約資訊已確認，繼續執行程式")

    # dev 模式一樣校時：它是一次完整彩排，跳過校時就驗證不到送出時刻的補償。
    theta_ntp = query_clock_offset()

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
            probe_deadline_epoch = (
                upcoming_booking_date + PROBE_DEADLINE_OFFSET
            ).timestamp()
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
            count_down(booking_date=upcoming_booking_date, offset=PRE_CHECK_OFFSET)
            report_slot_states(
                service=service,
                html=await run_with_deadline(
                    service.fetch_list_page(
                        session=session,
                        year=booking_target.year,
                        month=booking_target.month,
                        day=booking_target.day,
                    ),
                    budget_seconds=LIST_PAGE_BUDGET_SECONDS,
                    label="開放前場地檢查",
                    fallback=None,
                ),
                booking_periods=booking_periods,
                label="開放前",
            )

            # 重新校時。確認畫面當下量的值可能已經是十幾分鐘前的事，
            # 而實測這台機器十幾分鐘就能漂 1.3 秒。
            count_down(booking_date=upcoming_booking_date, offset=NTP_REFRESH_OFFSET)
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
                    "送出時刻的準確度取決於本機時鐘，且已無手動偏移可介入"
                )

            # 倒數至預熱時機
            count_down(booking_date=upcoming_booking_date, offset=WARM_UP_OFFSET)
            warm_up_results = await run_with_deadline(
                service.warm_up(session=session),
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

            attempts = await run_booking_race(
                service=service,
                session=session,
                booking_periods=booking_periods,
                send_at_epoch=send_at_epoch,
            )
            report_attempts(
                attempts=attempts, reference_epoch=nominal_epoch, source=source
            )

            # 事後偵察：這是「輸幾毫秒」與「根本沒開放給你」之間唯一的判別依據
            await asyncio.sleep(POST_CHECK_DELAY_SECONDS)
            report_slot_states(
                service=service,
                html=await run_with_deadline(
                    service.fetch_list_page(
                        session=session,
                        year=booking_target.year,
                        month=booking_target.month,
                        day=booking_target.day,
                    ),
                    budget_seconds=LIST_PAGE_BUDGET_SECONDS,
                    label="事後偵察",
                    fallback=None,
                ),
                booking_periods=booking_periods,
                label="開放後",
            )


def set_logger(log_dir: Path = LOG_DIR, debug_mode: bool = False) -> Path:
    """Send log records to the terminal and to a timestamped file.

    The file is written through logging rather than by redirecting the shell,
    and that is a safety property rather than a style choice: input() prompts
    never pass through logging, so the confirmation screen's credentials cannot
    reach the file. Shell redirection offers no such guarantee.

    Args:
        log_dir (Path, optional): directory for log files. Created if missing.
        debug_mode (bool, optional): log at DEBUG instead of INFO.

    Returns:
        Path: the file this run is logging to.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{datetime.now().strftime('%Y-%m-%dT%H%M%S')}.log"

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if debug_mode else logging.INFO)

    # 重複呼叫不該疊加 handler，否則同一行會被印很多次
    for existing in list(root_logger.handlers):
        root_logger.removeHandler(existing)
        existing.close()

    for handler in (
        logging.StreamHandler(),
        logging.FileHandler(log_path, encoding="utf-8"),
    ):
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)

    return log_path


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

        # booking_date 最遠可能在七天之後，一路每 5 秒印一次會累積十幾萬行 log，
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
        target_epoch=count_down_target_time.timestamp(), on_tick=_report
    )


def webservice_factory(court_no: int) -> SportsCenterWebService:
    """Return the corresponding webservice class according to the court number.

    Args:
        court_no (int): the court_no in the mapping object

    Raises:
        ValueError: if the input court_no is not in the mapping object, raise this error

    Returns:
        SportsCenterWebService: the corresponding webservice class
    """
    webservice = WEBSERVICE_MAPPING.get(court_no)
    if webservice:
        return WEBSERVICE_MAPPING.get(court_no)
    else:
        raise ValueError("無效的運動中心編號")


if __name__ == "__main__":
    asyncio.run(main())
