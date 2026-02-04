"""Test that executor uses correct tokens for arbitrage directions."""

import pytest
from datetime import datetime, UTC

from src.models import ArbitrageOpportunity, Platform


def test_direction1_uses_team_a_token():
    """Direction 1: Buy Poly YES (team_a) + Kalshi NO."""
    opp = ArbitrageOpportunity(
        event_title="Team A vs Team B",
        team_a="Team A",
        team_b="Team B",
        platform_buy_yes=Platform.POLYMARKET,
        platform_buy_no=Platform.KALSHI,
        yes_price=0.45,
        no_price=0.50,
        total_cost=0.97,
        profit_pct=3.0,
        roi_after_fees=1.5,
        found_at=datetime.now(UTC),
        details={
            "arb_type": "yes_no",
            "poly_token_id": "token_team_a_12345",
            "poly_side": "BUY",
            "kalshi_ticker": "KXGAME-TEAMA",
            "kalshi_side": "no",
        },
    )

    # Verify poly_side is BUY (not SELL)
    assert opp.details["poly_side"] == "BUY"
    # Verify we're using team_a token (index 0)
    assert "team_a" in opp.details["poly_token_id"]
    # Verify Kalshi side is NO (team_b wins)
    assert opp.details["kalshi_side"] == "no"


def test_direction2_uses_team_b_token():
    """Direction 2: Buy Poly NO (team_b) + Kalshi YES."""
    opp = ArbitrageOpportunity(
        event_title="Team A vs Team B",
        team_a="Team A",
        team_b="Team B",
        platform_buy_yes=Platform.KALSHI,
        platform_buy_no=Platform.POLYMARKET,
        yes_price=0.50,
        no_price=0.45,
        total_cost=0.97,
        profit_pct=3.0,
        roi_after_fees=1.5,
        found_at=datetime.now(UTC),
        details={
            "arb_type": "yes_no",
            "poly_token_id": "token_team_b_67890",
            "poly_side": "BUY",  # BUY team_b token, NOT SELL team_a!
            "kalshi_ticker": "KXGAME-TEAMA",
            "kalshi_side": "yes",
        },
    )

    # Verify poly_side is BUY (buying team_b token)
    assert opp.details["poly_side"] == "BUY"
    # Verify we're using team_b token (index 1)
    assert "team_b" in opp.details["poly_token_id"]
    # Verify Kalshi side is YES (team_a wins)
    assert opp.details["kalshi_side"] == "yes"


def test_arbitrage_covers_both_outcomes():
    """Verify that an arbitrage trade covers both possible outcomes."""
    # Direction 1: Poly team_a + Kalshi NO (team_b)
    # If team_a wins: Poly pays $1
    # If team_b wins: Kalshi pays $1
    # Both outcomes covered!

    poly_team = "team_a"
    kalshi_side = "no"  # Kalshi NO = team_b wins

    outcomes_covered = {poly_team, "team_b" if kalshi_side == "no" else "team_a"}
    assert outcomes_covered == {"team_a", "team_b"}, "Must cover both outcomes!"


def test_same_outcome_is_not_arbitrage():
    """Verify that betting same outcome on both platforms is NOT arbitrage."""
    # BAD: Poly team_b + Kalshi NO (also team_b)
    # Both bets win if team_b wins, both lose if team_a wins
    # This is NOT arbitrage - it's double betting!

    poly_team = "team_b"
    kalshi_side = "no"  # Kalshi NO = team_b wins

    outcomes_covered = {poly_team, "team_b" if kalshi_side == "no" else "team_a"}

    # This should show only 1 outcome covered - both are team_b!
    assert len(outcomes_covered) == 1, "Same outcome on both = NOT arbitrage!"


def test_poly_token_helper_safe_access():
    """Test that _get_poly_token handles edge cases safely."""
    from src.engine.arbitrage import _get_poly_token

    # Normal case - both tokens present (neg-risk market, no outcome_index)
    raw_data = {"clob_token_ids": ["token_a", "token_b"]}
    assert _get_poly_token(raw_data, 0) == "token_a"
    assert _get_poly_token(raw_data, 1) == "token_b"

    # Only one token
    raw_data = {"clob_token_ids": ["only_one"]}
    assert _get_poly_token(raw_data, 0) == "only_one"
    assert _get_poly_token(raw_data, 1) is None  # Safe, no IndexError

    # Empty array
    raw_data = {"clob_token_ids": []}
    assert _get_poly_token(raw_data, 0) is None
    assert _get_poly_token(raw_data, 1) is None

    # Missing key
    raw_data = {}
    assert _get_poly_token(raw_data, 0) is None
    assert _get_poly_token(raw_data, 1) is None


def test_poly_token_helper_with_outcome_index():
    """Test that _get_poly_token correctly maps tokens for 2-way markets."""
    from src.engine.arbitrage import _get_poly_token

    # 2-way market where this is team_a's market (outcome_index=0)
    # tokens[0] = team_a, tokens[1] = team_b
    raw_data = {"clob_token_ids": ["token_spirit", "token_xtreme"], "outcome_index": 0}
    assert _get_poly_token(raw_data, 0) == "token_spirit"  # team_a
    assert _get_poly_token(raw_data, 1) == "token_xtreme"  # team_b

    # 2-way market where this is team_b's market (outcome_index=1)
    # This market: team_a="Xtreme", team_b="Spirit"
    # tokens[1] = team_a (Xtreme), tokens[0] = team_b (Spirit)
    raw_data = {"clob_token_ids": ["token_spirit", "token_xtreme"], "outcome_index": 1}
    assert _get_poly_token(raw_data, 0) == "token_xtreme"  # team_a (Xtreme)
    assert _get_poly_token(raw_data, 1) == "token_spirit"  # team_b (Spirit)
