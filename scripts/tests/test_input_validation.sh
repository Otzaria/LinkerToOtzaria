#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
VALIDATOR="$ROOT/ci/validate_printable_ascii.py"

python3 "$VALIDATOR" adopt_fingerprint 8192 'old::new;policy=drop'
python3 "$VALIDATOR" boundary 8192 "$(printf 'x%.0s' {1..8192})"
! python3 "$VALIDATOR" too_long 8192 "$(printf 'x%.0s' {1..8193})" >/dev/null 2>&1
! python3 "$VALIDATOR" newline 8192 $'old\nnew' >/dev/null 2>&1
! python3 "$VALIDATOR" control 8192 $'old\x1fnew' >/dev/null 2>&1
! python3 "$VALIDATOR" unicode 8192 'אב' >/dev/null 2>&1

python3 - "$ROOT/.github/workflows/relink.yml" <<'PY'
import sys
from pathlib import Path
workflow = Path(sys.argv[1]).read_text(encoding="utf-8")
if "python3 ci/validate_printable_ascii.py adopt_fingerprint 8192 \"$ADOPT_FINGERPRINT\"" not in workflow:
    raise SystemExit("relink workflow bypasses the bounded input validator")
if "[[:print:]]{1,8192}" in workflow:
    raise SystemExit("pathological bounded grep regex returned")
PY

echo "ok   printable-ASCII validator is bounded and rejects controls/Unicode/oversize"
