"""_fetch_rpc_responses: concurrent RPC fan-out without a live Supabase."""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.retrieval import (
    _cap_per_url,
    _dedupe_key,
    _explicit_attachment_candidates,
    _fetch_rpc_responses,
    _focused_document_chunks,
    _term_title_chunks,
    _title_anchor_chunks,
    search,
)
from app.reviewed_campus_sources import source_rows


class FakeClient:
    def __init__(self, delay=0.0, fail=()):
        self.delay = delay
        self.fail = set(fail)
        self.calls = {}
        self._lock = threading.Lock()

    def rpc(self, name, params):
        client = self

        class Builder:
            def execute(self_inner):
                time.sleep(client.delay)
                with client._lock:
                    client.calls[name] = dict(params)
                if name in client.fail:
                    raise RuntimeError(f"{name} down")
                return SimpleNamespace(data=[{"id": name}])

        return Builder()


class FetchRpcResponsesTests(unittest.TestCase):
    def test_reviewed_campus_source_reaches_answer_evidence_when_search_misses(self):
        rpc_name = "match_pknu_main_documents"
        empty = (SimpleNamespace(data=[]), 1, None)
        with patch("app.retrieval._fetch_rpc_responses", return_value={rpc_name: empty, f"{rpc_name}_lexical": empty}):
            result = search(None, rpc_names=[rpc_name], embedding=[1.0], top_k=2, first_stage_k=4,
                            min_similarity=.3, query_text="부경대 교목 이름은 뭐예요?", capture_trace=True)
        self.assertEqual([row["url"] for row in result], ["https://www.pknu.ac.kr/main/124"])
        self.assertIn("곰솔", result[0]["content"])
        self.assertIn("reviewed_campus_sources", {stage["stage"] for stage in result.trace["stages"]})

    def test_reviewed_campus_source_respects_exam_date_and_institution(self):
        october = source_rows("부경대 2026년 10월 모의 토익 응시료 알려줘")
        self.assertEqual(len(october), 1)
        self.assertIn("4,000원", october[0]["content"])
        self.assertEqual(source_rows("부경대 2026년 11월 모의토익 응시료 알려줘"), [])
        self.assertEqual(source_rows("부산대학교 교목은 뭐야?"), [])

    def test_reviewed_exchange_evidence_preserves_the_in_scope_attachment(self):
        rows = source_rows("인천대학교 2024학년도 여름 학점교류에서 학번은 어떻게 받나요?")
        self.assertEqual(len(rows), 1)
        self.assertIn("2024년 5월 27일", rows[0]["content"])
        self.assertTrue(rows[0]["metadata"]["attachment_name"].endswith(".zip"))
        self.assertEqual(source_rows("인천대 2024학년도 겨울 학점교류 학번 안내"), [])

    def test_full_attachment_name_selects_document_before_chunk_ranking(self):
        guide = {"source_id": "guide", "metadata": {"source_kind": "archive_member", "source_file": "2026학년도 연세대학교 국내교환학생 학사안내.pdf"}}
        form = {"source_id": "form", "metadata": {"source_kind": "archive_member", "source_file": "2026학년도 연세대학교 교류수학 추천자명단.hwp"}}
        unrelated = {"source_id": "other", "metadata": {"source_kind": "attachment", "source_file": "신청서.hwp"}}
        rows = [guide, form, unrelated]
        self.assertEqual(_explicit_attachment_candidates(rows, "2026학년도 연세대학교 국내교환학생 학사안내 내용을 알려줘"), [guide])
        self.assertEqual(_explicit_attachment_candidates(rows, "교류수학 신청 방법"), rows)
        both = "2026학년도 연세대학교 국내교환학생 학사안내와 2026학년도 연세대학교 교류수학 추천자명단 비교"
        self.assertEqual(_explicit_attachment_candidates(rows, both), [guide, form])

    def test_attachments_sharing_one_notice_keep_separate_document_caps_and_chunk_identity(self):
        def row(source, chunk):
            return {"id": source + str(chunk), "source_id": source, "chunk_index": chunk,
                    "url": "https://www.pknu.ac.kr/main/95?action=view&no=123", "title": "교류수학 안내",
                    "content": "공통 문서 제목과 표 머리글 " * 60 + source,
                    "metadata": {"source_kind": "archive_member"}}
        rows = [row(source, chunk) for source in ("guide-pdf", "application-hwp") for chunk in range(3)]
        kept = _cap_per_url(rows, max_chunks_per_url=2)
        self.assertEqual([(r["source_id"], r["chunk_index"]) for r in kept],
                         [("guide-pdf", 0), ("guide-pdf", 1), ("application-hwp", 0), ("application-hwp", 1)])
        self.assertNotEqual(_dedupe_key(rows[0]), _dedupe_key(rows[3]))
        self.assertEqual(_dedupe_key(rows[0]), _dedupe_key({**rows[0], "similarity": .99}))

    def test_title_lookups_scope_departments_without_restricting_school_notices(self):
        queries = []
        class Builder:
            def __init__(self, table):
                self.table = table
                self.filters = None
                queries.append(self)
            def contains(self, column, value):
                self.filters = value
                return self
            def execute(self):
                return SimpleNamespace(data=[])
            def __getattr__(self, name):
                return lambda *args, **kwargs: self
        client = SimpleNamespace(table=Builder)
        for helper in (_title_anchor_chunks, _term_title_chunks):
            queries.clear()
            helper(client, ["match_rag_documents", "match_pknu_notice_documents"], [1.0], [],
                   "2026학년도 2학기 캡스톤디자인 지원 안내", [], .3, .15, .1,
                   metadata_filters={"match_rag_documents": {"ingestion_dataset": "ce", "structured_table": {"rows": []}}})
            rag = [q for q in queries if q.table == "rag_sources"]
            notice = [q for q in queries if q.table == "pknu_notice_sources"]
            self.assertTrue(rag)
            self.assertTrue(notice)
            self.assertTrue(all(q.filters == {"ingestion_dataset": "ce"} for q in rag))
            self.assertTrue(all(q.filters is None for q in notice))

    def test_exact_keyword_hit_reaches_reranker_despite_high_vector_similarities(self):
        def row(identifier, similarity):
            return {"id": identifier, "source_id": identifier, "similarity": similarity,
                    "url": "https://www.pknu.ac.kr/" + identifier, "title": "교류수학 안내",
                    "content": ("교류수학 신청 안내입니다. 학생은 소속 학과에서 이수 가능한 개설 과목을 확인하고 "
                                "성적증명서와 신청서를 제출해야 합니다. 교류 대학의 수업 일정과 학점 인정 기준을 "
                                "확인한 뒤 담당 부서에서 승인 여부를 안내합니다. 등록금 납부와 수강신청 절차도 "
                                "해당 학기 공지의 대상자 조건에 따라 확인해야 합니다."),
                    "metadata": {"source_dataset": "pknu_main", "source_kind": "attachment"}}
        vector = [row("vector-" + str(i), .95) for i in range(6)]
        exact = row("exact-document", 0)
        exact["lexical_rank"] = 1.0
        fetched = {"match_pknu_main_documents": (SimpleNamespace(data=vector), 1, None),
                   "match_pknu_main_documents_lexical": (SimpleNamespace(data=[exact]), 1, None)}
        considered = []
        class Reranker:
            def score(self, question, rows):
                considered.extend(rows)
                return [1.0 if r["id"] == "exact-document" else 0.0 for r in rows]
        with patch("app.retrieval._fetch_rpc_responses", return_value=fetched), \
                patch("app.retrieval._term_title_chunks", return_value=[]), \
                patch("app.retrieval._focus_department", side_effect=lambda client, embedding, rows, *args: rows):
            result = search(None, rpc_names=["match_pknu_main_documents"], embedding=[1.0],
                            top_k=1, first_stage_k=6, min_similarity=.3, query_text="교류수학 안내", reranker=Reranker())
        self.assertEqual(result[0]["id"], "exact-document")
        self.assertLessEqual(len(considered), 6)
        self.assertTrue(any(r["id"].startswith("vector-") for r in considered))

    def test_course_evaluation_guide_is_retrieved_when_hybrid_channels_miss_it(self):
        lookup = Mock()
        for method in ("select", "eq", "in_", "order", "limit"):
            getattr(lookup, method).return_value = lookup
        lookup.execute.return_value = SimpleNamespace(data=[{"id": "guide"}])
        client = SimpleNamespace(table=Mock(return_value=lookup))
        guide = {"id": "guide", "title": "강의평가", "url": "https://www.pknu.ac.kr/main/97"}
        chunk = {
            "id": "guide-0",
            "source_id": "guide",
            "chunk_index": 0,
            "embedding": [1.0],
            "content": ("본문: 강의평가 및 성적공시 안내. 국립 부경대학교 포털 앱에서 "
                        "[종합정보서비스 → 강의평가]를 선택하세요. "
                        "강의평가 기간에 수강 중인 과목을 확인하고 과목별 설문을 제출합니다."),
        }
        rpc_name = "match_pknu_main_documents"
        empty = (SimpleNamespace(data=[]), 1, None)
        with patch("app.retrieval._fetch_rpc_responses", return_value={rpc_name: empty, f"{rpc_name}_lexical": empty}), \
                patch("app.retrieval._fetch_notice_chunks", return_value=([guide], [chunk])) as fetch_chunks:
            result = search(client, rpc_names=[rpc_name], embedding=[1.0], top_k=2, first_stage_k=5,
                            min_similarity=.3, query_text="부경대학교 포털 앱에서 강의평가 메뉴 위치",
                            title_anchor=True)
        client.table.assert_called_once_with("pknu_main_sources")
        lookup.in_.assert_called_once_with("title", ["강의평가"])
        fetch_chunks.assert_called_once_with(client, "pknu_main_sources", "pknu_main_chunks", ["guide"])
        self.assertEqual([row["url"] for row in result], [guide["url"]])
        self.assertIn("종합정보서비스 → 강의평가", result[0]["content"])

    def test_named_faculty_relative_course_rule_lookup_does_not_require_a_search_candidate(self):
        lookup = Mock()
        for method in ("select", "eq", "ilike", "order", "limit"):
            getattr(lookup, method).return_value = lookup
        lookup.execute.return_value = SimpleNamespace(data=[{"id": "rule"}])
        client = SimpleNamespace(table=Mock(return_value=lookup))
        rule = {"id": "rule", "title": "교수-자녀 간 강의수강 및 성적평가 공정성 제고 관리 지침",
                "url": "https://www.pknu.ac.kr/rule/bylawsAndGuidelineView.do?bbsId=2&no=702360"}
        chunk = {"id": "rule-chunk", "source_id": "rule", "chunk_index": 1, "embedding": [1.0],
                 "content": "본문: 제3조(수강신청 지도 등) 소속 학부장은 필수 교과목인 경우에도 타 분반을 선택하도록 지도한다."}
        question = "교수·자녀 수강 공정성 지침에서 부모 강의가 필수과목일 때 지도 원칙은?"
        with patch("app.retrieval._fetch_notice_chunks", return_value=([rule], [chunk])) as fetch_chunks:
            rows = _focused_document_chunks(client, ["match_rule_documents"], [1.0], [], question, [], .3, .15, .1, {})
        client.table.assert_called_once_with("rule_sources")
        lookup.ilike.assert_called_once_with("title", "%교수%자녀%강의%수강%성적%평가%공정성%")
        fetch_chunks.assert_called_once_with(client, "rule_sources", "rule_chunks", ["rule"])
        self.assertEqual([row["id"] for row in rows], ["rule-chunk"])

    def test_reviewed_fact_selection_keeps_the_original_scope(self):
        self.assertEqual(len(source_rows("교수 자녀가 부모 강의를 듣는데 필수과목이면 다른 분반으로 가나요?")), 1)
        self.assertEqual(source_rows("교수 자녀가 부모 강의를 듣는데 필수과목 분반이 없으면 사전 신고하나요?"), [])
        self.assertEqual(len(source_rows("간호학과 김정수 교수 상세 프로필의 주연구분야")), 1)
        self.assertEqual(source_rows("간호학과 김정수 교수진 목록에서 연구분야를 확인해줘"), [])
        self.assertEqual(len(source_rows("2026학년도 2학기 학부 이학계열 신입생 정규 등록금")), 1)
        self.assertEqual(source_rows("2026학년도 2학기 학부 이학계열 재학생 등록금 분납"), [])
        self.assertEqual(source_rows("2026학년도 1학기 학부 이학계열 신입생 정규 등록금"), [])

    def test_department_scope_does_not_filter_out_common_rules(self):
        client = FakeClient()
        _fetch_rpc_responses(client, ["rag", "rules"], {"metadata_filter": {}},
                             {"rag": 30, "rules": 30}, "credits",
                             metadata_filters={"rag": {"ingestion_dataset": "ce"}})
        self.assertEqual(client.calls["rag"]["metadata_filter"], {"ingestion_dataset": "ce"})
        self.assertEqual(client.calls["rag_lexical"]["metadata_filter"], {"ingestion_dataset": "ce"})
        self.assertEqual(client.calls["rules"]["metadata_filter"], {})
        self.assertNotIn("metadata_filter", client.calls["rules_lexical"])

    def test_scoped_lexical_search_receives_the_same_dataset_filter_as_vector_search(self):
        client = FakeClient()
        filters = {"ingestion_dataset": "ce"}
        _fetch_rpc_responses(client, ["a"], {"metadata_filter": filters}, {"a": 30}, "LMS")
        self.assertEqual(client.calls["a_lexical"]["metadata_filter"], filters)
        self.assertEqual(client.calls["a"]["metadata_filter"], filters)

    def test_vector_and_lexical_calls_get_their_own_payloads(self):
        client = FakeClient()
        result = _fetch_rpc_responses(
            client, ["a", "b"], {"query_embedding": "[1]", "min_similarity": 0.3},
            {"a": 30, "b": 80}, "학점",
        )
        self.assertEqual({"a", "a_lexical", "b", "b_lexical"}, set(result))
        self.assertEqual(30, client.calls["a"]["match_count"])
        self.assertEqual(80, client.calls["b"]["match_count"])
        self.assertEqual({"query_text": "학점", "match_count": 80}, client.calls["b_lexical"])
        response, elapsed_ms, error = result["a"]
        self.assertEqual([{"id": "a"}], response.data)
        self.assertIsNone(error)
        self.assertGreaterEqual(elapsed_ms, 0)

    def test_no_lexical_calls_without_lexical_query(self):
        result = _fetch_rpc_responses(FakeClient(), ["a"], {}, {"a": 30}, None)
        self.assertEqual({"a"}, set(result))

    def test_failure_is_returned_not_raised(self):
        result = _fetch_rpc_responses(FakeClient(fail={"b_lexical"}), ["a", "b"], {}, {"a": 30, "b": 30}, "q")
        self.assertIsInstance(result["b_lexical"][2], RuntimeError)
        self.assertIsNone(result["b_lexical"][0])
        self.assertIsNone(result["a"][2])

    def test_calls_run_concurrently_when_allowed(self):
        client = FakeClient(delay=0.3)
        started = time.perf_counter()
        _fetch_rpc_responses(client, ["a", "b", "c", "d"], {}, {k: 30 for k in "abcd"}, "q", max_workers=8)
        # 8 calls x 0.3s would take 2.4s sequentially.
        self.assertLess(time.perf_counter() - started, 1.2)

    def test_concurrent_calls_use_one_client_per_worker_thread(self):
        built = []
        threads_by_client = {}
        lock = threading.Lock()

        class RealishClient(FakeClient):
            supabase_url = "https://example.supabase.co"
            supabase_key = "key"

            def rpc(self, name, params):
                with lock:
                    threads_by_client.setdefault(id(self), set()).add(threading.get_ident())
                return super().rpc(name, params)

        def fake_build(url, key):
            client = RealishClient(delay=0.05)
            built.append(client)
            return client

        shared = RealishClient()
        with patch("app.retrieval.build_supabase_client", side_effect=fake_build):
            for _ in range(3):
                result = _fetch_rpc_responses(
                    shared, ["a", "b", "c", "d"], {}, {k: 30 for k in "abcd"}, "q", max_workers=3,
                )
                self.assertTrue(all(error is None for _r, _ms, error in result.values()))
        self.assertEqual({}, shared.calls)  # the shared client is never used across threads
        self.assertLessEqual(len(built), 3)  # reused across queries, not rebuilt per call
        self.assertTrue(all(len(threads) == 1 for threads in threads_by_client.values()))

    def test_default_is_sequential_in_original_order(self):
        client = FakeClient()
        order = []
        original = client.rpc
        client.rpc = lambda name, params: (order.append(name), original(name, params))[1]
        _fetch_rpc_responses(client, ["a", "b"], {}, {"a": 30, "b": 30}, "q")
        self.assertEqual(["a", "a_lexical", "b", "b_lexical"], order)


if __name__ == "__main__":
    unittest.main()
