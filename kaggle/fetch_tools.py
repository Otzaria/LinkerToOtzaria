# linker-tools-fetcher — one-shot CPU kernel whose OUTPUT is the pinned tool bundle.
# Downstream kernels attach it via kernel_sources and read the tarballs from
# /kaggle/input/linker-tools-fetcher/. Every file is sha256-pinned: a silent
# upstream re-tag can never slip through.
import hashlib
import shutil
import urllib.request

TOOLS = [
    ("https://github.com/cli/cli/releases/download/v2.63.2/gh_2.63.2_linux_amd64.tar.gz",
     "gh_2.63.2_linux_amd64.tar.gz",
     "912fdb1ca29cb005fb746fc5d2b787a289078923a29d0f9ec19a0b00272ded00"),
    ("https://fastdl.mongodb.org/linux/mongodb-linux-x86_64-ubuntu2204-7.0.14.tgz",
     "mongodb-linux-x86_64-ubuntu2204-7.0.14.tgz",
     "b4cf8caae108785135ecc8b86ad8ed6df5d6efb84b9b95a50ecba2fc246ce084"),
    ("https://fastdl.mongodb.org/tools/db/mongodb-database-tools-ubuntu2204-x86_64-100.10.0.tgz",
     "mongodb-database-tools-ubuntu2204-x86_64-100.10.0.tgz",
     "e92ef9448aeb347216f253ca3cd34ef5490102d4c6269353c799f51b69206e4a"),
    ("https://downloads.mongodb.com/compass/mongosh-2.3.1-linux-x64.tgz",
     "mongosh-2.3.1-linux-x64.tgz",
     "f1fefacf0b5b1f2fca966200478fee1e278be2619df5e2605cbc0f24dd179a1a"),
    ("https://github.com/actions/runner/releases/download/v2.335.1/actions-runner-linux-x64-2.335.1.tar.gz",
     "actions-runner-linux-x64-2.335.1.tar.gz",
     "4ef2f25285f0ae4477f1fe1e346db76d2f3ebf03824e2ddd1973a2819bf6c8cf"),
]

for url, name, expected in TOOLS:
    dest = f"/kaggle/working/{name}"
    print(f"fetching {name} ...", flush=True)
    with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    h = hashlib.sha256()
    with open(dest, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected:
        raise SystemExit(f"sha256 mismatch for {name}: {actual} != {expected}")
    print(f"ok {name} sha256={actual}", flush=True)

print("all tools fetched and verified", flush=True)
