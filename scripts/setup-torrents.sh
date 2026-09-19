#!/bin/bash
# Installs libtorrent (the BitTorrent engine) into Peak's own Python environment.
# libtorrent ships wheels for Python 3.9–3.13; Peak finds this venv on its own.
set -e
VENV="$HOME/Library/Application Support/Peak Download Manager/lt-venv"
PY=""
for v in 3.13 3.12 3.11 3.10; do
  for p in "$(command -v python$v)" /opt/homebrew/bin/python$v /usr/local/bin/python$v /opt/anaconda3/bin/python$v; do
    [ -x "$p" ] && { PY="$p"; break 2; }
  done
done
[ -z "$PY" ] && { echo "Needs Python 3.10–3.13 (brew install python@3.13)"; exit 1; }
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade libtorrent
"$VENV/bin/python" -c "import libtorrent as lt; print('libtorrent', lt.__version__, 'ready')"
