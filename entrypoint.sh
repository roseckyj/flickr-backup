#!/bin/sh
set -eu

exec python3 /app/flickr_sync.py "${@:-watch}"
