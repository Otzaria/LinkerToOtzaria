#!/usr/bin/env bash
# Publish an exact, immutable set of files as a non-draft pre-release.
set -euo pipefail

tag=${1:?tag is required}
title=${2:?title is required}
target=${3:?target commit is required}
shift 3
[ "$#" -gt 0 ] || { echo "::error::at least one handoff asset is required"; exit 2; }
[[ "$tag" =~ ^[A-Za-z0-9._-]{1,240}$ ]]
[[ "$target" =~ ^[0-9a-f]{40}$ ]]
: "${GH_TOKEN:?GH_TOKEN is required}"

for path in "$@"; do
  [ -f "$path" ] || { echo "::error::missing handoff asset $path"; exit 1; }
  name=${path##*/}
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$ ]] || {
    echo "::error::release asset basename is unsafe or would be normalized by GitHub: $name"
    exit 2
  }
  [ "$(stat --format='%s' "$path")" -le 2147483647 ] || {
    echo "::error::handoff asset exceeds GitHub's 2 GiB limit: $path"; exit 1;
  }
done

state="$RUNNER_TEMP/release-handoff-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}.json"
read_release() {
  # Older gh versions (including Kaggle's pinned client) omit asset.digest
  # from `release view --json assets`; the REST response is authoritative.
  gh api "repos/$GITHUB_REPOSITORY/releases/tags/$tag" --jq \
    '{isDraft:.draft,isPrerelease:.prerelease,targetCommitish:.target_commitish,assets:[.assets[]|{name,size,digest}]}'
}

if ! read_release > "$state" 2>/dev/null; then
  if ! gh release create "$tag" --target "$target" --title "$title" \
      --notes "Immutable workflow handoff. Consumers verify every asset digest." --prerelease; then
    read_release > "$state"
  fi
fi

verify_and_list_missing() {
  read_release > "$state"
  python3 - "$state" "$target" "$@" <<'PY'
import hashlib,json,sys
from pathlib import Path
value=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
target=sys.argv[2]
paths=[Path(item) for item in sys.argv[3:]]
if value.get("isDraft") is not False or value.get("isPrerelease") is not True:
    raise SystemExit("handoff release is not a published pre-release")
if value.get("targetCommitish") != target:
    raise SystemExit("handoff release target commit differs")
if len({path.name for path in paths}) != len(paths):
    raise SystemExit("handoff assets must have unique basenames")
remote=value.get("assets",[])
actual={item["name"]:(item["size"],item.get("digest")) for item in remote}
if len(actual)!=len(remote): raise SystemExit("duplicate remote handoff asset names")
expected={}
for path in paths:
    digest=hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda:source.read(1024*1024),b""): digest.update(chunk)
    expected[path.name]=(path.stat().st_size,"sha256:"+digest.hexdigest())
unexpected=set(actual)-set(expected)
if unexpected: raise SystemExit(f"unexpected handoff assets: {sorted(unexpected)}")
for name in actual:
    if actual[name][0]!=expected[name][0]: raise SystemExit(f"handoff size conflict: {name}")
    if actual[name][1] not in (None,"",expected[name][1]):
        raise SystemExit(f"handoff digest conflict: {name}")
for path in paths:
    if path.name not in actual: print(path)
PY
}

missing="$RUNNER_TEMP/release-handoff-missing-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}"
verify_and_list_missing "$@" > "$missing"
while IFS= read -r path; do
  [ -n "$path" ] || continue
  gh release upload "$tag" "$path"
done < "$missing"

verified=false
for _ in $(seq 1 24); do
  if verify_and_list_missing "$@" > "$missing" && [ ! -s "$missing" ]; then
    if python3 - "$state" "$@" <<'PY'
import hashlib,json,sys
from pathlib import Path
remote=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["assets"]
actual={item["name"]:(item["size"],item.get("digest")) for item in remote}
expected={}
for path in map(Path,sys.argv[2:]):
    digest=hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda:source.read(1024*1024),b""): digest.update(chunk)
    expected[path.name]=(path.stat().st_size,"sha256:"+digest.hexdigest())
if actual!=expected: raise SystemExit(1)
PY
    then verified=true; break; fi
  fi
  sleep 5
done
[ "$verified" = true ] || { echo "::error::release handoff never became byte-exact"; exit 1; }
echo "Published immutable release handoff $tag"
