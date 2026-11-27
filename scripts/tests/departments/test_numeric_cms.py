from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup

from scripts.crawlers.common.storage import get_dataset_paths
from scripts.crawlers.departments import engine
from scripts.crawlers.departments.adapters import get_adapter
from scripts.crawlers.departments.adapters.numeric_cms import NumericCMSAdapter, extract_body_content
from scripts.crawlers.departments.config import get_department
from scripts.crawlers.departments.discovery import analyze_section_html
from scripts.tests._paths import FIXTURES_ROOT, PROJECT_ROOT


FIXTURES = FIXTURES_ROOT / "departments"


class NumericCMSClassificationTests(unittest.TestCase):
    def _analyze(self, fixture: str, name: str):
        return analyze_section_html(
            name=name,
            page_url="https://department.example.test/dept/1234",
            html=(FIXTURES / fixture).read_text(encoding="utf-8"),
        )

    def test_view_link_collections_are_static_pages(self) -> None:
        cases = (
            ("faculty_page.html", "교수진"),
            ("member_directory.html", "구성원"),
            ("profile_list.html", "프로필"),
            ("static_gallery.html", "갤러리"),
            ("static_link_list.html", "교육과정"),
        )
        for fixture, name in cases:
            with self.subTest(fixture=fixture):
                section = self._analyze(fixture, name)
                self.assertEqual("static_page", section.kind)
                self.assertEqual("guide", section.document_type)
                self.assertEqual("candidate", section.status)
                self.assertIsNone(section.bbs_id)
                self.assertEqual([], section.warnings)

    def test_real_board_without_bbs_id_requires_an_adapter(self) -> None:
        section = self._analyze("board_without_bbs_id.html", "소식")
        self.assertEqual("board", section.kind)
        self.assertEqual("notice", section.document_type)
        self.assertEqual("requires_adapter", section.status)
        self.assertIsNone(section.bbs_id)
        self.assertIn("adapter required", section.warnings[0])

    def test_list_variants_use_action_view_document_ids(self) -> None:
        adapter = get_adapter("numeric_cms")
        cases = (
            ("numeric_cms/list_table_variant.html", ["pinned-A", "9994213"], [None, 270]),
            ("numeric_cms/list_card_variant.html", ["28554", "28343"], [None, None]),
            ("numeric_cms/list_gallery_variant.html", ["9983255", "9983254"], [None, None]),
        )
        for fixture, source_ids, post_numbers in cases:
            with self.subTest(fixture=fixture):
                soup = BeautifulSoup((FIXTURES / fixture).read_text(encoding="utf-8"), "lxml")
                items = adapter.parse_list(soup, "https://department.example.test/dept/100")
                self.assertEqual(source_ids, [item["source_id"] for item in items])
                self.assertEqual(post_numbers, [item["post_no"] for item in items])

    def test_navigation_detail_link_is_not_a_list_item(self) -> None:
        adapter = get_adapter("numeric_cms")
        soup = BeautifulSoup(
            (FIXTURES / "numeric_cms/list_table_variant.html").read_text(encoding="utf-8"),
            "lxml",
        )
        items = adapter.parse_list(soup, "https://department.example.test/dept/100")
        self.assertNotIn("9990000", {item["source_id"] for item in items})

    def test_sns_cards_keep_public_content_and_external_links_without_following(self) -> None:
        adapter = get_adapter('numeric_cms')
        soup = BeautifulSoup('''<ul class="snsList"><li>
          <a class="snsBox" onclick="snsModal('https://youtu.be/sample')">
          <div class="txtbox"><span></span><p>홍보영상</p></div></a></li>
          <li><a class="snsBox" onclick="snsModal('')"><div class="txtbox">
          <p>대학 신설</p></div></a></li></ul>''', 'lxml')
        board_url = 'https://department.example.test/dept/100'
        items = adapter.parse_list(soup, board_url)
        self.assertEqual(2, len(items))
        self.assertNotEqual(items[0]['source_id'], items[1]['source_id'])
        self.assertTrue(items[0]['post_url'].startswith(board_url + '#sns_'))
        page_one = adapter.parse_list(soup, board_url + '?pageIndex=1&bbsId=123')
        page_two = adapter.parse_list(soup, board_url + '?pageIndex=2&bbsId=123')
        self.assertEqual(page_one[0]['post_url'], page_two[0]['post_url'])
        detail = adapter.parse_detail(soup, items[0]['post_url'], items[0], base_url=board_url, site_prefix='dept')
        self.assertEqual('홍보영상', detail['body'])
        self.assertEqual([{'label': '홍보영상', 'url': 'https://youtu.be/sample', 'followed': False}], detail['reference_links'])
        self.assertIsNone(adapter.parse_detail(BeautifulSoup('<ul class="snsList"></ul>', 'lxml'), items[0]['post_url'], items[0], base_url=board_url, site_prefix='dept'))


class NumericCMSTableTests(unittest.TestCase):
    def test_embedded_media_only_body_retains_sources(self):
        adapter = NumericCMSAdapter()
        page_url = 'https://department.example.test/dept/100?action=view&no=1'
        soup = BeautifulSoup('''<div class="a_bdCont"><table><tr><td class="bdvEdit">
          <iframe src="https://www.youtube.com/embed/example" title="홍보영상"></iframe>
          <iframe src="javascript:alert(1)"></iframe>
          </td></tr></table></div>''', 'lxml')
        parsed = adapter.parse_detail(soup, page_url, {'title': '영상 안내'},
                                      base_url='https://department.example.test', site_prefix='dept')
        self.assertIn('홍보영상: https://www.youtube.com/embed/example', parsed['body'])
        self.assertNotIn('empty', parsed.get('content_state', ''))
        self.assertEqual([{'url': 'https://www.youtube.com/embed/example', 'label': '홍보영상',
                          'source': 'iframe', 'followed': False}], parsed['reference_links'])
        video = BeautifulSoup('''<main><video><source src="/usingDownload.do?no=1">
          동영상을 지원하지 않는 브라우저입니다.</source></video></main>''', 'lxml')
        self.assertIn('영상: /usingDownload.do?no=1', adapter.parse_static(video, fallback_title='안내')['content'])
        self.assertNotIn('지원하지 않는', adapter.parse_static(video, fallback_title='안내')['content'])

    def test_icon_only_email_keeps_mailto_address(self):
        soup = BeautifulSoup('<main>이메일: <a href="mailto:faculty@example.test"><img alt="메일"></a></main>', 'lxml')
        self.assertIn('faculty@example.test', extract_body_content(soup.main))

    def test_saved_ce_curriculum_fixture(self):
        fixture = FIXTURES_ROOT / 'departments/numeric_cms/ce_ai_curriculum.html'
        soup = BeautifulSoup(fixture.read_text(encoding='utf-8'), 'lxml')
        parsed = NumericCMSAdapter().parse_static(soup, fallback_title='AI')
        self.assertGreater(len(parsed['content']), 1000)
        self.assertIn('2026', parsed['content'])
        self.assertIn(' | ', parsed['content'])
        self.assertGreater(len(parsed['content'].splitlines()), 10)

    def test_static_embedded_documents_are_attachments_without_video_links(self):
        soup = BeautifulSoup('''<div id="sbCont">
          <iframe src="/upload/support.pdf"></iframe>
          <a href="/upload/support.pdf">Download PDF</a>
          <embed src="/upload/curriculum.pdf">
          <object data="/upload/guide.pdf"></object>
          <iframe src="https://www.youtube.com/embed/example"></iframe>
          <iframe src="javascript:download('ignored.pdf')"></iframe>
          </div>''', "lxml")
        parsed = NumericCMSAdapter().parse_static(soup, fallback_title="Support")
        self.assertEqual([item["url"] for item in parsed["attachments"]],
                         ["/upload/support.pdf", "/upload/curriculum.pdf", "/upload/guide.pdf"])
        self.assertIn("문서: /upload/support.pdf", parsed["content"])
        self.assertIn("영상: https://www.youtube.com/embed/example", parsed["content"])

    def test_static_table_only_guide(self):
        soup = BeautifulSoup('''<main><table><caption>교육과정</caption>
          <tr><th>학년</th><th>과목</th><th>학점</th></tr>
          <tr><td rowspan="2">1학년</td><td>프로그래밍</td><td>3</td></tr>
          <tr><td>수학</td><td>2</td></tr></table></main>''', 'lxml')
        result = NumericCMSAdapter().parse_static(soup, fallback_title='교육과정')
        self.assertIn('학년 | 과목 | 학점', result['content'])
        self.assertIn('1학년 | 프로그래밍 | 3\n1학년 | 수학 | 2', result['content'])
        self.assertIn('교육과정', result['content'])

    def test_detail_keeps_tables_but_excludes_metadata_and_navigation(self):
        soup = BeautifulSoup('''<div class="a_bdCont"><table>
          <tr><th>작성자</th><td>관리자</td></tr>
          <tr><td class="bdvEdit">신청 안내<table>
            <tr><td>기간</td><td>9월</td></tr></table>문의 바랍니다</td></tr>
          </table><div class="c_bdvNav">이전 글</div></div>''', 'lxml')
        text = extract_body_content(soup)
        self.assertIn('기간 | 9월', text)
        self.assertIn('신청 안내', text)
        self.assertIn('문의 바랍니다', text)
        self.assertNotIn('관리자', text)
        self.assertNotIn('이전 글', text)

    def test_fallback_preserves_multiple_tables_and_colspan(self):
        soup = BeautifulSoup('''<main>안내<table><tr><td colspan="2">공통</td>
          <td>선택</td></tr><tr><td>A</td><td></td><td>B</td></tr></table>
          <table><tr><td>C</td><td>D</td></tr></table><script>hidden()</script></main>''', 'lxml')
        text = extract_body_content(soup)
        self.assertIn('공통 | 공통 | 선택', text)
        self.assertIn('A | | B', text)
        self.assertIn('C | D', text)
        self.assertNotIn('hidden', text)

    def test_nested_table_content_is_not_duplicated(self):
        soup = BeautifulSoup('<main><table><tr><td>외부<table><tr><td>내부</td></tr></table></td></tr></table></main>', 'lxml')
        self.assertEqual(extract_body_content(soup).count('내부'), 1)

    def test_empty_layout_container_before_real_body(self):
        soup = BeautifulSoup('<div class="container"></div><div id="sbCont"><table><tr><td>AI</td><td>3</td></tr></table></div>', 'lxml')
        self.assertEqual(NumericCMSAdapter().parse_static(soup, fallback_title='AI')['content'], 'AI | 3')

    def test_verified_layout_prefers_a_bdcont_over_earlier_containers(self):
        adapter = NumericCMSAdapter()
        soup = BeautifulSoup('''<div class="container"></div>
          <div class="c_bdCont"><div class="bdvEdit">다른 영역</div></div>
          <div class="a_bdCont"><div class="bdvEdit">학과 안내</div></div>''', 'lxml')
        page_url = 'https://control.pknu.ac.kr/control/3867'
        guide = adapter.parse_static(soup, fallback_title='안내', page_url=page_url)
        detail = adapter.parse_detail(soup, page_url + '?action=view&no=1', {},
                                      base_url='https://control.pknu.ac.kr', site_prefix='control')
        self.assertEqual('학과 안내', guide['content'])
        self.assertEqual('학과 안내', detail['body'])

    def test_verified_c_bdcont_detail_keeps_body_and_attachments(self):
        soup = BeautifulSoup('''<div class="bdvTitle">신청 안내</div>
          <div class="c_bdCont"><table>
          <tr><th>작성자</th><td>관리자</td></tr>
          <tr><td><a class="c_bdvFile" href="/boardDownload.do?no=2">신청서.hwp</a></td></tr>
          <tr><td class="bdvEdit">신청 본문<table><tr><td>기간</td><td>9월</td></tr></table></td></tr>
          </table><div class="c_bdvNav">이전 글</div></div>''', 'lxml')
        parsed = NumericCMSAdapter().parse_detail(
            soup, 'https://control.pknu.ac.kr/control/6229?action=view&no=1', {},
            base_url='https://control.pknu.ac.kr', site_prefix='control',
        )
        self.assertEqual('신청 안내', parsed['title'])
        self.assertIn('신청 본문', parsed['body'])
        self.assertIn('기간 | 9월', parsed['body'])
        self.assertNotIn('관리자', parsed['body'])
        self.assertNotIn('이전 글', parsed['body'])
        self.assertEqual([{'name': '신청서.hwp', 'url': 'https://control.pknu.ac.kr/boardDownload.do?no=2'}],
                         parsed['attachments'])

    def test_verified_c_bdcont_retains_empty_body_evidence(self):
        adapter = NumericCMSAdapter()
        for body, state in [('<div class="bdvEdit"><img src="/poster.png"></div>', 'image_only'),
                            ('<div class="bdvEdit"></div><a href="/download/form.pdf">신청서.pdf</a>', 'attachment_only')]:
            with self.subTest(state=state):
                soup = BeautifulSoup(f'<div class="c_bdCont">{body}</div>', 'lxml')
                parsed = adapter.parse_detail(
                    soup, 'https://fashion.pknu.ac.kr/fashion/2129?action=view&no=1', {},
                    base_url='https://fashion.pknu.ac.kr', site_prefix='fashion',
                )
                self.assertEqual(state, parsed['content_state'])
                if state == 'image_only':
                    self.assertEqual(['/poster.png'], parsed['content_images'])
                else:
                    self.assertEqual(1, len(parsed['attachments']))

    def test_configured_department_layout_applies_to_shared_cms_links(self):
        adapter = NumericCMSAdapter()
        soup = BeautifulSoup('''<div class="bdvTitle">학과 공지</div>
          <div class="c_bdCont"><div class="bdvEdit"><img src="/poster.png"></div>
          <a href="/boardDownload.do?no=2">신청서.hwp</a></div>''', 'lxml')
        page_url = 'https://icms.pknu.ac.kr/semiconductor/5517?action=view&no=1'
        parsed = adapter.parse_detail(soup, page_url, {},
                                      base_url='https://semiconductor.pknu.ac.kr', site_prefix='semiconductor')
        self.assertEqual('attachment_only', parsed['content_state'])
        self.assertEqual(['/poster.png'], parsed['content_images'])
        self.assertEqual('https://icms.pknu.ac.kr/boardDownload.do?no=2', parsed['attachments'][0]['url'])
        guide = adapter.parse_static(
            BeautifulSoup('<div class="container"></div><div class="a_bdCont">학과 안내</div>', 'lxml'),
            fallback_title='안내', page_url=page_url, base_url='https://semiconductor.pknu.ac.kr',
        )
        self.assertEqual('학과 안내', guide['content'])

    def test_alternate_layout_is_limited_to_verified_hosts(self):
        adapter = NumericCMSAdapter()
        soup = BeautifulSoup('<div class="bdvTitle">안내</div><div class="c_bdCont"><div class="bdvEdit">다른 영역</div></div>', 'lxml')
        for host in ['ce.pknu.ac.kr', 'control.pknu.ac.kr.example.test']:
            with self.subTest(host=host):
                parsed = adapter.parse_detail(soup, f'https://{host}/dept/100?action=view&no=1', {},
                                              base_url=f'https://{host}', site_prefix='dept')
                self.assertEqual('', parsed['body'])

    def test_verified_static_page_without_layout_keeps_existing_fallback(self):
        soup = BeautifulSoup('<div class="container"></div><div id="sbCont">기존 안내</div>', 'lxml')
        parsed = NumericCMSAdapter().parse_static(
            soup, fallback_title='안내', page_url='https://control.pknu.ac.kr/control/3867',
        )
        self.assertEqual('기존 안내', parsed['content'])

    def test_empty_body_states(self):
        from scripts.crawlers.departments.engine import validate_body_content, CrawlDiagnostics
        for state, status, failed in [('empty', 'failed', True), ('image_only', 'partial_success', False), ('attachment_only', 'success', False)]:
            with self.subTest(state=state):
                doc = {'content': '', 'metadata': {}, 'crawl': {'warnings': []}, 'source_id': 'test', 'url': 'https://example.test/page'}
                diagnostics = CrawlDiagnostics()
                self.assertEqual(validate_body_content(doc, {'content_state': state}, diagnostics), failed)
                self.assertEqual(doc['crawl']['status'], status)
                self.assertTrue(doc['crawl']['warnings'])
                self.assertEqual(bool(diagnostics.errors), state != 'attachment_only')

    def test_image_only_static(self):
        parsed = NumericCMSAdapter().parse_static(BeautifulSoup('<div class="container"></div><div id="sbCont"><h3 id="subTitle">guide</h3><img src="/guide.png"></div>', 'lxml'), fallback_title='guide')
        self.assertEqual(parsed['content_state'], 'image_only')

    def test_attachment_only_detail(self):
        parsed = NumericCMSAdapter().parse_detail(BeautifulSoup('<div class="bdvTitle">title</div><div class="a_bdCont"><div class="bdvEdit"></div><a href="/download/a.pdf">a.pdf</a></div>', 'lxml'), 'https://example.test/1', {}, base_url='https://example.test', site_prefix='ce')
        self.assertEqual(parsed['content_state'], 'attachment_only')

    def test_file_named_cms_post_is_preserved_as_a_reference(self):
        url='https://cms.pknu.ac.kr/dept/view.do?idx=1&view=view'
        soup=BeautifulSoup(f'<div class="bdvTitle">안내</div><div class="a_bdCont"><div class="bdvEdit"><a href="{url}">공고.hwp</a></div></div>','lxml')
        parsed=NumericCMSAdapter().parse_detail(soup,'https://ce.pknu.ac.kr/ce/1814?action=view&no=1',{},
                                              base_url='https://ce.pknu.ac.kr',site_prefix='ce')
        self.assertEqual(parsed['attachments'],[])
        self.assertEqual(parsed['reference_links'][0]['url'],url)
        self.assertEqual(parsed['reference_links'][0]['label'],'공고.hwp')
        self.assertFalse(parsed['reference_links'][0]['followed'])

    def test_static_relative_attachments_are_kept(self):
        html = ('<div class="container"></div><div id="sbCont"><p>guide</p>'
                '<a href="/upload/ce/guide.pdf">guide.pdf</a><a href="../files/map.hwp">map.hwp</a>'
                '<a href="https://ce.pknu.ac.kr/upload/abs.pdf">abs.pdf</a></div>')
        parsed = NumericCMSAdapter().parse_static(
            BeautifulSoup(html, 'lxml'), fallback_title='guide',
            page_url='https://ce.pknu.ac.kr/ce/4945', base_url='https://ce.pknu.ac.kr', site_prefix='ce',
        )
        self.assertEqual(
            ['https://ce.pknu.ac.kr/upload/ce/guide.pdf', 'https://ce.pknu.ac.kr/files/map.hwp',
             'https://ce.pknu.ac.kr/upload/abs.pdf'],
            [attachment['url'] for attachment in parsed['attachments']],
        )


WORKSPACE_ROOT = PROJECT_ROOT


FIXTURE_ROOT = FIXTURES_ROOT / "departments" / "numeric_cms"


class NumericCMSStaticLayoutTests(unittest.TestCase):
    def setUp(self):
        self.adapter = NumericCMSAdapter()
        self.url = 'https://control.pknu.ac.kr/control/3868'

    def test_b_and_d_templates_keep_tables_and_exclude_page_chrome(self):
        for wrapper in ('b_bdCont', 'd_sbCont'):
            with self.subTest(wrapper=wrapper):
                soup = BeautifulSoup(f'''<header><div class="container">전체 메뉴</div></header>
                  <div id="sub"><div class="container">
                  <aside>학과 메뉴</aside><div class="{wrapper}">
                  <h3 class="d_subTitle">교육과정</h3>
                  <table><tr><th>교과목</th><th>학점</th></tr><tr><td>설계</td><td>3</td></tr></table>
                  <div style="display:none"><table><tr><td>심화</td><td>2</td></tr></table></div>
                  </div></div></div><footer>푸터</footer>''', 'lxml')
                parsed = self.adapter.parse_static(soup, fallback_title='교육과정', page_url=self.url)
                self.assertEqual('교과목 | 학점\n설계 | 3\n심화 | 2', parsed['content'])
                self.assertIs(soup.select_one('.' + wrapper), self.adapter.select_body_root(soup, page_url=self.url))
                self.assertNotIn('content_state', parsed)

    def test_verified_direct_sub_container_skips_outer_sidebar_wrapper(self):
        soup = BeautifulSoup('''<header><div class="container">전체 메뉴</div></header>
          <div id="sub"><div class="container"><div id="a_sbLft">학과 메뉴</div>
          <div id="sub"><div class="container"><h3 id="subTitle">졸업요건</h3>
          <p>이수학점 안내</p><table><tr><td>전공</td><td>60</td></tr></table>
          </div></div></div></div><footer>푸터</footer>''', 'lxml')
        parsed = self.adapter.parse_static(soup, fallback_title='졸업요건', page_url=self.url)
        self.assertIn('이수학점 안내', parsed['content'])
        self.assertIn('전공 | 60', parsed['content'])
        for chrome in ('전체 메뉴', '학과 메뉴', '푸터', '졸업요건'):
            self.assertNotIn(chrome, parsed['content'])

    def test_custom_sub_fallback_uses_configured_host_and_retains_empty_body(self):
        soup = BeautifulSoup('''<div class="container">메뉴</div>
          <div id="sub"><div class="container"></div></div>''', 'lxml')
        root = self.adapter.select_body_root(soup, page_url='https://icms.pknu.ac.kr/control/3868',
                                             base_url='https://control.pknu.ac.kr')
        self.assertIs(soup.select_one('#sub > .container'), root)
        parsed = self.adapter.parse_static(soup, fallback_title='안내', page_url=self.url)
        self.assertEqual('', parsed['content'])
        self.assertEqual('empty', parsed['content_state'])
        self.assertIs(soup.select_one('.container'), self.adapter.select_body_root(
            soup, page_url='https://control.pknu.ac.kr.example.test/control/3868'))

    def test_standard_body_retains_priority_over_alternate_layouts(self):
        soup = BeautifulSoup('''<div class="b_bdCont">다른 영역</div>
          <div class="d_sbCont">다른 영역</div><div id="sbCont">기존 안내</div>''', 'lxml')
        parsed = self.adapter.parse_static(soup, fallback_title='안내', page_url=self.url)
        self.assertEqual('기존 안내', parsed['content'])

    def test_new_body_roots_preserve_image_only_evidence_and_image_scope(self):
        from scripts.crawlers.common.body_images import collect_body_images
        for wrapper in ('<div class="b_bdCont">{}</div>', '<div class="d_sbCont">{}</div>',
                        '<div id="sub"><div class="container">{}</div></div>'):
            with self.subTest(wrapper=wrapper):
                body = '<h3>교육과정</h3><a href="/upload/full.png"><img data-src="/upload/preview.png"></a>'
                soup = BeautifulSoup('<header><div class="container"><img src="/upload/header.png"></div></header>'
                                     + wrapper.format(body) + '<footer><img src="/upload/footer.png"></footer>', 'lxml')
                parsed = self.adapter.parse_static(soup, fallback_title='교육과정', page_url=self.url)
                self.assertEqual('image_only', parsed['content_state'])
                self.assertEqual(['/upload/full.png'], parsed['content_images'])
                root = self.adapter.select_body_root(soup, page_url=self.url)
                self.assertEqual(['/upload/full.png'], [image['src'] for image in collect_body_images(soup, root=root)])

    def test_b_and_d_faculty_roots_expose_only_body_profile_links(self):
        for wrapper in ('b_bdCont', 'd_sbCont'):
            with self.subTest(wrapper=wrapper):
                soup = BeautifulSoup(f'''<div class="container"><a href="?action=view&amp;no=9">상세보기</a></div>
                  <div class="{wrapper}"><div class="tch_list"><h3 class="a_tch_name">교수 A</h3>
                  <p>연구분야: 냉동공학</p><a href="?action=view&amp;no=1">상세보기</a></div></div>''', 'lxml')
                links = self.adapter.static_detail_links(soup, page_url=self.url)
                self.assertEqual([self.url + '?action=view&no=1'], [link['url'] for link in links])
                self.assertIn('연구분야: 냉동공학', self.adapter.parse_static(
                    soup, fallback_title='교수진', page_url=self.url)['content'])

    def test_heading_language_links_are_content_without_following_targets(self):
        soup = BeautifulSoup('''<div id="sbCont"><h3 id="subTitle">유학생활 가이드북</h3>
          <h4><a href="https://oia.pknu.ac.kr/E-book/kor/index.html">한국어</a>
          <a href="https://oia.pknu.ac.kr/E-book/eng/index.html">English</a></h4></div>''', 'lxml')
        parsed = self.adapter.parse_static(soup, fallback_title='가이드북',
                                           page_url='https://gic.pknu.ac.kr/gic/8917')
        self.assertNotIn('content_state', parsed)
        self.assertEqual('한국어 English', parsed['content'])
        self.assertEqual(2, len(parsed['reference_links']))
        self.assertTrue(all(link['followed'] is False for link in parsed['reference_links']))

    def test_heading_navigation_links_do_not_make_empty_pages_successful(self):
        for href in ('#', 'javascript:openBook()', 'mailto:test@example.test', 'https://[invalid'):
            with self.subTest(href=href):
                soup = BeautifulSoup(f'<div id="sbCont"><h4><a href="{href}">안내</a></h4></div>', 'lxml')
                parsed = self.adapter.parse_static(soup, fallback_title='안내', page_url=self.url)
                self.assertEqual('empty', parsed['content_state'])


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class DepartmentNumericCmsRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.expected = json.loads((FIXTURE_ROOT / "golden_expected.json").read_text(encoding="utf-8"))
        cls.list_html = (FIXTURE_ROOT / "list.html").read_text(encoding="utf-8")
        cls.detail_html = (FIXTURE_ROOT / "detail.html").read_text(encoding="utf-8")
        cls.static_html = (FIXTURE_ROOT / "static.html").read_text(encoding="utf-8")
        engine.configure_department(get_department("ce"))
        engine.log.disabled = True

    @classmethod
    def tearDownClass(cls) -> None:
        engine.log.disabled = False


    def test_curriculum_output_keeps_source_type_metadata(self) -> None:
        captured: list[dict] = []
        section = next(item for item in engine.SECTIONS if item["id"] == "curriculum")
        with (
            patch.object(engine, "fetch", return_value=FakeResponse(self.static_html)),
            patch.object(engine, "save_document", side_effect=lambda doc, _html: captured.append(doc)),
        ):
            stats = engine.crawl_static(object(), section)
        self.assertEqual(0, stats.failed)
        self.assertEqual("static_page", captured[0]["type"])
        self.assertEqual("curriculum", captured[0]["metadata"]["source_type"])

    def test_sns_board_sample_limit_does_not_fetch_external_content(self) -> None:
        html = '''<ul class="snsList"><li><a class="snsBox" onclick="snsModal('https://youtu.be/sample')">
          <div class="txtbox"><p>영상 안내</p></div></a></li><li>
          <a class="snsBox" onclick="snsModal('')"><div class="txtbox"><p>대학 소개</p></div></a></li></ul>'''
        captured = []
        board = {'url': 'https://department.example.test/dept/100', 'bbs_id': '123',
                 'name': '뉴스', 'category': '커뮤니티', 'type': 'notice'}
        with (
            patch.object(engine, 'fetch', return_value=FakeResponse(html)) as fetch,
            patch.object(engine, 'save_document', side_effect=lambda doc, _: captured.append(doc)),
            patch.object(engine, 'load_existing_attachments', return_value=[]),
        ):
            stats, _ = engine.crawl_board(object(), board, {'items': {}}, is_initial=True,
                                          recent_only=1, max_items=1, no_download_files=True)
        self.assertEqual(1, stats.requested)
        self.assertEqual(0, stats.failed)
        self.assertEqual(1, len(captured))
        self.assertEqual('영상 안내', captured[0]['content'])
        self.assertEqual('https://youtu.be/sample', captured[0]['reference_links'][0]['url'])
        self.assertTrue(all(call.args[1].startswith(board['url']) for call in fetch.call_args_list))

    def test_list_parser_matches_numeric_cms_golden_result(self) -> None:
        items = engine.parse_list_page(
            BeautifulSoup(self.list_html, "lxml"),
            self.expected["list_item"]["post_url"].split("?", 1)[0],
        )
        self.assertEqual([self.expected["list_item"]], items)

    def test_detail_parser_matches_numeric_cms_golden_result(self) -> None:
        item = self.expected["list_item"]
        detail = engine.parse_view_page(
            BeautifulSoup(self.detail_html, "lxml"), item["post_url"], item
        )
        self.assertIsNotNone(detail)
        detail.pop("slug", None)
        self.assertEqual(self.expected["detail"], detail)

    def test_full_board_result_matches_legacy_document_contract(self) -> None:
        captured: list[dict] = []

        def fake_fetch(_session, _url, **kwargs):
            return FakeResponse(self.list_html if kwargs.get("params") else self.detail_html)

        def capture_document(doc: dict, _raw_html: str) -> None:
            captured.append(doc)

        temp_paths = get_dataset_paths(WORKSPACE_ROOT / ".test-output-do-not-create", "ce")
        with (
            patch.object(engine, "PATHS", temp_paths),
            patch.object(engine, "OUTPUT_JSON", temp_paths.json),
            patch.object(engine, "OUTPUT_HTML", temp_paths.html),
            patch.object(engine, "OUTPUT_FILES", temp_paths.files),
            patch.object(engine, "fetch", side_effect=fake_fetch),
            patch.object(engine, "load_existing_attachments", return_value=[]),
            patch.object(engine, "save_attachments", return_value=[]),
            patch.object(engine, "save_document", side_effect=capture_document),
            patch.object(engine, "CRAWL_ALL_BOARD_PAGES", False),
            patch.object(engine, "INITIAL_MAX_PAGES", 1),
            patch.object(engine, "REUSE_EXISTING_ATTACHMENTS", False),
        ):
            board = next(
                section for section in engine.SECTIONS
                if section["url"] == self.expected["list_item"]["post_url"].split("?", 1)[0]
            )
            stats, newest = engine.crawl_board(object(), board, {"items": {}}, is_initial=True)

        self.assertEqual(123, newest)
        self.assertEqual(1, stats.discovered)
        self.assertEqual(1, stats.requested)
        self.assertEqual(1, stats.new)
        self.assertEqual(0, stats.failed)
        self.assertEqual(1, len(captured))
        document = captured[0]
        expected = self.expected["document"]
        self.assertEqual(expected, {key: document[key] for key in expected})

    def test_static_page_result_matches_legacy_document_contract(self) -> None:
        captured: list[dict] = []

        temp_paths = get_dataset_paths(WORKSPACE_ROOT / ".test-output-do-not-create", "ce")
        with (
            patch.object(engine, "PATHS", temp_paths),
            patch.object(engine, "OUTPUT_JSON", temp_paths.json),
            patch.object(engine, "OUTPUT_HTML", temp_paths.html),
            patch.object(engine, "OUTPUT_FILES", temp_paths.files),
            patch.object(engine, "fetch", return_value=FakeResponse(self.static_html)),
            patch.object(
                engine, "save_document",
                side_effect=lambda doc, _raw_html: captured.append(doc),
            ),
        ):
            static_page = next(
                section for section in engine.SECTIONS
                if section["url"] == self.expected["static_document"]["url"]
            )
            stats = engine.crawl_static(object(), static_page)

        self.assertEqual(1, stats.discovered)
        self.assertEqual(1, stats.requested)
        self.assertEqual(1, stats.new)
        self.assertEqual(1, len(captured))
        expected = self.expected["static_document"]
        self.assertEqual(expected, {key: captured[0][key] for key in expected})


class DepartmentCoreContentTests(unittest.TestCase):
    def setUp(self):
        engine.configure_department(get_department('physics'))
        self.adapter = NumericCMSAdapter()
        self.url = 'https://physics.pknu.ac.kr/physics/303'
        self.section = {'id': 'faculty', 'name': '교수진', 'category': '교수진',
                        'type': 'guide', 'is_board': False, 'url': self.url}
        self.list_html = '''<div id="sbCont"><h3 id="subTitle">교수진</h3>
          <div class="tch_list"><h3 class="a_tch_name">교수 A</h3><p>연구분야: 물리학</p>
          <a href="?action=view&amp;no=1">상세보기</a>
          <a href="?no=1&amp;action=view">상세보기</a></div>
          <div class="tch_list"><h3>교수 B</h3><a href="?action=view&amp;no=2">상세보기</a></div>
          <a href="https://lab.example.org/">연구실 홈페이지</a></div>'''
        self.profile_html = '''<div id="sbCont"><h3 id="subTitle">교수진</h3>
          <div class="a_bdCont"><div class="tch_in"><h3 class="a_tch_name">교수 A</h3>
          <a href="mailto:faculty@example.org">메일</a></div>
          <ul class="tch_tab"><li>탭 이동</li></ul>
          <table><tr><th>학력</th><td class="bdvEdit">박사</td></tr></table>
          <div style="display:none"><table><tr><th>연구분야</th><td>응용물리학</td></tr></table></div>
          </div></div>'''

    def test_english_and_stance_roots_override_empty_layout_and_keep_tables(self):
        for html in (
            '<div id="sbCont"></div><div class="e_sbCont"><dl class="e_sbtNav">메뉴 문구</dl>'
            '<div id="sub"><table><tr><td>교과목</td><td>학점</td></tr></table></div></div>',
            '<div class="container">메뉴 문구</div><div id="stance"><table><tr><td>교과목</td><td>학점</td></tr></table></div>',
        ):
            with self.subTest(html=html):
                parsed = self.adapter.parse_static(BeautifulSoup(html, 'lxml'), fallback_title='교육과정')
                self.assertIn('교과목 | 학점', parsed['content'])
                self.assertNotIn('메뉴 문구', parsed['content'])

    def test_heading_and_lazy_image_remain_partial_content_evidence(self):
        soup = BeautifulSoup('<div id="stance"><h3>교육과정</h3><img data-src="/curriculum.png"></div>', 'lxml')
        parsed = self.adapter.parse_static(soup, fallback_title='교육과정')
        self.assertEqual('image_only', parsed['content_state'])
        self.assertEqual(['/curriculum.png'], parsed['content_images'])
        doc = {'url': self.url, 'source_id': 'static:test', 'content': parsed['content'],
               'metadata': {}, 'crawl': {'warnings': []}}
        diagnostics = engine.CrawlDiagnostics()
        self.assertFalse(engine.validate_body_content(doc, parsed, diagnostics))
        self.assertEqual('partial_success', doc['crawl']['status'])
        self.assertEqual('IMAGE_ONLY_REQUIRES_OCR', diagnostics.errors[0]['code'])

    def test_only_same_menu_profile_links_are_selected_and_deduplicated(self):
        html = self.list_html.replace('</div>', '''<a href="https://outside.example.org/physics/303?action=view&amp;no=3">상세보기</a>
          <a href="/physics/999?action=view&amp;no=4">상세보기</a>
          <a href="javascript:profile(5)">상세보기</a></div>''', 1)
        links = self.adapter.static_detail_links(BeautifulSoup(html, 'lxml'), page_url=self.url)
        self.assertEqual([self.url + '?action=view&no=1', self.url + '?action=view&no=2'], [link['url'] for link in links])

    def test_profile_body_has_person_title_and_rejects_a_returned_list(self):
        parsed = self.adapter.parse_profile(BeautifulSoup(self.profile_html, 'lxml'), fallback_title='교수진',
                                             page_url=self.url + '?action=view&no=1', base_url=self.url, site_prefix='physics')
        self.assertEqual('교수 A', parsed['title'])
        self.assertIn('학력 | 박사', parsed['content'])
        self.assertIn('연구분야 | 응용물리학', parsed['content'])
        self.assertIn('faculty@example.org', parsed['content'])
        self.assertNotIn('탭 이동', parsed['content'])
        self.assertIsNone(self.adapter.parse_profile(BeautifulSoup(self.list_html, 'lxml'), fallback_title='교수진',
                                                     page_url=self.url + '?action=view&no=1', base_url=self.url, site_prefix='physics'))

    def test_profile_binds_each_tab_heading_to_its_referenced_panel(self):
        html = '''<div id="sbCont"><div class="tch_in"><h3 class="a_tch_name">교수 A</h3></div>
          <ul class="tch_tab">
            <li><a onclick="fn_tab_ch(this,3);">주연구분야</a></li>
            <li><a href="#tab4">강의과목</a></li>
            <li><a href="#tab9">없는 본문</a></li>
          </ul>
          <div id="tab4" class="tab"><table><tr><td>질적연구 / 만성질환간호</td></tr></table></div>
          <div id="tab3" class="tab" style="display:none">성인간호학 / 노인간호학</div>
        </div>'''
        parsed = self.adapter.parse_profile(BeautifulSoup(html, 'lxml'), fallback_title='교수진',
                                            page_url=self.url + '?action=view&no=1', base_url=self.url, site_prefix='physics')
        self.assertIn('주연구분야:\n성인간호학 / 노인간호학', parsed['content'])
        self.assertIn('강의과목:\n질적연구 / 만성질환간호', parsed['content'])
        self.assertNotIn('없는 본문', parsed['content'])

    def test_profile_redirect_scope_requires_the_same_person_and_menu(self):
        requested = self.url + '?action=view&no=1'
        self.assertTrue(self.adapter.profile_target_allowed('https://icms.pknu.ac.kr/physics/303?no=1&action=view', requested))
        for target in ('https://outside.example.org/physics/303?action=view&no=1',
                       self.url + '?action=view&no=2', 'https://icms.pknu.ac.kr/other/303?action=view&no=1'):
            self.assertFalse(self.adapter.profile_target_allowed(target, requested))

    def test_external_profile_redirect_is_not_requested(self):
        requested = self.url + '?action=view&no=1'
        response = SimpleNamespace(status_code=302, url=requested,
                                   headers={'Location': 'https://outside.example.org/profile'}, close=lambda: None)
        with (patch.object(engine, 'get_with_tls_policy', return_value=(response, False)) as request,
              patch.object(engine.time, 'sleep')):
            result = engine.fetch(object(), requested,
                                  redirect_validator=lambda target: self.adapter.profile_target_allowed(target, requested))
        self.assertIsNone(result)
        self.assertEqual(1, request.call_count)
        self.assertFalse(request.call_args.kwargs['allow_redirects'])

    def test_profile_limit_duplicate_visits_and_list_provenance(self):
        captured = []
        visited = set()
        responses = [SimpleNamespace(text=self.list_html, url=self.url),
                     SimpleNamespace(text=self.profile_html, url=self.url + '?action=view&no=1')]
        with (patch.object(engine, 'fetch', side_effect=responses) as fetch,
              patch.object(engine, 'collect_document_images') as images,
              patch.object(engine, 'existing_document_path', return_value=SimpleNamespace(exists=lambda: False)),
              patch.object(engine, 'save_document', side_effect=lambda doc, _: captured.append(doc))):
            stats = engine.crawl_static(object(), self.section, max_items=2, no_download_files=True, visited_urls=visited)
            skipped = engine.crawl_static(object(), {**self.section, 'url': self.url + '?no=1&action=view'},
                                           visited_urls=visited)
        self.assertEqual(2, fetch.call_count)
        self.assertEqual(2, stats.requested)
        self.assertEqual(2, stats.new)
        self.assertEqual(0, stats.failed)
        self.assertEqual(1, skipped.skipped)
        self.assertTrue(all(not call.kwargs['download'] for call in images.call_args_list))
        profile, parent = captured
        self.assertEqual(self.url, profile['metadata']['profile_list_url'])
        self.assertEqual('collected', parent['metadata']['faculty_profiles'][0]['status'])
        self.assertEqual('item_limit', parent['metadata']['faculty_profiles'][1]['reason'])
        self.assertEqual('partial_success', parent['crawl']['status'])
        self.assertTrue(any(link['followed'] for link in parent['reference_links']))
        self.assertFalse(next(link for link in parent['reference_links'] if link['url'].startswith('https://lab.'))['followed'])

    def test_returned_list_does_not_replace_an_existing_profile(self):
        url = self.url + '?action=view&no=1'
        diagnostics = engine.CrawlDiagnostics()
        with (patch.object(engine, 'fetch', return_value=SimpleNamespace(text=self.list_html, url=url)),
              patch.object(engine, 'save_document') as save):
            stats = engine.crawl_static(object(), {**self.section, 'url': url}, diagnostics=diagnostics)
        self.assertEqual(1, stats.failed)
        save.assert_not_called()
        self.assertEqual('PROFILE_PARSE_FAILED', diagnostics.errors[0]['code'])

    def test_body_images_use_the_adapter_root_and_honor_metadata_only_mode(self):
        soup = BeautifulSoup('<header><img src="header.png"></header><article class="page_con"><img src="diagram.png"></article>', 'lxml')
        doc = {'url': 'http://geoinfo.pknu.ac.kr/03specialty/02.php', 'metadata': {}, 'crawl': {'warnings': []}}
        with patch.object(engine, 'ACTIVE_ADAPTER', get_adapter('legacy_php')):
            engine.collect_document_images(doc, soup, object(), download=False)
        self.assertEqual(['http://geoinfo.pknu.ac.kr/03specialty/diagram.png'],
                         [record['source_url'] for record in doc['metadata']['body_images']])
        self.assertEqual('not_attempted', doc['metadata']['body_images'][0]['status'])

    def test_board_images_use_the_body_of_each_supported_adapter(self):
        cases = {
            'query_view_do': '<div class="editor-data-box"><img src="body.png"></div>',
            'query_view_legacy': '<div class="board-view-wrap"><table><thead><tr><td><img src="author.png"></td></tr></thead>'
                                 '<tbody><tr><td><a class="file-add"><img src="attachment.png"></a></td></tr>'
                                 '<tr><td><img src="body.png"></td></tr></tbody></table></div>',
            'query_mcode': '<div class="boardReadBody"><div class="boardContents"><img src="body.png"></div></div>',
            'html_php': '<div class="board_read"><div class="readcontent"><img src="body.png"></div></div>',
            'legacy_php': '<table class="write"><tr><th>작성자</th><td><img src="author.png"></td></tr>'
                          '<tr><td colspan="2"><img src="body.png"></td></tr></table>',
        }
        for adapter, html in cases.items():
            with self.subTest(adapter=adapter):
                soup = BeautifulSoup('<header><img src="header.png"></header>' + html, 'lxml')
                doc = {'url': 'https://example.org/posts/1', 'metadata': {}, 'crawl': {'warnings': []}}
                with patch.object(engine, 'ACTIVE_ADAPTER', get_adapter(adapter)):
                    engine.collect_document_images(doc, soup, object(), download=False, detail=True)
                self.assertEqual(['https://example.org/posts/body.png'],
                                 [record['source_url'] for record in doc['metadata']['body_images']])

    def test_static_links_outside_faculty_category_are_not_followed(self):
        with (patch.object(engine, 'fetch', return_value=SimpleNamespace(text=self.list_html, url=self.url)) as fetch,
              patch.object(engine, 'collect_document_images'),
              patch.object(engine, 'save_document'),
              patch.object(engine, 'existing_document_path', return_value=SimpleNamespace(exists=lambda: False))):
            stats = engine.crawl_static(object(), {**self.section, 'category': '교육과정'})
        self.assertEqual(1, fetch.call_count)
        self.assertEqual(1, stats.requested)

    def test_attachment_only_text_check_does_not_hide_an_image_download_failure(self):
        doc = {'url': self.url, 'source_id': 'static:images', 'content': '', 'metadata': {}, 'crawl': {'warnings': []}}
        soup = BeautifulSoup('<div id="stance"><img src="/diagram.png"></div>', 'lxml')
        diagnostics = engine.CrawlDiagnostics()
        with patch('scripts.crawlers.common.body_images.save_body_images', return_value=[{'status': 'failed'}]):
            engine.collect_document_images(doc, soup, object(), diagnostics=diagnostics)
        engine.validate_body_content(doc, {'content_state': 'attachment_only'}, diagnostics)
        self.assertEqual('partial_success', doc['crawl']['status'])
        self.assertEqual('BODY_IMAGE_DOWNLOAD_FAILED', diagnostics.errors[0]['code'])


if __name__ == "__main__":
    unittest.main()
