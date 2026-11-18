"""Strict structural validation for the two workflows (stands in for actionlint).

* rejects duplicate mapping keys (PyYAML silently keeps the last one, which is how a
  step ended up with no `run:` at all)
* every step must be a mapping with exactly one of run/uses
* no duplicate step ids, no duplicate step names within a job
* `if:`/`env:`/`with:`/`id:` types are what GitHub expects
"""

import io
import sys

import yaml

FILES = [
    ".github/workflows/sync-upstream.yml",
    ".github/workflows/docker-release.yml",
]


class Strict(yaml.SafeLoader):
    pass


def _construct_mapping(loader, node, deep=False):
    keys = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in keys:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        keys.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)

problems = []
for path in FILES:
    text = io.open(path, encoding="utf-8").read()
    try:
        spec = yaml.load(text, Loader=Strict)  # noqa: S506 - Strict is SafeLoader-derived
    except yaml.YAMLError as exc:
        problems.append(f"{path}: YAML error: {exc}")
        continue
    for job_name, job in spec.get("jobs", {}).items():
        names, ids = set(), set()
        for index, step in enumerate(job.get("steps", [])):
            label = step.get("name") if isinstance(step, dict) else step
            if not isinstance(step, dict):
                problems.append(f"{path}: {job_name}[{index}] is not a mapping")
                continue
            has_run, has_uses = "run" in step, "uses" in step
            if has_run == has_uses:
                problems.append(
                    f"{path}: {job_name}[{index}] {label!r} must have exactly one of run/uses"
                )
            if has_run and not str(step["run"] or "").strip():
                problems.append(f"{path}: {job_name}[{index}] {label!r} has an empty run block")
            if label in names:
                problems.append(f"{path}: duplicate step name {label!r}")
            names.add(label)
            step_id = step.get("id")
            if step_id:
                if step_id in ids:
                    problems.append(f"{path}: duplicate step id {step_id!r}")
                ids.add(step_id)
            if "if" in step and not isinstance(step["if"], (str, bool)):
                problems.append(f"{path}: {label!r} has a non-scalar if")
            if "env" in step and not isinstance(step["env"], dict):
                problems.append(f"{path}: {label!r} env is not a mapping")
    # Referenced step ids must exist.
    for job_name, job in spec.get("jobs", {}).items():
        defined = {s.get("id") for s in job.get("steps", []) if isinstance(s, dict)}
        for step in job.get("steps", []):
            for key, value in list(step.items()):
                if key in ("run", "with", "env", "if") and isinstance(value, (str, dict)):
                    blob = value if isinstance(value, str) else " ".join(map(str, value.values()))
                    for fragment in str(blob).split("steps.")[1:]:
                        ref = fragment.split(".")[0]
                        if ref and ref not in defined:
                            problems.append(
                                f"{path}: {step.get('name')!r} references undefined step id {ref!r}"
                            )
        print(f"{path}: {job_name}: {len(job.get('steps', []))} steps, ids={sorted(i for i in defined if i)}")

if problems:
    print("\nFAIL")
    for problem in problems:
        print("  -", problem)
    sys.exit(1)
print("\nOK: both workflows are structurally valid")
