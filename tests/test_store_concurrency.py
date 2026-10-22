"""The shared SQLite connection must give every thread a complete, correct row: a missing row is a lost sign-in. No network."""
import sys
import threading
import time

from app.db import Store


def test_concurrent_readers_and_writers_never_see_a_missing_or_wrong_row():
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # force threads to interleave as much as possible
    try:
        s = Store()
        s.q("CREATE TABLE t(id TEXT PRIMARY KEY, v TEXT, n REAL)")
        for i in range(12):
            s.q("INSERT INTO t VALUES(?,?,?)", (f"k{i}", f"v{i}", 0.0))
        problems, stop = [], threading.Event()

        def reader(i):
            while not stop.is_set():
                try:
                    row = s.one("SELECT * FROM t WHERE id=?", (f"k{i}",))
                    rows = s.all("SELECT * FROM t WHERE id=?", (f"k{i}",))
                    if not row or row["v"] != f"v{i}" or len(rows) != 1 or rows[0]["id"] != f"k{i}":
                        problems.append((i, row, rows))
                except Exception as e:  # noqa: BLE001 - any failure is a finding
                    problems.append((i, repr(e)))

        def writer(i):
            n = 0
            while not stop.is_set():
                n += 1
                s.q("UPDATE t SET n=? WHERE id=?", (float(n), f"k{i % 12}"))

        ts = [threading.Thread(target=reader, args=(i % 12,)) for i in range(10)] + [threading.Thread(target=writer, args=(i,)) for i in range(3)]
        [t.start() for t in ts]
        time.sleep(1.5)
        stop.set()
        [t.join() for t in ts]
        assert problems == []
    finally:
        sys.setswitchinterval(old)
