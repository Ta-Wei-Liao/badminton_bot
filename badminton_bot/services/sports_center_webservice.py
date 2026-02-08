"""Service to interacte with Sports Center Website"""

import logging
from abc import ABC, abstractmethod

import aiohttp
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait


class SportsCenterWebService(ABC):
    def __init__(self, username: str, password: str) -> None:
        self.__is_login = False
        self.__username = username
        self.__password = password
        options = self.get_default_chrome_options()

        logging.info("開啟 Chrome 瀏覽器")
        self._driver = webdriver.Chrome(options=options)

        logging.info("開啟%s登入頁", self.sports_center_name())
        self._driver.get(self.login_page_url)

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

    @classmethod
    @abstractmethod
    def sports_center_name(self) -> str:
        """Return the sports center's name

        Returns:
            str: Sport center name
        """
        pass

    @property
    @abstractmethod
    def login_page_url(self) -> str:
        """Return the sports center's login page url

        Returns:
            str: Sports center login page url
        """
        pass

    def __del__(self) -> None:
        logging.info("關閉瀏覽器")
        self._driver.quit()

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.logout()

    def login(self) -> None:
        """輸入帳密並且登入網路預約平台"""
        if self.__is_login:
            welcome_message = self._get_login_user_name_from_website()
            logging.error(
                "%s，您已經登入%s網路預約系統", welcome_message, self.sports_center_name
            )

            return

        wait = WebDriverWait(self._driver, timeout=2)
        alert = wait.until(lambda d: d.switch_to.alert)
        logging.debug("第一個彈出視窗訊息： %s", alert.text)
        alert.accept()

        wait = WebDriverWait(self._driver, timeout=2)
        alert = wait.until(lambda d: d.switch_to.alert)
        logging.debug("第二個彈出視窗訊息： %s", alert.text)
        alert.accept()

        checkbox = self._find_checkbox_element()
        checkbox.click()

        logging.info("登入中...")
        username_input_box = self._find_username_input_box_element()
        password_input_box = self._find_password_input_box_element()
        username_input_box.send_keys(self.__username)
        password_input_box.send_keys(self.__password)

        try:
            self._driver.execute_script("DoSubmit()")

            welcome_message = self._get_login_user_name_from_website()
            self.__is_login = True
            logging.info("%s 登入成功!", welcome_message.text)
        except Exception:
            login_fail_element = self._get_login_failed_message()
            logging.error("%s", login_fail_element.text)
            self.__is_login = False

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
