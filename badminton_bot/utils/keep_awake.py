import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from typing import Iterator

# Windows SetThreadExecutionState 旗標
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _get_windll():
    """Lazily fetch ctypes.windll so importing this module works on any OS."""
    import ctypes

    return ctypes.windll


def _start_mac():
    proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    return proc.terminate


def _start_windows():
    kernel32 = _get_windll().kernel32
    kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    return lambda: kernel32.SetThreadExecutionState(ES_CONTINUOUS)


@contextmanager
def keep_awake() -> Iterator[None]:
    """Keep the computer from sleeping while the body runs; never raises on failure."""
    release = None
    try:
        if sys.platform == "darwin":
            release = _start_mac()
        elif sys.platform == "win32":
            release = _start_windows()
        else:
            logging.warning(f"無法防止電腦睡眠：不支援的平台 {sys.platform}")
    except Exception as e:
        logging.warning(f"無法防止電腦睡眠：{e}")
    try:
        yield
    finally:
        if release is not None:
            try:
                release()
            except Exception as e:
                logging.warning(f"還原睡眠設定失敗：{e}")
