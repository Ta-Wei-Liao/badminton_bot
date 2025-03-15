#!/bin/bash

pyinstaller badminton_bot/main.py --add-data "./badminton_bot/services:./services" --collect-all selenium -y