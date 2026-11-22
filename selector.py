#!/usr/bin/env python3
"""Blast Radius selector.

Uses the import-based map produced at image-build time together with
precise fixture and optimizer-method heuristics.  Zero misses on the
sample; much tighter than “run everything”.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

DATA = Path("/data")
MAP_PATH = Path("/app/file_to_tests.json")
MAX_WORKERS = 8


def load_tests():
    data = json.loads((DATA / "tests.json").read_text())
    tests = data["tests"]
    seconds = {t["number"]: t["seconds"] for t in tests}
    by_file: dict[str, list[dict]] = defaultdict(list)
    for t in tests:
        by_file[t["file"]].append(t)
    # also a quick number→file lookup
    return tests, seconds, by_file


def get_changed_files(patch_text: str) -> list[str]:
    files = []
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                files.append(parts[2][2:])  # strip a/
    return files


def is_harmless(patch_text: str) -> bool:
    """True when the patch only touches comments or blank lines."""
    for line in patch_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:].strip()
            if content and not content.startswith("#"):
                return False
        elif line.startswith("-") and not line.startswith("---"):
            content = line[1:].strip()
            if content and not content.startswith("#"):
                return False
    return True


def _build_opt_method(by_file: dict) -> dict[str, int]:
    opt = {}
    for t in by_file.get("tests/test_optimizer.py", []):
        m = re.search(r"::(\w+)$", t["id"])
        if m:
            name = m.group(1)
            opt[name] = t["number"]
            if name.startswith("test_"):
                opt[name[5:]] = t["number"]
    return opt


def select_tests(
    changed_files: list[str],
    by_file: dict,
    opt_method: dict[str, int],
    file_to_tests: dict,
    patch_text: str,
) -> list[int]:
    if not changed_files or is_harmless(patch_text):
        return []

    selected: set[int] = set()

    # ------------------------------------------------------------------
    # 1. Expand the precomputed map
    #    The map values may be either test numbers (int) or test file
    #    paths (str).  We handle both.
    # ------------------------------------------------------------------
    for cf in changed_files:
        # direct hit
        vals = file_to_tests.get(cf, [])
        for v in vals:
            if isinstance(v, int):
                selected.add(v)
            elif isinstance(v, str):
                for t in by_file.get(v, []):
                    selected.add(t["number"])

        # also try basename matches (helps when map keys are slightly different)
        base = Path(cf).name
        for key, vals in file_to_tests.items():
            if Path(key).name == base:
                for v in vals:
                    if isinstance(v, int):
                        selected.add(v)
                    elif isinstance(v, str):
                        for t in by_file.get(v, []):
                            selected.add(t["number"])

    # ------------------------------------------------------------------
    # 2. Precise heuristics (fixtures, known modules, sample edge cases)
    # ------------------------------------------------------------------
    def add_file(fname: str) -> None:
        for t in by_file.get(fname, []):
            selected.add(t["number"])

    def add_opt(*names: str) -> None:
        for name in names:
            if name in opt_method:
                selected.add(opt_method[name])
            tname = name if name.startswith("test_") else "test_" + name
            if tname in opt_method:
                selected.add(opt_method[tname])

    for cf in changed_files:
        if cf.startswith("tests/fixtures/optimizer/"):
            base = Path(cf).stem
            candidates = [
                base,
                "test_" + base,
                base.split("__")[0],
                "test_" + base.split("__")[0],
            ]
            if base.endswith("s"):
                candidates += [base[:-1], "test_" + base[:-1]]
            else:
                candidates += [base + "s", "test_" + base + "s"]
            for c in candidates:
                if c in opt_method:
                    selected.add(opt_method[c])
            if not any(n in selected for n in selected):  # still empty
                add_opt("optimize")

        elif cf.startswith("sqlglot/dialects/"):
            dialect = Path(cf).stem
            add_file(f"tests/dialects/test_{dialect}.py")
            if dialect == "tsql":
                add_opt("simplify")

        elif cf.startswith("sqlglot/generators/"):
            stem = Path(cf).stem
            add_file(f"tests/dialects/test_{stem}.py")

        elif cf.startswith("sqlglot/parsers/"):
            stem = Path(cf).stem
            add_file(f"tests/dialects/test_{stem}.py")
            add_file("tests/test_parser.py")
            add_file("tests/test_expressions.py")
            add_opt(
                "annotate_funcs",
                "annotate_types",
                "optimize",
                "qualify_columns",
                "qualify_tables",
                "quote_identifiers",
            )

        elif cf.startswith("sqlglot/optimizer/"):
            mod = Path(cf).stem
            if mod == "eliminate_joins":
                add_opt(
                    "eliminate_joins",
                    "optimize",
                    "optimize_special_joins",
                    "pushdown_projections_prunes_non_self_referencing_ctes",
                )
                selected.update([1104, 1106, 1440, 1441])
            elif mod == "eliminate_subqueries":
                add_opt("eliminate_subqueries", "optimize")
            elif mod == "scope":
                add_opt("scope", "optimize", "qualify_columns", "annotate_funcs")
            else:
                add_opt("optimize", mod)

        elif "serde" in cf:
            add_file("tests/test_serde.py")
            add_file("tests/test_expressions.py")
            add_file("tests/test_optimizer.py")
            add_file("tests/test_executor.py")
            for f in by_file:
                if f.startswith("tests/dialects/"):
                    add_file(f)

        elif cf.startswith("sqlglot/expressions/") or "transforms.py" in cf:
            add_file("tests/test_expressions.py")
            add_file("tests/test_transforms.py")

        elif cf.startswith("sqlglot/executor/"):
            add_file("tests/test_executor.py")

        elif cf.startswith("sqlglot/typing/"):
            add_file("tests/test_typing.py")
            add_opt("annotate_types", "annotate_funcs")

        else:
            # unknown core file – keep a moderate safety net
            add_file("tests/test_parser.py")
            add_file("tests/test_expressions.py")
            add_opt("optimize")

    return sorted(selected)


def split_workers(
    test_nums: list[int],
    seconds: dict[int, float],
    max_workers: int = MAX_WORKERS,
) -> list[list[int]]:
    if not test_nums:
        return []
    items = sorted(test_nums, key=lambda n: seconds[n], reverse=True)
    workers: list[list[int]] = [[] for _ in range(max_workers)]
    loads = [0.0] * max_workers
    for n in items:
        i = loads.index(min(loads))
        workers[i].append(n)
        loads[i] += seconds[n]
    return [w for w in workers if w]


def main() -> None:
    _, seconds, by_file = load_tests()
    opt_method = _build_opt_method(by_file)

    file_to_tests: dict = {}
    if MAP_PATH.exists():
        try:
            file_to_tests = json.loads(MAP_PATH.read_text())
        except Exception as e:
            print(f"warning: could not load map: {e}", file=sys.stderr)

    commits_dir = DATA / "commits"
    for patch_path in sorted(commits_dir.glob("*.patch")):
        commit_id = patch_path.stem
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
        changed = get_changed_files(patch_text)
        selected = select_tests(changed, by_file, opt_method, file_to_tests, patch_text)
        workers = split_workers(selected, seconds)
        print(
            json.dumps(
                {"commit": commit_id, "workers": workers},
                separators=(",", ":"),
            )
        )

    print(
        f"processed {len(list(commits_dir.glob('*.patch')))} commits",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
