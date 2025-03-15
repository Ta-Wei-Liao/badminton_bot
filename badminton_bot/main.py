"""Main entry point to execute the program"""

import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp

from services.zhongshan_sports_center_webservice import ZhongshanSportsCenterWebService

BOOKING_WEEKDAY = 4  # 填上星期幾搶場地
UPCOMING_THURSDAY_DATE = (
    datetime.today()
    + timedelta(days=((BOOKING_WEEKDAY - datetime.today().isoweekday()) % 7))
).replace(hour=0, minute=0, second=0, microsecond=0)  # 這次搶場地的時間
FIRST_BOOKING_DATE = (UPCOMING_THURSDAY_DATE + timedelta(days=14)).replace(hour=20)
SECOND_BOOKING_DATE = (UPCOMING_THURSDAY_DATE + timedelta(days=14)).replace(hour=21)


async def main():
    """搶球場主程式的進入點，倒數計時後搶球場
    """
    set_logger()

    national_id = input("請輸入你的身分證字號：")
    password = input("請輸入密碼：")

    is_booking_info_confirmed = input(
        f"\n身分證號碼：{national_id}\n"
        f"密碼：{password}\n"
        f"預定開搶時間：{UPCOMING_THURSDAY_DATE}\n"
        f"預計預約時段：{FIRST_BOOKING_DATE} & {SECOND_BOOKING_DATE}\n"
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

    with ZhongshanSportsCenterWebService(username=national_id, password=password) as service:
        if service.login_status:
            cookies = service.get_cookies()
            async with aiohttp.ClientSession(cookies=cookies) as session:
                # 時間倒數
                count_down()

                # 非同步發送兩個請求
                tasks = [
                    service.booking_courts(
                        session=session,
                        year=FIRST_BOOKING_DATE.year,
                        month=FIRST_BOOKING_DATE.month,
                        day=FIRST_BOOKING_DATE.day,
                        hour=FIRST_BOOKING_DATE.hour,
                    ),
                    service.booking_courts(
                        session=session,
                        year=SECOND_BOOKING_DATE.year,
                        month=SECOND_BOOKING_DATE.month,
                        day=SECOND_BOOKING_DATE.day,
                        hour=SECOND_BOOKING_DATE.hour,
                    ),
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


def count_down() -> None:
    """倒數計時到指定日期的半夜十二點
    """
    current_time = datetime.now()
    while not is_time_up(current_time=current_time):
        if current_time.microsecond == 0:
            delta_seconds = (UPCOMING_THURSDAY_DATE - current_time).seconds
            if delta_seconds >= 10 and delta_seconds % 5 == 0:
                logging.info("倒數 %d 秒", delta_seconds)
            elif delta_seconds < 10:
                logging.info("倒數 %d 秒", delta_seconds)
            else:
                pass

        current_time = datetime.now()


def is_time_up(current_time: datetime) -> bool:
    """檢查指定日期的半夜十二點到了沒的函式

    Args:
        current_time (datetime): 現在的時間

    Returns:
        bool: 時間到回傳 True，還沒到回傳 False
    """
    return (
        current_time.day == UPCOMING_THURSDAY_DATE.day
        and current_time.hour == UPCOMING_THURSDAY_DATE.hour
        and current_time.minute == UPCOMING_THURSDAY_DATE.minute
    )


if __name__ == "__main__":
    asyncio.run(main())
