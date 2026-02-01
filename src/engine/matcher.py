from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from datetime import timedelta

from rapidfuzz import fuzz

from src.models import Market, Platform, SportEvent

logger = logging.getLogger(__name__)

# Minimum similarity score (0-100) to consider a team name match
TEAM_MATCH_THRESHOLD = 75
# Higher threshold for single-team matching (more false positive prone)
SINGLE_TEAM_THRESHOLD = 93
# Maximum time difference between events to be considered the same
MAX_TIME_DIFF = timedelta(hours=6)
# Maximum date difference (days) for game matching
MAX_DATE_DIFF_DAYS = 1

# Mapping of event group aliases for cross-platform futures matching.
# Keys are canonical group names; values are substrings that appear in
# Polymarket event_title or Kalshi series_ticker / event_ticker.
EVENT_GROUP_ALIASES: dict[str, list[str]] = {
    # NFL
    "nfl_champ": ["Pro Football Champion", "Super Bowl", "KXSUPERBOWL", "KXNFLCHAMP"],
    "nfl_mvp": ["NFL MVP", "KXNFLMVP"],
    "nfl_sb_mvp": ["Super Bowl MVP", "SB MVP", "KXNFLSBMVP"],
    "nfl_droty": ["NFL Defensive Rookie", "KXNFLDROTY"],
    # NBA
    "nba_champ": ["NBA Finals", "NBA Champion", "KXNBACHAMP"],
    "nba_east": ["NBA Eastern", "KXNBAEAST"],
    "nba_west": ["NBA Western", "KXNBAWEST"],
    "nba_mvp": ["NBA MVP", "KXNBAMVP"],
    "nba_finals_mvp": ["NBA Finals MVP", "KXNBAFINMVP"],
    "nba_droty": ["NBA Rookie", "KXNBADROTY"],
    "nba_dpoy": ["NBA Defensive Player", "KXNBADPOY"],
    # MLB
    "mlb_ws": ["World Series", "KXMLBWS"],
    "mlb_al": ["AL Champion", "KXMLBALCHAMP"],
    "mlb_nl": ["NL Champion", "KXMLBNLCHAMP"],
    "mlb_mvp": ["MLB MVP", "KXMLBMVP"],
    # NHL
    "nhl_champ": ["Stanley Cup", "NHL Champion", "KXNHLCHAMP"],
    "nhl_finals": ["NHL Finals", "KXNHLFINALSEXACT"],
    # Soccer
    "ucl_champ": ["Champions League", "KXUCLCHAMP"],
    "epl_champ": ["Premier League", "EPL", "KXEPLCHAMP"],
    "epl_top4": ["EPL Top 4", "Premier League Top 4", "KXEPLTOP4"],
    "laliga_champ": ["La Liga", "KXLALIGACHAMP"],
    "bundesliga_champ": ["Bundesliga", "KXBUNDESLIGACHAMP"],
    "seriea_champ": ["Serie A", "KXSERIEACHAMP"],
    "ligue1_champ": ["Ligue 1", "KXLIGUE1CHAMP"],
    "world_cup": ["FIFA World Cup", "World Cup", "KXMENWORLDCUP"],
    # College
    "ncaafb_champ": ["College Football", "CFP", "KXNCAAFBCHAMP", "KXNCAAF"],
    "ncaamb_champ": ["March Madness", "NCAA Basketball", "KXNCAAMBCHAMP"],
    "heisman": ["Heisman", "KXHEISMAN"],
    # Tennis
    "french_open": ["French Open", "Roland Garros", "KXFOPENMENSINGLE"],
    "wimbledon": ["Wimbledon", "KXWIMBLEDONMENSINGLE"],
    "aus_open": ["Australian Open", "KXAUSOPENMENSINGLE"],
    "us_open_tennis": ["US Open", "KXUSOPENMENSINGLE"],
    # MMA
    "ufc_champ": ["UFC", "KXUFCCHAMP"],
    # Additional soccer
    "fa_cup": ["FA Cup", "KXFACUP"],
    "carabao": ["Carabao Cup", "League Cup", "EFL Cup", "KXCARABAOCUP"],
    "europa": ["Europa League", "KXEUROPALEAGUE"],
    "conference": ["Conference League", "KXCONFERENCELEAGUE"],
    "copa_america": ["Copa America", "KXCOPAAMERICA"],
    "gold_cup": ["Gold Cup", "KXGOLDCUP"],
    "mls_champ": ["MLS Cup", "KXMLSCUP"],
    "liga_mx": ["Liga MX", "KXLIGAMX"],
}

# Build reverse lookup: substring -> canonical group
_GROUP_LOOKUP: list[tuple[str, str]] = []
for _canonical, _aliases in EVENT_GROUP_ALIASES.items():
    for _alias in _aliases:
        _GROUP_LOOKUP.append((_alias.lower(), _canonical))
# Sort longer aliases first so more specific matches win
_GROUP_LOOKUP.sort(key=lambda x: -len(x[0]))


def _canonicalize_event_group(event_group: str) -> str:
    """Map a raw event_group string to a canonical group name."""
    if not event_group:
        return ""
    lower = event_group.lower()
    for alias, canonical in _GROUP_LOOKUP:
        if alias in lower:
            return canonical
    return lower  # return as-is if no alias matches


def normalize_team_name(name: str) -> str:
    """Normalize a team name for comparison."""
    name = name.lower().strip()
    # Remove common suffixes/prefixes
    for remove in ("fc", "sc", "cf", "ac", "afc", "united", "city", "town", "county"):
        name = name.replace(f" {remove}", "").replace(f"{remove} ", "")
    # Remove extra whitespace
    return " ".join(name.split())


def team_similarity(a: str, b: str) -> float:
    """Return similarity score between two team names (0-100)."""
    na, nb = normalize_team_name(a), normalize_team_name(b)
    if not na or not nb:
        return 0
    # Try exact match first
    if na == nb:
        return 100
    # Token sort ratio handles word order differences
    return fuzz.token_sort_ratio(na, nb)


def _dates_compatible(pm: Market, km: Market) -> bool:
    """Check if two game markets have compatible dates (±1 day)."""
    if pm.game_date and km.game_date:
        diff = abs((pm.game_date - km.game_date).days)
        return diff <= MAX_DATE_DIFF_DAYS
    # If either lacks a date, allow match (best-effort)
    return True


def _is_group_stage(text: str) -> bool:
    """Check if text indicates a group-stage market."""
    return bool(re.search(r"\bgroup\b", text, re.IGNORECASE))


def _is_tournament_winner(text: str) -> bool:
    """Check if text indicates a tournament-winner/champion market."""
    return bool(re.search(r"\b(winner|champion|champ)\b", text, re.IGNORECASE))


def _groups_compatible(pm: Market, km: Market) -> bool:
    """Check if two futures markets belong to the same event group."""
    # Reject group-stage vs tournament-winner mismatches
    pg_text = pm.event_group or ""
    kg_text = km.event_group or ""
    if (_is_group_stage(pg_text) and _is_tournament_winner(kg_text)) or \
       (_is_tournament_winner(pg_text) and _is_group_stage(kg_text)):
        return False

    pg = _canonicalize_event_group(pg_text)
    kg = _canonicalize_event_group(kg_text)
    if pg and kg:
        return pg == kg
    # If either lacks a group, allow match (best-effort)
    return True


def _sports_compatible(pm: Market, km: Market) -> bool:
    """Check if two markets are for the same sport."""
    if pm.sport and km.sport:
        return pm.sport == km.sport
    # If either lacks sport info, allow match
    return True


def _grouping_key(m: Market) -> tuple[str, str]:
    """Return a grouping key for pre-filtering: (sport, market_type)."""
    return (m.sport or "_any", m.market_type or "_any")


def match_events(
    poly_markets: list[Market],
    kalshi_markets: list[Market],
) -> list[SportEvent]:
    """Match events between Polymarket and Kalshi based on team names.

    Uses sport + market_type pre-grouping for O(n) instead of O(n²),
    then applies date/group filters and fuzzy name matching within groups.
    """
    matched_events: list[SportEvent] = []
    used_kalshi: set[str] = set()

    # Pre-group Kalshi markets by (sport, market_type) for faster lookup
    kalshi_groups: dict[tuple[str, str], list[Market]] = defaultdict(list)
    for km in kalshi_markets:
        if not km.team_a:
            continue
        kalshi_groups[_grouping_key(km)].append(km)

    for pm in poly_markets:
        if not pm.team_a:
            continue

        pm_key = _grouping_key(pm)
        # Candidate Kalshi markets: same group + wildcard groups
        candidates: list[Market] = []
        for k_key, k_list in kalshi_groups.items():
            # Match if sport matches (or either is unknown) AND market_type matches
            sport_ok = (pm_key[0] == "_any" or k_key[0] == "_any" or pm_key[0] == k_key[0])
            type_ok = (pm_key[1] == "_any" or k_key[1] == "_any" or pm_key[1] == k_key[1])
            if sport_ok and type_ok:
                candidates.extend(k_list)

        best_match: Market | None = None
        best_score: float = 0

        for km in candidates:
            if km.market_id in used_kalshi:
                continue

            # Date filter for games
            if pm.market_type == "game" and km.market_type == "game":
                if not _dates_compatible(pm, km):
                    continue

            # Group filter for futures
            if pm.market_type == "futures" and km.market_type == "futures":
                if not _groups_compatible(pm, km):
                    continue

            if pm.team_b and km.team_b:
                # Both have two teams — match both
                score_direct = min(
                    team_similarity(pm.team_a, km.team_a),
                    team_similarity(pm.team_b, km.team_b),
                )
                score_swapped = min(
                    team_similarity(pm.team_a, km.team_b),
                    team_similarity(pm.team_b, km.team_a),
                )
                score = max(score_direct, score_swapped)
            else:
                # Polymarket has single team (futures market)
                # Match against YES team on Kalshi
                kalshi_yes_team = km.raw_data.get("yes_team", km.team_a)
                score = max(
                    team_similarity(pm.team_a, km.team_a),
                    team_similarity(pm.team_a, km.team_b),
                    team_similarity(pm.team_a, kalshi_yes_team),
                )

            if score > best_score:
                best_score = score
                best_match = km

        # Use higher threshold for single-team matches (more prone to false positives)
        threshold = TEAM_MATCH_THRESHOLD if (pm.team_b and best_match and best_match.team_b) else SINGLE_TEAM_THRESHOLD
        if best_match and best_score >= threshold:
            title = f"{best_match.team_a} vs {best_match.team_b}" if best_match.team_b else pm.team_a
            event = SportEvent(
                id=uuid.uuid4().hex[:12],
                title=title,
                team_a=best_match.team_a if best_match.team_b else pm.team_a,
                team_b=best_match.team_b,
                start_time=pm.raw_data.get("start_time"),
                category="sports",
                markets={
                    Platform.POLYMARKET: pm,
                    Platform.KALSHI: best_match,
                },
                matched=True,
            )
            matched_events.append(event)
            used_kalshi.add(best_match.market_id)
            logger.info(
                f"Matched: {pm.title} <-> {best_match.title} "
                f"(score={best_score:.0f}, sport={pm.sport or '?'}/{best_match.sport or '?'})"
            )

    # Also create "Kalshi-only" events for display (no Polymarket match)
    unmatched_kalshi = [
        km for km in kalshi_markets
        if km.market_id not in used_kalshi and km.team_a and km.team_b
    ]
    # Group by event_id to avoid duplicates
    seen_events: set[str] = set()
    for km in unmatched_kalshi:
        eid = km.event_id
        if eid in seen_events:
            continue
        seen_events.add(eid)
        event = SportEvent(
            id=uuid.uuid4().hex[:12],
            title=f"{km.team_a} vs {km.team_b}",
            team_a=km.team_a,
            team_b=km.team_b,
            category="sports",
            markets={Platform.KALSHI: km},
            matched=False,
        )
        matched_events.append(event)

    cross_matched = sum(1 for e in matched_events if e.matched)
    logger.info(
        f"Events: {cross_matched} cross-platform matched, "
        f"{len(matched_events) - cross_matched} Kalshi-only, "
        f"{len(matched_events)} total"
    )
    return matched_events
