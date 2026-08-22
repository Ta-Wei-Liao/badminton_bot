#!/bin/bash

pyinstaller badminton_bot/main.py --paths . --collect-all selenium -y
