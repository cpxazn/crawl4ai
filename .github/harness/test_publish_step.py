"""Exercises the publication steps of the sync and docker-release workflows.

Publication is asynchronous and a dry run never enters it, so the step bodies are
extracted from the workflows and run against fake `gh`, `curl`, `docker` and `sleep`
binaries driven by a JSON state file.

Covered here:
  * sync "Publish and verify the Docker image for this tag"
      - an earlier completed failure with the same run name is never adopted
      - a dispatch that only becomes visible after several polls is still adopted
      - an already running publication is adopted without dispatching a second one
      - publisher failure, revision-label mismatch and digest mismatch all fail
      - an image that already exists is verified rather than silently accepted
        (retry, or a publication that completed while this run was validating)
      - verification-failure-then-retry: the retry re-verifies the existing image
  * docker-release "Recheck publication state before building"
      - sync tag present -> skip, absent -> build, registry error -> fail
      - the skip is restricted to sync tags so docker-rebuild-v* still rebuilds
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

SYNC_WORKFLOW = Path(sys.argv[1] if len(sys.argv) > 1 else "sync-upstream.v2.yml")
RELEASE_WORKFLOW = Path(sys.argv[2] if len(sys.argv) > 2 else SYNC_WORKFLOW)

PUBLISH_STEP = "Publish and verify the Docker image for this tag"
RECHECK_STEP = "Recheck publication state before building"
REPORT_STEP = "Report what this run published"

TAG = "sync-abcdef1234567890"
SHA = "abcdef1234567890abcdef1234567890abcdef12"
DIGEST = "sha256:" + "a" * 64

FAKE_GH = r'''
import json, os, re, sys
state_path = os.environ["STATE_FILE"]
state = json.load(open(state_path))

def save():
    json.dump(state, open(state_path, "w"))

args = sys.argv[1:]
jq = ""
for i, a in enumerate(args):
    if a == "--jq":
        jq = args[i + 1]
    elif a.startswith("--jq="):
        jq = a.split("=", 1)[1]

runs = state["runs"]

def out(value):
    print(value)
    sys.exit(0)

if args[:2] == ["run", "list"]:
    runs.sort(key=lambda r: r["databaseId"], reverse=True)  # gh lists newest first
    if jq in (".[].databaseId",):
        out("\n".join(str(r["databaseId"]) for r in runs))
    if state.get("pending_dispatch"):
        state["polls"] = state.get("polls", 0) + 1
        if state["polls"] >= state["visibility_delay"]:
            runs.append(state["pending_dispatch"])
            state["pending_dispatch"] = None
        save()
    m = re.search(r'select\(\.displayTitle == "docker ([^"]+)"( and \.status != "completed")?\)', jq)
    if not m:
        print("HARNESS: unsupported run list jq:", jq, file=sys.stderr)
        sys.exit(2)
    only_unfinished = bool(m.group(2))
    candidates = [r for r in runs if r["displayTitle"] == "docker " + m.group(1)]
    if only_unfinished:
        candidates = [r for r in candidates if r["status"] != "completed"]
    out(str(candidates[0]["databaseId"]) if candidates else "")

if args[:2] == ["run", "view"]:
    run_id = int(args[2])
    run = next((r for r in runs if r["databaseId"] == run_id), None)
    if run is None:
        print("HARNESS: unknown run id", run_id, file=sys.stderr)
        sys.exit(2)
    if "--log" in args:
        if run.get("final_status") != "completed":
            sys.exit(1)
        for line in run.get("log_lines", []):
            print(f"sync\tReport what this run published\t{line}")
        if not run.get("log_lines"):
            print(f"sync\tReport what this run published\tpublished-revision={run.get('publisher_revision', '')}")
            print(f"sync\tReport what this run published\tpublished-digest={run.get('publisher_digest', '')}")
        sys.exit(0)
    if jq == ".headSha":
        out(run["headSha"])
    state.setdefault("views", {})
    key = str(run_id)
    state["views"][key] = state["views"].get(key, 0) + 1
    if state["views"][key] <= state.get("queued_polls", 1):
        save()
        out(f"{run['status']} {run.get('conclusion', '')}".strip())
    save()
    out(f"{run['final_status']} {run['final_conclusion']}")

if args[:2] == ["workflow", "run"]:
    state["dispatches"] = state.get("dispatches", 0) + 1
    flags = [args[i + 1] for i, a in enumerate(args) if a == "-f"]
    tag = flags[0].split("=", 1)[1] if flags else ""
    ref = flags[1].split("=", 1)[1] if len(flags) > 1 else ""
    run = {
        "databaseId": 900 + state["dispatches"],
        "displayTitle": f"docker {tag}",
        "status": "queued",
        "final_status": state.get("publisher_status", "completed"),
        "final_conclusion": state.get("publisher_conclusion", "success"),
        "headSha": state.get("publisher_head_sha", ref),
        "publisher_revision": state.get("publisher_revision", ref),
        "publisher_digest": state.get("publisher_digest", state["registry_digest"]),
    }
    if state.get("visibility_delay", 0) > 0:
        state["pending_dispatch"] = run
        state["polls"] = 0
    else:
        runs.append(run)
    save()
    print("dispatched")
    sys.exit(0)

print("HARNESS: unsupported gh invocation:", args, file=sys.stderr)
sys.exit(2)
'''

FAKE_CURL = r'''
import json, os, sys
args = sys.argv[1:]
state = json.load(open(os.environ["STATE_FILE"]))
code = int(state.get("registry_code", 404))
url = args[-1]
if "-w" in args and "%{http_code}" in " ".join(args):
    print(code)
    sys.exit(0)
if "/tags/" in url:
    if code == 200:
        print(json.dumps({"digest": state["registry_digest"]}))
        sys.exit(0)
    sys.exit(22)
sys.exit(0)
'''

FAKE_DOCKER = r'''
import json, os, sys
args = sys.argv[1:]
state = json.load(open(os.environ["STATE_FILE"]))
if args[:1] == ["pull"]:
    print("pulled")
    sys.exit(0)
if args[:1] == ["inspect"]:
    fmt = args[args.index("--format") + 1]
    if "org.opencontainers.image.revision" in fmt:
        print(state["image_label"])
    elif "RepoDigests" in fmt:
        print(f"cpxazn/crawl4ai@{state['image_digest']}")
    sys.exit(0)
print("HARNESS: unsupported docker invocation:", args, file=sys.stderr)
sys.exit(2)
'''

FAKE_SLEEP = "#!/usr/bin/env bash\nexit 0\n"


def extract_step(workflow: Path, name: str) -> tuple[str, str]:
    spec = yaml.safe_load(io.open(workflow, encoding="utf-8"))
    for job in spec["jobs"].values():
        for step in job["steps"]:
            if step.get("name") == name:
                lines = step["run"].split("\n")
                indent = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
                body = "\n".join(line[indent:] if line.strip() else "" for line in lines)
                return body, step.get("if", "")
    raise SystemExit(f"{name!r} not found in {workflow}")


def write_fakes(bin_dir: Path) -> None:
    for name, body in (("gh", FAKE_GH), ("curl", FAKE_CURL), ("docker", FAKE_DOCKER)):
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        path.chmod(0o755)
    sleep = bin_dir / "sleep"
    sleep.write_text(FAKE_SLEEP, encoding="utf-8")
    sleep.chmod(0o755)


def run_script(root: Path, name: str, script: str, state: dict, env_extra: dict, expect_exit: int):
    case_dir = root / name
    case_dir.mkdir()
    bin_dir = case_dir / "bin"
    bin_dir.mkdir()
    write_fakes(bin_dir)

    state_file = case_dir / "state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    script_path = case_dir / "step.sh"
    script_path.write_text(script + "\n", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["STATE_FILE"] = str(state_file)
    env.update(env_extra)

    result = subprocess.run(["bash", str(script_path)], env=env, capture_output=True, text=True)
    final = json.loads(state_file.read_text(encoding="utf-8"))
    return result, final


def publish_script(workflow: Path, image_exists: bool) -> str:
    body, _ = extract_step(workflow, PUBLISH_STEP)
    return (
        body.replace("${{ secrets.PAT_TOKEN || github.token }}", "")
        .replace("${{ secrets.PAT_TOKEN }}", "")
        .replace("${{ steps.detect.outputs.image_exists }}", "true" if image_exists else "false")
    )


def run_publish_case(root, workflow, name, state, image_exists, expect_exit, checks) -> bool:
    script = publish_script(workflow, image_exists)
    env = {
        "TAG": TAG,
        "SHA": SHA,
        "IMAGE_NAME": "cpxazn/crawl4ai",
        "SYNC_BRANCH": "main",
        "GITHUB_REPOSITORY": "cpxazn/crawl4ai",
    }
    result, final = run_script(root, name, script, state, env, expect_exit)
    output = result.stdout + result.stderr
    problems = []
    if result.returncode != expect_exit:
        problems.append(f"exit={result.returncode} want {expect_exit}")
    problems += checks(output, final)
    if problems:
        print(f"FAIL  {name}")
        for problem in problems:
            print(f"      {problem}")
        print("      tail:", output.strip().splitlines()[-12:])
        return False
    print(f"PASS  {name}")
    return True


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="publish-harness-"))
    old_failed = {
        "databaseId": 111,
        "displayTitle": f"docker {TAG}",
        "status": "completed",
        "final_status": "completed",
        "final_conclusion": "failure",
        "headSha": SHA,
        "publisher_revision": "0" * 40,
        "publisher_digest": DIGEST,
    }
    base = {
        "runs": [],
        "image_label": SHA,
        "image_digest": DIGEST,
        "registry_digest": DIGEST,
        "registry_code": 200,
    }
    results = []
    try:
        results.append(run_publish_case(
            root, SYNC_WORKFLOW, "earlier-failed-run", {**base, "runs": [old_failed]}, False, 0,
            lambda out, final: (
                [] if "dispatched publication run 901" in out and "adopting in-flight publication run 111" not in out
                and final.get("dispatches") == 1
                else ["expected a fresh dispatch of 901, not adoption of 111"]
            )))

        results.append(run_publish_case(
            root, SYNC_WORKFLOW, "delayed-visibility", {**base, "visibility_delay": 4}, False, 0,
            lambda out, final: (
                [] if "dispatched publication run 901" in out and final.get("dispatches") == 1
                else ["a dispatch that became visible late was not adopted"]
            )))

        results.append(run_publish_case(
            root, SYNC_WORKFLOW, "in-flight-adopted",
            {**base, "runs": [{**old_failed, "databaseId": 555, "status": "in_progress",
                               "final_status": "completed", "final_conclusion": "success",
                               "publisher_revision": SHA}]}, False, 0,
            lambda out, final: (
                [] if "adopting in-flight publication run 555" in out and (final.get("dispatches") or 0) == 0
                else ["expected adoption of 555 without a second dispatch"]
            )))

        results.append(run_publish_case(
            root, SYNC_WORKFLOW, "publisher-fails", {**base, "publisher_conclusion": "failure"}, False, 1,
            lambda out, final: [] if "finished as" in out else ["publisher failure not reported"]))

        results.append(run_publish_case(
            root, SYNC_WORKFLOW, "label-mismatch", {**base, "image_label": "deadbeef"}, False, 1,
            lambda out, final: [] if "labelled deadbeef" in out else ["label mismatch not reported"]))

        results.append(run_publish_case(
            root, SYNC_WORKFLOW, "digest-mismatch", {**base, "image_digest": "sha256:" + "b" * 64}, False, 1,
            lambda out, final: [] if "does not match the pulled image" in out else ["digest mismatch not reported"]))

        results.append(run_publish_case(
            root, SYNC_WORKFLOW, "publisher-digest-mismatch",
            {**base, "publisher_digest": "sha256:" + "c" * 64}, False, 1,
            lambda out, final: [] if "registry has" in out else ["publisher digest mismatch not reported"]))

        results.append(run_publish_case(
            root, SYNC_WORKFLOW, "already-published-verified", base, True, 0,
            lambda out, final: (
                [] if "verifying instead of pushing" in out and (final.get("dispatches") or 0) == 0
                and f"digest {DIGEST}" in out
                else ["an existing image was not verified"]
            )))

        results.append(run_publish_case(
            root, SYNC_WORKFLOW, "publisher-reused",
            {**base, "publisher_action": "reused"}, False, 0,
            lambda out, final: (
                [] if f"digest {DIGEST}" in out else ["a reused publication was not accepted"]
            )))

        # End-to-end chain: run the reporter step for a skipped build, feed its actual
        # output to the consumer, and require the consumer to accept it.
        reporter_body, _ = extract_step(RELEASE_WORKFLOW, REPORT_STEP)
        chain_dir = root / "chain-reporter"
        chain_dir.mkdir()
        chain_bin = chain_dir / "bin"
        chain_bin.mkdir()
        write_fakes(chain_bin)
        chain_state = chain_dir / "state.json"
        chain_state.write_text(json.dumps({"registry_code": 200, "registry_digest": DIGEST,
                                          "image_label": SHA, "image_digest": DIGEST, "runs": []}),
                              encoding="utf-8")
        chain_script = chain_dir / "report.sh"
        chain_script.write_text(reporter_body + "\n", encoding="utf-8")
        chain_env = dict(os.environ)
        chain_env["PATH"] = f"{chain_bin}{os.pathsep}{chain_env['PATH']}"
        chain_env["STATE_FILE"] = str(chain_state)
        chain_env["IMAGE_NAME"] = "cpxazn/crawl4ai"
        chain_env["VERSION"] = TAG
        chain_env["REV"] = SHA
        chain_env["DIGEST"] = ""
        chain_env["SKIPPED"] = "true"
        reporter_run = subprocess.run(["bash", str(chain_script)], env=chain_env, capture_output=True, text=True)
        reporter_lines = [line for line in reporter_run.stdout.splitlines()
                          if line.startswith(("published-action=", "published-revision=",
                                              "published-digest=", "requested-revision="))]
        results.append(run_publish_case(
            root, SYNC_WORKFLOW, "reporter-to-consumer",
            {**base,
             "runs": [{**old_failed, "databaseId": 777, "status": "in_progress",
                       "final_status": "completed", "final_conclusion": "success",
                       "log_lines": reporter_lines}]}, False, 0,
            lambda out, final: (
                [] if reporter_run.returncode == 0
                and "adopting in-flight publication run 777" in out
                and f"digest {DIGEST}" in out
                and any(line.startswith("published-action=reused") for line in reporter_lines)
                else ["the reporter's own output did not satisfy the consumer"] + reporter_lines
            )))

        # Verification-failure-then-retry: the first attempt fails on the label, and the
        # retry must verify the (now existing) image instead of accepting it.
        failed = run_publish_case(
            root, SYNC_WORKFLOW, "retry-phase-1-label-mismatch",
            {**base, "image_label": "deadbeef"}, False, 1,
            lambda out, final: [] if "labelled deadbeef" in out else ["phase 1 did not fail on the label"])
        results.append(failed)
        results.append(run_publish_case(
            root, SYNC_WORKFLOW, "retry-phase-2-verified", base, True, 0,
            lambda out, final: (
                [] if "verifying instead of pushing" in out and (final.get("dispatches") or 0) == 0
                else ["the retry did not re-verify the existing image"]
            )))

        # Publisher-side reuse: a skipped build must report the published digest, not "none".
        report_body, _ = extract_step(RELEASE_WORKFLOW, REPORT_STEP)
        for name, code, expect_exit, expect_substr in (
            ("reporter-reused", 200, 0, "published-action=reused"),
            ("reporter-reused-unpublished", 404, 1, "is not published"),
        ):
            case_dir = root / name
            case_dir.mkdir()
            bin_dir = case_dir / "bin"
            bin_dir.mkdir()
            write_fakes(bin_dir)
            state_file = case_dir / "state.json"
            state_file.write_text(
                json.dumps({"registry_code": code, "registry_digest": DIGEST,
                            "image_label": SHA, "image_digest": DIGEST, "runs": []}),
                encoding="utf-8",
            )
            script_path = case_dir / "step.sh"
            script_path.write_text(report_body + "\n", encoding="utf-8")
            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            env["STATE_FILE"] = str(state_file)
            env["IMAGE_NAME"] = "cpxazn/crawl4ai"
            env["VERSION"] = TAG
            env["REV"] = SHA
            env["DIGEST"] = ""
            env["SKIPPED"] = "true"
            result = subprocess.run(["bash", str(script_path)], env=env, capture_output=True, text=True)
            output = result.stdout + result.stderr
            problems = []
            if result.returncode != expect_exit:
                problems.append(f"exit={result.returncode} want {expect_exit}")
            if expect_substr not in output:
                problems.append(f"missing {expect_substr!r} in output")
            if expect_exit == 0 and f"published-digest={DIGEST}" not in output:
                problems.append("the reused publication did not report the published digest")
            if problems:
                print(f"FAIL  {name}")
                for problem in problems:
                    print(f"      {problem}")
                print("      tail:", output.strip().splitlines()[-8:])
                results.append(False)
            else:
                print(f"PASS  {name}")
                results.append(True)

        # docker-release recheck step
        recheck, recheck_if = extract_step(RELEASE_WORKFLOW, RECHECK_STEP)
        if "startsWith(steps.version.outputs.version, 'sync-')" not in recheck_if:
            print("FAIL  recheck-scope")
            print("      the dedupe recheck is not restricted to sync tags:", recheck_if)
            results.append(False)
        else:
            print("PASS  recheck-scope")
            results.append(True)

        for name, code, expect_skip, expect_exit in (
            ("recheck-present", 200, "true", 0),
            ("recheck-absent", 404, "false", 0),
            ("recheck-registry-error", 500, None, 1),
        ):
            case_dir = root / name
            case_dir.mkdir()
            bin_dir = case_dir / "bin"
            bin_dir.mkdir()
            write_fakes(bin_dir)
            state_file = case_dir / "state.json"
            state_file.write_text(json.dumps({"registry_code": code, "registry_digest": DIGEST,
                                              "image_label": SHA, "image_digest": DIGEST, "runs": []}),
                                  encoding="utf-8")
            out_file = case_dir / "out.txt"
            out_file.write_text("", encoding="utf-8")
            script_path = case_dir / "step.sh"
            script_path.write_text(recheck + "\n", encoding="utf-8")
            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            env["STATE_FILE"] = str(state_file)
            env["GITHUB_OUTPUT"] = str(out_file)
            env["IMAGE_NAME"] = "cpxazn/crawl4ai"
            env["VERSION"] = TAG
            result = subprocess.run(["bash", str(script_path)], env=env, capture_output=True, text=True)
            problems = []
            if result.returncode != expect_exit:
                problems.append(f"exit={result.returncode} want {expect_exit}")
            if expect_skip is not None:
                written = out_file.read_text(encoding="utf-8")
                if f"skip={expect_skip}" not in written:
                    problems.append(f"GITHUB_OUTPUT did not contain skip={expect_skip}: {written!r}")
            if problems:
                print(f"FAIL  {name}")
                for problem in problems:
                    print(f"      {problem}")
                results.append(False)
            else:
                print(f"PASS  {name}")
                results.append(True)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    passed = sum(results)
    print(f"\nPASS {passed} / FAIL {len(results) - passed}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
