#!/bin/bash
# source /home/Lazycat/mysite/venv/bin/activate
# python /home/Lazycat/mysite/script.py --limit 250

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/venv/bin/activate"
python "${SCRIPT_DIR}/script.py" --limit 250