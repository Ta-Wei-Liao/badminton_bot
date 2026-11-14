"""The tkinter window: the form, the running view and the results table.

Tk owns the main thread. The grab runs in a daemon thread with its own event
loop and never touches a Tk object: stages, the outcome and log lines all go
into queues that the main thread drains with root.after.
"""

import asyncio
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from badminton_bot import __version__
from badminton_bot.booking_plan import (
    BOOKABLE_HOURS,
    TAIPEI,
    TOO_CLOSE,
    booking_day,
    describe_schedule,
    describe_selection,
    expand_targets,
    next_open_at,
    summarize,
)
from badminton_bot.engine import (
    CENTER_OPTIONS,
    LOG_DIR,
    STAGE_COUNTDOWN,
    STAGE_FIRED,
    GrabConfig,
    GrabRunner,
    LoginFailed,
    set_logger,
)
from badminton_bot.services.sports_center_webservice import (
    BookingAttempt,
    BrowserUnavailable,
)
from badminton_bot.utils.timing import MAX_AUTO_CORRECTION_SECONDS, Cancelled

DEV_OPEN_AT_FORMAT = "%Y-%m-%d %H:%M:%S"
DEV_DAY_FORMAT = "%Y-%m-%d"

# 最後這幾秒引擎在 busy-wait，Tk 重繪會搶 GIL，只更新倒數數字
QUIET_SECONDS = 10

STATUS_CANCELLING = "取消中…"
STATUS_CLOSING = "正在結束…"

WARNING_COLOUR = "#d9822b"

_OUTCOME_MARKS = {True: "✅", False: "❌", None: "⚠️"}


class QueueLogHandler(logging.Handler):
    """Put each formatted record on a queue for the Tk thread to display."""

    def __init__(self, messages: queue.Queue) -> None:
        super().__init__()
        self._messages = messages

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._messages.put_nowait(self.format(record))
        except Exception:
            self.handleError(record)


def is_dev_mode(environ: Mapping[str, str] = os.environ) -> bool:
    return environ.get("BADMINTON_BOT_DEV") == "1"


def results_grid(attempts: Sequence[BookingAttempt]) -> dict[tuple[str, int], str]:
    """Map (court name, hour) to the mark shown in that cell of the results table."""
    return {(a.court_name, a.hour): _OUTCOME_MARKS[a.success] for a in attempts}


def format_remaining(seconds: float) -> str:
    """Human-readable time left, coarser the further away it is."""
    total = max(int(seconds), 0)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小時 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def in_quiet_window(remaining: float, fired: bool) -> bool:
    """Whether the log view should hold still, given the seconds left to nominal open_at.

    The real send instant is nominal + θ and can land after nominal, by at most
    the largest correction the engine accepts, so stay quiet until fired.
    """
    return not fired and -MAX_AUTO_CORRECTION_SECONDS < remaining <= QUIET_SECONDS


def status_text(
    stage: str, remaining: float, fired: bool, pinned: str | None = None
) -> str | None:
    """The big status line, or None when there is nothing to show yet.

    A pinned status (e.g. 取消中…) wins over every stage until the outcome arrives.
    """
    if pinned is not None:
        return pinned
    if not stage:
        return None
    if fired or remaining <= 0:
        return stage
    if stage == STAGE_COUNTDOWN:
        return f"倒數 {format_remaining(remaining)}"
    return f"{stage}（還有 {format_remaining(remaining)}）"


def stage_reporter(events: queue.Queue) -> Callable[[str], None]:
    """The on_stage callback handed to the engine: it only enqueues, never touches Tk."""

    def report(stage: str) -> None:
        events.put(("stage", stage))

    return report


def run_grab(runner: GrabRunner, events: queue.Queue) -> None:
    """Worker-thread body: run the grab and enqueue its outcome. Never touches Tk."""
    try:
        events.put(("done", asyncio.run(runner.run())))
    except Cancelled:
        events.put(("cancelled", None))
    except (BrowserUnavailable, LoginFailed) as error:
        events.put(("error", str(error)))
    except BaseException as error:
        # 不管背景執行緒怎麼結束都要回報，否則表單會永遠鎖住
        logging.exception("發生未預期的錯誤")
        events.put(("unexpected", repr(error)))


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


class App:
    def __init__(self, root: tk.Tk, dev_mode: bool) -> None:
        self.root = root
        self.dev_mode = dev_mode
        self.center = next(o for o in CENTER_OPTIONS if o.enabled)
        self.courts = self.center.service_cls.courts
        self.messages: queue.Queue[str] = queue.Queue()
        # 背景執行緒只往這裡丟 (種類, 內容)，由主執行緒取出處理
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.runner: GrabRunner | None = None
        self.open_at: datetime | None = None
        self.stage = ""
        # 取消後固定顯示的狀態，直到結果回來；否則每秒的更新會把它蓋掉
        self.pinned_status: str | None = None
        self.closing = False
        self.log_path: Path | None = None

        root.title(f"羽球場地搶訂 v{__version__}")
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_form()
        self._build_run_view()
        self._refresh()
        self._tick()
        self._poll()

    # ---- 畫面 ----

    def _build_form(self) -> None:
        form = ttk.Frame(self.root, padding=12)
        form.grid(row=0, column=0, sticky="nsew")
        self.inputs: list[tk.Widget] = []

        ttk.Label(form, text="運動中心").grid(row=0, column=0, sticky="w")
        self.center_box = ttk.Combobox(
            form, state="readonly", values=[o.label for o in CENTER_OPTIONS]
        )
        self.center_box.current(CENTER_OPTIONS.index(self.center))
        self.center_box.bind("<<ComboboxSelected>>", self._on_center_selected)
        self.center_box.grid(row=0, column=1, sticky="ew", pady=2)

        self.username = tk.StringVar()
        self.password = tk.StringVar()
        ttk.Label(form, text="身分證字號").grid(row=1, column=0, sticky="w")
        username_entry = ttk.Entry(form, textvariable=self.username)
        username_entry.grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(form, text="密碼").grid(row=2, column=0, sticky="w")
        password_entry = ttk.Entry(form, textvariable=self.password, show="•")
        password_entry.grid(row=2, column=1, sticky="ew", pady=2)
        self.inputs += [username_entry, password_entry]

        courts_frame = ttk.LabelFrame(form, text="場地", padding=6)
        courts_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=4)
        self.court_vars = [tk.BooleanVar() for _ in self.courts]
        for i, (court, var) in enumerate(zip(self.courts, self.court_vars)):
            check = ttk.Checkbutton(courts_frame, text=court.name, variable=var)
            check.grid(row=0, column=i, padx=4)
            self.inputs.append(check)

        hours_frame = ttk.LabelFrame(form, text="時段", padding=6)
        hours_frame.grid(row=4, column=0, columnspan=2, sticky="ew", pady=4)
        self.hour_vars = [tk.BooleanVar() for _ in BOOKABLE_HOURS]
        for i, (hour, var) in enumerate(zip(BOOKABLE_HOURS, self.hour_vars)):
            check = ttk.Checkbutton(
                hours_frame, text=f"{hour:02d}:00–{hour + 1:02d}:00", variable=var
            )
            check.grid(row=i // 4, column=i % 4, sticky="w", padx=4)
            self.inputs.append(check)

        tracked = [self.username, self.password, *self.court_vars, *self.hour_vars]
        next_row = 5
        if self.dev_mode:
            now = datetime.now(TAIPEI)
            self.dev_open_at = tk.StringVar(value=now.strftime(DEV_OPEN_AT_FORMAT))
            self.dev_day = tk.StringVar(value=now.strftime(DEV_DAY_FORMAT))
            for label, var in (
                ("開搶時刻（開發）", self.dev_open_at),
                ("預約日期（開發）", self.dev_day),
            ):
                ttk.Label(form, text=label).grid(row=next_row, column=0, sticky="w")
                entry = ttk.Entry(form, textvariable=var)
                entry.grid(row=next_row, column=1, sticky="ew", pady=2)
                self.inputs.append(entry)
                next_row += 1
            tracked += [self.dev_open_at, self.dev_day]
        for var in tracked:
            var.trace_add("write", lambda *_: self._refresh())

        self.summary = ttk.Label(form, wraplength=520)
        self.summary.grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.warning = ttk.Label(form, foreground=WARNING_COLOUR)
        self.warning.grid(row=next_row + 1, column=0, columnspan=2, sticky="w")

        buttons = ttk.Frame(form)
        buttons.grid(row=next_row + 2, column=0, columnspan=2, sticky="w", pady=6)
        self.start_button = ttk.Button(buttons, text="開始", command=self._start)
        self.start_button.grid(row=0, column=0)
        self.cancel_button = ttk.Button(
            buttons, text="取消", command=self._cancel, state="disabled"
        )
        self.cancel_button.grid(row=0, column=1, padx=6)
        form.columnconfigure(1, weight=1)

    def _build_run_view(self) -> None:
        view = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        view.grid(row=1, column=0, sticky="nsew")
        self.status = tk.StringVar()
        ttk.Label(view, textvariable=self.status, font=("", 18, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        self.log_text = ScrolledText(view, height=10, width=80, state="disabled")
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=4)

        self.results = ttk.Frame(view)
        self.results.grid(row=2, column=0, sticky="ew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        view.columnconfigure(0, weight=1)
        view.rowconfigure(1, weight=1)

    # ---- 表單 ----

    def _selected_courts(self):
        return [c for c, v in zip(self.courts, self.court_vars) if v.get()]

    def _selected_hours(self) -> list[int]:
        return [h for h, v in zip(BOOKABLE_HOURS, self.hour_vars) if v.get()]

    def _schedule(self, now: datetime):
        """(open_at, booking day) for a run started now. Raises ValueError on bad dev input."""
        if self.dev_mode:
            open_at = datetime.strptime(
                self.dev_open_at.get().strip(), DEV_OPEN_AT_FORMAT
            ).replace(tzinfo=TAIPEI)
            day = datetime.strptime(self.dev_day.get().strip(), DEV_DAY_FORMAT).date()
            return open_at, day
        open_at = next_open_at(now)
        return open_at, booking_day(open_at, self.center.service_cls.booking_window_days)

    def _refresh(self) -> None:
        if self.runner is not None:
            return
        now = datetime.now(TAIPEI)
        result = summarize(
            now,
            self._selected_courts(),
            self._selected_hours(),
            self.center.service_cls.booking_window_days,
            bool(self.username.get().strip() and self.password.get()),
        )
        text, can_start, warning = result.text, result.can_start, result.warning
        if self.dev_mode:
            try:
                open_at, day = self._schedule(now)
            except ValueError:
                text, can_start = "開發模式：開搶時刻或預約日期格式錯誤", False
            else:
                warning = "距離開搶太近，校時可能不準" if open_at - now < TOO_CLOSE else None
                if can_start:
                    selection = describe_selection(
                        len(self._selected_courts()), len(self._selected_hours())
                    )
                    text = f"{selection}｜開發模式：{describe_schedule(open_at, day)}"
        self.summary.configure(text=text)
        self.warning.configure(text=warning or "")
        self.start_button.configure(state="normal" if can_start else "disabled")

    def _on_center_selected(self, _event) -> None:
        chosen = CENTER_OPTIONS[self.center_box.current()]
        if not chosen.enabled:
            self.center_box.current(CENTER_OPTIONS.index(self.center))
            messagebox.showinfo(
                "暫時停止", f"{chosen.service_cls.sport_center_name}目前暫時停止使用"
            )

    def _set_form_state(self, locked: bool) -> None:
        for widget in self.inputs:
            widget.configure(state="disabled" if locked else "normal")
        self.center_box.configure(state="disabled" if locked else "readonly")
        self.cancel_button.configure(state="normal" if locked else "disabled")
        if locked:
            self.start_button.configure(state="disabled")

    # ---- 執行 ----

    def _start(self) -> None:
        now = datetime.now(TAIPEI)
        try:
            open_at, day = self._schedule(now)
        except ValueError:
            return
        targets = expand_targets(self._selected_courts(), self._selected_hours(), day)
        config = GrabConfig(
            service_cls=self.center.service_cls,
            username=self.username.get().strip(),
            password=self.password.get(),
            open_at=open_at,
            targets=targets,
        )
        handler = QueueLogHandler(self.messages)
        try:
            self.log_path = set_logger(LOG_DIR, extra_handlers=[handler])
        except OSError as error:
            messagebox.showerror("無法建立 log 檔", str(error))
            return
        # 視窗裡只看時間和訊息，檔案裡保留完整格式
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

        self._clear_results()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.open_at = open_at
        self.stage = ""
        self.pinned_status = None
        self.runner = GrabRunner(config, on_stage=stage_reporter(self.events))
        self._set_form_state(locked=True)
        threading.Thread(
            target=run_grab, args=(self.runner, self.events), daemon=True
        ).start()

    def _on_stage(self, stage: str) -> None:
        self.stage = stage
        if stage == STAGE_FIRED:
            self.cancel_button.configure(state="disabled")
        self._update_status()

    def _update_status(self) -> None:
        if self.runner is None or self.open_at is None:
            return
        remaining = (self.open_at - datetime.now(TAIPEI)).total_seconds()
        text = status_text(self.stage, remaining, self.runner.fired, self.pinned_status)
        if text is not None:
            self.status.set(text)

    def _in_quiet_window(self) -> bool:
        if self.runner is None or self.open_at is None:
            return False
        remaining = (self.open_at - datetime.now(TAIPEI)).total_seconds()
        return in_quiet_window(remaining, self.runner.fired)

    def _cancel(self) -> None:
        if self.runner is None:
            return
        if self.runner.cancel():
            self.cancel_button.configure(state="disabled")
            self.pinned_status = STATUS_CANCELLING
            self.status.set(STATUS_CANCELLING)
        else:
            messagebox.showinfo("無法取消", "請求已送出，無法取消")

    def _on_finished(self, kind: str, payload) -> None:
        self.runner = None
        self.open_at = None
        self.pinned_status = None
        self._drain_log()
        self._set_form_state(locked=False)
        if self.closing:
            self.root.destroy()
            return
        if kind == "done":
            self.status.set("完成")
            self._show_results(payload)
        elif kind == "cancelled":
            self.status.set("已取消")
        elif kind == "error":
            self.status.set("未完成")
            messagebox.showerror("無法完成", payload)
        else:
            self.status.set("未完成")
            messagebox.showerror(
                "錯誤",
                f"發生未預期的錯誤：{payload}，請把 log 檔傳給作者\n\nlog 檔位置：{self.log_path}",
            )
        self._refresh()

    def _on_close(self) -> None:
        if self.runner is None:
            self.root.destroy()
            return
        if not messagebox.askyesno("放棄", "確定要放棄這次搶場地嗎？"):
            return
        # 等背景執行緒登出、關掉 Chrome 再關窗
        self.closing = True
        if not self.runner.cancel():
            messagebox.showinfo("無法取消", "請求已送出，無法取消，完成後會自動關閉")
        self.pinned_status = STATUS_CLOSING
        self.status.set(STATUS_CLOSING)

    # ---- log 與結果 ----

    def _tick(self) -> None:
        if self.runner is None:
            self._refresh()
        else:
            self._update_status()
        self.root.after(1000, self._tick)

    def _poll(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "stage":
                self._on_stage(payload)
            else:
                self._on_finished(kind, payload)
                if self.closing:
                    return  # 視窗已關閉，不再排下一輪
        if not self._in_quiet_window():
            self._drain_log()
        self.root.after(100, self._poll)

    def _drain_log(self) -> None:
        lines = []
        while True:
            try:
                lines.append(self.messages.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", "\n".join(lines) + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

    def _clear_results(self) -> None:
        for child in self.results.winfo_children():
            child.destroy()

    def _show_results(self, attempts: Sequence[BookingAttempt]) -> None:
        self._clear_results()
        grid = results_grid(attempts)
        hours = sorted({a.hour for a in attempts})
        courts = list(dict.fromkeys(a.court_name for a in attempts))
        table = ttk.Treeview(
            self.results, columns=[str(h) for h in hours], height=len(courts)
        )
        table.heading("#0", text="場地")
        table.column("#0", width=90)
        for hour in hours:
            table.heading(str(hour), text=f"{hour:02d}:00")
            table.column(str(hour), width=60, anchor="center")
        for court in courts:
            table.insert(
                "", "end", text=court, values=[grid.get((court, h), "") for h in hours]
            )
        table.grid(row=0, column=0, columnspan=2, sticky="w")

        won = sum(1 for a in attempts if a.success)
        ttk.Label(self.results, text=f"搶到 {won} / {len(attempts)} 格").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        ttk.Label(self.results, text=f"log 檔位置：{self.log_path}").grid(
            row=2, column=0, sticky="w"
        )
        ttk.Button(
            self.results,
            text="打開 log 資料夾",
            # log 可能因「文件」寫不進去而改存別處，開實際用到的那個資料夾
            command=lambda path=self.log_path.parent: open_folder(path),
        ).grid(row=2, column=1, padx=6)


def run_app() -> None:
    root = tk.Tk()
    App(root, dev_mode=is_dev_mode())
    root.mainloop()
