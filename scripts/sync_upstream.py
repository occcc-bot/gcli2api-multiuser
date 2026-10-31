#!/usr/bin/env python3
"""Create a sanitized, linear vendor history without importing upstream parents."""

import argparse
import ast
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

SANITIZER_VERSION = "agy2api-v1"
OAUTH_ENV = {
    "ANTIGRAVITY_CLIENT_ID": "ANTIGRAVITY_OAUTH_CLIENT_ID",
    "ANTIGRAVITY_CLIENT_SECRET": "ANTIGRAVITY_OAUTH_CLIENT_SECRET",
}
# This is a narrow fail-closed check, NOT a substitute for an external scanner.
GOOGLE_OAUTH = re.compile(
    rb"(?:[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com|GOCSPX-[A-Za-z0-9_-]+)"
)
PRIVATE_KEY = re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
RUNTIME_DIRS = {
    b"creds", b"data", b"privatekeys", b"private_keys", b".ssh", b".venv",
    b"venv", b"__pycache__", b".pytest_cache", b"node_modules",
}
RUNTIME_SUFFIXES = (b".pem", b".key", b".p12", b".pfx", b".sqlite", b".sqlite3",
                    b".db", b".db-wal", b".db-shm", b".log", b".pyc")


class SyncError(Exception):
    pass


class Git:
    def __init__(self, repo, env=None):
        self.repo = repo
        self.env = dict(os.environ if env is None else env)
        # Do not inherit a caller's staging area or object-store overrides.
        for name in ("GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                     "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_NAMESPACE"):
            self.env.pop(name, None)
        self.env["GIT_NO_REPLACE_OBJECTS"] = "1"

    def run(self, *args, data=None, allow_missing=False):
        result = subprocess.run(["git", "-C", str(self.repo), *args], input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        if result.returncode:
            if allow_missing and result.returncode == 1:
                return None
            # Never echo blob contents or arbitrary git diagnostics containing tokens.
            raise SyncError(f"git {args[0]} failed (exit {result.returncode})")
        return result.stdout


def excluded(path):
    parts = path.lower().split(b"/")
    name = parts[-1]
    return (
        path.lower().startswith(b".github/workflows/")
        or (name.startswith(b"response") and name.endswith(b".txt")
            and (b"example" in name or b"exmaple" in name))
        or bool(set(parts) & RUNTIME_DIRS)
        or (name.startswith(b".env") and name != b".env.example")
        or name in {b"id_rsa", b"id_ed25519", b"id_ecdsa", b"id_dsa"}
        or name.endswith(RUNTIME_SUFFIXES)
    )


def sanitize_utils(blob):
    try:
        text = blob.decode("utf-8")
        module = ast.parse(text)
    except (UnicodeError, SyntaxError):
        raise SyncError("src/utils.py cannot be parsed; review the sanitizer") from None
    # Byte offsets allow AST's UTF-8 columns and multiline assignments to be handled exactly.
    lines = blob.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits = []
    found = set()
    needs_os_import = not any(
        isinstance(n, ast.Import) and any(a.name == "os" and a.asname in (None, "os")
                                         for a in n.names)
        for n in module.body
    )
    for node in ast.walk(module):
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        names = {t.id for t in targets if isinstance(t, ast.Name)} & OAUTH_ENV.keys()
        if not names:
            continue
        if (node not in module.body or len(targets) != 1 or len(names) != 1
                or names & found):
            raise SyncError("OAuth assignments changed shape; review the sanitizer")
        name = names.pop()
        replacement = f'{name} = os.getenv("{OAUTH_ENV[name]}", "")'
        already_safe = ast.dump(node, include_attributes=False) == ast.dump(
            ast.parse(replacement).body[0], include_attributes=False)
        if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                or already_safe):
            raise SyncError("OAuth expressions changed; review the sanitizer")
        found.add(name)
        if needs_os_import:
            replacement = "import os\n" + replacement
            needs_os_import = False
        edits.append((offsets[node.lineno - 1] + node.col_offset,
                      offsets[node.end_lineno - 1] + node.end_col_offset,
                      replacement.encode("utf-8")))
    if found != OAUTH_ENV.keys():
        raise SyncError("Expected OAuth assignments missing; review the sanitizer")
    for start, end, replacement in sorted(edits, reverse=True):
        blob = blob[:start] + replacement + blob[end:]
    return blob


def validate_vendor_history(git, commit):
    # Reject accidental use of a raw upstream branch, merges, or undocumented ancestry.
    for row in git.run("rev-list", "--parents", commit).splitlines():
        fields = row.split()
        if len(fields) > 2:
            raise SyncError("Vendor history must be linear and sanitized, not raw upstream")
        message = git.run("show", "-s", "--format=%B", fields[0].decode())
        if not (re.search(rb"^Upstream-Commit: [0-9a-f]{40}(?:[0-9a-f]{24})?$", message, re.M)
                and re.search(rb"^Vendor-Sanitizer: agy2api-v[0-9]+$", message, re.M)):
            raise SyncError("Vendor ancestry lacks sanitizer provenance; refusing raw history")


def sync(args):
    git = Git(Path(args.repo).resolve())
    git.repo = Path(os.fsdecode(git.run("rev-parse", "--show-toplevel")).strip())
    ref = "refs/heads/" + args.vendor_branch
    git.run("check-ref-format", ref)
    upstream = git.run("rev-parse", "--verify", "--end-of-options",
                       args.upstream_ref + "^{commit}").decode().strip()
    previous_bytes = git.run("rev-parse", "--verify", "--quiet", "--end-of-options",
                             ref, allow_missing=True)
    previous = previous_bytes.decode().strip() if previous_bytes else None
    if b"branch " + ref.encode() in git.run("worktree", "list", "--porcelain").splitlines():
        raise SyncError("Vendor branch is checked out in a worktree; switch away first")
    if previous:
        validate_vendor_history(git, previous)
    entries = git.run("ls-tree", "-rz", "--full-tree", upstream).split(b"\0")
    kept, removed, changed = [], 0, 0
    with tempfile.TemporaryDirectory(prefix="agy2api-vendor-") as tmp:
        git.env["GIT_INDEX_FILE"] = str(Path(tmp) / "index")
        if args.dry_run:
            # Even object writes are temporary during a dry run.
            original_objects = os.fsdecode(git.run("rev-parse", "--git-path", "objects")).strip()
            original_objects = str((git.repo / original_objects).resolve())
            objects = Path(tmp) / "objects"
            objects.mkdir()
            git.env["GIT_OBJECT_DIRECTORY"] = str(objects)
            git.env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = original_objects
        saw_utils = False
        for entry in entries:
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            if excluded(path):
                removed += 1
                continue
            if kind != b"blob":
                raise SyncError("Unsupported non-blob entry; review upstream submodules")
            blob = git.run("cat-file", "blob", oid.decode())
            if path == b"src/utils.py":
                saw_utils = True
                sanitized = sanitize_utils(blob)
                if sanitized != blob:
                    changed += 1
                    oid = git.run("hash-object", "-w", "--stdin", data=sanitized).strip()
                    blob = sanitized
            if GOOGLE_OAUTH.search(blob) or PRIVATE_KEY.search(blob):
                raise SyncError("Residual Google OAuth/private-key material detected; external review required")
            kept.append(mode + b" " + oid + b"\t" + path + b"\0")
        if not saw_utils:
            raise SyncError("src/utils.py missing; review upstream layout before syncing")
        git.run("read-tree", "--empty")
        git.run("update-index", "-z", "--index-info", data=b"".join(kept))
        tree = git.run("write-tree").decode().strip()
        unchanged = previous and tree == git.run("rev-parse", previous + "^{tree}").decode().strip()
        print(f"Upstream-Commit: {upstream}")
        print(f"Vendor-Sanitizer: {SANITIZER_VERSION}")
        print(f"Files: kept={len(kept)} excluded={removed} transformed={changed}")
        print(f"Vendor-Tree: {tree}")
        if args.dry_run:
            print("Dry-run: no refs, real index, worktree, or persistent objects changed")
            if unchanged:
                print(f"Vendor-Commit: {previous} (unchanged)")
            return
        if unchanged:
            commit = previous
            print("Snapshot unchanged; no commit created")
        else:
            message = ("vendor: sanitized agy2api snapshot\n\n"
                       f"Upstream-Commit: {upstream}\nVendor-Sanitizer: {SANITIZER_VERSION}\n"
                       "Raw upstream ancestry intentionally excluded.\n")
            parent_args = ["-p", previous] if previous else []
            commit = git.run("commit-tree", tree, *parent_args, data=message.encode()).decode().strip()
            # Compare-and-swap: never overwrite a concurrently advanced vendor branch.
            git.run("update-ref", "-m", "sanitized upstream snapshot", ref, commit,
                    previous or "0" * len(commit))
        print(f"Vendor-Commit: {commit}")
        print("Next: run an external secret scanner on the vendor history and publishable HEAD.")
        if previous:
            print(f"After review, on your integration branch: git merge --no-commit --no-ff {commit}")
        else:
            print("Initial unrelated root only (review on a clean integration branch):")
            print(f"git merge --no-commit --no-ff --allow-unrelated-histories {commit}")
        print("No merge or push performed. Never merge raw upstream/master.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repository path (default: current directory)")
    parser.add_argument("--upstream-ref", default="upstream/master")
    parser.add_argument("--vendor-branch", default="vendor/agy2api")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        sync(args)
    except (SyncError, OSError) as error:
        # OSError filenames may contain private paths; don't echo its arbitrary payload.
        print(f"error: {error if isinstance(error, SyncError) else 'local I/O failure'}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
