#!/usr/bin/env bash
# Boots a fresh Kaggle GPU session into a ONE-JOB GitHub Actions JIT runner.
#
# Expects JIT_CONFIG in the environment — injected by scripts/dispatch_kaggle_relink.sh,
# which base64-embeds this file into the pushed kernel's run.py (the repo is private, so
# the kernel cannot fetch it itself). All third-party binaries (gh, mongod/mongorestore/
# mongosh, the Actions runner) come from the attached Kaggle dataset
# otzaria/linker-runner-tools — no fragile third-party URLs at boot time; the dataset is
# the pinned, years-stable source. Only distro packages come from the Ubuntu archive.
# The JIT runner executes a single job and deregisters, and the session dies with it —
# nothing here persists or needs cleanup.
set -euxo pipefail
[ -n "${JIT_CONFIG:-}" ] || { echo "JIT_CONFIG missing"; exit 1; }

echo "=== session resources ==="
head -2 /etc/os-release; nproc; free -h; df -h /; nvidia-smi || echo "NO GPU"

# Pinned tool bundle — attached via kernel-metadata dataset_sources. Required: the
# whole point is that boot does not depend on mongodb.org/github.com download URLs.
DS=/kaggle/input/linker-runner-tools
GH_VER=2.63.2
MONGO_VER=7.0.14
MONGO_TOOLS_VER=100.10.0
MONGOSH_VER=2.3.1
RUNNER_VER=2.335.1
for f in "gh_${GH_VER}_linux_amd64.tar.gz" \
         "mongodb-linux-x86_64-ubuntu2204-${MONGO_VER}.tgz" \
         "mongodb-database-tools-ubuntu2204-x86_64-${MONGO_TOOLS_VER}.tgz" \
         "mongosh-${MONGOSH_VER}-linux-x64.tgz" \
         "actions-runner-linux-x64-${RUNNER_VER}.tar.gz"; do
  [ -f "$DS/$f" ] || { echo "dataset asset missing: $DS/$f — attach otzaria/linker-runner-tools"; exit 1; }
done

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# python3.12-venv: the image ships python3.12 with broken ensurepip (and Sefaria's
# requirements pin django==6.0.4 which demands >=3.12, so an easier 3.11 is out) —
# setup_stack's venv-capability probe needs a 3.12 that can actually make venvs.
apt-get install -y -qq zstd unzip curl git ca-certificates procps \
  python3.12 python3.12-venv >/dev/null
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
./run.sh --jitconfig "$JIT_CONFIG"
echo "=== job finished; session ends ==="
