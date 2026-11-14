# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Shareable tkinter GUI app that races to book badminton courts on Taipei sports-center booking sites the moment reservations open. The user picks a centre, courts and hour slots in a window; credentials are typed in at runtime and never stored — there is no config file and no CLI flags. It is distributed to friends as an unsigned PyInstaller bundle (macOS `.app`, Windows folder) built by GitHub Actions; the user-facing guide is `README.md`.

The form has two modes (`booking_plan.GrabMode`): **搶午夜** (default — wait for Taipei midnight and race for the day a week out) and **立即預約** (book an already-open day, today … today + `booking_window_days`, right now, after a confirm dialog; on today, hours that have started cannot be ticked).

Currently only Zhongzheng (five courts, 羽球7-1 to 7-5) is enabled; Zhongshan is shown as 「中山運動中心（暫時停止）」 and the engine refuses to run it. Hours 06–21 can be chosen, and courts × hours is capped at `MAX_REQUESTS = 12` (enforced by both the GUI and the engine).

## Live-site constraint (read before running anything)

**Never hammer the booking sites during development.** These are real, live systems tied to a real member account; abnormal request patterns or frequency can get the account banned.

- Do not loop, retry, or script repeated logins/bookings against the live site.
- Space out any manual run so it resembles ordinary human use — a handful of interactions, minutes apart, not automated bursts.
- The only place concurrent requests are legitimate is the actual booking instant (N `asyncio.gather` GETs — one per court × hour, at most 12 — fired once, at the target time). Do not exercise that path repeatedly just to test.
- 立即預約 is a second real-booking path: login + N `StepFlag=25` GETs + logout, fired as soon as it is confirmed. It really reserves slots; the same restraint applies.
- Prefer verifying changes offline: URL construction (`_generate_booking_url`), response parsing (`_is_booking_success`), countdown maths, and booking-plan logic are all pure functions that can be checked without touching the network. Reserve live runs for the final confirmation.

## Commands

Environments are managed with **pyenv-virtualenv**. The env for this repo is `badminton_bot` (Python 3.11.11, built against `tcl-tk@8` so `tkinter` works) and already has the dependencies installed. Note the repo has no `.python-version` of its own — it inherits 3.11.4 from `~/Projects/` (which has no tkinter), so the env must be activated explicitly.

```bash
pyenv activate badminton_bot          # or: pyenv virtualenv 3.11.11 badminton_bot (first time; needs a tkinter-enabled Python, i.e. `brew install tcl-tk@8` first)
pip install -r requirements.txt -c constraints.txt   # only when requirements change

# Run — opens the GUI window; from the repo root, as a module (see Import layout below)
python -m badminton_bot.main
BADMINTON_BOT_DEV=1 python -m badminton_bot.main   # shows the dev-mode fields (see Timing)

# Test — all offline; nothing in the suite touches the live sites
pytest
pytest tests/test_booking_plan.py                      # one file
pytest tests/test_timing.py::TestComputeSendTime       # one class
pytest -k "zero_pad"                                   # by name

# Bundle a local macOS app into dist/BadmintonBot.app
./bundling_scripts.sh                 # runs pyinstaller, sets NSAppSleepDisabled, ad-hoc re-signs
```

`*.spec`, `build/` and `dist/` are gitignored build artifacts; bundling changes belong in `bundling_scripts.sh`. Note `-y` wipes `dist/`, including any `dist/.../logs` you wanted to keep — back them up first.

**App Nap:** `keep_awake` stops the machine sleeping, but `caffeinate` runs as a child process and App Nap is per-process, so a minimised or covered `.app` could still be napped and have its timers coalesced right at the send instant. Both `bundling_scripts.sh` and the macOS CI job therefore set `NSAppSleepDisabled = true` in `BadmintonBot.app/Contents/Info.plist` (`plutil -replace`) and then ad-hoc re-sign the bundle (`codesign --force --deep --sign -`, then `--verify --deep --strict`) — editing `Info.plist` invalidates PyInstaller's signature, and Apple Silicon refuses to run an app whose signature is broken.

**Releasing:** PyInstaller cannot cross-compile, so `.github/workflows/release.yml` builds on `macos-latest` (Apple Silicon only; a pinned `macos-14` label was retired by GitHub and failed the v1.0.0 run) and `windows-latest` whenever a `v*` tag is pushed. It first fails the job if the tag (minus the leading `v`) differs from `badminton_bot.__version__`, then runs `pytest`, builds the same `--windowed` bundle as `bundling_scripts.sh` (including the App Nap plist key and re-sign on macOS), zips it and attaches `BadmintonBot-macOS.zip` / `BadmintonBot-Windows.zip` (each with a `.sha256` checksum file) to the release at https://github.com/Ta-Wei-Liao/badminton_bot/releases. The release body links to the README, so keep its user-guide section at the top. Every action is pinned to a full commit SHA with the version in a trailing comment, because the job holds `contents: write` and a moved tag could swap the zips friends download; bump the SHA and the comment together. The version shown in the window title comes from `badminton_bot/__init__.py` (`__version__`) — bump it before tagging, or the release job stops.

**Xcode licence on this Mac:** if `git` or `pyinstaller` fail with "You have not agreed to the Xcode license agreements", prefix the command with `DEVELOPER_DIR=/Library/Developer/CommandLineTools` (process-local), or have the author run `sudo xcodebuild -license` once.

Requires a local Chrome (Selenium Manager fetches chromedriver on first run, so the first run needs network). On pressing 開始 the engine does a browser self-check (headless Chrome opens `about:blank`, no booking site is contacted) so a missing Chrome is reported immediately rather than at T−3 min.

`constraints.txt` pins every transitive dependency (both macOS and Windows sets) to the versions a release build was proven with, so a newly published malicious version of some indirect dependency cannot slip into the bundle; CI installs with `-c constraints.txt`. When you bump `requirements.txt`, update the matching line there too (`tests/test_dependencies.py` fails if they disagree), and refresh the rest from the next CI log's "Successfully installed" lines.

Tests run on **pytest** (`pytest.ini` sets `testpaths = tests`). No linter or formatter is configured — black/ruff are in neither `requirements.txt` nor the env; if you add one, add it to `requirements.txt` at the same time.

## Architecture

### Layout

- `badminton_bot/main.py` — entry point, just calls `gui.run_app()`.
- `badminton_bot/gui.py` — tkinter `App`: form, countdown/status, results table. The grab runs on a worker thread; the UI talks to it through queues (log records via `QueueLogHandler`, stage events, a final `done`/`cancelled`/`error` event). Cancel is possible until the requests are fired.
- `badminton_bot/engine.py` — `GrabConfig` (password is `field(repr=False)`), `GrabRunner`, `CENTER_OPTIONS` (the menu: which service classes are offered and whether each is `enabled`), the schedule, the booking race, and log setup.
- `badminton_bot/booking_plan.py` — pure logic: `GrabMode`, next Taipei midnight, court × hour expansion into `BookingTarget`s, `MAX_REQUESTS` validation, the 立即預約 rules (`immediate_days`, `bookable_hours`, `validate_immediate`), and the summary/confirm text shown in the window.
- `badminton_bot/services/` — per-site Selenium/aiohttp services (see Adding a sports center).
- `badminton_bot/utils/` — `ntp_client`, `server_clock`, `timing`, and `keep_awake` (stops the machine sleeping during a run: `caffeinate` on macOS, `SetThreadExecutionState` on Windows).

### Two-phase booking: Selenium logs in, aiohttp fires the requests

This split is the core design and the reason for everything else.

1. **Selenium (headless Chrome)** handles login only — it is the only thing that can drive the site's JS alerts, checkbox, and `DoSubmit()` handler.
2. `service.get_cookies()` extracts the authenticated session cookies out of the driver.
3. Those cookies are handed to an `aiohttp.ClientSession`, and every booking period is fired as a **concurrent GET** via `asyncio.gather`. Selenium is far too slow for the actual race.

The aiohttp session verifies TLS against `certifi`'s bundle (`build_tls_context`), never `ssl=False`: it carries the logged-in cookies, so an unverified connection on shared Wi-Fi would let anyone impersonate the site and take them. certifi rather than the system default because a bundled app on a friend's machine may not find the build machine's CA path.

### Timing is the whole point

Everything in this section is 搶午夜. 立即預約 (`GrabRunner._run_immediate`) skips all of it — no NTP, clock probes, list-page checks, warm-up or countdown — and releases `run_booking_race` at `time.time()` right after login, so cancel/fired semantics and the results table stay the same.

`engine.py` (`GrabRunner`) orchestrates a strict schedule:

- The grab instant is the **first Taipei-time midnight after the user presses 開始** (`booking_plan.next_open_at`, fixed `timezone(timedelta(hours=8))`, independent of the machine's zone). The booked day is that date plus the centre's `booking_window_days`. There is no weekday constant any more. If less than 4 minutes remain the run still proceeds but the GUI warns 「距離開搶太近，校時可能不準」.
- `count_down(booking_date, offset, cancel_event)` (called by `GrabRunner._count_down` with `LOGIN_OFFSET`, i.e. −3 minutes) blocks until **3 minutes before** the target, and only *then* does Selenium log in — logging in earlier risks session expiry.
- Between login and the deadline the program measures the booking server's own clock by binary-searching its `Date` header (~8 read-only probes at random 12–25s gaps), warms one connection per request (N) at T−10s, then fires. On Zhongzheng the probe target (the list page) takes 5+ seconds, so the first probe already proves the search cannot converge and it falls back to NTP after one request; a probe that fails slowly counts too, since the time waited is a lower bound on RTT. Static assets are no way round this: they are served by Cloudflare, whose `Date` is just NTP again.
- `count_down` sleeps coarsely while far from its target and busy-waits only inside the last few seconds — it needs millisecond precision at the instant, but pegging a core for three minutes beforehand is not free.
- The send instant is computed, not assumed: `nominal + θ − rtt_median/2 + margin`, where `θ` is the measured local-minus-server offset (falling back to NTP, then to zero) and `margin` deliberately biases late. Firing early is a hard failure — the server rejects a slot that has not opened — while firing late merely risks losing the race.
- There is no manual offset prompt any more; clock correction is entirely automatic. That also means there is no human escape hatch: if NTP fails the program fires on an uncorrected clock and says so loudly in the log.
- NTP is measured twice — once up front to seed the clock search, and again at T−20s, because a local clock was observed drifting 1.3 seconds in fourteen minutes and the early reading is stale by the deadline.
- Every run writes `~/Documents/badminton_bot_logs/<timestamp>.log` (`engine.LOG_DIR`) through a `logging.FileHandler`. If that folder is not writable (macOS denying access to Documents, Windows Controlled Folder Access) `set_logger` falls back to `engine.LOG_FALLBACK_DIRS` — `~/badminton_bot_logs`, then `<tempdir>/badminton_bot_logs` — and logs a warning naming the folder actually used; it only raises if none is writable. The results view shows the real log path and its button opens that folder and the README tells friends to send the file to the author when something goes wrong. `set_logger` pins the `selenium`, `urllib3` and `asyncio` loggers (`QUIET_LIBRARY_LOGGERS`) at WARNING, because Selenium's DEBUG output includes every WebDriver command — the password typed into the login form among them. Exceptions are formatted with `utils.errors.describe_error` (type name + `str()`), never `repr()`/`%r`: an aiohttp response error's repr includes the request headers, login cookie included, and `tests/test_errors.py` fails on any `%r` or `repr(error)` in the package. Credentials are held only in `GrabConfig` (password excluded from repr) and must never be passed to `logging` or written to disk.

**Dev mode** (搶午夜 only; its fields are hidden while 立即預約 is selected) appears only when the environment variable `BADMINTON_BOT_DEV=1` is set; friends' builds never show it. It adds two form fields — 開搶時刻（開發）(`%Y-%m-%d %H:%M:%S`) and 預約日期（開發）(`%Y-%m-%d`) — that replace the derived open time and booking day, so you can exercise the flow without waiting for midnight. The courts and hours are picked in the normal form. Everything else — NTP sync, the server-clock probes, warm-up, and the post-booking reconnaissance — runs exactly as it does for a real grab, so dev mode is a full rehearsal rather than a partial one.

That makes it **expensive and not something to run casually**: each dev run sends extra requests to the live site on top of login (on Zhongzheng roughly 3 + N, N = courts × hours: one clock probe, two list-page checks, N warm-ups), and the booking GETs are N real `StepFlag=25` requests, so available slots really will be reserved. Give the open time at least 4 minutes of runway — the clock probes need ~2.5 minutes between T−3min and T−30s to converge; a shorter window degrades to no measurement rather than failing. See the live-site constraint above.

### Known risk: Cloudflare Turnstile

The site's booking button now fetches a Turnstile token and POSTs it to `/api/api_captcha.aspx` before navigating to `StepFlag=25`; this bot GETs `StepFlag=25` directly and skips that step. It worked on 2026-11-05 but two slots on 2026-11-12 returned `X=2` (indistinguishable from "taken"). We deliberately do not handle or bypass Turnstile; if the server starts enforcing it the whole tool stops working. Unexpected responses are shown as ⚠️ in the results table. Details: `docs/superpowers/specs/2026-11-14-shareable-gui-flexible-booking-design.md` §7.

### Adding a sports center

`SportsCenterWebService` (`badminton_bot/services/sports_center_webservice.py`) is an ABC that holds all shared flow — login, logout, cookie extraction, `booking_courts()`. Its `__init_subclass__` **enforces at class-definition time** that every subclass declares four class attributes: `sport_center_name`, `login_page_url`, `booking_window_days`, `courts` (a tuple of `Court(name, qpid)`; one entry per bookable court). Missing one raises `TypeError` on import, not at runtime.

Subclasses supply only site-specific selectors and URL construction via the abstract hooks (`_find_username_input_box_element`, `_generate_booking_url`, `_is_booking_success`, etc.).

To add a center: create a subclass in `badminton_bot/services/`, then add a `CenterOption(YourService, enabled)` to `CENTER_OPTIONS` in `engine.py` — that tuple drives the GUI's centre dropdown, and a disabled option is shown as 「…（暫時停止）」, cannot be selected, and is refused by the engine.

`booking_window_days` is how far ahead that site opens reservations (Zhongshan 14, Zhongzheng 7). In non-dev mode it's added to the grab date to compute which day is actually being booked. Hours are not fixed: the user ticks any of 06–21 in the GUI (`BOOKABLE_HOURS`).

### The two existing sites are the same platform

Zhongshan (`scr.cyc.org.tw/tp01.aspx`) and Zhongzheng (`bwd.xuanen.com.tw/wd27.aspx`) run the same ASP.NET booking software, so both subclasses share identical element IDs (`ContentPlaceHolder1_loginid`, `loginpw`, `lab_Name`, `showerror3`), the same `DoSubmit()` login call, and the same success check — the response body contains `PT=1&X=1` on success, `PT=1&X=2` on failure, and anything else raises `RuntimeError`.

The real per-site differences are: host/page path, **`courts`** (the venue/court ids — class-attribute data, so changing which courts are offered is a one-line edit; Zhongzheng's are 羽球7-1…7-5 = QPid 1196…1200), `booking_window_days`, and `QTime` padding (Zhongshan zero-pads the hour, Zhongzheng does not). When adding a third center on this platform, expect to copy an existing subclass and change little more than those.

### Import layout

`main.py` and the other modules use fully-qualified package imports (`from badminton_bot.services... import ...`), so it must be run as a module from the repo root: **`python -m badminton_bot.main`**. Running `python badminton_bot/main.py` fails with `ModuleNotFoundError: No module named 'badminton_bot'`, because that puts `badminton_bot/` on `sys.path` instead of the repo root.

Because the imports are ordinary package imports, PyInstaller follows them on its own — `bundling_scripts.sh` just passes `--paths .` (plus `--collect-all selenium`), with no `--add-data` copying of `services/`/`utils/`. Modules *inside* `services/` use relative imports (`from .sports_center_webservice import ...`).

### Testing without touching the live sites

The suite covers only the offline parts, which is deliberate (see the constraint above): booking-plan logic (open time, court × hour expansion, the 12-request cap), GUI helpers, engine scheduling, `_generate_booking_url`, `_is_booking_success`, `count_down`, NTP/server-clock/timing maths, `keep_awake`, and the `__init_subclass__` contract. Service objects are built with `object.__new__(cls)` so `__init__` never runs and Chrome never launches — use that helper (`build_without_browser` in `tests/test_sports_center_webservice.py`) when adding service tests.

## Conventions

- GUI text, log messages, and inline comments are in **Traditional Chinese**; docstrings and identifiers are in English. Match this when editing.
- Validation of what the user selected lives in `badminton_bot/booking_plan.py` (pure functions, unit-tested); the GUI only displays the result and the engine re-checks the same limits.
- Commits use conventional-commit prefixes (`feat:`, `fix:`, `refactor:`, `style:`, `docs:`, `test:`), one feature branch per change, merged into `master` via PR. Do not add `Co-Authored-By` trailers.
- **Always pass `--repo Ta-Wei-Liao/badminton_bot` to `gh`.** `origin` is the SSH config alias `git@github.com-tawei:...`, not a literal `github.com` URL, so `gh` cannot reliably infer the repository from the remote. Pass the flag rather than rewriting the remote — the alias is what selects the right SSH key (`~/.ssh/personal_id_ed25519`).
