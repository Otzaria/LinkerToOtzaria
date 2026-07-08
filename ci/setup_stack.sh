#!/usr/bin/env bash
# Bring up the Sefaria linker stack on a self-hosted Linux runner, idempotently.
# Mirrors the validated local POC (see SeforimLibrary/LINKER_INTEGRATION_PLAN.md and the
# scheduler in ~/Documents/linker-full-run): MongoDB + restored Sefaria dump, the gpu-server
# NER microservice (gunicorn on :5051), and Sefaria-Project (Django) in a venv.
#
# Heavy inputs (Mongo dump, spaCy models) are cached under $CACHE so only the first run pays
# the download. Everything here is idempotent — safe to re-run each pipeline invocation.
set -euo pipefail

STACK="${GITHUB_WORKSPACE:-$PWD}/stack"
CACHE="${LINKER_CACHE_DIR:-$HOME/.cache/linker-stack}"
SEF="$STACK/Sefaria-Project"
GPU="$STACK/gpu-server"
NER_URL="http://127.0.0.1:5051/recognize-entities"
mkdir -p "$STACK" "$CACHE"

# ── 1. Sefaria-Project + gpu-server checkouts ────────────────────────────────
[ -d "$SEF" ] || git clone --depth 1 https://github.com/Sefaria/Sefaria-Project "$SEF"
[ -d "$GPU" ] || git clone --depth 1 https://github.com/Sefaria/gpu-server "$GPU"

# ── 2. Python venv (numpy<2 — thinc/spaCy binary compat, learned in the POC) ─
if [ ! -x "$SEF/.venv/bin/python" ]; then
  python3 -m venv "$SEF/.venv"
  "$SEF/.venv/bin/pip" install --upgrade pip
  "$SEF/.venv/bin/pip" install -r "$SEF/requirements.txt"
  "$SEF/.venv/bin/pip" install "numpy<2" thinc-apple-ops || "$SEF/.venv/bin/pip" install "numpy<2"
fi
# local_settings: point Django at the local Mongo; secrets stay empty (offline linking).
if [ ! -f "$SEF/sefaria/local_settings.py" ]; then
  cp "$SEF/sefaria/local_settings_example.py" "$SEF/sefaria/local_settings.py"
fi

# ── 3. MongoDB + Sefaria dump (cached) ───────────────────────────────────────
# The dump is a versioned asset; pin its URL via the LINKER_DUMP_URL secret/var so the
# lineage is reproducible. Restored only if the DB is empty.
pgrep -x mongod >/dev/null || (mkdir -p "$CACHE/mongo-data" && mongod --dbpath "$CACHE/mongo-data" --fork --logpath "$CACHE/mongod.log")
if [ ! -f "$CACHE/.dump-restored" ]; then
  : "${LINKER_DUMP_URL:?set LINKER_DUMP_URL to the Sefaria mongo dump archive}"
  curl -fsSL -o "$CACHE/dump.tar.gz" "$LINKER_DUMP_URL"
  tar -xzf "$CACHE/dump.tar.gz" -C "$CACHE"
  mongorestore --drop "$CACHE/dump"
  touch "$CACHE/.dump-restored"
fi

# ── 4. NER models + gpu-server (gunicorn on :5051) ───────────────────────────
# Fixed worker count set up-front; never change it live (macOS fork crashloop lesson —
# harmless on Linux but we keep the discipline). Restart cleanly if not already answering.
if ! curl -fsS -m 5 -X POST "$NER_URL" -H 'Content-Type: application/json' \
      -d '{"text":"בדיקה","lang":"he"}' >/dev/null 2>&1; then
  [ -x "$GPU/.venv/bin/gunicorn" ] || { python3 -m venv "$GPU/.venv"; "$GPU/.venv/bin/pip" install --upgrade pip; "$GPU/.venv/bin/pip" install -r "$GPU/requirements.txt"; }
  ( cd "$GPU/app" && APP_CONFIG=local_config.py nohup "$GPU/.venv/bin/gunicorn" -w 3 --timeout 600 \
      -b 127.0.0.1:5051 'app:create_app()' >"$CACHE/gunicorn.log" 2>&1 & )
  for _ in $(seq 1 60); do
    curl -fsS -m 5 -X POST "$NER_URL" -H 'Content-Type: application/json' \
      -d '{"text":"בדיקה","lang":"he"}' >/dev/null 2>&1 && break
    sleep 5
  done
fi

echo "stack up: Sefaria venv=$SEF/.venv, NER=$NER_URL"
