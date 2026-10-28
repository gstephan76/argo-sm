#!/usr/bin/env python3
"""Bounded retries for read-only OpenShift operations in full-multi history tools.

Only GETs are retried. Delete operations never retry automatically because a
network timeout does not establish whether the API server applied the delete.
The callers re-read live Rollout state and validate safe deletion themselves.
"""
import os
import subprocess
import sys
import time

_TRANSIENT = (
    "timeout", "timed out", "deadline exceeded", "connection reset",
    "connection refused", "connection closed", "connection aborted",
    "temporarily unavailable", "service unavailable", "server is currently unable",
    "tls handshake", "i/o timeout", "eof", "too many requests",
    "bad gateway", "gateway timeout", " 502", " 503", " 504", " 429",
)


def _positive_int(name, default, maximum):
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from exc
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be 1..{maximum}, got {value}")
    return value


def run_oc(*args):
    """Run oc with retries for transient GET errors; never retry mutations."""
    request_seconds = _positive_int("FULL_MULTI_OC_REQUEST_TIMEOUT", 30, 120)
    attempts = (_positive_int("FULL_MULTI_OC_READ_ATTEMPTS", 3, 5)
                if args and args[0] == "get" else 1)
    command = ["oc", f"--request-timeout={request_seconds}s", *args]
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(
                command, capture_output=True, text=True,
                timeout=request_seconds + 15, check=False,
            )
        except subprocess.TimeoutExpired:
            if attempt == attempts:
                raise
            print(f"WARN: oc GET subprocess timed out (attempt {attempt}/{attempts}); retrying",
                  file=sys.stderr, flush=True)
        else:
            if result.returncode == 0:
                return result.stdout
            transient = any(word in result.stderr.lower() for word in _TRANSIENT)
            if attempt == attempts or not transient:
                if result.stderr:
                    print(result.stderr.rstrip(), file=sys.stderr, flush=True)
                raise subprocess.CalledProcessError(
                    result.returncode, command,
                    output=result.stdout, stderr=result.stderr,
                )
            print(f"WARN: transient oc GET failure (attempt {attempt}/{attempts}): "
                  f"{result.stderr.strip()[:250]}", file=sys.stderr, flush=True)
        time.sleep(min(2 ** attempt, 8))
    raise AssertionError("unreachable")
