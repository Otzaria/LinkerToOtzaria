#!/usr/bin/env bash
# Boots a fresh Kaggle GPU session into a ONE-JOB GitHub Actions JIT runner.
#
# Expects JIT_CONFIG in the environment — injected by scripts/dispatch_kaggle_relink.sh,
# which base64-embeds this file into the pushed kernel's run.py (the repo is private, so
# the kernel cannot fetch it itself). Installs exactly what relink.yml + ci/setup_stack.sh
# need (gh, mongod/mongorestore/mongosh, zstd) and hands off to the runner; the JIT
# runner executes a single job and deregisters, and the session dies with it — nothing
# here persists or needs cleanup.
set -euxo pipefail
[ -n "${JIT_CONFIG:-}" ] || { echo "JIT_CONFIG missing"; exit 1; }

echo "=== session resources ==="
head -2 /etc/os-release; nproc; free -h; df -h /; nvidia-smi || echo "NO GPU"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq zstd unzip curl git ca-certificates procps >/dev/null

TOOLS=/kaggle/temp/tools; mkdir -p "$TOOLS"; cd "$TOOLS"

# gh CLI — the workflow leans on it for every release/artifact download.
GH_VER=2.63.2
curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VER}/gh_${GH_VER}_linux_amd64.tar.gz" | tar -xz
ln -sf "$TOOLS/gh_${GH_VER}_linux_amd64/bin/gh" /usr/local/bin/gh

# MongoDB server + database tools + mongosh (ci/setup_stack.sh expects all three in PATH).
curl -fsSL https://fastdl.mongodb.org/linux/mongodb-linux-x86_64-ubuntu2204-7.0.14.tgz | tar -xz
ln -sf "$TOOLS"/mongodb-linux-x86_64-ubuntu2204-7.0.14/bin/mongod /usr/local/bin/mongod
curl -fsSL https://fastdl.mongodb.org/tools/db/mongodb-database-tools-ubuntu2204-x86_64-100.10.0.tgz | tar -xz
ln -sf "$TOOLS"/mongodb-database-tools-ubuntu2204-x86_64-100.10.0/bin/mongorestore /usr/local/bin/mongorestore
curl -fsSL https://downloads.mongodb.com/compass/mongosh-2.3.1-linux-x64.tgz | tar -xz
ln -sf "$TOOLS"/mongosh-2.3.1-linux-x64/bin/mongosh /usr/local/bin/mongosh
mongod --version | head -1; mongorestore --version | head -1; mongosh --version

# GitHub Actions runner (x64). RUNNER_ALLOW_RUNASROOT: Kaggle kernels run as root.
RUNNER_DIR=/kaggle/temp/actions-runner; mkdir -p "$RUNNER_DIR"; cd "$RUNNER_DIR"
RUNNER_VER=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['tag_name'].lstrip('v'))") || RUNNER_VER=2.321.0
curl -fsSL "https://github.com/actions/runner/releases/download/v${RUNNER_VER}/actions-runner-linux-x64-${RUNNER_VER}.tar.gz" | tar -xz
./bin/installdependencies.sh >/dev/null || true
export RUNNER_ALLOW_RUNASROOT=1
echo "=== runner v${RUNNER_VER} up — waiting for the queued relink job ==="
./run.sh --jitconfig "$JIT_CONFIG"
echo "=== job finished; session ends ==="
