#!/bin/bash
cd "$(dirname "$0")" || exit 1
python3 monitor_perm.py
echo
echo "=== Последние записи журнала ==="
tail -n 5 found_objects.txt
