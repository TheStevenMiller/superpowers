"""Preparation-only CLI: snapshot and show declared files, never approve or run."""

import argparse
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any

from manifest import PREPARE_SECONDS, canonical_bytes, load_json, prepare_manifest
from policy import IntegrityError, validate_absolute_path
from store import publish_candidate

STATE_DIR = Path("/home/user/.local/state/execution-integrity-hook")


def _check(deadline):
    if time.monotonic() >= deadline:
        raise IntegrityError("preparation deadline exceeded")


def prepare_request(request_path: Path, state_dir: Path, *, deadline: float | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + PREPARE_SECONDS if deadline is None else deadline
    _check(deadline)
    validate_absolute_path(str(request_path))
    try:
        fd = os.open(request_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise IntegrityError("request is not a regular file")
            chunks = []
            while True:
                _check(deadline)
                chunk = os.read(fd, 65536)
                _check(deadline)
                if not chunk:
                    break
                chunks.append(chunk)
            request = load_json(b"".join(chunks))
            _check(deadline)
        finally:
            os.close(fd)
    except OSError as exc:
        raise IntegrityError("unable to read preparation request") from exc
    candidate = prepare_manifest(request, deadline=deadline)
    digest = publish_candidate(state_dir, candidate, deadline=deadline)
    _check(deadline)
    return {"digest": digest, "manifest": candidate}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare declared inputs; never run or approve them.")
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = prepare_request(args.request, STATE_DIR)
        candidate = result["manifest"]
        review = {"digest": result["digest"], "invocation": candidate["invocation"],
                  "roots": candidate["roots"], "files": candidate["files"]}
        print(canonical_bytes(review).decode("utf-8"))
        return 0
    except IntegrityError:
        print("Preparation failed; no approval recorded.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
