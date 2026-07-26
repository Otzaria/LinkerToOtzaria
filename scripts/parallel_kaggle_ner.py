"""Dispatch, monitor and download a bounded set of parallel Kaggle NER shards."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path


GPU_ROW = re.compile(
    r"^GPU\s+([0-9.]+)h\s+([0-9.]+)h\s+([0-9.]+)h\s+",
    re.MULTILINE,
)


def command(*argv: str, capture: bool = False, check: bool = True) -> str:
    result = subprocess.run(
        list(argv),
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(argv)}\n"
            f"{result.stdout or ''}"
        )
    return result.stdout if capture else ""


def quota() -> tuple[float, float, float]:
    output = command("kaggle", "quota", capture=True)
    match = GPU_ROW.search(output)
    if match is None:
        raise RuntimeError(f"cannot parse Kaggle GPU quota:\n{output}")
    used, remaining, total = map(float, match.groups())
    return used, remaining, total


def kernel_ref(prefix: str, index: int) -> str:
    return f"otzaria/{prefix}-{index:02d}"


def prepare_kernel(
    state: Path,
    worker: Path,
    *,
    prefix: str,
    dataset: str,
    runtime_kernel: str,
    index: int,
    session_budget_seconds: int,
) -> Path:
    root = state / f"kernel-{index:02d}"
    root.mkdir(parents=True, exist_ok=True)
    worker_source = worker.read_text(encoding="utf-8")
    entrypoint = 'if __name__ == "__main__":\n    main()\n'
    replacement = (
        'if __name__ == "__main__":\n'
        f'    sys.argv.extend(["--shard-index", {str(index)!r}, '
        f'"--session-budget-seconds", {str(session_budget_seconds)!r}])\n'
        '    main()\n'
    )
    if entrypoint not in worker_source:
        raise RuntimeError("cannot locate Kaggle worker entrypoint")
    (root / "run.py").write_text(
        worker_source.replace(entrypoint, replacement),
        encoding="utf-8",
    )
    reference = kernel_ref(prefix, index)
    (root / "kernel-metadata.json").write_text(
        json.dumps({
            "id": reference,
            "title": f"{prefix}-{index:02d}",
            "code_file": "run.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": True,
            "enable_internet": True,
            "dataset_sources": [dataset],
            "kernel_sources": [runtime_kernel],
            "competition_sources": [],
        }, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return root


def dispatch(args) -> None:
    used, remaining, total = quota()
    projected = args.count * args.session_budget_seconds / 3600
    reserve_after = remaining - projected
    if reserve_after < args.reserve_hours:
        raise RuntimeError(
            f"bounded sessions could consume {projected:.2f}h; Kaggle reports "
            f"{remaining:.2f}h remaining, leaving only {reserve_after:.2f}h "
            f"(< required {args.reserve_hours:.2f}h)"
        )
    worker = Path(__file__).resolve().parents[1] / "kaggle" / "parallel_ner_worker.py"
    roots = [
        prepare_kernel(
            args.state,
            worker,
            prefix=args.prefix,
            dataset=args.dataset,
            runtime_kernel=args.runtime_kernel,
            index=index,
            session_budget_seconds=args.session_budget_seconds,
        )
        for index in range(args.count)
    ]

    (args.state / "dispatch.json").write_text(
        json.dumps({
            "schema_version": 1,
            "prefix": args.prefix,
            "dataset": args.dataset,
            "runtime_kernel": args.runtime_kernel,
            "count": args.count,
            "session_budget_seconds": args.session_budget_seconds,
            "quota_before": {"used": used, "remaining": remaining, "total": total},
            "maximum_projected_gpu_hours": projected,
            "minimum_reserved_gpu_hours": reserve_after,
            "max_active": args.max_active,
            "kernels": [kernel_ref(args.prefix, index) for index in range(args.count)],
        }, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    reconcile(args.state, max_active=args.max_active, reserve_hours=args.reserve_hours)


def load_dispatch(state: Path) -> dict:
    return json.loads((state / "dispatch.json").read_text(encoding="utf-8"))


def statuses(state: Path) -> list[tuple[str, str]]:
    document = load_dispatch(state)
    values = []
    for reference in document["kernels"]:
        output = command(
            "kaggle", "kernels", "status", reference,
            capture=True, check=False,
        ).strip()
        values.append((reference, output))
    return values


def state_of(value: str) -> str:
    upper = value.upper()
    if any(word in upper for word in (
        "404 CLIENT ERROR",
        "CANNOT ACCESS KERNEL",
        "NOT FOUND",
        "PERMISSION 'KERNELS.GET' WAS DENIED",
    )):
        return "pending"
    if "COMPLETE" in upper:
        return "complete"
    if any(word in upper for word in ("RUNNING", "QUEUED", "INITIALIZ", "STARTING")):
        return "active"
    if any(word in upper for word in ("ERROR", "FAILED")):
        return "failed"
    return "pending"


def push_kernel(state: Path, reference: str) -> str:
    index = int(reference.rsplit("-", 1)[1])
    root = state / f"kernel-{index:02d}"
    return command(
        "kaggle", "kernels", "push",
        "--accelerator", "GPU",
        "-p", str(root),
        capture=True,
    ).strip()


def reconcile(state: Path, *, max_active: int, reserve_hours: float) -> bool:
    values = statuses(state)
    states = [(reference, value, state_of(value)) for reference, value in values]
    for reference, value, category in states:
        print(f"{reference}\t{category}\t{value}", flush=True)
    failed = [(reference, value) for reference, value, category in states if category == "failed"]
    if failed:
        detail = "\n".join(f"{reference}: {value}" for reference, value in failed)
        raise RuntimeError(f"Kaggle shard failed; inspect before retry:\n{detail}")
    if all(category == "complete" for _, _, category in states):
        return True

    active = sum(category == "active" for _, _, category in states)
    pending = [reference for reference, _, category in states if category == "pending"]
    available = max(0, max_active - active)
    if not available or not pending:
        return False

    document = load_dispatch(state)
    budget_hours = document["session_budget_seconds"] / 3600
    _, remaining, _ = quota()
    newly_reserved = 0.0
    for reference in pending[:available]:
        if remaining - newly_reserved - budget_hours < reserve_hours:
            raise RuntimeError(
                f"refusing to launch {reference}: one bounded session could leave "
                f"less than {reserve_hours:.2f} GPU hours"
            )
        try:
            print(f"{reference}\tlaunch\t{push_kernel(state, reference)}", flush=True)
            newly_reserved += budget_hours
        except RuntimeError as error:
            if "Maximum batch GPU session count" not in str(error):
                raise
            print(f"{reference}\tdeferred\t{error}", flush=True)
            break
    return False


def status(args) -> None:
    for reference, value in statuses(args.state):
        print(f"{reference}\t{value}")
    used, remaining, total = quota()
    print(f"quota\tused={used:.2f}h remaining={remaining:.2f}h total={total:.2f}h")


def run_queue(args) -> None:
    document = load_dispatch(args.state)
    max_active = args.max_active or document.get("max_active", 2)
    while True:
        if reconcile(
            args.state,
            max_active=max_active,
            reserve_hours=args.reserve_hours,
        ):
            print("all Kaggle NER shards complete", flush=True)
            return
        time.sleep(args.poll_seconds)


def download(args) -> None:
    args.output.mkdir(parents=True, exist_ok=True)
    for reference, value in statuses(args.state):
        if "COMPLETE" not in value:
            print(f"skip {reference}: {value}", file=sys.stderr)
            continue
        destination = args.output / reference.rsplit("/", 1)[1]
        destination.mkdir(parents=True, exist_ok=True)
        command(
            "kaggle", "kernels", "output", reference,
            "-p", str(destination), "--force",
        )
        print(f"downloaded {reference} -> {destination}")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command_name", required=True)
    create = subparsers.add_parser("dispatch")
    create.add_argument("--state", type=Path, required=True)
    create.add_argument("--dataset", required=True)
    create.add_argument("--prefix", required=True)
    create.add_argument("--runtime-kernel", default="otzaria/linker-python-runtime")
    create.add_argument("--count", type=int, default=10)
    create.add_argument("--session-budget-seconds", type=int, default=9_700)
    create.add_argument("--reserve-hours", type=float, default=2.0)
    create.add_argument("--max-active", type=int, default=2)
    create.set_defaults(function=dispatch)
    inspect = subparsers.add_parser("status")
    inspect.add_argument("--state", type=Path, required=True)
    inspect.set_defaults(function=status)
    queue = subparsers.add_parser("run")
    queue.add_argument("--state", type=Path, required=True)
    queue.add_argument("--max-active", type=int)
    queue.add_argument("--reserve-hours", type=float, default=2.0)
    queue.add_argument("--poll-seconds", type=int, default=60)
    queue.set_defaults(function=run_queue)
    fetch = subparsers.add_parser("download")
    fetch.add_argument("--state", type=Path, required=True)
    fetch.add_argument("--output", type=Path, required=True)
    fetch.set_defaults(function=download)
    args = parser.parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
