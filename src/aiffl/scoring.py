"""Fantasy points from a raw Sleeper stat line, using the weights in config/league.yaml.

Offensive weights come from the config. Kicker and team-defense tables are fixed here.
"""

from aiffl.config import load_league

# Config key -> Sleeper stat keys it applies to.
OFFENSE_STATS = {
    "pass_yd": ["pass_yd"],
    "pass_td": ["pass_td"],
    "pass_int": ["pass_int"],
    "rush_yd": ["rush_yd"],
    "rush_td": ["rush_td"],
    "rec": ["rec"],
    "rec_yd": ["rec_yd"],
    "rec_td": ["rec_td"],
    "fumble_lost": ["fum_lost"],
    "two_pt": ["pass_2pt", "rush_2pt", "rec_2pt"],
}
# Return and recovery touchdowns by offensive players.
OTHER_TD_STATS = ["st_td", "fum_rec_td"]

KICKER = {"fg_0_39": 3, "fg_40_49": 4, "fg_50_plus": 5, "xp": 1, "missed_fg": -1, "missed_xp": -1}
DST = {"sack": 1, "int": 2, "fum_rec": 2, "safety": 2, "blocked_kick": 2, "td": 6}  # td: defensive or return
DST_POINTS_ALLOWED = [(0, 10), (6, 7), (13, 4), (20, 1), (27, 0), (34, -1)]  # (max allowed, points)
DST_POINTS_ALLOWED_ELSE = -4


def _s(stats: dict, key: str) -> float:
    return float(stats.get(key) or 0)


def offense_points(stats: dict, weights: dict) -> float:
    pts = sum(weights[k] * _s(stats, sk) for k, keys in OFFENSE_STATS.items() for sk in keys)
    pts += 6 * sum(_s(stats, k) for k in OTHER_TD_STATS)
    return pts


def kicker_points(stats: dict) -> float:
    fg_50 = _s(stats, "fgm_50p")
    fg_40 = _s(stats, "fgm_40_49")
    fg_short = _s(stats, "fgm") - fg_50 - fg_40
    missed_fg = _s(stats, "fga") - _s(stats, "fgm")
    missed_xp = _s(stats, "xpa") - _s(stats, "xpm")
    return (KICKER["fg_0_39"] * fg_short + KICKER["fg_40_49"] * fg_40 + KICKER["fg_50_plus"] * fg_50
            + KICKER["xp"] * _s(stats, "xpm") + KICKER["missed_fg"] * missed_fg + KICKER["missed_xp"] * missed_xp)


def dst_points(stats: dict) -> float:
    pts = (DST["sack"] * _s(stats, "sack") + DST["int"] * _s(stats, "int") + DST["fum_rec"] * _s(stats, "fum_rec")
           + DST["safety"] * _s(stats, "safe") + DST["blocked_kick"] * _s(stats, "blk_kick")
           # Not "td": on team lines that counts every touchdown the team scored, offense included.
           + DST["td"] * (_s(stats, "def_td") + _s(stats, "def_st_td")))
    allowed = _s(stats, "pts_allow")
    pts += next((p for limit, p in DST_POINTS_ALLOWED if allowed <= limit), DST_POINTS_ALLOWED_ELSE)
    return pts


def fantasy_points(position: str, stats: dict, weights: dict | None = None, projected: bool = False) -> float:
    """Points for one player-week. Actual stat lines score zero if the player did not play;
    projections have no games-played field, so pass projected=True for them."""
    if not stats or (not projected and not _s(stats, "gp")):
        return 0.0
    if position == "K":
        return round(kicker_points(stats), 2)
    if position == "DEF":
        return round(dst_points(stats), 2)
    weights = weights or load_league()["scoring"]
    return round(offense_points(stats, weights), 2)
