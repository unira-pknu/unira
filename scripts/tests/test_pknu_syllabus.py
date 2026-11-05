import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<Root xmlns="http://www.nexacroplatform.com/platform/dataset">
  <Parameters><Parameter id="ErrorCode" type="string">0</Parameter></Parameters>
  <Dataset id="output1"><ColumnInfo /><Rows>
    <Row><Col id="YY">2026</Col><Col id="DEPT_NM">컴퓨터공학전공</Col><Col id="UNIV_GRSC_CLSF">학부</Col>
      <Col id="SBJT_KOR_NM">자료구조</Col><Col id="COURSE_NO">104605</Col><Col id="DCLSS_NO">106</Col>
      <Col id="STAFF_NM">김영봉</Col><Col id="PNT_THEO_PRAC">3-2-2</Col><Col id="SBJT_FG_NM">전공공통</Col>
      <Col id="LSN_MTHD_FG">대면</Col><Col id="KOR_YN">Y</Col></Row>
    <Row><Col id="YY">2026</Col><Col id="DEPT_NM">컴퓨터공학전공</Col><Col id="UNIV_GRSC_CLSF">학부</Col>
      <Col id="SBJT_KOR_NM">자료구조</Col><Col id="COURSE_NO">104605</Col><Col id="DCLSS_NO">105</Col>
      <Col id="STAFF_NM">권오흠</Col><Col id="PNT_THEO_PRAC">3-2-2</Col><Col id="SBJT_FG_NM">전공공통</Col>
      <Col id="LSN_MTHD_FG">대면</Col><Col id="KOR_YN">Y</Col></Row>
    <Row><Col id="YY">2026</Col><Col id="DEPT_NM">기계공학전공</Col><Col id="UNIV_GRSC_CLSF">학부</Col>
      <Col id="SBJT_KOR_NM">열역학</Col><Col id="COURSE_NO">100001</Col><Col id="DCLSS_NO">101</Col>
      <Col id="STAFF_NM">홍길동</Col><Col id="PNT_THEO_PRAC">3-3-0</Col></Row>
    <Row><Col id="YY">2026</Col><Col id="UNIV_GRSC_CLSF">학부</Col>
      <Col id="SBJT_KOR_NM">창의적문제해결전략</Col><Col id="COURSE_NO">K00003</Col><Col id="DCLSS_NO">901</Col></Row>
    <Row><Col id="YY">2026</Col><Col id="DEPT_NM">컴퓨터공학과</Col><Col id="UNIV_GRSC_CLSF">일반대학원</Col>
      <Col id="SBJT_KOR_NM">고급알고리즘</Col><Col id="COURSE_NO">900001</Col><Col id="DCLSS_NO">101</Col></Row>
  </Rows></Dataset>
</Root>"""


class SyllabusDownloadTests(unittest.TestCase):
    """Catalog scope, durable originals and restart behavior of HTTP downloads."""

    def task(self):
        from scripts.crawlers.syllabus.catalog import tasks_from_rows

        rows = [{
            "YY": "2026", "SHTM_CD": "U0003001", "UNIV_GRSC_CLSF": "학부",
            "COURSE_NO": "104605", "DCLSS_NO": "101", "SBJT_KOR_NM": "Data Structures",
            "STAFF_NO": "A1", "DEPT_NM": "컴퓨터공학전공", "ENG_YN": "Y",
        }]
        return tasks_from_rows(2026, "1", rows, ["eng"])[0][0]

    def make_pdf(self, path, text="Course Syllabus\n2026 first semester\nCourse Number\n104605\nClass Number\n101"):
        import fitz

        with fitz.open() as document:
            document.new_page().insert_text((50, 50), text)
            document.save(path)

    def test_catalog_preserves_graduate_and_all_report_variants(self):
        from scripts.crawlers.syllabus.catalog import read_sections, tasks_from_rows

        rows = read_sections(RESPONSE)
        rows[-1].update({"KOR_YN": "Y", "ENG_YN": "Y", "TECH_YN": "Y"})
        tasks, missing = tasks_from_rows(2026, "2", rows, ["kor", "eng", "tech"])
        graduate = [task for task in tasks if task["level"] == "일반대학원"]
        self.assertEqual({"kor", "eng", "tech"}, {task["variant"] for task in graduate})
        self.assertTrue(missing)
        self.assertEqual(len(tasks), len({task["id"] for task in tasks}))

    def test_catalog_rejects_error_or_missing_dataset_but_accepts_empty(self):
        from scripts.crawlers.syllabus.catalog import read_sections

        for xml in (
            '<Root><Parameter id="ErrorCode">-1</Parameter><Dataset id="output1" /></Root>',
            '<Root><Parameter id="ErrorCode">0</Parameter></Root>',
        ):
            with self.subTest(xml=xml), self.assertRaises(ValueError):
                read_sections(xml)
        self.assertEqual([], read_sections('<Root><Parameter id="ErrorCode">0</Parameter><Dataset id="output1" /></Root>'))
        xml = '<Root><Parameter id="ErrorCode">0</Parameter><Dataset id="output1"><Rows><Row><Col id="A"><![CDATA[C&C++]]></Col></Row></Rows></Dataset></Root>'
        self.assertEqual([{"A": "C&C++"}], read_sections(xml))

    def test_seasonal_term_and_three_calendar_years(self):
        from scripts.crawlers.syllabus.catalog import recent_years, request_body

        self.assertEqual([2024, 2025, 2026], recent_years(2026))
        self.assertIn(b'<Parameter id="SHTM_CD">U0003003</Parameter>', request_body(2025, "summer"))
        self.assertIn(b'<Parameter id="SHTM_CD">U0003004</Parameter>', request_body(2025, "winter"))

    def test_catalog_request_escapes_search_inputs_and_uses_term_code(self):
        import xml.etree.ElementTree as ET
        from scripts.crawlers.syllabus.catalog import build_request, read_sections

        body = build_request(2026, "2", course_name="C&C++", staff_name="A < B")
        self.assertIn('<Parameter id="SHTM_CD">U0003002</Parameter>', body)
        self.assertIn("C&amp;C++", body)
        self.assertIn("A &lt; B", body)
        ET.fromstring(body)
        self.assertEqual(["106", "105", "101", "901", "101"], [row["DCLSS_NO"] for row in read_sections(RESPONSE)])

    def test_different_terms_can_run_together_but_overlapping_runs_cannot(self):
        from contextlib import ExitStack
        from scripts.crawlers.syllabus.storage import collection_run

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            with ExitStack() as stack:
                for term in ("1", "summer", "2", "winter"):
                    stack.enter_context(collection_run(output, year=2026, term=term))
                with self.assertRaises(RuntimeError), collection_run(output, year=2026, term="1"):
                    pass
                with self.assertRaises(RuntimeError), collection_run(output):
                    pass
            with collection_run(output):
                with self.assertRaises(RuntimeError), collection_run(output, year=2026, term="2"):
                    pass
            self.assertEqual([], list((output / "_state").glob("*.lock")))

    def test_parallel_progress_merges_receipts_and_preserves_other_term_errors(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from scripts.crawlers.syllabus.storage import commit_progress, load_state

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "downloads.json"
            barrier = Barrier(4)
            def commit(term):
                barrier.wait(timeout=10)
                entry = {"status": "downloaded", "task": {"id": term, "year": 2026, "term": term}}
                commit_progress(path, entry=entry)
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(commit, term) for term in ("1", "summer", "2", "winter")]
                for future in futures:
                    future.result(timeout=30)
            self.assertEqual({"1", "summer", "2", "winter"}, set(load_state(path)["downloads"]))
            error = {"scope": "2026-1", "status": "failed", "task": None}
            commit_progress(path, error=error)
            commit_progress(path, entry={"status": "downloaded", "task": {"id": "2", "year": 2026, "term": "2"}})
            state = load_state(path)
            self.assertEqual(error, state["errors"]["2026-1"])
            self.assertEqual(error, state["last_error"])

    def test_atomic_json_retries_transient_windows_replace_denial(self):
        from scripts.crawlers.syllabus.storage import atomic_json

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "downloads.json"
            atomic_json(path, {"original": True})
            original_replace = Path.replace
            denied = PermissionError("temporary Windows sharing conflict")
            denied.winerror = 5
            attempts = []
            def replace(source, target):
                attempts.append(source)
                if len(attempts) <= 2:
                    self.assertEqual({"original": True}, json.loads(path.read_text(encoding="utf-8")))
                    raise denied
                return original_replace(source, target)
            with patch.object(Path, "replace", replace), patch("scripts.crawlers.syllabus.storage.time.sleep") as sleep:
                atomic_json(path, {"saved": True})
            self.assertEqual({"saved": True}, json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(2, sleep.call_count)
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_atomic_json_persistent_denial_preserves_original_and_pending_receipt(self):
        from scripts.crawlers.syllabus.storage import atomic_json

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "downloads.json"
            atomic_json(path, {"original": True})
            denied = PermissionError("persistent Windows denial")
            denied.winerror = 5
            with (
                patch.object(Path, "replace", side_effect=denied),
                patch("scripts.crawlers.syllabus.storage.time.monotonic", side_effect=[0, 10]),
                self.assertRaises(PermissionError),
            ):
                atomic_json(path, {"pending": True})
            self.assertEqual({"original": True}, json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual({"pending": True}, json.loads(path.with_name(path.name + ".tmp").read_text(encoding="utf-8")))

    def test_pdf_identity_rejects_wrong_course_year_term_and_section(self):
        from scripts.crawlers.syllabus.documents import inspect_pdf

        task = self.task()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, text in enumerate((
                "Course Syllabus\n2026 first semester\nCourse Number\n999999\nClass Number\n101",
                "Course Syllabus\n2025 first semester\nCourse Number\n104605\nClass Number\n101",
                "Course Syllabus\n2026 second semester\nCourse Number\n104605\nClass Number\n101",
                "Course Syllabus\n2026 first semester\nCourse Number\n104605\nClass Number\n102",
            )):
                path = root / f"wrong{index}.pdf"
                self.make_pdf(path, text)
                with self.subTest(text=text), self.assertRaises(ValueError):
                    inspect_pdf(path.read_bytes(), task)
            valid = root / "valid.pdf"
            self.make_pdf(valid)
            inspect_pdf(valid.read_bytes(), task)

    def test_save_preserves_source_refuses_overwrite_and_detects_tampering(self):
        from scripts.crawlers.syllabus.storage import is_complete, save_pdf, target_path

        task = self.task()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "download.pdf", root / "output"
            self.make_pdf(source)
            entry = save_pdf(source.read_bytes(), task, output)
            state = {"downloads": {task["id"]: entry}}
            self.assertTrue(source.exists())
            self.assertTrue(is_complete(output, task, state))
            with self.assertRaises(FileExistsError):
                save_pdf(source.read_bytes(), task, output)
            target_path(output, task).write_bytes(b"corrupted")
            with self.assertRaises(ValueError):
                is_complete(output, task, state)

    def test_catalog_and_destination_reject_paths_outside_output(self):
        from scripts.crawlers.syllabus.catalog import load_catalog, task_id
        from scripts.crawlers.syllabus.storage import atomic_json, target_path

        task = self.task()
        task["year"] = "../../outside"
        task["id"] = task_id(task)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "catalog.json"
            atomic_json(manifest, {"version": 1, "tasks": [task]})
            with self.assertRaises(ValueError):
                load_catalog(manifest)
            with self.assertRaises(ValueError):
                target_path(root / "output", task)

    def crawl_args(self, output, **overrides):
        from argparse import Namespace

        values = dict(
            year=2026, term="1", variants=["kor", "eng", "tech"], output=output,
            manifest=None, refresh_catalog=False,
        )
        values.update(overrides)
        return Namespace(**values)

    def crawl_catalog(self):
        return {
            "version": 1, "years": [2026], "terms": ["1"], "variants": ["kor", "eng", "tech"],
            "tasks": [self.task()], "unpublished": [], "batches": [],
        }

    def test_crawl_prepares_catalog_once_and_preserves_it_on_restart(self):
        from scripts.crawlers.syllabus.__main__ import prepare_crawl
        from scripts.crawlers.syllabus.storage import atomic_json

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            with patch("scripts.crawlers.syllabus.__main__.collect_catalog", return_value=self.crawl_catalog()) as collect:
                first = self.crawl_args(output)
                prepare_crawl(first)
                before = first.manifest.read_bytes()
                second = self.crawl_args(output)
                prepare_crawl(second)
                collect.assert_called_once_with([2026], ["1"], ["kor", "eng", "tech"])
                self.assertEqual(before, second.manifest.read_bytes())

    def test_crawl_rejects_mismatched_cached_scope_without_replacing_it(self):
        from scripts.crawlers.syllabus.__main__ import prepare_crawl
        from scripts.crawlers.syllabus.storage import atomic_json

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest = output / "_state" / "catalog-2026-1.json"
            cached = self.crawl_catalog()
            cached["terms"] = ["2"]
            atomic_json(manifest, cached)
            original = manifest.read_bytes()
            with patch("scripts.crawlers.syllabus.__main__.collect_catalog") as collect:
                with self.assertRaisesRegex(ValueError, "범위"):
                    prepare_crawl(self.crawl_args(output))
                collect.assert_not_called()
            self.assertEqual(original, manifest.read_bytes())

    def test_crawl_refresh_failure_keeps_cached_catalog_and_progress(self):
        from scripts.crawlers.syllabus.__main__ import prepare_crawl
        from scripts.crawlers.syllabus.storage import atomic_json

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest = output / "_state" / "catalog-2026-1.json"
            progress = output / "_state" / "downloads.json"
            atomic_json(manifest, self.crawl_catalog())
            atomic_json(progress, {"version": 1, "downloads": {}})
            original = {path: path.read_bytes() for path in (manifest, progress)}
            with patch("scripts.crawlers.syllabus.__main__.collect_catalog", side_effect=ConnectionError("offline")):
                with self.assertRaises(ConnectionError):
                    prepare_crawl(self.crawl_args(output, refresh_catalog=True))
            self.assertEqual(original, {path: path.read_bytes() for path in original})
            self.assertFalse(manifest.with_suffix(".lock").exists())

    def test_completed_crawl_exits_without_network(self):
        from scripts.crawlers.syllabus.__main__ import main
        from scripts.crawlers.syllabus.storage import atomic_json, save_pdf

        with tempfile.TemporaryDirectory(dir=ROOT / "files") as tmp:
            output = Path(tmp)
            catalog = self.crawl_catalog()
            source = output / "download.pdf"
            self.make_pdf(source)
            entry = save_pdf(source.read_bytes(), catalog["tasks"][0], output)
            atomic_json(output / "_state" / "catalog-2026-1.json", catalog)
            atomic_json(output / "_state" / "downloads.json", {
                "version": 1, "downloads": {catalog["tasks"][0]["id"]: entry},
            })
            with (
                patch("scripts.crawlers.syllabus.__main__.collect_catalog") as collect,
                patch("scripts.crawlers.syllabus.__main__.ReportClient") as client,
            ):
                result = main(["crawl", "--year", "2026", "--term", "1", "--output", str(output)])
                self.assertEqual(0, result)
                collect.assert_not_called()
                client.assert_not_called()


class SyllabusHttpTests(unittest.TestCase):
    # Captured from the site's anonymous embed response: a static report path,
    # without cookies, session IDs, personal metadata or generated PDF tokens.
    ENCODED_PATH = (
        "%96gwG%87%b2%a3%96%e2%c3%17%13%f3G%872r%02%c7%93t%b7%82B%82%03%04%e42%022T5%82%f2s"
        "%a4%85%e2c%c3%f2%05%e5CD%e3%a3eF%a3%83T%c4%a4%d5%b6%14%b4%d44%d4F%27%84%f2%85S"
        "%a4e%b4c%85%25%06%d3%06%d5G2u%06f%95"
    )

    def task(self):
        from scripts.crawlers.syllabus.catalog import task_id

        task = {
            "year": 2026, "term": "2", "term_code": "U0003002", "variant": "eng",
            "course_no": "104605", "section": "101", "staff_no": "123456", "staff_name": "Example",
            "course_name": "자료구조", "level": "학부", "department": "전자공학전공",
        }
        task["id"] = task_id(task)
        return task

    def embed(self):
        return (
            "var viewer=new m2soft.crownix.Viewer('https://reporting.pknu.ac.kr/ReportingServer/service','viewer');"
            "viewer.setParameterEncrypt('3');"
            f"viewer.openFile('{self.ENCODED_PATH}','{self.ENCODED_PATH}',option);"
        )

    def pdf_bytes(self, text=None):
        import fitz

        if text is None:
            text = "Course Syllabus\n2nd Semester, 2026\nCourse Number\n104605\nClass Number\n101"
        with fitz.open() as document:
            document.new_page().insert_text((50, 50), text)
            return document.tobytes()

    def response(self, body, status=200, headers=None):
        import requests

        response = requests.Response()
        response.status_code = status
        response.url = "https://reporting.pknu.ac.kr/ReportingServer/service"
        response._content = body.encode("utf-8") if isinstance(body, str) else body
        response._content_consumed = True
        response.encoding = "utf-8"
        response.headers.update(headers or {})
        return response

    def test_public_mode3_vector_and_double_encoded_form(self):
        from urllib.parse import unquote
        from scripts.crawlers.syllabus.protocol import decode_mode3, report_form

        self.assertEqual(
            "https://irumi.pknu.ac.kr/mrd/uni/cour/open/LtPdocMngtPrn.mrd",
            decode_mode3(self.ENCODED_PATH),
        )
        form = report_form(self.task())
        payload = json.loads(form["report"])
        params = json.loads(unquote(unquote(payload["reportParam"])))
        self.assertEqual("('104605','101','123456','U0253002')", params["IN_PARAM"])
        self.assertEqual({}, params["_CLIENT_USER_INFO"])
        self.assertNotIn("sessionId", payload)
        self.assertEqual("", form["key"])

    def test_embed_rejects_other_hosts_modes_and_report_templates(self):
        from scripts.crawlers.syllabus.protocol import ProtocolError, parse_embed

        task = self.task()
        self.assertIn("LtPdocMngtPrn.mrd", parse_embed(self.embed(), task).path)
        for html in (
            self.embed().replace("reporting.pknu.ac.kr", "example.com"),
            self.embed().replace("setParameterEncrypt('3')", "setParameterEncrypt('4')"),
            "<html>Login required</html>",
        ):
            with self.subTest(html=html[:80]), self.assertRaises(ProtocolError):
                parse_embed(html, task)
        with self.assertRaises(ProtocolError):
            parse_embed(self.embed(), {**task, "variant": "tech"})

    def test_export_rejects_error_bodies_and_external_or_traversing_filenames(self):
        from scripts.crawlers.syllabus.protocol import ProtocolError, export_filename, report_form

        self.assertEqual("2026/10/29/sample.pdf", export_filename("1|2026/10/29/sample.pdf"))
        for body in ("0|export failed", "<html>error</html>", "1|../other.pdf", "1|https://example.com/other.pdf"):
            with self.subTest(body=body), self.assertRaises(ProtocolError):
                export_filename(body)
        with self.assertRaises(ProtocolError):
            report_form({**self.task(), "staff_no": "' OR 1=1 --"})

    def test_http_pipeline_uses_export_receipt_then_validates_download_signature(self):
        from scripts.crawlers.syllabus.client import ReportClient
        from scripts.crawlers.syllabus.protocol import EMBED_URL, REPORT_BASE

        pdf = self.pdf_bytes()
        with ReportClient() as client:
            responses = [
                self.response(self.embed()),
                self.response("1|2026/10/29/sample.pdf", headers={"crownix-rdid": "sample-session"}),
                self.response(pdf),
            ]
            with patch.object(client.session, "request", side_effect=responses) as request:
                self.assertEqual(pdf, client.pdf(self.task()))
                calls = request.call_args_list
                self.assertEqual(EMBED_URL, calls[0].args[1])
                self.assertEqual("500", calls[1].kwargs["data"]["opcode"])
                self.assertEqual(REPORT_BASE + "/download", calls[2].args[1])
                self.assertEqual("sample-session", calls[2].kwargs["params"]["rdid"])
                self.assertTrue(client.session.verify)
                self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in calls))

    def test_http_access_denial_stops_before_export(self):
        from scripts.crawlers.syllabus.client import AccessDenied, ReportClient

        with ReportClient() as client:
            with patch.object(client.session, "request", return_value=self.response("denied", 403)) as request:
                with self.assertRaises(AccessDenied):
                    client.pdf(self.task())
                self.assertEqual(1, request.call_count)
                self.assertEqual("embed", client.stage)
                self.assertEqual(403, client.http_status)

    def test_html_download_is_not_a_successful_pdf(self):
        from scripts.crawlers.syllabus.client import ReportClient
        from scripts.crawlers.syllabus.protocol import ProtocolError

        with ReportClient() as client:
            responses = [
                self.response(self.embed()),
                self.response("1|sample.pdf", headers={"crownix-rdid": "sample-session"}),
                self.response("<html>server error</html>"),
            ]
            with patch.object(client.session, "request", side_effect=responses), self.assertRaises(ProtocolError):
                client.pdf(self.task())

    def test_english_class_number_and_wrong_pdf_identities(self):
        from scripts.crawlers.syllabus.documents import inspect_pdf
        from scripts.crawlers.syllabus.protocol import ProtocolError

        result = inspect_pdf(self.pdf_bytes(), self.task())
        self.assertEqual(1, result["pages"])
        for key, value in (("course_no", "999999"), ("section", "102"), ("year", 2025), ("term", "1"), ("variant", "kor")):
            with self.subTest(field=key), self.assertRaises(ProtocolError):
                inspect_pdf(self.pdf_bytes(), {**self.task(), key: value})

    def test_saved_pdf_is_atomic_preserved_and_not_overwritten(self):
        from scripts.crawlers.syllabus.storage import save_pdf
        from scripts.crawlers.syllabus.storage import is_complete

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            task, data = self.task(), self.pdf_bytes()
            entry = save_pdf(data, task, output)
            self.assertEqual(data, (output / entry["path"]).read_bytes())
            self.assertTrue(is_complete(output, task, {"downloads": {task["id"]: entry}}))
            with self.assertRaises(FileExistsError):
                save_pdf(data, task, output)
            self.assertEqual(data, (output / entry["path"]).read_bytes())

    def test_crawl_limit_then_unlimited_resume_preserves_receipts(self):
        from scripts.crawlers.syllabus.__main__ import main
        from scripts.crawlers.syllabus.catalog import task_id
        from scripts.crawlers.syllabus.storage import atomic_json, load_state

        tasks = []
        for section in ("101", "102", "103"):
            task = {**self.task(), "section": section}
            task["id"] = task_id(task)
            tasks.append(task)
        catalog = {
            "version": 1, "years": [2026], "terms": ["2"], "variants": ["eng"],
            "tasks": tasks, "unpublished": [], "batches": [],
        }
        with tempfile.TemporaryDirectory(dir=ROOT / "files") as tmp:
            output = Path(tmp)
            atomic_json(output / "_state" / "catalog-2026-2.json", catalog)
            command = ["crawl", "--year", "2026", "--term", "2", "--variants", "eng", "--output", str(output)]
            with (
                patch("scripts.crawlers.syllabus.__main__.collect_catalog") as collect,
                patch("scripts.crawlers.syllabus.__main__.ReportClient") as factory,
                patch("scripts.crawlers.syllabus.__main__.time.sleep"),
            ):
                client = factory.return_value.__enter__.return_value
                client.pdf.side_effect = lambda task: self.pdf_bytes(
                    "Course Syllabus\n2nd Semester, 2026\nCourse Number\n104605\nClass Number\n" + task["section"]
                )
                self.assertEqual(0, main(command + ["--limit", "1"]))
                first = load_state(output / "_state" / "downloads.json")["downloads"][tasks[0]["id"]]
                self.assertEqual(0, main(command))
                state = load_state(output / "_state" / "downloads.json")
                self.assertEqual({task["id"] for task in tasks}, set(state["downloads"]))
                self.assertEqual(first, state["downloads"][tasks[0]["id"]])
                self.assertEqual(["101", "102", "103"], [call.args[0]["section"] for call in client.pdf.call_args_list])
                count = factory.call_count
                self.assertEqual(0, main(command))
                self.assertEqual(count, factory.call_count)
                collect.assert_not_called()
                self.assertEqual([], list((output / "_incoming").glob("*.pdf")))

    def test_crawl_access_denial_is_deferred_and_stops_the_batch(self):
        from scripts.crawlers.syllabus.__main__ import main
        from scripts.crawlers.syllabus.catalog import task_id
        from scripts.crawlers.syllabus.client import AccessDenied
        from scripts.crawlers.syllabus.storage import atomic_json, load_state

        first = self.task()
        second = {**first, "section": "102"}
        second["id"] = task_id(second)
        catalog = {
            "version": 1, "years": [2026], "terms": ["2"], "variants": ["eng"],
            "tasks": [first, second], "unpublished": [], "batches": [],
        }
        with tempfile.TemporaryDirectory(dir=ROOT / "files") as tmp:
            output = Path(tmp)
            atomic_json(output / "_state" / "catalog-2026-2.json", catalog)
            with patch("scripts.crawlers.syllabus.__main__.ReportClient") as factory:
                client = factory.return_value.__enter__.return_value
                client.stage, client.http_status = "embed", 403
                client.pdf.side_effect = AccessDenied("HTTP 403")
                result = main(["crawl", "--year", "2026", "--term", "2", "--variants", "eng", "--output", str(output)])
                self.assertEqual(1, result)
                client.pdf.assert_called_once_with(first)
            state = load_state(output / "_state" / "downloads.json")
            self.assertEqual({}, state["downloads"])
            self.assertEqual("deferred_access_denied", state["last_error"]["status"])
            self.assertEqual("embed", state["last_error"]["stage"])
            self.assertEqual(403, state["last_error"]["http_status"])
            self.assertFalse((output / "_state" / "run.lock").exists())


if __name__ == "__main__":
    unittest.main()
