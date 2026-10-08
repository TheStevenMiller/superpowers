"""Pure invocation policy shared by preparation, approval and execution."""

import shlex
from typing import Any

Invocation = dict[str, Any]
Manifest = dict[str, Any]
Event = dict[str, Any]
Response = dict[str, Any]

SUPPORTED_INTERPRETERS = frozenset({"/usr/bin/python3.11", "/usr/bin/bash"})
FORBIDDEN_COMMAND_CHARS = frozenset(";&|<>$`\\*?[]{}()~#\n\r\x00")


class IntegrityError(Exception):
    """A sanitized integrity or unsupported-context failure."""


def validate_absolute_path(path: str) -> None:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise IntegrityError("expected absolute path")
    if path != "/" and any(part in {"", ".", ".."} for part in path[1:].split("/")):
        raise IntegrityError("ambiguous absolute path spelling")


def literal_argv(invocation: Invocation) -> list[str]:
    """Parse an already schema-checked invocation without evaluating shell code."""
    if invocation["tool"] != "Bash" or invocation["login"] is not False or invocation["tty"] is not False:
        raise IntegrityError("unsupported invocation context")
    validate_absolute_path(invocation["workdir"])
    validate_absolute_path(invocation["shell"])
    command = invocation["command"]
    if not isinstance(command, str) or not command or any(c in FORBIDDEN_COMMAND_CHARS for c in command):
        raise IntegrityError("unsupported command syntax")
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise IntegrityError("unsupported command syntax") from exc
    if not argv:
        raise IntegrityError("empty command")
    return argv


def validate_command(invocation: Invocation) -> str:
    argv = literal_argv(invocation)
    if len(argv) < 2 or argv[0] not in SUPPORTED_INTERPRETERS:
        raise IntegrityError("unsupported launch form")
    validate_absolute_path(argv[0])
    validate_absolute_path(argv[1])
    return argv[1]


def validate_entrypoint(invocation: Invocation, candidate: Manifest) -> None:
    entrypoint = validate_command(invocation)
    if entrypoint not in {item["path"] for item in candidate["files"]}:
        raise IntegrityError("entrypoint is not in the approved snapshot")
