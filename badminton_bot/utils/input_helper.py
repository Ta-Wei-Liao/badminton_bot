from datetime import datetime
import logging
from typing import Callable, Any


def get_valid_input(prompt: str, transform_func: Callable, error_hint: str = "") -> Any:
    while True:
        try:
            input_param = transform_func(input(prompt))
            break
        except (ValueError, AssertionError) as e:
            logging.error(e)
            logging.error(error_hint)

    return input_param


def transform_yes_no_input(yes_no_input: str) -> bool:
    """transform input Y or N string to boolean value

    Args:
        yes_no_input (str): the input string from user

    Returns:
        bool: if input Y then return True, if input N then return False
    """
    assert yes_no_input in ("Y", "N"), "輸入不是 Y/N"

    return yes_no_input == "Y"


def parse_input_booking_periods_str(
    input_booking_periods_str: str,
) -> tuple[datetime, ...]:
    """parse multiple datetime formatted strings seperated by comma to tuple of datetime objects

    Args:
        input_booking_periods_str (str): user input string representing multiple datetime

    Returns:
        tuple[datetime, ...]: transformed multiple datetime objects in tuple
    """
    datetime_list = []

    for date_str in input_booking_periods_str.split(","):
        tmp_datetime = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")
        datetime_list.append(check_if_target_datetime_is_outdated(target_datetime=tmp_datetime))

    return tuple(datetime_list)


def check_if_target_datetime_is_outdated(target_datetime: datetime) -> datetime:
    """check if the input datetime is in the pass and raise error

    Args:
        target_datetime (datetime): the datetime object to be check

    Raises:
        ValueError: if the input datetime is in the pass, raise this error

    Returns:
        datetime: the original input datetime
    """
    if target_datetime < datetime.now():
        raise ValueError("目標時間早於當下時間")
    
    return target_datetime


def transform_offset_milliseconds_param(input_milliseconds_param: str) -> int:
    """transform input into valid milliseconds param and raise error if invalid

    Args:
        input_milliseconds_param (str): input milliseconds parameter

    Raises:
        ValueError: if input can't be casted into int, raise this error
        ValueError: if input is not between -1000 and 1000, raise this error

    Returns:
        int: casted int value of milliseconds
    """
    if input_milliseconds_param == "":
        return 0
    
    try:
        milliseconds = int(input_milliseconds_param)
    except Exception:
        raise ValueError("輸入的偏移豪秒數不是有效的整數")
    
    if milliseconds < -1000 or milliseconds > 1000:
        raise ValueError("輸入的偏移豪秒數必須介於 -1000 ~ 1000 之間")
    
    return milliseconds


def cast_court_no_to_int_and_check_is_valid(input_court_no: str, mapping_dict: dict) -> int:
    try:
        input_court_no = int(input_court_no)
    except ValueError:
        raise ValueError("輸入參數必須為阿拉伯數字")
    
    if input_court_no in mapping_dict:
        return input_court_no
    else:
        raise ValueError(f"輸入的運動中心編號 {input_court_no} 無效")
