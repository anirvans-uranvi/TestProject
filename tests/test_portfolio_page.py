"""Tests for src.utils.portfolio_page's live F&O price override
(_fo_contract_key/_apply_live_fo_prices, migration 0032) -- the shared
helper load_positions applies so My Positions/My Trades/My CSP/Analyse
Trade all pick up a Dhan-provider account's live option/futures LTP
without each page wiring it up separately. The rest of this module is
either @st.cache_data-wrapped Streamlit glue or already covered by
load_live_broker_prices' own equity-LTP behavior, not retested here."""
from datetime import date

from src.models.enums import OptionType
from src.models.portfolio import PortfolioPosition
from src.utils import portfolio_page


def _position(**overrides) -> PortfolioPosition:
    defaults = dict(
        user_id="u1", portfolio_name="My Portfolio", broker="Dhan", raw_name="LEG",
        symbol="RELIANCE", expiry_date=None, strike_price=None, option_type=None,
        qty=1, avg_price=100.0, ltp=None, ltp_as_of=None,
    )
    defaults.update(overrides)
    return PortfolioPosition(**defaults)


class TestFoContractKey:
    def test_option_leg_key(self):
        p = _position(expiry_date=date(2026, 8, 27), strike_price=3000.0, option_type=OptionType.CE)
        assert portfolio_page._fo_contract_key(p) == ("RELIANCE", date(2026, 8, 27), 3000.0, "CE")

    def test_future_leg_key_uses_fut_sentinel(self):
        p = _position(expiry_date=date(2026, 8, 27), strike_price=None, option_type=None)
        assert portfolio_page._fo_contract_key(p) == ("RELIANCE", date(2026, 8, 27), 0.0, "FUT")

    def test_equity_holding_style_leg_has_no_key(self):
        # No expiry_date at all -- not an F&O leg.
        p = _position(expiry_date=None, strike_price=None, option_type=None)
        assert portfolio_page._fo_contract_key(p) is None

    def test_partially_decoded_leg_has_no_key(self):
        # option_type set but strike_price missing -- not a clean future
        # or a clean option, skip rather than guess.
        p = _position(expiry_date=date(2026, 8, 27), strike_price=None, option_type=OptionType.CE)
        assert portfolio_page._fo_contract_key(p) is None

    def test_unresolved_leg_has_no_key(self):
        p = _position(symbol=None, expiry_date=None)
        assert portfolio_page._fo_contract_key(p) is None


class TestApplyLiveFoPrices:
    def test_overrides_ltp_and_clears_ltp_as_of_on_a_live_hit(self, monkeypatch):
        p = _position(
            expiry_date=date(2026, 8, 27), strike_price=3000.0, option_type=OptionType.CE,
            ltp=40.0, ltp_as_of=date(2026, 8, 18),
        )
        monkeypatch.setattr(
            portfolio_page.snapshot_repo,
            "get_user_live_fo_prices",
            lambda client, user_id, contracts: {("RELIANCE", date(2026, 8, 27), 3000.0, "CE"): 45.5},
        )

        result = portfolio_page._apply_live_fo_prices(client=object(), user_id="u1", positions=[p])

        assert result[0].ltp == 45.5
        assert result[0].ltp_as_of is None

    def test_no_live_match_leaves_position_untouched(self, monkeypatch):
        p = _position(expiry_date=date(2026, 8, 27), strike_price=3000.0, option_type=OptionType.CE, ltp=40.0)
        monkeypatch.setattr(portfolio_page.snapshot_repo, "get_user_live_fo_prices", lambda client, user_id, contracts: {})

        result = portfolio_page._apply_live_fo_prices(client=object(), user_id="u1", positions=[p])

        assert result[0].ltp == 40.0
        assert result == [p]

    def test_equity_holding_style_position_is_not_queried(self):
        # No F&O legs at all -- confirms the function short-circuits
        # before ever calling get_user_live_fo_prices (would raise if
        # called, since it's left unmocked here).
        p = _position(expiry_date=None, strike_price=None, option_type=None)

        result = portfolio_page._apply_live_fo_prices(client=object(), user_id="u1", positions=[p])

        assert result == [p]

    def test_only_matching_legs_are_overridden_others_pass_through(self, monkeypatch):
        matched = _position(
            raw_name="MATCHED", expiry_date=date(2026, 8, 27), strike_price=3000.0, option_type=OptionType.CE, ltp=40.0
        )
        unmatched = _position(
            raw_name="UNMATCHED", symbol="TCS", expiry_date=date(2026, 8, 27), strike_price=4000.0,
            option_type=OptionType.PE, ltp=20.0,
        )
        monkeypatch.setattr(
            portfolio_page.snapshot_repo,
            "get_user_live_fo_prices",
            lambda client, user_id, contracts: {("RELIANCE", date(2026, 8, 27), 3000.0, "CE"): 45.5},
        )

        result = portfolio_page._apply_live_fo_prices(client=object(), user_id="u1", positions=[matched, unmatched])

        assert result[0].ltp == 45.5
        assert result[1].ltp == 20.0  # untouched, still the original object
        assert result[1] is unmatched
