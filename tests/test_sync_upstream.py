"""Only temporary repositories; never fetch, push, or alter the real repository."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_upstream.py"
# Build native-shaped fixtures at runtime, never put full OAuth literals in history.
CLIENT_ID = "123456789012-" + "syntheticfixture123" + ".apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-" + "syntheticfixture123456789"


def git(repo, *args, data=None):
    return subprocess.check_output(["git", "-C", str(repo), *args], input=data).decode().strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Vendor Test")
    git(tmp_path, "config", "user.email", "vendor@example.invalid")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/utils.py").write_text(
        '"""Keep this module."""\nfrom __future__ import annotations\n'
        f'ANTIGRAVITY_CLIENT_ID = {CLIENT_ID!r}\n'
        f'ANTIGRAVITY_CLIENT_SECRET = {CLIENT_SECRET!r}\n'
        'UNCHANGED = "keep me"\n'
    )
    (tmp_path / "LICENSE").write_text("upstream attribution\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "raw upstream root")
    git(tmp_path, "branch", "upstream/master")
    return tmp_path


def run(repo, *args, ok=True):
    result = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *args],
                            capture_output=True, text=True)
    assert result.returncode == (0 if ok else 1), result.stderr
    assert CLIENT_ID not in result.stdout + result.stderr
    assert CLIENT_SECRET not in result.stdout + result.stderr
    return result


def advance(repo):
    git(repo, "add", ".")
    git(repo, "commit", "-m", "upstream next")
    git(repo, "branch", "-f", "upstream/master")


def vendor(repo):
    return git(repo, "rev-parse", "vendor/agy2api")


def test_root_snapshot_sanitization_filters_and_provenance(repo):
    for name in (".github/workflows/publish.yml", "src/api/Response_example.txt",
                 "src/api/Response_exmaple.txt", "creds/token.json", "data/credentials.db",
                 ".env", ".env.production", "privatekeys/id_rsa", "server.pem"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CLIENT_SECRET)
    (repo / ".env.example").write_text("ANTIGRAVITY_CLIENT_ID=\n")
    advance(repo)
    raw = git(repo, "rev-parse", "upstream/master")
    result = run(repo)
    commit = vendor(repo)
    assert f"Vendor-Commit: {commit}" in result.stdout
    assert git(repo, "rev-list", "--parents", commit) == commit
    assert f"Upstream-Commit: {raw}" in git(repo, "show", "-s", "--format=%B", commit)
    files = git(repo, "ls-tree", "-r", "--name-only", commit).splitlines()
    assert sorted(files) == [".env.example", "LICENSE", "src/utils.py"]
    source = git(repo, "show", f"{commit}:src/utils.py")
    assert 'ANTIGRAVITY_CLIENT_ID = os.getenv("ANTIGRAVITY_OAUTH_CLIENT_ID", "")' in source
    assert 'ANTIGRAVITY_CLIENT_SECRET = os.getenv("ANTIGRAVITY_OAUTH_CLIENT_SECRET", "")' in source
    assert 'UNCHANGED = "keep me"' in source
    compile(source, "sanitized utils", "exec")
    assert CLIENT_SECRET not in source and CLIENT_ID not in source


def test_next_snapshot_has_only_previous_vendor_parent(repo):
    run(repo)
    first = vendor(repo)
    (repo / "LICENSE").write_text("updated upstream attribution\n")
    advance(repo)
    raw = git(repo, "rev-parse", "HEAD")
    run(repo)
    second = vendor(repo)
    assert git(repo, "rev-list", "--parents", "-n", "1", second) == f"{second} {first}"
    assert raw not in git(repo, "rev-list", second).splitlines()
    assert len(git(repo, "rev-list", second).splitlines()) == 2


def test_repeat_and_excluded_only_change_do_not_commit(repo):
    run(repo)
    first = vendor(repo)
    assert "unchanged" in run(repo).stdout
    (repo / ".env").write_text("do not publish")
    advance(repo)
    run(repo)
    assert vendor(repo) == first


def test_dry_run_has_no_persistent_objects_or_ref_or_worktree_changes(repo):
    before = sorted(str(p) for p in (repo / ".git/objects").rglob("*") if p.is_file())
    index = (repo / ".git/index").read_bytes()
    result = run(repo, "--dry-run")
    after = sorted(str(p) for p in (repo / ".git/objects").rglob("*") if p.is_file())
    assert "Vendor-Tree:" in result.stdout and "Dry-run:" in result.stdout
    assert before == after and index == (repo / ".git/index").read_bytes()
    assert not git(repo, "branch", "--list", "vendor/agy2api")


def test_dirty_worktree_and_real_index_are_untouched(repo):
    (repo / "LICENSE").write_text("staged fork edit\n")
    git(repo, "add", "LICENSE")
    (repo / "LICENSE").write_text("unstaged fork edit\n")
    (repo / "untracked").write_text("local only")
    index = (repo / ".git/index").read_bytes()
    status = git(repo, "status", "--porcelain")
    run(repo)
    assert index == (repo / ".git/index").read_bytes()
    assert status == git(repo, "status", "--porcelain")
    assert (repo / "LICENSE").read_text() == "unstaged fork edit\n"


def test_unknown_oauth_literal_fails_closed(repo):
    (repo / "unexpected.txt").write_text(CLIENT_SECRET)
    advance(repo)
    assert "Residual" in run(repo, ok=False).stderr
    assert not git(repo, "branch", "--list", "vendor/agy2api")


def test_changed_assignment_shape_requires_review(repo):
    (repo / "src/utils.py").write_text("ANTIGRAVITY_CLIENT_ID = load_secret()\n")
    advance(repo)
    assert "review" in run(repo, ok=False).stderr


def test_raw_upstream_cannot_be_used_as_vendor_parent(repo):
    git(repo, "branch", "vendor/agy2api", "upstream/master")
    assert "provenance" in run(repo, ok=False).stderr


def test_checked_out_vendor_branch_is_not_advanced(repo):
    run(repo)
    first = vendor(repo)
    git(repo, "switch", "vendor/agy2api")
    assert "checked out" in run(repo, ok=False).stderr
    assert vendor(repo) == first
