#!/bin/bash
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null; then
  echo "Python is not installed. Get it from https://www.python.org/downloads/"
  read -r -p "Press Enter to close..."
  exit 1
fi
python3 -c "import requests, PIL, numpy" 2>/dev/null || python3 -m pip install -r requirements.txt
python3 catalog.py
