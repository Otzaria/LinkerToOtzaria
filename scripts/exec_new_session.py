#!/usr/bin/env python3
"""Become a new session/process-group leader, then exec the requested argv."""
import os
import sys

if len(sys.argv) < 2:
    raise SystemExit("usage: exec_new_session.py COMMAND [ARG ...]")
os.setsid()
os.execvp(sys.argv[1], sys.argv[1:])
