"""Main entry point to execute the program"""

import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp

from services.zhongshan_sports_center_webservice import ZhongshanSportsCenterWebService
from utils.input_helper import (
    get_valid_input,
    parse_input_booking_periods_str,
    transform_yes_no_input,
    check_if_target_datetime_is_outdated,
)

BOOKING_WEEKDAY = 4  # 填上星期幾搶場地
UPCOMING_BOOKING_DATE = (
    datetime.today()
    + timedelta(days=((BOOKING_WEEKDAY - datetime.today().isoweekday()) % 7))
).replace(hour=0, minute=0, second=0, microsecond=0)  # 這次搶場地的時間
FIRST_BOOKING_DATE = (UPCOMING_BOOKING_DATE + timedelta(days=14)).replace(hour=20)
SECOND_BOOKING_DATE = (UPCOMING_BOOKING_DATE + timedelta(days=14)).replace(hour=21)


async def main():
    """搶球場主程式的進入點，倒數計時後搶球場"""
    set_logger()

    national_id = get_valid_input(
        prompt="請輸入你的身分證字號：", transform_func=lambda x: x
    )
    password = get_valid_input(prompt="請輸入密碼：", transform_func=lambda x: x)
    dev_mode = get_valid_input(
        prompt="是否要進入開發測試模式？ Y/N：",
        transform_func=transform_yes_no_input,
        error_hint="請輸入 Y/N 決定是否要進入開發測試模式",
    )

    if dev_mode:
        upcoming_booking_date = get_valid_input(
            prompt="\n指定開搶時間(輸入格式為 YYYY-mm-ddTHH:MM:SS，例： 2025-04-12T15:00:00)\n：",
            transform_func=lambda x: check_if_target_datetime_is_outdated(
                target_datetime=datetime.strptime(x, "%Y-%m-%dT%H:%M:%S")
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
        upcoming_booking_date = UPCOMING_BOOKING_DATE
        booking_periods = (FIRST_BOOKING_DATE, SECOND_BOOKING_DATE)

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

    with ZhongshanSportsCenterWebService(
        username=national_id, password=password
    ) as service:
        if service.login_status:
            cookies = service.get_cookies()
            async with aiohttp.ClientSession(cookies=cookies) as session:
                # 時間倒數
                count_down(booking_date=upcoming_booking_date)

                # 非同步發送兩個請求
                tasks = [
                    service.booking_courts(
                        session=session,
                        year=booking_date.year,
                        month=booking_date.month,
                        day=booking_date.day,
                        hour=booking_date.hour,
                    )
                    for booking_date in booking_periods
                ]
                await asyncio.gather(*tasks)
        else:
            logging.error("登入失敗！")


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


def count_down(booking_date: datetime) -> None:
    """count down to the midnight of the booking date

    Args:
        booking_date (datetime): specified date to book the court
    """
    current_time = datetime.now()
    while not is_time_up(current_time=current_time, booking_date=booking_date):
        if current_time.microsecond == 0:
            delta_seconds = (booking_date - current_time).seconds
            if delta_seconds >= 10 and delta_seconds % 5 == 0:
                logging.info("倒數 %d 秒", delta_seconds)
            elif delta_seconds < 10:
                logging.info("倒數 %d 秒", delta_seconds)
            else:
                pass

        current_time = datetime.now()


def is_time_up(current_time: datetime, booking_date: datetime) -> bool:
    """檢查指定日期的半夜十二點到了沒的函式

    Args:
        current_time (datetime): 現在的時間

    Returns:
        bool: 時間到回傳 True，還沒到回傳 False
    """
    return (
        current_time.day == booking_date.day
        and current_time.hour == booking_date.hour
        and current_time.minute == booking_date.minute
    )


if __name__ == "__main__":
    asyncio.run(main())
