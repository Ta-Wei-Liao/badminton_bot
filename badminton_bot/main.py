"""Main entry point to execute the program"""

import asyncio
import logging
import time
from datetime import datetime, timedelta

import aiohttp
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
    transform_offset_milliseconds_param,
    transform_yes_no_input,
)
from badminton_bot.utils.ntp_client import query_clock_offset
from badminton_bot.utils.server_clock import ClockMeasurement, measure_server_clock
from badminton_bot.utils.timing import plan_send_time, sleep_then_spin

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

# 預熱時機。夠做完預熱，又短到 keep-alive 一定守得住
WARM_UP_OFFSET = timedelta(seconds=-10)

# 搶完之後隔多久回頭看一次最終狀態
POST_CHECK_DELAY_SECONDS = 2.0


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


async def main():
    """搶球場主程式的進入點，倒數計時後搶球場"""
    set_logger()

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
        offset_milliseconds = get_valid_input(
            prompt=(
                "請輸入想要偏移的毫秒數(輸入範圍為 -1000 ~ 1000，"
                "想要提早就輸入負整數，延後就輸入正整數，不想要偏移就不輸入)："
            ),
            transform_func=lambda x: transform_offset_milliseconds_param(
                input_milliseconds_param=x
            ),
            error_hint="輸入的偏移豪秒數不正確，請重新輸入",
        )
        upcoming_booking_date = UPCOMING_BOOKING_DATE + timedelta(
            milliseconds=offset_milliseconds
        )
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


def set_logger(debug_mode: bool = False) -> None:
    """set logging settings

    Args:
        debug_mode (bool, optional): set log level to debug with debug_mode is True. Defaults to False.
    """
    if debug_mode:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    logging.basicConfig(
        level=log_level, format="%(asctime)s - %(levelname)s - %(message)s"
    )


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
