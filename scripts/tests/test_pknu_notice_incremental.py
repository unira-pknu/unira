"""pknu_notice incremental runs: page past the limit while posts are unseen,
and never archive posts that merely scrolled out of the pages read."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.crawlers import pknu_notice as pn
from scripts.crawlers.main.common.board import ListItem

CD = "10001"
LABEL = pn.CD_TO_LABEL[CD]


def board(pages, pinned=()):
    """Fake board: pages[i] lists the post numbers on page i+1 (newest first);
    `pinned` posts sit on top of every page, and like the real board every row
    parses as is_notice=True."""
    def fetch(_session, _url, params=None, delay=0):
        return SimpleNamespace(text=params["pageIndex"])

    def parse_page_indicator(text):
        return int(text), len(pages)

    def parse_list_page(text, cd, label):
        return [ListItem(no=str(no), notice_no=None, is_notice=True, list_date="", pknu_cd={cd}, categories={label})
                for no in (*pinned, *pages[int(text) - 1])]

    return patch.multiple(pn, fetch=fetch, parse_page_indicator=parse_page_indicator, parse_list_page=parse_list_page)


# 8 pages of 3 posts each: 124..101
PAGES = [[124 - 3 * p - i for i in range(3)] for p in range(8)]


class GapFillTests(unittest.TestCase):
    def test_keeps_paging_until_reaching_the_newest_known_post(self):
        # State's newest post is 109 (page 6): pages 4-5 hold new posts past the limit.
        with board(PAGES):
            index, windows = pn.collect_list_items(None, [CD], 3, False, newest_known_no={LABEL: 109})
        self.assertIn("110", index)
        self.assertEqual(107, windows[LABEL])  # stopped after page 6, where 109 shows up

    def test_stops_at_the_limit_when_nothing_is_new(self):
        with board(PAGES):
            index, windows = pn.collect_list_items(None, [CD], 3, False, newest_known_no={LABEL: 124})
        self.assertEqual(9, len(index))
        self.assertEqual(116, windows[LABEL])

    def test_old_pinned_posts_do_not_widen_the_window(self):
        with board(PAGES, pinned=(90,)):
            _, windows = pn.collect_list_items(None, [CD], 3, False, newest_known_no={LABEL: 124})
        self.assertEqual(116, windows[LABEL])

    def test_first_run_without_state_reads_only_the_limit(self):
        with board(PAGES):
            index, _ = pn.collect_list_items(None, [CD], 3, False, newest_known_no={})
        self.assertEqual(9, len(index))


    def test_sparse_state_does_not_page_to_the_cap(self):
        # Old posts missing from state are not "new": only numbers above 124 are.
        with board(PAGES):
            index, _ = pn.collect_list_items(None, [CD], 3, False, newest_known_no={LABEL: 124})
        self.assertNotIn("110", index)


class OrphanWindowTests(unittest.TestCase):
    def state(self):
        return {"items": {
            "120": {"slug": "s120", "category": LABEL, "miss_count": 0},  # inside the pages read
            "102": {"slug": "s102", "category": LABEL, "miss_count": 2},  # scrolled out long ago
        }}

    def test_only_posts_inside_the_window_count_as_missing(self):
        state = self.state()
        with patch.object(pn, "find_existing_doc", return_value=(None, None)):
            pn.process_orphans(state, seen_slugs=set(), full_resync=False, windows={LABEL: 116})
        self.assertEqual(1, state["items"]["120"]["miss_count"])
        self.assertEqual(2, state["items"]["102"]["miss_count"])  # untouched, not archived

    def test_unknown_category_is_never_counted(self):
        state = {"items": {"120": {"slug": "s120", "category": "없는분류", "miss_count": 0}}}
        pn.process_orphans(state, seen_slugs=set(), full_resync=False, windows={LABEL: 100})
        self.assertEqual(0, state["items"]["120"]["miss_count"])


if __name__ == "__main__":
    unittest.main()
