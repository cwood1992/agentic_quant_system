#!/bin/bash
set -e
python data_collector/backfill.py --pairs all --days 180 --timeframes 1m,1h,4h,1d
exit $?
