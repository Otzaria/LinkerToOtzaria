#!/usr/bin/env bash
# Boots a fresh Kaggle GPU session into a ONE-JOB GitHub Actions JIT runner.
#
# Expects JIT_CONFIG in the environment — injected by scripts/dispatch_kaggle_relink.sh,
# which base64-embeds this file into the pushed kernel's run.py (the repo is private, so
# the kernel cannot fetch it itself). All third-party binaries (gh, mongod/mongorestore/
# mongosh, the Actions runner) come from the attached OUTPUT of the one-shot Kaggle
# kernels otzaria/linker-tools-fetcher and otzaria/linker-python-runtime
# (sha256-pinned outputs; big private datasets fail Kaggle's processing, kernel
# outputs do not). Only small distro packages come from the Ubuntu archive.
# The JIT runner executes a single job and deregisters, and the session dies with it —
# nothing here persists or needs cleanup.
set -euxo pipefail
[ -n "${JIT_CONFIG:-}" ] || { echo "JIT_CONFIG missing"; exit 1; }
PICKUP_TTL_SECONDS="${PICKUP_TTL_SECONDS:-1800}"
PICKUP_DEADLINE_EPOCH=$(( $(date +%s) + PICKUP_TTL_SECONDS ))

echo "=== session resources ==="
head -2 /etc/os-release; nproc; free -h; df -h /; nvidia-smi || echo "NO GPU"

# Pinned tool bundle — attached via kernel-metadata kernel_sources (the output of
# otzaria/linker-tools-fetcher). Required: the whole point is that boot does not
# depend on mongodb.org/github.com download URLs.
GH_VER=2.63.2
MONGO_VER=7.0.14
MONGO_TOOLS_VER=100.10.0
MONGOSH_VER=2.3.1
RUNNER_VER=2.335.1
TOOL_MARKER="gh_${GH_VER}_linux_amd64.tar.gz"
TOOL_MATCHES=$(find /kaggle/input -mindepth 2 -maxdepth 5 -type f -name "$TOOL_MARKER" -print)
TOOL_MATCH_COUNT=$(printf '%s\n' "$TOOL_MATCHES" | awk 'NF' | wc -l | tr -d ' ')
[ "$TOOL_MATCH_COUNT" -eq 1 ] || {
  echo "expected one attached $TOOL_MARKER; found $TOOL_MATCH_COUNT — attach kernel_sources otzaria/linker-tools-fetcher"
  exit 1
}
DS=$(dirname "$TOOL_MATCHES")
for f in "gh_${GH_VER}_linux_amd64.tar.gz" \
         "mongodb-linux-x86_64-ubuntu2204-${MONGO_VER}.tgz" \
         "mongodb-database-tools-ubuntu2204-x86_64-${MONGO_TOOLS_VER}.tgz" \
         "mongosh-${MONGOSH_VER}-linux-x64.tgz" \
         "actions-runner-linux-x64-${RUNNER_VER}.tar.gz"; do
  [ -f "$DS/$f" ] || { echo "kernel-output asset missing: $DS/$f — attach kernel_sources otzaria/linker-tools-fetcher"; exit 1; }
done

# Full pre-resolved Python environments. The 2.6GB archive replaces several GB of
# live PyPI/CUDA downloads on every ephemeral session. setup_stack verifies this
# hard-coded content digest and the source/requirements identities before install.
RUNTIME_NAME=linker-python-runtime-v1.tar.zst
export LINKER_RUNTIME_ROOT=/kaggle/input
export LINKER_RUNTIME_SHA256=bacad1b486c2bb392ee786bcc35b27dcc2beb17ea90b05f47352a06e44c8ff43
RUNTIME_MATCHES=$(find "$LINKER_RUNTIME_ROOT" -mindepth 2 -maxdepth 5 -type f -name "$RUNTIME_NAME" -print)
RUNTIME_MATCH_COUNT=$(printf '%s\n' "$RUNTIME_MATCHES" | awk 'NF' | wc -l | tr -d ' ')
[ "$RUNTIME_MATCH_COUNT" -eq 1 ] || {
  echo "expected one attached $RUNTIME_NAME; found $RUNTIME_MATCH_COUNT — attach kernel_sources otzaria/linker-python-runtime"
  exit 1
}
export LINKER_RUNTIME_ARCHIVE=$RUNTIME_MATCHES

export DEBIAN_FRONTEND=noninteractive
timeout 600 apt-get update -qq
# Sefaria pins django==6.0.4 and therefore needs Python >=3.12. Venv creation is
# not needed on the ephemeral path: both environments come from the verified
# runtime output. Persistent server fallback still provisions its own venv support.
timeout 900 apt-get install -y -qq zstd unzip curl git ca-certificates procps \
  python3.12 >/dev/null
python3.12 --version

TOOLS=/kaggle/temp/tools; mkdir -p "$TOOLS"; cd "$TOOLS"

# gh CLI — the workflow leans on it for every release/artifact download.
tar -xzf "$DS/gh_${GH_VER}_linux_amd64.tar.gz"
ln -sf "$TOOLS/gh_${GH_VER}_linux_amd64/bin/gh" /usr/local/bin/gh

# MongoDB server + database tools + mongosh (ci/setup_stack.sh expects all three in PATH).
tar -xzf "$DS/mongodb-linux-x86_64-ubuntu2204-${MONGO_VER}.tgz"
ln -sf "$TOOLS/mongodb-linux-x86_64-ubuntu2204-${MONGO_VER}/bin/mongod" /usr/local/bin/mongod
tar -xzf "$DS/mongodb-database-tools-ubuntu2204-x86_64-${MONGO_TOOLS_VER}.tgz"
ln -sf "$TOOLS/mongodb-database-tools-ubuntu2204-x86_64-${MONGO_TOOLS_VER}/bin/mongorestore" /usr/local/bin/mongorestore
tar -xzf "$DS/mongosh-${MONGOSH_VER}-linux-x64.tgz"
ln -sf "$TOOLS/mongosh-${MONGOSH_VER}-linux-x64/bin/mongosh" /usr/local/bin/mongosh
mongod --version | head -1; mongorestore --version | head -1; mongosh --version

# GitHub Actions runner (x64, pinned — no "latest" API call at boot).
# RUNNER_ALLOW_RUNASROOT: Kaggle kernels run as root.
RUNNER_DIR=/kaggle/temp/actions-runner; mkdir -p "$RUNNER_DIR"; cd "$RUNNER_DIR"
tar -xzf "$DS/actions-runner-linux-x64-${RUNNER_VER}.tar.gz"
./bin/installdependencies.sh >/dev/null || true
export RUNNER_ALLOW_RUNASROOT=1
# Kaggle exports PYTHONPATH pointing into its conda stack (kaggle_gcp, wrapt...);
# our clean 3.12 venvs inherit it and the NER workers die on those imports. The
# runner subtree must see a pristine python environment — anything that needs
# PYTHONPATH (the engine) sets it explicitly itself.
unset PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE
# Kaggle also drops a sitecustomize.py into the SYSTEM python3.x that intercepts
# every `google.cloud` import and demands kaggle_gcp (its GCP-credentials magic) —
# gpu-server imports google.cloud.storage and the NER worker dies on it. Remove it;
# we carry no Kaggle GCP integration.
rm -f /usr/lib/python3.*/sitecustomize.py
echo "=== runner v${RUNNER_VER} up — waiting for the queued relink job ==="
# Pickup TTL: a one-job JIT runner whose queued job was cancelled before pickup (a
# cancelled build, a reaped orphan) would otherwise idle until Kaggle kills the
# session (~9h of wasted GPU quota — the orphaned-session incident class). Watch for
# an actual job (a Runner.Worker child appears the moment one starts); if none has
# EVER started within the TTL, kill the runner and end the session. Once a job has
# started the watchdog exits and never limits the job's own (hours-long) runtime.
python3 - ./run.sh --jitconfig "$JIT_CONFIG" <<'PY' &
import os, sys
os.setsid()
os.execv(sys.argv[1], sys.argv[1:])
PY
RUNNER_PID=$!
JOB_STARTED=0
while kill -0 "$RUNNER_PID" 2>/dev/null; do
  if pgrep -f Runner.Worker >/dev/null 2>&1; then
    JOB_STARTED=1
    break
  fi
  if [ "$(date +%s)" -ge "$PICKUP_DEADLINE_EPOCH" ]; then
    echo "=== no job picked up by the absolute bootstrap deadline — ending the whole runner group ==="
    kill -TERM -- "-$RUNNER_PID" 2>/dev/null || true
    for _ in $(seq 1 60); do kill -0 "$RUNNER_PID" 2>/dev/null || break; sleep 0.25; done
    kill -KILL -- "-$RUNNER_PID" 2>/dev/null || true
    wait "$RUNNER_PID" 2>/dev/null || true
    exit 1
  fi
  sleep 5
done
RUNNER_RC=0
wait "$RUNNER_PID" || RUNNER_RC=$?
echo "=== job finished (runner rc=$RUNNER_RC); session ends ==="
exit "$RUNNER_RC"
