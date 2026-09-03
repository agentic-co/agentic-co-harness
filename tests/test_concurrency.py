"""Lock contention: concurrent writers must not corrupt the queue."""

import threading

from agentco_harness.beads import Beads, Task, TaskStatus

WRITERS = 8
CREATES_PER_WRITER = 10


def test_concurrent_creates_do_not_corrupt(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    errors = []

    def writer(n: int):
        try:
            # Each thread gets its own Beads handle, mirroring daemon + CLI
            # racing as separate writers against the same file.
            own = Beads(tmp_path / "tasks.jsonl")
            for i in range(CREATES_PER_WRITER):
                own.create(title=f"w{n}-t{i}", description="x")
        except Exception as e:  # pragma: no cover - failure reporting
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(WRITERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    tasks = beads._read_all()
    assert len(tasks) == WRITERS * CREATES_PER_WRITER
    assert len(beads._quarantined) == 0
    assert len({t.id for t in tasks}) == WRITERS * CREATES_PER_WRITER


def test_concurrent_updates_serialize(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    created = [beads.create(title=f"t{i}", description="x") for i in range(20)]

    def updater(task_id: str):
        own = Beads(tmp_path / "tasks.jsonl")
        own.update(task_id, status=TaskStatus.DONE)

    threads = [threading.Thread(target=updater, args=(t.id,)) for t in created]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    tasks = beads._read_all()
    assert len(tasks) == 20
    assert all(t.status == TaskStatus.DONE for t in tasks)
    assert len(beads._quarantined) == 0
