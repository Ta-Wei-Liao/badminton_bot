# 可分享的視窗版搶場地程式 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把終端機版搶場地程式改成球友雙擊就能用的 tkinter 視窗程式：中山停用、中正可多選場地×時段（上限 12）、開搶日依按下開始的時刻自動決定，並由 GitHub Actions 產出 Mac／Windows 下載檔。

**Architecture:** 純邏輯（開搶時刻、組合展開、上限）放 `booking_plan.py`；原 `main.py` 的整套時序搬進 `engine.py`，包成不碰 UI 的 `GrabRunner`；`gui.py` 在主執行緒跑 Tk、在背景執行緒跑 `GrabRunner`，log 經 queue 回到畫面。service 層從單一 `target_qpid` 改為場地清單，網址與解析都改吃 `qpid` 參數。

**Tech Stack:** Python 3.11、tkinter、asyncio/aiohttp、Selenium、PyInstaller 6.12、GitHub Actions、pytest。

**Spec:** `docs/superpowers/specs/2026-11-14-shareable-gui-flexible-booking-design.md`

## Global Constraints

- 中正場地只開放五片：羽球7-1=1196、羽球7-2=1197、羽球7-3=1198、羽球7-4=1199、羽球7-5=1200。
- 時段起始小時 6～21（共 16 格），中正 `QTime` 不補零。
- 同時送出的請求數上限 `MAX_REQUESTS = 12`，GUI 擋、引擎再擋一次。
- 開搶時刻 = 按下開始後第一個**台北時間**（固定 `timezone(timedelta(hours=8))`）午夜；預約日期 = 開搶日 + `booking_window_days`（中正 7）。
- 距離開搶不到 4 分鐘仍執行，但顯示「距離開搶太近，校時可能不準」。
- 中山顯示為「中山運動中心（暫時停止）」，不可選，引擎拒絕執行。
- 沒有重試、沒有迴圈、沒有優先順序；不碰 Cloudflare Turnstile。
- 密碼永遠不進 logging、不寫檔；`GrabConfig.password` 用 `field(repr=False)`。
- log 目錄：`Path.home() / "Documents" / "badminton_bot_logs"`。
- 開發模式只在環境變數 `BADMINTON_BOT_DEV=1` 時出現。
- UI 字串、log、註解用繁體中文；docstring 與識別字用英文。
- 測試全部離線，不得對實站送任何請求（CLAUDE.md 實站限制）。
- commit 用 conventional-commit 前綴，**不加 `Co-Authored-By`**；`gh` 一律加 `--repo Ta-Wei-Liao/badminton_bot`。
- 分支：`feature_shareable_gui`，完成後 PR 回 `master`。

## Review Focus

1. **Windows `--windowed` 打包後 `sys.stderr is None`**：`set_logger` 不能加 StreamHandler，否則每行 log 都出錯。→ Task 4 `test_set_logger_skips_console_when_stderr_is_missing`。
2. **剛過午夜才按開始（00:00:30）**：要對準「下一個」午夜，不能對準剛過去的那個而立刻開搶。→ Task 3 `test_just_after_midnight_targets_the_next_midnight`。
3. **球友電腦時區不是台北**：開搶 epoch 與網址裡的日期都必須照台北時間。→ Task 3 `test_open_at_ignores_the_machine_timezone`。
4. **請求送出後才按取消**：不能把飛行中的請求取消掉，`cancel()` 回傳 False 且結果照常回來。→ Task 4 `test_cancel_after_fire_is_refused`。
5. **流程中途出例外（登入失敗、Chrome 掛掉）**：防睡眠一定要釋放。→ Task 6 `test_releases_even_when_the_body_raises`。

---

### Task 1: service 層改為多場地

**Files:**
- Modify: `badminton_bot/services/sports_center_webservice.py`
- Modify: `badminton_bot/services/zhongzheng_sports_center_webservice.py`
- Modify: `badminton_bot/services/zhongshan_sports_center_webservice.py`
- Test: `tests/test_sports_center_webservice.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Court: name: str; qpid: int`（定義在 `sports_center_webservice.py`）
  - 類別屬性 `courts: tuple[Court, ...]` 取代 `target_qpid`；`__init_subclass__` 必要屬性改為 `sport_center_name, login_page_url, booking_window_days, courts`
  - `ZhongzhengSportsCenterWebService.courts` = 五片（見 Global Constraints）；`ZhongshanSportsCenterWebService.courts = (Court("中山羽球場", 84),)`
  - `_generate_booking_url(self, year: int, month: int, day: int, hour: int, qpid: int) -> str`
  - `parse_slot_state(self, html: str, hour: int, qpid: int) -> str`
  - `BookingAttempt` 末尾新增 `court_name: str = ""`、`qpid: int = 0`
  - `booking_courts(self, session, booking_url: str, hour: int, ready_event: asyncio.Event, court: Court) -> BookingAttempt`（回傳值填入 court 的 name/qpid；log 改成「羽球7-4 20 ~ 21 點的場地預約成功」）

- [ ] **Step 0: 建分支並 commit 設計與計畫**

```bash
git checkout -b feature_shareable_gui
git add docs/superpowers/specs/2026-11-14-shareable-gui-flexible-booking-design.md docs/superpowers/plans/2026-11-14-shareable-gui-flexible-booking.md
git commit -m "docs: 可分享視窗版的設計與實作計畫"
```

- [ ] **Step 1: 改寫既有測試、加新測試**

把所有 `target_qpid` 相關測試改成 `courts`（`test_shipped_services_declare_every_required_attribute` 的屬性清單、`test_a_subclass_without_target_qpid_is_rejected_at_import_time` 改名為 `..._without_courts_...` 並 match `"courts"`、`test_each_center_targets_its_own_venue_id` 改成傳入 qpid）。所有呼叫 `_generate_booking_url` / `parse_slot_state` 的測試補上 `qpid=`。新增：

```python
def test_zhongzheng_offers_exactly_the_five_7_courts():
    assert ZhongzhengSportsCenterWebService.courts == (
        Court("羽球7-1", 1196), Court("羽球7-2", 1197), Court("羽球7-3", 1198),
        Court("羽球7-4", 1199), Court("羽球7-5", 1200),
    )

def test_booking_url_uses_the_qpid_it_is_given():
    service = build_without_browser(ZhongzhengSportsCenterWebService)
    url = service._generate_booking_url(year=2026, month=11, day=26, hour=20, qpid=1197)
    assert "QPid=1197&QTime=20&" in url and "StepFlag=25" in url and "D=2026/11/26" in url

def test_slot_state_is_looked_up_per_court():
    service = build_without_browser(ZhongzhengSportsCenterWebService)
    html = "Step3Action(1196,20) Step3Action(1199,21)"
    assert service.parse_slot_state(html, hour=20, qpid=1196) == SLOT_AVAILABLE
    assert service.parse_slot_state(html, hour=20, qpid=1199) == SLOT_TAKEN
    assert service.parse_slot_state(html, hour=20, qpid=1200) == SLOT_UNKNOWN

def test_booking_attempt_carries_the_court():  # 用既有的 RecordingSession / FakeBookingResponse
    attempt = asyncio.run(...booking_courts(..., court=Court("羽球7-2", 1197)))
    assert (attempt.court_name, attempt.qpid) == ("羽球7-2", 1197)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_sports_center_webservice.py -v`
Expected: FAIL（`Court` 不存在、`qpid` 參數不存在）

- [ ] **Step 3: 實作上述 Interfaces**

`parse_slot_state` 內的 `qpid = type(self).target_qpid` 改用參數，其餘正則不變。

- [ ] **Step 4: 跑全部測試**

Run: `pytest tests/test_sports_center_webservice.py -v`
Expected: PASS。`tests/test_main.py` 此時會因 `main.py` 仍呼叫舊簽章而壞——**暫時在 `main.py` 把呼叫補上 `qpid=service.courts[0].qpid` / `court=service.courts[0]` 讓它維持可跑**，Task 4 會整個換掉。`pytest` 全綠。

- [ ] **Step 5: Commit**

```bash
git add badminton_bot/services tests/test_sports_center_webservice.py badminton_bot/main.py
git commit -m "refactor: service 從單一 target_qpid 改為場地清單，網址與解析改吃 qpid"
```

---

### Task 2: 預熱 N 條連線、放大連線池

**Files:**
- Modify: `badminton_bot/services/sports_center_webservice.py`
- Modify: `badminton_bot/services/zhongzheng_sports_center_webservice.py`、`zhongshan_sports_center_webservice.py`
- Test: `tests/test_sports_center_webservice.py`

**Interfaces:**
- Produces:
  - `CONNECTION_LIMIT = 20`（必須 > 12；註解說明理由）
  - `_static_asset_urls(self) -> tuple[str, ...]`：中正回傳 spec §4.4 列的 15 個完整網址（`https://bwd.xuanen.com.tw` + 路徑，順序照 spec）；中山維持原本兩個
  - `_generate_warm_up_urls(self, count: int) -> tuple[str, ...]`：回傳前 `max(2, min(count, len(assets)))` 個
  - `warm_up(self, session, count: int) -> list[WarmUpResult]`

- [ ] **Step 1: 寫測試**（既有 `test_warms_two_different_pages`、`test_opens_both_pages_concurrently` 改為傳 `count=2`）

```python
def test_warms_one_distinct_asset_per_request():
    urls = build_without_browser(ZhongzhengSportsCenterWebService)._generate_warm_up_urls(count=12)
    assert len(urls) == 12 and len(set(urls)) == 12
    assert all(u.startswith("https://bwd.xuanen.com.tw/") and "StepFlag" not in u for u in urls)

def test_never_warms_fewer_than_two():
    assert len(build_without_browser(ZhongzhengSportsCenterWebService)._generate_warm_up_urls(count=1)) == 2

def test_caps_at_the_assets_available():
    assert len(build_without_browser(ZhongshanSportsCenterWebService)._generate_warm_up_urls(count=12)) == 2

def test_pool_can_hold_every_request_at_once():
    from badminton_bot.services.sports_center_webservice import CONNECTION_LIMIT
    assert CONNECTION_LIMIT > 12
```

- [ ] **Step 2: 跑測試確認失敗** — `pytest tests/test_sports_center_webservice.py -k "warm or pool" -v`，Expected: FAIL
- [ ] **Step 3: 實作**；`main.py` 的 `service.warm_up(session=session)` 暫時補 `count=2`。
- [ ] **Step 4: `pytest`** — Expected: 全部 PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: 預熱連線數跟著請求數走，連線池放大到 20"`

---

### Task 3: 純邏輯 `booking_plan.py`

**Files:**
- Create: `badminton_bot/booking_plan.py`
- Test: `tests/test_booking_plan.py`

**Interfaces:**
- Consumes: `Court`（Task 1）
- Produces:
  - `TAIPEI = timezone(timedelta(hours=8))`、`BOOKABLE_HOURS = tuple(range(6, 22))`、`MAX_REQUESTS = 12`、`TOO_CLOSE = timedelta(minutes=4)`
  - `next_open_at(now: datetime) -> datetime`：`now` 必須是 aware；回傳台北時區的下一個 00:00（嚴格大於 now）
  - `booking_day(open_at: datetime, window_days: int) -> date`
  - `@dataclass(frozen=True) class BookingTarget: court: Court; start: datetime`（`start` 為台北 aware，屬性 `hour` 回傳 `start.hour`）
  - `expand_targets(courts: Sequence[Court], hours: Sequence[int], day: date) -> tuple[BookingTarget, ...]`：依 courts 外層、hours 內層排序
  - `validate_targets(targets: Sequence[BookingTarget]) -> None`：0 個 → `ValueError("請至少選一片場地和一個時段")`；> 12 → `ValueError("最多只能同時搶 12 格，目前選了 N 格")`
  - `@dataclass(frozen=True) class SelectionSummary: text: str; can_start: bool; warning: str | None`
  - `summarize(now: datetime, courts, hours, window_days: int, has_credentials: bool) -> SelectionSummary`：text 形如「已選 2 片 × 3 時段 ＝ 6 個請求（上限 12）｜將於 11/19（四）00:00 開搶，預約 11/26（四）的場地」；不能開始時 text 寫原因（未填帳密／未選／超過上限）。星期用「一二三四五六日」。

- [ ] **Step 1: 寫測試**

```python
T = TAIPEI
def test_wednesday_evening_targets_thursday_midnight():
    assert next_open_at(datetime(2026, 11, 18, 21, 0, tzinfo=T)) == datetime(2026, 11, 19, tzinfo=T)
def test_just_after_midnight_targets_the_next_midnight():
    assert next_open_at(datetime(2026, 11, 19, 0, 0, 30, tzinfo=T)) == datetime(2026, 11, 20, tzinfo=T)
def test_exactly_midnight_targets_the_next_one():
    assert next_open_at(datetime(2026, 11, 19, tzinfo=T)) == datetime(2026, 11, 20, tzinfo=T)
def test_open_at_ignores_the_machine_timezone():
    now_utc = datetime(2026, 11, 18, 15, 59, tzinfo=timezone.utc)   # 台北 23:59
    assert next_open_at(now_utc) == datetime(2026, 11, 19, tzinfo=T)
def test_crosses_month_and_year():
    assert next_open_at(datetime(2026, 12, 31, 22, tzinfo=T)) == datetime(2027, 1, 1, tzinfo=T)
def test_booking_day_is_a_week_later_for_zhongzheng():
    assert booking_day(datetime(2026, 11, 19, tzinfo=T), 7) == date(2026, 11, 26)
def test_expand_is_the_full_cartesian_product():
    ts = expand_targets([Court("a", 1), Court("b", 2)], [20, 21], date(2026, 11, 26))
    assert [(t.court.qpid, t.hour) for t in ts] == [(1, 20), (1, 21), (2, 20), (2, 21)]
    assert ts[0].start == datetime(2026, 11, 26, 20, tzinfo=T)
def test_twelve_is_allowed_thirteen_is_not():  # 3×4 OK；用 13 個 target 觸發 ValueError match "12"
def test_empty_selection_is_rejected():
def test_summary_blocks_start_without_credentials():
def test_summary_warns_when_too_close():  # now = 23:57 → warning == "距離開搶太近，校時可能不準", can_start True
def test_summary_text_names_both_dates():  # 週三 21:00 → text 含 "11/19（四）00:00" 與 "11/26（四）"
```

- [ ] **Step 2:** `pytest tests/test_booking_plan.py -v` → FAIL（模組不存在）
- [ ] **Step 3:** 實作。`next_open_at`：`local = now.astimezone(TAIPEI)`，取 `local.date() + 1 day` 的 00:00。
- [ ] **Step 4:** `pytest tests/test_booking_plan.py -v` → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: 開搶時刻、場地×時段展開與上限的純邏輯"`

---

### Task 4: 時序搬進 `engine.py`，包成 `GrabRunner`

**Files:**
- Create: `badminton_bot/engine.py`（`main.py` 裡除了 `main()` 以外的全部：常數、`report_slot_states`、`run_with_deadline`、`run_booking_race`、`report_attempts`、`set_logger`、`count_down`）
- Modify: `badminton_bot/utils/timing.py`（`sleep_then_spin` 加取消）
- Modify: `badminton_bot/main.py`（暫時只剩 `asyncio`-free 的入口殼；Task 7 改成開 GUI）
- Delete: `badminton_bot/utils/input_helper.py`、`tests/test_input_helper.py`
- Rename: `tests/test_main.py` → `tests/test_engine.py`（import 改為 `badminton_bot.engine`，`webservice_factory` 測試改為 `CENTER_OPTIONS` 測試）
- Test: `tests/test_engine.py`、`tests/test_timing.py`

**Interfaces:**
- Consumes: Task 1–3 全部
- Produces:
  - `timing.py`：`class Cancelled(Exception)`；`sleep_then_spin(target_epoch, spin_window=..., on_tick=None, cancel_event: threading.Event | None = None)`——只在**粗略睡眠階段**檢查 `cancel_event`，被設定就 raise `Cancelled`；busy-wait 階段不檢查
  - `engine.py`：
    - `LOG_DIR = Path.home() / "Documents" / "badminton_bot_logs"`
    - `@dataclass(frozen=True) class CenterOption: service_cls: type[SportsCenterWebService]; enabled: bool` 與 property `label`（停用時為 `f"{name}（暫時停止）"`）
    - `CENTER_OPTIONS: tuple[CenterOption, ...] = (CenterOption(Zhongzheng, True), CenterOption(Zhongshan, False))`
    - `@dataclass(frozen=True) class GrabConfig: service_cls; username: str; password: str = field(repr=False); open_at: datetime; targets: tuple[BookingTarget, ...]`
    - `class GrabRunner(config: GrabConfig, on_stage: Callable[[str], None] = lambda s: None)`：
      - `async def run(self) -> list[BookingAttempt]`：依序 `check_browser()`（Task 5 提供；本 task 先以模組層級名稱呼叫，測試 monkeypatch）→ `with keep_awake():`（Task 6；同樣先以名稱呼叫）→ 現有 `main()` 的時序，把 `booking_periods` 換成 `targets`。停用的中心或 `validate_targets` 不過 → `ValueError`。被取消 → raise `Cancelled`（離開前 `with service` 照常登出、關 Chrome）
      - `def cancel(self) -> bool`：執行緒安全；設定 `threading.Event`，並 `loop.call_soon_threadsafe(task.cancel)`；**放出請求後**一律回傳 False 且不做任何事
      - `property fired: bool`
    - `run_booking_race(service, session, targets: Sequence[BookingTarget], send_at_epoch: float, on_fired: Callable[[], None] | None = None) -> list[BookingAttempt]`（`ready_event.set()` 之前呼叫 `on_fired`）
    - `report_slot_states(service, html, targets, label)` 改為逐一 target 用 `parse_slot_state(html, hour, qpid)`
    - `count_down(booking_date: datetime, offset=timedelta(), cancel_event=None)`：aware datetime，`datetime.now(booking_date.tzinfo)`
    - `set_logger(log_dir=LOG_DIR, extra_handlers: Sequence[logging.Handler] = ()) -> Path`：`sys.stderr is None` 時不加 StreamHandler；`extra_handlers` 給 GUI 掛 queue handler
    - `on_stage` 的階段文字：「瀏覽器自我檢查」「等待登入」「登入中」「校時中」「開放前檢查」「預熱連線」「倒數」「已送出，等待回應」「事後檢查」「完成」
  - 預熱改為 `service.warm_up(session=session, count=len(targets))`；開放前／事後偵察抓的是 `targets[0].start` 那天（全部 target 同一天）

- [ ] **Step 1: 搬移並改 import**：`git mv tests/test_main.py tests/test_engine.py`，函式原封不動搬到 `engine.py`；刪 `input_helper.py` 與其測試。`pytest` → 既有測試只因簽章（targets）不同而失敗。
- [ ] **Step 2: 改既有 `TestRunBookingRace` 用 `BookingTarget`，並新增測試**

```python
# test_timing.py
def test_cancel_event_interrupts_the_coarse_sleep():
    ev = threading.Event(); threading.Timer(0.1, ev.set).start()
    with pytest.raises(Cancelled):
        sleep_then_spin(time.time() + 30, cancel_event=ev)

# test_engine.py — FakeService：繼承 ZhongzhengSportsCenterWebService 但覆寫 __init__（不開 Chrome）、
# login/logout/get_cookies/create_session（回傳記錄請求的假 session，回應 "PT=1&X=1"）。
# fixture 把 check_browser、keep_awake、query_clock_offset 換成假的，POST_CHECK_DELAY_SECONDS 設 0。
def test_runner_fires_every_target_after_open_at():  # 2 片 × 2 時段，open_at = now+2s
    assert len(booking_urls) == 4 and all(a.sent_epoch >= open_at.timestamp() for a in attempts)
    assert {(a.qpid, a.hour) for a in attempts} == {(1196,20),(1196,21),(1199,20),(1199,21)}
def test_runner_rejects_a_disabled_center():  # Zhongshan → ValueError，FakeService 從未被建立
def test_runner_rejects_thirteen_targets():
def test_cancel_before_login_never_opens_the_browser():  # open_at=now+10min；另一執行緒 0.2s 後 cancel() → Cancelled，服務未建立
def test_cancel_after_fire_is_refused():  # on_fired 回呼裡呼叫 runner.cancel() → False，4 個 attempt 照常回來
def test_password_never_appears_in_config_repr():
    assert "s3cret" not in repr(GrabConfig(..., password="s3cret", ...))
def test_set_logger_skips_console_when_stderr_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stderr", None); set_logger(tmp_path); logging.info("x")
    assert not any(type(h) is logging.StreamHandler for h in logging.getLogger().handlers)
def test_center_options_disable_zhongshan():
    assert [o.enabled for o in CENTER_OPTIONS] == [True, False]
    assert CENTER_OPTIONS[1].label == "中山運動中心（暫時停止）"
```

- [ ] **Step 3:** `pytest tests/test_engine.py tests/test_timing.py -v` → FAIL
- [ ] **Step 4: 實作。** `run()` 開頭記下 `asyncio.get_running_loop()` 與 `asyncio.current_task()`；`count_down` 一律傳入 `self._cancel_event`；`asyncio.CancelledError` 在 `run()` 最外層轉成 `Cancelled`。`main.py` 暫時只留 `if __name__ == "__main__": print("請改用視窗版")`（Task 7 換掉）。
- [ ] **Step 5:** `pytest` → 全部 PASS
- [ ] **Step 6: Commit** — `git commit -m "refactor: 時序搬到 engine.GrabRunner，支援多目標與取消，移除終端機提問"`

---

### Task 5: 瀏覽器自我檢查

**Files:**
- Modify: `badminton_bot/services/sports_center_webservice.py`
- Test: `tests/test_sports_center_webservice.py`

**Interfaces:**
- Produces: `class BrowserUnavailable(RuntimeError)`、`check_browser() -> None`（模組函式）：headless Chrome（與 `get_default_chrome_options` 同樣選項）→ `get("about:blank")` → `quit()`；任何例外包成 `BrowserUnavailable("找不到可用的 Google Chrome，請先安裝 Chrome 並確認網路連線")`，`quit()` 一定執行（若 driver 已建立）。`engine.py` 從這裡 import。

- [ ] **Step 1: 測試**（monkeypatch `webdriver.Chrome`）
  - `test_check_browser_quits_after_success`：假 driver 記錄 `get("about:blank")` 與 `quit()` 各一次
  - `test_check_browser_never_visits_a_booking_site`：唯一的 `get` 參數是 `"about:blank"`
  - `test_missing_chrome_becomes_browser_unavailable`：`Chrome` raise `WebDriverException` → `BrowserUnavailable`
  - `test_quit_runs_even_when_get_fails`
- [ ] **Step 2:** 跑 → FAIL；**Step 3:** 實作；**Step 4:** `pytest` → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: 按下開始先做瀏覽器自我檢查，不連預約網站"`

---

### Task 6: 防睡眠

**Files:**
- Create: `badminton_bot/utils/keep_awake.py`
- Test: `tests/test_keep_awake.py`

**Interfaces:**
- Produces: `@contextmanager def keep_awake() -> Iterator[None]`；`engine.py` 從這裡 import。
  - `sys.platform == "darwin"`：`subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])`，結束時 `terminate()`
  - `sys.platform == "win32"`：`ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)`，結束時 `SetThreadExecutionState(0x80000000)`
  - 其他平台或啟動失敗：`logging.warning("無法防止電腦睡眠：…")`，照常 yield，絕不 raise

- [ ] **Step 1: 測試**（monkeypatch `sys.platform`、`subprocess.Popen`、注入假 `windll`）
  - `test_mac_starts_and_stops_caffeinate`、`test_windows_sets_and_restores_execution_state`
  - `test_releases_even_when_the_body_raises`：body raise `RuntimeError` → 仍 terminate／還原，例外照常往外傳
  - `test_failure_to_start_only_warns`：`Popen` raise `FileNotFoundError` → 不 raise，有 warning
- [ ] **Step 2:** 跑 → FAIL；**Step 3:** 實作；**Step 4:** `pytest` → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: 搶場地期間防止電腦睡眠"`

---

### Task 7: tkinter 視窗

**Files:**
- Create: `badminton_bot/gui.py`
- Modify: `badminton_bot/main.py`（`from badminton_bot.gui import run_app`；`if __name__ == "__main__": run_app()`）
- Modify: `badminton_bot/__init__.py`（`__version__ = "1.0.0"`）
- Test: `tests/test_gui.py`（只測不需要畫面的部分）

**Interfaces:**
- Consumes: `CENTER_OPTIONS`、`GrabConfig`、`GrabRunner`、`set_logger`、`Cancelled`（Task 4）；`summarize`、`expand_targets`、`next_open_at`、`booking_day`、`BOOKABLE_HOURS`、`TAIPEI`（Task 3）；`BrowserUnavailable`（Task 5）
- Produces:
  - `class QueueLogHandler(logging.Handler)`：把格式化後的訊息放進 `queue.Queue`
  - `def is_dev_mode(environ: Mapping[str, str] = os.environ) -> bool`：`environ.get("BADMINTON_BOT_DEV") == "1"`
  - `def results_grid(attempts: Sequence[BookingAttempt]) -> dict[tuple[str, int], str]`：`(court_name, hour) → "✅" / "❌" / "⚠️"`（success True / False / None）
  - `def run_app() -> None`

- [ ] **Step 1: 本機環境（作者 2026-11-14 已決定）**

**絕對不要重裝或 `-f` 覆蓋 3.11.4**——它底下還有 `community-matcher`、`data-engineer-handbook`、`DataAPI`、`merge_customer_list` 四個別的專案環境。改為：

```bash
brew install tcl-tk@8                       # Python 3.11 不支援 Tk 9
pyenv install --list | grep -E '^\s*3\.11\.[0-9]+$' | tail -1   # 取最新 3.11 修補版，記為 X
pyenv install 3.11.X                        # python-build 會偵測 brew 的 tcl-tk；若沒偵測到，用
                                            # PYTHON_CONFIGURE_OPTS="--with-tcltk-includes=... --with-tcltk-libs=..." 指向 tcl-tk@8
pyenv virtualenv-delete -f badminton_bot    # 只刪本專案的環境
pyenv virtualenv 3.11.X badminton_bot
pyenv activate badminton_bot && pip install -r requirements.txt
```

Run: `python -c "import tkinter; tkinter.Tk().destroy(); print('ok')" && pytest`
Expected: `ok`，測試全綠。Task 9 要把 CLAUDE.md 的 `3.11.4` 改成 `3.11.X`。

- [ ] **Step 2: 測試**

```python
def test_dev_mode_needs_the_exact_flag():
    assert is_dev_mode({"BADMINTON_BOT_DEV": "1"}) and not is_dev_mode({}) and not is_dev_mode({"BADMINTON_BOT_DEV": "0"})
def test_results_grid_maps_each_outcome():  # 三種 success 值各一個 attempt
def test_queue_handler_never_receives_the_password():
    # set_logger(tmp_path, extra_handlers=[handler]) 後跑 FakeService 的完整 GrabRunner（沿用 Task 4 fixture），
    # 密碼 "s3cret" 不出現在 queue 任何一則訊息，也不出現在 log 檔
```

- [ ] **Step 3:** 跑 → FAIL；**Step 4: 實作 `gui.py`**，畫面照 spec §3：
  - 中心下拉用 `CENTER_OPTIONS`，停用項目選了也會被拉回中正（ttk.Combobox 不支援單項停用，用 `<<ComboboxSelected>>` 擋並提示）。
  - 密碼 `show="•"`。場地 5 個、時段 16 個 Checkbutton（時段排成 4×4）。
  - 任何欄位變動就呼叫 `summarize(datetime.now(TAIPEI), ...)` 更新摘要列與「開始」按鈕狀態；`warning` 以橘色顯示。
  - 開發模式多兩欄：開搶時刻 `YYYY-MM-DD HH:MM:SS`、預約日期 `YYYY-MM-DD`（皆解讀為台北時間）。
  - 按「開始」：鎖表單 → 背景 `threading.Thread(daemon=True)` 跑 `asyncio.run(runner.run())`；`on_stage` 與結果都透過 `root.after(0, ...)` 回到主執行緒。`root.after(100, poll)` 從 queue 取 log 顯示。
  - 最後 10 秒只更新倒數數字，不重繪其他元件（spec §5.1 的 GIL 注意事項）。
  - 「取消」呼叫 `runner.cancel()`；回傳 False 時顯示「請求已送出，無法取消」。關窗時若在執行中，`messagebox.askyesno("確定要放棄這次搶場地嗎？")`。
  - 結果：Treeview，列＝場地、欄＝時段，格子用 `results_grid`；下方「搶到 X / N 格」、log 路徑、「打開 log 資料夾」（Mac `open`、Windows `os.startfile`）。
  - `BrowserUnavailable` → `messagebox.showerror` 顯示其訊息、解鎖表單。
  - 視窗標題 `f"羽球場地搶訂 v{__version__}"`。
- [ ] **Step 5:** `pytest` → 全部 PASS
- [ ] **Step 6: 手動驗證（不碰實站）**：`python -m badminton_bot.main`，確認：中山選不了、勾 4 片 × 4 時段時「開始」反灰且寫出原因、摘要日期正確；填假帳密按開始 → 通過瀏覽器自我檢查、進入「等待登入」→ 按取消 → 表單解鎖、log 檔裡沒有密碼。**不要等到登入。**
- [ ] **Step 7: Commit** — `git commit -m "feat: tkinter 視窗介面"`

---

### Task 8: 打包與 GitHub Release

**Files:**
- Modify: `bundling_scripts.sh`
- Create: `.github/workflows/release.yml`

**Interfaces:**
- Produces: 打包名稱 `BadmintonBot`；產物 `BadmintonBot-macOS.zip`（內含 `BadmintonBot.app`）、`BadmintonBot-Windows.zip`（內含 `BadmintonBot/BadmintonBot.exe` 資料夾）

- [ ] **Step 1: `bundling_scripts.sh`**

```bash
pyinstaller badminton_bot/main.py --name BadmintonBot --windowed --paths . --collect-all selenium -y
```

- [ ] **Step 2: `release.yml`**：`on: push: tags: ["v*"]`；`permissions: contents: write`；matrix `os: [macos-14, windows-latest]`；步驟：checkout → `actions/setup-python@v5`（`python-version: "3.11"`）→ `pip install -r requirements.txt` → `pytest` → 同上的 pyinstaller 指令 → 壓縮（Mac：`ditto -c -k --keepParent dist/BadmintonBot.app BadmintonBot-macOS.zip`，保留 ad-hoc 簽章；Windows：`Compress-Archive -Path dist/BadmintonBot -DestinationPath BadmintonBot-Windows.zip`）→ `softprops/action-gh-release@v2` 上傳，release body 連到 README 的使用說明。
- [ ] **Step 3: 本機驗證**：備份 `dist/main/logs`（見記憶：重新打包會清空）後跑 `./bundling_scripts.sh`；Expected: 產生 `dist/BadmintonBot.app`，雙擊開得起來、看得到表單，關掉。
- [ ] **Step 4: Commit** — `git commit -m "build: 視窗版打包與 GitHub Actions 自動發佈"`
- [ ] **Step 5: CI 驗證**（push 分支後）：`git tag v1.0.0-rc1 && git push origin v1.0.0-rc1`；`gh run watch --repo Ta-Wei-Liao/badminton_bot`；Expected: 兩個 job 綠、release 上有兩個 zip。驗證完把 rc release 與 tag 刪掉（`gh release delete v1.0.0-rc1 --cleanup-tag --repo Ta-Wei-Liao/badminton_bot`）。**push 與打 tag 前先問作者。**

---

### Task 9: 文件

**Files:**
- Modify: `README.md`（給球友的中文使用說明）
- Modify: `CLAUDE.md`

- [ ] **Step 1: README**：spec §6.4 的全部項目——下載（Release 頁連結）、解壓、Mac「系統設定 → 隱私權與安全性 → 強制打開」、Windows「其他資訊 → 仍要執行」、要裝 Chrome、插電不闔蓋、選場地時段與上限 12、怎麼看結果、log 在 `文件/badminton_bot_logs`、出事把 log 傳給作者。截圖位置留 `<!-- 截圖：... -->` 註記讓作者補。
- [ ] **Step 2: CLAUDE.md**：更新 Overview（GUI、不再是 `input()`）、Commands（`python -m badminton_bot.main` 開視窗、`BADMINTON_BOT_DEV=1`、`./bundling_scripts.sh` 新產物、打 tag 發佈）、Timing（移除 `BOOKING_WEEKDAY`，改述台北午夜規則）、Dev mode（改為環境變數＋兩個欄位）、Adding a sports center（`courts` 取代 `target_qpid`、`CENTER_OPTIONS` 取代 `WEBSERVICE_MAPPING`）、移除 `get_valid_input` 慣例、「hours are hardcoded」那句、log 位置、Turnstile 風險一句。若 Task 7 換了 Python 版本，同步更新版本號。
- [ ] **Step 3:** `pytest` → PASS
- [ ] **Step 4: Commit** — `git commit -m "docs: 球友使用說明與 CLAUDE.md 更新"`
