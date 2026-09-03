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


def league_avg_scored(standings):
    rates = [
        v['gf'] / v['played'] for v in standings.values()
        if v.get('played')
    ]
    return sum(rates) / len(rates) if rates else 1.3


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
        scored_ht, conceded_ht = [], []  # half-time goals — same match data, just the other score field
        clean_sheets, blanks = 0, 0       # clean sheet = didn't concede; blank = didn't score
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
            if c == 0:
                clean_sheets += 1
            if s == 0:
                blanks += 1

            ht = m.get('score', {}).get('halfTime', {})
            hs_ht, aws_ht = ht.get('home'), ht.get('away')
            if hs_ht is not None and aws_ht is not None:
                scored_ht.append(hs_ht if is_home else aws_ht)
                conceded_ht.append(aws_ht if is_home else hs_ht)

        if not scored:
            return None
        n = len(scored)
        form = {
            'avg_scored': round(sum(scored) / n, 2),
            'avg_conceded': round(sum(conceded) / n, 2),
            'total_avg': round((sum(scored) + sum(conceded)) / n, 1),
            'form_str': '-'.join(results[::-1]),
            'last5_goals': f"{sum(scored)} scored in last {n}",
            'n_games': n,
            'clean_sheet_pct': round(clean_sheets / n * 100),
            'blank_pct': round(blanks / n * 100),
        }
        if scored_ht:
            n_ht = len(scored_ht)
            form['avg_scored_ht'] = round(sum(scored_ht) / n_ht, 2)
            form['avg_conceded_ht'] = round(sum(conceded_ht) / n_ht, 2)
        team_form_cache[team_id] = form
        return form
    except Exception:
        return None


PRIOR_STRENGTH = 3  # "worth" of league-average games blended in for small samples


def shrink(form, lg_scored, lg_conceded, scored_key='avg_scored', conceded_key='avg_conceded'):
    """Early season (or after a long injury gap etc.), a team might only
    have 1-2 real games on record — trusting that tiny sample as a stable
    'true rate' is exactly the bug that produced 8-9 expected goals from a
    single 1-5 loss. This blends the team's own average with the league
    average, weighted by how many real games back it up: with n_games=1 the
    league average dominates; by n_games=5+ the team's own form dominates.
    Same idea as the outlier-capping fix in the MLB tool, adapted for
    'too little data' instead of 'one freak result in enough data'.

    scored_key/conceded_key let this same function shrink either the
    full-time or half-time stats — both need this protection for the same
    reason, and a 1-game half-time sample is if anything MORE volatile
    (fewer minutes, more likely to be 0-0) than a 1-game full-time sample.
    """
    n = form.get('n_games', 1)
    w_scored = (n * form[scored_key] + PRIOR_STRENGTH * lg_scored) / (n + PRIOR_STRENGTH)
    w_conceded = (n * form[conceded_key] + PRIOR_STRENGTH * lg_conceded) / (n + PRIOR_STRENGTH)
    return round(w_scored, 2), round(w_conceded, 2)


def predict(h_form, a_form, lg_avg_conceded, lg_avg_scored=None, h2h=None):
    """Real Poisson projection from the actual form data — this is the part
    that was previously disconnected (a per-league guess + the match ID's
    digits stood in for a real calculation). Same shape as our earlier
    goal_watch.py: each side's scoring rate normalized against how much the
    opponent's league tends to concede.

    Both teams' raw averages are first shrunk toward the league average
    based on sample size (see shrink()) — this keeps a 1-game sample from
    producing an absurd projection while barely affecting a team with a
    full 5-game sample.

    Also projects a first-half total from the same match data (half-time
    scores were already being fetched, just unused), and — if h2h stats are
    passed in with a big enough sample — nudges the full-time total lightly
    toward this specific matchup's own scoring history. The h2h nudge is
    deliberately light (15%) and gated on sample size, so it adds real
    signal for well-worn rivalries without letting a 2-game h2h sample
    swing the number the way the early-season single-game bug did.
    """
    if lg_avg_scored is None:
        lg_avg_scored = lg_avg_conceded  # fallback if caller doesn't have it separately

    h_scored, h_conceded = shrink(h_form, lg_avg_scored, lg_avg_conceded)
    a_scored, a_conceded = shrink(a_form, lg_avg_scored, lg_avg_conceded)

    exp_home = h_scored * (a_conceded / lg_avg_conceded)
    exp_away = a_scored * (h_conceded / lg_avg_conceded)
    exp_total = exp_home + exp_away

    if h2h and h2h.get('n_matches', 0) >= 3:
        exp_total = 0.85 * exp_total + 0.15 * h2h['avg_goals']
    exp_total = round(exp_total, 2)

    p_over25 = 1 - poisson_cdf(2, exp_total)
    p_home_scores = 1 - poisson_pmf(0, exp_home)
    p_away_scores = 1 - poisson_pmf(0, exp_away)
    p_btts = p_home_scores * p_away_scores  # independence assumption — see earlier caveat

    result = {
        'exp_total': exp_total,
        'over25': round(p_over25 * 100),
        'btts': round(p_btts * 100),
    }

    # First-half projection — only if both sides have half-time data.
    # Uses the SAME shrinkage as the full-time stats (this was missing
    # before, which is why a 1-game sample produced a bare "0.0" — a
    # single 0-0-at-half match was being trusted as the team's true rate).
    # League half-time average is approximated as half the full-time
    # league average — a reasonable proxy since no separate HT standings
    # data exists to compute a real one.
    if 'avg_scored_ht' in h_form and 'avg_scored_ht' in a_form:
        lg_scored_ht = max(lg_avg_scored / 2, 0.1)
        lg_conceded_ht = max(lg_avg_conceded / 2, 0.1)
        h_scored_ht, h_conceded_ht = shrink(h_form, lg_scored_ht, lg_conceded_ht,
                                             'avg_scored_ht', 'avg_conceded_ht')
        a_scored_ht, a_conceded_ht = shrink(a_form, lg_scored_ht, lg_conceded_ht,
                                             'avg_scored_ht', 'avg_conceded_ht')

        exp_home_ht = h_scored_ht * (a_conceded_ht / lg_conceded_ht)
        exp_away_ht = a_scored_ht * (h_conceded_ht / lg_conceded_ht)
        exp_ht_total = round(exp_home_ht + exp_away_ht, 2)
        result['exp_ht_total'] = exp_ht_total
        result['over05_ht'] = round((1 - poisson_cdf(0, exp_ht_total)) * 100)
        result['over15_ht'] = round((1 - poisson_cdf(1, exp_ht_total)) * 100)

    return result


def get_head2head(match_id):
    """Historical results between these two specific teams — some matchups
    run consistently high or low scoring regardless of either team's
    general form (tight tactical rivalries, one team who always sits deep
    against the other, etc). Costs one extra request per fixture.

    Note: I couldn't verify football-data.org's exact head2head response
    field names against a live call — if this silently returns None for
    every fixture, check the printed parse-failure message below against
    the actual API docs and adjust the field names.
    """
    r = _get(f"{BASE}/matches/{match_id}/head2head", timeout=15)
    if r is None or r.status_code != 200:
        return None
    try:
        data = r.json()
        agg = data.get('aggregates', {})
        n = agg.get('numberOfMatches', 0)
        if not n:
            return None
        total_goals = agg.get('totalGoals')
        if total_goals is None:
            return None
        return {
            'n_matches': n,
            'avg_goals': round(total_goals / n, 2),
            'home_wins': agg.get('homeTeam', {}).get('wins'),
            'away_wins': agg.get('awayTeam', {}).get('wins'),
            'draws': agg.get('homeTeam', {}).get('draws'),
        }
    except Exception as e:
        print(f"  [!] head2head parse failed for match {match_id}: {e}")
        return None


def get_games():
    out = []
    date_to = (datetime.utcnow() + timedelta(days=14)).strftime("%Y-%m-%d")
    date_from = datetime.utcnow().strftime("%Y-%m-%d")

    for code, meta in LEAGUES.items():
        standings = get_standings(code)
        lg_avg_conceded = league_avg_conceded(standings)
        lg_avg_scored = league_avg_scored(standings)

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

            h2h = get_head2head(m['id'])
            proj = predict(h_form, a_form, lg_avg_conceded, lg_avg_scored, h2h)

            h_pos = standings.get(h_id, {})
            a_pos = standings.get(a_id, {})

            out.append({
                "league": meta['name'],
                "match": f"{m['homeTeam']['shortName']} vs {m['awayTeam']['shortName']}",
                "date": m['utcDate'][:16].replace("T", " ") + " UTC",
                "home_team": m['homeTeam']['shortName'], "away_team": m['awayTeam']['shortName'],
                "home_form": h_form, "away_form": a_form,
                "home_pos": f"{h_pos.get('pos','?')}th ({h_pos.get('pts','?')}pts)" if h_pos else "",
                "away_pos": f"{a_pos.get('pos','?')}th ({a_pos.get('pts','?')}pts)" if a_pos else "",
                "h2h": h2h,
                **proj,
            })
            added += 1

        print(f"{meta['name']}: added {added} games")

    return sorted(out, key=lambda x: x['exp_total'], reverse=True)[:8]


def make_html(games):
    cards = ""
    for g in games:
        ht_row = ""
        if 'exp_ht_total' in g:
            ht_row = f"""
  <div style="display:flex;justify-content:space-between;text-align:center;margin-top:6px">
    <div><div style="color:#aaa;font-size:10px">1H EXP</div><div style="color:#7ec8ff;font-size:15px;font-weight:bold">{g['exp_ht_total']}</div></div>
    <div><div style="color:#aaa;font-size:10px">1H OVER 0.5</div><div style="color:#7ec8ff;font-size:15px;font-weight:bold">{g['over05_ht']}%</div></div>
    <div><div style="color:#aaa;font-size:10px">1H OVER 1.5</div><div style="color:#7ec8ff;font-size:15px;font-weight:bold">{g['over15_ht']}%</div></div>
  </div>"""

        h2h_row = ""
        h2h = g.get('h2h')
        if h2h:
            h2h_row = f"""
  <div style="color:#888;font-size:10px;margin-top:8px;text-align:center">H2H (last {h2h['n_matches']}): {h2h['avg_goals']} goals/game avg · {h2h.get('home_wins','?')}W-{h2h.get('draws','?')}D-{h2h.get('away_wins','?')}W</div>"""

        hf, af = g['home_form'], g['away_form']
        cards += f"""
<div style="background:#1e1e1e;border-radius:12px;padding:16px;margin:12px 0;border:1px solid #333">
  <div style="display:flex;justify-content:space-between"><span style="background:#ff1a1a;padding:4px 8px;border-radius:6px;font-size:12px">{g['league']}</span><span style="color:#999;font-size:11px">{g['date']}</span></div>
  <div style="font-size:18px;font-weight:bold;margin:10px 0;color:white">{g['match']} <span style="font-size:11px;color:#666">{g['home_pos']} vs {g['away_pos']}</span></div>
  <div style="display:flex;justify-content:space-between;text-align:center;margin:10px 0">
    <div><div style="color:#aaa;font-size:11px">EXP</div><div style="color:#ffeb3b;font-size:20px;font-weight:bold">{g['exp_total']}</div></div>
    <div><div style="color:#aaa;font-size:11px">OVER 2.5</div><div style="color:#ffeb3b;font-size:20px;font-weight:bold">{g['over25']}%</div></div>
    <div><div style="color:#aaa;font-size:11px">BTTS</div><div style="color:#ffeb3b;font-size:20px;font-weight:bold">{g['btts']}%</div></div>
  </div>{ht_row}
  <div style="background:#0f0f0f;border-radius:8px;padding:8px;margin-top:10px;display:flex;justify-content:space-between;font-size:11px">
    <div><div style="color:#888">🏠 {g['home_team']} [{hf['form_str']}]</div><div style="color:white">{hf['avg_scored']} scored • {hf['avg_conceded']} conc • avg {hf['total_avg']}</div><div style="color:#666;font-size:10px">clean sheets {hf['clean_sheet_pct']}% • blanks {hf['blank_pct']}%</div></div>
    <div style="text-align:right"><div style="color:#888">✈️ {g['away_team']} [{af['form_str']}]</div><div style="color:white">{af['avg_scored']} scored • {af['avg_conceded']} conc • avg {af['total_avg']}</div><div style="color:#666;font-size:10px">clean sheets {af['clean_sheet_pct']}% • blanks {af['blank_pct']}%</div></div>
  </div>{h2h_row}
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
