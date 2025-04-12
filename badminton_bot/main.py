"""Main entry point to execute the program"""

import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp

from services.zhongshan_sports_center_webservice import ZhongshanSportsCenterWebService

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

    national_id = input("請輸入你的身分證字號：")
    password = input("請輸入密碼：")
    dev_mode = input("是否要進入開發測試模式？ Y/N：")

    while True:
        if dev_mode in ("Y", "N"):
            dev_mode = dev_mode == "Y"
            break
        else:
            dev_mode = input("請輸入 Y/N 決定是否要進入開發測試模式：")
            continue

    if dev_mode:
        while True:
            try:
                input_upcoming_booking_date_str = input(
                    "\n指定開搶時間：\n"
                    "(輸入格式為 YYYY-mm-ddTHH:MM:SS，"
                    "例： 2025-04-12T15:00:00)\n"
                )
                upcoming_booking_date = datetime.strptime(
                    input_upcoming_booking_date_str, "%Y-%m-%dT%H:%M:%S"
                )
            except ValueError:
                logging.error("輸入日期格式不正確，請重新輸入")
                continue

            break

        while True:
            try:
                input_booking_periods_str = input(
                    "\n指定想要預約的時段：\n"
                    "(輸入格式為 YYYY-mm-ddTHH:MM:SS，可輸入多個時段，用 , 分隔，不要有空格，"
                    "例：2025-04-12T15:00:00,2025-04-12T16:00:00)\n"
                )
                booking_periods = parse_input_booking_periods_str(
                    input_booking_periods_str=input_booking_periods_str
                )
            except ValueError:
                logging.error("輸入日期格式不正確，請重新輸入")
                continue

            break
    else:
        booking_periods = (FIRST_BOOKING_DATE, SECOND_BOOKING_DATE)

    is_booking_info_confirmed = input(
        f"\n身分證號碼：{national_id}\n"
        f"密碼：{password}\n"
        f"預定開搶時間：{upcoming_booking_date if dev_mode else UPCOMING_BOOKING_DATE}\n"
        f"預計預約時段：{' & '.join(date.strftime('%Y-%m-%d %H:%M%:%S') for date in booking_periods)}\n"
        f"請確認以上搶球場資訊是否正確？ Y/N："
    )

    while True:
        if is_booking_info_confirmed == "Y":
            logging.info("預約資訊已確認，繼續執行程式")
            break
        elif is_booking_info_confirmed == "N":
            logging.info("預約資訊不正確，終止程式。")
            return
        else:
            is_booking_info_confirmed = input("請輸入 Y/N 確認預約資訊是否正確：")
            continue

    with ZhongshanSportsCenterWebService(
        username=national_id, password=password
    ) as service:
        if service.login_status:
            cookies = service.get_cookies()
            async with aiohttp.ClientSession(cookies=cookies) as session:
                # 時間倒數
                count_down(
                    booking_date=upcoming_booking_date
                    if dev_mode
                    else UPCOMING_BOOKING_DATE
                )

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
    """設定 logging 的基本配置"""
    if debug_mode:
        log_level = logging.DEBUG
    else:
        log_level = logging.INFO

    logging.basicConfig(
        level=log_level, format="%(asctime)s - %(levelname)s - %(message)s"
    )


def parse_input_booking_periods_str(input_booking_periods_str: str) -> tuple[datetime]:
    date_str_list = input_booking_periods_str.split(",")

    return tuple(
        [datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S") for date_str in date_str_list]
    )


def count_down(booking_date: datetime) -> None:
    """倒數計時到指定日期的半夜十二點"""
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
