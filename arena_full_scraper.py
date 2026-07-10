import csv
import json
import os
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

STATUS_FILE = "web_data.json"
TARGET_FILE = "targets.csv"

def load_target_list():
    """CSVからターゲットリストを読み込む"""
    targets = {}
    if not os.path.exists(TARGET_FILE):
        print(f"【警告】{TARGET_FILE} が見つかりません。")
        return targets
        
    with open(TARGET_FILE, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            targets[row['iidx_id'].strip()] = {
                "custom_name": row['player_name'].strip(),
                "memo": row['memo'].strip()
            }
    return targets

def parse_arena_ranking(html_content, arena_ranking):
    """1. アリーナ(勝利数)ランキングから ID -> DJ NAME と 勝利数を抽出"""
    soup = BeautifulSoup(html_content, 'html.parser')
    tables = soup.find_all('table')
    if len(tables) == 0:
        return

    rows = tables[0].find_all('tr')
    for row in rows:
        cols = row.find_all('td')
        if len(cols) >= 7:
            try:
                name_id_text = cols[1].get_text(separator='\n', strip=True).split('\n')
                if len(name_id_text) < 2:
                    continue
                dj_name = name_id_text[0].strip()
                iidx_id = name_id_text[1].strip()
                
                wins_str = cols[6].get_text(strip=True)
                wins = int(wins_str.replace('勝', '').replace(',', ''))

                arena_ranking[iidx_id] = {
                    "dj_name": dj_name,
                    "wins": wins
                }
            except Exception:
                pass

def parse_cube_ranking(html_content, cube_ranking):
    """2. キューブランキングから DJ NAME -> キューブ数を抽出"""
    soup = BeautifulSoup(html_content, 'html.parser')
    tables = soup.find_all('table')
    if len(tables) == 0:
        return

    rows = tables[0].find_all('tr')
    for row in rows:
        cols = row.find_all('td')
        if len(cols) >= 5:
            try:
                dj_name = cols[1].get_text(strip=True)
                cube_str = cols[4].get_text(strip=True)
                cubes = int(cube_str.replace(',', ''))
                cube_ranking[dj_name] = cubes
            except Exception:
                pass

def main():
    JST = timezone(timedelta(hours=9))
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] ハイブリッド監視バッチを開始します...")
    
    target_list = load_target_list()
    if not target_list:
        return
        
    try:
        with open(STATUS_FILE, 'r', encoding='utf-8') as f:
            prev_players = json.load(f).get("players", {})
    except FileNotFoundError:
        prev_players = {}
    
    arena_ranking = {}
    cube_ranking = {}
    max_pages = 5 

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True) 
        page = browser.new_page()
        
        # 巡回1: アリーナランキング
        url_arena = "https://p.eagate.573.jp/game/2dx/33/ranking/arena/top_ranking.html"
        page.goto(url_arena, wait_until="networkidle")
        page.wait_for_timeout(1000)
        page.locator("li[data-play-style='0']").click()
        page.wait_for_timeout(1000)
        for current_page in range(1, max_pages + 1):
            parse_arena_ranking(page.content(), arena_ranking)
            if current_page < max_pages:
                page.locator("div.page-next").first.click()
                page.wait_for_timeout(2000) 
                
        # 巡回2: キューブランキング
        url_cube = "https://p.eagate.573.jp/game/2dx/33/ranking/arena/ranking.html?season_id=5&display=1"
        page.goto(url_cube, wait_until="networkidle")
        page.wait_for_timeout(1000)
        for current_page in range(1, max_pages + 1):
            parse_cube_ranking(page.content(), cube_ranking)
            if current_page < max_pages:
                page.locator("div.page-next").first.click()
                page.wait_for_timeout(2000) 

        browser.close()

    # 3. オンライン判定
    output_players = {}
    
    for target_id, info in target_list.items():
        if target_id in arena_ranking:
            current_arena = arena_ranking[target_id]
            official_name = current_arena["dj_name"]
            current_wins = current_arena["wins"]
            
            is_cube_known = official_name in cube_ranking
            current_cubes = cube_ranking[official_name] if is_cube_known else None
            
            prev_info = prev_players.get(target_id, {})
            prev_wins = prev_info.get("wins", current_wins)
            prev_cubes = prev_info.get("cubes", current_cubes)
            prev_status = prev_info.get("status", "OFFLINE")
            last_active = prev_info.get("last_active", "データなし")
            
            is_online = False
            
            if is_cube_known and current_cubes is not None and prev_cubes is not None:
                if current_cubes > prev_cubes:
                    is_online = True
                    print(f"🔥 キューブ変動検知: {official_name} がプレイ中！ ({prev_cubes} -> {current_cubes})")
            else:
                if current_wins > prev_wins:
                    is_online = True
                    print(f"🔥 勝利数変動検知(キューブ不明): {official_name} がプレイ中！ ({prev_wins}勝 -> {current_wins}勝)")
            
            status = "ONLINE" if is_online else prev_status
            
            output_players[target_id] = {
                "official_name": official_name,
                "custom_name": info["custom_name"],
                "memo": info["memo"],
                "wins": current_wins,
                "cubes": current_cubes,
                "status": status,
                "last_active": last_active
            }
        else:
            # ⬇️ 圏外だった場合、official_nameを "None" に指定 ⬇️
            output_players[target_id] = {
                "official_name": "None",
                "custom_name": info["custom_name"],
                "memo": info["memo"],
                "wins": 0,
                "cubes": None,
                "status": "UNKNOWN (圏外)",
                "last_active": "データなし"
            }

    # 4. JSON出力
    web_data = {
        "last_updated": now_str,
        "players": output_players
    }
    with open(STATUS_FILE, 'w', encoding='utf-8') as f:
        json.dump(web_data, f, ensure_ascii=False, indent=4)
        
    print(f"[{now_str}] 突合・更新完了。")

if __name__ == "__main__":
    main()
