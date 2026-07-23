#!/usr/bin/env python3
"""Validate one bounded, single-line printable-ASCII workflow input."""

import sys


def main() -> int:
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} NAME MAX_BYTES VALUE", file=sys.stderr)
        return 2
    name, raw_limit, value = sys.argv[1:]
    try:
        limit = int(raw_limit)
        encoded = value.encode("ascii", "strict")
    except (UnicodeEncodeError, ValueError):
        print(f"::error::{name} must be printable ASCII", file=sys.stderr)
        return 1
    if limit < 1 or not 1 <= len(encoded) <= limit or any(byte < 0x20 or byte > 0x7E for byte in encoded):
        print(f"::error::{name} must be single-line printable ASCII (max {limit} bytes)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
