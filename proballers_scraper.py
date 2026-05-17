from bs4 import BeautifulSoup
import pandas as pd
import psycopg2
import random
import json
import re
import undetected_chromedriver as uc
import time
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

# --- INITIALIZE ROBOTIC CHROME ---
print("Waking up the robotic browser...")
options = uc.ChromeOptions()
# We are keeping the window VISIBLE for this test so we can watch it solve Cloudflare.
# options.add_argument('--headless')

# --- Database Connection ---
def get_db_connection():
    return psycopg2.connect(
        dbname="Your database name",
        user="postgres",
        password= "your password",
        host="localhost",
        port="5432"
    )


def initialize_driver():
    options = uc.ChromeOptions()
    options.page_load_strategy = 'eager'
    options.add_argument('--window-size=1920,1080')

    # Optional: If you are running this in the background, uncomment the line below.
    # Note: Some strict sites detect headless mode more easily.
    # options.add_argument('--headless')

    driver = uc.Chrome(
        options=options,
        version_main=148,  # Match this to the exact version of the driver you downloaded
    )
    driver.implicitly_wait(5)
    return driver

# --- Then define your global driver right below it so it's ready for Stage 1 & 2 ---
driver = initialize_driver()


def add_to_queue(url, page_type, priority=1, team_id=None):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO scraping_queue (url, page_type, status, priority, team_id)
            VALUES (%s, %s, 'PENDING', %s, %s)
            ON CONFLICT (url) DO NOTHING;
        """, (url, page_type, priority, team_id))
        conn.commit()
    except Exception as e:
        print(f"Queue Insert Error: {e}")
        conn.rollback()
    finally:
        cur.close(); conn.close()


def get_next_in_queue(page_type):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT url, team_id FROM scraping_queue 
            WHERE status = 'PENDING' AND page_type = %s 
            ORDER BY priority DESC, queue_id ASC 
            LIMIT 1;
        """, (page_type,))

        row = cur.fetchone()

        # If a row is found, row[0] is the url, row[1] is the team_id
        return (row[0], row[1]) if row else (None, None)

    except Exception as e:
        print(f"Error fetching from queue: {e}")
        return None, None
    finally:
        cur.close()
        conn.close()

def mark_completed(url):
    """Marks a URL as COMPLETED so we don't scrape it again."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE scraping_queue SET status = 'COMPLETED' WHERE url = %s", (url,))
        conn.commit()
    except Exception as e:
        print(f"Error updating status: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

def extract_player_id_from_url(url):
    """Extracts '75446' from '.../player/75446/jaylon-brown/games'"""
    parts = url.split('/')
    for part in parts:
        if part.isdigit():
            return int(part)
    return None

def extract_proballers_info(url, entity_type):
    """
    Pass 'league', 'team', or 'player' as the entity_type.
    Returns a tuple: (ID, Name).
    Example: returns (168, 'turkey-bsl')
    """
    parts = url.split('/')
    if entity_type in parts:
        idx = parts.index(entity_type)
        if idx + 2 < len(parts):
            if parts[idx+1].isdigit():
                clean_name = parts[idx+2].replace('-', ' ').title()
                return int(parts[idx+1]), clean_name
    return None, None


def ensure_player_exists(player_id, player_slug, birth_year=None, height_cm=None):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        clean_name = player_slug.replace('-', ' ').title()

        # We use ON CONFLICT DO UPDATE so if we scrape a player again later,
        # it updates their height/age if we missed it the first time!
        cur.execute("""
            INSERT INTO players (player_id, full_name, birth_year, height_cm) 
            VALUES (%s, %s, %s, %s) 
            ON CONFLICT (player_id) DO UPDATE 
            SET birth_year = EXCLUDED.birth_year, height_cm = EXCLUDED.height_cm;
        """, (player_id, clean_name, birth_year, height_cm))
        conn.commit()
    except Exception as e:
        print(f"Error ensuring player exists: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

# --- Helper Functions for Data Cleaning ---

def parse_made_attempted(raw_stat_string):
    """Splits '2-6' into 2 (Made) and 6 (Attempted)."""
    clean_string = raw_stat_string.strip()
    if not clean_string or clean_string == "-":
        return 0, 0
    try:
        made, attempted = clean_string.split('-')
        return int(made), int(attempted)
    except ValueError:
        return 0, 0


def parse_percentage(pct_string):
    """Converts '46.7%' into a clean 46.7 float."""
    clean = pct_string.strip().replace('%', '')
    if not clean or clean == '-':
        return 0.0
    try:
        return float(clean)
    except ValueError:
        return 0.0


# --- The Phase 2 Spider (League to Teams) ---
def spider_league_for_teams(league_url):
    print(f"Crawling League: {league_url}")

    league_id, league_name = extract_proballers_info(league_url, 'league')
    if not league_id:
        print("Could not extract League ID from URL. Aborting.")
        return

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO leagues (league_id, league_name) 
            VALUES (%s, %s) ON CONFLICT DO NOTHING;
        """, (league_id, league_name))
        conn.commit()
    except Exception as e:
        print(f"DB Error saving league: {e}")
        conn.rollback()

    # --- LOAD LEAGUE PAGE ---
    try:
        driver.get(league_url)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/basketball/team/']"))
        )
        time.sleep(1)
        html = driver.page_source
        soup = BeautifulSoup(html, 'html.parser')
    except Exception as e:
        print(f"❌ Browser error: {e}")
        cur.close()
        conn.close()
        return

    url_parts = league_url.rstrip('/').split('/')
    target_year = url_parts[-1] if url_parts[-1].isdigit() else None

    processed_team_ids = set()
    teams_found = 0

    for link in soup.find_all('a', href=True):
        href = link['href']
        if "/basketball/team/" not in href:
            continue
        if "all-time-roster" in href or "team-records" in href:
            continue

        path_parts = href.split('/')
        # Last part must be a 4-digit year
        if not (path_parts[-1].isdigit() and len(path_parts[-1]) == 4):
            continue
        # Second-to-last must not be a known subpage
        subpage_keywords = ['schedule', 'players', 'roster', 'stats', 'games', 'records', 'calendar']
        if any(kw in path_parts[-2].lower() for kw in subpage_keywords):
            continue

        full_url = href if "proballers.com" in href else f"https://www.proballers.com{href}"
        team_id, team_name = extract_proballers_info(full_url, 'team')
        if not team_id:
            continue

        if team_id in processed_team_ids:
            continue
        processed_team_ids.add(team_id)

        try:
            cur.execute("""
                INSERT INTO teams (team_id, team_name, league_id) 
                VALUES (%s, %s, %s) ON CONFLICT DO NOTHING;
            """, (team_id, team_name, league_id))
            conn.commit()
            add_to_queue(full_url, page_type='team_roster', priority=2, team_id=team_id)
            teams_found += 1
        except Exception as e:
            conn.rollback()

    cur.close()
    conn.close()
    print(f"✅ Saved League {league_name.upper()} and queued {teams_found} teams!")


# --- The Phase 2 Spider (Team to Players) ---
def spider_team_for_players(team_url, team_id):
    print(f"\nCrawling Team: {team_url}")

    # NEW: Extract the year from the team URL (e.g., .../anadolu-efes/2014)
    url_parts = team_url.rstrip('/').split('/')
    target_year = url_parts[-1] if url_parts[-1].isdigit() else None

    # --- AUTOMATED BROWSER CONNECTION ---
    try:
        driver.get(team_url)
        time.sleep(3)  # Wait for page to physically load
        html = driver.page_source
        soup = BeautifulSoup(html, 'html.parser')
    except Exception as e:
        print(f"❌ Browser crashed on Team page: {e}")
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE scraping_queue SET status = 'ERROR' WHERE url = %s", (team_url,))
        conn.commit()
        cur.close()
        conn.close()
        return
    # ------------------------------------

    links = soup.find_all('a', href=True)
    unique_player_links = set()

    for link in links:
        href = link['href']
        if "/basketball/player/" in href:
            # Clean the URL just in case
            base_player_url = href.split('/games')[0]
            full_url = base_player_url if "proballers.com" in base_player_url else f"https://www.proballers.com{base_player_url}"

            # NEW: Force the specific historical year into the games URL!
            if target_year:
                games_url = f"{full_url}/games/{target_year}"
            else:
                games_url = f"{full_url}/games"

            unique_player_links.add(games_url)

    for games_url in unique_player_links:
        add_to_queue(games_url, page_type='player_games', priority=3, team_id=team_id)

    print(f"✅ Queued {len(unique_player_links)} players for year {target_year} (Team ID: {team_id})")
    mark_completed(team_url)

def get_cached_player_demographics(player_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT birth_year, height_cm FROM players WHERE player_id = %s", (player_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    if row and row[0] is not None and row[1] is not None:
        return row[0], row[1]
    return None, None

# --- Main Scraping Function ---
def scrape_single_player_logs(url, player_id):
    print(f"Processing: {url}")

    # --- 1. Check cache first ---
    birth_year, height_cm = get_cached_player_demographics(player_id)
    if birth_year is not None and height_cm is not None:
        print(f"   ✅ Using cached demographics: {height_cm} cm, born {birth_year}")
    else:
        # --- 2. Visit profile page and scan full text ---
        base_profile_url = re.sub(r'/games.*', '', url)
        print(f"   Fetching profile: {base_profile_url}")
        driver.get(base_profile_url)

        # Minimal wait for page body
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except:
            pass
        time.sleep(1)  # brief moment for text to render

        page_text = driver.find_element(By.TAG_NAME, 'body').text

        # Extract height (pattern: "1m93" or "193 cm")
        h_match = re.search(r'(\d)\s*m\s*(\d{2})', page_text)
        if h_match:
            height_cm = int(h_match.group(1)) * 100 + int(h_match.group(2))
        else:
            cm_match = re.search(r'(\d{3})\s*cm', page_text)
            height_cm = int(cm_match.group(1)) if cm_match else None

        # Extract birth year (first 4-digit year starting with 19/20)
        y_match = re.search(r'\b(19|20)\d{2}\b', page_text)
        birth_year = int(y_match.group(0)) if y_match else None

        print(f"   ✅ Text scan: {height_cm} cm, born {birth_year}")

    # --- 3. Go to games page and scrape logs ---
    print(f"   Loading games: {url}")
    driver.get(url)
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#regul tbody tr td"))
        )
    except:
        time.sleep(2)

    html = driver.page_source
    soup = BeautifulSoup(html, 'html.parser')

    # --- 4. Extract game logs ---
    games_list = []
    regular_season_box = soup.find('section', id='europe')
    if regular_season_box:
        table = regular_season_box.find('table', class_='table')
        if table:
            tbody = table.find('tbody')
            rows = tbody.find_all('tr') if tbody else table.find_all('tr')[1:]
            print(f"   -> Found {len(rows)} rows.")
            for row in rows:
                cols = row.find_all('td')
                if len(cols) < 10:
                    continue
                c = [col.text.strip() for col in cols]
                try:
                    fg2m, fg2a = parse_made_attempted(c[7]) if len(c) > 7 else (0,0)
                    fg3m, fg3a = parse_made_attempted(c[8]) if len(c) > 8 else (0,0)
                    ftm, fta = parse_made_attempted(c[10]) if len(c) > 10 else (0,0)
                    game_data = {
                        "Date": c[1] if len(c) > 1 else "",
                        "Score_Opp": c[2] if len(c) > 2 else "",
                        "MIN": int(c[6]) if len(c) > 6 and c[6].isdigit() else 0,
                        "PTS": int(c[3]) if len(c) > 3 and c[3].isdigit() else 0,
                        "FG2M": fg2m, "FG2A": fg2a, "FG3M": fg3m, "FG3A": fg3a,
                        "FTM": ftm, "FTA": fta,
                        "FG_PCT": parse_percentage(c[9]) if len(c) > 9 else 0.0,
                        "FT_PCT": parse_percentage(c[11]) if len(c) > 11 else 0.0,
                        "OFF_REB": int(c[12]) if len(c) > 12 and c[12].isdigit() else 0,
                        "DEF_REB": int(c[13]) if len(c) > 13 and c[13].isdigit() else 0,
                        "TOT_REB": int(c[4]) if len(c) > 4 and c[4].isdigit() else 0,
                        "AST": int(c[5]) if len(c) > 5 and c[5].isdigit() else 0,
                        "STL": int(c[16]) if len(c) > 16 and c[16].isdigit() else 0,
                        "TOV": int(c[17]) if len(c) > 17 and c[17].isdigit() else 0,
                        "BLK": int(c[18]) if len(c) > 18 and c[18].isdigit() else 0,
                        "EFF": int(c[20]) if len(c) > 20 and c[20].replace('-','',1).isdigit() else 0,
                    }
                    games_list.append(game_data)
                except Exception as e:
                    print(f"   ⚠️ Row parse error: {e}")
                    continue
    if not games_list:
        print("   ⚠️ No games extracted.")
    else:
        print(f"   ✅ Extracted {len(games_list)} game rows.")

    return pd.DataFrame(games_list), height_cm, birth_year

def parse_basketball_date(date_str, season_start_year):
    try:
        temp_date = pd.to_datetime(date_str)

        # Apply basketball season logic
        if temp_date.month >= 8:
            final_year = season_start_year
        else:
            final_year = season_start_year + 1

        return temp_date.replace(year=final_year).strftime('%Y-%m-%d')
    except Exception:
        return None


def insert_game_logs_to_db(df, player_id, team_id, player_url):
    """Takes the scraped DataFrame and inserts it into the game_logs table."""
    conn = get_db_connection()
    cur = conn.cursor()
    games_inserted = 0

    # NEW: Extract the season year from the URL we are currently scraping
    url_parts = player_url.rstrip('/').split('/')
    season_start_year = int(url_parts[-1]) if url_parts[-1].isdigit() else 2024

    try:
        for index, row in df.iterrows():
            # NEW: Use our custom basketball date parser
            db_date = parse_basketball_date(row['Date'], season_start_year)

            if not db_date:
                print(f"   ⚠️ DB Reject: Could not parse date format '{row['Date']}'")
                continue  # Skip if date is totally unreadable

            cur.execute("""
                    INSERT INTO game_logs (
                        player_id, team_id, game_date, minutes_played, points, 
                        fg2m, fg2a, fg3m, fg3a, ftm, fta, 
                        fg_pct, ft_pct, off_reb, def_reb, tot_reb, 
                        assists, steals, turnovers, blocks, efficiency
                    ) VALUES (
                        %s, %s, %s, %s, %s, 
                        %s, %s, %s, %s, %s, %s, 
                        %s, %s, %s, %s, %s, 
                        %s, %s, %s, %s, %s
                    ) ON CONFLICT (player_id, game_date) DO NOTHING;
                """, (
                player_id, team_id, db_date, row['MIN'], row['PTS'],
                row['FG2M'], row['FG2A'], row['FG3M'], row['FG3A'], row['FTM'], row['FTA'],
                row['FG_PCT'], row['FT_PCT'], row['OFF_REB'], row['DEF_REB'], row['TOT_REB'],
                row['AST'], row['STL'], row['TOV'], row['BLK'], row['EFF']
            ))
            games_inserted += cur.rowcount

        conn.commit()
        print(f"✅ DB Insert: {games_inserted} games for Player {player_id}.")
    except Exception as e:
        print(f"❌ DATABASE INSERTION ERROR: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()


# --- Execution Test (FULL PRODUCTION PIPELINE) ---
if __name__ == "__main__":
    print("🧪 INITIALIZING FULL PIPELINE")

    target_years = range(2014, 2025)

    league_base_urls = [
        "https://www.proballers.com/basketball/league/192/eurocup",
    ]

   # --- STAGE 1: LEAGUE CRAWLER ---
    print("\n--- STAGE 1: LEAGUE CRAWLER ---")
    for base_url in league_base_urls:
        for year in target_years:
            seed_league_url = f"{base_url}/teams/{year}"
            spider_league_for_teams(seed_league_url)
            time.sleep(2)


    # --- STAGE 2: TEAM ROSTER CRAWLER ---
    print("\n--- STAGE 2: TEAM ROSTER CRAWLER ---")
    teams_processed = 0
    while True:
        team_url, team_id = get_next_in_queue('team_roster')
        if not team_url:
            print("No more teams pending! Moving to Phase 3.")
            break

        spider_team_for_players(team_url, team_id)

        ''' # TRIAL LIMIT: Stop after exactly 1 team
               teams_processed += 1
                if teams_processed >= 1:
                    print("🛑 TRIAL LIMIT REACHED: Stopping Team Crawler after 1 team.")
                    break '''

    time.sleep(2)

    # --- STAGE 3: PLAYER GAME LOG EXTRACTOR ---
    print("\n--- STAGE 3: PLAYER GAME LOG EXTRACTOR ---")
    players_processed = 0
    try:
        while True:
            player_url, team_id = get_next_in_queue('player_games')

            if not player_url:
                print("🎉 PIPELINE COMPLETE! All queued URLs have been processed. 🎉")
                break

            print(f"\nTarget Acquired: {player_url}")

            # Extract Player ID and Slug
            url_parts = player_url.split('/')
            player_id = None
            player_slug = "Unknown"

            for i, part in enumerate(url_parts):
                if part.isdigit():
                    player_id = int(part)
                    if i + 1 < len(url_parts): player_slug = url_parts[i + 1]
                    break

            if not player_id:
                print("No player ID found in URL.")
                continue

            # --- ROBUST EXTRACTION BLOCK ---
            try:
                result = scrape_single_player_logs(player_url, player_id)

                if result:
                    print("✅ Data successfully extracted.")
                    df_games, height, birth_year = result
                    ensure_player_exists(player_id, player_slug, birth_year, height)

                    if not df_games.empty:
                        insert_game_logs_to_db(df_games, player_id, team_id, player_url)

                    mark_completed(player_url)
                else:
                    print("❌ Page extraction failed (Data not found). Marking as ERROR.")
                    conn = get_db_connection()
                    cur = conn.cursor()
                    cur.execute("UPDATE scraping_queue SET status = 'ERROR' WHERE url = %s", (player_url,))
                    conn.commit()
                    cur.close()
                    conn.close()

                '''# TRIAL LIMIT: Stop after exactly 2 players
                players_processed += 1
                if players_processed >= 2:
                    print("🛑 TRIAL LIMIT REACHED: Stopping Player Crawler after 2 players.")
                    break '''

                time.sleep(3)

            except Exception as e:
                # [KEEP YOUR EXISTING ERROR HANDLING HERE]
                print(f"❌ CRITICAL ERROR during extraction for {player_url}: {e}")
                break  # Just break out for the trial if a critical error hits


    finally:
        print("Shutting down the robotic browser cleanly...")
        try:
            driver.quit()
        except OSError:
            pass  # ignore "invalid handle" error on Windows
        except Exception:
            pass