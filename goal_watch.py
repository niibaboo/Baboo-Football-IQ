#!/usr/bin/env python3
"""
Goal Watch — Over 2.5 / BTTS Predictor
For each upcoming fixture in the selected competitions, combines the home
team's HOME scoring+conceding record with the away team's AWAY
scoring+conceding record (recency-weighted, last 5 by venue), normalizes
against the league average, and projects expected goals for each side.
From there: P(Over 2.5) via Poisson on the combined total, and P(BTTS Yes)
as the product of each side's P(scores >= 1) — a simplifying independence
assumption (real models use a small correlation correction between the two
scorelines; this doesn't, so treat BTTS edges with a bit more skepticism
than Over/Under edges).

Setup:
    1. Free API key: https://www.football-data.org/client/register
    2. export FOOTBALL_DATA_API_KEY=your_key_here
    3. pip3 install requests --break-system-packages
    4. python3 goal_watch.py

Output:
    docs/goal_watch.html — open in your browser

Note: free tier is rate-limited to 10 req/min. This script paces itself
accordingly. Runtime scales with how many teams have upcoming fixtures
across the selected competitions — expect several minutes, not seconds.
"""

import os
import sys
import time
import math
import json
from datetime import datetime, timedelta
import requests

BASE = "https://api.football-data.org/v4"
API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
REQUEST_DELAY = 6.5  # seconds between calls — stays under the 10 req/min free-tier limit
FIXTURE_WINDOW_DAYS = 7  # how far ahead to look for upcoming matches

COMPETITIONS = {
    "PL": "Premier League",
    "BL1": "Bundesliga",
    "DED": "Eredivisie",
    "FL1": "Ligue 1",
    "PPL": "Primeira Liga",
}

RECENT_WEIGHT_N = 5  # last N matches by venue


def api_get(path, params=None):
    if not API_KEY:
        print("ERROR: set FOOTBALL_DATA_API_KEY before running.", file=sys.stderr)
        sys.exit(1)
    headers = {"X-Auth-Token": API_KEY}
    r = requests.get(f"{BASE}{path}", headers=headers, params=params or {})
    time.sleep(REQUEST_DELAY)
    if r.status_code == 429:
        print("Rate limited — waiting 60s and retrying once...")
        time.sleep(60)
        r = requests.get(f"{BASE}{path}", headers=headers, params=params or {})
        time.sleep(REQUEST_DELAY)
    r.raise_for_status()
    return r.json()


def poisson_pmf(k, lam):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def poisson_cdf(k, lam):
    return sum(poisson_pmf(i, lam) for i in range(k + 1))


def weighted_avg(values):
    if not values:
        return None
    n = len(values)
    wts = [1.4 ** i for i in range(n)]  # most recent (last index) weighted highest
    return sum(w * v for w, v in zip(wts, values)) / sum(wts)


def get_upcoming_matches(comp_code):
    date_from = datetime.utcnow().date().isoformat()
    date_to = (datetime.utcnow().date() + timedelta(days=FIXTURE_WINDOW_DAYS)).isoformat()
    data = api_get(f"/competitions/{comp_code}/matches", params={
        "status": "SCHEDULED", "dateFrom": date_from, "dateTo": date_to,
    })
    return data.get("matches", [])


def get_venue_record(team_id, comp_code, venue):
    """venue = 'HOME' or 'AWAY'. Returns (scored_list, conceded_list), oldest first."""
    data = api_get(f"/teams/{team_id}/matches", params={
        "status": "FINISHED", "competitions": comp_code, "venue": venue,
        "limit": RECENT_WEIGHT_N,
    })
    matches = data.get("matches", [])
    matches.sort(key=lambda m: m.get("utcDate", ""))
    scored, conceded = [], []
    for m in matches:
        ft = m.get("score", {}).get("fullTime", {})
        hg, ag = ft.get("home"), ft.get("away")
        if hg is None or ag is None:
            continue
        if venue == "HOME":
            scored.append(hg)
            conceded.append(ag)
        else:
            scored.append(ag)
            conceded.append(hg)
    return scored, conceded


def build_predictions():
    report = {}
    for code, name in COMPETITIONS.items():
        print(f"Fetching upcoming {name} fixtures…")
        fixtures = get_upcoming_matches(code)
        if not fixtures:
            report[code] = {"name": name, "fixtures": []}
            continue

        cache = {}

        def record(team_id, venue):
            key = (team_id, venue)
            if key not in cache:
                cache[key] = get_venue_record(team_id, code, venue)
            return cache[key]

        fixture_data = []
        for m in fixtures:
            home = m["homeTeam"]
            away = m["awayTeam"]
            print(f"  {home['name']} vs {away['name']}")
            h_scored, h_conceded = record(home["id"], "HOME")
            a_scored, a_conceded = record(away["id"], "AWAY")
            fixture_data.append({
                "match_date": m.get("utcDate", ""),
                "home_name": home["name"], "away_name": away["name"],
                "home_scored": h_scored, "home_conceded": h_conceded,
                "away_scored": a_scored, "away_conceded": a_conceded,
            })

        all_home_scored = [weighted_avg(f["home_scored"]) for f in fixture_data if f["home_scored"]]
        all_away_scored = [weighted_avg(f["away_scored"]) for f in fixture_data if f["away_scored"]]
        all_home_conceded = [weighted_avg(f["home_conceded"]) for f in fixture_data if f["home_conceded"]]
        all_away_conceded = [weighted_avg(f["away_conceded"]) for f in fixture_data if f["away_conceded"]]

        lg_home_scored = sum(all_home_scored) / len(all_home_scored) if all_home_scored else 1.5
        lg_away_scored = sum(all_away_scored) / len(all_away_scored) if all_away_scored else 1.1
        lg_home_conceded = sum(all_home_conceded) / len(all_home_conceded) if all_home_conceded else 1.1
        lg_away_conceded = sum(all_away_conceded) / len(all_away_conceded) if all_away_conceded else 1.5

        predictions = []
        for f in fixture_data:
            h_scored_avg = weighted_avg(f["home_scored"])
            a_conceded_avg = weighted_avg(f["away_conceded"])
            a_scored_avg = weighted_avg(f["away_scored"])
            h_conceded_avg = weighted_avg(f["home_conceded"])

            if None in (h_scored_avg, a_conceded_avg, a_scored_avg, h_conceded_avg):
                continue

            exp_home = h_scored_avg * (a_conceded_avg / lg_away_conceded)
            exp_away = a_scored_avg * (h_conceded_avg / lg_home_conceded)
            exp_total = exp_home + exp_away

            p_over25 = 1 - poisson_cdf(2, exp_total)
            p_home_scores = 1 - poisson_pmf(0, exp_home)
            p_away_scores = 1 - poisson_pmf(0, exp_away)
            p_btts = p_home_scores * p_away_scores

            predictions.append({
                "date": f["match_date"], "home": f["home_name"], "away": f["away_name"],
                "exp_home": round(exp_home, 2), "exp_away": round(exp_away, 2),
                "exp_total": round(exp_total, 2), "p_over25": round(p_over25 * 100, 1),
                "p_btts": round(p_btts * 100, 1),
                "home_scored_l5": f["home_scored"], "home_conceded_l5": f["home_conceded"],
                "away_scored_l5": f["away_scored"], "away_conceded_l5": f["away_conceded"],
            })

        predictions.sort(key=lambda x: x["exp_total"], reverse=True)
        report[code] = {"name": name, "fixtures": predictions}
    return report


HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Goal Watch — {generated_date}</title>
<style>
  :root{{--bg:#0b0f14; --panel:#121820; --panel2:#161d27; --border:#233040; --text:#e8edf2; --sub:#8b98a8; --yellow:#facc15; --green:#22c55e;}}
  body{{margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; padding:16px; max-width:640px; margin:0 auto;}}
  h1{{font-size:20px; margin-bottom:4px;}}
  .sub{{color:var(--sub); font-size:13px; margin-bottom:18px;}}
  .compGroup{{margin-bottom:18px; border:1px solid var(--border); border-radius:12px; overflow:hidden; background:var(--panel);}}
  .compHead{{background:var(--panel2); padding:10px 14px; font-size:14px; font-weight:700;}}
  .fixtureRow{{padding:12px 14px; border-top:1px solid var(--border);}}
  .matchup{{font-weight:700; font-size:15px; margin-bottom:2px;}}
  .matchDate{{font-size:11px; color:var(--sub); margin-bottom:8px;}}
  .l5{{font-size:11px; color:var(--sub); margin-bottom:8px; line-height:1.6;}}
  .predRow{{display:flex; gap:10px;}}
  .predBox{{flex:1; background:var(--panel2); border-radius:8px; padding:8px; text-align:center;}}
  .predLabel{{font-size:10px; color:var(--sub); text-transform:uppercase;}}
  .predNum{{font-size:18px; font-weight:800; color:var(--yellow);}}
  .noFixtures{{padding:14px; color:var(--sub); font-size:12px; font-style:italic;}}
  .footnote{{font-size:11px; color:var(--sub); text-align:center; margin-top:20px; line-height:1.6;}}
</style></head>
<body>
<h1>⚽ Goal Watch</h1>
<div class="sub">Over 2.5 / BTTS projections · generated {generated_date}</div>
{comp_html}
<div class="footnote">Expected goals combine each team's venue-specific (home/away) scoring and conceding record, recency-weighted over their last 5 matches at that venue, normalized against this window's league averages. BTTS assumes independence between the two scorelines — a simplification real models correct for — so treat BTTS edges with more caution than Over/Under.</div>
</body></html>
"""

COMP_TEMPLATE = """<div class="compGroup">
  <div class="compHead">{name}</div>
  {fixture_rows}
</div>"""

FIXTURE_TEMPLATE = """<div class="fixtureRow">
  <div class="matchup">{home} vs {away}</div>
  <div class="matchDate">{date}</div>
  <div class="l5">{home} home L5 — scored: {home_scored} · conceded: {home_conceded}<br>{away} away L5 — scored: {away_scored} · conceded: {away_conceded}</div>
  <div class="predRow">
    <div class="predBox"><div class="predLabel">Exp. Total</div><div class="predNum">{exp_total}</div></div>
    <div class="predBox"><div class="predLabel">Over 2.5</div><div class="predNum">{p_over25}%</div></div>
    <div class="predBox"><div class="predLabel">BTTS Yes</div><div class="predNum">{p_btts}%</div></div>
  </div>
</div>"""


def render_html(report):
    comp_html = []
    for code, data in report.items():
        if not data["fixtures"]:
            rows = '<div class="noFixtures">No upcoming fixtures in range, or insufficient venue-specific data.</div>'
        else:
            rows = "".join(FIXTURE_TEMPLATE.format(
                home=f["home"], away=f["away"], date=f["date"][:10],
                home_scored="·".join(str(g) for g in f["home_scored_l5"]),
                home_conceded="·".join(str(g) for g in f["home_conceded_l5"]),
                away_scored="·".join(str(g) for g in f["away_scored_l5"]),
                away_conceded="·".join(str(g) for g in f["away_conceded_l5"]),
                exp_total=f["exp_total"], p_over25=f["p_over25"], p_btts=f["p_btts"],
            ) for f in data["fixtures"])
        comp_html.append(COMP_TEMPLATE.format(name=data["name"], fixture_rows=rows))
    return HTML_TEMPLATE.format(
        generated_date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        comp_html="".join(comp_html),
    )


if __name__ == "__main__":
    report = build_predictions()

    os.makedirs("docs", exist_ok=True)
    with open("docs/goal_watch.html", "w") as f:
        f.write(render_html(report))
    with open("docs/goal_watch.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    print("\nDone. Written to docs/goal_watch.html")
