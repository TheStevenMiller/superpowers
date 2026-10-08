# Execution integrity hook

Standalone Linux/Python component for a user-global Codex execution-integrity hook.
The policy, manifest checker, preparation helper, approval store, hook protocol
and reversible installer are built and offline-tested: **built, not installed/not
natively verified**. Nothing is globally registered or trusted by this build.
This is an inactive, fork-specific component: the existing Superpowers hook
registrations and plugin manifests do not register or install it. The original
implementation and its local review evidence are preserved outside this published
copy; [progress.md](progress.md) records the publication provenance and scope.
Real host coverage and prompt-based approval are unsupported pending the
feasibility gates below.

The six runtime modules, five test modules and synthetic fixture are imported
unchanged from the reviewed standalone implementation. Private planning documents,
session records and local review artifacts are not part of this repository.
The fixture's comparative-source filesystem reference is historical provenance,
not a bundled dependency or evidence of current native support. This initial
version retains fixed `/home/user` configuration, release and state paths; it is
not a portable installer for arbitrary user accounts.

## Local checking core

`policy.py` accepts only `/usr/bin/python3.11 /absolute/script [literal args...]`
and `/usr/bin/bash /absolute/script [literal args...]`, with a non-login,
non-TTY `Bash` invocation. There are no interpreter options before the script,
wrapper fallbacks, arbitrary executables or read-only command exemptions. The
script must appear in the approved file set. Quotes for spaces are supported;
shell expansion characters are rejected even inside quotes. Exact command text
and all six invocation context fields participate in the invocation key.

`manifest.py` validates schema and policy during both preparation and verification.
`validate_manifest_schema` exposes those pure checks for candidate publication
without re-reading declared files; `verify_manifest` always takes a fresh snapshot.
It hashes explicit files or recursively enumerated directories with SHA-256,
including hidden members, and compares the entire sorted file list. Preparation
returns a candidate only; neither module creates approval receipts or executes
the requested command.

Traversal starts at an open `/` descriptor. Every component is inspected without
following symlinks and opened relative to its retained parent. Device/inode/type,
file metadata and directory membership are checked again while descriptors remain
open. Inputs that are missing, unreadable, symlinked, special, overlapping, empty
or ambiguously spelled are rejected. The filesystem root, home root, hook
state/install directories, and ancestors containing those protected directories
are prohibited as snapshot roots.

Bounds are 4,096 files, 128 MiB total content and one cooperative 3-second
monotonic deadline per operation. Preparation has its own 3-second operation
budget; nested operations use the original deadline. A blocked filesystem call
can still outlast the deadline.

Run the offline verification from the repository root with:

```sh
cd hooks/execution-integrity
python3 -m unittest discover -s tests -v
python3 -m py_compile policy.py manifest.py store.py hook.py prepare.py configure.py
python3 configure.py preview
```

All filesystem mutations in these tests use temporary directories. Race tests
insert replacements at deterministic syscall boundaries; they do not launch a
protected command or exercise native hook delivery.

The offline lifecycle covers preparation, unapproved denial, exact synthetic
human-prompt approval, unchanged `{}` results, content drift with restored mtime,
fresh preparation that still cannot approve, explicit fresh-digest approval and
revocation. Two independent temporary projects and two actual linked Git
worktrees demonstrate that the same command cannot reuse approval in another
effective workdir. Added/deleted files under declared directory roots deny;
empty-directory existence between checks is not attested. Changes outside
declared roots are not attested. A passing check is never a whole-project claim.
Temporary release installation/removal also preserves real approval state and
unrelated hook definitions. The publication progress entry records the executed
test counts and compilation result; historical real read-only preview evidence
remains in the original local implementation records.

## Hook protocol and receipt state

`hook.py --event PreToolUse` and `hook.py --event UserPromptSubmit` process bounded
JSON input and return event-specific denials or an empty object preserving normal
permission checks. The production context normalizer and human-delivery check
always refuse: no native adapter is supported. Exact approval/revocation prompts
therefore block without changing receipts; ordinary prompts return `{}`. Successful
offline tests replace `normalize_invocation` and `require_supported_prompt_delivery`
only in the test process/bootstrap while exercising the real protocol, checker
and temporary store. There is no production switch for those synthetic adapters.

The preparation exemption checks the recorded interpreter and adjacent helper
against `release-metadata.json`, then freshly hashes all six release modules using
retained, no-follow descriptors. It neither executes the helper nor approves its
request. Ordinary execution checking always freshly verifies the approved files.

Receipt commit and response acknowledgment are not atomic together. In the
supported-delivery test path, validation or other pre-commit failures leave
receipts unchanged, but a sync error after atomic replacement or a deadline after
successful approval can block the response while leaving the requested receipt
active. A blocked response does not prove rollback. Before continuing, verify the
exact digest's receipt and candidate in the private store, including its `active`
state; receipts normally live at
`/home/user/.local/state/execution-integrity-hook/receipts/<digest>.json`.
Neither a receipt nor an acknowledgment grants execution permission or a retry.

## Release preview and rollback

`configure.py preview` reads the existing `/home/user/.codex/hooks.json`, adjacent
`config.toml` when present, the six development runtime modules and actual running
Python 3.11+ identity. It prints a proposed diff without creating files or changing
trust. Missing/malformed JSON, duplicate keys, symlink paths, or inline event
registrations in `config.toml` cause an actionable refusal. `hooks.state` trust
entries are not event registrations and are never changed.

The CLI accepts only `preview`, `install`, and `remove`. **Production `install`
currently refuses** because native effective context and authenticated human
prompt delivery are unsupported. There is no capability override, path override,
environment bypass, or auto-trust action. Path-parameterized Python helpers exist
to exercise the installer against temporary paths in offline tests; they do not
establish native support or authorize global activation.

The fixed proposed release directory is
`/home/user/.local/share/execution-integrity-hook/v1`. An offline installation
copies exactly `policy.py`, `manifest.py`, `store.py`, `hook.py`, `prepare.py` and
`configure.py`, plus adjacent `release-metadata.json`, into a fresh private (0700)
directory. Metadata records the resolved actual interpreter, exact adjacent
hook/helper paths, and all six SHA-256 digests. Registrations are synchronous
5-second command hooks for `PreToolUse` (matcher `Bash`) and `UserPromptSubmit`,
with shell-quoted recorded paths and explicit `--event` modes. Existing identical
releases and registrations are unchanged on repeated installation. Different or
partial release bytes require a new deliberately reviewed version; they are not
repaired or overwritten in place. An interrupted copy stays unregistered.
Cold-start preview and blocked installation suppress local bytecode creation.
An existing non-symlink `__pycache__` directory is tolerated without copying,
modifying or treating its contents as release proof; other extra release members
are refused. The six checksums pin source bytes, not cached bytecode that Python
may import in a later hook/helper process. Imported bytecode and the interpreter's
ambient import environment remain runtime trust assumptions, not covered inputs.

Config updates retain a private (0600) byte backup, preserve the original file
mode, and compare the original content digest and file identity immediately
before atomic replacement. Reads and writes retain no-follow parent descriptors
and recheck path bindings. Concurrent edits detected before replacement are
preserved. This is not a transaction against a writer racing after the final
comparison. Each operation has one cooperative 3-second monotonic deadline and
an 8-MiB per-file read bound; blocked filesystem calls can outlast that deadline.

Only after a separately requested rollback, `configure.py remove` removes exact
definitions described by the release metadata from the **current** config. It
preserves later unrelated edits, other guards, the release directory and all
approval state. It never replaces the config wholesale from an old backup and
does not need intact runtime module bytes or the current interpreter spelling.
Changed owned commands, options, matchers or timeouts require review and are not
silently removed. Malformed or missing metadata also requires manual review.
An update can be committed even if a subsequent sync/deadline check reports an
error; inspect current registrations and the retained backup before continuing.
No global install or remove command has been run for this implementation.

## Host feasibility and limits

The fixture packet at `tests/fixtures/codex-events.json` distinguishes live
official documentation, installed-package/comparative-source evidence, and
authored synthetic examples. There are no native captures. The local CLI reports
0.161.0, but its matching hook source/schema was unavailable. Comparative 0.160.0
source omits effective workdir/shell/login/TTY from the `exec_command` hook payload
and lacks authenticated human prompt origin. Those comparative findings are
not claims about the installed CLI or this hosted session.

The official documentation establishes the `Bash` matcher, command mapping and
event-specific response shapes. It does not establish the missing effective
context or human-origin semantics here. Do not substitute session `cwd` for
effective workdir, infer context defaults, or treat synthetic fields as native
facts. Both proposed interpreter paths were observed as regular executable,
non-symlink files; their bytes remain a runtime trust assumption unless pinned.

This is a declared-file drift guard. It does not discover undeclared dependencies,
pin the ambient runtime/environment/network, authenticate a human against a
same-user agent, make a sequential snapshot atomic, or remove the check/use race.
It does not cover input to already-running processes or guarantee denial if the
host skips a hook, fails open or rewrites an invocation after checking it.

## Later activation gate

The next activation action requires a separately named install-and-canary request.
That request alone cannot enable this release: genuine host evidence and reviewed
changes to both the native adapter and the production installer gate are required.
No bypass, launcher, host patch, terminal-approval workaround or weaker receipt
policy is part of this delivery.

- Confirm intended hosts, exact interpreter/script forms and acceptance of the
  restrictive shell-inspection policy. Keep coverage cross-project, without a
  workdir-prefix exemption. Verify actual effective command/workdir/shell/login/TTY
  fields on fresh, resumed and child sessions, another project/workdir, and nested
  code-mode `exec_command`; synthetic fields or session `cwd` are not evidence.
- Establish explicit human prompt provenance, automated/child prompt behavior and
  replay on resume. Session, turn and parent/agent IDs do not authenticate a human.
  Unknown or unsafe delivery keeps prompt approval unsupported.
- Review the global config diff, active feature/managed policy, and every other
  matching hook for command/context rewriting. A rewrite after checking invalidates
  the checked identity unless the host establishes what is actually executed.
- Only after reviewed adapter/installer changes and the named request, install the
  reviewed release, have the human review/trust the exact definitions using `/hooks`,
  and verify required restarts and release/interpreter availability.
- Use a harmless disposable marker canary: unchanged approved input permits the
  marker; changed input is denied before the marker exists. Repeat across the
  supported contexts above. No project trial or paid API is an activation canary.
- In isolated host configuration, test missing, untrusted, crashing, timed-out and
  malformed-output hooks. Record any fail-open result; do not claim unconditional
  enforcement. Interactive continuation and already-running shells are outside
  coverage and are not retroactively protected.
- Leave unsupported paths explicitly unverified. If rollback is separately
  requested, remove only this hook's exact recorded registrations; retain unrelated
  hooks, release files and approval state.

A registration file, successful preview or passing offline test establishes none
of these native gates. Independent review remains distinct from native evidence.
