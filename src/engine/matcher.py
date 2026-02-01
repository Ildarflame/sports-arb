from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from datetime import timedelta

from rapidfuzz import fuzz

from src.models import Market, Platform, SportEvent

logger = logging.getLogger(__name__)

# Sports where draws are possible — don't allow swapped team matching
# (swapped price inversion is only valid for 2-outcome sports)
_THREE_OUTCOME_SPORTS = {"soccer", "rugby", "cricket"}

# Minimum similarity score (0-100) to consider a team name match
TEAM_MATCH_THRESHOLD = 75
# Higher threshold for single-team matching (more false positive prone)
SINGLE_TEAM_THRESHOLD = 93
# Maximum time difference between events to be considered the same
MAX_TIME_DIFF = timedelta(hours=6)
# Maximum date difference (days) for game matching
MAX_DATE_DIFF_DAYS = 1
# Sports where platforms often disagree on dates (tournament start vs match day)
_LENIENT_DATE_SPORTS = {"tennis", "table_tennis", "mma", "boxing", "golf"}
MAX_DATE_DIFF_DAYS_LENIENT = 2

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
    # Golf
    "pga_tour": ["PGA", "PGA Tour", "KXPGATOUR"],
    "lpga_tour": ["LPGA", "LPGA Tour", "KXLPGATOUR"],
    "dp_world_tour": ["DP World", "European Tour", "KXDPWORLDTOUR"],
    # Motorsport
    "f1_champ": ["F1 Driver", "Formula 1", "Formula One", "KXF1"],
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


# Sport-specific alias lookups: city/nickname → canonical full name
# Separated by sport to avoid "Seattle" → Seahawks vs Kraken conflicts
_NBA_ALIASES: dict[str, str] = {
    "grizzlies": "memphis grizzlies", "memphis": "memphis grizzlies",
    "lakers": "los angeles lakers", "la lakers": "los angeles lakers",
    "celtics": "boston celtics", "boston": "boston celtics",
    "warriors": "golden state warriors", "golden state": "golden state warriors",
    "thunder": "oklahoma city thunder", "oklahoma city": "oklahoma city thunder", "okc": "oklahoma city thunder",
    "cavaliers": "cleveland cavaliers", "cavs": "cleveland cavaliers", "cleveland": "cleveland cavaliers",
    "knicks": "new york knicks", "new york": "new york knicks",
    "nets": "brooklyn nets", "brooklyn": "brooklyn nets",
    "76ers": "philadelphia 76ers", "sixers": "philadelphia 76ers", "philadelphia": "philadelphia 76ers",
    "heat": "miami heat", "miami": "miami heat",
    "hawks": "atlanta hawks", "atlanta": "atlanta hawks",
    "bulls": "chicago bulls", "chicago": "chicago bulls",
    "bucks": "milwaukee bucks", "milwaukee": "milwaukee bucks",
    "pacers": "indiana pacers", "indiana": "indiana pacers",
    "pistons": "detroit pistons", "detroit": "detroit pistons",
    "raptors": "toronto raptors", "toronto": "toronto raptors",
    "magic": "orlando magic", "orlando": "orlando magic",
    "wizards": "washington wizards", "washington": "washington wizards",
    "hornets": "charlotte hornets", "charlotte": "charlotte hornets",
    "spurs": "san antonio spurs", "san antonio": "san antonio spurs",
    "mavericks": "dallas mavericks", "mavs": "dallas mavericks", "dallas": "dallas mavericks",
    "rockets": "houston rockets", "houston": "houston rockets",
    "nuggets": "denver nuggets", "denver": "denver nuggets",
    "timberwolves": "minnesota timberwolves", "wolves": "minnesota timberwolves", "minnesota": "minnesota timberwolves",
    "trail blazers": "portland trail blazers", "blazers": "portland trail blazers", "portland": "portland trail blazers",
    "jazz": "utah jazz", "utah": "utah jazz",
    "pelicans": "new orleans pelicans", "new orleans": "new orleans pelicans",
    "suns": "phoenix suns", "phoenix": "phoenix suns",
    "kings": "sacramento kings", "sacramento": "sacramento kings",
    "clippers": "los angeles clippers", "la clippers": "los angeles clippers", "los angeles c": "los angeles clippers",
}
_NFL_ALIASES: dict[str, str] = {
    "chiefs": "kansas city chiefs", "kansas city": "kansas city chiefs",
    "eagles": "philadelphia eagles", "philadelphia": "philadelphia eagles",
    "bills": "buffalo bills", "buffalo": "buffalo bills",
    "ravens": "baltimore ravens", "baltimore": "baltimore ravens",
    "49ers": "san francisco 49ers", "niners": "san francisco 49ers", "san francisco": "san francisco 49ers",
    "lions": "detroit lions", "detroit": "detroit lions",
    "cowboys": "dallas cowboys", "dallas": "dallas cowboys",
    "packers": "green bay packers", "green bay": "green bay packers",
    "dolphins": "miami dolphins", "miami": "miami dolphins",
    "vikings": "minnesota vikings", "minnesota": "minnesota vikings",
    "chargers": "los angeles chargers", "la chargers": "los angeles chargers",
    "steelers": "pittsburgh steelers", "pittsburgh": "pittsburgh steelers",
    "texans": "houston texans", "houston": "houston texans",
    "bengals": "cincinnati bengals", "cincinnati": "cincinnati bengals",
    "seahawks": "seattle seahawks", "seattle": "seattle seahawks",
    "commanders": "washington commanders", "washington": "washington commanders",
    "broncos": "denver broncos", "denver": "denver broncos",
    "jaguars": "jacksonville jaguars", "jacksonville": "jacksonville jaguars", "jags": "jacksonville jaguars",
    "colts": "indianapolis colts", "indianapolis": "indianapolis colts",
    "falcons": "atlanta falcons", "atlanta": "atlanta falcons",
    "saints": "new orleans saints", "new orleans": "new orleans saints",
    "panthers": "carolina panthers", "carolina": "carolina panthers",
    "cardinals": "arizona cardinals", "arizona": "arizona cardinals",
    "bears": "chicago bears", "chicago": "chicago bears",
    "raiders": "las vegas raiders", "las vegas": "las vegas raiders",
    "patriots": "new england patriots", "new england": "new england patriots",
    "giants": "new york giants",
    "jets": "new york jets",
    "titans": "tennessee titans", "tennessee": "tennessee titans",
    "browns": "cleveland browns",
    "rams": "los angeles rams", "la rams": "los angeles rams",
    "buccaneers": "tampa bay buccaneers", "bucs": "tampa bay buccaneers", "tampa bay": "tampa bay buccaneers",
}
_NHL_ALIASES: dict[str, str] = {
    "bruins": "boston bruins", "boston": "boston bruins",
    "lightning": "tampa bay lightning", "tampa bay": "tampa bay lightning",
    "maple leafs": "toronto maple leafs", "leafs": "toronto maple leafs", "toronto": "toronto maple leafs",
    "panthers": "florida panthers", "florida": "florida panthers",
    "rangers": "new york rangers", "new york": "new york rangers",
    "hurricanes": "carolina hurricanes", "carolina": "carolina hurricanes",
    "oilers": "edmonton oilers", "edmonton": "edmonton oilers",
    "avalanche": "colorado avalanche", "colorado": "colorado avalanche",
    "stars": "dallas stars", "dallas": "dallas stars",
    "flames": "calgary flames", "calgary": "calgary flames",
    "jets": "winnipeg jets", "winnipeg": "winnipeg jets",
    "wild": "minnesota wild", "minnesota": "minnesota wild",
    "capitals": "washington capitals", "caps": "washington capitals", "washington": "washington capitals",
    "red wings": "detroit red wings", "detroit": "detroit red wings",
    "penguins": "pittsburgh penguins", "pens": "pittsburgh penguins", "pittsburgh": "pittsburgh penguins",
    "blues": "st. louis blues", "st louis": "st. louis blues", "st. louis": "st. louis blues",
    "canucks": "vancouver canucks", "vancouver": "vancouver canucks",
    "islanders": "new york islanders", "new york i": "new york islanders",
    "predators": "nashville predators", "preds": "nashville predators", "nashville": "nashville predators",
    "senators": "ottawa senators", "sens": "ottawa senators", "ottawa": "ottawa senators",
    "blackhawks": "chicago blackhawks", "chicago": "chicago blackhawks",
    "golden knights": "vegas golden knights", "vegas": "vegas golden knights",
    "kraken": "seattle kraken", "seattle": "seattle kraken",
    "ducks": "anaheim ducks", "anaheim": "anaheim ducks",
    "blue jackets": "columbus blue jackets", "columbus": "columbus blue jackets",
    "utah hockey club": "utah hockey club", "coyotes": "utah hockey club",
    "sabres": "buffalo sabres", "buffalo": "buffalo sabres",
    "devils": "new jersey devils", "new jersey": "new jersey devils",
    "flyers": "philadelphia flyers", "philadelphia": "philadelphia flyers",
    "canadiens": "montreal canadiens", "habs": "montreal canadiens", "montreal": "montreal canadiens",
    "sharks": "san jose sharks", "san jose": "san jose sharks",
}

# Sport-specific alias map
_SPORT_ALIASES: dict[str, dict[str, str]] = {
    "nba": _NBA_ALIASES,
    "nfl": _NFL_ALIASES,
    "ncaa_fb": _NFL_ALIASES,  # College uses similar names
    "nhl": _NHL_ALIASES,
}

# Fallback aliases (non-ambiguous nicknames that are unique across sports)
_TEAM_ALIASES: dict[str, str] = {
    # NBA (unique nicknames only)
    "grizzlies": "memphis grizzlies", "lakers": "los angeles lakers",
    "celtics": "boston celtics", "warriors": "golden state warriors",
    "thunder": "oklahoma city thunder", "cavaliers": "cleveland cavaliers", "cavs": "cleveland cavaliers",
    "knicks": "new york knicks", "nets": "brooklyn nets",
    "76ers": "philadelphia 76ers", "sixers": "philadelphia 76ers",
    "heat": "miami heat", "bucks": "milwaukee bucks",
    "pacers": "indiana pacers", "raptors": "toronto raptors",
    "magic": "orlando magic", "hornets": "charlotte hornets",
    "mavericks": "dallas mavericks", "mavs": "dallas mavericks",
    "rockets": "houston rockets", "nuggets": "denver nuggets",
    "timberwolves": "minnesota timberwolves",
    "trail blazers": "portland trail blazers", "blazers": "portland trail blazers",
    "jazz": "utah jazz", "pelicans": "new orleans pelicans",
    "suns": "phoenix suns", "clippers": "los angeles clippers",
    # NFL (unique nicknames only)
    "chiefs": "kansas city chiefs", "eagles": "philadelphia eagles",
    "bills": "buffalo bills", "ravens": "baltimore ravens",
    "49ers": "san francisco 49ers", "niners": "san francisco 49ers",
    "cowboys": "dallas cowboys", "packers": "green bay packers",
    "dolphins": "miami dolphins", "vikings": "minnesota vikings",
    "chargers": "los angeles chargers", "steelers": "pittsburgh steelers",
    "texans": "houston texans", "bengals": "cincinnati bengals",
    "seahawks": "seattle seahawks", "commanders": "washington commanders",
    "broncos": "denver broncos", "jaguars": "jacksonville jaguars", "jags": "jacksonville jaguars",
    "colts": "indianapolis colts", "falcons": "atlanta falcons",
    "saints": "new orleans saints", "buccaneers": "tampa bay buccaneers", "bucs": "tampa bay buccaneers",
    "titans": "tennessee titans", "rams": "los angeles rams",
    "patriots": "new england patriots",
    # NHL (unique nicknames only)
    "bruins": "boston bruins", "lightning": "tampa bay lightning",
    "maple leafs": "toronto maple leafs", "leafs": "toronto maple leafs",
    "oilers": "edmonton oilers", "avalanche": "colorado avalanche",
    "flames": "calgary flames", "wild": "minnesota wild",
    "capitals": "washington capitals", "caps": "washington capitals",
    "red wings": "detroit red wings", "penguins": "pittsburgh penguins", "pens": "pittsburgh penguins",
    "blues": "st. louis blues", "st louis": "st. louis blues", "st. louis": "st. louis blues",
    "canucks": "vancouver canucks", "islanders": "new york islanders",
    "predators": "nashville predators", "preds": "nashville predators",
    "senators": "ottawa senators", "sens": "ottawa senators",
    "blackhawks": "chicago blackhawks",
    "golden knights": "vegas golden knights",
    "kraken": "seattle kraken", "ducks": "anaheim ducks",
    "blue jackets": "columbus blue jackets", "coyotes": "utah hockey club",
    "sabres": "buffalo sabres", "devils": "new jersey devils",
    "flyers": "philadelphia flyers",
    "canadiens": "montreal canadiens", "habs": "montreal canadiens",
    "sharks": "san jose sharks",
}


# Soccer club name suffixes to strip — only at end of name
# Two tiers: safe to always strip (fc, sc...) and contextual (city, united — only at end)
_SOCCER_STRIP_ALWAYS = re.compile(
    r"\s+(?:fc|sc|cf|ac|afc|bk|sk|fk|if|ff|ssc|as|ss|ssd"
    r"|saudi club|club|hotspur|wanderers|albion|rovers|athletic|spor|sport)$",
    re.IGNORECASE,
)
_SOCCER_STRIP_END = re.compile(
    r"\s+(?:united|city|town|county)$",
    re.IGNORECASE,
)
# Common prefixes for European clubs: "AJ Auxerre" → "Auxerre"
_SOCCER_STRIP_PREFIX = re.compile(
    r"^(?:aj|us|rc|ss|as|ac|fc|fk|sk|bk|if|ff|sc|cf|ssc|ssd|cd|ca|cs|ud|bv"
    r"|real|sporting|racing|dynamo|dinamo|\d+\.)\s+",
    re.IGNORECASE,
)
# Year suffix: "Bologna FC 1909" → "Bologna FC"
_YEAR_SUFFIX = re.compile(r"\s+\d{4}$")
# Umlaut mapping
_UMLAUTS = str.maketrans({"ü": "u", "ö": "o", "ä": "a", "é": "e", "á": "a", "í": "i", "ó": "o", "ú": "u", "ñ": "n", "ç": "c", "ş": "s", "ı": "i", "ğ": "g", "ž": "z", "š": "s", "č": "c", "ř": "r", "ý": "y", "ą": "a", "ę": "e", "ł": "l", "ń": "n", "ś": "s", "ź": "z", "ż": "z"})

# Esports game suffixes to strip from team names
_ESPORTS_GAME_SUFFIXES = re.compile(
    r"\s+(?:valorant|league of legends|dota\s*2|counter\s*strike|cs2|csgo|overwatch|rocket league|table tennis)$",
    re.IGNORECASE,
)

# NCAA mascot names to strip (common D1 mascots appended by Polymarket)
_NCAA_MASCOTS = re.compile(
    r"\s+(?:cardinals|cardinal|demons|bulldogs|wildcats|tigers|bears|eagles|falcons|hawks|huskies"
    r"|cougars|mustangs|knights|warriors|lions|panthers|wolves|rams|spartans|trojans"
    r"|gators|seminoles|crimson tide|volunteers|sooners|longhorns|buckeyes|wolverines"
    r"|jayhawks|tar heels|cavaliers|hokies|cyclones|boilermakers|hoosiers|badgers"
    r"|golden gophers|golden bears|golden lions|fighting irish|fighting illini|blue devils|orange|terrapins|nittany lions"
    r"|razorbacks|commodores|gamecocks|rebels|aggies|owls|bruins|beavers|ducks"
    r"|sun devils|buffaloes|mountaineers|red raiders|horned frogs|bearcats|shockers"
    r"|gaels|zags|gonzaga bulldogs|blue jays|friars|musketeers|pirates|johnnies"
    r"|peacocks|billikens|explorers|bonnies|dukes|monarchs|49ers|roadrunners"
    r"|thundering herd|mean green|bobcats|hilltoppers|red wolves|chanticleers"
    r"|paladins|catamounts|phoenix|lumberjacks|antelopes|flames|penguins|leathernecks"
    # Additional mascots found from live Polymarket data
    r"|cornhuskers|hawkeyes|yellow jackets|wolf pack|golden hurricane|green wave"
    r"|fighting camels|fightin' blue hens|golden griffins|golden grizzlies"
    r"|bison|broncos|broncs|buccaneers|bulls|bluejays|braves|blazers"
    r"|chippewas|colonials|crusaders|chargers|dragons|flyers|hoyas"
    r"|jaguars|jaspers|lancers|leopards|mocs|norse|pacers|patriots|pioneers"
    r"|privateers|ramblers|rattlers|red flash|red foxes|red storm|redhawks"
    r"|revolutionaries|rockets|roos|saints|salukis|seahawks|seawolves"
    r"|spiders|stags|terriers|tribe|utes|vaqueros|warhawks|zips"
    r"|aces|beacons|bearkats|blue hose|mastodons|shock|sycamores|redbirds"
    r"|bengals|colonels|dolphins|governors|highlanders|miners|skyhawks"
    r"|cowboys|raiders|vikings|toreros|dons|waves|pilots|anteaters"
    r"|gauchos|retrievers|matadors|ospreys|texans|tritons|islanders"
    r"|tommies|keydets|greyhounds|griffins|racers|midshipmen|lobos|aztecs"
    r"|demon deacons|wolfpack|big red|big green|crimson|pride|gladiators"
    r"|hurricanes|hoosiers|scarlet knights|yellow jackets|commodores)$",
    re.IGNORECASE,
)

# European club aliases (common mismatches between platforms)
_SOCCER_ALIASES: dict[str, str] = {
    "atletico": "atletico madrid", "atletico madrid": "atletico madrid", "atletico de madrid": "atletico madrid", "club atletico madrid": "atletico madrid", "atl. madrid": "atletico madrid", "atl madrid": "atletico madrid",
    "bayern munich": "bayern munchen", "bayern munchen": "bayern munchen", "bayern": "bayern munchen",
    "psg": "paris saint-germain", "paris saint germain": "paris saint-germain", "paris sg": "paris saint-germain",
    "inter": "inter milan", "inter milan": "inter milan", "internazionale": "inter milan", "internazionale milano": "inter milan",
    "ac milan": "milan", "milan": "milan",
    "man city": "manchester city", "man utd": "manchester united", "man united": "manchester united",
    "spurs": "tottenham",
    "wolves": "wolverhampton",
    "brighton": "brighton", "brighton and hove albion": "brighton", "brighton & hove albion": "brighton", "brighton & hove": "brighton", "brighton and hove": "brighton", "brighton hove": "brighton",
    "nottingham forest": "nottingham", "nott'm forest": "nottingham",
    "bournemouth": "bournemouth", "afc bournemouth": "bournemouth",
    "bilbao": "athletic bilbao", "athletic bilbao": "athletic bilbao", "athletic club": "athletic bilbao",
    "betis": "real betis", "real betis": "real betis",
    "sociedad": "real sociedad", "real sociedad": "real sociedad",
    "celta vigo": "celta vigo", "celta de vigo": "celta vigo", "celta": "celta vigo",
    "1. fc cologne": "fc koln", "fc koln": "fc koln", "koln": "fc koln", "cologne": "fc koln",
    "rb leipzig": "leipzig", "leipzig": "leipzig", "rasenballsport leipzig": "leipzig",
    "monchengladbach": "borussia monchengladbach", "gladbach": "borussia monchengladbach", "bmg": "borussia monchengladbach", "m'gladbach": "borussia monchengladbach", "mgladbach": "borussia monchengladbach",
    "dortmund": "borussia dortmund", "bvb": "borussia dortmund", "borussia dortmund": "borussia dortmund", "bv borussia 09 dortmund": "borussia dortmund",
    "leverkusen": "bayer leverkusen", "bayer leverkusen": "bayer leverkusen",
    "hertha": "hertha berlin", "hertha bsc": "hertha berlin",
    "napoli": "napoli", "ssc napoli": "napoli",
    "lazio": "lazio", "ss lazio": "lazio",
    "roma": "roma", "as roma": "roma",
    "fiorentina": "fiorentina", "acf fiorentina": "fiorentina",
    "atalanta": "atalanta", "atalanta bc": "atalanta",
    "marseille": "olympique marseille", "om": "olympique marseille", "olympique de marseille": "olympique marseille",
    "lyon": "olympique lyon", "olympique lyonnais": "olympique lyon", "ol": "olympique lyon",
    "monaco": "as monaco", "as monaco": "as monaco",
    "st etienne": "saint-etienne", "saint etienne": "saint-etienne",
    "al akhdoud": "al okhdood", "al-akhdoud": "al okhdood", "al okhdood": "al okhdood",
    "al ittifaq": "al ettifaq", "al-ittifaq": "al ettifaq", "al ettifaq": "al ettifaq",
    # Additional frequently mismatched
    "everton": "everton", "everton fc": "everton",
    "leeds": "leeds", "leeds utd": "leeds",
    "como": "como", "como 1907": "como",
    "genoa": "genoa", "genoa cfc": "genoa",
    "hellas verona": "verona", "verona": "verona",
    "le havre": "le havre", "le havre ac": "le havre",
    "alaves": "alaves", "deportivo alaves": "alaves",
    "angers": "angers", "angers sco": "angers",
    "mallorca": "mallorca", "rcd mallorca": "mallorca",
    # German clubs
    "hamburg": "hamburger sv", "hamburger sv": "hamburger sv", "hsv": "hamburger sv",
    "st pauli": "st pauli", "st. pauli": "st pauli", "fc st pauli": "st pauli",
    "schalke": "schalke 04", "schalke 04": "schalke 04", "fc schalke 04": "schalke 04",
    "stuttgart": "vfb stuttgart", "vfb stuttgart": "vfb stuttgart",
    "wolfsburg": "vfl wolfsburg", "vfl wolfsburg": "vfl wolfsburg",
    "freiburg": "freiburg", "sc freiburg": "freiburg",
    "hoffenheim": "hoffenheim", "tsg hoffenheim": "hoffenheim",
    "mainz": "mainz", "mainz 05": "mainz", "1. fsv mainz 05": "mainz",
    "heidenheim": "heidenheim", "fc heidenheim": "heidenheim", "1. fc heidenheim": "heidenheim",
    "augsburg": "augsburg", "fc augsburg": "augsburg",
    "bochum": "vfl bochum", "vfl bochum": "vfl bochum",
    "werder bremen": "werder bremen", "bremen": "werder bremen",
    "union berlin": "union berlin", "1. fc union berlin": "union berlin",
    "eintracht frankfurt": "eintracht frankfurt", "frankfurt": "eintracht frankfurt",
    "kaiserslautern": "kaiserslautern", "1. fc kaiserslautern": "kaiserslautern",
    "greuther furth": "greuther furth", "spvgg greuther furth": "greuther furth",
    "dusseldorf": "fortuna dusseldorf", "fortuna dusseldorf": "fortuna dusseldorf",
    # French clubs
    "lille": "lille", "lille osc": "lille", "losc lille": "lille",
    "nice": "nice", "ogc nice": "nice",
    "rennes": "stade rennais", "stade rennais": "stade rennais",
    "strasbourg": "strasbourg", "rc strasbourg": "strasbourg",
    "nantes": "nantes", "fc nantes": "nantes",
    "lens": "lens", "rc lens": "lens",
    "brest": "stade brestois", "stade brestois": "stade brestois",
    "reims": "stade reims", "stade reims": "stade reims", "stade de reims": "stade reims",
    "montpellier": "montpellier", "montpellier hsc": "montpellier",
    "toulouse": "toulouse", "toulouse fc": "toulouse",
    "auxerre": "auxerre", "aj auxerre": "auxerre",
    # Italian clubs
    "juventus": "juventus", "juve": "juventus",
    "torino": "torino", "torino fc": "torino",
    "udinese": "udinese", "udinese calcio": "udinese",
    "bologna": "bologna", "bologna fc": "bologna",
    "empoli": "empoli", "empoli fc": "empoli",
    "cagliari": "cagliari", "cagliari calcio": "cagliari",
    "lecce": "lecce", "us lecce": "lecce",
    "monza": "monza", "ac monza": "monza",
    "parma": "parma", "parma calcio": "parma",
    "venezia": "venezia", "venezia fc": "venezia",
    "sampdoria": "sampdoria", "uc sampdoria": "sampdoria",
    "sassuolo": "sassuolo", "us sassuolo": "sassuolo",
    # Spanish clubs
    "valladolid": "valladolid", "real valladolid": "valladolid",
    "getafe": "getafe", "getafe cf": "getafe",
    "osasuna": "osasuna", "ca osasuna": "osasuna",
    "girona": "girona", "girona fc": "girona",
    "las palmas": "las palmas", "ud las palmas": "las palmas",
    "espanyol": "espanyol", "rcd espanyol": "espanyol",
    "sevilla": "sevilla", "sevilla fc": "sevilla",
    "villarreal": "villarreal", "villarreal cf": "villarreal",
    "rayo vallecano": "rayo vallecano", "rayo": "rayo vallecano",
    "leganes": "leganes", "cd leganes": "leganes",
    "valencia": "valencia", "valencia cf": "valencia",
    # Portuguese clubs
    "benfica": "benfica", "sl benfica": "benfica",
    "porto": "porto", "fc porto": "porto",
    "sporting cp": "sporting lisbon", "sporting lisbon": "sporting lisbon", "sporting": "sporting lisbon",
    "braga": "braga", "sc braga": "braga",
    # Dutch clubs
    "psv": "psv eindhoven", "psv eindhoven": "psv eindhoven",
    "ajax": "ajax", "afc ajax": "ajax",
    "feyenoord": "feyenoord",
    "az alkmaar": "az alkmaar", "az": "az alkmaar",
    # Turkish clubs
    "galatasaray": "galatasaray",
    "fenerbahce": "fenerbahce",
    "besiktas": "besiktas",
    "trabzonspor": "trabzonspor",
    # Scottish clubs
    "celtic": "celtic", "celtic fc": "celtic",
    "rangers": "rangers", "rangers fc": "rangers",
    # Belgian clubs
    "club brugge": "club brugge", "brugge": "club brugge",
    "anderlecht": "anderlecht", "rsc anderlecht": "anderlecht",
    # MLS clubs
    "la galaxy": "la galaxy", "los angeles galaxy": "la galaxy",
    "lafc": "los angeles fc", "los angeles fc": "los angeles fc",
    "nycfc": "new york city fc", "new york city fc": "new york city fc",
    "ny red bulls": "new york red bulls", "new york red bulls": "new york red bulls",
    "inter miami": "inter miami", "inter miami cf": "inter miami",
    "atlanta united": "atlanta united", "atlanta utd": "atlanta united",
    # Real Sociedad full name
    "real sociedad futbol": "real sociedad", "sociedad futbol": "real sociedad",
}


def normalize_team_name(name: str, sport: str = "") -> str:
    """Normalize a team name for comparison.

    Uses sport-specific alias tables when sport is provided to resolve
    city-name ambiguity (e.g. "Seattle" → Seahawks in NFL, Kraken in NHL).
    Then strips common suffixes like FC/SC/Hotspur/Wanderers for soccer teams.
    """
    name = name.lower().strip()
    # Normalize special chars: acute accent (´), backtick, curly quotes
    name = name.replace("´", "'").replace("`", "'").replace("\u2018", "'").replace("\u2019", "'")
    # Normalize dashes and umlauts
    name = name.replace("-", " ")
    name = name.translate(_UMLAUTS)
    name = " ".join(name.split())
    # Strip esports game suffixes: "Karmine Corp Valorant" → "Karmine Corp"
    if sport in ("esports", "table_tennis"):
        name = _ESPORTS_GAME_SUFFIXES.sub("", name).strip()
    # Strip NCAA mascots: "Incarnate Word Cardinals" → "Incarnate Word"
    if sport in ("ncaa_mb", "ncaa_wb", "ncaa_fb"):
        prev = name
        name = _NCAA_MASCOTS.sub("", name).strip()
        # Only strip mascot if something remains
        if len(name) < 3:
            name = prev
    # Normalize "St." — in NCAA context it usually means "State", elsewhere "Saint"
    if sport in ("ncaa_mb", "ncaa_wb", "ncaa_fb"):
        name = re.sub(r"\bst\.(?:\s|$)", "state ", name).strip()
    else:
        name = re.sub(r"\bst\.(?:\s|$)", "saint ", name).strip()
    name = re.sub(r"\bmt\.(?:\s|$)", "mount ", name).strip()
    # Check sport-specific aliases first (resolves city ambiguity)
    sport_aliases = _SPORT_ALIASES.get(sport, {})
    if sport_aliases and name in sport_aliases:
        return sport_aliases[name]
    # Check generic alias table (non-ambiguous nicknames)
    if name in _TEAM_ALIASES:
        return _TEAM_ALIASES[name]
    # Check soccer aliases
    if name in _SOCCER_ALIASES:
        return _SOCCER_ALIASES[name]
    # Remove year suffix: "1909", "1893", etc.
    name = _YEAR_SUFFIX.sub("", name).strip()
    # Remove soccer suffixes iteratively (some names have multiple: "Wolverhampton Wanderers FC")
    for _ in range(3):
        prev = name
        name = _SOCCER_STRIP_ALWAYS.sub("", name).strip()
        name = _SOCCER_STRIP_END.sub("", name).strip()
        name = " ".join(name.split())
        if name == prev:
            break
    # Strip common prefixes after suffixes (order matters: "FC Bayern München" → "bayern munchen")
    name_no_prefix = _SOCCER_STRIP_PREFIX.sub("", name).strip()
    if len(name_no_prefix) > 2:
        name = name_no_prefix
    # Strip Spanish/Italian/Portuguese articles: "de", "del", "di", "do", "da", "e"
    name = re.sub(r"\b(?:de|del|di|do|da)\b", " ", name)
    # Normalize & to space
    name = name.replace("&", " ")
    name = " ".join(name.split())
    # Check aliases again after cleaning
    if sport_aliases and name in sport_aliases:
        return sport_aliases[name]
    if name in _TEAM_ALIASES:
        return _TEAM_ALIASES[name]
    if name in _SOCCER_ALIASES:
        return _SOCCER_ALIASES[name]
    return name


def team_similarity(a: str, b: str, sport: str = "") -> float:
    """Return similarity score between two team names (0-100)."""
    na, nb = normalize_team_name(a, sport), normalize_team_name(b, sport)
    if not na or not nb:
        return 0
    # Try exact match first
    if na == nb:
        return 100
    # Tennis/individual sports: one platform may use "First Last", other just "Last"
    # If one name is a single token and matches the last token of the other, it's a match
    if sport in ("tennis", "table_tennis", "ufc", "boxing", "golf"):
        parts_a, parts_b = na.split(), nb.split()
        if len(parts_a) == 1 and len(parts_b) > 1 and parts_a[0] == parts_b[-1]:
            return 98
        if len(parts_b) == 1 and len(parts_a) > 1 and parts_b[0] == parts_a[-1]:
            return 98
    # Token sort ratio handles word order differences
    return fuzz.token_sort_ratio(na, nb)


def _dates_compatible(pm: Market, km: Market, *, require_both: bool = False) -> bool:
    """Check if two markets have compatible dates.

    Uses ±1 day for most sports, ±2 days for tennis/MMA/golf where platforms
    often disagree on dates (tournament start vs individual match day).
    When require_both=True (game markets), both must have a date.
    """
    if pm.game_date and km.game_date:
        diff = abs((pm.game_date - km.game_date).days)
        sport = pm.sport or km.sport or ""
        max_diff = MAX_DATE_DIFF_DAYS_LENIENT if sport in _LENIENT_DATE_SPORTS else MAX_DATE_DIFF_DAYS
        return diff <= max_diff
    if require_both:
        return False
    return True


def _is_group_stage(text: str) -> bool:
    """Check if text indicates a group-stage market (e.g. 'Group L', 'Group A')."""
    return bool(re.search(r"\bGroup\s+[A-Za-z0-9]\b", text))


def _is_tournament_winner(text: str) -> bool:
    """Check if text indicates a tournament-winner/champion market."""
    return bool(re.search(r"\b(winner|champion|champ)\b", text, re.IGNORECASE))


def _groups_compatible(pm: Market, km: Market) -> bool:
    """Check if two futures markets belong to the same event group."""
    pg_text = pm.event_group or ""
    kg_text = km.event_group or ""

    # Reject group-stage vs tournament-winner mismatches
    # Check both event_group AND title for group-stage indicators
    pm_is_group = _is_group_stage(pg_text) or _is_group_stage(pm.title)
    km_is_group = _is_group_stage(kg_text) or _is_group_stage(km.title)
    pm_is_winner = _is_tournament_winner(pg_text) or _is_tournament_winner(pm.title)
    km_is_winner = _is_tournament_winner(kg_text) or _is_tournament_winner(km.title)

    # If one is group-stage and the other is not → incompatible
    if pm_is_group != km_is_group:
        return False

    if (pm_is_group and km_is_winner) or (km_is_group and pm_is_winner):
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


def _dedup_markets(markets: list[Market]) -> list[Market]:
    """Keep one market per event_id (game markets only).

    For 2-way game markets, both platforms return 2 markets per game
    (one per team). Keep only the first one encountered to avoid
    double-matching and duplicate arbitrage calculations.
    Futures markets are not deduped (each team is a separate bet).
    """
    seen_events: set[str] = set()
    result: list[Market] = []
    for m in markets:
        if m.market_type == "game" and m.event_id in seen_events:
            continue
        if m.market_type == "game":
            seen_events.add(m.event_id)
        result.append(m)
    return result


def match_events(
    poly_markets: list[Market],
    kalshi_markets: list[Market],
) -> list[SportEvent]:
    """Match events between Polymarket and Kalshi based on team names.

    Uses sport + market_type pre-grouping for O(n) instead of O(n²),
    then applies date/group filters and fuzzy name matching within groups.
    """
    # Deduplicate game markets: keep 1 market per event_id per platform
    poly_markets = _dedup_markets(poly_markets)
    kalshi_markets = _dedup_markets(kalshi_markets)

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
        best_swapped: bool = False

        for km in candidates:
            if km.market_id in used_kalshi:
                continue

            # Date filter for games — require both dates present
            if pm.market_type == "game" and km.market_type == "game":
                if not _dates_compatible(pm, km, require_both=True):
                    continue

            # Group filter for futures
            if pm.market_type == "futures" and km.market_type == "futures":
                if not _groups_compatible(pm, km):
                    continue

            # Use the more specific sport for normalization
            sport = pm.sport or km.sport or ""

            if pm.team_b and km.team_b:
                # Both have two teams — check direct and swapped order.
                direct_score = min(
                    team_similarity(pm.team_a, km.team_a, sport),
                    team_similarity(pm.team_b, km.team_b, sport),
                )
                swapped_score = min(
                    team_similarity(pm.team_a, km.team_b, sport),
                    team_similarity(pm.team_b, km.team_a, sport),
                )
                if swapped_score > direct_score:
                    score = swapped_score
                    # For 3-outcome sports (soccer etc.), allow swapped team
                    # ORDER for matching but do NOT flag teams_swapped — price
                    # inversion is invalid when draws are possible.
                    is_swapped = sport not in _THREE_OUTCOME_SPORTS
                else:
                    score = direct_score
                    is_swapped = False
            else:
                # Polymarket has single team (futures market)
                # Match against YES team on Kalshi
                kalshi_yes_team = km.raw_data.get("yes_team", km.team_a)
                score = max(
                    team_similarity(pm.team_a, km.team_a, sport),
                    team_similarity(pm.team_a, km.team_b, sport),
                    team_similarity(pm.team_a, kalshi_yes_team, sport),
                )
                is_swapped = False  # N/A for single-team futures

            if score > best_score:
                best_score = score
                best_match = km
                best_swapped = is_swapped

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
                teams_swapped=best_swapped,
            )
            matched_events.append(event)
            used_kalshi.add(best_match.market_id)
            swap_tag = " SWAPPED" if best_swapped else ""
            logger.info(
                f"Matched{swap_tag}: {pm.title} <-> {best_match.title} "
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
