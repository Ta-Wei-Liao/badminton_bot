#!/bin/bash
# 本機打包：產出 dist/BadmintonBot.app（macOS）。不需先啟用 pyenv 環境。
# 注意：-y 會覆蓋 dist/ 內的舊產物，打包前先備份要留的東西（例如舊的 logs）。

PYINSTALLER="${PYINSTALLER:-$HOME/.pyenv/versions/badminton_bot/bin/pyinstaller}"

"$PYINSTALLER" badminton_bot/main.py --name BadmintonBot --windowed --paths . --collect-all selenium -y
