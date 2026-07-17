from src.repositories.fundamentals_repo import carry_forward_fields


def row(as_of_date, pe_ratio=None, peg_ratio=None, eps=None, market_cap=None, week_52_high=None, week_52_low=None):
    return {
        "as_of_date": as_of_date, "pe_ratio": pe_ratio, "peg_ratio": peg_ratio,
        "eps": eps, "market_cap": market_cap,
        "week_52_high": week_52_high, "week_52_low": week_52_low,
    }


class TestCarryForwardFields:
    def test_uses_latest_row_when_fully_populated(self):
        rows = [row("2026-07-14", pe_ratio=20.0, peg_ratio=1.2, eps=50.0, market_cap=1e10, week_52_high=1500.0, week_52_low=900.0)]
        result = carry_forward_fields(rows)
        assert result == {
            "pe_ratio": 20.0, "peg_ratio": 1.2, "eps": 50.0, "market_cap": 1e10,
            "week_52_high": 1500.0, "week_52_low": 900.0,
        }

    def test_falls_back_to_older_row_for_missing_field(self):
        # Newest first, matching real query order (order("as_of_date", desc=True))
        rows = [
            row("2026-07-14", pe_ratio=44.0, peg_ratio=None, eps=10.0, market_cap=5e10),
            row("2026-07-11", pe_ratio=43.0, peg_ratio=0.83, eps=9.5, market_cap=4.9e10),
        ]
        result = carry_forward_fields(rows)
        assert result["pe_ratio"] == 44.0  # from the latest row, present there
        assert result["peg_ratio"] == 0.83  # not in the latest row -- carried forward
        assert result["eps"] == 10.0
        assert result["market_cap"] == 5e10

    def test_each_field_carried_forward_independently(self):
        rows = [
            row("2026-07-14", pe_ratio=None, peg_ratio=None, eps=None, market_cap=None),
            row("2026-07-13", pe_ratio=None, peg_ratio=0.9, eps=None, market_cap=None),
            row("2026-07-12", pe_ratio=20.0, peg_ratio=None, eps=None, market_cap=None),
            row("2026-07-11", pe_ratio=None, peg_ratio=None, eps=8.0, market_cap=3e10),
        ]
        result = carry_forward_fields(rows)
        assert result == {
            "pe_ratio": 20.0, "peg_ratio": 0.9, "eps": 8.0, "market_cap": 3e10,
            "week_52_high": None, "week_52_low": None,
        }

    def test_52w_high_low_carried_forward_independently(self):
        rows = [
            row("2026-07-14", week_52_high=None, week_52_low=850.0),
            row("2026-07-11", week_52_high=1600.0, week_52_low=None),
        ]
        result = carry_forward_fields(rows)
        assert result["week_52_high"] == 1600.0
        assert result["week_52_low"] == 850.0

    def test_field_never_available_stays_none(self):
        rows = [
            row("2026-07-14", pe_ratio=20.0),
            row("2026-07-11", pe_ratio=19.0),
        ]
        result = carry_forward_fields(rows)
        assert result["peg_ratio"] is None
        assert result["eps"] is None
        assert result["market_cap"] is None

    def test_empty_rows_returns_all_none(self):
        result = carry_forward_fields([])
        assert all(v is None for v in result.values())

    def test_stops_early_once_all_fields_found(self):
        # A huge history where everything is found in the first two rows --
        # correctness check that it doesn't silently prefer a later row.
        rows = [
            row("2026-07-14", pe_ratio=20.0, peg_ratio=1.0, eps=5.0, market_cap=1e9),
        ] + [row(f"2026-06-{d:02d}", pe_ratio=999.0, peg_ratio=999.0, eps=999.0, market_cap=999.0) for d in range(1, 30)]
        result = carry_forward_fields(rows)
        assert result == {
            "pe_ratio": 20.0, "peg_ratio": 1.0, "eps": 5.0, "market_cap": 1e9,
            "week_52_high": None, "week_52_low": None,
        }
