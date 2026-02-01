from src.engine.matcher import match_events, normalize_team_name, team_similarity
from src.models import Market, Platform


def test_normalize_team_name():
    assert normalize_team_name("Manchester United FC") == "manchester"
    assert normalize_team_name("  Real Madrid  ") == "real madrid"
    assert normalize_team_name("FC Barcelona") == "barcelona"


def test_team_similarity_exact():
    assert team_similarity("Real Madrid", "Real Madrid") == 100


def test_team_similarity_case_insensitive():
    score = team_similarity("Real Madrid", "real madrid")
    assert score == 100


def test_team_similarity_with_suffix():
    score = team_similarity("Manchester United FC", "Manchester United")
    assert score >= 80


def test_team_similarity_different():
    score = team_similarity("Real Madrid", "FC Barcelona")
    assert score < 50


def test_match_events_basic():
    poly = [
        Market(
            platform=Platform.POLYMARKET,
            market_id="pm1",
            event_id="e1",
            title="Real Madrid vs Barcelona",
            team_a="Real Madrid",
            team_b="Barcelona",
        )
    ]
    kalshi = [
        Market(
            platform=Platform.KALSHI,
            market_id="k1",
            event_id="e2",
            title="Real Madrid vs FC Barcelona",
            team_a="Real Madrid",
            team_b="FC Barcelona",
        )
    ]
    matched = match_events(poly, kalshi)
    assert len(matched) == 1
    assert matched[0].team_a == "Real Madrid"
    assert Platform.POLYMARKET in matched[0].markets
    assert Platform.KALSHI in matched[0].markets


def test_match_events_no_match():
    poly = [
        Market(
            platform=Platform.POLYMARKET,
            market_id="pm1",
            event_id="e1",
            title="Lakers vs Celtics",
            team_a="Lakers",
            team_b="Celtics",
        )
    ]
    kalshi = [
        Market(
            platform=Platform.KALSHI,
            market_id="k1",
            event_id="e2",
            title="Warriors vs Rockets",
            team_a="Warriors",
            team_b="Rockets",
        )
    ]
    matched = match_events(poly, kalshi)
    # No cross-platform match, but Kalshi-only event is included
    cross_matched = [e for e in matched if e.matched]
    assert len(cross_matched) == 0


def test_match_events_swapped_order():
    poly = [
        Market(
            platform=Platform.POLYMARKET,
            market_id="pm1",
            event_id="e1",
            title="Team A vs Team B",
            team_a="Lakers",
            team_b="Celtics",
        )
    ]
    kalshi = [
        Market(
            platform=Platform.KALSHI,
            market_id="k1",
            event_id="e2",
            title="Team B vs Team A",
            team_a="Celtics",
            team_b="Lakers",
        )
    ]
    matched = match_events(poly, kalshi)
    assert len(matched) == 1
