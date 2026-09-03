import requests, json, os, time, math
from datetime import datetime, timedelta

LEAGUES = {
    'BL1': {'name': 'Bundesliga', 'id': 2002},
    'DED': {'name': 'Eredivisie', 'id': 2003},
    'PL': {'name': 'Premier League', 'id': 2021},
    'PPL': {'name': 'Primeira Liga', 'id': 2017},
    'PD': {'name': 'La Liga', 'id': 2014},
}

BASE = "https://api.football-data.org/v4"
HEAD = {"X-Auth-Token": os.getenv("FOOTBALL_DATA_API_KEY", "")}

REQUEST_DELAY = 6.5  # free tier is 10 req/min — this keeps every call safely under that

team_form_cache = {}
standings_cache = {}


def _get(url, timeout=15):
    """Single choke point for every HTTP call, so the rate-limit delay is
    applied consistently on every request — success, failure, or exception —
    instead of being scattered (and sometimes skipped) across call sites."""
    try:
        r = requests.get(url, headers=HEAD, timeout=timeout)
        time.sleep(REQUEST_DELAY)
        if r.status_code != 200:
            print(f"  [!] {r.status_code} on {url} — {r.text[:200]}")
        return r
    except Exception as e:
        time.sleep(REQUEST_DELAY)
        print(f"  [!] request failed: {url} ({e})")
        return None


def poisson_pmf(k, lam):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def poisson_cdf(k, lam):
    return sum(poisson_pmf(i, lam) for i in range(k + 1))


def get_standings(league_code):
    if league_code in standings_cache:
        return standings_cache[league_code]
    r = _get(f"{BASE}/competitions/{league_code}/standings")
    if r is None or r.status_code != 200:
        return {}
    try:
        table = r.json()['standings'][0]['table']
        pos_map = {
            t['team']['id']: {
                'pos': t['position'], 'pts': t['points'],
                'gf': t['goalsFor'], 'ga': t['goalsAgainst'],
                'played': t.get('playedGames', 0),
            } for t in table
        }
        standings_cache[league_code] = pos_map
        return pos_map
    except Exception:
        return {}


def league_avg_conceded(standings):
    """Average goals conceded per game across the table — used to normalize
    an individual team's conceding rate against the league's actual scoring
    environment, instead of a guessed constant."""
    rates = [
        v['ga'] / v['played'] for v in standings.values()
        if v.get('played')
    ]
    return sum(rates) / len(rates) if rates else 1.3  # sane fallback if standings unavailable


def get_team_form(team_id):
    if team_id in team_form_cache:
        return team_form_cache[team_id]
    url = f"{BASE}/teams/{team_id}/matches?limit=5&status=FINISHED"
    r = _get(url)
    if r is None or r.status_code != 200:
        return None
    try:
        matches = r.json().get('matches', [])[-5:]
        if not matches:
            return None
        scored, conceded, results = [], [], []
        for m in matches:
            is_home = m['homeTeam']['id'] == team_id
            hs = m['score']['fullTime']['home']
            aws = m['score']['fullTime']['away']
            if hs is None:
                continue
            s = hs if is_home else aws
            c = aws if is_home else hs
            scored.append(s)
            conceded.append(c)
            results.append('W' if s > c else ('D' if s == c else 'L'))
        if not scored:
            return None
        form = {
            'avg_scored': round(sum(scored) / len(scored), 2),
            'avg_conceded': round(sum(conceded) / len(conceded), 2),
            'total_avg': round((sum(scored) + sum(conceded)) / len(scored), 1),
            'form_str': '-'.join(results[::-1]),
            'last5_goals': f"{sum(scored)} scored in last {len(scored)}",
        }
        team_form_cache[team_id] = form
        return form
    except Exception:
        return None


def predict(h_form, a_form, lg_avg_conceded):
    """Real Poisson projection from the actual form data — this is the part
    that was previously disconnected (a per-league guess + the match ID's
    digits stood in for a real calculation). Same shape as our earlier
    goal_watch.py: each side's scoring rate normalized against how much the
    opponent's league tends to concede."""
    exp_home = h_form['avg_scored'] * (a_form['avg_conceded'] / lg_avg_conceded)
    exp_away = a_form['avg_scored'] * (h_form['avg_conceded'] / lg_avg_conceded)
    exp_total = round(exp_home + exp_away, 2)

    p_over25 = 1 - poisson_cdf(2, exp_total)
    p_home_scores = 1 - poisson_pmf(0, exp_home)
    p_away_scores = 1 - poisson_pmf(0, exp_away)
    p_btts = p_home_scores * p_away_scores  # independence assumption — see earlier caveat

    return exp_total, round(p_over25 * 100), round(p_btts * 100)


def get_games():
    out = []
    date_to = (datetime.utcnow() + timedelta(days=14)).strftime("%Y-%m-%d")
    date_from = datetime.utcnow().strftime("%Y-%m-%d")

    for code, meta in LEAGUES.items():
        standings = get_standings(code)
        lg_avg_conceded = league_avg_conceded(standings)

        url = f"{BASE}/competitions/{code}/matches?dateFrom={date_from}&dateTo={date_to}"
        r = _get(url)
        if r is None or r.status_code != 200:
            print(f"{meta['name']}: matches fetch failed, skipping league")
            continue

        try:
            matches = r.json().get('matches', [])
        except Exception:
            print(f"{meta['name']}: couldn't parse matches response")
            continue

        print(f"{meta['name']}: {len(matches)} matches in window")
        added = 0
        for m in matches[:4]:
            # football-data.org marks matches with a confirmed kickoff time
            # as TIMED rather than SCHEDULED — both mean "upcoming, not yet
            # played." Filtering only on SCHEDULED silently drops most
            # near-term fixtures, which is very likely why nothing showed up.
            if m['status'] not in ('SCHEDULED', 'TIMED'):
                continue

            h_id, a_id = m['homeTeam']['id'], m['awayTeam']['id']
            h_form = get_team_form(h_id)
            a_form = get_team_form(a_id)

            # If either side's real form is unavailable (rate-limited, new
            # team, etc.), skip the game rather than silently substituting
            # a fake number that looks like a real prediction.
            if not h_form or not a_form:
                print(f"  skipping {m['homeTeam']['shortName']} vs {m['awayTeam']['shortName']}: missing form data (home={bool(h_form)}, away={bool(a_form)})")
                continue

            exp_total, over25, btts = predict(h_form, a_form, lg_avg_conceded)

            h_pos = standings.get(h_id, {})
            a_pos = standings.get(a_id, {})

            out.append({
                "league": meta['name'],
                "match": f"{m['homeTeam']['shortName']} vs {m['awayTeam']['shortName']}",
                "date": m['utcDate'][:16].replace("T", " ") + " UTC",
                "exp_total": exp_total, "over25": over25, "btts": btts,
                "home_team": m['homeTeam']['shortName'], "away_team": m['awayTeam']['shortName'],
                "home_form": h_form, "away_form": a_form,
                "home_pos": f"{h_pos.get('pos','?')}th ({h_pos.get('pts','?')}pts)" if h_pos else "",
                "away_pos": f"{a_pos.get('pos','?')}th ({a_pos.get('pts','?')}pts)" if a_pos else "",
            })
            added += 1

        print(f"{meta['name']}: added {added} games")

    return sorted(out, key=lambda x: x['exp_total'], reverse=True)[:8]


def make_html(games):
    cards = ""
    for g in games:
        cards += f"""
<div style="background:#1e1e1e;border-radius:12px;padding:16px;margin:12px 0;border:1px solid #333">
  <div style="display:flex;justify-content:space-between"><span style="background:#ff1a1a;padding:4px 8px;border-radius:6px;font-size:12px">{g['league']}</span><span style="color:#999;font-size:11px">{g['date']}</span></div>
  <div style="font-size:18px;font-weight:bold;margin:10px 0;color:white">{g['match']} <span style="font-size:11px;color:#666">{g['home_pos']} vs {g['away_pos']}</span></div>
  <div style="display:flex;justify-content:space-between;text-align:center;margin:10px 0">
    <div><div style="color:#aaa;font-size:11px">EXP</div><div style="color:#ffeb3b;font-size:20px;font-weight:bold">{g['exp_total']}</div></div>
    <div><div style="color:#aaa;font-size:11px">OVER 2.5</div><div style="color:#ffeb3b;font-size:20px;font-weight:bold">{g['over25']}%</div></div>
    <div><div style="color:#aaa;font-size:11px">BTTS</div><div style="color:#ffeb3b;font-size:20px;font-weight:bold">{g['btts']}%</div></div>
  </div>
  <div style="background:#0f0f0f;border-radius:8px;padding:8px;margin-top:10px;display:flex;justify-content:space-between;font-size:11px">
    <div><div style="color:#888">🏠 {g['home_team']} [{g['home_form']['form_str']}]</div><div style="color:white">{g['home_form']['avg_scored']} scored • {g['home_form']['avg_conceded']} conc • avg {g['home_form']['total_avg']}</div></div>
    <div style="text-align:right"><div style="color:#888">✈️ {g['away_team']} [{g['away_form']['form_str']}]</div><div style="color:white">{g['away_form']['avg_scored']} scored • {g['away_form']['avg_conceded']} conc • avg {g['away_form']['total_avg']}</div></div>
  </div>
</div>"""
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Baboo Goal IQ</title></head>
<body style="background:#121212;color:white;font-family:Arial;padding:12px;max-width:600px;margin:auto">
<h2 style="text-align:center">⚽ BABOO GOAL IQ v2 — REAL FORM</h2>
<p style="text-align:center;color:#888;font-size:11px">Real last-5 avg + standings, Poisson-projected · {datetime.now().strftime('%d %b %H:%M')} BST</p>
{cards if cards else '<p style="text-align:center;color:#888">No scheduled fixtures with usable form data right now — check back once the season is underway.</p>'}
</body></html>"""
    return html


def main():
    games = get_games()
    # No hardcoded demo fallback anymore — an empty state is shown honestly
    # in make_html() instead, so you're never looking at fake numbers
    # without knowing it.

    os.makedirs('docs', exist_ok=True)
    html = make_html(games)
    for p in ['docs/goal_watch.json', 'goal_watch.json']:
        with open(p, 'w') as f:
            json.dump(games, f, indent=2)
    for p in ['docs/index.html', 'docs/goal_watch.html']:
        with open(p, 'w') as f:
            f.write(html)
    print(f"Done v2 — {len(games)} real, fully-computed games")


if __name__ == "__main__":
    main()
