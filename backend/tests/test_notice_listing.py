from datetime import datetime
from types import SimpleNamespace

from app.notice_listing import application_period, list_notices
from app.query_plan import KST, finish_plan, rule_plan


class Query:
    def __init__(self, rows):
        self.rows = list(rows)
    def select(self, *args): return self
    def eq(self, key, value):
        self.rows = [r for r in self.rows if r.get(key) == value]
        return self
    def ilike(self, key, value):
        self.rows = [r for r in self.rows if all(part in r[key] for part in value.strip("%").split("%"))]
        return self
    def filter(self, key, operator, value):
        assert (key, operator, value) == ("metadata->>published_at", "not.is", "null")
        self.rows = [r for r in self.rows if r.get("metadata", {}).get("published_at") is not None]
        return self
    def order(self, key, desc=False, **kwargs):
        field = lambda r: (r.get("metadata", {}).get(key.split("->>")[-1]) or "") if "->>" in key else r.get(key, "")
        self.rows.sort(key=field, reverse=desc)
        return self
    def range(self, start, end):
        self.rows = self.rows[start:end + 1]
        return self
    def limit(self, count):
        self.rows = self.rows[:count]
        return self
    def execute(self): return SimpleNamespace(data=self.rows)


class DB:
    def __init__(self, sources, chunks=()): self.sources, self.chunks = sources, list(chunks)
    def table(self, name): return Query(self.sources if name.endswith("sources") else self.chunks)


def notice(identity, day, **metadata):
    return {"id": str(identity), "status": "active", "title": "장학금 신청 " + str(identity),
            "department_id": metadata.get("department_id"), "url": "https://example.com/notice/" + str(identity),
            "metadata": {"published_at": "2026-11-" + day + "T00:00:00+09:00", **metadata}}


def task(q):
    result = finish_plan(rule_plan(q), q, [q]).tasks[0]
    result.conditions.as_of = datetime(2026, 11, 7, 12, tzinfo=KST)
    return result


def test_latest_uses_published_date_dedupes_notices_and_filters_department():
    sources = [notice(1, "01", department_id="ce"), notice(2, "06", department_id="ce"),
               notice(3, "07", department_id="humanbio"), notice(4, "05", department_id="ce"), notice(5, "30", department_id="ce")]
    sources.append({**sources[1], "id": "duplicate attachment"})
    result = list_notices(DB(sources), task("컴공 최신 공지 3개"))
    assert [r["title"] for r in result.rows] == ["장학금 신청 2", "장학금 신청 4", "장학금 신청 1"]
    assert "게시일 2026-11-06 [1]" in result.answer


def test_undated_attachments_cannot_starve_the_latest_notice_query():
    sources = [{**notice(i, "01", department_id="ce"), "metadata": {"department_id": "ce", "is_notice": False}} for i in range(600)]
    sources.append(notice("dated", "06", department_id="ce"))
    result = list_notices(DB(sources), task("컴공 최신 공지 3개"))
    assert len(result.rows) == 1 and result.rows[0]["id"] == "dated"


def test_non_pinned_board_posts_are_not_excluded_from_latest():
    source = notice(1, "06", department_id="ce", source_kind="post", is_notice=False)
    assert len(list_notices(DB([source]), task("컴공 최신 공지 3개")).rows) == 1


def test_on_campus_scholarship_query_does_not_substitute_external_scholarship():
    source = notice(1, "01", application_start="2026-11-01", application_end="2026-11-08")
    source["title"] = "푸른등대 기부장학금"
    assert not list_notices(DB([source]), task("오늘 신청 가능한 교내 장학금 목록 알려줘")).rows


def test_open_application_requires_both_dates_and_actual_current_interval():
    sources = [notice(1, "01", application_start="2026-11-01", application_end="2026-11-08"),
               notice(2, "02", application_start="2026-10-01", application_end="2026-11-06"),
               notice(3, "03"), notice(4, "04", application_start="2026-11-08", application_end="2026-11-30")]
    result = list_notices(DB(sources), task("오늘 신청 가능한 장학금 목록 알려줘"))
    assert [r["id"] for r in result.rows] == ["1"]
    assert "개인별 지원 자격" in result.answer and "확인하지 못한" in result.answer


def test_explicit_time_deadline_and_date_only_end_are_distinct():
    source = notice(1, "01", application_start="2026-11-01", application_end="2026-11-07T11:00:00+09:00")
    assert not list_notices(DB([source]), task("현재 신청 가능한 장학금 목록 알려줘")).rows
    source["metadata"]["application_end"] = "2026-11-07"
    assert len(list_notices(DB([source]), task("현재 신청 가능한 장학금 목록 알려줘")).rows) == 1


def test_period_labels_distinguish_application_from_document_submission():
    text = "신청기간: 2026. 11. 1.(일) 09:00 ~ 2026. 11. 7.(토) 18:00\n서류제출기간: 2026. 11. 1. ~ 2026. 11. 9."
    start, end = application_period(notice(1, "01"), text)
    assert (start.day, start.hour, end.day, end.hour) == (1, 9, 7, 18)
    assert application_period(notice(1, "01"), "게시일 2026-11-01 대상학기 2026-2학기") is None


def test_conflicting_periods_are_not_silently_selected():
    text = "신청기간: 2026. 11. 1. ~ 2026. 11. 8.\n접수기간: 2026. 11. 2. ~ 2026. 11. 9."
    assert application_period(notice(1, "01"), text) is None


def test_missing_period_returns_unknown_not_no_programs_exist():
    result = list_notices(DB([notice(1, "01")]), task("지금 모집 중인 장학금 있나요?"))
    assert not result.rows and "없다는 뜻은 아닙니다" in result.answer


def test_open_uses_document_chunks_when_structured_dates_are_absent():
    source = notice(1, "01")
    chunks = [{"source_id": "1", "chunk_index": 0, "content": "신청기간: 2026. 11. 1. ~ 2026. 11. 10."}]
    result = list_notices(DB([source], chunks), task("현재 신청 가능한 장학금 목록 알려줘"))
    assert len(result.rows) == 1 and "2026-11-10" in result.rows[0]["content"]
