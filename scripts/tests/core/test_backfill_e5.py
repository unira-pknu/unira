"""backfill_e5: pending selection, resumable rounds and the halfvec payload (no database)."""

import unittest

from scripts.vectorization import backfill_e5 as backfill


class FakeCopy:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write_row(self, row):
        self.sink.append(row)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)
        if sql.startswith("select count(*)"):
            self.result = [(len(self.conn.pending),)]
        elif sql.startswith("select c.id"):
            self.result = list(self.conn.pending)

    def fetchmany(self, size):
        rows, self.result = self.result[:size], self.result[size:]
        return rows

    def fetchone(self):
        return self.result[0]

    def fetchall(self):
        return self.result

    def copy(self, _sql):
        return FakeCopy(self.conn.copied)


class FakeConn:
    def __init__(self, pending):
        self.pending = sorted(pending)
        self.sql, self.copied, self.commits, self.named = [], [], 0, []

    def cursor(self, name=None):
        if name:
            self.named.append(name)
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


def encode(texts):
    return [[0.5] * backfill.E5_DIMENSIONS for _ in texts]


class BackfillTests(unittest.TestCase):
    def test_rounds_stream_pending_rows_and_commit_each_round(self):
        conn = FakeConn([(f"id{i}", f"h{i}", f"본문 {i}") for i in range(5)])
        seen = []
        written = backfill.fill_family(conn, "rule", lambda texts: (seen.extend(texts), encode(texts))[1],
                                       round_size=2)
        self.assertEqual(5, written)
        self.assertEqual([row[0] for row in conn.copied], [f"id{i}" for i in range(5)])
        self.assertEqual(seen[0], "passage: 본문 0")
        self.assertEqual(1 + 3, conn.commits)  # count + 3 rounds
        self.assertEqual(3, sum("insert into public.rule_chunks_e5" in s for s in conn.sql))

    def test_limit_stops_early(self):
        conn = FakeConn([(f"id{i}", "h", "본문") for i in range(5)])
        self.assertEqual(3, backfill.fill_family(conn, "rule", encode, round_size=2, limit=3))
        self.assertEqual(3, len(conn.copied))

    def test_wrong_vector_size_writes_nothing(self):
        conn = FakeConn([("id0", "h", "본문")])
        with self.assertRaises(ValueError):
            backfill.fill_family(conn, "rule", lambda texts: [[0.1] * 384 for _ in texts], round_size=2)
        self.assertEqual([], conn.copied)

    def test_sql_targets_only_known_families_and_checks_content_hash(self):
        self.assertIn("e.content_hash = c.content_hash", backfill.pending_sql("rag"))
        self.assertNotIn("order by", backfill.pending_sql("rag"))  # storage order, one sequential pass
        self.assertIn("join public.rag_chunks c on c.id = s.id", backfill.merge_sql("rag"))
        with self.assertRaises(ValueError):
            backfill.pending_sql("rag; drop table x")

    def test_halfvec_literal(self):
        self.assertEqual("[0.12346,-1,0]", backfill.halfvec_literal([0.123456789, -1.0, 0.0]))


class ReadConnectionTests(unittest.TestCase):
    def test_rows_stream_from_the_read_connection_and_writes_commit_elsewhere(self):
        write = FakeConn([])
        read = FakeConn([(f"id{i}", "h", "본문") for i in range(3)])
        written = backfill.fill_family(write, "rag", encode, round_size=2, total=3, read_conn=read)
        self.assertEqual(3, written)
        self.assertEqual(["e5_pending_rag"], read.named)
        self.assertEqual(0, read.commits)  # a commit there would close the cursor
        self.assertEqual(2, write.commits)
        self.assertEqual(3, len(write.copied))


class DeletedChunkTests(unittest.TestCase):
    """A loader deleting a chunk mid-round must not stop the run."""

    def conn_failing(self, times):
        from psycopg import errors

        conn = FakeConn([(f"id{i}", "h", "본문") for i in range(3)])
        conn.rollbacks, failures = 0, [times]
        plain_cursor = conn.cursor

        def cursor(name=None):
            cur = plain_cursor(name)
            plain_execute = cur.execute

            def execute(sql, params=None):
                plain_execute(sql, params)
                if sql.startswith("insert into") and failures[0]:
                    failures[0] -= 1
                    raise errors.ForeignKeyViolation("chunk deleted")
            cur.execute = execute
            return cur

        def rollback():
            conn.rollbacks += 1
            conn.copied.clear()  # the stage table empties on rollback
        conn.cursor, conn.rollback = cursor, rollback
        return conn

    def test_round_is_retried_after_a_deleted_chunk(self):
        conn = self.conn_failing(1)
        self.assertEqual(3, backfill.fill_family(conn, "rag", encode, round_size=3, total=3))
        self.assertEqual(1, conn.rollbacks)
        self.assertEqual(3, len(conn.copied))  # the retry staged the whole round again

    def test_gives_up_after_repeated_failures(self):
        from psycopg import errors

        conn = self.conn_failing(backfill.WRITE_ATTEMPTS)
        with self.assertRaises(errors.ForeignKeyViolation):
            backfill.fill_family(conn, "rag", encode, round_size=3, total=3)
        self.assertEqual(backfill.WRITE_ATTEMPTS, conn.rollbacks)


class DatasetCopyTests(unittest.TestCase):
    """Migration 026: the rag department copy."""

    def test_rag_rounds_also_write_the_copy_in_the_same_transaction(self):
        conn = FakeConn([(f"id{i}", "h", "본문") for i in range(3)])
        backfill.fill_family(conn, "rag", encode, round_size=3, total=3, dataset_copy=True)
        merged = [s for s in conn.sql if s.startswith("insert into")]
        self.assertEqual(["rag_chunks_e5 ", "rag_chunks_e5_dataset "],
                         [s.split("public.")[1].split("(")[0] for s in merged])
        self.assertEqual(1, conn.commits)  # one commit covers both

    def test_copy_is_written_only_for_rag_and_only_when_asked(self):
        for family, flag in (("rag", False), ("rule", True)):
            conn = FakeConn([("id0", "h", "본문")])
            backfill.fill_family(conn, family, encode, round_size=1, total=1, dataset_copy=flag)
            self.assertFalse(any("rag_chunks_e5_dataset" in s for s in conn.sql), family)

    def test_copy_merge_keeps_only_departments_already_copied(self):
        sql = backfill.dataset_copy_merge_sql()
        self.assertIn("where exists (select 1 from public.rag_chunks_e5_dataset d", sql)
        self.assertIn("on conflict (id) do update", sql)

    def test_departments_selected_by_size_or_by_name(self):
        counts = {"stat": 7109, "ce": 112518, "nulife": 9243, None: 5}
        self.assertEqual(["nulife", "stat"], backfill.select_copy_datasets(counts, 50000))
        self.assertEqual(["ce"], backfill.select_copy_datasets(counts, 50000, ["ce"]))
        with self.assertRaises(ValueError):
            backfill.select_copy_datasets(counts, 50000, ["nope"])

    def test_full_build_truncates_once_and_commits_per_department(self):
        conn = FakeConn([])
        backfill.build_dataset_copy(conn, ["nulife", "stat"], replace_all=True)
        self.assertEqual(1, sum(s.startswith("truncate") for s in conn.sql))
        self.assertFalse(any(s.startswith("delete") for s in conn.sql))
        self.assertEqual(3, conn.commits)

    def test_named_build_replaces_only_those_departments(self):
        conn = FakeConn([])
        backfill.build_dataset_copy(conn, ["stat"], replace_all=False)
        self.assertFalse(any(s.startswith("truncate") for s in conn.sql))
        self.assertEqual(1, sum(s.startswith("delete") for s in conn.sql))


class RunTests(unittest.TestCase):
    def test_second_run_does_not_get_the_lock(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.lock"
            first = backfill.acquire_run_lock(path)
            self.assertIsNotNone(first)
            self.assertIsNone(backfill.acquire_run_lock(path))
            first.close()
            again = backfill.acquire_run_lock(path)
            self.assertIsNotNone(again)
            again.close()

    def test_nothing_pending_ends_without_loading_the_model(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        class Conn(FakeConn):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=None):
                cur = self.cursor()
                cur.execute(sql, params)
                return cur

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(backfill, "LOCK_PATH", Path(tmp) / "x.lock"), \
                patch("scripts.db.connect_postgres", return_value=Conn([])), \
                patch.object(backfill, "load_encoder") as load:
            self.assertEqual(0, backfill.main([]))
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
