"""Service to interacte with Zhongshan Sports Center Website"""

import logging

import aiohttp
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait


class ZhongshanSportsCenterWebService:
    def __init__(self, username: str, password: str) -> None:
        self.__is_login = False
        self.__username = username
        self.__password = password
        options = self.get_default_chrome_options()

        logging.info("開啟 Chrome 瀏覽器")
        self.__driver = webdriver.Chrome(options=options)

        logging.info("開啟中山運動中心登入頁")
        self.__driver.get(
            "https://scr.cyc.org.tw/tp01.aspx?module=login_page&files=login"
        )

    def get_default_chrome_options(self) -> Options:
        """Setting default chrome browser options and return

        Returns:
            Options: Chrome options object
        """
        options = webdriver.ChromeOptions()

        # run chrome browser without UI
        options.add_argument("--headless")

        # 模擬真實瀏覽器
        options.add_argument("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")

        return options

    def __del__(self) -> None:
        logging.info("關閉瀏覽器")
        self.__driver.quit()

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.logout()

    def login(self) -> None:
        """輸入帳密並且登入網路預約平台
        """
        if self.__is_login:
            welcome_message = self.__driver.find_element(
                By.XPATH, "//span[@id='lab_Name']"
            )
            logging.error("%s，您已經登入中山運動中心網路預約系統", welcome_message)

            return

        wait = WebDriverWait(self.__driver, timeout=2)
        alert = wait.until(lambda d: d.switch_to.alert)
        logging.debug("第一個彈出視窗訊息： %s", alert.text)
        alert.accept()

        wait = WebDriverWait(self.__driver, timeout=2)
        alert = wait.until(lambda d: d.switch_to.alert)
        logging.debug("第二個彈出視窗訊息： %s", alert.text)
        alert.accept()

        checkbox = self.__driver.find_element(By.CLASS_NAME, "swal2-actions")
        checkbox.click()

        logging.info("登入中...")
        username_input_box = self.__driver.find_element(
            By.ID, "ContentPlaceHolder1_loginid"
        )
        password_input_box = self.__driver.find_element(By.ID, "loginpw")
        username_input_box.send_keys(self.__username)
        password_input_box.send_keys(self.__password)

        try:
            self.__driver.execute_script("DoSubmit()")

            welcome_message = self.__driver.find_element(
                By.XPATH, "//span[@id='lab_Name']"
            )
            self.__is_login = True
            logging.info("%s 登入成功!", welcome_message.text)
        except Exception:
            login_fail_element = self.__driver.find_element(By.ID, "showerror3")
            logging.error("%s", login_fail_element.text)
            self.__is_login = False

    def logout(self) -> None:
        """登出網路預約平台
        """
        if not self.__is_login:
            logging.error("已是登出狀態")

            return

        # 找到包含登出文字的 <a> 元素
        logout_button = self.__driver.find_element(
            By.XPATH, "//a[span[text()='[登出]']]"
        )
        # 點擊登出按鈕，觸發 onclick 事件
        logout_button.click()
        # 等待頁面加載或跳轉
        self.__driver.implicitly_wait(
            5
        )  # TODO 可以調整等待時間，或者使用 WebDriverWait 來精確控制

        # 讀取重導向的登入頁面看是否有找到登入鈕來確認有確實登出
        member_login = self.__driver.find_element(By.ID, "member_login")
        if member_login.text == "會員註冊/登入":
            logging.info("登出成功")
            self.__is_login = False
        else:
            logging.info("登出失敗")

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

        cookies = self.__driver.get_cookies()
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
        booking_url = f"https://scr.cyc.org.tw/tp01.aspx?module=net_booking&files=booking_place&StepFlag=25&QPid=84&QTime={str(hour).zfill(2)}&PT=1&D={year}/{str(month).zfill(2)}/{str(day).zfill(2)}"

        async with session.get(booking_url, ssl=False) as response:
            text = await response.text()

            if "PT=1&X=2" in text:
                logging.info(
                    "%d/%d/%d %d ~ %d 的場地預約失敗", year, month, day, hour, hour + 1
                )
            elif "PT=1&X=1" in text:
                logging.info(
                    "%d/%d/%d %d ~ %d 的場地預約成功", year, month, day, hour, hour + 1
                )
            else:
                raise RuntimeError(f"搶場地未知的結果:\n {text}")
