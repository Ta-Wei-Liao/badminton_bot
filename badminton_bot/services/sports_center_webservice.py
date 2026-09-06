"""Service to interacte with Sports Center Website"""

import logging
import re
from abc import ABC, abstractmethod
from typing import Callable

import aiohttp
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait

# 登入頁的提醒視窗與元素都是延遲出現的。登入本身在開搶前三分鐘執行，等久一點不影響搶場地
LOGIN_WAIT_SECONDS = 10

SLOT_AVAILABLE = "可訂"
SLOT_TAKEN = "已訂"
SLOT_UNKNOWN = "未知"


class SportsCenterWebService(ABC):
    sport_center_name: str
    login_page_url: str
    booking_window_days: int
    target_qpid: int

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        if cls is SportsCenterWebService:
            return

        required_attrs = [
            "sport_center_name",
            "login_page_url",
            "booking_window_days",
            "target_qpid",
        ]

        for attr in required_attrs:
            if attr not in cls.__dict__:
                raise TypeError(f"{cls.__name__} must define class attribute '{attr}'")

    def __init__(self, username: str, password: str) -> None:
        cls = type(self)
        self.__is_login = False
        self.__username = username
        self.__password = password
        options = self.get_default_chrome_options()

        logging.info("開啟 Chrome 瀏覽器")
        self._driver = webdriver.Chrome(options=options)

        logging.info("開啟%s登入頁", cls.sport_center_name)
        self._driver.get(cls.login_page_url)

    def get_default_chrome_options(self) -> Options:
        """Setting default chrome browser options and return

        Returns:
            Options: Chrome options object
        """
        options = webdriver.ChromeOptions()

        # run chrome browser without UI
        options.add_argument("--headless")

        # 模擬真實瀏覽器
        options.add_argument(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        )

        return options

    def __del__(self) -> None:
        # 瀏覽器有可能還沒開起來就失敗，這時不需要（也無法）關閉，
        # 否則 __del__ 拋出的 AttributeError 會蓋掉原本真正的錯誤
        driver = getattr(self, "_driver", None)
        if driver is None:
            return

        logging.info("關閉瀏覽器")
        driver.quit()

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.logout()

    def login(self) -> None:
        """輸入帳密並且登入網路預約平台"""
        if self.__is_login:
            cls = type(self)
            welcome_message = self._get_login_user_name_from_website()
            logging.error(
                "%s，您已經登入%s網路預約系統", welcome_message, cls.sport_center_name
            )

            return

        # 兩個提醒視窗是頁面載入完才跳出來的（實測約 0.5 ~ 1 秒），所以要輪詢等待。
        # 不能用 lambda d: d.switch_to.alert，WebDriverWait 預設只忽略 NoSuchElementException，
        # alert 還沒跳出時丟的 NoAlertPresentException 會直接往外拋，等於只看了一次就放棄。
        for order in ("第一", "第二"):
            alert = WebDriverWait(self._driver, timeout=LOGIN_WAIT_SECONDS).until(
                expected_conditions.alert_is_present()
            )
            logging.debug("%s個彈出視窗訊息： %s", order, alert.text)
            alert.accept()

        # 詐騙提醒的 SweetAlert2 視窗是兩個 alert 關掉之後才渲染出來的（實測只差約 50 毫秒），
        # find_element 沒有隱含等待，立刻找會 NoSuchElementException
        checkbox = self._wait_for_element(
            self._find_checkbox_element, require_displayed=True
        )
        checkbox.click()

        logging.info("登入中...")
        username_input_box = self._find_username_input_box_element()
        password_input_box = self._find_password_input_box_element()
        username_input_box.send_keys(self.__username)
        password_input_box.send_keys(self.__password)

        try:
            self._driver.execute_script("DoSubmit()")

            # DoSubmit() 會重新導向頁面，歡迎訊息不會馬上出現在 DOM 裡，
            # 不等的話會誤判成登入失敗
            welcome_message = self._wait_for_element(
                self._get_login_user_name_from_website
            )
            self.__is_login = True
            logging.info("%s 登入成功!", welcome_message.text)
        except Exception:
            login_fail_element = self._get_login_failed_message()
            logging.error("%s", login_fail_element.text)
            self.__is_login = False

    def _wait_for_element(
        self,
        find_element_func: Callable[[], WebElement],
        require_displayed: bool = False,
    ) -> WebElement:
        """輪詢等待延遲渲染的元素出現後回傳

        WebDriverWait 預設就會忽略 NoSuchElementException，所以元素還沒渲染出來時會繼續等。

        Args:
            find_element_func (Callable[[], WebElement]): 回傳目標元素的函式
            require_displayed (bool, optional): 是否要等到元素可見（要點擊的元素才需要）。
                Defaults to False.

        Returns:
            WebElement: 已經出現的目標元素
        """

        def _found(driver) -> WebElement | bool:
            element = find_element_func()
            if require_displayed and not element.is_displayed():
                return False

            return element

        return WebDriverWait(self._driver, timeout=LOGIN_WAIT_SECONDS).until(_found)

    @abstractmethod
    def _get_login_user_name_from_website(self) -> str:
        pass

    @abstractmethod
    def _find_checkbox_element(self) -> WebElement:
        pass

    @abstractmethod
    def _find_username_input_box_element(self) -> WebElement:
        pass

    @abstractmethod
    def _find_password_input_box_element(self) -> WebElement:
        pass

    @abstractmethod
    def _get_login_failed_message(self) -> WebElement:
        pass

    def logout(self) -> None:
        """登出網路預約平台"""
        if not self.__is_login:
            logging.error("已是登出狀態")

            return

        logout_button = self._get_logout_button()
        # 點擊登出按鈕，觸發 onclick 事件
        logout_button.click()
        # 等待頁面加載或跳轉
        self._driver.implicitly_wait(
            5
        )  # TODO 可以調整等待時間，或者使用 WebDriverWait 來精確控制

        if self._is_logout_success():
            logging.info("登出成功")
            self.__is_login = False
        else:
            logging.info("登出失敗")

    @abstractmethod
    def _get_logout_button(self) -> WebElement:
        pass

    @abstractmethod
    def _is_logout_success(self) -> bool:
        pass

    @property
    def login_status(self) -> bool:
        return self.__is_login

    def get_cookies(self) -> dict[str, str] | None:
        """取得登入後 session 中的 cookies

        Returns:
            dict[str, str] | None: 未登入的話回傳空，有登入則回傳所有 cookies
        """
        if not self.__is_login:
            logging.error("未登入，無法取得 cookies")

        cookies = self._driver.get_cookies()
        return {cookie["name"]: cookie["value"] for cookie in cookies}

    async def booking_courts(
        self, session: aiohttp.ClientSession, year: int, month: int, day: int, hour: int
    ) -> None:
        """發出搶場地的請求，並且檢查回傳的內容中重導向的網址中的參數來判斷是否預約成功

        Args:
            session (aiohttp.ClientSession): 輸入登入資訊相關 cookies 的非同步 session
            year (int): 指定要搶的場地的年份
            month (int): 指定要搶的場地的月份
            day (int): 指定要搶的場地的日期
            hour (int): 指定要搶的場地的小時

        Raises:
            RuntimeError: 判斷不出來搶場地的結果時發出的例外
        """
        logging.info("搶 %d/%d/%d %d ~ %d 的場地", year, month, day, hour, hour + 1)

        # 產生搶場地 url
        booking_url = self._generate_booking_url(
            year=year, month=month, day=day, hour=hour
        )

        async with session.get(booking_url, ssl=False) as response:
            text = await response.text()

            if self._is_booking_success(text=text):
                logging.info(
                    "%d/%d/%d %d ~ %d 的場地預約成功", year, month, day, hour, hour + 1
                )
            else:
                logging.info(
                    "%d/%d/%d %d ~ %d 的場地預約失敗", year, month, day, hour, hour + 1
                )

    @abstractmethod
    def _generate_booking_url(self, year: int, month: int, day: int, hour: int) -> str:
        pass

    @abstractmethod
    def _is_booking_success(self, text: str) -> bool:
        pass

    @abstractmethod
    def _generate_list_page_url(self, year: int, month: int, day: int) -> str:
        """唯讀的場地列表頁（StepFlag=2）。探測、預熱與狀態偵察都用這一頁。"""

    def parse_slot_state(self, html: str, hour: int) -> str:
        """Read the target court's state for one hour off the read-only list page.

        A bookable cell carries the site's own click handler, Step3Action(QPid,
        QTime); a cell already taken does not. Both centres run the same ASP.NET
        platform, so one parser serves them both. The hour is matched with an
        optional leading zero because the two sites pad it differently.

        The distinction that matters is between "taken" and "unknown": before
        the booking window opens the page may not render that day at all, and
        reporting that as taken would be a lie.

        Args:
            html (str): the list page body.
            hour (int): the hour to look up.

        Returns:
            str: SLOT_AVAILABLE, SLOT_TAKEN or SLOT_UNKNOWN.
        """
        qpid = type(self).target_qpid
        bookable = re.compile(
            rf"Step3Action\(\s*{qpid}\s*,\s*0?{hour}\s*\)"
        )

        if bookable.search(html):
            return SLOT_AVAILABLE

        if str(qpid) in html:
            return SLOT_TAKEN

        return SLOT_UNKNOWN

    def _generate_warm_up_urls(
        self, year: int, month: int, day: int
    ) -> tuple[str, ...]:
        """Two read-only pages to open concurrently, leaving two hot connections.

        Two connections because the two booking requests each need one. Two
        *different* pages because a browser loading a page in parallel is
        ordinary traffic, whereas the same URL fetched twice at once is not.

        Returns:
            tuple[str, ...]: the list page and the login page.
        """
        return (
            self._generate_list_page_url(year=year, month=month, day=day),
            type(self).login_page_url,
        )
