"""No harness module may import `fcntl` except `agentco_harness/filelock.py`.

`fcntl` is POSIX-only; a module-scope import of it made the CLI fail at import
on native Windows (beads, asop_store, natural_key, recurring, schedules). The
lock goes through `filelock`, which picks `msvcrt` there. This test is what
stops a sixth module from reintroducing it.
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path

from agentco_harness.filelock import lock_exclusive, unlock

PACKAGE = Path(__file__).resolve().parents[1] / "agentco_harness"


def _imports_fcntl(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import) and any(a.name == "fcntl" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "fcntl":
            return True
    return False


def test_only_filelock_imports_fcntl():
    offenders = sorted(str(p.relative_to(PACKAGE)) for p in PACKAGE.rglob("*.py")
                       if p.name != "filelock.py" and _imports_fcntl(p))
    assert offenders == [], f"route these through agentco_harness.filelock: {offenders}"


def test_the_lock_excludes_a_second_holder(tmp_path):
    path = tmp_path / "x.lock"
    order = []
    with open(path, "a+") as first:
        lock_exclusive(first)

        def second():
            with open(path, "a+") as handle:
                lock_exclusive(handle)
                order.append("second")
                unlock(handle)

        thread = threading.Thread(target=second)
        thread.start()
        thread.join(0.3)
        order.append("first")
        unlock(first)
    thread.join(5)
    assert order == ["first", "second"]
