#!/bin/bash
# 本機打包：產出 dist/BadmintonBot.app（macOS）。不需先啟用 pyenv 環境。
# 注意：-y 會覆蓋 dist/ 內的舊產物，打包前先備份要留的東西（例如舊的 logs）。
set -euo pipefail

PYINSTALLER="${PYINSTALLER:-$HOME/.pyenv/versions/badminton_bot/bin/pyinstaller}"

"$PYINSTALLER" badminton_bot/main.py --name BadmintonBot --windowed --paths . --collect-all selenium -y

if [[ "$(uname)" == "Darwin" ]]; then
    # 關掉 App Nap：視窗被縮小或蓋住時 macOS 會讓整個 process 打盹、合併計時器，
    # 開搶那一刻的送出時間就會飄掉。caffeinate 在子行程裡，擋不到這個。
    APP=dist/BadmintonBot.app
    plutil -replace NSAppSleepDisabled -bool YES "$APP/Contents/Info.plist"
    # 改了 Info.plist 原本的簽章就失效，重新 ad-hoc 簽一次，否則 Apple Silicon 不讓它執行
    codesign --force --deep --sign - "$APP"
    codesign --verify --deep --strict "$APP"
fi
