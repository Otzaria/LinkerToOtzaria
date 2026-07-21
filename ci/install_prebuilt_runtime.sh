#!/usr/bin/env bash
# Verify and atomically install the two content-addressed linker venvs.
set -euo pipefail

[ "$#" -eq 5 ] || {
  echo "usage: $0 ARCHIVE EXPECTED_SHA256 STACK SEFARIA_ID GPU_SERVER_ID" >&2
  exit 2
}
ARCHIVE=$1
EXPECTED_SHA256=$2
STACK=$3
EXPECTED_SEFARIA_ID=$4
EXPECTED_GPU_ID=$5

[[ "$EXPECTED_SHA256" =~ ^[0-9a-f]{64}$ ]] || { echo "invalid runtime SHA-256" >&2; exit 2; }
[ -f "$ARCHIVE" ] || { echo "prebuilt runtime archive missing: $ARCHIVE" >&2; exit 1; }
[ -d "$STACK/Sefaria-Project" ] && [ -d "$STACK/gpu-server" ] || {
  echo "runtime destinations do not exist under $STACK" >&2
  exit 1
}
ACTUAL_SHA256=$(sha256sum "$ARCHIVE" | cut -d' ' -f1)
[ "$ACTUAL_SHA256" = "$EXPECTED_SHA256" ] || {
  echo "prebuilt runtime SHA-256 mismatch: $ACTUAL_SHA256 != $EXPECTED_SHA256" >&2
  exit 1
}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
python3 - "$ARCHIVE" "$EXPECTED_SEFARIA_ID" "$EXPECTED_GPU_ID" <<'PY'
import hashlib, json, pathlib, re, subprocess, sys, tarfile

archive = pathlib.Path(sys.argv[1])
expected_sefaria = sys.argv[2]
expected_gpu = sys.argv[3]
required_roots = {"sefaria-venv", "gpu-venv", "freeze", "runtime-manifest.json"}
seen_roots = set()
selected = {}
decompressor = subprocess.Popen(["zstd", "-q", "-dc", archive], stdout=subprocess.PIPE)
try:
  with tarfile.open(fileobj=decompressor.stdout, mode="r|") as stream:
    names = set()
    for member in stream:
        path = pathlib.PurePosixPath(member.name)
        if member.name in names or path.is_absolute() or not path.parts or ".." in path.parts:
            raise SystemExit(f"unsafe/duplicate runtime member: {member.name!r}")
        if not (member.isfile() or member.isdir() or member.issym()):
            raise SystemExit(f"unsupported runtime member type: {member.name!r}")
        if member.issym():
            target = pathlib.PurePosixPath(member.linkname)
            # venvs contain relative links such as bin/python -> python3.12 and
            # lib64 -> lib. They are safe while they remain below the archive
            # root. The sole absolute link is the exact system interpreter ABI.
            safe_relative = bool(target.parts) and not target.is_absolute() and ".." not in target.parts
            if not safe_relative and member.linkname != "/usr/bin/python3.12":
                raise SystemExit(f"unexpected runtime symlink target: {member.name!r} -> {member.linkname!r}")
        names.add(member.name)
        seen_roots.add(path.parts[0])
        if member.name in {"runtime-manifest.json", "freeze/sefaria.txt", "freeze/gpu-server.txt"}:
            if not member.isfile():
                raise SystemExit(f"runtime metadata is not a regular file: {member.name}")
            selected[member.name] = stream.extractfile(member).read()
finally:
  if decompressor.stdout:
    decompressor.stdout.close()
  if decompressor.wait() != 0:
    raise SystemExit("runtime archive decompression failed")
if not names:
    raise SystemExit("empty prebuilt runtime archive")
if seen_roots != required_roots:
    raise SystemExit(f"unexpected runtime archive roots: {sorted(seen_roots)!r}")
if set(selected) != {"runtime-manifest.json", "freeze/sefaria.txt", "freeze/gpu-server.txt"}:
    raise SystemExit("runtime archive is missing required metadata")
raw = selected["runtime-manifest.json"]
try:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate manifest key: {key}")
            result[key] = value
        return result
    manifest = json.loads(raw, object_pairs_hook=pairs)
except (ValueError, UnicodeDecodeError) as error:
    raise SystemExit(f"invalid runtime manifest JSON: {error}") from error
else:
    expected_keys = {
        "schema_version", "python_version", "platform", "machine",
        "sefaria_commit", "sefaria_requirements_sha256", "sefaria_freeze_sha256", "sefaria_identity",
        "gpu_server_commit", "gpu_server_requirements_sha256", "gpu_server_freeze_sha256", "gpu_server_identity",
        "builder_sha256",
    }
    if set(manifest) != expected_keys or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise SystemExit("invalid runtime manifest schema")
    if any(type(manifest[key]) is not str for key in expected_keys - {"schema_version"}):
        raise SystemExit("invalid runtime manifest value type")
    for key in ("sefaria_commit", "gpu_server_commit", "sefaria_requirements_sha256",
                "gpu_server_requirements_sha256", "sefaria_freeze_sha256",
                "gpu_server_freeze_sha256", "builder_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", manifest[key]):
            raise SystemExit(f"invalid runtime manifest digest: {key}")
    if manifest["python_version"] != "Python 3.12.13":
        raise SystemExit(f"unexpected runtime Python: {manifest['python_version']!r}")
    if manifest["sefaria_identity"] != expected_sefaria or manifest["gpu_server_identity"] != expected_gpu:
        raise SystemExit("prebuilt runtime identity does not match pinned source requirements")
    for filename, key in (("freeze/sefaria.txt", "sefaria_freeze_sha256"),
                          ("freeze/gpu-server.txt", "gpu_server_freeze_sha256")):
        content = selected[filename]
        if hashlib.sha256(content).hexdigest() != manifest[key]:
            raise SystemExit(f"runtime freeze digest mismatch: {filename}")
PY
zstd -q -dc "$ARCHIVE" | tar -xf - -C "$TMP"

[ "$(cat "$TMP/sefaria-venv/.identity")" = "$EXPECTED_SEFARIA_ID" ] || {
  echo "extracted Sefaria runtime identity mismatch" >&2; exit 1;
}
[ "$(cat "$TMP/gpu-venv/.identity")" = "$EXPECTED_GPU_ID" ] || {
  echo "extracted gpu-server runtime identity mismatch" >&2; exit 1;
}

# Verification is complete before either old environment is touched. These are
# cache directories inside exact, freshly checked-out repositories.
rm -rf "$STACK/Sefaria-Project/.venv" "$STACK/gpu-server/.venv"
mv "$TMP/sefaria-venv" "$STACK/Sefaria-Project/.venv"
mv "$TMP/gpu-venv" "$STACK/gpu-server/.venv"
cp "$TMP/freeze/sefaria.txt" "$STACK/Sefaria-Project/.venv/.freeze"
cp "$TMP/freeze/gpu-server.txt" "$STACK/gpu-server/.venv/.freeze"
printf '%s\n' "$ACTUAL_SHA256" > "$STACK/.python-runtime-archive-sha256"
echo "installed verified prebuilt Python runtime sha256=$ACTUAL_SHA256"
