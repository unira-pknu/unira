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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)
        if sql.startswith("select count(*)"):
            self.result = [(len(self.conn.pending),)]
        elif sql.startswith("select c.id"):
            after, size = params
            self.result = [row for row in self.conn.pending if row[0] > after][:size]

    def fetchone(self):
        return self.result[0]

    def fetchall(self):
        return self.result

    def copy(self, _sql):
        return FakeCopy(self.conn.copied)


class FakeConn:
    def __init__(self, pending):
        self.pending = sorted(pending)
        self.sql, self.copied, self.commits = [], [], 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


def encode(texts):
    return [[0.5] * backfill.E5_DIMENSIONS for _ in texts]


class BackfillTests(unittest.TestCase):
    def test_rounds_walk_ids_in_order_and_commit_each_round(self):
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
        self.assertIn("e.content_hash <> c.content_hash", backfill.pending_sql("rag"))
        self.assertIn("join public.rag_chunks c on c.id = s.id", backfill.merge_sql("rag"))
        with self.assertRaises(ValueError):
            backfill.pending_sql("rag; drop table x")

    def test_halfvec_literal(self):
        self.assertEqual("[0.12346,-1,0]", backfill.halfvec_literal([0.123456789, -1.0, 0.0]))


if __name__ == "__main__":
    unittest.main()
