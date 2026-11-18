"""e5 search switch: RPC suffix, similarity rescaling, side-table vectors, query prefix."""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from app import retrieval
from app.embeddings import Embedder
from app.retrieval import (_fetch_notice_chunks, _fetch_rpc_responses, _notice_chunk_rows,
                           raw_min_similarity, rescale_similarity, search)

RPC = "match_pknu_main_documents"


class RecordingClient:
    def __init__(self):
        self.calls = {}

    def rpc(self, name, params):
        self.calls[name] = dict(params)
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=[{"id": name}]))


def test_suffix_changes_only_the_vector_call_and_keeps_result_keys():
    client = RecordingClient()
    result = _fetch_rpc_responses(client, ["a", "b"], {}, {"a": 3, "b": 3}, "q", vector_rpc_suffix="_e5")
    assert set(client.calls) == {"a_e5", "a_lexical", "b_e5", "b_lexical"}
    assert set(result) == {"a", "a_lexical", "b", "b_lexical"}
    assert result["a"][0].data == [{"id": "a_e5"}]


def test_no_suffix_calls_the_original_functions():
    client = RecordingClient()
    _fetch_rpc_responses(client, ["a"], {}, {"a": 3}, None)
    assert set(client.calls) == {"a"}


def test_rescale_maps_floor_to_zero_and_leaves_default_unchanged():
    assert rescale_similarity(0.8, 0.0) == 0.8
    assert rescale_similarity(0.75, 0.75) == 0.0
    assert rescale_similarity(1.0, 0.75) == 1.0
    assert rescale_similarity(0.875, 0.75) == pytest.approx(0.5)
    assert rescale_similarity(None, 0.75) is None


def test_ceiling_widens_packed_scores_and_caps_at_one():
    assert rescale_similarity(0.8, 0.7, 0.9) == pytest.approx(0.5)
    assert rescale_similarity(0.95, 0.7, 0.9) == 1.0
    assert rescale_similarity(0.8, 0.0, 1.0) == 0.8
    assert raw_min_similarity(0.3, 0.7, 0.9) == pytest.approx(0.76)
    assert raw_min_similarity(0.3, 0.0, 1.0) == 0.3


def _row(name, similarity):
    return {"id": "db-" + name, "source_id": "src-" + name, "chunk_id": 0, "chunk_index": 0,
            "title": "교류수학 안내", "url": "https://www.pknu.ac.kr/" + name,
            "content": name + " 교류수학 신청은 학생포털에서 진행합니다. 성적증명서와 신청서를 제출해야 합니다. " * 3,
            "similarity": similarity,
            "metadata": {"rpc_name": RPC, "source_dataset": "pknu_main"}}


def _run(rows, **options):
    captured = {}

    def fake_fetch(client, rpc_names, payload, *args, **kwargs):
        captured["payload"], captured["kwargs"] = payload, kwargs
        captured["source"] = retrieval._VECTOR_SOURCE.get()
        return {RPC: (SimpleNamespace(data=rows), 1.0, None),
                RPC + "_lexical": (SimpleNamespace(data=[]), 1.0, None)}

    with patch("app.retrieval._fetch_rpc_responses", side_effect=fake_fetch), patch(
        "app.retrieval._term_title_chunks", return_value=[]
    ), patch("app.retrieval._focus_department", side_effect=lambda client, embedding, rows, *a: rows):
        result = search(None, rpc_names=[RPC], embedding=[1.0], top_k=2, first_stage_k=2,
                        min_similarity=0.3, query_text="교류수학 신청 안내", **options)
    return result, captured


def test_ceiling_reaches_the_rpc_threshold_and_the_helpers():
    rows = [_row("A", 0.86), _row("B", 0.80)]
    _result, captured = _run(rows, vector_rpc_suffix="_e5", similarity_floor=0.7, similarity_ceiling=0.9)
    assert captured["payload"]["min_similarity"] == pytest.approx(0.76)
    assert captured["source"] == retrieval._VectorSource("_e5", 0.7, 0.9)
    assert [r["similarity"] for r in rows] == pytest.approx([0.8, 0.5])


def test_floor_raises_the_database_threshold_and_rescales_vector_rows():
    rows = [_row("A", 0.95), _row("B", 0.85)]
    result, captured = _run(rows, vector_rpc_suffix="_e5", similarity_floor=0.75)
    assert captured["payload"]["min_similarity"] == pytest.approx(0.75 + 0.3 * 0.25)
    assert captured["kwargs"]["vector_rpc_suffix"] == "_e5"
    assert captured["source"] == retrieval._VectorSource("_e5", 0.75, 1.0)
    assert [r["similarity"] for r in rows] == pytest.approx([0.8, 0.4])
    assert retrieval._VECTOR_SOURCE.get() == retrieval._VectorSource()  # reset after the call
    assert [r["id"] for r in result] == ["db-A", "db-B"]


def test_default_search_keeps_the_threshold_and_scores():
    rows = [_row("A", 0.95)]
    _result, captured = _run(rows)
    assert captured["payload"]["min_similarity"] == 0.3
    assert rows[0]["similarity"] == 0.95


class TableClient:
    """client.table(name).select(cols).in_(...).lt(...).limit(n).execute()"""

    def __init__(self, data):
        self.data, self.selects = data, {}

    def table(self, name):
        client = self

        class Query:
            def select(self, columns):
                client.selects[name] = columns
                return self

            def __getattr__(self, _attr):
                return lambda *a, **k: self

            def execute(self):
                return SimpleNamespace(data=[dict(row) for row in client.data.get(name, [])])

        return Query()


def test_notice_chunks_read_current_side_table_vectors():
    chunks = [
        {"id": "c1", "source_id": "s", "content_hash": "h1", "e5": {"content_hash": "h1", "embedding": "[1,0]"}},
        {"id": "c2", "source_id": "s", "content_hash": "h2-new", "e5": {"content_hash": "h2", "embedding": "[0,1]"}},
        {"id": "c3", "source_id": "s", "content_hash": "h3", "e5": None},
    ]
    client = TableClient({"pknu_notice_sources": [{"id": "s"}], "pknu_notice_chunks": chunks})
    token = retrieval._VECTOR_SOURCE.set(retrieval._VectorSource("_e5", 0.75))
    try:
        _sources, fetched = _fetch_notice_chunks(client, "pknu_notice_sources", "pknu_notice_chunks", ["s"])
    finally:
        retrieval._VECTOR_SOURCE.reset(token)
    assert "e5:pknu_notice_chunks_e5(content_hash, embedding)" in client.selects["pknu_notice_chunks"]
    assert [c["embedding"] for c in fetched] == ["[1,0]", None, None]
    assert all("e5" not in c for c in fetched)


def test_notice_chunks_default_reads_the_original_column():
    client = TableClient({"pknu_notice_sources": [], "pknu_notice_chunks": []})
    _fetch_notice_chunks(client, "pknu_notice_sources", "pknu_notice_chunks", ["s"])
    assert client.selects["pknu_notice_chunks"].endswith(", embedding")


def test_notice_rows_skip_vectors_of_another_size():
    source = {"id": "s", "title": "교류수학 안내", "url": "https://www.pknu.ac.kr/s", "metadata": {}}
    chunk = {"id": "c", "source_id": "s", "chunk_index": 0,
             "content": "교류수학 신청은 학생포털에서 진행합니다. " * 5, "embedding": [1.0, 0.0, 0.0]}
    assert _notice_chunk_rows(RPC, [source], [chunk], [1.0, 0.0], "교류수학", [], 0.3, 0.15, 0.1) == []
    same_size = _notice_chunk_rows(RPC, [source], [chunk], [1.0, 0.0, 0.0], "교류수학", [], 0.3, 0.15, 0.1)
    assert [r["id"] for r in same_size] == ["c"]


def test_embedder_puts_the_query_prefix_in_front():
    seen = []

    class FakeModel:
        def __init__(self, name, device):
            pass

        def get_sentence_embedding_dimension(self):
            return 2

        def encode(self, texts, **kwargs):
            seen.extend(texts)
            return [[0.6, 0.8]]

    module = ModuleType("sentence_transformers")
    module.SentenceTransformer = FakeModel
    with patch.dict(sys.modules, {"sentence_transformers": module}):
        embedder = Embedder(model_name="e5", device="cpu", expected_dimensions=2, query_prefix="query: ")
        assert embedder.encode_query("휴학 신청") == [0.6, 0.8]
        Embedder(model_name="m", device="cpu", expected_dimensions=2).encode_query("휴학 신청")
    assert seen == ["query: 휴학 신청", "휴학 신청"]
