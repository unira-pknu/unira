from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from tempfile import TemporaryDirectory
from pathlib import Path
from dataclasses import replace
from contextlib import nullcontext
from bs4 import BeautifulSoup

from scripts.crawlers.main.collectors import tuition as crawler
from scripts.crawlers.common.schema import attachment_text, validate_common_document
from scripts.crawlers.main.collectors.guides import parse_guide_items_from_html
from scripts.crawlers.main.collectors.academic_calendar import _site_id, parse_month
from scripts.crawlers.main.collectors.curriculum_files import _clean_page_text, _is_visually_blank, parse_curriculum_attachments
from scripts.crawlers.main.collectors.major_program import collect_major_program, parse_major_program_page
from scripts.crawlers.main.collectors.campus_buildings import parse_campus_buildings, parse_building_details
from scripts.crawlers.main.collectors.facility_rental import parse_building_options, parse_room_options, parse_facility_detail
from scripts.crawlers.main.collectors.navigation import crawl_linked_pages, parse_page_choices
from scripts.crawlers.main.routes import ROUTES
from scripts.crawlers.main.collectors import boards


class BoardSampleTests(unittest.TestCase):
    def test_all_pages_reads_later_lists_and_deduplicates_pinned_posts(self) -> None:
        route = replace(ROUTES[228], options={"board_id": "85"})
        root = Path("board-archive-output").resolve()
        responses = []
        for page in range(1, 4):
            body = '<input name="bbsId" value="85"><ul class="bdWz">' + ''.join(
                f'<li><a href="?action=view&no={n}"><h5>Post {n}</h5></a></li>'
                for n in dict.fromkeys((1, page))) + '</ul>'
            list_url = route.url if page == 1 else f"{route.url}?bbsId=85&pageIndex={page}"
            responses.append(SimpleNamespace(status_code=200, url=list_url,
                             headers={"Content-Type": "text/html"}, text=body))
        responses.append(SimpleNamespace(status_code=200, url=f"{route.url}?action=view&no=1",
                         headers={"Content-Type": "text/html"}, text='''
          <form id="frmPost"><input name="bbsId" value="85"><input name="chkNo" value="1"></form>
          <table><tr class="first_noti"><td class="title_b">Post 1</td></tr></table>
          <div class="bdvTxt"><p>Original article body.</p></div>'''))
        paths = Mock()
        paths.document_json.return_value = root / "post.json"
        paths.attachment_dir.return_value = root / "files"
        with (patch.dict(ROUTES, {228: route}), patch.object(boards, "PROJECT_ROOT", root),
              patch.object(boards, "PATHS", paths), patch.object(boards, "fetch", side_effect=responses) as fetch,
              patch.object(boards.cms_board, "parse_page_indicator", return_value=(1, 3)),
              patch.object(boards.cms_board, "save_attachments", return_value=[]),
              patch.object(boards, "save_json"), patch.object(boards, "write_json")):
            _, result = boards.crawl_board_page(Mock(), {"items": {}}, 228, False,
                                                max_pages=None, max_posts=1)
        self.assertEqual(result["pages_crawled"], 3)
        self.assertEqual(result["listed_count"], 3)
        self.assertEqual(fetch.call_count, 4)
        self.assertIn("pageIndex=3", fetch.call_args_list[2].args[1])

    def test_post_limit_bounds_detail_requests_and_keeps_original_language(self) -> None:
        route = replace(ROUTES[228], options={"board_id": "85", "language": "en"})
        list_html = '<input name="bbsId" value="85"><ul class="bdWz">' + ''.join(
            f'<li><a href="?action=view&no={n}"><h5>Research {n}</h5></a></li>'
            for n in (1, 2, 3)) + '</ul>'
        root = Path("board-sample-output").resolve()

        def response(url, body):
            return SimpleNamespace(status_code=200, url=url,
                                   headers={"Content-Type": "text/html"}, text=body)

        for limit, selected in ((2, ["1", "2"]), (None, ["1", "2", "3"])):
            with self.subTest(limit=limit):
                paths = Mock()
                paths.document_json.side_effect = lambda category, slug, **kwargs: root / "posts" / f"{slug}.json"
                paths.attachment_dir.return_value = root / "files"
                responses = [response(route.url, list_html)]
                for n in selected:
                    detail = f'''<form id="frmPost"><input name="bbsId" value="85">
                      <input name="chkNo" value="{n}"></form><table><tr class="first_noti">
                      <td class="title_b">Research {n}</td></tr></table>
                      <div class="bdvTxt"><p>Original English research content {n}.</p></div>'''
                    responses.append(response(f"{route.url}?action=view&no={n}", detail))
                with (patch.dict(ROUTES, {228: route}),
                      patch.object(boards, "PROJECT_ROOT", root),
                      patch.object(boards, "PATHS", paths),
                      patch.object(boards, "fetch", side_effect=responses) as fetch,
                      patch.object(boards, "save_json") as save,
                      patch.object(boards, "write_json"),
                      patch.object(boards.cms_board, "save_attachments", return_value=[])):
                    stats, result = boards.crawl_board_page(Mock(), {"items": {}}, 228, False, max_posts=limit)
                    self.assertEqual(result["status"], "success")
                    self.assertEqual(result["listed_count"], 3)
                    self.assertEqual([d["post_no"] for d in result["documents"]], selected)
                    self.assertEqual(fetch.call_count, len(selected) + 1)
                    self.assertEqual(stats.new, len(selected))
                    for call in save.call_args_list:
                        doc = call.args[0]
                        self.assertEqual(doc["language"], "en")
                        self.assertIn("Original English research content", doc["content"])
                        self.assertEqual(validate_common_document(doc), [])
        for invalid in (0, -1, True, 1.5):
            with self.assertRaisesRegex(ValueError, "positive integer"):
                boards.crawl_board_page(Mock(), {"items": {}}, 228, False, max_posts=invalid)


class NoticeBoardArchiveTests(unittest.TestCase):
    def test_incomplete_archive_list_is_not_reported_as_success(self) -> None:
        from scripts.crawlers import pknu_notice as notice

        with (patch.object(notice, "fetch", side_effect=[SimpleNamespace(text="first"), None]),
              patch.object(notice, "parse_page_indicator", return_value=(1, 5)),
              patch.object(notice, "parse_list_page", return_value=[SimpleNamespace(no="1")])):
            with self.assertRaisesRegex(RuntimeError, "notice_list_incomplete.*page=2/5"):
                notice.collect_list_items(Mock(), ["10001"], None, False, all_pages=True)

    def test_all_pages_exceeds_incremental_limit_without_forcing_redownloads(self) -> None:
        from scripts.crawlers import pknu_notice as notice
        from scripts.crawlers.common.schema import CrawlStats

        responses = [SimpleNamespace(text=str(page)) for page in range(1, 6)]
        session = Mock()
        with (patch.object(notice, "build_session", return_value=session),
              patch.object(notice, "load_state", return_value={"items": {}}),
              patch.object(notice, "fetch", side_effect=responses) as fetch,
              patch.object(notice, "parse_page_indicator", return_value=(1, 5)),
              patch.object(notice, "parse_list_page", side_effect=lambda text, *a: [SimpleNamespace(no=text)]),
              patch.object(notice, "crawl_details", return_value=(CrawlStats(), set())) as details,
              patch.object(notice, "process_orphans") as orphans,
              patch.object(notice, "save_state")):
            notice.run_crawl(only_cd="10001", all_pages=True)
        self.assertEqual(fetch.call_count, 5)
        self.assertEqual(fetch.call_args_list[-1].kwargs["params"]["pageIndex"], "5")
        self.assertEqual(len(details.call_args.args[1]), 5)
        self.assertFalse(details.call_args.args[3])
        orphans.assert_not_called()

    def test_community_passes_all_pages_to_notice_crawler(self) -> None:
        from scripts.crawlers.main.categories import community
        from scripts.crawlers.common.schema import CrawlStats

        with patch("scripts.crawlers.pknu_notice.run_crawl", return_value=CrawlStats()) as crawl:
            community.execute([{"page_id": 163, "handler": "notice"}], full_resync=False,
                              all_board_pages=True)
        self.assertTrue(crawl.call_args.kwargs["all_pages"])
        self.assertFalse(crawl.call_args.kwargs["full_resync"])


class LinkedPageTests(unittest.TestCase):
    def test_selection_scope_keeps_unlinked_choices_and_rejects_other_destinations(self) -> None:
        html = '''<div id="subCont"><ul class="spot-nav">
          <li class="active"><a href="/main/498"><span class="spot-num">1경</span><span class="spot-name">Forest</span></a></li>
          <li><a><span class="spot-num">5경</span><span class="spot-name">Planned garden</span></a></li>
          </ul><a href="/main/39">Unrelated navigation</a></div>'''
        choices = parse_page_choices(html, 495)
        self.assertEqual([item["page_id"] for item in choices], [498, None])
        self.assertTrue(choices[0]["selected"])
        self.assertEqual(choices[1]["status"], "unavailable")
        for destination in ("https://other.example/main/498", "/main/39", "/main/498?action=view&no=1"):
            with self.assertRaisesRegex(ValueError, "outside registered siblings"):
                parse_page_choices(html.replace("/main/498", destination), 495)

    def test_index_links_each_document_and_reuses_complete_results_without_requests(self) -> None:
        html = '''<div id="subCont"><ul class="spot-nav">
          <li class="active"><a href="/main/498"><span class="spot-name">Forest</span></a></li>
          <li><a><span class="spot-name">Planned garden</span></a></li></ul></div>'''
        route = replace(ROUTES[495], options={**ROUTES[495].options, "document_title": "Landmarks"})
        session = Mock()
        response = SimpleNamespace(status_code=200, url=route.url,
                                   headers={"Content-Type": "text/html"}, text=html)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root.joinpath("files", "pknu_main", *route.category_path, "json", "pages")
            directory.mkdir(parents=True)
            child = directory / "child.json"
            child.write_text(json.dumps({"url": ROUTES[498].url, "type": "page", "content": "Forest description"}), encoding="utf-8")
            with (patch("scripts.crawlers.main.collectors.navigation.PROJECT_ROOT", root),
                  patch("scripts.crawlers.main.collectors.navigation.fetch", return_value=response) as fetch,
                  patch("scripts.crawlers.main.collectors.navigation.crawl_static_pages") as collect,
                  patch.dict(ROUTES, {495: route})):
                from scripts.crawlers.common.schema import CrawlStats
                collect.return_value = CrawlStats()
                stats, detail = crawl_linked_pages(session, {}, 495, False)
                index = json.loads((root / detail["output"]).read_text(encoding="utf-8"))
                self.assertEqual(index["collected_count"], 1)
                self.assertEqual(index["unavailable_count"], 1)
                self.assertEqual(index["selected_document_json"], child.relative_to(root).as_posix())
                self.assertEqual(json.loads(child.read_text(encoding="utf-8"))["page_group_links"][0]["page_id"], 495)
                self.assertEqual(collect.call_args.args[3], ())
                fetch.reset_mock()
                collect.reset_mock()
                cached_stats, cached_detail = crawl_linked_pages(session, {}, 495, False)
                self.assertEqual(cached_detail["status"], "unchanged")
                self.assertEqual(cached_stats.requested, 0)
                fetch.assert_not_called()
                collect.assert_not_called()


class FacilityRentalTests(unittest.TestCase):
    def test_building_options_use_the_verified_two_stage_widget(self) -> None:
        html = '''<div id="subCont"><select id="buildingList">
          <option value="">건물명</option><option value="70001">대학극장</option></select>
          <select id="roomList"><option>건물을 선택하세요</option></select></div>
          <script>$.ajax({url:'/facility/roomList.do',data:{cd:$('#buildingList option:selected').val()}});
          $.ajax({url:'/facility/view.do',data:{no:$('#roomList option:selected').val()}});</script>'''
        self.assertEqual(parse_building_options(html), [{"building_code": "70001", "name": "대학극장"}])
        with self.assertRaisesRegex(ValueError, "widget source"):
            parse_building_options(html.replace("/facility/view.do", "https://other.example/view.do"))

    def test_empty_building_is_valid_and_room_list_preserves_displayed_records(self) -> None:
        building = {"building_code": "70001", "name": "대학극장"}
        self.assertEqual(parse_room_options({"list": []}, building), [])
        self.assertEqual(parse_room_options({"list": [{"facSeq": 2, "facNm": "대학극장",
                        "buildingNm": "대학극장", "stat": "1"}]}, building),
                         [{"facility_id": "2", "name": "대학극장"}])
        with self.assertRaisesRegex(ValueError, "another building"):
            parse_room_options({"list": [{"facSeq": 2, "facNm": "강의실", "buildingNm": "다른 건물"}]}, building)

    def test_details_preserve_prose_tables_and_photo_links_without_following_links(self) -> None:
        building, room = {"building_code": "70001", "name": "대학극장"}, {"facility_id": "2", "name": "대학극장"}
        payload = {"result": {"facSeq": 2, "buildingCd": "70001", "buildingNm": "대학극장",
            "facNm": "대학극장", "seats": "794석", "area": "1,653", "use": "행사, 공연",
            "regIp": "excluded", "cont": '''<h4 class="subNameH4">기본물품</h4>
              <!-- template --><p>강연대와 빔프로젝터</p><script>hidden()</script>
              <table><tr><th>관리부서</th><td>총무과</td></tr></table>
              <a href="https://example.org/apply">신청</a>'''},
            "resultFile": [{"facSeq": 2, "fafSeq": 5, "fileNm": "시설사진.jpg"}]}
        result = parse_facility_detail(payload, building, room)
        self.assertEqual(result["structured_content"]["table_count"], 1)
        self.assertIn("강연대와 빔프로젝터", result["content"])
        self.assertNotIn("template", result["content"])
        self.assertNotIn("hidden()", result["content"])
        self.assertNotIn("regIp", result)
        self.assertFalse(result["reference_links"][0]["followed"])
        self.assertEqual(result["images"][0]["source_url"],
                         "https://www.pknu.ac.kr/imageView.do?target=facility&cd=5")
        payload["result"]["buildingCd"] = "70002"
        with self.assertRaisesRegex(ValueError, "disagree"):
            parse_facility_detail(payload, building, room)


class CampusBuildingTests(unittest.TestCase):
    def test_visible_building_list_excludes_map_and_filter_controls(self) -> None:
        html = '''<div class="campusMap_conts">
          <div class="campusMap_title"><p>대연캠퍼스<span>건물번호</span></p></div>
          <a href="/main/134">용당캠퍼스</a><div id="kMap">지도</div>
          <div class="campusList_scroll"><div class="campusList_conts">
            <div class="campusList_conts_open"><ul><li>A11</li><li>대학본부</li></ul></div>
            <a class="campusList_road" href="https://map.kakao.com/">길찾기</a>
            <a class="campusList_detail" onclick="buildingInfoAjax('B0000001');setCenter()">상세정보</a>
          </div></div></div><script>
          function buildingInfoAjax(code) {var stat = "D";
            $.ajax({url: "/buildingInfoAjax.do", data: {code:code,stat:stat}, success: function(data) {}});
          }</script>'''
        parsed = parse_campus_buildings(html)
        self.assertEqual(parsed, {"campus": "대연캠퍼스", "campus_code": "D", "buildings": [
            {"building_code": "B0000001", "building_number": "A11", "name": "대학본부"}]})
        with self.assertRaisesRegex(ValueError, "widget source changed"):
            parse_campus_buildings(html.replace("/buildingInfoAjax.do", "/unknown.do"))

    def test_rooms_keep_floor_type_and_hidden_records_are_excluded(self) -> None:
        building = {"building_code": "B0000001", "building_number": "A11", "name": "대학본부"}
        payload = {"response": {"campusCode": "B0000001", "deps1": [
            {"buildingCode": "B0000001", "buildingNo": "A11", "buildingName": "대학본부",
             "campusTyp": "D", "stat": "0", "regIp": "excluded"}], "max": [
            {"buildingCode": "B0000001", "layerTyp": "0", "layer": "1", "stat": "0"},
            {"buildingCode": "B0000001", "layerTyp": "1", "layer": "B1", "stat": 0}], "deps2": [
            {"buildingCode": "B0000001", "layerTyp": "0", "layer": "1", "stat": "0",
             "roomNo": "101", "roomName": " 안내\u00a0  사무실\u200b "},
            {"buildingCode": "B0000001", "layerTyp": "1", "layer": "1", "stat": 0,
             "roomNo": "B01", "roomName": "자료실"},
            {"buildingCode": "B0000001", "layerTyp": "0", "layer": "1", "stat": "1",
             "roomNo": "102", "roomName": "삭제된 호실"}]}}
        parsed = parse_building_details(payload, building, "D")
        self.assertEqual([floor["label"] for floor in parsed["floors"]], ["지상1층", "지하1층"])
        self.assertEqual(parsed["floors"][0]["rooms"], [{"room_number": "101", "name": "안내 사무실"}])
        self.assertEqual(parsed["floors"][1]["source_floor_number"], "B1")
        self.assertEqual(parsed["room_count"], 2)
        self.assertNotIn("regIp", str(parsed))
        payload["response"]["deps2"][0]["buildingCode"] = "B0000002"
        with self.assertRaisesRegex(ValueError, "unexpected building or floor"):
            parse_building_details(payload, building, "D")

    def test_detail_response_must_match_requested_building(self) -> None:
        with self.assertRaisesRegex(ValueError, "another building"):
            parse_building_details({"response": {"campusCode": "B0000002"}},
                                   {"building_code": "B0000001"}, "D")


class AcademicCalendarTests(unittest.TestCase):
    def test_reads_only_the_schedule_widget_source(self) -> None:
        html = '''<div id="subCont"><tbody id="scheduleList"></tbody></div>
        <script>function getMonthList() { $.ajax({url : "/getScheduleList.do",
        data : {steId : 'IEav5ISH'}}); }</script>'''
        self.assertEqual(_site_id(html), "IEav5ISH")
        with self.assertRaisesRegex(ValueError, "table is missing"):
            _site_id(html.replace('id="scheduleList"', 'id="other"'))

    def test_bad_source_date_is_reviewable_without_losing_valid_rows(self) -> None:
        rows = [
            {"schSeq": 1, "schNm": "개강", "schCont": "개강",
             "strDt": "20260901", "endDt": "20260901", "steId": "IEav5ISH"},
            {"schSeq": 2, "schNm": "잘못된 일정", "strDt": "20261221",
             "endDt": "20260112", "steId": "IEav5ISH"},
        ]
        events, rejected = parse_month(json.dumps({"list": rows}).encode(),
                                       year=2026, month=9, site_id="IEav5ISH")
        self.assertEqual([event["source_record_id"] for event in events], [1])
        self.assertEqual(rejected[0]["reason"], "end date precedes start date")
        self.assertEqual(rejected[0]["raw_end_date"], "20260112")

    def test_unexpected_site_is_not_accepted(self) -> None:
        payload = json.dumps({"list": [{"steId": "other"}]}).encode()
        with self.assertRaisesRegex(ValueError, "unexpected site ID"):
            parse_month(payload, year=2026, month=9, site_id="IEav5ISH")


class MajorProgramTests(unittest.TestCase):
    def test_policy_disables_ocr_and_preserves_major_program_original(self) -> None:
        html = '<div id="subCont"><h3 class="subTitle">Guide</h3><div class="content_wrap"><img src="/upload/guide.jpg"></div></div>'
        response = Mock(status_code=200, url=ROUTES[233].url, encoding="utf-8",
                        headers={"Content-Type": "text/html"})
        response.iter_content.return_value = [html.encode()]
        session = Mock()
        session.get.return_value = nullcontext(response)
        image = {"order": 1, "status": "saved", "source_url": "https://www.pknu.ac.kr/upload/guide.jpg",
                 "saved_path": "files/guide.jpg", "sha256": "original"}
        with (patch("scripts.crawlers.main.routes.COLLECTION_POLICY", {"body_image_ocr": False}),
              patch("scripts.crawlers.main.collectors.major_program.save_body_images", return_value=[image]),
              patch("scripts.crawlers.main.collectors.major_program.extract_image") as ocr):
            document = collect_major_program(session, 233)
        ocr.assert_not_called()
        self.assertEqual(document["status"], "collected")
        self.assertEqual(document["images"][0]["saved_path"], image["saved_path"])
        self.assertEqual(document["image_text_extraction"], "not_requested")
        self.assertEqual(document["content"], "")
        self.assertEqual(document["warnings"], [])

    def test_image_inside_layout_table_is_collected(self) -> None:
        html = """<div id="subCont"><h3 class="subTitle">마이크로전공</h3>
          <div class="content_wrap"><table><tr><td>
          <img src="/upload/guide.jpg" alt="안내문"></td></tr></table></div></div>"""
        title, images = parse_major_program_page(html, 235)
        self.assertEqual(title, "마이크로전공")
        self.assertEqual(images, [{"order": 1, "src": "/upload/guide.jpg", "alt": "안내문"}])

    def test_new_html_body_text_requires_parser_review(self) -> None:
        html = """<div id="subCont"><h3 class="subTitle">전공제도</h3>
          <div class="content_wrap"><p>새 안내</p><img src="/guide.jpg"></div></div>"""
        with self.assertRaisesRegex(ValueError, "HTML body text"):
            parse_major_program_page(html, 233)


class MainGuideTests(unittest.TestCase):
    def test_discovery_policy_does_not_run_ocr_or_image_layout_analysis(self) -> None:
        from scripts.crawlers.main.discovery import process_pknu_main_content as discovery
        root = BeautifulSoup('<div><img src="/upload/guide.jpg"></div>', 'lxml').div
        record = {"status": "saved", "source_url": "https://www.pknu.ac.kr/upload/guide.jpg",
                  "saved_path": "files/guide.jpg", "sha256": "original"}
        with (patch("scripts.crawlers.main.routes.COLLECTION_POLICY", {"body_image_ocr": False}),
              patch.object(discovery, "ROOT", Path("never-written-discovery").resolve()),
              patch.object(discovery, "save_body_images", return_value=[record]),
              patch.object(discovery, "extract_image") as ocr,
              patch.object(discovery, "enrich_entry") as layout,
              patch.object(Path, "mkdir"), patch.object(Path, "write_text")):
            result = discovery.inspect_images(root, 9999, "https://www.pknu.ac.kr/main/9999", Mock(),
                        tesseract="unused", language="kor", tessdata=None, ocr_psm=6, restore_ocr_spacing=False)
        ocr.assert_not_called()
        layout.assert_not_called()
        self.assertEqual(result["status"], "collected")
        self.assertEqual(result["image_text_extraction"], "not_requested")
        self.assertEqual(result["ocr_block_count"], 0)

    def test_contest_pdfs_are_discovered_with_section_context(self) -> None:
        html = """<div id="subCont"><div class="content_wrap">
          <h4 class="subNameH4">2024학년도 「대학생활계획서 콘테스트」 우수작</h4>
          <ul><li>우수작 1번(PDF) 보기</li><div class="uploadPdf" data-id="6260"></div>
          <li>우수작 2번(PDF) 보기</li><div class="uploadPdf" data-id="6262"></div></ul>
          </div></div>"""
        with patch(
            "scripts.crawlers.main.collectors.guides.resolve_media_pdf_url",
            side_effect=lambda _session, media_id: f"https://www.pknu.ac.kr/upload/{media_id}.pdf",
        ):
            items = parse_guide_items_from_html(None, html)
        self.assertEqual([item.media_id for item in items], ["6260", "6262"])
        self.assertEqual([item.title for item in items], [
            "2024학년도 대학생활계획서 콘테스트 우수작 1번",
            "2024학년도 대학생활계획서 콘테스트 우수작 2번",
        ])
        self.assertEqual([item.year for item in items], [2024, 2024])
        self.assertEqual([item.subcategory for item in items], ["학생생활", "학생생활"])


class CurriculumFileTests(unittest.TestCase):
    def test_embedded_pdf_filter_selects_only_named_guides_and_validates_media_id(self) -> None:
        html = '''<div id="subCont"><h3 class="subTitle">수강신청 안내</h3>
          <div class="content_wrap">
            <ul><li>2026-1학기 강의편람(PDF) <a href="#none">보기</a></li></ul>
            <div class="uploadPdf" data-id="8740"></div>
            <a download="다른 안내" href="https://example.org/ignored.pdf">다른 PDF</a>
            <ul><li>다른 자료(PDF)</li></ul><div class="uploadPdf" data-id="9999"></div>
            <a href="https://portal.pknu.ac.kr/">포털 바로가기</a>
          </div></div>'''
        route = replace(ROUTES[106], options={"include_embedded_pdfs": True,
                        "attachment_title_contains": "강의편람"})
        with patch.dict(ROUTES, {106: route}):
            _, attachments = parse_curriculum_attachments(html, 106)
            self.assertEqual(attachments, [{"title": "2026-1학기 강의편람",
                "filename": "2026-1학기 강의편람.pdf", "media_id": "8740"}])
            with self.assertRaisesRegex(ValueError, "media ID missing"):
                parse_curriculum_attachments(html.replace('data-id="8740"', 'data-id="bad"'), 106)

    def test_collects_pdf_attachments_without_ebook_links(self) -> None:
        html = """
        <div id="subCont"><h3 class="subTitle">교육과정 안내</h3>
          <div class="content_wrap">
            <a href="/ebook/curr/index.html">ebook 바로가기</a>
            <a download="2026 교육과정 안내서" href="/upload/media/guide.pdf">PDF 다운로드</a>
            <a href="https://portal.pknu.ac.kr/">포털 바로가기</a>
          </div>
        </div>
        """
        title, attachments = parse_curriculum_attachments(html, 106)
        self.assertEqual(title, "교육과정 안내")
        self.assertEqual(attachments, [{
            "title": "2026 교육과정 안내서",
            "filename": "2026 교육과정 안내서.pdf",
            "url": "https://www.pknu.ac.kr/upload/media/guide.pdf",
        }])

    def test_rejects_an_unverified_attachment_host(self) -> None:
        html = """
        <div id="subCont"><h3 class="subTitle">비교과 교육과정 안내</h3>
          <div class="content_wrap">
            <a download="자료" href="https://example.org/guide.pdf">PDF</a>
          </div>
        </div>
        """
        with self.assertRaisesRegex(ValueError, "unexpected curriculum attachment URL"):
            parse_curriculum_attachments(html, 362)

    def test_page_text_cleanup_preserves_content_and_removes_ocr_noise(self) -> None:
        raw = "21\n|  비교과   교육과정  |\n___\n내용\u00a0 설명\n내용  설명\n21"
        self.assertEqual(
            _clean_page_text(raw, 21, ocr=True),
            "비교과 교육과정\n내용 설명",
        )

    def test_blank_pdf_page_is_not_sent_to_ocr(self) -> None:
        import fitz

        with fitz.open() as pdf:
            page = pdf.new_page()
            self.assertTrue(_is_visually_blank(page))
            page.insert_text((72, 72), "Visible text")
            self.assertFalse(_is_visually_blank(page))


class TuitionFaqTests(unittest.TestCase):
    def test_attachment_only_answer_is_extracted_without_following_payment_tab(self) -> None:
        faq_url = "https://www.pknu.ac.kr/main/250"
        post_url = f"{faq_url}?action=view&no=999001"
        list_html = """
            <div id="subCont"><a href="/main/251">등록금납부</a>
            <input name="bbsId" value="86"><span>1</span> / 1
            <table class="brdList"><tbody><tr>
              <td class="bdlNum">1</td><td class="bdlDate">2026-09-01</td>
              <td><a href="?action=view&no=999001">질문</a></td>
            </tr></tbody></table></div>
        """
        detail_html = """
            <form id="frmPost"><input name="bbsId" value="86">
              <input name="chkNo" value="999001"></form>
            <div class="bdCont"><table><tr class="first_noti">
              <td class="title_b">등록금 질문</td></tr></table>
              <div class="bdvTxt"></div>
              <a href="/boardDownload.do?no=42">answer.pdf</a></div>
        """
        requested: list[str] = []

        def fake_fetch(_session: object, url: str) -> SimpleNamespace:
            requested.append(url)
            if url == faq_url:
                html = list_html
            elif url == post_url:
                html = detail_html
            else:
                raise AssertionError(f"unexpected request: {url}")
            return SimpleNamespace(status_code=200, url=url,
                                   headers={"Content-Type": "text/html"}, text=html)

        attachment = {
            "id": "attachment-001", "name": "answer.pdf",
            "url": "https://www.pknu.ac.kr/boardDownload.do?no=42",
            "final_url": None, "saved_path": "files/answer.pdf", "downloaded": True,
            "content_type": "application/pdf", "size_bytes": 10, "sha256": "a" * 64,
            "text": attachment_text(), "error": None,
        }
        with (patch.object(crawler, "fetch", side_effect=fake_fetch),
              patch.object(crawler.cms_board, "save_attachments", return_value=[attachment]),
              patch.object(crawler, "extract_pdf_text", return_value="PDF의 답변")):
            faq, docs = crawler.collect_tuition_faq(object(), faq_url)

        self.assertEqual(requested, [faq_url, post_url])
        self.assertEqual(faq["item_count"], 1)
        self.assertIn("PDF의 답변", faq["items"][0]["content"])
        self.assertEqual(docs[0]["subcategory"], "학사정보")
        self.assertEqual(docs[0]["metadata"]["parent_page_id"], 102)
        self.assertEqual(docs[0]["attachments"][0]["text"]["status"], "success")
        self.assertEqual(validate_common_document(docs[0]), [])


if __name__ == "__main__":
    unittest.main()
