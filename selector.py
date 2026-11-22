#!/usr/bin/env python3
"""Select and schedule SQLGlot tests from a trace of the pinned revision."""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
import gzip
import json
from pathlib import Path
import resource
import time

import base64
import lzma

from build_map import _COVERAGE, PythonImpact, class_config_edits, comparison_substitutions, PatchError, parse_patch, check_patch


HERE = Path(__file__).resolve().parent
CONFIG = {
    "pyproject.toml", "setup.py", "setup.cfg", "pytest.ini",
    "tox.ini", "requirements.txt", "conftest.py",
}
RUNTIME_TREES = ("sqlglot/", "tests/", "sqlglotc/", "sqlglot-integration-tests/")


def split_workers(test_nums, seconds):
    """Compare longest-first schedules at each legal worker count."""
    durations = {n: int(seconds[n] * 1000 + 0.5) for n in test_nums}
    tests = sorted(test_nums, key=lambda n: (-durations[n], n))
    if not tests:
        return [], 0.0
    best_cost = float("inf")
    best = []
    for size in range(1, min(8, len(tests)) + 1):
        workers = [[] for _ in range(size)]
        loads = [0] * size
        for test in tests:
            worker = min(range(size), key=loads.__getitem__)
            workers[worker].append(test)
            loads[worker] += durations[test]
        cost = 2000 * size + max(loads)
        if cost < best_cost:
            best, best_cost = workers, cost
    return best, best_cost / 1000


class Selector:
    def __init__(self, base, trace, tests):
        self.base = base
        self.numbers = {test["number"] for test in tests}
        self.seconds = {test["number"]: float(test["seconds"]) for test in tests}
        self.by_file = defaultdict(set)
        self.nodeids = {}
        self.numbers_by_node = defaultdict(set)
        aliases = defaultdict(list)
        for node in trace["nodes"]:
            file, _, name = node.rpartition("::")
            aliases[(file.split("::", 1)[0], name)].append(node)
        for test in tests:
            number = test["number"]
            file = test["file"].removeprefix("./")
            self.by_file[file].add(number)
            node = test.get("id") or test.get("nodeid") or test.get("name")
            if isinstance(node, str):
                node = node.removeprefix("./")
                own_module = file.removesuffix(".py").replace("/", ".") + "."
                if node.startswith(own_module):
                    node = f"{file}::" + node[len(own_module):].replace(".", "::")
                if node.startswith("::"):
                    node = file + node
                elif "::" not in node and not node.endswith(".py"):
                    candidates = aliases[(file, node)]
                    node = candidates[0] if len(candidates) == 1 else f"{file}::{node}"
                elif "::" in node:
                    prefix, suffix = node.split("::", 1)
                    node = f"{file}::{suffix if prefix.endswith('.py') else node}"
                self.nodeids[number] = node
                self.numbers_by_node[node].add(number)

        matched = set(trace["nodes"]) & self.numbers_by_node.keys()
        self.unmatched = self.numbers - {
            n for node in matched for n in self.numbers_by_node[node]
        }
        self.profiles = [
            {
                n
                for index in profile
                for n in self.numbers_by_node[trace["nodes"][index]]
            }
            for profile in trace["profiles"]
        ]
        self.measured = {
            path: {int(line): profile for line, profile in lines.items()}
            for path, lines in trace["lines"].items()
        }
        self.collection = {
            path: set(lines) for path, lines in trace.get("collection", {}).items()
        }
        self.attribute_reads = trace.get("attributes", {})
        self.safe_comparisons = trace.get("safe_comparisons", {})
        config = trace.get("config_reads", {})
        self.config_reads = config.get("sites", {})
        self.config_unverified = self.numbers.copy()
        self.config_groups = defaultdict(set)
        for node in config.get("verified", ()):
            numbers = self.numbers_by_node.get(node, ())
            self.config_unverified.difference_update(numbers)
            self.config_groups[node.rpartition("::")[0] + "::*"].update(numbers)
        self.readers = {
            path: self.profiles[profile] for path, profile in trace["reads"].items()
        }
        self.estimate = dict(zip(trace["nodes"], trace.get("seconds", ())))
        self.unverified = set(trace["unverified"])
        self.file_hits = {}

    def whole_file(self, path):
        if path not in self.file_hits:
            hits = set(self.by_file.get(path, ()))
            for profile in set(self.measured.get(path, {}).values()):
                hits.update(self.profiles[profile])
            if path.startswith(("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/")):
                name = Path(path).stem
                hits.update(self.by_file.get(f"tests/dialects/test_{name}.py", ()))
                dialect_path = f"sqlglot/dialects/{name}.py"
                for profile in set(self.measured.get(dialect_path, {}).values()):
                    hits.update(self.profiles[profile])
            self.file_hits[path] = hits
        return self.file_hits[path]

    def analyze(self, text):
        selected = set()
        overlay = {}
        affects_runtime = False
        check_imports = False
        changes = parse_patch(text)
        for change in changes:
            paths = {path for path in (change.old, change.new) if path is not None}
            for path in paths:
                selected.update(self.readers.get(path, ()))
                if change.old != change.new:
                    selected.update(self.readers.get(path.rpartition("/")[0] + "/", ()))
            if any(path in CONFIG or path.endswith("/conftest.py") for path in paths):
                return self.numbers.copy(), {}, False
            if not any(path.startswith(RUNTIME_TREES) for path in paths):
                continue

            prior = (self.base / change.old).read_bytes().decode("utf-8") if change.old else ""
            source = change.apply(prior)
            if change.old and change.old != change.new:
                affects_runtime = True
                overlay[change.old] = None
                selected.update(self.whole_file(change.old))
                check_imports |= change.old.endswith(".py")
            if change.new is None:
                continue
            overlay[change.new] = source
            if not change.new.endswith(".py"):
                affects_runtime |= bool(selected) or change.new.startswith("sqlglot/")
                continue

            try:
                old_tree = compile(prior.encode("utf-8"), change.new, "exec", ast.PyCF_ONLY_AST)
                new_tree = compile(source.encode("utf-8"), change.new, "exec", ast.PyCF_ONLY_AST)
                compile(new_tree, change.new, "exec")
            except (SyntaxError, ValueError):
                affects_runtime = True
                check_imports = True
                selected.update(self.whole_file(change.new))
                continue

            if self.config_reads and len(changes) == 1 and change.old == change.new:
                edits = class_config_edits(change.old, old_tree, new_tree)
                if edits and all(site in self.config_reads for site, _ in edits):
                    contexts = {
                        node for site, keys in edits for key in [*keys, "*"]
                        for node in self.config_reads[site].get(key, ())
                    }
                    if ":import:" not in contexts:
                        selected.update(self.config_unverified)
                        for node in contexts:
                            selected.update(self.numbers_by_node.get(node, ()))
                            selected.update(self.config_groups.get(node, ()))
                        affects_runtime = True
                        continue

            comparison_data = self.safe_comparisons.get(change.old)
            sites = comparison_substitutions(old_tree, new_tree) if comparison_data else None
            if (
                sites
                and len(changes) == 1
                and change.old == change.new
                and all(key in comparison_data for key, _, _ in sites)
            ):
                for key, first, last in sites:
                    safe = {
                        number for node in comparison_data[key]
                        for number in self.numbers_by_node.get(node, ())
                        if node not in self.unverified
                    }
                    for line in range(first, last + 1):
                        profile = self.measured.get(change.old, {}).get(line, 0)
                        selected.update(self.profiles[profile] - safe)
                        check_imports |= line in self.collection.get(change.old, ())
                affects_runtime = True
                continue

            impact = PythonImpact(
                self.measured.get(change.old, {}), self.collection.get(change.old, ())
            )
            impact.compare(old_tree, new_tree)
            affects_runtime |= impact.changed
            if impact.whole_file or (change.old != change.new and impact.changed):
                selected.update(self.whole_file(change.new))
            for profile in impact.profiles:
                selected.update(self.profiles[profile])
            for name in impact.class_attributes:
                for path, lines in self.attribute_reads.get(name, {}).items():
                    covered = self.measured.get(path, {})
                    for line in lines:
                        selected.update(self.profiles[covered.get(line, 0)])
            check_imports |= impact.import_check

        if affects_runtime or selected:
            selected.update(self.unmatched)
        return selected, overlay, check_imports

    def select(self, text, probe_cpu, probe_wall):
        try:
            selected, overlay, check_imports = self.analyze(text)
        except (OSError, UnicodeError, PatchError):
            return split_workers(self.numbers, self.seconds)[0]

        workers, cost = split_workers(selected, self.seconds)
        if not overlay:
            return workers

        ids = {
            self.nodeids[n] for n in selected - self.unmatched
            if self.nodeids[n] not in self.unverified
        }
        trace_seconds = sum(self.estimate.get(node, float("inf")) for node in ids)
        check_tests = (
            check_imports and bool(ids) and probe_cpu < 28
            and trace_seconds < min(3.0, cost - 1.5)
        )
        if selected == self.numbers and not check_tests:
            return workers
        if not check_imports:
            return workers
        if probe_cpu > 50 or probe_wall > 135:
            return split_workers(self.numbers, self.seconds)[0] if check_imports else workers
        result = check_patch(self.base, overlay, ids if check_tests else set())
        if result is None:
            if check_imports:
                return split_workers(self.numbers, self.seconds)[0]
            return workers
        passed = set(result["passed"]) - set(result["failed"])
        for node in passed:
            selected.difference_update(self.numbers_by_node[node])
        for file in result["bad_files"]:
            if file in self.by_file:
                selected.update(self.by_file[file])
            else:
                # A package or session level collection error can hide any item.
                selected.update(self.numbers)
        return split_workers(selected, self.seconds)[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--base", type=Path, default=Path("/baseline"))
    parser.add_argument("--trace", type=Path)
    args = parser.parse_args()
    data = json.loads((args.data / "tests.json").read_text())
    if args.trace is None:
        trace = json.loads(lzma.decompress(base64.b85decode(_COVERAGE)))
    else:
        with gzip.open(args.trace, "rt") as f:
            trace = json.load(f)
    selector = Selector(args.base.resolve(), trace, data["tests"] if isinstance(data, dict) else data)
    started = time.monotonic()
    for patch in sorted((args.data / "commits").glob("*.patch")):
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        text = patch.read_bytes().decode("utf-8", errors="replace")
        workers = selector.select(text, usage.ru_utime + usage.ru_stime, time.monotonic() - started)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
