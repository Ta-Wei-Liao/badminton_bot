import logging
from typing import Callable, Any


def get_valid_input(prompt: str, transform_func: Callable, error_hint: str = "") -> Any:
    while True:
        try:
            input_param = transform_func(input(prompt))
            break
        except (ValueError, AssertionError):
            logging.error(error_hint)

    return input_param
