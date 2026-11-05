from __future__ import annotations

import unittest
import json
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

from scripts.crawlers.common.main_static import (
    extract_structure, linked_file_ids, parse_flow, parse_table, parse_system_organization_chart,
)
from scripts.crawlers.main.collectors.boards import parse_board_intro_html
from scripts.crawlers.main.collectors.static_pages import (
    crawl_static_pages, extract_static_attachment_text, image_text_sections, parse_static_page_html,
)
from scripts.crawlers.main.collectors.organization import collect_organization_chart, crawl_intro_page
from scripts.crawlers.main.common.board import parse_list_page
from scripts.crawlers.main.routes import ROUTES


class MainStaticStructureTests(unittest.TestCase):
    def test_text_cards_preserve_fields_and_order_without_collecting_images(self) -> None:
        html = '''<title>Former leaders</title><div id="subCont"><div class="content_wrap">
          <div class="grid"><article><img src="/portrait-a.jpg" alt="Photo">
            <h5>Leader A</h5><p>First institution, second institution</p>
            <span class="term">1924~1930</span><span class="term">1935~1940</span></article>
          <article><img src="https://other.example/portrait-b.jpg"><h5>Leader B</h5>
            <p>Third institution</p><span class="term">1940~1945</span></article></div>
          </div></div>'''
        options = {"collect_body_images": False, "ocr_body_images": False,
                   "card_selector": ".grid > article", "card_title_selector": "h5",
                   "card_field_selectors": {"positions": "p", "terms": ".term"}}
        paths = Mock()
        paths.document_json.return_value = Path(__file__).resolve().parents[3] / "never-written-card-sample.json"
        paths.attachment_dir.return_value = Path(__file__).resolve().parents[3] / "never-written-card-assets"
        session = Mock()
        with (patch.dict(ROUTES, {114: replace(ROUTES[114], options=options, download_attachments=False)}),
              patch("scripts.crawlers.main.collectors.static_pages.PATHS", paths),
              patch("scripts.crawlers.main.collectors.static_pages.save_body_images") as download,
              patch("scripts.crawlers.main.collectors.static_pages.recognize_static_body_images") as ocr,
              patch("scripts.crawlers.main.collectors.static_pages.save_json", return_value=paths.document_json.return_value) as save):
            parsed = parse_static_page_html(html, 114)
            self.assertEqual(parsed.status, "ready")
            self.assertFalse(parsed.body_images)
            cards = parsed.structure["sections"][0]["blocks"]
            self.assertEqual([(card["order"], card["title"]) for card in cards], [(1, "Leader A"), (2, "Leader B")])
            self.assertEqual(cards[0]["fields"], {"positions": ["First institution, second institution"],
                                                "terms": ["1924~1930", "1935~1940"]})
            self.assertEqual(parsed.structure["card_count"], 2)
            self.assertNotIn("portrait", json.dumps(parsed.structure))
            self.assertIn("1935~1940\n\nLeader B", parsed.content)
            stats = crawl_static_pages(session, {"items": {}}, False, (114,), prepared={114: parsed})
            missing = parse_static_page_html(html.replace('class="term"', 'class="unknown"'), 114)
            self.assertEqual(missing.status, "needs_review")
            self.assertIn("card field is missing: terms", missing.reason)
        download.assert_not_called()
        ocr.assert_not_called()
        session.get.assert_not_called()
        session.request.assert_not_called()
        self.assertEqual(stats.failed, 0)
        document = save.call_args.args[0]
        self.assertEqual(document["card_count"], 2)
        self.assertEqual(document["image_collection"], "not_requested")
        self.assertFalse(document["images"])
        with patch.dict(ROUTES, {114: replace(ROUTES[114], options={})}):
            self.assertEqual(len(parse_static_page_html(html, 114).body_images), 2)

    def test_audio_assets_are_opt_in_and_keep_only_selected_file_types(self) -> None:
        html = '''<title>Song</title><div id="subCont"><div class="content_wrap">
          <h4>College song</h4><a download href="/upload/song.zip">All files</a>
          <ul><li><h5>Duet</h5><audio><source src="/upload/duet.mp3"></audio></li>
          <li><h5>Solo</h5><audio src="/upload/solo.mp3"></audio></li>
          <li><audio src="https://other.example/upload/external.mp3"></audio></li></ul>
          <a download href="/upload/movie.mp4">Video</a><img src="/upload/score.jpg">
        </div></div>'''
        options = {"collect_audio_files": True, "attachment_extensions": [".mp3"],
                   "allow_asset_only_body": True}
        with patch.dict(ROUTES, {114: replace(ROUTES[114], options=options)}):
            parsed = parse_static_page_html(html, 114)
        self.assertEqual(parsed.status, "ready")
        self.assertEqual([item["name"] for item in parsed.attachments], ["Duet", "Solo"])
        self.assertEqual([item["url"] for item in parsed.attachments], [
            "https://www.pknu.ac.kr/upload/duet.mp3", "https://www.pknu.ac.kr/upload/solo.mp3"])
        self.assertEqual(parsed.body_images[0]["src"], "/upload/score.jpg")
        with patch.dict(ROUTES, {114: replace(ROUTES[114], options={})}):
            default = parse_static_page_html(html, 114)
        self.assertFalse(any(item["url"].endswith(".mp3") for item in default.attachments))

    def test_audio_downloads_keep_originals_without_document_text_extraction(self) -> None:
        record = {"downloaded": True, "saved_path": "files/audio/duet.mp3"}
        image = {"downloaded": True, "saved_path": "files/images/wallpaper.png"}
        with (patch("scripts.crawlers.main.collectors.static_pages.extract_blocks") as extract,
              patch("scripts.crawlers.main.routes.COLLECTION_POLICY", {"body_image_ocr": False})):
            extract_static_attachment_text([record, image])
        extract.assert_not_called()
        self.assertEqual(record["text"]["status"], "skipped")
        self.assertEqual(record["text"]["extractor"], "audio_original")
        self.assertEqual(image["text"]["extractor"], "image_original")

    def test_pdf_link_only_scope_preserves_direct_and_embedded_download_urls(self) -> None:
        html = '''<title>History</title><div id="subCont"><div class="content_wrap">
          <p>The university publishes its history and instructions for using anniversary images.
          The original files remain available on the official university website.</p>
          <a href="/upload/history.pdf">History book</a>
          <ul><li>Setup instructions</li><li class="uploadPdf" data-id="123"></li></ul>
          </div></div>'''
        options = {"download_pdf_attachments": False,
                   "record_body_links": True, "embedded_pdf_count": 1}
        paths = Mock()
        paths.document_json.return_value = Path(__file__).resolve().parents[3] / "never-written-pdf-link-sample.json"
        with (patch.dict(ROUTES, {114: replace(ROUTES[114], options=options)}),
              patch("scripts.crawlers.main.collectors.static_pages.PATHS", paths),
              patch("scripts.crawlers.main.collectors.static_pages.resolve_static_embedded_pdfs", return_value=[
                  {"name": "Setup instructions", "url": "https://www.pknu.ac.kr/upload/setup.pdf", "media_id": "123"}]),
              patch("scripts.crawlers.main.collectors.static_pages.cms_board.save_attachments", return_value=[]) as download,
              patch("scripts.crawlers.main.collectors.static_pages.save_json", return_value=paths.document_json.return_value) as save):
            parsed = parse_static_page_html(html, 114)
            self.assertEqual(parsed.status, "ready")
            self.assertFalse(parsed.attachments)
            stats = crawl_static_pages(Mock(), {"items": {}}, False, (114,), prepared={114: parsed})
        self.assertEqual(stats.failed, 0)
        self.assertEqual(download.call_args.args[1], [])
        links = save.call_args.args[0]["reference_links"]
        self.assertEqual([link["url"] for link in links], [
            "https://www.pknu.ac.kr/upload/history.pdf", "https://www.pknu.ac.kr/upload/setup.pdf"])
        self.assertTrue(all(link["selected_for_download"] is False for link in links))

    def test_introduction_keeps_staff_and_college_links_without_visiting_homepages(self) -> None:
        url = ROUTES[531].url
        html = '''<title>College introduction</title><div id="subCont"><div class="content_wrap">
          <div class="con03_bottom_l"><ul><li>위치</li><li>전화번호</li><li>팩스번호</li></ul>
            <ul><li>A12 1407</li><li>051-1234</li><li>051-5678</li></ul>
            <a href="http://college.pknu.ac.kr">홈페이지 바로가기</a></div>
          <div class="con05_sub_wrap"><div><ul><li>관장</li><li>업무시간</li></ul>
            <ul><li>Public director</li><li>09:00–18:00</li></ul></div></div>
          <p>College mission and introduction.</p><ul><a href="http://major.pknu.ac.kr"><li>Major A</li></a></ul>
          <div class="getOrgMngList" data-id="265"></div></div></div>'''
        response = Mock(status_code=200, url=url, headers={"Content-Type": "text/html"}, text=html)
        staff = {"dept": 265, "mngSeq": 1, "belongs": "College", "position": "Manager",
                 "tel": "051-9999", "jobDesc": "Admissions\r\nStudent affairs",
                 "name": "Not displayed", "email": "hidden@example.org"}
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (patch("scripts.crawlers.main.collectors.organization.PROJECT_ROOT", root),
                  patch("scripts.crawlers.main.collectors.organization.fetch", return_value=response) as fetch,
                  patch("scripts.crawlers.main.collectors.organization._widget_rows", return_value=[staff]) as widget):
                stats, detail = crawl_intro_page(Mock(), 531)
                doc = json.loads((root / detail["output"]).read_text(encoding="utf-8"))
                self.assertEqual(detail["status"], "success")
                self.assertEqual(stats.new, 1)
                self.assertEqual(doc["contact"]["location"], "A12 1407")
                self.assertEqual(doc["contact"]["관장"], "Public director")
                self.assertEqual(doc["contact"]["업무시간"], "09:00–18:00")
                self.assertIn("관장: Public director", doc["content"])
                self.assertIn("업무시간: 09:00–18:00", doc["content"])
                self.assertIn("Major A", doc["content"])
                self.assertIn("Admissions\nStudent affairs", doc["content"])
                self.assertEqual(doc["structured_content"]["table_count"], 1)
                self.assertEqual(doc["staff_count"], 1)
                self.assertEqual([link["url"] for link in doc["reference_links"]],
                                 ["http://college.pknu.ac.kr", "http://major.pknu.ac.kr"])
                self.assertTrue(all(link["followed"] is False for link in doc["reference_links"]))
                self.assertNotIn("Not displayed", json.dumps(doc))
                self.assertNotIn("hidden@example.org", json.dumps(doc))
                self.assertTrue((root / doc["source_html"]).is_file())
                fetch.assert_called_once()
                self.assertEqual(fetch.call_args.args[1], url)
                widget.assert_called_once_with(fetch.call_args.args[0], 265, url)
                widget.return_value = [{**staff, "dept": 999}]
                _, invalid = crawl_intro_page(Mock(), 531)
                self.assertEqual(invalid["status"], "needs_review")
                self.assertIn("unexpected department", invalid["warnings"][0])

    def test_image_only_sections_require_explicit_ocr_and_keep_source_order(self) -> None:
        html = '''<title>Walking paths</title><div id="subCont"><div class="content_wrap">
          <section class="image-section" id="first"><ul class="section-nav">
            <li><a class="active" href="#first">First path</a></li><li><a href="#second">Second path</a></li></ul>
            <img src="/upload/first.png"></section>
          <section class="image-section" id="second"><ul class="section-nav">
            <li><a href="#first">First path</a></li><li><a class="active" href="#second">Second path</a></li></ul>
            <img src="/upload/second.png"></section></div></div>'''
        options = {"content_from_images": True, "image_section_selector": "section.image-section",
                   "image_section_title_selector": ".section-nav a.active",
                   "exclude_body_selectors": [".section-nav"]}
        with (patch.dict(ROUTES, {114: replace(ROUTES[114], options=options)}),
              patch("scripts.crawlers.main.routes.COLLECTION_POLICY", {"body_image_ocr": True})):
            parsed = parse_static_page_html(html, 114)
        self.assertEqual(parsed.status, "ready")
        self.assertEqual([image["section_title"] for image in parsed.body_images], ["First path", "Second path"])
        records = [{**image, "source_url": image["src"], "ocr_status": "needs_review",
                    "ocr_text": "  Clean\u200b   text  \n\n Second line "} for image in parsed.body_images]
        content, sections = image_text_sections(records)
        self.assertEqual([section["id"] for section in sections], ["first", "second"])
        self.assertEqual(sections[0]["content"], "Clean text\nSecond line")
        self.assertIn("First path\nClean text", content)
        records[0].update(sha256="original", visual_extraction={"status": "extracted",
            "source_sha256": "original", "text": "Reviewed source text"})
        self.assertEqual(image_text_sections(records)[1][0]["text_source"], "visual_extraction")
        records[0]["sha256"] = "changed"
        self.assertEqual(image_text_sections(records)[1][0]["text_source"], "image_ocr")
        with patch.dict(ROUTES, {114: replace(ROUTES[114], options={**options, "ocr_body_images": False})}):
            originals = parse_static_page_html(html, 114)
            self.assertEqual(originals.status, "ready")
            self.assertEqual(len(originals.body_images), 2)

    def test_main_policy_keeps_original_images_without_calling_ocr(self) -> None:
        html = '<title>Image guide</title><div id="subCont"><div class="content_wrap"><img src="/upload/guide.png"></div></div>'
        root = Path(__file__).resolve().parents[3]
        output = root / "never-written-ocr-policy-sample.json"
        paths = Mock()
        paths.document_json.return_value = output
        paths.attachment_dir.return_value = root / "never-written-assets"
        image = {"order": 1, "alt": "", "source_url": "https://www.pknu.ac.kr/upload/guide.png",
                 "saved_path": "files/guide.png", "sha256": "original", "status": "saved"}
        options = {"content_from_images": True, "ocr_body_images": True}
        with (patch.dict(ROUTES, {114: replace(ROUTES[114], options=options)}),
              patch("scripts.crawlers.main.routes.COLLECTION_POLICY", {"body_image_ocr": False}),
              patch("scripts.crawlers.main.collectors.static_pages.PATHS", paths),
              patch("scripts.crawlers.main.collectors.static_pages.download_static_body_images", return_value=[image]),
              patch("scripts.crawlers.main.collectors.static_pages.recognize_static_body_images") as ocr,
              patch("scripts.crawlers.main.collectors.static_pages.save_json", return_value=output) as save):
            parsed = parse_static_page_html(html, 114)
            self.assertEqual(parsed.status, "ready")
            stats = crawl_static_pages(Mock(), {"items": {}}, False, (114,), prepared={114: parsed})
        ocr.assert_not_called()
        self.assertEqual(stats.failed, 0)
        document = save.call_args.args[0]
        self.assertEqual(document["content"], parsed.content)
        self.assertEqual(document["images"][0]["ocr_status"], "not_requested")
        self.assertEqual(document["image_text_extraction"], "not_requested")
        self.assertFalse(document["ocr_review_required"])
        self.assertEqual(document["extraction_warnings"], [])

    def test_table_strikethrough_keeps_deleted_facilities_distinct_from_current_text(self) -> None:
        table = BeautifulSoup('''<table><tr><th>시설</th><th>안내</th></tr>
          <tr><td>101~106호, <strike>406호</strike></td><td><del>대관 가능</del> 대관 제외</td></tr>
          </table>''', "lxml").table
        original = str(table)
        parsed = parse_table(table)
        self.assertEqual(parsed["rows"][1][0]["text"], "101~106호, [취소선: 406호]")
        self.assertEqual(parsed["rows"][1][1]["text"], "[취소선: 대관 가능] 대관 제외")
        self.assertEqual(str(table), original)

    def test_html_comments_are_excluded_from_direct_and_nested_body_text(self) -> None:
        html = '''<div class="bdvTxt"><!-- 갤러리 게시판 -->
          <div><!-- hidden editor marker --><p>실제 답변입니다.</p>
            <div>일반 안내 <p>추가 안내입니다.</p><!-- 에디터작성내용 --></div>
            <table><caption>연락처</caption><tr><th>전화</th><td>051-123-4567</td></tr></table>
          </div><!-- 에디터작성내용 --></div>'''
        parsed = extract_structure(BeautifulSoup(html, "lxml").select_one(".bdvTxt"))
        self.assertEqual(parsed["table_count"], 1)
        for visible in ("실제 답변입니다.", "일반 안내", "추가 안내입니다.", "연락처", "051-123-4567"):
            self.assertIn(visible, parsed["content"])
        for comment in ("갤러리 게시판", "hidden editor marker", "에디터작성내용"):
            self.assertNotIn(comment, parsed["content"])

    def test_download_selector_keeps_latest_years_and_records_other_links(self) -> None:
        html = """<title>Statistics</title><div id="subCont"><div class="content_wrap">
          <h4 class="subNameH4">Student statistics</h4>
          <p>The university publishes enrollment numbers and annual statistical reports.
          Each table includes its own reference date and measurement units.</p>
          <table><tr><th>Students</th><td>1234</td></tr></table>
          <select id="reports"><option value="/">Choose a year</option>
          <option download="Report 2024.hwp" value="/upload/2024.pdf">2024</option>
          <option download="Report 2022.hwp" value="/upload/2022.hwp">2022</option>
          <option download="Report 2025.pdf" value="/upload/2025.pdf">2025</option>
          <option download="External report.pdf" value="https://other.example/upload/2026.pdf">2026</option>
          <option download="Placeholder.pdf" value="javascript:download()">2027</option>
          </select></div></div>"""
        route = replace(ROUTES[114], options={"attachment_limit": 2,
                                             "exclude_body_selectors": ["#reports"]})
        with patch.dict(ROUTES, {114: route}):
            parsed = parse_static_page_html(html, 114)
        self.assertEqual(parsed.status, "ready")
        self.assertEqual(parsed.structure["table_count"], 1)
        self.assertEqual([(item["year"], item["name"]) for item in parsed.attachments],
                         [(2025, "Report 2025.pdf"), (2024, "Report 2024.pdf")])
        self.assertEqual({link["year"]: link["selected_for_download"]
                          for link in parsed.reference_links}, {2024: True, 2022: False, 2025: True})
        self.assertNotIn("Choose a year", parsed.content)

    def test_static_curriculum_links_keep_external_urls_and_board_pdf_attachments(self) -> None:
        html = '''<title>Curriculum</title><div id="subCont"><div class="content_wrap">
          <h4>Curriculum</h4><p>This page lists departments and curriculum documents.
          Each department publishes details separately from the university website.</p>
          <ul><a href="https://department.example/program"><li>Department A</li></a>
            <a href="https://department.example/other"><li>Department B</li></a></ul>
          <a href="/upload/program.pdf">Program PDF</a>
          <a href="/boardDownload.do?no=42">Microdegree PDF</a>
          <a href="/boardDownload.do">Missing file ID</a>
          <a href="https://other.example/boardDownload.do?no=43">External file</a>
          </div></div>'''
        route = replace(ROUTES[114], options={"record_body_links": True})
        with patch.dict(ROUTES, {114: route}):
            parsed = parse_static_page_html(html, 114)
        self.assertEqual(parsed.status, "ready")
        self.assertIn("Department A", parsed.content)
        self.assertIn("Department B", parsed.content)
        self.assertEqual([item["url"] for item in parsed.attachments],
                         ["https://www.pknu.ac.kr/upload/program.pdf",
                          "https://www.pknu.ac.kr/boardDownload.do?no=42"])
        links = {item["text"]: item for item in parsed.reference_links}
        self.assertEqual(links["Department A"]["url"], "https://department.example/program")
        self.assertFalse(links["Department A"]["followed"])
        self.assertFalse(links["External file"]["followed"])

    def test_card_board_keeps_post_ids_and_metadata_dates(self) -> None:
        html = """<div id="subCont"><ul class="bdWz"><li>
          <a href="?action=view&amp;no=712"><dd><h5>Speech</h5>
          <p>The speech mentions 2024-12-01 in its body.</p>
          <span>Administrator (2025-01-02)<em>Views 12</em></span></dd></a>
          </li><li><a href="?action=view&amp;no=713"><dd><h5>Address</h5>
          <span>Administrator (2025-02-03)</span></dd></a></li></ul></div>"""
        items = parse_list_page(html, "167", "Speeches")
        self.assertEqual([(item.no, item.list_date) for item in items],
                         [("712", "2025-01-02"), ("713", "2025-02-03")])
        self.assertTrue(all(item.categories == {"Speeches"} for item in items))

    def test_numbered_rows_are_not_pinned_even_with_the_noti_class(self) -> None:
        # The notice board marks every number cell class="bdlNum noti" (checked 2026-11-02).
        html = """<table class="brdList"><tbody>
          <tr><td class="bdlNum noti">NOTICE</td><td><a href="?action=view&amp;no=900">Pinned</a></td></tr>
          <tr><td class="bdlNum noti"><img alt="pinned"></td><td><a href="?action=view&amp;no=901">Icon</a></td></tr>
          <tr><td class="bdlNum noti">6936</td><td><a href="?action=view&amp;no=726900">Regular</a></td></tr>
          </tbody></table>"""
        items = parse_list_page(html, "1", "Notice")
        self.assertEqual([(i.no, i.is_notice, i.notice_no) for i in items],
                         [("900", True, None), ("901", True, None), ("726900", False, 6936)])

    def test_system_chart_preserves_paired_lists_side_branches_and_notes(self) -> None:
        html = """<div class="system_conts">
          <div class="system_step1"><a href="/main/276">President<i>i</i></a></div>
          <div class="system_step2"><div><ul><li>Council</li></ul></div>
            <div class="flagship-branch"><div class="flagship-box">Research institute</div></div>
            <ul><li><a href="/main/286">Vice president<i>i</i></a><span>Policy advisor</span></li></ul></div>
          <div class="system_step4"><div><h3>Headquarters</h3><ul>
            <li><a href="/main/298">Academic affairs</a></li>
            <li><ul><li><a href="/main/299">Academic office</a></li></ul></li>
            <li>Student affairs</li><li><ul><li>Student office</li></ul></li>
          </ul></div></div></div>"""
        parsed = parse_system_organization_chart(BeautifulSoup(html, "lxml").select_one(".system_conts"))
        nodes = {node["text"]: node for node in parsed["nodes"]}
        self.assertEqual(nodes["Academic office"]["parent_order"], nodes["Academic affairs"]["order"])
        self.assertEqual(nodes["Student office"]["parent_order"], nodes["Student affairs"]["order"])
        self.assertEqual(nodes["Academic office"]["href"], "/main/299")
        self.assertEqual(nodes["Council"]["kind"], "associated_body")
        self.assertEqual(nodes["Research institute"]["kind"], "side_branch")
        self.assertEqual(nodes["Vice president"]["notes"], ["Policy advisor"])

    def test_chart_introductions_reuse_cache_and_never_visit_homepage_shortcuts(self) -> None:
        chart_html = """<title>Organization</title><div id="subCont"><div class="system_conts">
          <div class="system_step1">President</div><div class="system_step4"><div>
          <h3>Headquarters</h3><ul><li><a href="/main/299">Academic office</a></li>
          <li><a href="/main/533">Anchor office</a></li></ul></div></div></div></div>"""
        intro = """<title>Academic office</title><div id="subCont"><div class="content_wrap">
          <h4 class="subNameH4">Introduction</h4><p>Office responsibilities and guidance.</p>
          <a href="https://other.pknu.ac.kr/">홈페이지 바로가기</a></div></div>"""
        response = Mock(status_code=200, headers={"Content-Type": "text/html"})
        response.iter_content.return_value = [intro.encode("utf-8")]
        session = Mock()
        session.get.return_value = nullcontext(response)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            cached = root / "files/pknu_main/대학소개/조직도/json/pages/main_533.json"
            cached.parent.mkdir(parents=True)
            cached.write_text(json.dumps({"url": "https://www.pknu.ac.kr/main/533",
                "title": "Anchor office", "status": "collected", "contact": {"phone": "123"},
                "staff": []}), encoding="utf-8")
            result = collect_organization_chart(session, "https://www.pknu.ac.kr/main/39", chart_html, root)
            academic = next(node for node in result["nodes"] if node["text"] == "Academic office")
            document = json.loads((root / academic["document_json"]).read_text(encoding="utf-8"))
            self.assertEqual(document["organization_links"][0]["node_id"], academic["id"])
            self.assertEqual(document["organization_links"][0]["path"], academic["path"])
            self.assertFalse(document["reference_links"][0]["followed"])
            self.assertEqual(document["reference_links"][0]["role"], "homepage_shortcut")
            self.assertEqual(result["reused_count"], 1)
        session.get.assert_called_once_with("https://www.pknu.ac.kr/main/299",
                                            timeout=15, stream=True, allow_redirects=False)
        session.post.assert_not_called()

    def test_residence_hall_page_keeps_body_without_shortcut_or_pdf(self) -> None:
        html = """<html><title>Residence hall</title><div id="subCont">
          <div class="content_wrap"><h4 class="subNameH4">Residence hall</h4>
          <div class="sblTxt"><a href="https://dormitory.pknu.ac.kr/">Homepage shortcut</a></div>
          <p>Students can live in the residence hall and use its facilities.
          The university provides space for students on both campuses.</p>
          <img src="/upload/intro.jpg" alt="Welcome">
          <table><tr><th>Hall</th><th>Capacity</th><th>Photo</th></tr>
          <tr><td>Sejong</td><td>850</td>
          <td><img src="https://devwww.pknu.ac.kr/upload/missing.jpg"></td></tr></table>
          <p>See the hall homepage for details.
          <a href="https://dormitory.pknu.ac.kr/">Homepage link</a></p>
          <ul><li>Penalty guide PDF <a href="#none">View</a></li>
          <div class="uploadPdf" data-id="7780"></div></ul>
          </div></div></html>"""
        parsed = parse_static_page_html(html, 262)
        self.assertEqual(parsed.status, "ready")
        self.assertEqual(parsed.structure["table_count"], 1)
        self.assertIn("Sejong", parsed.content)
        self.assertNotIn("Homepage shortcut", parsed.content)
        self.assertNotIn("Homepage link", parsed.content)
        self.assertNotIn("Penalty guide", parsed.content)
        self.assertFalse(parsed.attachments)
        self.assertFalse(parsed.embedded_pdfs)
        self.assertFalse(parsed.reference_links)
        self.assertEqual([item["src"] for item in parsed.body_images], ["/upload/intro.jpg"])
        self.assertEqual(parsed.warnings, ("unavailable_source_images:1",))

    def test_student_organization_chart_and_homepage_links(self) -> None:
        html = """<html><title>Student groups</title><div id="subCont">
          <div class="content_wrap"><h4 class="subNameH4">Organization</h4>
          <p>Student groups plan activities and represent enrolled students.</p>
          <div class="orgCont"><h3>Student council</h3><h4>President</h4>
          <h5>Vice president</h5><h6>Executive chair</h6>
          <ul><li>Welfare office</li><li>Culture office</li></ul></div>
          <h4 class="subNameH4">Homepages</h4><table>
          <tr><th>Council</th><td><a href="https://example.org/council">Council homepage</a></td></tr>
          <tr><th>Clubs</th><td><a href="https://example.org/clubs">Clubs homepage</a></td></tr>
          </table></div></div></html>"""
        parsed = parse_static_page_html(html, 263)
        self.assertEqual(parsed.status, "ready")
        self.assertEqual(parsed.structure["organization_chart_count"], 1)
        self.assertEqual(parsed.structure["table_count"], 1)
        chart = parsed.structure["sections"][0]["blocks"][1]
        self.assertEqual(chart["type"], "organization_chart")
        self.assertEqual([node["parent_order"] for node in chart["nodes"]],
                         [None, 1, 2, 3, 4, 4])
        self.assertEqual([link["context"] for link in parsed.reference_links],
                         [["Council"], ["Clubs"]])
        self.assertEqual([link["url"] for link in parsed.reference_links],
                         ["https://example.org/council", "https://example.org/clubs"])
        self.assertFalse(parsed.attachments)

    def test_accident_files_include_download_endpoint_and_zip(self) -> None:
        links = "".join(
            f'<li><a download="Case {index}" href="{href}">Case {index}</a></li>'
            for index, href in enumerate((
                "/mdaDownload.do?no=400", "/upload/2021.pdf",
                "/upload/2022.pdf", "/upload/2023.pdf", "/upload/2024.zip",
            ), 1)
        )
        html = ("<html><title>Accident cover</title><div id='subCont'>"
                "<div class='content_wrap'><h4 class='subNameH4'>Required files</h4>"
                "<p>Use the forms that match the time when the accident occurred. "
                "The school reviews each claim and its supporting documents.</p>"
                f"<ul>{links}</ul></div></div></html>")
        parsed = parse_static_page_html(html, 260)
        self.assertEqual(parsed.status, "ready")
        self.assertEqual(len(parsed.attachments), 5)
        self.assertEqual(parsed.attachments[0]["url"],
                         "https://www.pknu.ac.kr/mdaDownload.do?no=400")
        self.assertTrue(parsed.attachments[-1]["url"].endswith("2024.zip"))

    def test_static_parser_keeps_internal_downloads(self) -> None:
        html = """<html><title>Student ID</title><div id="subCont">
          <div class="content_wrap"><h4>Issuance</h4>
          <p>Students can apply through the portal or visit the bank office.
          Each method has a separate instruction document for the process.</p>
          <a download="Issuance process" href="/upload/process.pptx">Process</a>
          <a href="https://www.pknu.ac.kr/upload/guide.hwp">Guide</a>
          <a href="https://elsewhere.example/upload/other.pdf">Other site</a>
          </div></div></html>"""
        parsed = parse_static_page_html(html, 114)
        self.assertEqual(parsed.status, "ready")
        self.assertEqual([item["url"] for item in parsed.attachments], [
            "https://www.pknu.ac.kr/upload/process.pptx",
            "https://www.pknu.ac.kr/upload/guide.hwp",
        ])

    def test_nested_student_id_page_excludes_login_shortcut(self) -> None:
        html = """<html><title>International student ID</title>
          <div id="subCont"><ul class="subMenu"><li>Other pages</li></ul>
          <div id="subCont"><div class="col-lg-12"><h4 class="subName">ISIC</h4>
          <p>Students can apply for an international ID after checking eligibility.
          The university verifies enrollment before the student completes payment.</p>
          <img src="/upload/card.jpg" alt="Card">
          <div class="lgnForm"><a href="/main/149">Login shortcut</a></div>
          </div></div></div></html>"""
        parsed = parse_static_page_html(html, 115)
        self.assertEqual(parsed.status, "ready")
        self.assertIn("verifies enrollment", parsed.content)
        self.assertEqual([image["src"] for image in parsed.body_images], ["/upload/card.jpg"])
        self.assertNotIn("Login shortcut", parsed.content)
        self.assertNotIn("Other pages", parsed.content)

    def test_certificate_page_keeps_two_embedded_pdf_ids(self) -> None:
        html = """<html><title>Certificates</title><div id="subCont">
          <div class="content_wrap"><h4 class="subNameH4">Issuance</h4>
          <p>Students can request certificates at the office or through an online
          service. The university also provides two PDF instruction documents.</p>
          <ul><li>Machine location PDF <button>View</button></li>
          <div class="uploadPdf" data-id="5738"></div>
          <li>Password change PDF <button>View</button></li>
          <div class="uploadPdf" data-id="5740"></div></ul>
          </div></div></html>"""
        parsed = parse_static_page_html(html, 449)
        self.assertEqual(parsed.status, "ready")
        self.assertEqual([item["media_id"] for item in parsed.embedded_pdfs],
                         ["5738", "5740"])
        self.assertEqual(parse_static_page_html(html, 448).status, "needs_review")

    def test_table_keeps_headers_coordinates_and_shared_cell(self) -> None:
        soup = BeautifulSoup("""<table><tr><th>Category</th><th>Visit</th><th>Online</th></tr>
          <tr><th>Application</th><td>Office</td><td>Portal</td></tr>
          <tr><th>Review</th><td colspan="2">Shared approval</td></tr></table>""", "lxml")
        table = parse_table(soup.select_one("table"))
        self.assertEqual((table["row_count"], table["column_count"]), (3, 3))
        self.assertEqual(table["rows"][2][1]["column"], 1)
        self.assertEqual(table["rows"][2][1]["colspan"], 2)
        self.assertTrue(table["rows"][0][1]["header"])

    def test_both_diagram_layouts_keep_card_order(self) -> None:
        for layout, card in (("daStep", "dasCont"), ("stFlw_B", "stfCont_B")):
            soup = BeautifulSoup(f"""<div class="{layout}">
              <div class="{card}"><h5>Apply</h5><p>Student</p></div>
              <div class="{card}"><h5>Approve</h5><p>Department</p></div></div>""", "lxml")
            flow = parse_flow(soup.select_one(f".{layout}"))
            self.assertEqual([step["heading"] for step in flow["steps"]], ["Apply", "Approve"])
            self.assertEqual(flow["transitions"], [{"from_order": 1, "to_order": 2}])

    def test_exchange_program_diagram_keeps_numbered_and_final_steps(self) -> None:
        html = """<html><title>Exchange program</title><div id="subCont">
          <div class="content_wrap"><h4 class="subNameH4">Process</h4>
          <div class="stFlw col4"><div><div class="stfCont"><em>01</em>
          <div><p>Announcement</p></div></div></div>
          <div><div class="stfCont"><em>02</em><div><p>Application</p></div></div></div>
          <div><div class="stfCont last"><div><p>Departure</p></div></div></div>
          </div><p>Students should confirm the host university requirements and
          prepare their travel documents before departure.</p></div></div></html>"""
        parsed = parse_static_page_html(html, 258)
        self.assertEqual(parsed.status, "ready")
        self.assertEqual(parsed.structure["flow_count"], 1)
        flow = parsed.structure["sections"][0]["blocks"][0]
        self.assertEqual(flow["type"], "flow")
        self.assertEqual([step["heading"] for step in flow["steps"]],
                         ["Announcement", "Application", "Departure"])
        self.assertEqual([step["display_number"] for step in flow["steps"]],
                         ["01", "02", None])
        self.assertEqual(flow["transitions"], [
            {"from_order": 1, "to_order": 2}, {"from_order": 2, "to_order": 3}])
        self.assertIn("3. Departure", parsed.content)

    def test_static_parser_finds_structure_and_verified_file_tab(self) -> None:
        html = """<html><title>Graduation guide</title><div id="subCont">
          <ul class="subTab"><li><a href="/main/238">Graduation requirements PDF</a></li></ul>
          <div class="content_wrap"><h4 class="subName">Requirements</h4>
          <p>Students must check their required credits and apply through the academic portal.
          The department reviews the application and the college confirms the result.
          Please consult the linked document for detailed graduation requirements.</p>
          <table><tr><th>Credits</th><td>130 or more</td></tr></table>
          <dl><dd><div class="stFlw_B">
            <div class="stfCont_B"><h5>Apply</h5><p>Student</p></div>
            <div class="stfCont_B"><h5>Approve</h5><p>Department</p></div>
          </div></dd></dl></div></div></html>"""
        parsed = parse_static_page_html(html, 94)
        self.assertEqual(parsed.status, "ready")
        self.assertEqual(parsed.linked_file_page_ids, (238,))
        self.assertEqual(parsed.structure["table_count"], 1)
        self.assertEqual(parsed.structure["flow_count"], 1)
        self.assertIn("130 or more", parsed.content)

    def test_empty_decorative_table_does_not_fail_page(self) -> None:
        soup = BeautifulSoup("""<div id="subCont"><div class="content_wrap">
          <h4 class="subName">Guide</h4><table><tr></tr></table>
          <p>This is a sufficiently long academic guide paragraph for students.</p>
          </div></div>""", "lxml")
        structure = extract_structure(soup.select_one("#subCont"))
        self.assertEqual(structure["table_count"], 0)
        self.assertIn("academic guide", structure["content"])

    def test_static_parser_keeps_diagram_images_after_tables(self) -> None:
        html = """<html><title>Field practice</title><div id="subCont"><div class="content_wrap">
          <h4 class="subName">Field practice</h4>
          <p>Students complete a placement and submit a report to their department.
          The department checks the placement record before granting course credit.</p>
          <table><tr><th>Course</th><td>Placement</td></tr></table>
          <h4 class="subNameH4">Workflow 1</h4><div><a href="/flow1.png"><img src="/flow1.png"></a></div>
          <h4 class="subNameH4">Workflow 2</h4><div><img src="/flow2.jpg"></div>
          </div></div></html>"""
        parsed = parse_static_page_html(html, 101)
        self.assertEqual(parsed.status, "ready")
        self.assertEqual(parsed.structure["table_count"], 1)
        self.assertEqual([image["src"] for image in parsed.body_images],
                         ["/flow1.png", "/flow2.jpg"])
        self.assertEqual(parsed.content.count("[이미지]"), 2)

    def test_table_inside_list_item_keeps_grid(self) -> None:
        soup = BeautifulSoup("""<div id="subCont"><div class="content_wrap">
          <h4 class="subName">Grades</h4><ul><li>Grade scale
          <div class="subTb_scroll"><table><tr><th>Grade</th><th>Point</th></tr>
          <tr><td>A+</td><td>4.50</td></tr></table></div></li></ul>
          </div></div>""", "lxml")
        structure = extract_structure(soup.select_one("#subCont"))
        self.assertEqual(structure["table_count"], 1)
        item = structure["sections"][0]["blocks"][0]["items"][0]
        table = next(block for block in item["blocks"] if block["type"] == "table")
        self.assertEqual((table["row_count"], table["column_count"]), (2, 2))
        self.assertIn("A+ / 4.50", structure["content"])

    def test_nested_list_directly_inside_list_keeps_child_items(self) -> None:
        soup = BeautifulSoup("""<div id="subCont"><div class="content_wrap">
          <h4 class="subNameH4">Application period</h4>
          <ul><li>Apply before the deadline</li>
            <ul><li>Late applications are rejected</li><li>Check the form</li></ul>
          </ul></div></div>""", "lxml")
        structure = extract_structure(soup.select_one("#subCont"))
        self.assertIn("Late applications are rejected", structure["content"])
        self.assertIn("Check the form", structure["content"])
        outer = structure["sections"][0]["blocks"][0]
        self.assertEqual(outer["items"][0]["blocks"][1]["type"], "list")

    def test_file_discovery_rejects_unknown_or_external_links(self) -> None:
        soup = BeautifulSoup("""<div class="subTab">
          <a href="/main/238">file</a><a href="/main/999">unknown</a>
          <a href="https://elsewhere.example/main/238">external</a></div>""", "lxml")
        self.assertEqual(linked_file_ids(soup, "https://www.pknu.ac.kr/main/94", {238}), (238,))

    def test_board_intro_keeps_guide_without_list_rows(self) -> None:
        html = """<html><title>Credit transfer</title><div id="subCont">
          <h4 class="subName">Credit transfer</h4>
          <h4 class="subNameH4">Eligibility</h4>
          <p>Students may apply after completing one semester at this university.
          Applications need approval from their department and college.</p>
          <form id="frmPost"><input name="bbsId" value="307"></form>
          <div class="bdCont">Board controls</div>
          <table class="brdList"><tr><td><a href="?action=view&amp;no=123">Post title</a></td></tr></table>
          <div class="paging">1 / 38</div></div></html>"""
        parsed = parse_board_intro_html(html, 95)
        self.assertEqual(parsed.status, "ready")
        self.assertIn("Eligibility", parsed.content)
        self.assertNotIn("Post title", parsed.content)
        self.assertNotIn("Board controls", parsed.content)


if __name__ == "__main__":
    unittest.main()
