#!/bin/bash

pyinstaller badminton_bot/main.py --add-data "./badminton_bot/services:./services" --add-data "./badminton_bot/utils:./utils" --collect-all selenium -y