#!/usr/bin/env bash
# Restore the one authoritative complete linker artifact store, fail-closed.
set -euo pipefail

rm -rf artifacts line-baseline
TMP_ROOT="${RUNNER_TEMP:-/tmp}/linker-restore-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}"
rm -rf "$TMP_ROOT"
mkdir -p "$TMP_ROOT"
trap 'rm -rf "$TMP_ROOT"' EXIT
releases="$TMP_ROOT/releases.json"
selected="$TMP_ROOT/release.json"
payload="$TMP_ROOT/linker_links.zst"
archive="$TMP_ROOT/linker_links.tar"

gh api --paginate -X GET "repos/$GITHUB_REPOSITORY/releases" -f per_page=100 \
  | jq -s 'add' > "$releases"
jq -c '
  [.[]
   | select(.draft|not)
   | select(.tag_name|test("^(linker-release-sha256-[0-9a-f]{64}|linker-links-sha256-[0-9a-f]{64}|links-[A-Za-z0-9._-]{1,150})$"))
   | select([.assets[]|select(.name=="linker_links.zst")]|length==1)]
  | sort_by(.published_at,.id)
  | last // error("no published linker artifact-store release")' \
  "$releases" > "$selected"
asset_id="$(jq -r '.assets[]|select(.name=="linker_links.zst")|.id' "$selected")"
remote_size="$(jq -r '.assets[]|select(.name=="linker_links.zst")|.size' "$selected")"
remote_digest="$(jq -r '.assets[]|select(.name=="linker_links.zst")|.digest' "$selected")"
[[ "$asset_id" =~ ^[1-9][0-9]*$ ]]
[[ "$remote_size" =~ ^[1-9][0-9]*$ ]]
[[ "$remote_digest" =~ ^sha256:[0-9a-f]{64}$ ]]
gh api -H 'Accept: application/octet-stream' \
  "repos/$GITHUB_REPOSITORY/releases/assets/$asset_id" > "$payload"
[[ "$(stat -c %s "$payload")" == "$remote_size" ]]
[[ "$(sha256sum "$payload" | cut -d' ' -f1)" == "${remote_digest#sha256:}" ]]
zstd -q -t "$payload"
zstd -q -dc "$payload" > "$archive"
python3 - "$archive" <<'PY'
import pathlib, sys, tarfile
archive = pathlib.Path(sys.argv[1])
with tarfile.open(archive, "r:") as stream:
    members = stream.getmembers()
    if not members:
        raise SystemExit("empty linker artifact archive")
    names = set()
    jsonl = 0
    selected = []
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if member.name in names:
            raise SystemExit(f"duplicate linker artifact member: {member.name!r}")
        if member.name == "meta.json" and member.isfile():
            names.add(member.name)
            selected.append(member)
            continue
        if (path.is_absolute() or not path.parts
                or path.parts[0] not in {"artifacts", "line-baseline"}
                or ".." in path.parts or not (member.isdir() or member.isfile())):
            raise SystemExit(f"unsafe linker artifact member: {member.name!r}")
        if (member.isfile() and path.parts[0] == "line-baseline"
                and path.suffix != ".json"):
            raise SystemExit(f"unexpected line-baseline member: {member.name!r}")
        if (member.isfile() and path.parts[0] == "artifacts"
                and path.suffix != ".jsonl"):
            if path.name in {".DS_Store", ".gitkeep"} or path.name.startswith("._"):
                names.add(member.name)
                continue
            raise SystemExit(f"unexpected artifact member: {member.name!r}")
        names.add(member.name)
        jsonl += bool(member.isfile() and path.suffix == ".jsonl")
        selected.append(member)
    if not jsonl or "meta.json" not in names:
        raise SystemExit("linker artifact archive lacks payloads or meta.json")
    stream.extractall(path=".", members=selected, filter="data")
PY
echo "restored $(find artifacts -name '*.jsonl' | wc -l) artifact files"
if [ -f line-baseline/manifest.json ]; then
  echo "restored exact per-line reuse baseline"
else
  echo "no per-line reuse baseline in legacy release; changed books will use full NER"
fi
