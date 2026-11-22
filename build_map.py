from __future__ import annotations

"""Compare Python statement trees and look up impact in the baseline trace."""


import ast
import copy
from difflib import SequenceMatcher


FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)
DEFINITION = (*FUNCTION, ast.ClassDef)
BLOCKS = ("body", "orelse", "finalbody")
COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar)


def fingerprint(node):
    return ast.dump(node, include_attributes=False)


def header(node):
    fields = [(name, value) for name, value in ast.iter_fields(node) if name not in BLOCKS]
    return tuple((name, field_key(value)) for name, value in fields)


def field_key(value):
    if isinstance(value, ast.AST):
        return fingerprint(value)
    if isinstance(value, list):
        return tuple(map(field_key, value))
    return value


def statement_key(node):
    if isinstance(node, DEFINITION):
        return type(node), node.name
    if isinstance(node, COMPOUND):
        return type(node), header(node)
    return fingerprint(node)


def start(node):
    return min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", ()))])


def local_names(node):
    """Detect compiler scope changes even when the edited line is unreached."""
    names = set()

    def visit(node):
        if isinstance(node, DEFINITION):
            names.add(("definition", node.name))
            return
        if isinstance(node, ast.Lambda):
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(("local", node.id))
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update((type(node), name) for name in node.names)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(("local", a.asname or a.name.split(".")[0]) for a in node.names)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(("local", node.name))
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(("local", node.rest))
        for child in ast.iter_child_nodes(node):
            visit(child)

    for stmt in node.body:
        visit(stmt)
    return names


def changed_lambda_bodies(old, new):
    """Return old lambda bodies if an assignment changes only their bodies."""
    if not isinstance(old, (ast.Assign, ast.AnnAssign)) or type(old) is not type(new):
        return None

    def mask(node):
        bodies = []

        class Mask(ast.NodeTransformer):
            def visit_Lambda(self, item):
                bodies.append(item.body)
                item.body = ast.Constant(None)
                return item

        return Mask().visit(copy.deepcopy(node)), bodies

    before, old_bodies = mask(old)
    after, new_bodies = mask(new)
    if not old_bodies or len(old_bodies) != len(new_bodies) or fingerprint(before) != fingerprint(after):
        return None
    return [
        body for body, updated in zip(old_bodies, new_bodies)
        if fingerprint(body) != fingerprint(updated)
    ]


class PythonImpact:
    def __init__(self, covered, at_collection):
        self.covered = covered
        self.at_collection = at_collection
        self.profiles = set()
        self.import_check = False
        self.whole_file = False
        self.changed = False
        self.edits = 0
        self.class_attributes = set()

    def record(self, first, last):
        for line in range(first, last + 1):
            self.profiles.add(self.covered.get(line, 0))
            if line in self.at_collection:
                self.import_check = True

    def record_node(self, node):
        self.record(start(node), node.end_lineno)

    def structural_change(self):
        self.import_check = True
        self.whole_file = True

    def compare(self, before, after):
        self.compare_block(before.body, after.body)

    def compare_block(self, old, new, function=None, owner=None, class_scope=False):
        if not old and not new:
            return
        matcher = SequenceMatcher(None, list(map(statement_key, old)), list(map(statement_key, new)),
                                  autojunk=False)
        for tag, first, last, new_first, new_last in matcher.get_opcodes():
            if tag != "equal":
                self.changed = True
                self.edits += 1
                if function is None:
                    if class_scope and last - first == new_last - new_first == 1:
                        bodies = changed_lambda_bodies(old[first], new[new_first])
                        if bodies is not None:
                            for body in bodies:
                                self.record_node(body)
                            continue
                    if class_scope:
                        for node in [*old[first:last], *new[new_first:new_last]]:
                            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                                for target in targets:
                                    self.class_attributes.update(
                                        name.id for name in ast.walk(target)
                                        if isinstance(name, ast.Name)
                                    )
                    self.structural_change()
                    continue
                for node in old[first:last]:
                    self.record_node(node)
                if tag == "insert" or new_last - new_first > last - first:
                    if first > 0:
                        self.record_node(old[first - 1])
                    if last < len(old):
                        self.record_node(old[last])
                    if not old and owner is not None:
                        self.record(owner.lineno, owner.lineno)
                continue
            for prior, current in zip(old[first:last], new[new_first:new_last]):
                if isinstance(prior, DEFINITION):
                    if header(prior) != header(current):
                        self.changed = True
                        self.edits += 1
                        self.record_node(prior)
                        if function is None:
                            self.structural_change()
                        else:
                            self.record_node(function)
                    method = prior if isinstance(prior, FUNCTION) else None
                    old_changed = self.edits
                    self.compare_block(
                        prior.body, current.body, method, class_scope=isinstance(prior, ast.ClassDef)
                    )
                    if (
                        method is not None
                        and self.edits != old_changed
                        and local_names(prior) != local_names(current)
                    ):
                        self.record_node(prior)
                elif isinstance(prior, COMPOUND):
                    for field in BLOCKS:
                        self.compare_block(
                            getattr(prior, field, ()), getattr(current, field, ()),
                            function, prior, class_scope
                        )


"""Read and apply independent unified diffs without writing to the checkout."""

from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(ValueError):
    pass


def path_from_header(header: str) -> str | None:
    name = header[4:].split("\t", 1)[0]
    if name == "/dev/null":
        return None
    if name.startswith('"'):
        # Git uses C quoting for unusual paths. Fall back to the full suite
        # instead of interpreting a path we cannot verify.
        raise PatchError("quoted file path")
    if name.startswith(("a/", "b/")):
        name = name[2:]
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        raise PatchError("unsafe or invalid file path")
    return name


@dataclass
class FilePatch:
    old: str | None
    new: str | None
    hunks: list = field(default_factory=list)

    def apply(self, source: str) -> str:
        chunks = source.split("\n")
        original = [line + "\n" for line in chunks[:-1]]
        if chunks[-1]:
            original.append(chunks[-1])
        result = []
        cursor = 0
        for start, count, lines in self.hunks:
            offset = start - bool(count)
            if not cursor <= offset <= len(original):
                raise PatchError("overlapping or out-of-range hunks")
            result.extend(original[cursor:offset])
            cursor = offset
            for line in lines:
                kind, content = line[0], line[1:]
                if kind != "+":
                    if cursor >= len(original) or original[cursor] != content:
                        raise PatchError("patch context does not match pinned source")
                    cursor += 1
                if kind != "-":
                    result.append(content)
        return "".join(result + original[cursor:])


def parse_patch(text: str) -> list[FilePatch]:
    # A diff is delimited by LF; other Unicode separators may occur in source.
    lines = text.removesuffix("\n").split("\n")
    changes = []
    current = None
    metadata_only = False
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("diff --git "):
            if metadata_only:
                raise PatchError("file change without text headers")
            current = None
            metadata_only = True
        elif line.startswith("--- "):
            if i >= len(lines) or not lines[i].startswith("+++ "):
                raise PatchError("missing new path")
            current = FilePatch(path_from_header(line), path_from_header(lines[i]))
            changes.append(current)
            metadata_only = False
            i += 1
        elif line.startswith("@@ "):
            match = HUNK.match(line)
            if not match or current is None:
                raise PatchError("invalid hunk header")
            start, count, _, new_count = match.groups()
            old_left, new_left = int(count or "1"), int(new_count or "1")
            body = []
            while old_left or new_left:
                if i == len(lines) or lines[i][:1] not in (" ", "+", "-"):
                    raise PatchError("truncated hunk")
                kind = lines[i][0]
                body.append(lines[i] + "\n")
                old_left -= kind != "+"
                new_left -= kind != "-"
                i += 1
                if min(old_left, new_left) < 0:
                    raise PatchError("invalid hunk size")
                if i < len(lines) and lines[i] == "\\ No newline at end of file":
                    body[-1] = body[-1].removesuffix("\n")
                    i += 1
            current.hunks.append((int(start), int(count or "1"), body))
    if metadata_only or not changes or any(not change.hunks for change in changes):
        raise PatchError("unsupported or empty patch")
    return changes


"""Check collection and, when inexpensive, predicted tests on an isolated overlay."""


import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


def make_overlay(base, target, changes):
    grouped = {}
    for name, source in changes.items():
        entry, sep, tail = name.partition("/")
        if sep:
            grouped.setdefault(entry, {})[tail] = source
        else:
            grouped[entry] = source

    for name in set(grouped) | ({p.name for p in base.iterdir()} if base.is_dir() else set()):
        if name in {".git", "__pycache__", ".pytest_cache"}:
            continue
        dst = target / name
        if name not in grouped:
            dst.symlink_to(base / name)
        elif isinstance(grouped[name], dict):
            dst.mkdir()
            make_overlay(base / name, dst, grouped[name])
        elif grouped[name] is not None:
            dst.write_bytes(grouped[name].encode("utf-8"))


def check_patch(base, overlay, nodes):
    with tempfile.TemporaryDirectory(prefix="br-") as tmp:
        tmp = Path(tmp)
        root = tmp / "repo"
        root.mkdir()
        request = tmp / "request"
        output = tmp / "result"
        try:
            make_overlay(base, root, overlay)
            request.write_text(json.dumps(sorted(nodes)))
            with subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), str(request), str(output)],
                cwd=root,
                env={
                    **os.environ,
                    "OPENBLAS_NUM_THREADS": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    "SKIP_INTEGRATION": "0",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            ) as proc:
                try:
                    if proc.wait(timeout=5):
                        return None
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return None
            return json.loads(output.read_text())
        except (OSError, subprocess.SubprocessError, ValueError):
            return None


def run(request, output):
    import pytest

    keep = set(json.loads(request.read_text()))
    passed, failed, bad_files = set(), set(), set()

    class Results:
        def pytest_collection_modifyitems(self, items):
            deselected = [item for item in items if item.nodeid not in keep]
            items[:] = [item for item in items if item.nodeid in keep]
            if deselected:
                self.config.hook.pytest_deselected(items=deselected)

        def pytest_configure(self, config):
            self.config = config

        def pytest_collectreport(self, report):
            if report.failed:
                bad_files.add(report.nodeid.split("::", 1)[0])

        def pytest_runtest_logreport(self, report):
            if report.failed:
                failed.add(report.nodeid)
            if report.when == "call" and report.passed and type(report) is pytest.TestReport:
                passed.add(report.nodeid)

    # Put the overlay first; the script itself lives outside the checkout.
    sys.path.insert(0, os.getcwd())
    args = ["-q", "-p", "no:cacheprovider", "--continue-on-collection-errors", "tests"]
    if not keep:
        args.append("--collect-only")
    status = pytest.main(args, plugins=[Results()])
    if status not in {pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED,
                      pytest.ExitCode.NO_TESTS_COLLECTED}:
        return 1
    output.write_text(json.dumps({
        "passed": sorted(passed - failed), "failed": sorted(failed), "bad_files": sorted(bad_files)
    }))
    return 0


REQUIREMENTS = 'ast_serialize==0.13.1\ncfgv==3.5.0\ndistlib==0.4.4\nduckdb==2.0.0\nexecnet==2.1.2\nfilelock==4.2.12\nidentify==2.6.20\niniconfig==2.3.1\nJinja2==3.1.6\nlibrt==0.17.0\nmarkdown2==2.5.5\nMarkupSafe==3.0.4\nmypy==2.4.1\nmypy_extensions==1.2.0\nnodeenv==1.12.0\nnumpy==2.5.5\npackaging==26.3\npandas==3.1.1\npandas-stubs==3.1.0.261029\npathspec==1.1.1\npdoc==16.0.0\nplatformdirs==4.14.7\npluggy==1.7.0\npre_commit==4.7.0\npsutil==7.2.2\nPygments==2.21.0\npyperf==2.11.0\npytest==9.2.0\npytest-timeout==2.4.0\npytest-xdist==3.8.0\npython-dateutil==4.0.0\npython-discovery==1.9.6\npytz==2026.6\nPyYAML==6.0.3\nruff==0.15.6\nsetuptools==84.2.1\nsetuptools-scm==10.3.4\ntypes-python-dateutil==2.9.0.20261116\ntypes-pytz==2026.6.0.20261031\ntyping_extensions==4.16.0\ntzdata==2026.6\nvcs-versioning==2.6.0\nvirtualenv==21.15.9\n'

if __name__ == "__main__":
    if sys.argv[1:] == ["--requirements"]:
        print(REQUIREMENTS, end="")
    else:
        raise SystemExit(run(Path(sys.argv[1]), Path(sys.argv[2])))
