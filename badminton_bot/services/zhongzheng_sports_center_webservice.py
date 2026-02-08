"""Service to interacte with Zhongshan Sports Center Website"""

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from .sports_center_webservice import SportsCenterWebService


class ZhongzhengSportsCenterWebService(SportsCenterWebService):
    def __init__(self, username: str, password: str) -> None:
        super().__init__(username=username, password=password)

    @classmethod
    def sports_center_name(self) -> str:
        return "中正運動中心"

    @property
    def login_page_url(self) -> str:
        return "https://bwd.xuanen.com.tw/wd27.aspx?module=login_page&files=login"

    def _get_login_user_name_from_website(self) -> WebElement:
        return self._driver.find_element(By.XPATH, "//span[@id='lab_Name']")

    def _find_checkbox_element(self) -> WebElement:
        return self._driver.find_element(By.CLASS_NAME, "swal2-actions")

    def _find_username_input_box_element(self) -> WebElement:
        return self._driver.find_element(By.ID, "ContentPlaceHolder1_loginid")

    def _find_password_input_box_element(self) -> WebElement:
        return self._driver.find_element(By.ID, "loginpw")

    def _get_login_failed_message(self) -> WebElement:
        return self._driver.find_element(By.ID, "showerror3")

    def _get_logout_button(self) -> WebElement:
        # 找到包含登出文字的 <a> 元素
        return self._driver.find_element(By.XPATH, "//a[span[text()='[登出]']]")

    def _is_logout_success(self) -> bool:
        # 讀取重導向的登入頁面看是否有找到登入鈕來確認有確實登出
        member_login = self._driver.find_element(By.ID, "member_login")
        if member_login.text == "會員註冊/登入":
            return True
        else:
            return False

    def _generate_booking_url(self, year: int, month: int, day: int, hour: int) -> str:
        # 產生搶場地 url
        return f"https://bwd.xuanen.com.tw/wd27.aspx?module=net_booking&files=booking_place&StepFlag=25&QPid=1199&QTime={str(hour)}&PT=1&D={year}/{str(month).zfill(2)}/{str(day).zfill(2)}"

    def _is_booking_success(self, text: str) -> bool:
        if "PT=1&X=2" in text:
            return False
        elif "PT=1&X=1" in text:
            return True
        else:
            raise RuntimeError(f"搶場地未知的結果:\n {text}")
