import os
import aiohttp
import asyncio
from dotenv import load_dotenv
from functools import lru_cache

load_dotenv()
RIOT_API_KEY = os.getenv("RIOT_API_KEY")

match_cache = {}
rate_limit_lock = asyncio.Semaphore(20)  # allow 20 concurrent requests safely

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

async def fetch_json(session, url, headers):
    return await safe_request(session, url, headers)

async def get_account_by_riot_id(game_name, tag_line):
    async with aiohttp.ClientSession() as session:
        url = f"https://americas.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        headers = {"X-Riot-Token": RIOT_API_KEY}
        return await fetch_json(session, url, headers)

async def get_summoner_by_puuid(region, puuid):
    async with aiohttp.ClientSession() as session:
        url = f"https://{region}.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
        headers = {"X-Riot-Token": RIOT_API_KEY}
        return await fetch_json(session, url, headers)

_champion_data_cache = None

async def get_champion_data():
    global _champion_data_cache
    if _champion_data_cache is not None:
        return _champion_data_cache

    async with aiohttp.ClientSession() as session:
        version_url = "https://ddragon.leagueoflegends.com/api/versions.json"
        version_response = await fetch_json(session, version_url, {})
        version = version_response[0] if version_response else "14.24.1"

        champ_data_url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
        champ_data = await fetch_json(session, champ_data_url, {})

        id_to_name = {}
        name_to_id = {}
        if champ_data:
            for key, val in champ_data["data"].items():
                id_to_name[int(val["key"])] = val["name"]
                name_to_id[val["name"].lower()] = int(val["key"])

        _champion_data_cache = (id_to_name, name_to_id)
        return _champion_data_cache

async def get_cached_match_data(session, match_id):
    if match_id in match_cache:
        return match_cache[match_id]
    data = await get_match_data(session, match_id)
    if data:
        match_cache[match_id] = data
    return data

async def get_match_data(session, match_id):
    url = f"https://americas.api.riotgames.com/lol/match/v5/matches/{match_id}"
    headers = {"X-Riot-Token": RIOT_API_KEY}
    return await fetch_json(session, url, headers)

async def get_match_timeline(session, match_id):
    url = f"https://americas.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
    headers = {"X-Riot-Token": RIOT_API_KEY}
    return await fetch_json(session, url, headers)

async def get_summoner_rank(region, riot_id):
    """Get summoner rank using Riot ID format (GameName#TAG)"""
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
        
        # Get summoner data using PUUID
        summoner_data = await get_summoner_by_puuid(region, puuid)
        if not summoner_data:
            print(f"Could not find summoner data for {riot_id}")
            return None
        
        summoner_id = summoner_data["id"]
        
        # Get rank data
        rank_url = f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-summoner/{summoner_id}"
        headers = {"X-Riot-Token": RIOT_API_KEY}
        
        ranks = await fetch_json(session, rank_url, headers)
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
        
        # Get summoner data using PUUID
        summoner_data = await get_summoner_by_puuid(region, puuid)
        if not summoner_data:
            print(f"Could not find summoner data for {riot_id}")
            return None
        
        summoner_id = summoner_data["id"]
        
        # Get rank data
        rank_url = f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-summoner/{summoner_id}"
        headers = {"X-Riot-Token": RIOT_API_KEY}
        
        ranks = await fetch_json(session, rank_url, headers)
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

async def get_match_history(region, riot_id, count=10):
    """Get recent match history for a player using Riot ID format (GameName#TAG), only Ranked Solo/Duo games (queueId 420)"""
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
        match_history_url = f"https://americas.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count=30"
        headers = {"X-Riot-Token": RIOT_API_KEY}
        
        match_ids = await fetch_json(session, match_history_url, headers)
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
        match_history_url = f"https://americas.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count * 2}"
        headers = {"X-Riot-Token": RIOT_API_KEY}
        
        match_ids = await fetch_json(session, match_history_url, headers)
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
        
        mastery = await fetch_json(session, mastery_url, headers)
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
        
        masteries = await fetch_json(session, mastery_url, headers)
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
        
        match_ids = await fetch_json(session, match_history_url, headers)
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
        
        match_ids = await fetch_json(session, match_history_url, headers)
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

