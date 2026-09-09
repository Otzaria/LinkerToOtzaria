#!/usr/bin/env python3
"""Validate the terminal run a local batch checkpoint may be resumed from."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


WORKFLOW_PATH = ".github/workflows/relink.yml"
# Only a run that stopped mid-flight can have left a partial checkpoint behind.
# NEVER widen this to "success": a run that finished published its payload instead.
TERMINAL_CONCLUSIONS = ("cancelled", "failure")
# The FIRST recovery of a cycle can only ever chain from the original
# "relink request=…" attempt; every later one chains from the previous
# "relink-recovery request=…" attempt. Both run-names carry the SAME request id and
# the SAME parent coordinates, so request identity and serial topology stay exact
# either way — see the run-name template at the top of relink.yml.
TITLE_PREFIXES = ("relink", "relink-recovery")


def accepted_titles(args: argparse.Namespace) -> list[str]:
    parent = f"parent={args.parent_run_id}:{args.parent_run_attempt}"
    return [f"{prefix} request={args.request_id} {parent}" for prefix in TITLE_PREFIXES]


def rejections(source: dict, args: argparse.Namespace) -> list[str]:
    """Every condition this source fails — the operator must never have to guess."""
    reasons = []
    if source.get("status") != "completed":
        reasons.append(f"status {source.get('status')!r} is not 'completed'")
    if source.get("conclusion") not in TERMINAL_CONCLUSIONS:
        expected = " or ".join(repr(value) for value in TERMINAL_CONCLUSIONS)
        reasons.append(f"conclusion {source.get('conclusion')!r} is not terminal (expected {expected})")
    if source.get("event") != "workflow_dispatch":
        reasons.append(f"event {source.get('event')!r} is not 'workflow_dispatch'")
    if source.get("path") != WORKFLOW_PATH:
        reasons.append(f"path {source.get('path')!r} is not {WORKFLOW_PATH!r}")
    attempt = source.get("run_attempt")
    if type(attempt) is not int or attempt != args.run_attempt:
        reasons.append(f"run_attempt {attempt!r} is not {args.run_attempt}")
    if source.get("display_title") not in accepted_titles(args):
        expected = " nor ".join(repr(title) for title in accepted_titles(args))
        reasons.append(f"display_title {source.get('display_title')!r} is neither {expected}")
    if source.get("head_sha") != args.head_sha:
        reasons.append(f"head_sha {source.get('head_sha')!r} is not this Linker commit {args.head_sha!r}")
    return reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_json", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--parent-run-id", required=True)
    parser.add_argument("--parent-run-attempt", required=True)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args(argv)
    try:
        source = json.loads(args.source_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        reasons = [f"run metadata is unreadable ({error})"]
    else:
        reasons = rejections(source, args) if isinstance(source, dict) else ["run metadata is not a JSON object"]
    if not reasons:
        return 0
    for reason in reasons:
        print(f"checkpoint source {args.run_id} rejected: {reason}")
    print("::error::local checkpoint source is not the exact terminal recovery attempt at this Linker commit")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
