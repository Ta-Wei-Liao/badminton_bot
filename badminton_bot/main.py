"""Main entry point to execute the program"""

import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp

from services.sports_center_webservice import SportsCenterWebService
from services.zhongshan_sports_center_webservice import ZhongshanSportsCenterWebService
from services.zhongzheng_sports_center_webservice import ZhongzhengSportsCenterWebService
from utils.input_helper import (
    get_valid_input,
    parse_input_booking_periods_str,
    transform_yes_no_input,
    check_if_target_datetime_is_outdated,
    transform_offset_milliseconds_param,
    cast_court_no_to_int_and_check_is_valid,
)

BOOKING_WEEKDAY = 4  # 填上星期幾搶場地
UPCOMING_BOOKING_DATE = (
    datetime.today()
    + timedelta(days=((BOOKING_WEEKDAY - datetime.today().isoweekday()) % 7))
).replace(hour=0, minute=0, second=0, microsecond=0)  # 這次搶場地的時間
FIRST_BOOKING_DATE = (UPCOMING_BOOKING_DATE + timedelta(days=14)).replace(hour=20)
SECOND_BOOKING_DATE = (UPCOMING_BOOKING_DATE + timedelta(days=14)).replace(hour=21)
WEBSERVICE_MAPPING = {
    0: ZhongshanSportsCenterWebService,
    1: ZhongzhengSportsCenterWebService
}


async def main():
    """搶球場主程式的進入點，倒數計時後搶球場"""
    set_logger()
    
    courts_list_message = ""
    for court_no, court_service in WEBSERVICE_MAPPING.items():
        courts_list_message += f"{court_service.sports_center_name()} -> {court_no}\n"

    input_court_no = get_valid_input(
        prompt=f"\n{courts_list_message}請輸入編號指定要預約的運動中心，運動中心編號清單如上：",
        transform_func=lambda x: cast_court_no_to_int_and_check_is_valid(
                input_court_no=x,
                mapping_dict=WEBSERVICE_MAPPING
            ),
        error_hint="請輸入正確的運動中心編號"
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
            error_hint="輸入的偏移豪秒數不正確，請重新輸入"
        )
        upcoming_booking_date = UPCOMING_BOOKING_DATE + timedelta(milliseconds=offset_milliseconds)
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

    # 時間倒數至開始搶票前的指定時間，再開始登入動作，避免登入太久導致 session 過期
    count_down(booking_date=upcoming_booking_date, offset=timedelta(minutes=-3))

    webservice = webservice_factory(court_no=input_court_no)
    with webservice(
        username=national_id, password=password
    ) as service:
        if service.login_status:
            cookies = service.get_cookies()
            async with aiohttp.ClientSession(cookies=cookies) as session:
                # 時間倒數至開始搶票的時間
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


def count_down(booking_date: datetime, offset: timedelta = timedelta()) -> None:
    """Count down to the target_time (which is booking_date plus offset timedelta),
    but always show the remaining seconds to the specified booking date.

    Args:
        booking_date (datetime): specified date to book the court
        offset (timedelta, optional): _description_. Defaults to timedelta().
    """
    current_time = datetime.now()
    count_down_target_time = booking_date + offset
    while current_time < count_down_target_time:
        if current_time.microsecond == 0:
            delta_seconds = (booking_date - current_time).seconds
            if delta_seconds >= 10 and delta_seconds % 5 == 0:
                logging.info("倒數 %d 秒", delta_seconds)
            elif delta_seconds < 10:
                logging.info("倒數 %d 秒", delta_seconds)
            else:
                pass

        current_time = datetime.now()


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
