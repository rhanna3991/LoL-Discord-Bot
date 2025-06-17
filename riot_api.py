import os
import aiohttp
import asyncio
from dotenv import load_dotenv
from functools import lru_cache
import json
import aiosqlite
import time
from collections import defaultdict

load_dotenv()
RIOT_API_KEY = os.getenv("RIOT_API_KEY")

# Cache configurations
match_cache = {}
match_history_cache = {}
puuid_cache = {}

# TTL configurations
MATCH_DATA_TTL = 32400  # 9 hours in seconds
MATCH_HISTORY_TTL = 600  # 10 minutes in seconds

# Rate limiting and metrics
rate_limit_lock = asyncio.Semaphore(20)  # allow 20 concurrent requests safely
cache_metrics = {
    "match_cache_hits": 0,
    "match_cache_misses": 0,
    "match_history_cache_hits": 0,
    "match_history_cache_misses": 0,
    "puuid_cache_hits": 0,
    "puuid_cache_misses": 0
}

# Global session
_session = None

async def get_session():
    global _session
    if _session is None:
        _session = aiohttp.ClientSession()
    return _session

async def close_session():
    global _session
    if _session:
        await _session.close()
        _session = None

# Replace the old fetch_json with this version
async def fetch_json(url, headers):
    session = await get_session()
    async with session.get(url, headers=headers) as response:
        if response.status == 429:
            retry_after = int(response.headers.get('Retry-After', 10))
            await asyncio.sleep(retry_after)
            return await fetch_json(url, headers)
        return await response.json() if response.status == 200 else None

# Add shutdown handler to LeagueBot's on_ready
async def cleanup():
    await close_session()

async def safe_request(session, url, headers, retries=3):
    for attempt in range(retries):
        async with rate_limit_lock:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 429:
                    retry_after = int(response.headers.get("Retry-After", "1"))
                    print(f"Rate limit hit. Retrying in {retry_after} seconds...")
                    await asyncio.sleep(retry_after)
                else:
                    print(f"Error {response.status}: {await response.text()}")
                    return None
    return None

async def ensure_puuid_table():
    async with aiosqlite.connect("riot_bot.db") as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS puuid_cache (
                riot_id TEXT PRIMARY KEY,
                puuid TEXT,
                cached_at INTEGER
            )
        ''')
        await conn.commit()

async def get_puuid_from_db(riot_id):
    async with aiosqlite.connect("riot_bot.db") as conn:
        async with conn.execute(
            "SELECT puuid FROM puuid_cache WHERE riot_id = ?",
            (riot_id.lower(),)
        ) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else None

async def save_puuid_to_db(riot_id, puuid):
    async with aiosqlite.connect("riot_bot.db") as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO puuid_cache (riot_id, puuid, cached_at) VALUES (?, ?, ?)",
            (riot_id.lower(), puuid, int(time.time()))
        )
        await conn.commit()

async def batch_fetch_puuids(riot_ids):
    """Fetch multiple PUUIDs in batch, using cache where possible"""
    to_fetch = []
    results = {}
    
    # Check cache first
    for riot_id in riot_ids:
        cached = await get_puuid_from_db(riot_id)
        if cached:
            results[riot_id] = cached
        else:
            to_fetch.append(riot_id)
    
    # Fetch missing PUUIDs in chunks
    chunk_size = 10  # Adjust based on rate limits
    for i in range(0, len(to_fetch), chunk_size):
        chunk = to_fetch[i:i + chunk_size]
        tasks = []
        for riot_id in chunk:
            if "#" not in riot_id:
                continue
            game_name, tag_line = riot_id.split("#", 1)
            task = get_account_by_riot_id(game_name, tag_line)
            tasks.append(task)
        
        chunk_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for riot_id, result in zip(chunk, chunk_results):
            if isinstance(result, Exception) or not result:
                continue
            puuid = result.get("puuid")
            if puuid:
                results[riot_id] = puuid
                await save_puuid_to_db(riot_id, puuid)
        
        await asyncio.sleep(1)  # Rate limit compliance
    
    return results

async def prefetch_puuids():
    """Pre-fetch PUUIDs for all tracked players across all guilds"""
    async with aiosqlite.connect("riot_bot.db") as conn:
        async with conn.execute("SELECT DISTINCT summoner_name FROM tracked_players") as cursor:
            players = await cursor.fetchall()
            riot_ids = [player[0] for player in players]
    
    if not riot_ids:
        return
    
    print(f"Pre-fetching PUUIDs for {len(riot_ids)} players...")
    results = await batch_fetch_puuids(riot_ids)
    print(f"Successfully cached {len(results)} PUUIDs")

# Update get_puuid to use the batch system for single lookups
async def get_puuid(riot_id):
    # Check memory cache first
    if riot_id in puuid_cache:
        return puuid_cache[riot_id]
    
    # Check database cache
    db_puuid = await get_puuid_from_db(riot_id)
    if db_puuid:
        puuid_cache[riot_id] = db_puuid
        return db_puuid
    
    # Fetch single PUUID using batch system
    results = await batch_fetch_puuids([riot_id])
    return results.get(riot_id)

async def get_account_by_riot_id(game_name, tag_line):
    headers = {"X-Riot-Token": RIOT_API_KEY}
    url = f"https://americas.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    return await fetch_json(url, headers)

async def get_summoner_by_puuid(region, puuid):
    async with aiohttp.ClientSession() as session:
        url = f"https://{region}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
        headers = {"X-Riot-Token": RIOT_API_KEY}
        return await fetch_json(url, headers)

_champion_data_cache = None

async def get_champion_data():
    global _champion_data_cache
    if _champion_data_cache is not None:
        return _champion_data_cache

    async with aiohttp.ClientSession() as session:
        version_url = "https://ddragon.leagueoflegends.com/api/versions.json"
        version_response = await fetch_json(version_url, {})
        version = version_response[0] if version_response else "14.24.1"

        champ_data_url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
        champ_data = await fetch_json(champ_data_url, {})

        id_to_name = {}
        name_to_id = {}
        if champ_data:
            for key, val in champ_data["data"].items():
                id_to_name[int(val["key"])] = val["name"]
                name_to_id[val["name"].lower()] = int(val["key"])

        _champion_data_cache = (id_to_name, name_to_id)
        return _champion_data_cache

# Persistent match data cache using SQLite
async def get_match_data_local(match_id):
    async with aiosqlite.connect("riot_bot.db") as conn:
        async with conn.execute("SELECT data, cached_at FROM match_data WHERE match_id = ?", (match_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                data, cached_at = row
                if time.time() - cached_at < MATCH_DATA_TTL:
                    return json.loads(data)
    return None

async def save_match_data_local(match_id, data):
    async with aiosqlite.connect("riot_bot.db") as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO match_data (match_id, data, cached_at) VALUES (?, ?, ?)",
            (match_id, json.dumps(data), int(time.time()))
        )
        await conn.commit()

# Ensure table exists at startup
async def ensure_match_data_table():
    async with aiosqlite.connect("riot_bot.db") as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS match_data (
                match_id TEXT PRIMARY KEY,
                data TEXT,
                cached_at INTEGER
            )
        ''')
        await conn.commit()

# Update get_cached_match_data to use persistent cache
async def get_cached_match_data(session, match_id):
    # 1. Try in-memory cache
    if match_id in match_cache:
        return match_cache[match_id]
    # 2. Try persistent cache
    match_data = await get_match_data_local(match_id)
    if match_data:
        match_cache[match_id] = match_data
        return match_data
    # 3. Fetch from Riot API
    data = await get_match_data(session, match_id)
    if data:
        match_cache[match_id] = data
        await save_match_data_local(match_id, data)
    return data

async def get_match_data(session, match_id):
    url = f"https://americas.api.riotgames.com/lol/match/v5/matches/{match_id}"
    headers = {"X-Riot-Token": RIOT_API_KEY}
    return await fetch_json(url, headers)

async def get_match_timeline(session, match_id):
    url = f"https://americas.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
    headers = {"X-Riot-Token": RIOT_API_KEY}
    return await fetch_json(url, headers)

async def get_summoner_rank(region, riot_id):
    """Get summoner rank using Riot ID format (GameName#TAG)"""
    puuid = await get_puuid(riot_id)
    if not puuid:
        return None
    async with aiohttp.ClientSession() as session:
        # Get summoner data using PUUID
        summoner_data = await get_summoner_by_puuid(region, puuid)
        if not summoner_data:
            print(f"Could not find summoner data for {riot_id}")
            return None
        summoner_id = summoner_data["id"]
        # Get rank data
        rank_url = f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-summoner/{summoner_id}"
        headers = {"X-Riot-Token": RIOT_API_KEY}
        ranks = await fetch_json(rank_url, headers)
        if not ranks:
            return None
        # Look for Solo/Duo queue rank
        for queue in ranks:
            if queue["queueType"] == "RANKED_SOLO_5x5":
                return {
                    "tier": queue["tier"],
                    "rank": queue["rank"],
                    "lp": queue["leaguePoints"]
                }
    return None

async def get_flex_rank(region, riot_id):
    """Get summoner's Flex queue rank using Riot ID format (GameName#TAG)"""
    puuid = await get_puuid(riot_id)
    if not puuid:
        return None
    async with aiohttp.ClientSession() as session:
        # Get summoner data using PUUID
        summoner_data = await get_summoner_by_puuid(region, puuid)
        if not summoner_data:
            print(f"Could not find summoner data for {riot_id}")
            return None
        summoner_id = summoner_data["id"]
        # Get rank data
        rank_url = f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-summoner/{summoner_id}"
        headers = {"X-Riot-Token": RIOT_API_KEY}
        ranks = await fetch_json(rank_url, headers)
        if not ranks:
            return None
        # Look for Flex queue rank
        for queue in ranks:
            if queue["queueType"] == "RANKED_FLEX_SR":
                return {
                    "tier": queue["tier"],
                    "rank": queue["rank"],
                    "lp": queue["leaguePoints"]
                }
    return None

async def get_cached_match_ids(session, puuid, count):
    now = time.time()
    key = (puuid, count)
    
    if key in match_history_cache:
        match_ids, cached_time = match_history_cache[key]
        if now - cached_time < MATCH_HISTORY_TTL:
            cache_metrics["match_history_cache_hits"] += 1
            return match_ids
    
    cache_metrics["match_history_cache_misses"] += 1
    url = f"https://americas.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}"
    headers = {"X-Riot-Token": RIOT_API_KEY}
    match_ids = await fetch_json(url, headers)
    
    if match_ids:
        match_history_cache[key] = (match_ids, now)
    return match_ids

async def get_match_history(region, riot_id, count=10):
    """Get recent match history for a player using Riot ID format (GameName#TAG), only Ranked Solo/Duo games (queueId 420)"""
    async with aiohttp.ClientSession() as session:
        puuid = await get_puuid(riot_id)
        if not puuid:
            return None

        match_ids = await get_cached_match_ids(session, puuid, count * 2)  # Get more matches to account for filtering
        if not match_ids:
            return None

        matches = []
        for match_id in match_ids:
            match_data = await get_cached_match_data(session, match_id)
            if not match_data:
                continue

            # Only include Ranked Solo/Duo games (queueId 420)
            if match_data["info"].get("queueId") != 420:
                continue

            # Skip remakes (games that ended very early)
            if match_data["info"]["gameDuration"] < 180:  # 3 minutes in seconds
                continue

            # Find the player's data in the match
            for participant in match_data["info"]["participants"]:
                if participant["puuid"] == puuid:
                    matches.append({
                        "matchId": match_id,
                        "champion": participant["championName"],
                        "kills": participant["kills"],
                        "deaths": participant["deaths"],
                        "assists": participant["assists"],
                        "win": participant["win"],
                        "gameMode": match_data["info"]["gameMode"],
                        "gameDuration": match_data["info"]["gameDuration"],
                        "timestamp": match_data["info"]["gameStartTimestamp"]
                    })
                    break

            # Stop if we've collected enough ranked games
            if len(matches) >= count:
                break

        return matches

async def get_detailed_match_history(region, riot_id, count=20):
    """Get detailed match history including all stats needed for /stats and /feederscore commands"""
    async with aiohttp.ClientSession() as session:
        puuid = await get_puuid(riot_id)
        if not puuid:
            return None

        match_ids = await get_cached_match_ids(session, puuid, count * 2)  # Get more matches to account for filtering
        if not match_ids:
            return None

        detailed_matches = []
        for match_id in match_ids:
            match_data = await get_cached_match_data(session, match_id)
            if not match_data:
                continue

            # Only include Ranked Solo/Duo games (queueId 420)
            if match_data["info"].get("queueId") != 420:
                continue

            # Find the player's data in the match
            player_data = None
            player_team_id = None
            for participant in match_data["info"]["participants"]:
                if participant["puuid"] == puuid:
                    player_data = participant
                    player_team_id = participant["teamId"]
                    break
            if not player_data:
                continue

            # Calculate team totals for percentage calculations
            team_kills = 0
            team_damage = 0
            team_gold = 0
            team_tower_damage = 0
            
            for participant in match_data["info"]["participants"]:
                if participant["teamId"] == player_team_id:
                    team_kills += participant["kills"]
                    team_damage += participant["totalDamageDealtToChampions"]
                    team_gold += participant["goldEarned"]
                    team_tower_damage += participant.get("damageDealtToBuildings", 0)
            
            # Get timeline data for death times and objective timestamps
            timeline_data = await get_match_timeline(session, match_id)
            death_times = []
            objective_timestamps = []
            first_blood_kill = False
            first_blood_assist = False
            first_blood_victim = False
            
            if timeline_data:
                participant_id = player_data["participantId"]
                found_first_blood = False
                
                for frame in timeline_data["info"]["frames"]:
                    for event in frame.get("events", []):
                        if event["type"] == "CHAMPION_KILL":
                            if not found_first_blood:
                                found_first_blood = True
                                if event.get("killerId") == participant_id:
                                    first_blood_kill = True
                                elif event.get("assistingParticipantIds") and participant_id in event.get("assistingParticipantIds"):
                                    first_blood_assist = True
                                elif event.get("victimId") == participant_id:
                                    first_blood_victim = True
                            
                            if event.get("victimId") == participant_id:
                                death_times.append(event["timestamp"] / 1000)
                        
                        if event["type"] in ["ELITE_MONSTER_KILL", "BUILDING_KILL"]:
                            if event.get("monsterType") in ["DRAGON", "BARON_NASHOR", "RIFTHERALD"]:
                                objective_timestamps.append(event["timestamp"])
            
            detailed_match = {
                "matchId": match_id,
                "champion": player_data["championName"],
                "kills": player_data["kills"],
                "deaths": player_data["deaths"],
                "assists": player_data["assists"],
                "win": player_data["win"],
                "gameMode": match_data["info"]["gameMode"],
                "gameDuration": match_data["info"]["gameDuration"],
                "timestamp": match_data["info"]["gameStartTimestamp"],
                "cs": player_data["totalMinionsKilled"] + player_data["neutralMinionsKilled"],
                "visionScore": player_data["visionScore"],
                "damageDealtToChampions": player_data["totalDamageDealtToChampions"],
                "goldEarned": player_data["goldEarned"],
                "killParticipation": ((player_data["kills"] + player_data["assists"]) / max(1, team_kills)) * 100,
                "damageShare": (player_data["totalDamageDealtToChampions"] / max(1, team_damage)) * 100,
                "goldShare": (player_data["goldEarned"] / max(1, team_gold)) * 100,
                "teamKills": team_kills,
                "totalMinionsKilled": player_data["totalMinionsKilled"],
                "neutralMinionsKilled": player_data["neutralMinionsKilled"],
                "damageDealtToBuildings": player_data.get("damageDealtToBuildings", 0),
                "damageDealtToTurrets": player_data.get("damageDealtToTurrets", 0),
                "teamTowerDamage": team_tower_damage,
                "goldDiff": player_data.get("goldDiff", 0),
                "xpDiff": player_data.get("xpDiff", 0),
                "firstBloodKill": first_blood_kill,
                "firstBloodAssist": first_blood_assist,
                "firstBloodVictim": first_blood_victim,
                "deathTimes": death_times,
                "objectiveTimestamps": objective_timestamps,
                "turretKills": player_data.get("turretKills", 0),
                "inhibitorKills": player_data.get("inhibitorKills", 0),
                "totalDamageDealt": player_data.get("totalDamageDealt", 0),
                "largestKillingSpree": player_data.get("largestKillingSpree", 0),
                "championLevel": player_data.get("champLevel", 0),
            }
            
            detailed_matches.append(detailed_match)
            
            # Stop if we've collected enough ranked games
            if len(detailed_matches) >= count:
                break

        return detailed_matches

async def get_specific_champion_mastery(region, riot_id, champion_name):
    """Get mastery data for a specific champion using Riot ID format (GameName#TAG)"""
    if "#" not in riot_id:
        print(f"Invalid Riot ID format: {riot_id}")
        return None
    
    game_name, tag_line = riot_id.split("#", 1)
    
    async with aiohttp.ClientSession() as session:
        # First, get the account to get the PUUID
        account_data = await get_account_by_riot_id(game_name, tag_line)
        if not account_data:
            print(f"Could not find account for {riot_id}")
            return None
        
        puuid = account_data["puuid"]
        
        # Get champion data
        id_to_name, name_to_id = await get_champion_data()
        champion_id = name_to_id.get(champion_name.lower())
        if not champion_id:
            print(f"Could not find champion ID for {champion_name}")
            return None
        
        # Get mastery data for specific champion
        mastery_url = f"https://{region}.api.riotgames.com/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/by-champion/{champion_id}"
        headers = {"X-Riot-Token": RIOT_API_KEY}
        
        mastery = await fetch_json(mastery_url, headers)
        if not mastery:
            return None
        
        return {
            "championId": mastery["championId"],
            "championName": champion_name,
            "championLevel": mastery["championLevel"],
            "championPoints": mastery["championPoints"],
            "lastPlayTime": mastery.get("lastPlayTime", 0)
        }

async def get_champion_mastery(region, riot_id, count=10):
    """Get top champion masteries for a player using Riot ID format (GameName#TAG)"""
    if "#" not in riot_id:
        print(f"Invalid Riot ID format: {riot_id}")
        return None
    
    game_name, tag_line = riot_id.split("#", 1)
    
    async with aiohttp.ClientSession() as session:
        # First, get the account to get the PUUID
        account_data = await get_account_by_riot_id(game_name, tag_line)
        if not account_data:
            print(f"Could not find account for {riot_id}")
            return None
        
        puuid = account_data["puuid"]
        
        # Get top champion masteries
        mastery_url = f"https://{region}.api.riotgames.com/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top?count={count}"
        headers = {"X-Riot-Token": RIOT_API_KEY}
        
        masteries = await fetch_json(mastery_url, headers)
        if not masteries:
            return None
        
        # Get champion data
        id_to_name, _ = await get_champion_data()
        
        # Format mastery data
        formatted_masteries = []
        for mastery in masteries:
            champion_name = id_to_name.get(mastery["championId"], f"Champion {mastery['championId']}")
            formatted_masteries.append({
                "championId": mastery["championId"],
                "championName": champion_name,
                "championLevel": mastery["championLevel"],
                "championPoints": mastery["championPoints"],
                "lastPlayTime": mastery.get("lastPlayTime", 0)
            })
        
        return formatted_masteries

async def get_last_played_games(region, riot_id):
    """Get the last game played for each game mode using Riot ID format (GameName#TAG)"""
    if "#" not in riot_id:
        print(f"Invalid Riot ID format: {riot_id}")
        return None
    
    game_name, tag_line = riot_id.split("#", 1)
    
    async with aiohttp.ClientSession() as session:
        # First, get the account to get the PUUID
        account_data = await get_account_by_riot_id(game_name, tag_line)
        if not account_data:
            print(f"Could not find account for {riot_id}")
            return None
        
        puuid = account_data["puuid"]
        
        # Get match history
        match_history_url = f"https://americas.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count=50"
        headers = {"X-Riot-Token": RIOT_API_KEY}
        
        match_ids = await fetch_json(match_history_url, headers)
        if not match_ids:
            return None

        last_games = {
            "RANKED_SOLO": None,
            "RANKED_FLEX": None,
            "NORMAL_DRAFT": None,
            "ARAM": None,
            "SWIFT_PLAY": None
        }

        for match_id in match_ids:
            match_data = await get_cached_match_data(session, match_id)
            if not match_data:
                continue
                
            queue_id = match_data["info"].get("queueId")
            
            # Find the player's data in the match
            for participant in match_data["info"]["participants"]:
                if participant["puuid"] == puuid:
                    game_info = {
                        "matchId": match_id,
                        "champion": participant["championName"],
                        "kills": participant["kills"],
                        "deaths": participant["deaths"],
                        "assists": participant["assists"],
                        "win": participant["win"],
                        "gameMode": match_data["info"]["gameMode"],
                        "gameDuration": match_data["info"]["gameDuration"],
                        "timestamp": match_data["info"]["gameStartTimestamp"]
                    }
                    
                    # Map queue IDs to our game modes
                    if queue_id == 420:  # Ranked Solo/Duo
                        if not last_games["RANKED_SOLO"]:
                            last_games["RANKED_SOLO"] = game_info
                    elif queue_id == 440:  # Ranked Flex
                        if not last_games["RANKED_FLEX"]:
                            last_games["RANKED_FLEX"] = game_info
                    elif queue_id == 400:  # Normal Draft
                        if not last_games["NORMAL_DRAFT"]:
                            last_games["NORMAL_DRAFT"] = game_info
                    elif queue_id == 450:  # ARAM
                        if not last_games["ARAM"]:
                            last_games["ARAM"] = game_info
                    elif queue_id == 1700:  # Swift Play
                        if not last_games["SWIFT_PLAY"]:
                            last_games["SWIFT_PLAY"] = game_info
                    
                    break
            
            # Check if we've found all game modes
            if all(last_games.values()):
                break

        return last_games

async def get_role_summary(region, riot_id, count=50):
    """Get role distribution data for a player using Riot ID format (GameName#TAG)"""
    if "#" not in riot_id:
        print(f"Invalid Riot ID format: {riot_id}")
        return None
    
    game_name, tag_line = riot_id.split("#", 1)
    
    async with aiohttp.ClientSession() as session:
        # First, get the account to get the PUUID
        account_data = await get_account_by_riot_id(game_name, tag_line)
        if not account_data:
            print(f"Could not find account for {riot_id}")
            return None
        
        puuid = account_data["puuid"]
        
        # Get match history
        match_history_url = f"https://americas.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={min(count * 2, 100)}"
        headers = {"X-Riot-Token": RIOT_API_KEY}
        
        match_ids = await fetch_json(match_history_url, headers)
        if not match_ids:
            return None

        role_data = {
            "TOP": 0,
            "JUNGLE": 0,
            "MIDDLE": 0,
            "BOTTOM": 0,
            "UTILITY": 0  # Support
        }
        games_analyzed = 0
        
        # Limit to 20 games max to avoid rate limits
        games_to_analyze = min(count, 20)
        
        for i, match_id in enumerate(match_ids[:games_to_analyze * 2]):
            match_data = await get_cached_match_data(session, match_id)
            if not match_data:
                continue
                
            # Skip non-Summoner's Rift games
            if match_data["info"]["queueId"] not in [420, 440, 400]:  # Solo/Duo, Flex, Normal Draft
                continue
            
            # Find the player's data
            for participant in match_data["info"]["participants"]:
                if participant["puuid"] == puuid:
                    # Get the player's position
                    position = participant.get("teamPosition", "")
                    
                    # Only count if position is valid
                    if position in role_data:
                        role_data[position] += 1
                        games_analyzed += 1
                    
                    break
            
            if games_analyzed >= games_to_analyze:
                break
        
        # Convert UTILITY to Support for display
        role_display = {
            "Top": role_data["TOP"],
            "Jungle": role_data["JUNGLE"],
            "Mid": role_data["MIDDLE"],
            "ADC": role_data["BOTTOM"],
            "Support": role_data["UTILITY"]
        }
        
        # Remove roles with 0 games
        role_display = {k: v for k, v in role_display.items() if v > 0}
        
        return {
            "role_data": role_display,
            "games_analyzed": games_analyzed
        }

class GuildThrottler:
    def __init__(self):
        self.guild_last_check = defaultdict(float)
        self.guild_player_counts = defaultdict(int)
        
    def get_delay(self, guild_id):
        """Calculate delay based on guild's player count"""
        player_count = self.guild_player_counts[guild_id]
        if player_count > 100:
            return 5.0  # 5 second delay for large guilds
        elif player_count > 50:
            return 3.0  # 3 second delay for medium guilds
        elif player_count > 20:
            return 1.0  # 1 second delay for small guilds
        return 0.5     # 0.5 second delay for tiny guilds
    
    async def wait_for_guild(self, guild_id, player_count):
        """Wait appropriate time based on guild size"""
        self.guild_player_counts[guild_id] = player_count
        now = time.time()
        last_check = self.guild_last_check[guild_id]
        delay = self.get_delay(guild_id)
        
        if now - last_check < delay:
            await asyncio.sleep(delay - (now - last_check))
        
        self.guild_last_check[guild_id] = time.time()

# Create global throttler instance
guild_throttler = GuildThrottler()

# Update the check_streaks function to use throttling
async def check_streaks_for_guild(guild_id, players):
    """Check streaks for a single guild with throttling"""
    await guild_throttler.wait_for_guild(guild_id, len(players))
    # ... rest of the streak checking logic ...

