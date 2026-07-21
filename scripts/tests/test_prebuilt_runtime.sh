#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

SEF_ID='Python 3.12.13:aaaaaaaaaaaa:59291d9f8754de0062711bc1cd49214b0e618fc5'
GPU_ID='Python 3.12.13:bbbbbbbbbbbb:d9da16119e9c0de64224d7b187cd999b2ead4bad'
mkdir -p "$TMP/payload/sefaria-venv/bin" "$TMP/payload/gpu-venv/bin" "$TMP/payload/freeze"
printf '%s' "$SEF_ID" > "$TMP/payload/sefaria-venv/.identity"
printf '%s' "$GPU_ID" > "$TMP/payload/gpu-venv/.identity"
ln -s python3.12 "$TMP/payload/sefaria-venv/bin/python"
ln -s /usr/bin/python3.12 "$TMP/payload/sefaria-venv/bin/python3.12"
ln -s python3.12 "$TMP/payload/gpu-venv/bin/python"
ln -s /usr/bin/python3.12 "$TMP/payload/gpu-venv/bin/python3.12"
mkdir "$TMP/payload/sefaria-venv/lib" "$TMP/payload/gpu-venv/lib"
ln -s lib "$TMP/payload/sefaria-venv/lib64"
ln -s lib "$TMP/payload/gpu-venv/lib64"
printf 'a==1\n' > "$TMP/payload/freeze/sefaria.txt"
printf 'b==2\n' > "$TMP/payload/freeze/gpu-server.txt"
SEF_FREEZE=$(sha256sum "$TMP/payload/freeze/sefaria.txt" | cut -d' ' -f1)
GPU_FREEZE=$(sha256sum "$TMP/payload/freeze/gpu-server.txt" | cut -d' ' -f1)
SEF_ID="$SEF_ID" GPU_ID="$GPU_ID" SEF_FREEZE="$SEF_FREEZE" GPU_FREEZE="$GPU_FREEZE" \
  python3 - "$TMP/payload/runtime-manifest.json" <<'PY'
import json, os, sys
v={
 "schema_version":1,"python_version":"Python 3.12.13","platform":"test","machine":"x86_64",
 "sefaria_commit":"5"*64,"sefaria_requirements_sha256":"a"*64,"sefaria_freeze_sha256":os.environ["SEF_FREEZE"],
 "sefaria_identity":os.environ["SEF_ID"],"gpu_server_commit":"d"*64,"gpu_server_requirements_sha256":"b"*64,
 "gpu_server_freeze_sha256":os.environ["GPU_FREEZE"],"gpu_server_identity":os.environ["GPU_ID"],
 "builder_sha256":"c"*64,
}
open(sys.argv[1],"w").write(json.dumps(v,sort_keys=True,separators=(",",":"))+"\n")
PY
tar -C "$TMP/payload" -cf "$TMP/runtime.tar" \
  sefaria-venv gpu-venv freeze runtime-manifest.json
zstd -q -f "$TMP/runtime.tar" -o "$TMP/runtime.tar.zst"
SHA=$(sha256sum "$TMP/runtime.tar.zst" | cut -d' ' -f1)
mkdir -p "$TMP/stack/Sefaria-Project/.venv" "$TMP/stack/gpu-server/.venv"
printf old > "$TMP/stack/Sefaria-Project/.venv/old"
bash "$ROOT/ci/install_prebuilt_runtime.sh" "$TMP/runtime.tar.zst" "$SHA" "$TMP/stack" "$SEF_ID" "$GPU_ID"
test -L "$TMP/stack/Sefaria-Project/.venv/bin/python"
test -f "$TMP/stack/Sefaria-Project/.venv/.freeze"
test ! -e "$TMP/stack/Sefaria-Project/.venv/old"
echo "ok   valid content-addressed runtime installs both environments"

printf old > "$TMP/stack/Sefaria-Project/.venv/sentinel"
rc=0
bash "$ROOT/ci/install_prebuilt_runtime.sh" "$TMP/runtime.tar.zst" "$(printf '0%.0s' {1..64})" \
  "$TMP/stack" "$SEF_ID" "$GPU_ID" >/dev/null 2>&1 || rc=$?
test "$rc" -ne 0
test -f "$TMP/stack/Sefaria-Project/.venv/sentinel"
echo "ok   digest failure is fail-before-mutation"

python3 - "$ROOT/scripts/dispatch_kaggle_relink.sh" "$ROOT/kaggle/bootstrap.sh" "$ROOT/.github/workflows/relink.yml" <<'PY'
import re, sys
dispatcher=open(sys.argv[1],encoding="utf-8").read()
bootstrap=open(sys.argv[2],encoding="utf-8").read()
workflow=open(sys.argv[3],encoding="utf-8").read()
if 'RUNTIME_KERNEL=otzaria/linker-python-runtime' not in dispatcher:
    raise SystemExit("dispatcher does not pin the runtime kernel output")
if '["$TOOLS_KERNEL", "$RUNTIME_KERNEL"]' not in dispatcher:
    raise SystemExit("runtime kernel output is not attached to the JIT kernel")
match=re.search(r'LINKER_RUNTIME_SHA256=([0-9a-f]{64})',bootstrap)
if not match or match.group(1) != "bacad1b486c2bb392ee786bcc35b27dcc2beb17ea90b05f47352a06e44c8ff43":
    raise SystemExit("bootstrap does not pin the reviewed runtime archive")
if workflow.count("bacad1b486c2bb392ee786bcc35b27dcc2beb17ea90b05f47352a06e44c8ff43") != 1:
    raise SystemExit("relink job does not independently pin the reviewed runtime archive")
if "LINKER_RUNTIME_ARCHIVE: ${{ inputs.target == 'kaggle' && '/kaggle/temp/linker-python-runtime-v1.tar.zst' || '' }}" not in workflow:
    raise SystemExit("relink job does not consume the bootstrap-to-worker handoff path")
if "Preflight — verified Kaggle runtime handoff" not in workflow:
    raise SystemExit("relink job does not verify the runtime before downloading the snapshot")
if 'cp --reflink=auto "$RUNTIME_MATCHES" "$LINKER_RUNTIME_ARCHIVE.tmp"' not in bootstrap:
    raise SystemExit("bootstrap does not materialize the input mount into worker-visible storage")
PY
echo "ok   JIT kernel attaches the exact reviewed runtime output"
