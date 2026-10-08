"""No-write preview and reversible, offline-testable release configuration.

The production install command is deliberately unavailable until native host
context and authenticated human prompt delivery have been established.
"""

import sys

if __name__ == "__main__":
    # Preview/unsupported install must not create local import caches on startup.
    sys.dont_write_bytecode = True

import copy
from contextlib import ExitStack
import argparse
import difflib
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import time
import tomllib

from manifest import CHECK_SECONDS, canonical_bytes, load_json
from policy import IntegrityError, validate_absolute_path

CONFIG_PATH = Path("/home/user/.codex/hooks.json")
RELEASE_DIR = Path("/home/user/.local/share/execution-integrity-hook/v1")
SOURCE_DIR = Path(__file__).absolute().parent
RUNTIME_FILES = ("policy.py", "manifest.py", "store.py", "hook.py", "prepare.py", "configure.py")
MAX_FILE_BYTES = 8 * 1024 * 1024


def _check(deadline):
    if time.monotonic() >= deadline:
        raise IntegrityError("configuration deadline exceeded")


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _file_state(metadata):
    return (_identity(metadata), metadata.st_mode, metadata.st_uid, metadata.st_gid,
            metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def _filesystem_errors(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except OSError as exc:
            raise IntegrityError("cannot read/write configuration or runtime path; check missing files and permissions") from exc
    return wrapped


class _Parent:
    """Hold no-follow directory bindings through reads and atomic replacement."""

    def __init__(self, path, deadline, *, create=False):
        self.path = Path(path)
        validate_absolute_path(str(self.path))
        self.deadline = deadline
        self.create = create
        self.stack = ExitStack()
        self.bindings = []

    def __enter__(self):
        try:
            _check(self.deadline)
            self.fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            self.stack.callback(os.close, self.fd)
            for name in self.path.parts[1:-1]:
                _check(self.deadline)
                try:
                    before = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                except FileNotFoundError:
                    if not self.create:
                        raise
                    os.mkdir(name, 0o700, dir_fd=self.fd)
                    os.fsync(self.fd)
                    before = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                if not stat.S_ISDIR(before.st_mode):
                    raise IntegrityError("symlink or non-directory configuration path")
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=self.fd)
                self.stack.callback(os.close, child)
                if _identity(before) != _identity(os.fstat(child)):
                    raise IntegrityError("directory identity changed before open")
                self.bindings.append((self.fd, name, before))
                self.fd = child
            self.recheck()
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def recheck(self):
        for fd, name, expected in reversed(self.bindings):
            _check(self.deadline)
            current = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if _identity(current) != _identity(expected):
                raise IntegrityError("concurrent directory path change")
        _check(self.deadline)

    def read(self, name=None):
        name = self.path.name if name is None else name
        _check(self.deadline)
        before = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise IntegrityError("symlink or non-regular configuration/runtime file")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     dir_fd=self.fd)
        try:
            if _file_state(before) != _file_state(os.fstat(fd)):
                raise IntegrityError("file identity changed before read")
            if before.st_size > MAX_FILE_BYTES:
                raise IntegrityError("configuration/runtime byte limit exceeded")
            chunks = []
            count = 0
            while True:
                _check(self.deadline)
                chunk = os.read(fd, min(65536, MAX_FILE_BYTES - count + 1))
                _check(self.deadline)
                if not chunk:
                    break
                count += len(chunk)
                if count > MAX_FILE_BYTES:
                    raise IntegrityError("configuration/runtime byte limit exceeded")
                chunks.append(chunk)
            if _file_state(before) != _file_state(os.fstat(fd)) or _file_state(before) != _file_state(
                os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            ):
                raise IntegrityError("concurrent file change during read")
            self.recheck()
            return b"".join(chunks), before
        finally:
            os.close(fd)

    def write_new(self, name, raw, *, mode=0o600):
        _check(self.deadline)
        self.recheck()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=self.fd)
        try:
            remaining = memoryview(raw)
            while remaining:
                _check(self.deadline)
                count = os.write(fd, remaining[:65536])
                if count <= 0:
                    raise IntegrityError("incomplete configuration/runtime write")
                remaining = remaining[count:]
            os.fchmod(fd, mode)
            os.fsync(fd)
            _check(self.deadline)
            self.recheck()
        finally:
            os.close(fd)


def _read(path, deadline):
    with _Parent(path, deadline) as parent:
        return parent.read()


def registrations(release):
    _validate_release(release)
    events = {}
    for event in ("PreToolUse", "UserPromptSubmit"):
        group = {"hooks": [{"type": "command", "command": shlex.join([
            release["python_path"], release["hook_path"], "--event", event]),
            "async": False, "timeout": 5}]}
        if event == "PreToolUse":
            group["matcher"] = "Bash"
        events[event] = [group]
    return {"hooks": events}


def _validate_release(release):
    if not isinstance(release, dict) or set(release) != {
        "python_path", "hook_path", "prepare_path", "checksums"
    }:
        raise IntegrityError("invalid release metadata fields")
    for key in ("python_path", "hook_path", "prepare_path"):
        validate_absolute_path(release[key])
    hook = Path(release["hook_path"])
    if hook.name != "hook.py" or release["prepare_path"] != str(hook.with_name("prepare.py")):
        raise IntegrityError("release helper must be adjacent to hook.py")
    digests = release["checksums"]
    if not isinstance(digests, dict) or set(digests) != set(RUNTIME_FILES) or any(
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in digests.values()
    ):
        raise IntegrityError("invalid release checksums")


def _validate_config(config):
    if not isinstance(config, dict) or not isinstance(config.get("hooks", {}), dict):
        raise IntegrityError("invalid hooks configuration object")
    for event, groups in config.get("hooks", {}).items():
        if not isinstance(event, str) or not isinstance(groups, list):
            raise IntegrityError("invalid hooks configuration event")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise IntegrityError("invalid hooks configuration group")
            if "matcher" in group and not isinstance(group["matcher"], str):
                raise IntegrityError("invalid hooks configuration matcher")
            for h in group["hooks"]:
                if not isinstance(h, dict) or not isinstance(h.get("type"), str) or not h["type"]:
                    raise IntegrityError("invalid hooks configuration definition")
                if h["type"] == "command" and (not isinstance(h.get("command"), str) or not h["command"]):
                    raise IntegrityError("invalid command hooks configuration definition")
                if "async" in h and type(h["async"]) is not bool:
                    raise IntegrityError("invalid hooks configuration async value")
                if "timeout" in h and (type(h["timeout"]) not in (int, float) or h["timeout"] <= 0):
                    raise IntegrityError("invalid hooks configuration timeout value")


def _group_identity(group):
    return {key: value for key, value in group.items() if key != "hooks"}


def _exact(left, right):
    # Python equality conflates JSON false/0 and integer/float spellings.
    return canonical_bytes(left) == canonical_bytes(right)


def _refuse_disagreement(existing, ours):
    """A registration aimed at this hook is not an unrelated guard to repair."""
    definitions = [(event, _group_identity(group), h)
                   for event, groups in ours["hooks"].items()
                   for group in groups for h in group["hooks"]]
    owned_paths = {shlex.split(h["command"])[1] for _, _, h in definitions}
    for event, groups in existing.get("hooks", {}).items():
        for group in groups:
            for h in group["hooks"]:
                command = h.get("command", "")
                if not isinstance(command, str):
                    continue
                try:
                    tokens = shlex.split(command)
                    targeted = any(path in tokens for path in owned_paths)
                except ValueError:
                    targeted = any(path in command for path in owned_paths)
                if targeted and not any(event == e and _exact(_group_identity(group), identity) and _exact(h, definition)
                                        for e, identity, definition in definitions):
                    raise IntegrityError("owned registration definition disagrees; review deployment before changing it")


def merge_hooks(existing, ours):
    _validate_config(existing)
    _validate_config(ours)
    _refuse_disagreement(existing, ours)
    result = copy.deepcopy(existing)
    events = result.setdefault("hooks", {})
    for event, groups in ours["hooks"].items():
        target = events.setdefault(event, [])
        for group in groups:
            matching = [g for g in target if _exact(_group_identity(g), _group_identity(group))]
            for h in group["hooks"]:
                if any(_exact(h, existing_hook) for g in matching for existing_hook in g["hooks"]):
                    continue
                if matching:
                    matching[0]["hooks"].append(copy.deepcopy(h))
                else:
                    new = copy.deepcopy(group)
                    new["hooks"] = [copy.deepcopy(h)]
                    target.append(new)
                    matching.append(new)
    return result


def remove_hooks(existing, ours):
    _validate_config(existing)
    _validate_config(ours)
    _refuse_disagreement(existing, ours)
    result = copy.deepcopy(existing)
    for event, groups in ours["hooks"].items():
        target = result.get("hooks", {}).get(event, [])
        for group in list(target):
            owned = [h for ours_group in groups
                     if _exact(_group_identity(ours_group), _group_identity(group))
                     for h in ours_group["hooks"]]
            previous = group["hooks"]
            group["hooks"] = [h for h in previous if not any(_exact(h, owned_hook) for owned_hook in owned)]
            if previous and not group["hooks"]:
                target.remove(group)
        if event in result.get("hooks", {}) and not target:
            # Keep an unrelated event that was already empty.
            if existing["hooks"][event]:
                del result["hooks"][event]
    return result


def _inline_check(config_path, inline_config_path, deadline):
    path = Path(inline_config_path) if inline_config_path is not None else Path(config_path).with_name("config.toml")
    try:
        raw, _ = _read(path, deadline)
    except FileNotFoundError:
        return
    try:
        config = tomllib.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise IntegrityError("invalid adjacent config.toml; review inline-hook coexistence") from exc
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict) or any(key != "state" for key in hooks):
        raise IntegrityError("inline hooks in config.toml may coexist with hooks.json; review and resolve registrations explicitly")
    _check(deadline)


def _python_identity(python_path, deadline):
    _check(deadline)
    if sys.version_info < (3, 11):
        raise IntegrityError("Python 3.11+ is required")
    # Resolve the current process's interpreter; never launch an identity probe.
    actual = str(Path(sys.executable).resolve(strict=True))
    if python_path is not None and str(python_path) != actual:
        raise IntegrityError("Python identity disagrees with the running interpreter")
    with _Parent(actual, deadline) as parent:
        before = os.stat(parent.path.name, dir_fd=parent.fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or not before.st_mode & 0o111:
            raise IntegrityError("resolved Python must be a regular executable, not a symlink")
        fd = os.open(parent.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                     dir_fd=parent.fd)
        try:
            if _identity(before) != _identity(os.fstat(fd)) or _identity(before) != _identity(
                os.stat(parent.path.name, dir_fd=parent.fd, follow_symlinks=False)
            ):
                raise IntegrityError("Python identity changed")
            parent.recheck()
        finally:
            os.close(fd)
    return actual


def _recorded_release(release_dir, deadline, *, verify_bytes):
    path = Path(release_dir)
    raw, _ = _read(path / "release-metadata.json", deadline)
    release = load_json(raw)
    _validate_release(release)
    if release["hook_path"] != str(path / "hook.py") or release["prepare_path"] != str(path / "prepare.py"):
        raise IntegrityError("recorded release paths disagree with this release directory")
    if verify_bytes:
        with _Parent(path / "release-metadata.json", deadline) as parent:
            if stat.S_IMODE(os.fstat(parent.fd).st_mode) != 0o700:
                raise IntegrityError("release directory must be private (0700)")
            names = set(os.listdir(parent.fd))
            if names - {"__pycache__"} != {*RUNTIME_FILES, "release-metadata.json"}:
                raise IntegrityError("different or incomplete release; use a new reviewed version")
            if "__pycache__" in names and not stat.S_ISDIR(
                os.stat("__pycache__", dir_fd=parent.fd, follow_symlinks=False).st_mode
            ):
                raise IntegrityError("symlink or non-directory runtime cache")
            for name in RUNTIME_FILES:
                module, _ = parent.read(name)
                if hashlib.sha256(module).hexdigest() != release["checksums"][name]:
                    raise IntegrityError("release checksum differs; use a new reviewed version")
            current, _ = parent.read()
            if current != raw:
                raise IntegrityError("concurrent release metadata change")
            parent.recheck()
    return release


def _existing_release(release_dir, release, deadline):
    try:
        with _Parent(release_dir, deadline) as parent:
            os.stat(parent.path.name, dir_fd=parent.fd, follow_symlinks=False)
            parent.recheck()
    except FileNotFoundError:
        return False
    try:
        recorded = _recorded_release(release_dir, deadline, verify_bytes=True)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise IntegrityError("different or incomplete existing release; use a new reviewed version") from exc
    if recorded != release:
        raise IntegrityError("different existing release metadata; use a new reviewed version")
    return True


def _proposal(config_path, release_dir, source_dir, python_path, inline_config_path, deadline):
    for path in (config_path, release_dir, source_dir):
        validate_absolute_path(str(path))
    _inline_check(config_path, inline_config_path, deadline)
    raw, metadata = _read(config_path, deadline)
    existing = load_json(raw)
    _validate_config(existing)
    modules = {name: _read(Path(source_dir) / name, deadline)[0] for name in RUNTIME_FILES}
    release = {"python_path": _python_identity(python_path, deadline),
               "hook_path": str(Path(release_dir) / "hook.py"),
               "prepare_path": str(Path(release_dir) / "prepare.py"),
               "checksums": {name: hashlib.sha256(data).hexdigest() for name, data in modules.items()}}
    proposed = merge_hooks(existing, registrations(release))
    exists = _existing_release(release_dir, release, deadline)
    _check(deadline)
    return raw, metadata, existing, proposed, release, modules, exists


@_filesystem_errors
def preview(config_path=CONFIG_PATH, release_dir=RELEASE_DIR, source_dir=SOURCE_DIR, *, python_path=None, inline_config_path=None):
    """Read-only proposal: synthetic paths are permitted, not execution proof."""
    deadline = time.monotonic() + CHECK_SECONDS
    _, _, existing, proposed, _, _, _ = _proposal(
        config_path, release_dir, source_dir, python_path, inline_config_path, deadline)
    before = json.dumps(existing, indent=2, ensure_ascii=False, sort_keys=True).splitlines(keepends=True)
    after = json.dumps(proposed, indent=2, ensure_ascii=False, sort_keys=True).splitlines(keepends=True)
    result = "".join(difflib.unified_diff(before, after, fromfile=str(config_path), tofile=str(config_path) + " (proposed)"))
    _check(deadline)
    return result


def _create_release(release_dir, release, modules, deadline):
    with _Parent(release_dir, deadline, create=True) as parent:
        parent.recheck()
        try:
            os.mkdir(parent.path.name, 0o700, dir_fd=parent.fd)
        except FileExistsError as exc:
            raise IntegrityError("concurrent release creation; review existing version, never overwrite") from exc
        created = os.stat(parent.path.name, dir_fd=parent.fd, follow_symlinks=False)
        os.fsync(parent.fd)
        parent.recheck()
        with _Parent(Path(release_dir) / "release-metadata.json", deadline) as target:
            if _identity(created) != _identity(os.fstat(target.fd)):
                raise IntegrityError("concurrent release directory identity change")
            # A failed copy intentionally leaves an unregistered partial version.
            os.fchmod(target.fd, 0o700)
            for name in RUNTIME_FILES:
                target.write_new(name, modules[name])
            target.write_new("release-metadata.json", canonical_bytes(release))
            os.fsync(target.fd)
            target.recheck()
        if _identity(created) != _identity(os.stat(parent.path.name, dir_fd=parent.fd, follow_symlinks=False)):
            raise IntegrityError("concurrent release directory identity change")
        parent.recheck()
    if _recorded_release(release_dir, deadline, verify_bytes=True) != release:
        raise IntegrityError("release changed before registration")


def _update_config(config_path, original, metadata, proposed, deadline):
    """Private backup + compare-before-replace, never restore an old whole file."""
    backup_name = ".hooks-backup-" + secrets.token_hex(12) + ".json"
    update_name = ".hooks-update-" + secrets.token_hex(12) + ".json"
    with _Parent(config_path, deadline) as parent:
        parent.write_new(backup_name, original)
        try:
            parent.write_new(update_name, canonical_bytes(proposed), mode=stat.S_IMODE(metadata.st_mode))
            current, current_metadata = parent.read()
            if hashlib.sha256(current).digest() != hashlib.sha256(original).digest() or _file_state(
                current_metadata
            ) != _file_state(metadata):
                raise IntegrityError("concurrent configuration change; editor changes retained")
            parent.recheck()
            # The comparison is immediately before replacement; this is not a
            # transaction against a writer racing after the final comparison.
            os.replace(update_name, parent.path.name, src_dir_fd=parent.fd, dst_dir_fd=parent.fd)
            os.fsync(parent.fd)
            parent.recheck()
        finally:
            # Only this operation's exclusive, uncommitted staging filename.
            try:
                os.unlink(update_name, dir_fd=parent.fd)
            except FileNotFoundError:
                pass
        return str(Path(config_path).with_name(backup_name))


@_filesystem_errors
def install(config_path, release_dir, source_dir, *, python_path=None, inline_config_path=None):
    """Path-parameterized offline helper; not a production activation adapter."""
    deadline = time.monotonic() + CHECK_SECONDS
    raw, metadata, existing, proposed, release, modules, exists = _proposal(
        config_path, release_dir, source_dir, python_path, inline_config_path, deadline)
    if not exists:
        _create_release(release_dir, release, modules, deadline)
    backup = None if proposed == existing else _update_config(config_path, raw, metadata, proposed, deadline)
    _check(deadline)
    return {"release": release, "backup": backup}


@_filesystem_errors
def remove(config_path, release_dir, *, inline_config_path=None):
    """Remove only exact recorded registrations, retaining release and state."""
    deadline = time.monotonic() + CHECK_SECONDS
    _inline_check(config_path, inline_config_path, deadline)
    # Rollback does not require intact runtime bytes or the current interpreter.
    release = _recorded_release(release_dir, deadline, verify_bytes=False)
    raw, metadata = _read(config_path, deadline)
    existing = load_json(raw)
    proposed = remove_hooks(existing, registrations(release))
    backup = None if proposed == existing else _update_config(config_path, raw, metadata, proposed, deadline)
    _check(deadline)
    return {"release": release, "backup": backup}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "install", "remove"))
    args = parser.parse_args(argv)
    if args.action == "install":
        print("Installation unsupported: native effective context and authenticated human prompt delivery "
              "are unestablished. No override exists; obtain reviewed native support before activation.", file=sys.stderr)
        return 1
    try:
        if args.action == "preview":
            print(preview(CONFIG_PATH, RELEASE_DIR, SOURCE_DIR), end="")
        else:
            result = remove(CONFIG_PATH, RELEASE_DIR)
            print("Removed exact recorded registrations; release and approval state retained. "
                  f"Backup: {result['backup'] or 'none (unchanged)'}")
        return 0
    except IntegrityError as exc:
        print(f"Configuration refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
