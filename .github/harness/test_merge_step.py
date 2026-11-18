"""Exercises the "Merge upstream into the sync branch" step of sync-upstream.yml.

Run from the repository root:  python .github/harness/test_merge_step.py

The step's shell body is extracted from the workflow and run against synthetic
repositories, so the merge policy is tested without GitHub.

Cases:
  1. conflict in a regenerated-looking asset file -> aborted, exit 1, nothing committed
  2. conflict in a lockfile                        -> aborted, exit 1, nothing committed
  3. conflict in source                            -> aborted, exit 1, nothing committed
  4. non-conflicting upstream change               -> merge commit, exit 0

The policy is deliberately strict: this repo has no regenerable build output, so
every conflict is a human decision and the run must push nothing.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

WORKFLOW = Path(sys.argv[1] if len(sys.argv) > 1 else ".github/workflows/sync-upstream.yml")
STEP_NAME = "Merge upstream into the sync branch"

BASE = {
    "assets/bundle.js": "base bundle\n",
    "src/app.py": "def main():\n    return 1\n",
}


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def extract_step(workflow: Path) -> str:
    spec = yaml.safe_load(io.open(workflow, encoding="utf-8"))
    for step in spec["jobs"]["sync"]["steps"]:
        if step.get("name") == STEP_NAME:
            lines = step["run"].split("\n")
            indent = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
            return "\n".join(line[indent:] if line.strip() else "" for line in lines)
    raise SystemExit(f"{STEP_NAME!r} not found in {workflow}")


def write_tree(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def build_upstream(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "harness@example.com")
    git(path, "config", "user.name", "harness")
    write_tree(path, BASE)
    git(path, "add", "-A")
    git(path, "commit", "-qm", "upstream base")
    return path


def run_case(root: Path, script: str, name: str, upstream_edit: dict[str, str],
             fork_edit: dict[str, str], expect_exit: int, expect) -> bool:
    """upstream base -> fork clone + fork commit -> upstream commit -> run the step."""
    upstream = build_upstream(root / f"{name}-upstream")

    fork = root / name
    subprocess.run(["git", "clone", "-q", str(upstream), str(fork)],
                   check=True, capture_output=True)
    git(fork, "config", "user.email", "harness@example.com")
    git(fork, "config", "user.name", "harness")
    write_tree(fork, fork_edit)
    git(fork, "add", "-A")
    git(fork, "commit", "-qm", "fork change")

    write_tree(upstream, upstream_edit)
    git(upstream, "add", "-A")
    git(upstream, "commit", "-qm", "upstream change")

    git(fork, "remote", "add", "upstream", str(upstream))
    git(fork, "fetch", "-q", "upstream", "main")

    env = dict(os.environ)
    env["GITHUB_OUTPUT"] = str(root / f"{name}.out")
    env["GITHUB_STEP_SUMMARY"] = str(root / f"{name}.summary")
    env["UPSTREAM_BRANCH"] = "main"
    env["UPSTREAM_SHA"] = git(upstream, "rev-parse", "HEAD").stdout.strip()
    env["UPSTREAM_REPO"] = str(upstream)
    (root / f"{name}.out").write_text("", encoding="utf-8")
    (root / f"{name}.summary").write_text("", encoding="utf-8")

    before = git(fork, "rev-parse", "HEAD").stdout.strip()
    script_path = root / f"{name}.sh"
    script_path.write_text(script + "\n", encoding="utf-8")
    result = subprocess.run(["bash", str(script_path)], cwd=fork, env=env,
                            capture_output=True, text=True)

    problems = []
    if result.returncode != expect_exit:
        problems.append(f"exit={result.returncode} want {expect_exit}")
    after = git(fork, "rev-parse", "HEAD").stdout.strip()
    dirty = git(fork, "status", "--porcelain").stdout.strip()
    problems += expect(fork, before, after, dirty, result, root / f"{name}.summary")

    if problems:
        print(f"FAIL  {name}")
        for problem in problems:
            print(f"      {problem}")
        print("      output:", (result.stdout + result.stderr).strip().splitlines()[-4:])
        print("      summary:", (root / f"{name}.summary").read_text(encoding="utf-8").strip())
        return False
    print(f"PASS  {name}")
    return True


def merged_cleanly(fork: Path, before: str, after: str, dirty: str, result, summary: Path):
    problems = []
    if after == before:
        problems.append("no merge commit was created")
    if dirty:
        problems.append(f"working tree left dirty: {dirty.splitlines()[:3]}")
    return problems


def aborted(fork: Path, before: str, after: str, dirty: str, result, summary: Path):
    problems = []
    if after != before:
        problems.append("HEAD moved even though the merge should have aborted")
    if dirty:
        problems.append(f"merge was not aborted cleanly: {dirty.splitlines()[:3]}")
    if (fork / ".git" / "MERGE_HEAD").exists():
        problems.append("merge state left behind (.git/MERGE_HEAD still present)")
    output = result.stdout + result.stderr
    if "Nothing was pushed" not in output:
        problems.append("the failure did not say that nothing was pushed")
    text = summary.read_text(encoding="utf-8")
    if "blocked by merge conflicts" not in text:
        problems.append("job summary does not explain the block")
    if not text.strip().endswith("`"):
        problems.append("job summary does not list the conflicting paths")
    return problems


def main() -> int:
    script = extract_step(WORKFLOW)
    root = Path(tempfile.mkdtemp(prefix="merge-harness-"))
    try:
        results = [
            # A regenerated-looking asset file is NOT special-cased any more.
            run_case(root, script, "asset-conflict",
                     {"assets/bundle.js": "upstream bundle\n"},
                     {"assets/bundle.js": "fork bundle\n"}, 1, aborted),
            run_case(root, script, "lockfile-conflict",
                     {"uv.lock": "upstream lock\n"},
                     {"uv.lock": "fork lock\n"}, 1, aborted),
            run_case(root, script, "source-conflict",
                     {"src/app.py": "def main():\n    return 99\n"},
                     {"src/app.py": BASE["src/app.py"] + "# fork\n"}, 1, aborted),
            run_case(root, script, "clean",
                     {"extra.txt": "new\n"},
                     {"src/app.py": BASE["src/app.py"] + "# fork\n"}, 0, merged_cleanly),
        ]
    finally:
        shutil.rmtree(root, ignore_errors=True)

    passed = sum(results)
    print(f"\nPASS {passed} / FAIL {len(results) - passed}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
