"""Tests for fo_repo's pagination helper.

Real bug this covers: get_all_open_options() had no pagination, and
PostgREST caps a single response at a server-configured max (commonly
1000 rows) regardless of how many rows actually match the query --
against live data this silently truncated ~5,000 PE legs down to exactly
1000, and whichever symbols fell outside that window (most of the
universe, including RELIANCE/TCS/HDFCBANK) were missing entirely from the
Dashboard's "5% CSP" column with no error anywhere. Confirmed live before
the fix (`PE rows: 1000`) and after (`PE rows: 5053`, all 50 symbols
present) -- see the F&O chapter of the session transcript.
"""
import types

from src.repositories import fo_repo


class _FakeRangeQuery:
    """Mimics supabase-py's chained .select()...range(a,b).execute() --
    only .range()/.execute() are needed here since _paginate() calls the
    query-builder callable fresh for each page and applies .range() itself."""

    def __init__(self, all_rows: list[dict]):
        self.all_rows = all_rows
        self._start = 0
        self._end = 0

    def range(self, start: int, end: int):
        self._start, self._end = start, end
        return self

    def execute(self):
        return types.SimpleNamespace(data=self.all_rows[self._start : self._end + 1])


class TestPaginate:
    def test_accumulates_across_multiple_pages(self):
        all_rows = [{"id": i} for i in range(2500)]
        result = fo_repo._paginate(lambda: _FakeRangeQuery(all_rows), page_size=1000)
        assert len(result) == 2500
        assert [r["id"] for r in result] == list(range(2500))

    def test_single_page_under_page_size(self):
        all_rows = [{"id": i} for i in range(150)]
        result = fo_repo._paginate(lambda: _FakeRangeQuery(all_rows), page_size=1000)
        assert len(result) == 150

    def test_exact_multiple_of_page_size_still_terminates(self):
        # A naive "stop when page is empty" implementation is fine here,
        # but this locks in that hitting the boundary exactly (a full last
        # page) doesn't loop forever probing for more.
        all_rows = [{"id": i} for i in range(2000)]  # exactly 2 full pages
        result = fo_repo._paginate(lambda: _FakeRangeQuery(all_rows), page_size=1000)
        assert len(result) == 2000

    def test_empty_result(self):
        result = fo_repo._paginate(lambda: _FakeRangeQuery([]), page_size=1000)
        assert result == []

    def test_default_page_size_matches_the_real_postgrest_cap_that_caused_the_bug(self):
        # 1000 is the exact number that silently truncated live data before
        # this fix -- confirm the default wasn't accidentally left smaller
        # or larger by a refactor.
        all_rows = [{"id": i} for i in range(1500)]
        result = fo_repo._paginate(lambda: _FakeRangeQuery(all_rows))
        assert len(result) == 1500
