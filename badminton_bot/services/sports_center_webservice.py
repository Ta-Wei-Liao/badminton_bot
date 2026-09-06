"""Service to interacte with Sports Center Website"""

import asyncio
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
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

# 連線池上限。只需要兩條熱連線，留餘裕給預熱與探測。
CONNECTION_LIMIT = 10

# DNS 快取存活秒數，讓開搶那一刻不必再解析一次。
DNS_CACHE_SECONDS = 300

# aiohttp 預設 15 秒，短於探測階段的隨機間隔（12~25 秒）。沿用預設值的話，
# 連線會在兩次探測之間被回收，每次探測都得重新握手 —— 量到的 RTT 會被
# 握手成本汙染，無法代表開搶時那條熱連線，連帶讓 RTT/2 的補償失準。
KEEPALIVE_TIMEOUT_SECONDS = 60


@dataclass
class WarmUpResult:
    """What one warm-up request revealed about the connection."""

    url: str
    ok: bool
    connection: str | None = None
    keep_alive: str | None = None
    body: str | None = None


@dataclass
class BookingAttempt:
    """One booking request, with the timings needed to tune the next run."""

    hour: int
    url: str
    sent_epoch: float
    rtt: float
    server_date: str | None
    success: bool | None
    error: str | None

# Selenium 與 aiohttp 共用同一個 UA，否則登入與搶場地會用不同身分出現在對方的 log 裡。
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


def build_browser_headers(referer: str) -> dict[str, str]:
    """Assemble the headers an ordinary browser would send on this navigation.

    aiohttp's default User-Agent announces itself as a Python script. Note the
    trade-off: the TLS fingerprint and header order still say Python, so on a
    site with fingerprinting this is a claim that can be caught out. On a local
    sports centre's ASP.NET system that is very unlikely, and looking ordinary
    in the access log is worth more.

    Args:
        referer (str): the page this request would have been clicked from.

    Returns:
        dict[str, str]: headers for the aiohttp session.
    """
    return {
        "User-Agent": BROWSER_USER_AGENT,
        "Referer": referer,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
    }


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

        # 模擬真實瀏覽器。必須用 --user-agent= 這個旗標，
        # 直接丟裸字串進去 Chrome 不會當成 UA 設定。
        options.add_argument(f"--user-agent={BROWSER_USER_AGENT}")

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

    def create_session(
        self, cookies: dict[str, str], referer: str
    ) -> aiohttp.ClientSession:
        """Build the session the race runs on.

        An explicit connector is what makes warming up worthwhile: the default
        one would drop the pooled connection between probes.

        Args:
            cookies (dict[str, str]): the authenticated cookies from Selenium.
            referer (str): the page the booking requests would be clicked from.

        Returns:
            aiohttp.ClientSession: a session with pooled, browser-looking requests.
        """
        connector = aiohttp.TCPConnector(
            limit=CONNECTION_LIMIT,
            ttl_dns_cache=DNS_CACHE_SECONDS,
            keepalive_timeout=KEEPALIVE_TIMEOUT_SECONDS,
            ssl=False,
        )

        return aiohttp.ClientSession(
            connector=connector,
            cookies=cookies,
            headers=build_browser_headers(referer=referer),
        )

    async def warm_up(
        self, session, year: int, month: int, day: int
    ) -> list[WarmUpResult]:
        """Open two read-only pages so two hot connections wait in the pool.

        Everything the booking request would otherwise pay for at the opening
        instant — DNS, the TCP handshake, the TLS handshake — happens here.

        Args:
            session: the aiohttp session.
            year (int): the target booking year.
            month (int): the target booking month.
            day (int): the target booking day.

        Returns:
            list[WarmUpResult]: one entry per warm-up URL, in URL order.
        """
        urls = self._generate_warm_up_urls(year=year, month=month, day=day)

        async def _open(url: str) -> WarmUpResult:
            try:
                async with session.get(url) as response:
                    body = await response.text()
                    return WarmUpResult(
                        url=url,
                        ok=True,
                        connection=response.headers.get("Connection"),
                        keep_alive=response.headers.get("Keep-Alive"),
                        body=body,
                    )
            except Exception as error:
                logging.warning("預熱請求失敗（%s）：%s", url, error)
                return WarmUpResult(url=url, ok=False)

        results = await asyncio.gather(*(_open(url) for url in urls))

        for result in results:
            if result.ok:
                logging.info(
                    "預熱完成：Connection=%s Keep-Alive=%s",
                    result.connection or "（無）",
                    result.keep_alive or "（無）",
                )
                if (result.connection or "").lower() == "close":
                    logging.warning(
                        "伺服器要求關閉連線，預熱無效，開搶時仍需重新握手"
                    )

        return list(results)

    async def booking_courts(
        self,
        session,
        booking_url: str,
        hour: int,
        ready_event: asyncio.Event,
    ) -> BookingAttempt:
        """Fire one pre-built booking request the moment the event is set.

        The URL is built and this coroutine is scheduled well before the
        deadline, so the only work left at the opening instant is the send.

        Never raises. An unrecognised response is recorded in the result rather
        than thrown, because one request blowing up must not take the other
        booking down with it.

        Args:
            session: the aiohttp session.
            booking_url (str): the URL assembled ahead of time.
            hour (int): the hour being booked, for logging.
            ready_event (asyncio.Event): released at the send instant.

        Returns:
            BookingAttempt: the outcome and its timings.
        """
        await ready_event.wait()

        sent_epoch = time.time()
        started = time.perf_counter()

        try:
            async with session.get(booking_url) as response:
                text = await response.text()
                rtt = time.perf_counter() - started
                server_date = response.headers.get("Date")
        except Exception as error:
            logging.error("%d 點的場地請求失敗：%s", hour, error)
            return BookingAttempt(
                hour=hour,
                url=booking_url,
                sent_epoch=sent_epoch,
                rtt=time.perf_counter() - started,
                server_date=None,
                success=None,
                error=str(error),
            )

        try:
            success = self._is_booking_success(text=text)
            error = None
        except RuntimeError as runtime_error:
            success = None
            error = str(runtime_error)
            logging.error("%d 點的場地回應無法判讀：%s", hour, runtime_error)

        if success is True:
            logging.info("%d ~ %d 點的場地預約成功", hour, hour + 1)
        elif success is False:
            logging.info("%d ~ %d 點的場地預約失敗", hour, hour + 1)

        return BookingAttempt(
            hour=hour,
            url=booking_url,
            sent_epoch=sent_epoch,
            rtt=rtt,
            server_date=server_date,
            success=success,
            error=error,
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
        # 我們的場地出現在頁面上（任何時段皆可），才有資格說「已訂」；
        # 只是字串裡湊巧出現這個數字不算 —— 中山的 QPid=84 太短，
        # 很容易在圖檔名或 id 裡撞到，那會謊報成已訂。
        mentioned = re.compile(rf"Step3Action\(\s*{qpid}\s*,")

        if bookable.search(html):
            return SLOT_AVAILABLE

        if mentioned.search(html):
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
