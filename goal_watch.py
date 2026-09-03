def main():
    games=get_games()
    print(f"WRITING {len(games)} games")
    if len(games)==0:
        print("No games - keeping old file, not overwriting with []")
        return
    os.makedirs('docs', exist_ok=True)
    os.makedirs('docs/data', exist_ok=True)
    with open('docs/goal_watch.json','w') as f:
        json.dump(games,f,indent=2)
    with open('docs/data/matches.json','w') as f:
        json.dump({'updated': datetime.now().isoformat(), 'games': games}, f, indent=2)
    with open('goal_watch.json','w') as f:
        json.dump(games,f,indent=2)
