#!/usr/bin/env python3
"""Build a precise file → test-numbers map from the pinned sqlglot checkout.

Strategy (fast, no test execution):
1. Walk every test file under tests/.
2. Parse its import statements (ast) to discover which sqlglot.* modules it
   touches.
3. Also map by conventional naming: tests/dialects/test_foo.py ↔ sqlglot/dialects/foo.py
4. For optimizer fixtures we keep the exact method-name mapping in the
   runtime selector (more precise than coverage for those).

The resulting JSON is consumed by selector.py at runtime.
"""

from __future__ import annotations

import ast
import json
import sys
from collections import defaultdict
from pathlib import Path


def module_from_path(rel: str) -> str | None:
    """sqlglot/optimizer/foo.py → sqlglot.optimizer.foo"""
    if not rel.endswith(".py"):
        return None
    parts = Path(rel).with_suffix("").parts
    if parts[0] != "sqlglot":
        return None
    return ".".join(parts)


def collect_imports(py_file: Path) -> set[str]:
    """Return the set of sqlglot.* modules imported by this file."""
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()

    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("sqlglot"):
                    mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("sqlglot"):
                mods.add(node.module)
                # also record the parent packages
                parts = node.module.split(".")
                for i in range(1, len(parts)):
                    mods.add(".".join(parts[:i]))
    return mods


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: build_map.py <sqlglot-root> <out.json> [tests.json]", file=sys.stderr)
        sys.exit(1)

    sqlglot_root = Path(sys.argv[1])          # .../sqlglot (the repo root)
    out_path = Path(sys.argv[2])

    # Optional: real tests.json so we can emit actual test numbers.
    # During Docker build we may not have it; in that case we emit a
    # module → list-of-test-ids map and the runtime will resolve numbers.
    tests_json = Path(sys.argv[3]) if len(sys.argv) > 3 else None

    # ------------------------------------------------------------------
    # 1. Index every source file under sqlglot/
    # ------------------------------------------------------------------
    src_files: dict[str, str] = {}  # module → relative path
    for py in (sqlglot_root / "sqlglot").rglob("*.py"):
        rel = str(py.relative_to(sqlglot_root))
        mod = module_from_path(rel)
        if mod:
            src_files[mod] = rel

    # ------------------------------------------------------------------
    # 2. Walk test files and record which modules they import
    # ------------------------------------------------------------------
    # module → set of test file paths (relative to repo root)
    module_to_testfiles: dict[str, set[str]] = defaultdict(set)

    tests_root = sqlglot_root / "tests"
    if not tests_root.is_dir():
        print("WARNING: no tests/ directory found", file=sys.stderr)
    else:
        for py in tests_root.rglob("*.py"):
            rel_test = str(py.relative_to(sqlglot_root))
            imports = collect_imports(py)
            for mod in imports:
                module_to_testfiles[mod].add(rel_test)
                # also parent packages
                parts = mod.split(".")
                for i in range(1, len(parts)):
                    module_to_testfiles[".".join(parts[:i])].add(rel_test)

    # ------------------------------------------------------------------
    # 3. Conventional naming: dialects, parsers, generators
    # ------------------------------------------------------------------
    for py in (sqlglot_root / "sqlglot").rglob("*.py"):
        rel = str(py.relative_to(sqlglot_root))
        stem = py.stem
        # dialects
        if "/dialects/" in rel:
            candidate = f"tests/dialects/test_{stem}.py"
            if (sqlglot_root / candidate).exists():
                module_to_testfiles.setdefault(module_from_path(rel) or "", set()).add(candidate)
        # parsers / generators often share the dialect test file
        if "/parsers/" in rel or "/generators/" in rel:
            candidate = f"tests/dialects/test_{stem}.py"
            if (sqlglot_root / candidate).exists():
                module_to_testfiles.setdefault(module_from_path(rel) or "", set()).add(candidate)

    # ------------------------------------------------------------------
    # 4. Turn module → test-files into file → test-numbers (if we have tests.json)
    # ------------------------------------------------------------------
    file_to_tests: dict[str, list[int]] = defaultdict(list)

    if tests_json and tests_json.exists():
        data = json.loads(tests_json.read_text())
        # test file → list of numbers
        by_file: dict[str, list[int]] = defaultdict(list)
        for t in data["tests"]:
            by_file[t["file"]].append(t["number"])

        for mod, testfiles in module_to_testfiles.items():
            src = src_files.get(mod)
            if not src:
                continue
            nums: set[int] = set()
            for tf in testfiles:
                nums.update(by_file.get(tf, []))
            if nums:
                file_to_tests[src] = sorted(nums)
    else:
        # No tests.json at build time – emit the module→testfile map.
        # Runtime will resolve numbers from the real /data/tests.json.
        for mod, testfiles in module_to_testfiles.items():
            src = src_files.get(mod)
            if src:
                file_to_tests[src] = sorted(testfiles)  # type: ignore[arg-type]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(file_to_tests, indent=None, separators=(",", ":")))
    print(f"wrote map with {len(file_to_tests)} entries → {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
