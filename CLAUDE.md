# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Interactive CLI bot that races to book badminton courts on Taipei sports-center booking sites the moment reservations open. All input comes from `input()` prompts at runtime — there is no config file, no CLI flags, and no stored credentials.

## Live-site constraint (read before running anything)

**Never hammer the booking sites during development.** These are real, live systems tied to a real member account; abnormal request patterns or frequency can get the account banned.

- Do not loop, retry, or script repeated logins/bookings against the live site.
- Space out any manual run so it resembles ordinary human use — a handful of interactions, minutes apart, not automated bursts.
- The only place concurrent requests are legitimate is the actual booking instant (two `asyncio.gather` GETs, once, at the target time). Do not exercise that path repeatedly just to test.
- Prefer verifying changes offline: URL construction (`_generate_booking_url`), response parsing (`_is_booking_success`), countdown maths, and input transforms are all pure functions that can be checked without touching the network. Reserve live runs for the final confirmation.

## Commands

Environments are managed with **pyenv-virtualenv**. The env for this repo is `badminton_bot` (Python 3.11.4) and already has the dependencies installed. Note the repo has no `.python-version` of its own — it inherits 3.11.4 from `~/Projects/`, so the env must be activated explicitly.

```bash
pyenv activate badminton_bot          # or: pyenv virtualenv 3.11.4 badminton_bot (first time)
pip install -r requirements.txt       # only when requirements change

# Run — from the repo root, as a module (see Import layout below)
python -m badminton_bot.main

# Test — all offline; nothing in the suite touches the live sites
pytest
pytest tests/test_input_helper.py                      # one file
pytest tests/test_main.py::TestCountDown               # one class
pytest -k "zero_pad"                                   # by name

# Bundle a standalone executable into dist/main/ (macOS host)
./bundling_scripts.sh                 # generates main.spec, then builds
pyinstaller main.spec                 # faster rebuild once the spec exists
```

`main.spec`, `build/` and `dist/` are all gitignored — the spec is a build artifact of `bundling_scripts.sh`, not a source file, so bundling changes belong in that script.

Requires a local Chrome + matching chromedriver on PATH (Selenium resolves the driver itself).

Tests run on **pytest** (`pytest.ini` sets `testpaths = tests`). No linter or formatter is configured — black/ruff are in neither `requirements.txt` nor the env; if you add one, add it to `requirements.txt` at the same time.

## Architecture

### Two-phase booking: Selenium logs in, aiohttp fires the requests

This split is the core design and the reason for everything else.

1. **Selenium (headless Chrome)** handles login only — it is the only thing that can drive the site's JS alerts, checkbox, and `DoSubmit()` handler.
2. `service.get_cookies()` extracts the authenticated session cookies out of the driver.
3. Those cookies are handed to an `aiohttp.ClientSession`, and every booking period is fired as a **concurrent GET** via `asyncio.gather`. Selenium is far too slow for the actual race.

### Timing is the whole point

`main.py` orchestrates a strict schedule:

- `BOOKING_WEEKDAY = 4` (module constant, ISO weekday — Mon=1) determines which upcoming weekday's midnight is the target. Changing the grab day means editing this constant.
- `count_down(booking_date, offset=timedelta(minutes=-3))` blocks until **3 minutes before** the target, and only *then* does Selenium log in — logging in earlier risks session expiry.
- A second `count_down()` blocks to the exact target instant, then the requests go out.
- `count_down` is a deliberate busy-wait loop polling `datetime.now()`, not `sleep` — it needs millisecond precision.
- The offset prompt (`-1000`..`1000` ms) lets the user fire slightly early or late relative to the server clock.

**Dev mode** (`Y` at the "開發測試模式" prompt) skips the derived schedule and lets you type an arbitrary open time and arbitrary booking periods, so you can exercise the flow without waiting for the real window. It still hits the live site — see the live-site constraint above.

### Adding a sports center

`SportsCenterWebService` (`badminton_bot/services/sports_center_webservice.py`) is an ABC that holds all shared flow — login, logout, cookie extraction, `booking_courts()`. Its `__init_subclass__` **enforces at class-definition time** that every subclass declares three class attributes: `sport_center_name`, `login_page_url`, `booking_window_days`. Missing one raises `TypeError` on import, not at runtime.

Subclasses supply only site-specific selectors and URL construction via the abstract hooks (`_find_username_input_box_element`, `_generate_booking_url`, `_is_booking_success`, etc.).

To add a center: create a subclass in `badminton_bot/services/`, then register it in `WEBSERVICE_MAPPING` in `main.py` — that dict drives both the numbered menu and `webservice_factory()`.

`booking_window_days` is how far ahead that site opens reservations (Zhongshan 14, Zhongzheng 7). In non-dev mode it's added to the target date to compute which day is actually being booked; the hours are hardcoded to 20:00 and 21:00 in `main.py`.

### The two existing sites are the same platform

Zhongshan (`scr.cyc.org.tw/tp01.aspx`) and Zhongzheng (`bwd.xuanen.com.tw/wd27.aspx`) run the same ASP.NET booking software, so both subclasses share identical element IDs (`ContentPlaceHolder1_loginid`, `loginpw`, `lab_Name`, `showerror3`), the same `DoSubmit()` login call, and the same success check — the response body contains `PT=1&X=1` on success, `PT=1&X=2` on failure, and anything else raises `RuntimeError`.

The real per-site differences are: host/page path, **`QPid`** (the venue/court id in the booking URL — this is what you change to target a different court), `booking_window_days`, and `QTime` padding (Zhongshan zero-pads the hour, Zhongzheng does not). When adding a third center on this platform, expect to copy an existing subclass and change little more than those.

### Import layout

`main.py` uses fully-qualified package imports (`from badminton_bot.services... import ...`), so it must be run as a module from the repo root: **`python -m badminton_bot.main`**. Running `python badminton_bot/main.py` fails with `ModuleNotFoundError: No module named 'badminton_bot'`, because that puts `badminton_bot/` on `sys.path` instead of the repo root.

Because the imports are ordinary package imports, PyInstaller follows them on its own — `bundling_scripts.sh` just passes `--paths .`, with no `--add-data` copying of `services/`/`utils/`. Modules *inside* `services/` use relative imports (`from .sports_center_webservice import ...`).

### Testing without touching the live sites

The suite covers only the offline parts, which is deliberate (see the constraint above): input transforms, `_generate_booking_url`, `_is_booking_success`, `webservice_factory`, `count_down`, and the `__init_subclass__` contract. Service objects are built with `object.__new__(cls)` so `__init__` never runs and Chrome never launches — use that helper (`build_without_browser` in `tests/test_sports_center_webservice.py`) when adding service tests.

## Conventions

- Prompts, log messages, and inline comments are in **Traditional Chinese**; docstrings and identifiers are in English. Match this when editing.
- Input validation lives in `badminton_bot/utils/input_helper.py`. `get_valid_input(prompt, transform_func, error_hint)` re-prompts forever until `transform_func` stops raising `ValueError`/`AssertionError` — new prompts should be a transform function passed to it, not a hand-rolled loop.
- Commits use conventional-commit prefixes (`feat:`, `fix:`, `refactor:`, `style:`, `docs:`), one feature branch per change, merged into `master` via PR.
