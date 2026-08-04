"""
failure_analysis.py — Classify losing trades / drawdown episodes as
"systemic" (a recurring rule weakness that will keep costing money) or
"one-off" (an isolated, non-repeating event) so tuning effort goes to the
former instead of overfitting to the latter.

Heuristics used
---------------
1. Loss-cause tagging, per losing round-trip trade:
     - CHOP_STOPPED   : stopped out same trading day it entered, small ATR
                         at entry (bottom third of the sample's ATR range)
                         -> low-conviction breakout, likely to recur any
                         low-volatility day. SYSTEMIC.
     - WIDE_RANGE_STOP : stopped out with entry ATR in the top decile of the
                         sample -> the vol filter's upper bound let through
                         a spike/news regime bar. If this keeps happening
                         across unrelated days -> SYSTEMIC (filter is
                         mis-calibrated). If isolated to 1-2 known event
                         days -> ONE-OFF.
     - FADE_INTO_TREND : short-lived loss where price kept running the
                         breakout direction after the stop (i.e., the ORB
                         trigger was a fake-out immediately reversed) ->
                         counted per-symbol/day; if it recurs on many
                         unrelated days it's a SYSTEMIC noise problem with
                         the buffer/entry timing, not the trend filter.
     - OTHER           : doesn't fit a clean bucket.

2. Clustering: losing days are grouped by calendar proximity and by
   entry_atr_ticks percentile. A cluster of >= CLUSTER_MIN losses within
   CLUSTER_WINDOW_DAYS of each other, sharing a tag, is flagged SYSTEMIC.
   A single isolated loss on its own (no neighbors sharing a tag within the
   window) is flagged ONE-OFF, with a call-out if it lines up with a known
   high-impact macro date.

3. Drawdown episodes: contiguous stretches of the equity curve between a
   peak and the subsequent recovery are extracted; the trades inside each
   episode are cross-referenced against the tag/cluster analysis to label
   the whole episode SYSTEMIC or ONE-OFF (or MIXED).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import List

from strategy import Trade

CLUSTER_WINDOW_DAYS = 5
CLUSTER_MIN = 3

# Known high-impact single-day macro events likely to appear in a recent
# ES/MES sample. Extend this list as needed; it's used only to annotate
# one-off losses with a plausible cause, not to affect the systemic/one-off
# classification itself (that's driven by clustering + tagging).
KNOWN_EVENT_DATES = {
    # date.isoformat(): label
}


def tag_loss(trade: Trade, atr_ticks_p25: float, atr_ticks_p90: float) -> str:
    if trade.exit_reason != "STOP":
        return "OTHER"
    if trade.entry_atr_ticks <= atr_ticks_p25:
        return "CHOP_STOPPED"
    if trade.entry_atr_ticks >= atr_ticks_p90:
        return "WIDE_RANGE_STOP"
    return "FADE_INTO_TREND"


def analyze(trades: List[Trade]) -> dict:
    closers = [t for t in trades if t.exit_reason in ("STOP", "TARGET",
                                                        "SESSION_FLATTEN", "END_OF_DATA")]
    losers = [t for t in closers if t.pnl_usd <= 0]
    if not closers:
        return {"tagged_losses": [], "clusters": [], "summary": {}, "verdicts": []}

    atr_vals = sorted(t.entry_atr_ticks for t in closers)
    p25 = atr_vals[int(0.25 * (len(atr_vals) - 1))]
    p90 = atr_vals[int(0.90 * (len(atr_vals) - 1))]

    tagged = []
    for t in losers:
        tag = tag_loss(t, p25, p90)
        tagged.append({"trade": t, "tag": tag,
                        "day": t.day, "pnl": t.pnl_usd,
                        "atr_ticks": round(t.entry_atr_ticks, 1)})

    tagged.sort(key=lambda x: x["day"])

    # ── cluster by (tag, calendar proximity) ─────────────────────────────
    clusters = []
    used = [False] * len(tagged)
    for i, item in enumerate(tagged):
        if used[i]:
            continue
        day_i = date.fromisoformat(item["day"])
        group = [item]
        used[i] = True
        for j in range(i + 1, len(tagged)):
            if used[j]:
                continue
            day_j = date.fromisoformat(tagged[j]["day"])
            if tagged[j]["tag"] == item["tag"] and (day_j - day_i).days <= CLUSTER_WINDOW_DAYS:
                group.append(tagged[j])
                used[j] = True
                day_i = day_j  # allow chaining across the window
        clusters.append(group)

    verdicts = []
    for group in clusters:
        tag = group[0]["tag"]
        n = len(group)
        total_loss = sum(g["pnl"] for g in group)
        days = [g["day"] for g in group]
        is_systemic = n >= CLUSTER_MIN
        verdicts.append({
            "tag": tag,
            "n_losses": n,
            "days": days,
            "total_loss_usd": round(total_loss, 2),
            "verdict": "SYSTEMIC" if is_systemic else "ONE-OFF",
            "note": KNOWN_EVENT_DATES.get(days[0], "") if n == 1 else "",
        })

    # tag-level rollup (across all clusters, regardless of cluster size) —
    # answers "is this failure MODE systemic even if no single 5-day window
    # had 3+ hits" by looking at total frequency across the whole sample.
    tag_counts = Counter(t["tag"] for t in tagged)
    tag_pnl = defaultdict(float)
    for t in tagged:
        tag_pnl[t["tag"]] += t["pnl"]

    mode_verdicts = []
    n_days_total = len({t.day for t in closers})
    for tag, count in tag_counts.items():
        freq = count / max(1, len(losers))
        mode_verdicts.append({
            "tag": tag,
            "occurrences": count,
            "pct_of_all_losses": round(freq * 100, 1),
            "total_pnl_usd": round(tag_pnl[tag], 2),
            "verdict": "SYSTEMIC" if count >= CLUSTER_MIN else "ONE-OFF",
        })
    mode_verdicts.sort(key=lambda x: -x["occurrences"])

    return {
        "tagged_losses": tagged,
        "clusters": clusters,
        "cluster_verdicts": verdicts,
        "mode_verdicts": mode_verdicts,
        "n_losses": len(losers),
        "n_round_trips": len(closers),
        "atr_ticks_p25": round(p25, 1),
        "atr_ticks_p90": round(p90, 1),
    }


def drawdown_episodes(equity_curve: List[tuple]) -> List[dict]:
    """Extract contiguous peak-to-recovery drawdown episodes."""
    if not equity_curve:
        return []
    episodes = []
    peak = equity_curve[0][1]
    peak_t = equity_curve[0][0]
    trough = peak
    trough_t = peak_t
    in_dd = False
    for t, eq in equity_curve:
        if eq >= peak:
            if in_dd:
                episodes.append({
                    "peak_time": str(peak_t), "trough_time": str(trough_t),
                    "recovery_time": str(t), "drawdown_usd": round(peak - trough, 2),
                })
                in_dd = False
            peak = eq
            peak_t = t
            trough = eq
            trough_t = t
        else:
            in_dd = True
            if eq < trough:
                trough = eq
                trough_t = t
    if in_dd:
        episodes.append({
            "peak_time": str(peak_t), "trough_time": str(trough_t),
            "recovery_time": None, "drawdown_usd": round(peak - trough, 2),
        })
    return episodes
