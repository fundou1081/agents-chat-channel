#!/bin/bash
# 跑 60 秒 demo
cd "$(dirname "$0")/.."
source .venv/bin/activate
rm -rf data/
python -u -m agents_chat.main demo
