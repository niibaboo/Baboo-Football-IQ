import json, requests, os, time
from datetime import datetime, timedelta

API_KEY = os.getenv('FOOTBALL_DATA_API_KEY')
HEAD = {'X-Auth-Token': API_KEY} if API_KEY else {}

# FIXED - NO ITALY, NO BSA (BSA crashes API), NO 5.5 BUG
LEAGUES = {
    'BL1': {'name': 'Bundesliga', 'boost': 0.35},
    'DED': {'name': 'Eredivisie', 'boost': 0.45},
    'PL': {'name': 'Premier League', 'boost': 0.20},
    'PPL': {'name': 'Primeira Liga', 'boost': 0.25},
    'PD': {'name': 'La Liga', 'boost': 0.10},
}

BASE = "https://api.football-data.org/v4"

def get_games():
    out=[]
    for code, meta in LEAGUES.items():
        try:
            url = f"{BASE}/competitions/{code}/matches?dateFrom={datetime.now().date()}&dateTo={(datetime.now()+timedelta(days=7)).date()}"
            r=requests.get(url, headers=HEAD, timeout=15)
            if r.status_code!=200:
                print(f"{code} {r.status_code}"); continue
            for m in r.json().get('matches',[])[:6]:
                if m['status']!='SCHEDULED': continue
                # CAPPED EXP 2.9-4.2 MAX
                exp = round(min(4.2, 2.65 + meta['boost'] + (hash(m['homeTeam']['shortName'])%8)/100),2)
                exp = max(2.9, exp)
                over25 = int(52 + (exp-2.9)*18) # 52-78%
                btts = int(48 + (exp-2.9)*12)

                out.append({
                    "league": meta['name'],
                    "league_code": code,
                    "match": f"{m['homeTeam']['shortName']} vs {m['awayTeam']['shortName']}",
                    "date": m['utcDate'][:10]+" "+m['utcDate'][11:16],
                    "exp_total": exp, # CAPPED
                    "over25": over25,
                    "btts": btts,
                    "home_form": {"avg_scored": round(1.2+meta['boost'],1), "avg_conceded": 1.1, "total_avg": round(2.3+meta['boost'],1)},
                    "away_form": {"avg_scored": 1.3, "avg_conceded": 1.2, "total_avg": 2.5}
                })
            time.sleep(1)
        except Exception as e:
            print(e); continue
    return sorted(out, key=lambda x:x['exp_total'], reverse=True)[:15]

def main():
    games=get_games()
    print(f"WRITING {len(games)} games")
    os.makedirs('docs', exist_ok=True)
    with open('docs/goal_watch.json','w') as f:
        json.dump(games,f,indent=2)
    # also root for safety
    with open('goal_watch.json','w') as f:
        json.dump(games,f,indent=2)

if __name__=="__main__":
    main()
