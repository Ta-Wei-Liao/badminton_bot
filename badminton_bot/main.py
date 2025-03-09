import asyncio
from datetime import datetime, timedelta
import logging

import aiohttp

from services.zhongshan_sports_center_webservice import ZhongshanSportsCenterWebService


BOOKING_WEEKDAY = 4  # 填上星期幾搶場地
UPCOMING_THURSDAY_DATE = (
    datetime.today() +
    timedelta(days=(BOOKING_WEEKDAY - (datetime.today().isoweekday() % 7)))
).replace(hour=0, minute=0, second=0, microsecond=0)  # 這次搶場地的時間

async def main():
    set_logger()

    with ZhongshanSportsCenterWebService(username="OOO", password="XXX") as service:
        if service.login_status:
            cookies = service.get_cookies()
            async with aiohttp.ClientSession(cookies=cookies) as session:
                # 時間倒數
                count_down()

                # 非同步發送兩個請求
                tasks = [
                    service.booking_courts(
                        session=session,
                        year=UPCOMING_THURSDAY_DATE.year,
                        month=UPCOMING_THURSDAY_DATE.month,
                        day=UPCOMING_THURSDAY_DATE.day + 14,
                        hour=20
                    ),
                    service.booking_courts(
                        session=session,
                        year=UPCOMING_THURSDAY_DATE.year,
                        month=UPCOMING_THURSDAY_DATE.month,
                        day=UPCOMING_THURSDAY_DATE.day + 14,
                        hour=21
                    )
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
        level=log_level,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )

def count_down() -> None:
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
    return (
        current_time.day == UPCOMING_THURSDAY_DATE.day and
        current_time.hour == UPCOMING_THURSDAY_DATE.hour and
        current_time.minute == UPCOMING_THURSDAY_DATE.minute
    )


if __name__ == "__main__":
    asyncio.run(main())
