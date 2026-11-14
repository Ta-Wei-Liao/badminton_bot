# Immediate Booking Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 「搶午夜／立即預約」 toggle; 立即預約 logs in and fires the selected court × hour requests at once for an already-open day.

**Architecture:** Pure date/hour/validation/summary rules go in `booking_plan.py`. `GrabConfig` gains a `mode`; `GrabRunner.run` branches to a short `_run_immediate` path (login → session → `run_booking_race` released now → logout) that skips NTP, clock probes, checks, warm-up and countdown. The GUI adds radio buttons, a day combobox, past-hour disabling, a reminder line and a confirm dialog.

**Tech Stack:** Python 3.11, tkinter, asyncio/aiohttp, Selenium, pytest.

**Spec:** `docs/superpowers/specs/2026-11-14-immediate-booking-mode-design.md`

## Global Constraints

- 搶午夜 behaviour, requests and timing stay exactly as in v1.0.2; every existing test keeps passing unchanged (except where a test constructs `GrabConfig` positionally — none do today).
- 立即預約 sends only: the login flow + N booking GETs + logout. No `query_clock_offset`, `measure_server_clock`, `fetch_list_page`, `warm_up`, `count_down`.
- Dates are Taipei time (`TAIPEI`), never the machine zone.
- Selectable days: today … today + `booking_window_days` (Zhongzheng 7 → 8 days). Default today.
- On today, an hour is selectable only if its start (`HH:00` Taipei) is strictly after now.
- `MAX_REQUESTS = 12` still caps courts × hours.
- Reminder text, verbatim: 「立即預約會真的訂下場地；請勿短時間內反覆送出，頻繁登入可能導致帳號被停權。」
- GUI text, logs, comments in Traditional Chinese; docstrings and identifiers in English.
- Errors formatted only via `describe_error`; never `repr()`/`%r` (enforced by `tests/test_errors.py`).
- The worker thread never touches Tk; it reports through the existing queues.
- Never contact the live site in tests. No `Co-Authored-By` trailers. Conventional-commit prefixes.
- Run git with `DEVELOPER_DIR=/Library/Developer/CommandLineTools` prefix on this Mac; tests with `~/.pyenv/versions/badminton_bot/bin/python -m pytest -q -W error`.

## Review Focus

1. Form left open across Taipei midnight → the day list shifts; a selected day that is now yesterday must reset to today, and 開始 must re-validate (Task 3 test `test_days_follow_the_clock`, Task 1 `validate_immediate` rejects yesterday).
2. Today selected and the clock passes the top of an hour while the form is open → that hour is unticked/disabled and 開始 refuses it (Task 1 `bookable_hours` boundary tests; Task 3 refresh on `_tick`).
3. Cancel pressed while 立即預約 is logging in → no booking GET is sent, logout still runs (Task 2 test `test_cancel_during_login_sends_nothing`).
4. Default `GrabConfig` (no `mode`) behaves exactly as 搶午夜 today (Task 2 test `test_default_mode_is_midnight`).
5. Dev mode on and 立即預約 selected → dev fields hidden and ignored (Task 3 manual check; `_schedule` branches on mode first).

---

### Task 1: Pure rules for 立即預約 in `booking_plan.py`

**Files:**
- Modify: `badminton_bot/booking_plan.py`
- Test: `tests/test_booking_plan.py`

**Interfaces:**
- Produces:
  - `class GrabMode(Enum)`: `MIDNIGHT = "搶午夜"`, `IMMEDIATE = "立即預約"` (value is the radio label).
  - `IMMEDIATE_REMINDER: str` — the verbatim reminder.
  - `immediate_days(now: datetime, window_days: int) -> tuple[date, ...]`
  - `bookable_hours(day: date, now: datetime) -> tuple[int, ...]` — subset of `BOOKABLE_HOURS`.
  - `describe_day(day: date, today: date) -> str`
  - `validate_immediate(targets: Sequence[BookingTarget], now: datetime, window_days: int) -> None` — raises `ValueError`.
  - `summarize_immediate(now, courts, hours, day, window_days, has_credentials) -> SelectionSummary`
  - `describe_confirmation(targets: Sequence[BookingTarget]) -> str`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_booking_plan.py`; import the new names)

```python
NOW = datetime(2026, 11, 14, 18, 20, tzinfo=T)  # 週六 18:20
C72 = Court("羽球7-2", 1197)


class TestImmediateRules:
    def test_mode_labels(self):
        assert [m.value for m in GrabMode] == ["搶午夜", "立即預約"]

    def test_days_run_from_today_through_the_window(self):
        days = immediate_days(NOW, 7)
        assert days[0] == date(2026, 11, 14)
        assert days[-1] == date(2026, 11, 21)
        assert len(days) == 8

    def test_days_use_taipei_time(self):
        now_utc = datetime(2026, 11, 14, 16, 30, tzinfo=timezone.utc)  # 台北 11/15 00:30
        assert immediate_days(now_utc, 7)[0] == date(2026, 11, 15)

    def test_today_offers_only_hours_that_have_not_started(self):
        assert bookable_hours(date(2026, 11, 14), NOW) == (19, 20, 21)

    def test_an_hour_starting_exactly_now_is_gone(self):
        assert 19 not in bookable_hours(date(2026, 11, 14), datetime(2026, 11, 14, 19, 0, tzinfo=T))

    def test_future_days_offer_every_hour(self):
        assert bookable_hours(date(2026, 11, 15), NOW) == BOOKABLE_HOURS

    def test_past_days_offer_nothing(self):
        assert bookable_hours(date(2026, 11, 13), NOW) == ()

    def test_describe_day(self):
        assert describe_day(date(2026, 11, 14), today=date(2026, 11, 14)) == "11/14（六）今天"
        assert describe_day(date(2026, 11, 16), today=date(2026, 11, 14)) == "11/16（一）"

    def test_validate_accepts_an_open_future_slot(self):
        validate_immediate(expand_targets([C72], [6], date(2026, 11, 16)), NOW, 7)

    def test_validate_rejects_a_started_hour(self):
        with pytest.raises(ValueError, match="已經過了"):
            validate_immediate(expand_targets([C72], [18], date(2026, 11, 14)), NOW, 7)

    def test_validate_rejects_days_outside_the_window(self):
        for day in (date(2026, 11, 13), date(2026, 11, 22)):
            with pytest.raises(ValueError, match="不在可預約"):
                validate_immediate(expand_targets([C72], [20], day), NOW, 7)

    def test_validate_keeps_the_request_cap(self):
        courts = [Court(f"c{i}", i) for i in range(5)]
        with pytest.raises(ValueError):
            validate_immediate(expand_targets(courts, [6, 7, 8], date(2026, 11, 16)), NOW, 7)

    def test_summary_text(self):
        result = summarize_immediate(NOW, [C72], [6], date(2026, 11, 16), 7, True)
        assert result.can_start
        assert result.warning is None
        assert result.text == "已選 1 片 × 1 時段 ＝ 1 個請求（上限 12）｜立即預約 11/16（一）的場地"

    def test_summary_refuses_without_credentials_or_selection(self):
        assert summarize_immediate(NOW, [C72], [6], date(2026, 11, 16), 7, False).text == "未填帳密"
        assert summarize_immediate(NOW, [], [6], date(2026, 11, 16), 7, True).can_start is False

    def test_summary_refuses_a_started_hour(self):
        result = summarize_immediate(NOW, [C72], [18], date(2026, 11, 14), 7, True)
        assert result.can_start is False

    def test_confirmation_lists_every_booking_and_warns(self):
        text = describe_confirmation(expand_targets([C72], [6, 7], date(2026, 11, 16)))
        assert "11/16（一）" in text
        assert "羽球7-2 06:00–07:00" in text
        assert "羽球7-2 07:00–08:00" in text
        assert "會真的預約" in text
        assert IMMEDIATE_REMINDER in text
```

- [ ] **Step 2: Run to verify failure**

Run: `~/.pyenv/versions/badminton_bot/bin/python -m pytest -q tests/test_booking_plan.py -k Immediate`
Expected: collection error, `ImportError: cannot import name 'GrabMode'`.

- [ ] **Step 3: Implement in `badminton_bot/booking_plan.py`**

Error messages: started hour → `f"{hour:02d}:00 的時段已經過了"`; day outside window → `"{_fmt(day)}不在可預約的日期範圍內"`. `validate_immediate` calls `validate_targets` first. `summarize_immediate` returns `("未填帳密", False, None)` / `("未選場地或時段", False, None)` / over-cap text same as `summarize` / for a started hour or bad day, the `ValueError` message with `can_start=False`; otherwise `f"{describe_selection(...)}｜立即預約 {_fmt(day)}的場地"`. `describe_confirmation` body:

```
將立即預約 11/16（一）的以下場地：
羽球7-2 06:00–07:00
羽球7-2 07:00–08:00

按下「確定」會真的預約這些場地。
立即預約會真的訂下場地；請勿短時間內反覆送出，頻繁登入可能導致帳號被停權。
```

- [ ] **Step 4: Run to verify pass** — same command, all pass; then the full suite.

- [ ] **Step 5: Commit** — `feat: add immediate booking rules to booking plan`

---

### Task 2: Engine — `GrabConfig.mode` and `_run_immediate`

**Files:**
- Modify: `badminton_bot/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `GrabMode`, `validate_immediate` (Task 1).
- Produces: `GrabConfig(service_cls, username, password, open_at: datetime | None, targets, mode: GrabMode = GrabMode.MIDNIGHT)`; `STAGE_SENDING = "送出中"`; `GrabRunner.run` dispatches on `config.mode`.

- [ ] **Step 1: Write the failing tests** (new class in `tests/test_engine.py`)

```python
def make_immediate_config(targets=None):
    day = (datetime.now(TAIPEI) + timedelta(days=2)).date()
    return GrabConfig(
        service_cls=FakeService,
        username="A123456789",
        password="s3cret",
        open_at=None,
        targets=make_targets(day=day) if targets is None else targets,
        mode=GrabMode.IMMEDIATE,
    )


class TestImmediateMode:
    def test_default_mode_is_midnight(self):
        assert make_config().mode is GrabMode.MIDNIGHT

    def test_fires_at_once_and_logs_out(self, offline_engine):
        stages: list[str] = []
        started = time.time()
        attempts = asyncio.run(GrabRunner(make_immediate_config(), on_stage=stages.append).run())
        assert time.time() - started < 2
        (service,) = FakeService.instances
        assert len(service.session.booking_urls) == 4
        assert all(a.success is True for a in attempts)
        assert service.logged_out
        assert stages == ["瀏覽器自我檢查", "登入中", "送出中", "已送出，等待回應", "完成"]

    def test_skips_every_timing_and_reconnaissance_step(self, offline_engine, monkeypatch):
        from badminton_bot import engine

        def forbidden(*args, **kwargs):
            raise AssertionError("立即預約不該呼叫這一步")

        monkeypatch.setattr(engine, "query_clock_offset", forbidden)
        monkeypatch.setattr(engine, "measure_server_clock", forbidden)
        monkeypatch.setattr(engine, "count_down", forbidden)
        monkeypatch.setattr(FakeService, "warm_up", forbidden)
        monkeypatch.setattr(FakeService, "fetch_list_page", forbidden)
        asyncio.run(GrabRunner(make_immediate_config()).run())
        (service,) = FakeService.instances
        assert len(service.session.booking_urls) == 4

    def test_rejects_a_started_hour(self, offline_engine):
        today = datetime.now(TAIPEI).date()
        targets = make_targets(hours=(6,), day=today - timedelta(days=1))
        with pytest.raises(ValueError):
            asyncio.run(GrabRunner(make_immediate_config(targets=targets)).run())
        assert FakeService.instances == []

    def test_cancel_during_login_sends_nothing(self, offline_engine, monkeypatch):
        runner_box = []

        def slow_login(self):
            runner_box[0].cancel()
            self.logged_in = True

        monkeypatch.setattr(FakeService, "login", slow_login)
        runner = GrabRunner(make_immediate_config())
        runner_box.append(runner)
        with pytest.raises(Cancelled):
            asyncio.run(runner.run())
        (service,) = FakeService.instances
        assert service.session.booking_urls == []
        assert service.logged_out
        assert runner.fired is False
```

`SportsCenterWebService.__enter__` calls `self.login()`, so patching `FakeService.login` is where the engine's `with config.service_cls(...)` path logs in. The cancel lands while login is running; the engine must notice it before releasing (`_mark_fired` already raises `Cancelled` when `_cancel_event` is set).

- [ ] **Step 2: Run to verify failure** — `pytest -q tests/test_engine.py -k Immediate` → `ImportError`/`TypeError: unexpected keyword 'mode'`.

- [ ] **Step 3: Implement**
  - `GrabConfig`: `open_at: datetime | None`, add `mode: GrabMode = GrabMode.MIDNIGHT` last.
  - `run()`: after the centre check, validate with `validate_immediate(config.targets, datetime.now(TAIPEI), config.service_cls.booking_window_days)` for IMMEDIATE, else `validate_targets`. Inside `keep_awake()`, call `_run_immediate()` or `_run_schedule()`.
  - `_run_immediate()`: stage `STAGE_LOGGING_IN`; if `_cancel_event` is set, mark delivered and raise `Cancelled`; `with config.service_cls(...) as service:` → if logged in: cookies, `create_session` with the list-page referer of the booking day, stage `STAGE_SENDING`, `send_at = time.time()`, `run_booking_race(..., send_at_epoch=send_at, on_fired=self._mark_fired, on_released=lambda: self._stage(STAGE_FIRED))`, `report_attempts(attempts, reference_epoch=send_at, source="未校時（立即預約）")`. Login failure → `LoginFailed` exactly as `_run_schedule`. Stage `STAGE_DONE`.
  - Log one line at start: `"立即預約：%s，共 %d 筆"` with the day (`_fmt`-style text from Task 1's `describe_day`) and count.

- [ ] **Step 4: Run** — new tests pass; full suite passes with `-W error`.

- [ ] **Step 5: Commit** — `feat: add immediate booking mode to the engine`

---

### Task 3: GUI — mode toggle, day picker, past-hour disabling, reminder, confirm

**Files:**
- Modify: `badminton_bot/gui.py`
- Test: `tests/test_gui.py`

**Interfaces:**
- Consumes: everything from Task 1; `GrabConfig.mode`, `STAGE_SENDING` (Task 2).
- Produces: `day_choices(now: datetime, window_days: int) -> list[tuple[str, date]]` (label, day) in `gui.py`, pure.

- [ ] **Step 1: Write the failing tests** (`tests/test_gui.py`)

```python
def test_day_choices_label_today():
    now = datetime(2026, 11, 14, 18, 20, tzinfo=TAIPEI)
    choices = day_choices(now, 7)
    assert choices[0] == ("11/14（六）今天", date(2026, 11, 14))
    assert len(choices) == 8


def test_days_follow_the_clock():
    """表單開著跨過午夜，選單要跟著變，昨天不能再出現。"""
    before = day_choices(datetime(2026, 11, 14, 23, 59, tzinfo=TAIPEI), 7)
    after = day_choices(datetime(2026, 11, 15, 0, 1, tzinfo=TAIPEI), 7)
    assert date(2026, 11, 14) in [d for _, d in before]
    assert date(2026, 11, 14) not in [d for _, d in after]


def test_status_without_open_time_shows_the_stage():
    assert status_text("登入中", remaining=0.0, fired=False) == "登入中"
```

- [ ] **Step 2: Run to verify failure** — `ImportError: day_choices`.

- [ ] **Step 3: Implement in `App`**
  - Row 0 of the form: two `ttk.Radiobutton`s bound to `self.mode = tk.StringVar(value=GrabMode.MIDNIGHT.value)`, added to `self.inputs`; trace → `_on_mode_changed` → show/hide widgets, `_refresh()`.
  - 立即預約 only: 「預約日期」 readonly `ttk.Combobox` filled from `day_choices(now, window_days)`, default index 0; reminder `ttk.Label` with `IMMEDIATE_REMINDER`, `foreground=WARNING_COLOUR`, `wraplength=520`. 搶午夜 only: the dev-mode fields (when `dev_mode`). Use `grid_remove()`/`grid()` so rows keep their place.
  - `_refresh()` in 立即預約: rebuild the combobox values from `day_choices` (if the selected day vanished, select index 0); for each hour checkbox, `state="normal"` if the hour is in `bookable_hours(day, now)` else untick and `"disabled"`; summary from `summarize_immediate`. `_set_form_state(locked=False)` must not re-enable a past hour (apply the hour rule after unlocking). `_tick` already calls `_refresh` once a second while idle.
  - `_schedule(now)`: branch on mode first — 立即預約 returns `(None, selected day)`; dev fields are read only in 搶午夜.
  - `_start()` in 立即預約: `messagebox.askokcancel("確認立即預約", describe_confirmation(targets), icon="warning")` (Tk's buttons are 確定/取消; the text tells them 確定 means 確定預約). On cancel return before touching the logger. Build `GrabConfig(..., open_at=None, mode=GrabMode.IMMEDIATE)`.
  - `_update_status()`: when `self.open_at is None` (立即預約), use `remaining = 0.0` so `status_text` shows the stage name; `_in_quiet_window` stays False.

- [ ] **Step 4: Run** — new tests pass; full suite passes. Then launch `python -m badminton_bot.main` (and with `BADMINTON_BOT_DEV=1`) and click through both modes **without pressing 開始/確定**; the person checks layout. No network.

- [ ] **Step 5: Commit** — `feat: add the immediate booking toggle to the window`

---

### Task 4: Docs and version

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `badminton_bot/__init__.py`

- [ ] **Step 1:** README user guide: in §5 explain the two modes; add a 「立即預約」 section — pick day/court/hour, confirm, it really books; use it once before a real midnight run as a rehearsal and cancel the rehearsal booking on the website; do not resend repeatedly (quote `IMMEDIATE_REMINDER`).
- [ ] **Step 2:** CLAUDE.md: Overview mentions both modes; Timing section notes 立即預約 skips all timing and sends login + N GETs + logout; Live-site constraint lists 立即預約 as another real-booking path; booking_plan bullet lists the new rules.
- [ ] **Step 3:** `__version__ = "1.1.0"`.
- [ ] **Step 4:** Full suite passes.
- [ ] **Step 5: Commit** — `docs: document immediate booking mode` and `chore: bump version to 1.1.0`
