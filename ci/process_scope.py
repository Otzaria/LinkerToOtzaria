#!/usr/bin/env python3
"""Record and terminate a pipeline-owned Linux process group safely.

A PID file is not an ownership proof: PIDs are reused.  The state written here
binds a process-group leader to its /proc start time, uid and command line.  A
later teardown signals the group only after every binding still matches.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import tempfile
import time


SCHEMA = 1


def proc_identity(pid: int) -> dict:
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    # comm is parenthesized and may contain spaces.  Everything after the last
    # ')' starts at field 3; starttime is field 22, hence index 19 below.
    tail = stat.rsplit(")", 1)[1].strip().split()
    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    return {
        "pid": pid,
        "pgid": os.getpgid(pid),
        "start_ticks": int(tail[19]),
        "uid": Path(f"/proc/{pid}").stat().st_uid,
        "cmdline": cmdline,
    }


def canonical_write(path: Path, value: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)


def load_strict(path: Path) -> dict:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError(f"duplicate key {key!r}")
            out[key] = value
        return out

    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    if (
        set(value) != {"schema_version", "kind", "identity"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA
        or not isinstance(value["kind"], str)
    ):
        raise ValueError("unknown process-scope schema")
    identity = value["identity"]
    required = {"pid", "pgid", "start_ticks", "uid", "cmdline"}
    if not isinstance(identity, dict) or set(identity) != required:
        raise ValueError("process identity has an unexpected key set")
    for key in ("pid", "pgid", "start_ticks", "uid"):
        if type(identity[key]) is not int or identity[key] < 0:
            raise ValueError(f"invalid process identity {key}")
    if identity["pid"] < 1 or identity["pgid"] != identity["pid"]:
        raise ValueError("process identity must describe a positive group leader")
    if not isinstance(identity["cmdline"], str) or not identity["cmdline"]:
        raise ValueError("process identity cmdline must be a non-empty string")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if raw != canonical:
        raise ValueError("process-scope state is not canonical JSON")
    return value


def record(args) -> int:
    ident = proc_identity(args.pid)
    if ident["pgid"] != args.pid:
        raise SystemExit("process is not a process-group/session leader")
    if args.expect not in ident["cmdline"]:
        raise SystemExit(f"process command does not contain expected marker {args.expect!r}")
    canonical_write(
        Path(args.state),
        {"schema_version": SCHEMA, "kind": args.kind, "identity": ident},
        args.mode,
    )
    return 0


def identity_matches(recorded: dict, expected: str) -> bool:
    try:
        current = proc_identity(recorded["pid"])
    except (FileNotFoundError, ProcessLookupError):
        return False
    return current == recorded and recorded["pgid"] == recorded["pid"] and expected in recorded["cmdline"]


def group_has_live_members(pgid: int) -> bool:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text()
            tail = text.rsplit(")", 1)[1].strip().split()
            state, process_group = tail[0], int(tail[2])  # fields 3 and 5
            if process_group == pgid and state != "Z":
                return True
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
            continue
    return False


def terminate(args) -> int:
    state = Path(args.state)
    if not state.exists():
        print("no process-scope state — nothing to stop")
        return 0
    try:
        value = load_strict(state)
        identity = value["identity"]
        matches = identity_matches(identity, args.expect)
    except Exception as exc:
        print(f"refusing to signal: invalid process-scope state: {exc}", file=os.sys.stderr)
        return 2
    if not matches:
        # A gone/reused leader is never authority to kill.  But if its recorded
        # process group still has live descendants, do NOT erase the only forensic
        # ownership record or pretend cleanup succeeded: fail loudly and keep the
        # lease blocked for explicit recovery.
        try:
            pgid = identity["pgid"]
            if group_has_live_members(pgid):
                print(
                    f"refusing to signal group {pgid}: leader identity no longer matches but descendants remain",
                    file=os.sys.stderr,
                )
                return 2
        except Exception as exc:
            print(f"refusing to discard unverifiable stale scope: {exc}", file=os.sys.stderr)
            return 2
        state.unlink(missing_ok=True)
        print("stale process-scope state removed; no process was signalled")
        return 0

    pgid = identity["pgid"]
    os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + args.grace
    while time.monotonic() < deadline:
        if not group_has_live_members(pgid):
            break
        time.sleep(0.2)
    else:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    # Wait for the group to disappear; do not report success while descendants live.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not group_has_live_members(pgid):
            break
        time.sleep(0.1)
    else:
        print(f"process group {pgid} still exists after SIGKILL", file=os.sys.stderr)
        return 1
    state.unlink(missing_ok=True)
    print(f"terminated owned {value['kind']} process group {pgid}")
    return 0


def check(args) -> int:
    state = Path(args.state)
    if not state.exists():
        return 1
    try:
        value = load_strict(state)
        return 0 if identity_matches(value["identity"], args.expect) else 1
    except Exception as exc:
        print(f"invalid process-scope state: {exc}", file=os.sys.stderr)
        return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--state", required=True)
    rec.add_argument("--pid", type=int, required=True)
    rec.add_argument("--kind", required=True)
    rec.add_argument("--expect", required=True)
    rec.add_argument("--mode", type=lambda value: int(value, 8), default=0o600)
    rec.set_defaults(func=record)
    stop = sub.add_parser("terminate")
    stop.add_argument("--state", required=True)
    stop.add_argument("--expect", required=True)
    stop.add_argument("--grace", type=float, default=20)
    stop.set_defaults(func=terminate)
    chk = sub.add_parser("check")
    chk.add_argument("--state", required=True)
    chk.add_argument("--expect", required=True)
    chk.set_defaults(func=check)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
