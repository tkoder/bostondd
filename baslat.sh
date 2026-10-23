#!/bin/sh
# Stok Takip uygulamasını başlatır: http://127.0.0.1:5050
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
# Fiş okuma için ANTHROPIC_API_KEY burada tutulur.
[ -f ayarlar.env ] && set -a && . ./ayarlar.env && set +a
exec .venv/bin/python app.py
