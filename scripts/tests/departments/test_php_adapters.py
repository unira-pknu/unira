import unittest

from bs4 import BeautifulSoup

from scripts.crawlers.departments.adapters import get_adapter
from scripts.crawlers.departments.discovery import discover_from_html
from scripts.crawlers.departments.probe import analyze_site_html
from scripts.crawlers.departments.redirects import follow_meta_refresh_once, meta_refresh_target
from scripts.tests._paths import FIXTURES_ROOT


FIXTURES = FIXTURES_ROOT / "departments" / "geoinfo"


GEOINFO_BASE = "http://geoinfo.pknu.ac.kr/"


class FakeResponse:
    def __init__(self, url: str, text: str):
        self.url, self.text, self.status_code = url, text, 200
        self.encoding, self.apparent_encoding = "utf-8", "utf-8"

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response, self.calls = response, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class GeoinfoLegacyPHPAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.adapter = get_adapter("legacy_php")
        cls.root = (FIXTURES / "root_meta.html").read_text(encoding="utf-8")
        cls.home = (FIXTURES / "homepage.html").read_text(encoding="utf-8")
        cls.static = (FIXTURES / "static.html").read_text(encoding="utf-8")
        cls.board = (FIXTURES / "board.html").read_text(encoding="utf-8")
        cls.detail = (FIXTURES / "detail.html").read_text(encoding="utf-8")

    def test_meta_refresh_is_same_host_http_and_followed_once(self) -> None:
        self.assertEqual(GEOINFO_BASE + "00main/main.php", meta_refresh_target(self.root, GEOINFO_BASE))
        loop = (FIXTURES / "loop_meta.html").read_text(encoding="utf-8")
        session = FakeSession(FakeResponse(GEOINFO_BASE + "00main/main.php", loop))
        response, followed = follow_meta_refresh_once(session, FakeResponse(GEOINFO_BASE, self.root), timeout=1)
        self.assertTrue(followed)
        self.assertEqual(1, len(session.calls))
        self.assertEqual(GEOINFO_BASE + "00main/main.php", response.url)

    def test_external_and_unsafe_meta_refresh_are_blocked(self) -> None:
        with self.assertRaisesRegex(ValueError, "external"):
            meta_refresh_target('<meta http-equiv="refresh" content="0;url=https://outside.example/main.php">', GEOINFO_BASE)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            meta_refresh_target('<meta http-equiv="refresh" content="0;url=javascript:alert(1)">', GEOINFO_BASE)

    def test_probe_and_discovery_distinguish_static_and_board(self) -> None:
        probe = analyze_site_html(site_key="fixture", requested_url=GEOINFO_BASE, html=self.home,
                                  final_url=GEOINFO_BASE + "00main/main.php")
        self.assertTrue(probe.compatible)
        menus = self.adapter.discover_menus(self.home, GEOINFO_BASE + "00main/main.php")
        self.assertEqual(3, len(menus))
        html = {m["url"]: self.board if "/05piazza/" in m["url"] else self.static for m in menus}
        result = discover_from_html(site_key="fixture", base_url=GEOINFO_BASE, homepage_html=self.home,
                                    section_html=html, adapter_name="legacy_php")
        sections = {section.path: section for section in result.sections}
        self.assertEqual("static_page", sections["/01about/00.php"].kind)
        self.assertEqual("board", sections["/05piazza/08.php"].kind)
        self.assertEqual("cate0501", sections["/05piazza/08.php"].bbs_id)

    def test_list_pagination_detail_and_attachment(self) -> None:
        board_url = GEOINFO_BASE + "05piazza/08.php"
        request_url, params = self.adapter.list_request(board_url, 2, "cate0501")
        self.assertEqual(board_url, request_url)
        self.assertEqual({"p": 2, "bbscode": "cate0501"}, params)
        items = self.adapter.parse_list(BeautifulSoup(self.board, "lxml"), board_url)
        self.assertEqual(["8800", "8799"], [item["source_id"] for item in items])
        self.assertEqual([True, False], [item["is_notice"] for item in items])
        parsed = self.adapter.parse_detail(BeautifulSoup(self.detail, "lxml"), items[0]["post_url"], items[0],
                                           base_url=GEOINFO_BASE, site_prefix="geoinfo")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual("8800", parsed["source_id"])
        self.assertEqual("학과사무실", parsed["author"])
        self.assertEqual("2026-08-31", parsed["date"])
        self.assertEqual("상세 본문입니다.", parsed["body"])
        self.assertEqual("http://geoinfo.pknu.ac.kr/program/amibbs/bbsExe.php?idx=4627&bbscode=cate0501&mode=down",
                         parsed["attachments"][0]["url"])

    def test_static_page_uses_page_con_and_marks_heading_only_image_content(self):
        soup = BeautifulSoup('''<div id="container"><nav>메뉴</nav><p>페이지 외부 문구</p>
          <article class="page_con"><h3>모듈형 교육과정</h3>
          <a href="../images/full.jpg"><img src="../images/preview.jpg" alt="모듈형 교육과정"></a>
          </article></div>''', 'lxml')
        parsed = self.adapter.parse_static(soup, fallback_title='교육과정',
                                           page_url=GEOINFO_BASE + '03specialty/02.php')
        self.assertNotIn('외부 문구', parsed['content'])
        self.assertEqual('image_only', parsed['content_state'])
        self.assertEqual(['../images/full.jpg'], parsed['content_images'])
        self.assertEqual('page_con', self.adapter.select_body_root(soup)['class'][0])

    def test_static_video_literal_preserves_source_without_changing_saved_dom(self):
        soup = BeautifulSoup('''<article class="page_con"><iframe id="youtubeFrame" src=""></iframe></article>
          <script>
          const youtubeFrame = document.getElementById('youtubeFrame');
          function openModal() {
            const videoId = "zwiRpmiOEKY";
            youtubeFrame.src = `https://www.youtube.com/embed/${videoId}`;
          }
          openModal();
          </script>''', 'lxml')
        parsed = self.adapter.parse_static(soup, fallback_title='학과소개 동영상',
                                           page_url=GEOINFO_BASE + '01about/05.php')
        self.assertEqual('영상: https://www.youtube.com/embed/zwiRpmiOEKY', parsed['content'])
        self.assertNotIn('content_state', parsed)
        self.assertEqual([{'url': 'https://www.youtube.com/embed/zwiRpmiOEKY', 'label': '영상',
                          'source': 'iframe', 'followed': False}], parsed['reference_links'])
        self.assertEqual('', soup.select_one('#youtubeFrame')['src'])

    def test_static_video_does_not_guess_unverified_bindings_or_ids(self):
        script = '''
          const youtubeFrame = document.getElementById('youtubeFrame');
          const videoId = "zwiRpmiOEKY";
          youtubeFrame.src = `https://www.youtube.com/embed/${videoId}`;
        '''
        cases = (
            (script, 'https://geoinfo.pknu.ac.kr.example.test/01about/05.php'),
            (script.replace("getElementById('youtubeFrame')", "getElementById('other')"), GEOINFO_BASE),
            (script.replace('"zwiRpmiOEKY"', 'getVideoId()'), GEOINFO_BASE),
            (script.replace('zwiRpmiOEKY', 'invalid'), GEOINFO_BASE),
            (script.replace('youtubeFrame.src =', '//youtubeFrame.src ='), GEOINFO_BASE),
            (script + '\nconst videoId = "abcdefghijk";', GEOINFO_BASE),
        )
        for code, url in cases:
            with self.subTest(code=code, url=url):
                soup = BeautifulSoup('<article class="page_con"><iframe id="youtubeFrame" src=""></iframe></article>'
                                     + '<script>' + code + '</script>', 'lxml')
                parsed = self.adapter.parse_static(soup, fallback_title='동영상', page_url=url)
                self.assertEqual('empty', parsed['content_state'])
                self.assertNotIn('reference_links', parsed)

    def test_static_video_existing_src_is_preserved(self):
        soup = BeautifulSoup('''<article class="page_con">
          <iframe id="youtubeFrame" src="https://www.youtube.com/embed/abcdefghijk"></iframe>
          </article>''', 'lxml')
        parsed = self.adapter.parse_static(soup, fallback_title='동영상', page_url=GEOINFO_BASE)
        self.assertEqual('https://www.youtube.com/embed/abcdefghijk', parsed['reference_links'][0]['url'])


ITC_FIX = FIXTURES_ROOT / "departments" / "itc"


ITC_BASE = "https://itc.pknu.ac.kr/html/00_main/"


class ItcHtmlPHPAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapter = get_adapter("html_php")
        cls.home = (ITC_FIX / "homepage.html").read_text(encoding="utf-8")
        cls.board = (ITC_FIX / "board.html").read_text(encoding="utf-8")
        cls.detail = (ITC_FIX / "detail.html").read_text(encoding="utf-8")
        cls.static = (ITC_FIX / "static.html").read_text(encoding="utf-8")

    def test_discovery_and_category_filter_identity(self):
        menus = self.adapter.discover_menus(self.home, ITC_BASE)
        self.assertEqual(4, len(menus))
        faculty = next(m for m in menus if m["name"] == "교수진")
        self.assertIn("category_idx=1", faculty["url"])
        html = {m["url"]: self.board if "/06/" in m["url"] else self.static for m in menus}
        result = discover_from_html(site_key="itc", base_url=ITC_BASE, homepage_html=self.home,
                                    section_html=html, adapter_name="html_php")
        self.assertEqual(4, len(result.sections))
        self.assertTrue(any(s.kind == "board" for s in result.sections))

    def test_list_uses_only_idx_as_document_id(self):
        url = "https://itc.pknu.ac.kr/html/06/01.php?category_idx=36"
        request_url, params = self.adapter.list_request(url, 3)
        self.assertEqual("https://itc.pknu.ac.kr/html/06/01.php", request_url)
        self.assertEqual({"category_idx": "36", "pagenum": 3}, params)
        items = self.adapter.parse_list(BeautifulSoup(self.board, "lxml"), url)
        self.assertEqual(["524", "523"], [x["source_id"] for x in items])
        self.assertNotEqual("36", items[0]["source_id"])

    def test_detail_body_date_and_attachment(self):
        url = "https://itc.pknu.ac.kr/html/06/01.php?mode=read&idx=524&category_idx=36"
        parsed = self.adapter.parse_detail(BeautifulSoup(self.detail, "lxml"), url,
                                           {"source_id": "524", "is_notice": True},
                                           base_url=ITC_BASE, site_prefix="itc")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual("524", parsed["source_id"])
        self.assertEqual("2026-09-02", parsed["date"])
        self.assertEqual("상세 본문입니다.", parsed["body"])
        self.assertEqual("https://itc.pknu.ac.kr/upload/file_download.php?file_idx=88",
                         parsed["attachments"][0]["url"])

    def test_faculty_cards_use_their_container_without_global_navigation_or_major_tabs(self):
        soup = BeautifulSoup('''<header><div class="container">전체 메뉴</div></header>
          <div class="container mgb_100"><ul class="tab_ul3"><li>다른 전공</li></ul>
          <h3 class="cont_title">현재 전공</h3><div><img src="/UploadFolder/050000_fix_board_professor/photo.jpg">
          <p>홍길동 교수</p><p>연구분야: 공간정보</p><a href="https://lab.example.org/">연구실</a>
          <a href="mailto:faculty@example.org">메일</a></div></div><footer>푸터</footer>''', 'lxml')
        parsed = self.adapter.parse_static(soup, fallback_title='교수진',
                                           page_url='https://itc.pknu.ac.kr/html/03/01.php?category_idx=1')
        self.assertEqual('현재 전공', parsed['title'])
        self.assertIn('홍길동 교수', parsed['content'])
        self.assertIn('faculty@example.org', parsed['content'])
        self.assertNotIn('전체 메뉴', parsed['content'])
        self.assertNotIn('다른 전공', parsed['content'])
        self.assertNotIn('푸터', parsed['content'])
        self.assertEqual('https://lab.example.org/', parsed['reference_links'][0]['url'])


if __name__ == "__main__":
    unittest.main()
