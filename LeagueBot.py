import os
from dotenv import load_dotenv
import discord
from discord.ext import commands
from discord import app_commands
from db import init_db, add_tracked_player, get_tracked_players, remove_tracked_player, is_tiltcheck_enabled, toggle_tiltcheck, get_tiltcheck_cooldown, update_tiltcheck_cooldown, get_winstreak_cooldown, update_winstreak_cooldown, is_wincheck_enabled, toggle_wincheck, set_notification_channel, get_notification_channel, link_discord_riot, get_riot_id_for_discord, get_all_mapped_players, get_discord_id_for_riot, unlink_discord_riot, clear_tracked_players
from riot_api import (get_account_by_riot_id, get_summoner_rank, get_flex_rank, get_match_history, 
                     get_detailed_match_history, get_champion_mastery, get_specific_champion_mastery, 
                     get_last_played_games, get_role_summary, ensure_match_data_table, 
                     ensure_puuid_table, cleanup, prefetch_puuids, clear_corrupted_puuid_cache, clear_expired_puuid_cache, clear_expired_match_data_cache, clear_corrupted_match_data_cache, get_champion_data, get_arena_challenges)
import asyncio
from datetime import datetime
from discord.ui import View, Button
from PIL import Image, ImageDraw, ImageFont
import io
import aiohttp
import urllib.parse
from discord.ext import tasks
import aiosqlite
import matplotlib.pyplot as plt
import numpy as np
import json

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="/", intents=intents)

DEFAULT_REGION = "na1"

SPECIAL_EMOJI_NAMES = {
    "Kha'Zix": "Khazix",
    "Dr. Mundo": "DrMundo",
    "LeBlanc": "Leblanc",
    "Rek'Sai": "Reksai",
    "Kai'Sa": "Kaisa",
    "Cho'Gath": "Chogath",
    "Vel'Koz": "Velkoz",
    "Nunu & Willump": "Nunu",
    "Bel'Veth": "Belveth",
    "K'Sante": "KSante",
}

def get_champion_emoji(champion_name):
    """Get the champion emoji from bot's app emojis"""
    if not hasattr(bot, 'app_emojis'):
        return ""
    # Use special mapping if needed
    lookup_name = SPECIAL_EMOJI_NAMES.get(champion_name, champion_name)
    # Try the mapped name and a version with spaces/apostrophes removed
    candidates = [
        lookup_name,
        lookup_name.replace(" ", "").replace("'", "")
    ]
    for name in candidates:
        if name in bot.app_emojis:
            emoji_id = bot.app_emojis[name]
            return f"<:{name}:{emoji_id}>"
    return ""

async def create_hastebin(content):
    """Create a paste using a Hastebin-compatible mirror that allows anonymous posting."""
    try:
        print(f"Attempting to create Hastebin paste. Content length: {len(content)}")
        if len(content) < 500:
            print(f"Content snippet: {content[:200]}")

        headers = {
            "Content-Type": "text/plain"
        }

        # Use a known working mirror that does not require an API key
        mirror_url = "https://haste.zneix.eu"  # ← You can change this to any other compatible mirror

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{mirror_url}/documents",
                data=content,
                headers=headers,
                timeout=10
            ) as response:
                print(f"Response status: {response.status}")
                response_text = await response.text()
                print(f"Response body: {response_text}")

                if response.status == 200:
                    result = await response.json()
                    print(f"Success! Paste key: {result['key']}")
                    return f'{mirror_url}/{result["key"]}'
                else:
                    print(f"❌ Hastebin API error: Status {response.status}, Response: {response_text}")
                    return None
    except aiohttp.ClientError as e:
        print(f"❌ Network error creating hastebin paste: {e}")
        return None
    except Exception as e:
        print(f"❌ General error creating hastebin paste: {e}")
        return None

class RefreshView(View):
    def __init__(self, generate_embed_func):
        super().__init__(timeout=None)
        self.generate_embed_func = generate_embed_func

    @discord.ui.button(label='Refresh', style=discord.ButtonStyle.primary, emoji='🔄')
    async def refresh_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.defer()
        
        # Disable the button
        button.disabled = True
        await interaction.message.edit(view=self)
        
        try:
            updated_embed = await self.generate_embed_func()
            await interaction.message.edit(embed=updated_embed, view=self)
        except Exception as e:
            print(f"Error refreshing leaderboard: {e}")
            await interaction.followup.send("Error refreshing leaderboard. Please try again.", ephemeral=True)
        
        # Wait 5 seconds before re-enabling the button
        await asyncio.sleep(300)
        button.disabled = False
        await interaction.message.edit(view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(view=self)
        except:
            pass

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    await bot.process_commands(message) # Keep processing prefix commands for now

@bot.tree.command(name="help", description="Shows all available commands and how to use them.")
async def help(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    embed = discord.Embed(
        title="SoloQ Snitch Commands 🔎",
        description=(
            "Welcome to **SoloQ Snitch** — your League stats assistant.\n"
            "To begin, use `/add SummonerName#TAG` to start tracking\n\n"
        ),
        color=discord.Color(0x00FFFF)
    )

    # Thumbnail (bot's avatar)
    if bot.user and bot.user.avatar:
        embed.set_thumbnail(url=bot.user.avatar.url)
    else:
        embed.set_thumbnail(url=bot.user.default_avatar.url)

    embed.add_field(
        name="👥 Player Tracking",
        value=(
            "`/add` — Add a player to the tracking list\n"
            "`/remove` — Remove a player from the list\n"
            "`/leaderboard` — View your server's leaderboard\n"
            "`/strongest` — Display an image of the "'Strongest'"\n"
            "`/rank` — Check a player's current rank\n"
            "`/lastplayed` — Check when a player last played\n"
            "`/link` — Link a discord account to a Riot ID\n"
            "`/unlink` — Unlink a discord account from a Riot ID\n"
            "`/clear` — Clear and unlink all tracked players"
        ),
        inline=False
    )

    embed.add_field(
        name="📊 Performance & Analysis",
        value=(
            "`/stats` — Show a player's full performance breakdown\n"
            "`/history` — Show a player's match history\n"
            "`/feederscore` — Calculate feeder score\n"
            "`/rolesummary` — View a player's role distribution\n"
            "`/firstblood` — Check a player's first blood performance\n"
            "`/arenagod` — Shows the amount of arena games a player has won"
        ),
        inline=False
    )

    embed.add_field(
        name="🧠 Miscellaneous",
        value=(
            "`/mastery` — View champion mastery for a player\n"
            "`/lfg` — Notifies other players that you're looking for a game"
        ),
        inline=False
    )

    embed.add_field(
        name="⚙️ Bot Settings",
        value=(
            "`/tiltcheck` — Toggle alerts for losing streaks\n"
            "`/wincheck` — Toggle alerts for win streaks\n"
            "`/setchannel` — Set alert/notification channel"
        ),
        inline=False
    )

    embed.add_field(
        name="🛎️ Automatic Features",
        value=(
            "• 🔥 Win streak alerts (3+ games)\n"
            "• 😵‍💫 Tilt alerts (3+ game losses)\n"
            "• 🏆 Auto-check strongest player every 6 hours"
        ),
        inline=False
    )

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="add", description="Add a player to the tracking list.")
async def add(interaction: discord.Interaction, riot_id: str):
    await interaction.response.defer(ephemeral=True) # Defer ephemerally so only the user sees the initial response

    if "#" not in riot_id:
        await interaction.followup.send("Invalid Riot ID. Use the format SummonerName#TAG")
        return

    game_name, tag_line = riot_id.split("#", 1)
    account_data = await get_account_by_riot_id(game_name, tag_line)

    if not account_data:
        await interaction.followup.send(f"Could not find any Riot account for `{riot_id}`")
        return

    try:
        normalized_name = f"{account_data['gameName']}#{account_data['tagLine']}"
        print(f"Storing player: {normalized_name}")
        await add_tracked_player(str(interaction.guild.id), normalized_name, DEFAULT_REGION)
        await interaction.followup.send(f"Now tracking **{normalized_name}**.")
    except ValueError as e:
        await interaction.followup.send(str(e))
    except Exception as e:
        await interaction.followup.send(f"Failed to add summoner: {e}")

@bot.tree.command(name="remove", description="Remove a player from the tracking list.")
async def remove(interaction: discord.Interaction, riot_id: str):
    await interaction.response.defer()

    if not riot_id:
        await interaction.followup.send("You need to provide a summoner name to remove.")
        return

    try:
        await remove_tracked_player(str(interaction.guild.id), riot_id)
        await interaction.followup.send(f"Removed **{riot_id}** from tracking list.")
    except Exception as e:
        await interaction.followup.send(f"Failed to remove summoner: {e}")

@bot.tree.command(name="leaderboard", description="Displays the ranked leaderboard for tracked players.")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()

    async def generate_leaderboard_embed():
        players = await get_tracked_players(str(interaction.guild.id))
        
        if not players:
            return discord.Embed(
                title="No Players Tracked",
                description="Use `/add SummonerName#TAG` to start tracking players.",
                color=discord.Color(0x00FFFF)
            )

        # Get ranks for all players
        leaderboard = []
        for summoner_name, region in players:
            rank_data = await get_summoner_rank(region, summoner_name)
            if rank_data:
                leaderboard.append((
                    summoner_name,
                    rank_data["tier"],
                    rank_data["rank"],
                    rank_data["lp"]
                ))
            else:
                leaderboard.append((summoner_name, "UNRANKED", "", 0))

        # Sort by rank
        rank_order = {
            "IRON": 1, "BRONZE": 2, "SILVER": 3, "GOLD": 4,
            "PLATINUM": 5, "EMERALD": 6, "DIAMOND": 7,
            "MASTER": 8, "GRANDMASTER": 9, "CHALLENGER": 10
        }

        def get_rank_value(entry):
            tier, division, lp = entry[1], entry[2], entry[3]
            if tier == "UNRANKED":
                return (0, 0, 0)
            if tier in ["MASTER", "GRANDMASTER", "CHALLENGER"]:
                return (rank_order[tier], 0, lp)
            division_value = {"I": 1, "II": 2, "III": 3, "IV": 4}.get(division, 4)
            return (rank_order[tier], -division_value, lp)

        leaderboard.sort(key=get_rank_value, reverse=True)

        # Calculate column widths
        rank_num_width = 3
        name_width = max(len(name) for name, _, _, _ in leaderboard) + 2
        rank_width = 20

        total_width = rank_num_width + name_width + rank_width + 4

        # Create header
        header_text = "LEADERBOARD"
        separator = "-" * total_width

        # Build lines with proper alignment
        lines = [
            header_text.center(total_width),
            separator,
            f"{'#':<{rank_num_width}} {'SUMMONER NAME':<{name_width}} {'CURRENT RANK':<{rank_width}}",
            separator
        ]

        for i, (name, tier, division, lp) in enumerate(leaderboard, start=1):
            if tier == "UNRANKED":
                rank_display = "UNRANKED"
            elif division:
                rank_display = f"{tier} {division} - {lp}LP"
            else:
                rank_display = f"{tier} - {lp}LP"

            line = f"{str(i) + '.':<{rank_num_width}} {name:<{name_width}} {rank_display:<{rank_width}}"
            lines.append(line)

        description = "```" + "\n".join(lines) + "```"

        embed = discord.Embed(
            title="",
            description=description,
            color=discord.Color(0x00FFFF)
        )
        
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        embed.set_footer(text=f"Last updated: {timestamp}")
        
        return embed

    embed = await generate_leaderboard_embed()
    view = RefreshView(generate_leaderboard_embed)
    message = await interaction.followup.send(embed=embed, view=view)
    view.message = message

async def get_strongest_player(guild_id):
    """Helper function to get the strongest player for a guild"""
    players = await get_tracked_players(guild_id)
    
    if not players:
        return None

    rank_order = {
        "IRON": 1, "BRONZE": 2, "SILVER": 3, "GOLD": 4,
        "PLATINUM": 5, "EMERALD": 6, "DIAMOND": 7,
        "MASTER": 8, "GRANDMASTER": 9, "CHALLENGER": 10
    }

    strongest_player = None
    highest_rank = (0, 0, 0)
    
    # Create tasks for all players
    tasks = [
        get_summoner_rank(region, summoner_name)
        for summoner_name, region in players
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True) # Use return_exceptions to avoid crashing on a single failed API call

    # Process results with corresponding player info
    for (summoner_name, region), rank_data in zip(players, results):
        if rank_data and not isinstance(rank_data, Exception):
            tier = rank_data["tier"]
            division = rank_data["rank"]
            lp = rank_data["lp"]
            
            if tier in ["MASTER", "GRANDMASTER", "CHALLENGER"]:
                sort_key = (rank_order.get(tier, 0), 0, lp)
            else:
                division_value = {"I": 1, "II": 2, "III": 3, "IV": 4}.get(division, 4)
                sort_key = (rank_order.get(tier, 0), -division_value, lp)
            
            if sort_key > highest_rank:
                highest_rank = sort_key
                strongest_player = {
                    'name': summoner_name,
                    'tier': tier,
                    'division': division,
                    'lp': lp
                }

    if not strongest_player:
        return None

    async with aiosqlite.connect("riot_bot.db") as conn:
        # fetch existing row
        async with conn.execute(
            "SELECT summoner_name, tier, division, lp, days_as_strongest, last_update FROM strongest_players WHERE guild_id = ?",
            (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()

        current_time = datetime.utcnow()
        is_new_strongest = False
        
        if not row:
            days_as_strongest = 1
            should_update = True
            is_new_strongest = True
        elif row[0] != strongest_player['name']:
            days_as_strongest = 1
            should_update = True
            is_new_strongest = True
        else:
            # Check if a full day has passed since last update
            last_update = datetime.fromisoformat(row[5].replace('Z', '+00:00'))
            time_diff = current_time - last_update
            
            if time_diff.days >= 1:
                days_as_strongest = row[4] + 1
                should_update = True
            else:
                days_as_strongest = row[4]
                should_update = False

        if should_update:
            # Update the database
            await conn.execute('''
                INSERT OR REPLACE INTO strongest_players
                  (guild_id, summoner_name, tier, division, lp, last_update, days_as_strongest)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            ''', (
                guild_id,
                strongest_player['name'],
                strongest_player['tier'],
                strongest_player['division'],
                strongest_player['lp'],
                days_as_strongest
            ))
            await conn.commit()

        strongest_player['days_as_strongest'] = days_as_strongest
        strongest_player['is_new_strongest'] = is_new_strongest
        return strongest_player

async def announce_strongest_player(target, strongest_player, is_interaction=False):
    """Helper function to announce the strongest player to a channel"""
    try:
        base_image = Image.open('TheStrongest.png')
        img = base_image.copy()
        draw = ImageDraw.Draw(img)
        
        try:
            font = ImageFont.truetype("MinecraftRegular-Bmg3.otf", 48)
        except:
            try:
                font = ImageFont.truetype(r"C:\Users\Sewde\Desktop\DiscordBot\MinecraftRegular-Bmg3.otf", 48)
            except:
                font = ImageFont.load_default()
        
        text = strongest_player['name']
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        
        img_width, img_height = img.size
        x = (img_width - text_width) // 2 - 625 
        y = img_height - 925
        
        outline_range = 2
        for adj_x in range(-outline_range, outline_range + 1):
            for adj_y in range(-outline_range, outline_range + 1):
                if adj_x != 0 or adj_y != 0:
                    draw.text((x + adj_x, y + adj_y), text, font=font, fill='white')
        
        draw.text((x, y), text, font=font, fill='black')
        
        img_bytes = io.BytesIO()
        img.save(img_bytes, format='PNG')
        img_bytes.seek(0)
        
        file = discord.File(img_bytes, filename='TheStrongest.png')
        
        tier_capitalized = strongest_player['tier'].capitalize()
        if strongest_player['tier'] in ["MASTER", "GRANDMASTER", "CHALLENGER"]:
            rank_display = f"{tier_capitalized} - {strongest_player['lp']}LP"
        else:
            rank_display = f"{tier_capitalized} {strongest_player['division']} - {strongest_player['lp']}LP"
        
        # Calculate duration in a natural format
        days = strongest_player['days_as_strongest']
        if days >= 365:
            years = days // 365
            duration = f"{years} {'YEAR' if years == 1 else 'YEARS'}"
        elif days >= 30:
            months = days // 30
            duration = f"{months} {'MONTH' if months == 1 else 'MONTHS'}"
        else:
            duration = f"{days} {'DAY' if days == 1 else 'DAYS'}"
        
        content = f"🏆  **{strongest_player['name']},** currently **{rank_display},** has been The Strongest for **{duration}**  🏆"
        
        if is_interaction:
            await target.followup.send(content=content, file=file)
        else:
            await target.send(content=content, file=file)
        
    except Exception as e:
        print(f"Error posting strongest update: {e}")

@tasks.loop(hours=6)
async def check_strongest():
    for guild in bot.guilds:
        guild_id = str(guild.id)
        strongest_player = await get_strongest_player(guild_id)
        
        if not strongest_player:
            continue

        # Only announce if there's a change in the strongest player
        if not strongest_player.get('is_new_strongest', False):
            continue

        # Try to get the notification channel, fall back to general or first available channel
        channel = None
        channel_id = await get_notification_channel(guild_id)
        if channel_id:
            channel = bot.get_channel(int(channel_id))
        
        if not channel:
            # Try to find general channel
            channel = discord.utils.get(guild.text_channels, name="general")
            
        if not channel and guild.text_channels:
            # If no general channel, use the first available text channel
            channel = guild.text_channels[0]
            
        if not channel:
            continue

        await announce_strongest_player(channel, strongest_player)

@tasks.loop(hours=168)  # 7 days = 168 hours
async def clean_puuid_cache():
    """Clean PUUID cache every 7 days"""
    print("Running scheduled PUUID cache cleanup...")
    # Clear corrupted entries
    corrupted_count = await clear_corrupted_puuid_cache()
    # Clear expired entries
    expired_count = await clear_expired_puuid_cache()
    # Clear expired match data cache entries
    await clear_expired_match_data_cache()
    # Clear corrupted match data cache entries
    corrupted_match_data = await clear_corrupted_match_data_cache()
    if corrupted_match_data > 0:
        print(f"Cleared {corrupted_match_data} corrupted match_data entries during scheduled cleanup")
    print(f"Cache cleanup complete: {corrupted_count} corrupted, {expired_count} expired entries cleared")

@bot.tree.command(name="strongest", description="Finds the strongest tracked player based on rank.")
async def strongest(interaction: discord.Interaction):
    await interaction.response.defer()

    strongest_player = await get_strongest_player(str(interaction.guild.id))
    
    if not strongest_player:
        await interaction.followup.send("No summoners are currently being tracked for this server.")
        return

    await announce_strongest_player(interaction, strongest_player, is_interaction=True)

@bot.tree.command(name="history", description="Show recent match history for a player.")
async def history(interaction: discord.Interaction, riot_id: str, games: int = 10):
    await interaction.response.defer(thinking=True)
    
    if '#' not in riot_id:
        await interaction.followup.send("Please use format: GameName#TAG", ephemeral=True)
        return

    if games < 1:
        await interaction.followup.send("Please request at least 1 game.", ephemeral=True)
        return
    if games > 20:
        await interaction.followup.send("For future reference, a maxiumum of only 20 games can be displayed.", ephemeral=True)
        games = 20

    game_name, tag_line = riot_id.split("#", 1)
    cleaned_riot_id = f"{game_name.strip()}#{tag_line.strip()}"
    
    matches = await get_match_history(DEFAULT_REGION, cleaned_riot_id, games)

    if not matches:
        await interaction.followup.send("No match history found or rate limit reached.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"Latest {len(matches)} Games for {cleaned_riot_id}",
        color=discord.Color(0x00FFFF)
    )

    for i, match in enumerate(matches, 1):
        match_time = datetime.fromtimestamp(match["timestamp"] / 1000).strftime("%Y-%m-%d %H:%M")
        minutes = match["gameDuration"] // 60
        seconds = match["gameDuration"] % 60
        duration = f"{minutes}:{seconds:02d}"
        result_emoji = "✅" if match["win"] else "❌"
        kda = f"{match['kills']}/{match['deaths']}/{match['assists']}"
        kda_ratio = (match['kills'] + match['assists']) / max(1, match['deaths'])

        safe_game_name = urllib.parse.quote(game_name.strip())
        safe_tag_line = urllib.parse.quote(tag_line.strip().replace(" ", "-"))
        deeplol_link = f"https://www.deeplol.gg/summoner/na/{safe_game_name}-{safe_tag_line}/matches/{match['matchId']}"

        champion_emoji = get_champion_emoji(match['champion'])

        value = (
            f"**Game {i}**\u2002•\u2002{result_emoji}\u2002•\u2002Ranked Solo/Duo\n"
            f"{champion_emoji}\u2002-\u2002{match['champion']}\n"
            f"**KDA**: {kda} ({kda_ratio:.2f} KDA)\n"
            f"**Duration**: {duration}\n"
            f"**Date**: {match_time}\n"
            f"[View Match Link]({deeplol_link})\n"
        )

        embed.add_field(
            name="\u200b",
            value=value,
            inline=False
        )

    wins = sum(1 for match in matches if match['win'])
    total = len(matches)
    winrate = (wins / total) * 100 if total > 0 else 0

    embed.add_field(
        name="\u200b",
        value=f"**Winrate:** {wins}/{total} ({winrate:.2f}%)",
        inline=False
    )

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    embed.set_footer(text=f"Last updated: {timestamp}")

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="stats", description="Show detailed statistics for a player over a number of games.")
async def stats(interaction: discord.Interaction, riot_id: str, games: int = 20):
    await interaction.response.defer()
    
    if games < 1:
        await interaction.followup.send("Please request at least 1 game.", ephemeral=True)
        return
    if games > 100:
        await interaction.followup.send("Maximum of 100 games for stats calculation. Using 100 games.", ephemeral=True)
        games = 100

    if "#" not in riot_id:
        await interaction.followup.send("Please use format: GameName#TAG", ephemeral=True)
        return
    
    matches = await get_detailed_match_history(DEFAULT_REGION, riot_id, games)
    
    if not matches:
        await interaction.followup.send("Rate limit reached. Please try again in 2 minutes.")
        return
    
    total_games = len(matches)
    wins = sum(1 for match in matches if match['win'])
    losses = total_games - wins
    winrate = (wins / total_games) * 100
    
    total_kills = sum(match['kills'] for match in matches)
    total_deaths = sum(match['deaths'] for match in matches)
    total_assists = sum(match['assists'] for match in matches)
    average_kda = (total_kills + total_assists) / max(1, total_deaths)
    
    total_cs = sum(match['cs'] for match in matches)
    total_minutes = sum(match['gameDuration'] / 60 for match in matches)
    cs_per_min = total_cs / max(1, total_minutes)
    
    avg_kill_participation = sum(match['killParticipation'] for match in matches) / total_games
    avg_damage_share = sum(match['damageShare'] for match in matches) / total_games
    avg_gold_share = sum(match['goldShare'] for match in matches) / total_games
    avg_vision_score = sum(match['visionScore'] for match in matches) / total_games
    
    champion_stats = {}
    for match in matches:
        champ = match['champion']
        if champ not in champion_stats:
            champion_stats[champ] = {'games': 0, 'wins': 0, 'kills': 0, 'deaths': 0, 'assists': 0}
        
        champion_stats[champ]['games'] += 1
        if match['win']:
            champion_stats[champ]['wins'] += 1
        champion_stats[champ]['kills'] += match['kills']
        champion_stats[champ]['deaths'] += match['deaths']
        champion_stats[champ]['assists'] += match['assists']
    
    sorted_champions = sorted(champion_stats.items(), key=lambda x: x[1]['games'], reverse=True)[:5]
    
    embed = discord.Embed(
        title=f"Stats for {riot_id} - Last {total_games} Ranked Games",
        color=discord.Color(0x00FFFF)
    )
    
    embed.add_field(
        name="📊 **Overall Performance**",
        value=f"Winrate: {wins}W–{losses}L (**{winrate:.1f}%**)\n"
              f"Average KDA: **{average_kda:.2f}** ({total_kills}/{total_deaths}/{total_assists})\n"
              f"CS/min: **{cs_per_min:.1f}**",
        inline=False
    )
    
    embed.add_field(name="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", value="", inline=False)
    
    if sorted_champions:
        champ_text = ""
        for champ, stats in sorted_champions:
            champ_wr = (stats['wins'] / stats['games']) * 100
            champ_kda = (stats['kills'] + stats['assists']) / max(1, stats['deaths'])
            champion_emoji = get_champion_emoji(champ)
            champ_text += (
                f"{champion_emoji}**{champ}** — {stats['games']} games (**{champ_wr:.0f}% WR**, {champ_kda:.2f} KDA)\n"
                if champion_emoji else
                f"**{champ}** — {stats['games']} games (**{champ_wr:.0f}% WR**, {champ_kda:.2f} KDA)\n"
            )
        
        embed.add_field(
            name="**Most Played Champions**",
            value=champ_text.strip(),
            inline=False
        )
        
        embed.add_field(name="━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", value="", inline=False)
    
    embed.add_field(
        name="🤝 **Team Contribution**",
        value=f"Kill Participation: **{avg_kill_participation:.1f}%**\n"
              f"Damage Share: **{avg_damage_share:.1f}%**\n"
              f"Gold Share: **{avg_gold_share:.1f}%**\n"
              f"Vision Score: **{avg_vision_score:.1f}**",
        inline=False
    )
    
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    embed.set_footer(text=f"Stats from last {total_games} ranked games • {timestamp}")
    
    await interaction.followup.send(embed=embed)

async def champion_name_autocomplete(
    interaction: discord.Interaction,
    current: str
):
    # Get all champion names (cache this if possible)
    id_to_name, _ = await get_champion_data()
    all_names = sorted(id_to_name.values())
    # Filter by what the user has typed so far
    return [
        app_commands.Choice(name=champ, value=champ)
        for champ in all_names if current.lower() in champ.lower()
    ][:25]  # Discord only allows up to 25 choices

@bot.tree.command(name="mastery", description="Show champion mastery for a player.")
@app_commands.autocomplete(champion_name=champion_name_autocomplete)
async def mastery(interaction: discord.Interaction, riot_id: str, champion_name: str = None):
    await interaction.response.defer()
    
    if "#" not in riot_id:
        await interaction.followup.send("Please use format: GameName#TAG", ephemeral=True)
        return
    
    if champion_name:
        mastery = await get_specific_champion_mastery(DEFAULT_REGION, riot_id, champion_name)
        
        if not mastery:
            await interaction.followup.send(f"No mastery data found for {champion_name} or error occurred.")
            return
        
        embed = discord.Embed(
            title=f"Champion Mastery for {riot_id}",
            color=discord.Color(0x00FFFF)
        )
        
        points = f"{mastery['championPoints']:,}"
        champion_emoji = get_champion_emoji(mastery['championName'])
        
        formatted_line = (
            f"{champion_emoji} **{mastery['championName']}**: "
            f"Mastery {mastery['championLevel']} – {points} points"
            if champion_emoji else
            f"**{mastery['championName']}**: Mastery {mastery['championLevel']} – {points} points"
        )

        embed.description = formatted_line
        
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        embed.set_footer(text=f"Last updated: {timestamp}")
        
        await interaction.followup.send(embed=embed)
    else:
        masteries = await get_champion_mastery(DEFAULT_REGION, riot_id, 15)
        
        if not masteries:
            await interaction.followup.send("Rate limit reached. Please try again in 2 minutes.")
            return
        
        embed = discord.Embed(
            title=f"Top 15 Champion Masteries for {riot_id}",
            color=discord.Color(0x00FFFF)
        )
        
        def format_points(points):
            return f"{points:,}"
        
        total_mastery = sum(champ["championPoints"] for champ in masteries)
        
        description_lines = []
        for i, champ in enumerate(masteries, 1):
            champion_emoji = get_champion_emoji(champ['championName'])
            line = (
                f"{i}. {champion_emoji} **{champ['championName']}**: Mastery {champ['championLevel']} – {format_points(champ['championPoints'])} points"
                if champion_emoji else
                f"{i}. **{champ['championName']}**: Mastery {champ['championLevel']} – {format_points(champ['championPoints'])} points"
            )
            description_lines.append(line)
        embed.description = "\n".join(description_lines)
        
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        embed.set_footer(text=f"Last updated: {timestamp}")
        
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="tiltcheck", description="Toggle tiltcheck alerts.")
async def tiltcheck(interaction: discord.Interaction):
    await interaction.response.defer()
    enabled = await toggle_tiltcheck(str(interaction.guild.id))
    status_msg = "enabled ✅" if enabled else "disabled ❌"
    await interaction.followup.send(f"Tiltcheck alerts are now {status_msg}.")

@bot.tree.command(name="wincheck", description="Toggle win streak alerts.")
async def wincheck(interaction: discord.Interaction):
    await interaction.response.defer()
    enabled = await toggle_wincheck(str(interaction.guild.id))
    status_msg = "enabled ✅" if enabled else "disabled ❌"
    await interaction.followup.send(f"Win streak alerts are now {status_msg}.")

@tasks.loop(minutes=40)
async def check_streaks():
    for guild in bot.guilds:
        guild_id = str(guild.id)
        tilt_enabled = await is_tiltcheck_enabled(guild_id)
        win_enabled = await is_wincheck_enabled(guild_id)
        
        if not tilt_enabled and not win_enabled:
            continue
        
        players = await get_tracked_players(guild_id)
        for summoner_name, region in players:
            try:
                all_matches = await get_match_history(region, summoner_name, 20)
                
                # Filter out remakes (less than 4 minutes)
                matches = [m for m in all_matches if m.get("gameDuration", 0) >= 240]

                if not matches:
                    continue
                
                last_tilt_match_id, last_tilt_time, last_tilt_streak = await get_tiltcheck_cooldown(guild_id, summoner_name)
                last_win_match_id, last_win_time, last_win_streak = await get_winstreak_cooldown(guild_id, summoner_name)

                if tilt_enabled and (not last_tilt_match_id or matches[0]["matchId"] != last_tilt_match_id):
                    streak = 0
                    for match in matches:
                        if not match["win"]:
                            streak += 1
                        else:
                            break
                    
                    # Send alert if streak is 3+ and we haven't already reported this exact streak
                    if streak >= 3 and streak != last_tilt_streak:
                        channel = discord.utils.get(guild.text_channels, name="general") or guild.text_channels[0]
                        
                        if streak == 3:
                            message = f"😟 **{summoner_name}** is on a **3-game losing streak**. Might want to take a break."
                        elif streak == 4:
                            message = f"😨 **{summoner_name}** is on a **4-game losing streak**. Seriously, take a break!"
                        elif streak == 5:
                            message = f"😱 **{summoner_name}** is on a **5-game losing streak**. Please stop playing for today!"
                        elif streak == 7:
                            message = f"🥶 **{summoner_name}** is on a **7-game losing streak**. I am begging you! Please stop playing!"
                        else:
                            message = f"💀 **{summoner_name}** is on a **{streak}-game losing streak**. Somebody call Riot!"
                        
                        try:
                            await channel.send(message)
                        except Exception as e:
                            print(f"Failed to send tilt alert: {e}")
                        
                        await update_tiltcheck_cooldown(guild_id, summoner_name, matches[0]["matchId"], streak)
                
                if win_enabled and (not last_win_match_id or matches[0]["matchId"] != last_win_match_id):
                    streak = 0
                    for match in matches:
                        if match["win"]:
                            streak += 1
                        else:
                            break
                    
                    # Send alert if streak is 3+ and we haven't already reported this exact streak
                    if streak >= 3 and streak != last_win_streak:
                        channel = discord.utils.get(guild.text_channels, name="general") or guild.text_channels[0]
                        
                        if streak == 3:
                            message = f"🔥 **{summoner_name}** is on a **3-game winning streak**! Keep it up!"
                        elif streak == 4:
                            message = f"🔥 **{summoner_name}** is on a **4-game winning streak**! You're on fire!"
                        else:
                            message = f"🔥 **{summoner_name}** is on a **{streak}-game winning streak**! Absolutely dominating!"
                        
                        try:
                            await channel.send(message)
                        except Exception as e:
                            print(f"Failed to send win alert: {e}")
                        
                        await update_winstreak_cooldown(guild_id, summoner_name, matches[0]["matchId"], streak)
                
                await asyncio.sleep(1)
                
            except Exception as e:
                print(f"Error checking streaks for {summoner_name}: {e}")
                if "429" in str(e):
                    await asyncio.sleep(10)
                continue

@bot.tree.command(name="lastplayed", description="Show last played games for different modes.")
async def lastplayed(interaction: discord.Interaction, riot_id: str):
    await interaction.response.defer()
    
    if "#" not in riot_id:
        await interaction.followup.send("Please use format: SummonerName#TAG", ephemeral=True)
        return
    
    last_games = await get_last_played_games(DEFAULT_REGION, riot_id)
    if not last_games:
        await interaction.followup.send("Rate limit reached. Please try again in 2 minutes.")
        return
    
    embed = discord.Embed(
        title=f"Last Played Games for {riot_id}",
        color=discord.Color(0x00FFFF)
    )
    
    def format_game_info(game, mode_name):
        if not game:
            return f"No recent {mode_name} games found"
        
        game_time = datetime.fromtimestamp(game["timestamp"] / 1000).strftime("%Y-%m-%d %H:%M")
        
        minutes = game["gameDuration"] // 60
        seconds = game["gameDuration"] % 60
        duration = f"{minutes}:{seconds:02d}"
        
        result_emoji = "✅" if game["win"] else "❌"
        
        kda = f"{game['kills']}/{game['deaths']}/{game['assists']}"
        
        return (
            f"**{result_emoji} {game['champion']}**\n"
            f"KDA: {kda}\n"
            f"Duration: {duration}\n"
            f"Time: {game_time}"
        )
    
    embed.add_field(
        name="Ranked Solo/Duo",
        value=format_game_info(last_games["RANKED_SOLO"], "Ranked Solo/Duo"),
        inline=True
    )
    
    embed.add_field(
        name="Ranked Flex",
        value=format_game_info(last_games["RANKED_FLEX"], "Ranked Flex"),
        inline=True
    )
    
    embed.add_field(
        name="Normal Draft",
        value=format_game_info(last_games["NORMAL_DRAFT"], "Normal Draft"),
        inline=True
    )
    
    embed.add_field(
        name="ARAM",
        value=format_game_info(last_games["ARAM"], "ARAM"),
        inline=True
    )
    
    embed.add_field(
        name="Swift Play",
        value=format_game_info(last_games["SWIFT_PLAY"], "Swift Play"),
        inline=True
    )
    
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    embed.set_footer(text=f"Last updated: {timestamp}")
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="rank", description="Show a player's current rank.")
async def rank(interaction: discord.Interaction, riot_id: str):
    await interaction.response.defer()
    
    if "#" not in riot_id:
        await interaction.followup.send("Please use format: GameName#TAG", ephemeral=True)
        return
    
    rank_data = await get_summoner_rank(DEFAULT_REGION, riot_id)
    
    if not rank_data:
        await interaction.followup.send(f"{riot_id} is currently unranked in Solo/Duo queue.")
        return
    
    tier = rank_data["tier"]
    division = rank_data["rank"]
    lp = rank_data["lp"]
    
    if tier in ["MASTER", "GRANDMASTER", "CHALLENGER"]:
        rank_display = f"{tier} {lp}LP"
    else:
        rank_display = f"{tier} {division} {lp}LP"
    
    await interaction.followup.send(f"{riot_id} is currently **{rank_display}**")

@bot.tree.command(name="setchannel", description="Set the channel for bot notifications.")
async def setchannel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    channel_id = str(interaction.channel.id)
    await set_notification_channel(str(interaction.guild.id), channel_id)
    await interaction.followup.send(f"Notifications will now be sent to {interaction.channel.mention}")

@bot.tree.command(name="firstblood", description="Show first blood statistics for a player.")
async def firstblood(interaction: discord.Interaction, riot_id: str, games: int = 20):
    await interaction.response.defer()
    
    if "#" not in riot_id:
        await interaction.followup.send("Please use format: GameName#TAG", ephemeral=True)
        return

    tag_line_index = riot_id.rfind('#')
    game_name = riot_id[:tag_line_index].strip()
    tag_line = riot_id[tag_line_index + 1:].strip()

    matches = await get_detailed_match_history(DEFAULT_REGION, riot_id, games)

    if not matches:
        await interaction.followup.send("No recent games found or error occurred.")
        return

    first_blood_kills = 0
    first_blood_assists = 0
    first_blood_victims = 0
    fb_games = []

    for match in matches:
        fb_kill = match.get("firstBloodKill", False)
        fb_assist = match.get("firstBloodAssist", False)
        fb_victim = match.get("firstBloodVictim", False)

        if fb_kill or fb_assist or fb_victim:
            safe_game_name = urllib.parse.quote(game_name)
            safe_tag_line = urllib.parse.quote(tag_line.replace(' ', '-'))
            deeplol_link = f"https://www.deeplol.gg/summoner/na/{safe_game_name}-{safe_tag_line}/matches/{match['matchId']}"

            fb_games.append({
                "matchId": match["matchId"],
                "champion": match["champion"],
                "kill": fb_kill,
                "assist": fb_assist,
                "victim": fb_victim,
                "link": deeplol_link,
                "timestamp": match["timestamp"]
            })

        if fb_kill:
            first_blood_kills += 1
        if fb_assist:
            first_blood_assists += 1
        if fb_victim:
            first_blood_victims += 1

    embed = discord.Embed(
        title=f"First Blood Stats for {riot_id} in {len(matches)} games",
        color=discord.Color(0x00FFFF)
    )

    embed.add_field(name="First Blood Kills", value=f"🔪 {first_blood_kills}", inline=True)
    embed.add_field(name="First Blood Assists", value=f"🤝 {first_blood_assists}", inline=True)
    embed.add_field(name="First Blood Deaths", value=f"💀 {first_blood_victims}", inline=True)

    total_games = len(matches)
    kill_percent = (first_blood_kills / total_games) * 100 if total_games > 0 else 0
    assist_percent = (first_blood_assists / total_games) * 100 if total_games > 0 else 0
    death_percent = (first_blood_victims / total_games) * 100 if total_games > 0 else 0

    embed.add_field(
        name="Participation Rate",
        value=f"Kills: {kill_percent:.1f}%\nAssists: {assist_percent:.1f}%\nDeaths: {death_percent:.1f}%",
        inline=False
    )

    if fb_games:
        fb_games.sort(key=lambda x: x["timestamp"], reverse=True)
        game_list = ""
        char_budget = 950  # Safe limit to avoid hitting 1024-char cap

        for i, game in enumerate(fb_games):
            emoji = "🔪" if game["kill"] else "🤝" if game["assist"] else "💀"
            game_time = datetime.fromtimestamp(game["timestamp"] / 1000).strftime("%Y-%m-%d %H:%M")
            line = f"{emoji} {game['champion']} - {game_time}\n[View Match]({game['link']})\n\n"

            if len(game_list) + len(line) > char_budget:
                game_list += f"... and {len(fb_games) - i} more first blood games."
                break

            game_list += line

        embed.add_field(
            name="First Blood Games",
            value=game_list,
            inline=False
        )

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    embed.set_footer(text=f"Last updated: {timestamp}")

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="rolesummary", description="Show a player's role distribution.")
async def rolesummary(interaction: discord.Interaction, riot_id: str, games: int = 20):
    await interaction.response.defer()
    
    if "#" not in riot_id:
        await interaction.followup.send("Please use format: GameName#TAG", ephemeral=True)
        return
    
    role_data = await get_role_summary(DEFAULT_REGION, riot_id, games)
    if not role_data:
        await interaction.followup.send("Rate limit reached. Please try again in 2 minutes.")
        return
    
    plt.figure(figsize=(10, 6))
    roles = list(role_data["role_data"].keys())
    games_count = list(role_data["role_data"].values())
    
    plt.pie(games_count, labels=roles, autopct='%1.1f%%', startangle=90)
    plt.axis('equal')
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close()
    
    embed = discord.Embed(
        title=f"Role Distribution for {riot_id}",
        description=f"Based on {role_data['games_analyzed']} recent games",
        color=discord.Color(0x00FFFF)
    )
    
    for role, games_played in role_data["role_data"].items():
        percentage = (games_played / role_data["games_analyzed"]) * 100
        embed.add_field(
            name=role,
            value=f"{games_played} games ({percentage:.1f}%)",
            inline=True
        )
    
    file = discord.File(buf, filename='role_distribution.png')
    embed.set_image(url="attachment://role_distribution.png")
    
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    embed.set_footer(text=f"Last updated: {timestamp}")
    
    await interaction.followup.send(embed=embed, file=file)

@bot.tree.command(name="feederscore", description="Calculate a player's feeder score.")
async def feederscore(interaction: discord.Interaction, riot_id: str, games: int = 20):
    await interaction.response.defer()
    
    if "#" not in riot_id:
        await interaction.followup.send("Please use format: GameName#TAG", ephemeral=True)
        return

    tag_line_index = riot_id.rfind('#')
    game_name = riot_id[:tag_line_index].strip()
    tag_line = riot_id[tag_line_index + 1:].strip()
    cleaned_riot_id = f"{game_name}#{tag_line}"

    def calculate_feeder_score(stats, game_number):
        deaths       = stats["deaths"]
        kills        = stats["kills"]
        assists      = stats["assists"]
        vision_score = stats["visionScore"]
        duration     = stats["timePlayed"]
        dmg_share    = stats["damageShare"]
        cs           = stats.get("cs", 0)
        team_kills   = stats.get("teamKills", 1)
        death_times  = stats.get("deathTimes", [])
        obj_times    = stats.get("objectiveTimestamps", [])
        tower_damage = stats.get("damageDealtToBuildings", 0)

        mins = max(duration / 60, 1)
        death_min = deaths / mins
        kda = (kills + assists) / max(deaths, 1)
        vision_per_min = vision_score / mins
        cs_per_min = cs / mins
        kill_participation = (kills + assists) / max(team_kills, 1)
        tower_dmg_min = tower_damage / mins

        EARLY_WINDOW = 12
        EARLY_PENALTY = 0.25
        OBJ_WINDOW = 15 * 1000

        death_timing_penalty = 0
        for death_time in death_times:
            dt_min = death_time / 60.0
            if dt_min <= EARLY_WINDOW:
                death_timing_penalty += EARLY_PENALTY
            elif dt_min > 40:
                death_timing_penalty += 0.4 * (1 + (dt_min - 40) / 20)

        obj_death_penalty = 0
        death_times_ms = [t * 1000 for t in death_times]
        for death_time in death_times_ms:
            for obj_time in obj_times:
                if abs(death_time - obj_time) < OBJ_WINDOW:
                    obj_death_penalty += 0.4
                    break

        norm_deaths      = min(death_min / 0.2, 2.0)
        norm_kda         = min(kda / 2.0, 2.0)
        norm_vision      = min(vision_per_min / 1.0, 1.5)
        norm_dmg_share   = min(dmg_share / 0.20, 2.0)
        norm_cs          = min(cs_per_min / 6.0, 1.5)
        norm_kp          = min(kill_participation / 0.5, 2.0)
        norm_tower       = min(tower_dmg_min / 200.00, 1.5)

        raw_ultra        = max(0, (deaths - 10) * 0.5)
        ultra_penalty    = min(raw_ultra, 3.0)    
        low_kp_penalty   = 2.0 if kill_participation < 0.25 else 0
        
        fb_penalty = 0.5 if stats.get("firstBloodVictim", False) else 0.0
        fb_perf = -0.25 if stats.get("firstBloodKill", False) else (-0.15 if stats.get("firstBloodAssist", False) else 0.0)
        
        death_score = norm_deaths * 3.0 + ultra_penalty + death_timing_penalty + fb_penalty + obj_death_penalty
        perf_score  = (
            norm_kda       * 1.25 +
            norm_vision    * 1.25 +
            norm_dmg_share * 1.30 +
            norm_cs        * 1.20 +
            norm_kp        * 1.25 +
            norm_tower     * 1.15 +
            fb_perf
        )

        raw = death_score - perf_score + low_kp_penalty

        clamped = max(raw + 7.0, 0)
        final_score = min(clamped, 10.0)

        debug_info = {
            "game_number": game_number,
            "raw_stats": {
                "deaths": deaths,
                "kills": kills,
                "assists": assists,
                "vision_score": vision_score,
                "duration_minutes": mins,
                "damage_share": dmg_share,
                "cs": cs,
                "team_kills": team_kills,
                "death_times": death_times,
                "objective_times": obj_times,
                "tower_damage": tower_damage
            },
            "derived_stats": {
                "deaths_per_min": death_min,
                "kda": kda,
                "vision_per_min": vision_per_min,
                "cs_per_min": cs_per_min,
                "kill_participation": kill_participation,
                "tower_damage_per_min": tower_dmg_min
            },
            "normalized_values": {
                "deaths": norm_deaths,
                "kda": norm_kda,
                "vision": norm_vision,
                "damage_share": norm_dmg_share,
                "cs": norm_cs,
                "kill_participation": norm_kp,
                "tower_damage": norm_tower
            },
            "penalties": {
                "death_timing": death_timing_penalty,
                "objective_death": obj_death_penalty,
                "ultra": ultra_penalty,
                "low_kp": low_kp_penalty,
                "first_blood": fb_penalty
            },
            "rewards": {
                "first_blood": fb_perf
            },
            "score_components": {
                "death_score": death_score,
                "performance_score": perf_score,
                "raw_score": raw,
                "clamped_score": clamped,
                "final_score": final_score
            }
        }

        return final_score, debug_info

    scores = []
    debug_info_list = []
    try:
        matches = await get_detailed_match_history(DEFAULT_REGION, cleaned_riot_id, games)
        if not matches:
            await interaction.followup.send(f"Rate limit reached. Please try again in 2 minutes.")
            return

        for i, m in enumerate(matches, 1):
            if m["gameDuration"] < 300: continue
            
            stats = {
                "deaths": m["deaths"],
                "kills": m["kills"],
                "assists": m["assists"],
                "visionScore": m["visionScore"],
                "timePlayed": m["gameDuration"],
                "damageShare": m["damageShare"] / 100.0,
                "goldDiff": m.get("goldDiff", 0),
                "xpDiff": m.get("xpDiff", 0),
                "cs": m.get("totalMinionsKilled", 0) + m.get("neutralMinionsKilled", 0),
                "teamKills": m.get("teamKills", 1),
                "deathTimes": m.get("deathTimes", []),
                "firstBloodKill": m.get("firstBloodKill", False),
                "firstBloodAssist": m.get("firstBloodAssist", False),
                "firstBloodVictim": m.get("firstBloodVictim", False),
                "objectiveTimestamps": m.get("objectiveTimestamps", []),
                "damageDealtToBuildings": m.get("damageDealtToBuildings", 0)
            }
            score, debug_info = calculate_feeder_score(stats, i)
            scores.append(score)
            debug_info_list.append(debug_info)

    except Exception as e:
        print(f"Error calculating feeder score for {cleaned_riot_id}: {e}")
        await interaction.followup.send(f"Rate limit reached. Please try again in 2 minutes.")
        return

    if not scores:
        await interaction.followup.send(f"No suitable recent games found for {cleaned_riot_id} to calculate feeder score.")
        return

    avg_score = sum(scores) / len(scores)

    debug_text = f"Feeder Score Debug Information for {cleaned_riot_id}\n"
    debug_text += f"Analyzed {len(scores)} games\n"
    debug_text += f"Average Score: {avg_score:.2f}\n\n"
    
    for game_debug in debug_info_list:
        debug_text += f"Game {game_debug['game_number']}:\n"
        debug_text += f"Final Score: {game_debug['score_components']['final_score']:.2f}\n"
        debug_text += f"Raw Stats: {json.dumps(game_debug['raw_stats'], indent=2)}\n"
        debug_text += f"Derived Stats: {json.dumps(game_debug['derived_stats'], indent=2)}\n"
        debug_text += f"Normalized Values: {json.dumps(game_debug['normalized_values'], indent=2)}\n"
        debug_text += f"Penalties: {json.dumps(game_debug['penalties'], indent=2)}\n"
        debug_text += f"Rewards: {json.dumps(game_debug['rewards'], indent=2)}\n"
        debug_text += f"Score Components: {json.dumps(game_debug['score_components'], indent=2)}\n"
        debug_text += "-" * 80 + "\n\n"

    hastebin_url = await create_hastebin(debug_text)

    embed = discord.Embed(
        title=f"Feeder Score for {cleaned_riot_id}",
        description="Higher Score = Bad",
        color=discord.Color(0x00FFFF)
    )

    if avg_score >= 7:
        emoji = "💀"
    elif avg_score >= 5:
        emoji = "🗑️"
    elif avg_score >= 2:
        emoji = "⚠️"
    else:
        emoji = "✅"

    embed.add_field(
        name=f"Average Feeder Score: {emoji}",
        value=f"**{avg_score:.2f}**/10 (from {len(scores)} games)",
        inline=False
    )

    embed.add_field(
        name="Legend",
        value="💀 7+  | 🗑️ 5 - 6.9 | ⚠️ 3 - 4.9  | ✅ 0 - 2.9",
        inline=False
    )

    if hastebin_url:
        embed.add_field(
            name="Detailed Analysis",
            value=f"[View Detailed Score Breakdown]({hastebin_url})",
            inline=False
        )

    embed.set_footer(text=f"Calculation based on last {len(scores)} ranked games • {datetime.utcnow():%Y-%m-%d %H:%M UTC}")

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="link", description="Link a Discord account to a Riot ID for duo notifications.")
async def link(interaction: discord.Interaction, riot_id: str, discord_user: discord.Member = None):
    await interaction.response.defer()
    
    if "#" not in riot_id:
        await interaction.followup.send("Invalid Riot ID. Use the format SummonerName#TAG", ephemeral=True)
        return

    # If no discord_user specified, use the command caller
    target_user = discord_user or interaction.user

    try:
        # Check if the Riot ID is already linked to any Discord account
        existing_discord_id = await get_discord_id_for_riot(str(interaction.guild.id), riot_id)
        if existing_discord_id:
            await interaction.followup.send(f"Riot ID **{riot_id}** is already linked to <@{existing_discord_id}>")
            return

        # Check if the Discord user already has a Riot ID linked
        existing_riot_id = await get_riot_id_for_discord(str(interaction.guild.id), str(target_user.id))
        if existing_riot_id:
            await interaction.followup.send(f"<@{target_user.id}> is already linked to **{existing_riot_id}**")
            return

        await link_discord_riot(str(interaction.guild.id), str(target_user.id), riot_id)
        
        if target_user.id == interaction.user.id:
            await interaction.followup.send(f"Successfully linked the Discord account to **{riot_id}**!")
        else:
            await interaction.followup.send(f"Successfully linked {target_user.mention}'s Discord account to **{riot_id}**!")
    except ValueError as e:
        await interaction.followup.send(str(e))
    except Exception as e:
        await interaction.followup.send(f"Failed to link account: {e}")

@bot.tree.command(name="unlink", description="Unlink a Discord account from its Riot ID.")
async def unlink(interaction: discord.Interaction, discord_user: discord.Member = None):
    await interaction.response.defer()

    # If no discord_user specified, use the command caller
    target_user = discord_user or interaction.user

    try:
        # Check if the user has a Riot ID linked
        riot_id = await get_riot_id_for_discord(str(interaction.guild.id), str(target_user.id))
        if not riot_id:
            await interaction.followup.send(f"<@{target_user.id}> doesn't have a Riot ID linked.")
            return

        # Unlink the account
        await unlink_discord_riot(str(interaction.guild.id), str(target_user.id))
        
        if target_user.id == interaction.user.id:
            await interaction.followup.send(f"Successfully unlinked the Discord account from **{riot_id}**!")
        else:
            await interaction.followup.send(f"Successfully unlinked {target_user.mention}'s Discord account from **{riot_id}**!")
    except Exception as e:
        await interaction.followup.send(f"Failed to unlink account: {e}")

@bot.tree.command(name="lfg", description="Notifies other players that you're looking for a game.")
@app_commands.choices(queue_type=[
    app_commands.Choice(name="Ranked Solo/Duo", value="ranked"),
    app_commands.Choice(name="Ranked Flex", value="flex"),
    app_commands.Choice(name="Unranked", value="unranked")
])
async def lfg(interaction: discord.Interaction, queue_type: app_commands.Choice[str]):
    await interaction.response.defer()
    
    # Get the caller's Riot ID from the mapping
    caller_riot_id = await get_riot_id_for_discord(str(interaction.guild.id), str(interaction.user.id))
    if not caller_riot_id:
        await interaction.followup.send("You need to link your Discord account to a Riot ID first. Use `/link` to set this up.", ephemeral=True)
        return

    # Get all other mapped users from the leaderboard
    mapped_users = await get_all_mapped_players(str(interaction.guild.id))
    mapped_users = [(discord_id, riot_id) for discord_id, riot_id in mapped_users if discord_id != str(interaction.user.id)]

    if not mapped_users:
        await interaction.followup.send("No other mapped players found to notify.", ephemeral=True)
        return

    # Get caller's rank based on queue type
    caller_rank = None
    if queue_type.value != "unranked":
        if queue_type.value == "ranked":
            caller_rank = await get_summoner_rank(DEFAULT_REGION, caller_riot_id)
        else:  # flex
            caller_rank = await get_flex_rank(DEFAULT_REGION, caller_riot_id)
            
        if not caller_rank:
            await interaction.followup.send(f"You need to be ranked in {queue_type.name} to use this feature.", ephemeral=True)
            return

    # Filter users based on queue type and rank restrictions
    eligible_users = []
    for discord_id, riot_id in mapped_users:
        if queue_type.value == "unranked":
            eligible_users.append((discord_id, riot_id))
            continue

        # Get user's rank based on queue type
        if queue_type.value == "ranked":
            user_rank = await get_summoner_rank(DEFAULT_REGION, riot_id)
        else:  # flex
            user_rank = await get_flex_rank(DEFAULT_REGION, riot_id)
            
        if not user_rank:
            continue

        # Define rank tiers and their values (no Emerald)
        rank_tiers = {
            "IRON": 1, "BRONZE": 2, "SILVER": 3, "GOLD": 4,
            "PLATINUM": 5, "DIAMOND": 6, "MASTER": 7,
            "GRANDMASTER": 8, "CHALLENGER": 9
        }
        div_map = {"I": 1, "II": 2, "III": 3, "IV": 4}
        
        caller_tier = rank_tiers.get(caller_rank["tier"], 0)
        user_tier = rank_tiers.get(user_rank["tier"], 0)
        can_duo = False

        if queue_type.value == "ranked":
            # Grandmaster/Challenger: no duo allowed
            if caller_tier >= 8 or user_tier >= 8:
                can_duo = False
            # Master: only with Diamond I or other Master
            elif caller_tier == 7 or user_tier == 7:
                if caller_tier == 7 and user_tier == 6 and user_rank["rank"] == "I":
                    can_duo = True
                elif user_tier == 7 and caller_tier == 6 and caller_rank["rank"] == "I":
                    can_duo = True
                elif caller_tier == 7 and user_tier == 7:
                    can_duo = True
            # Diamond: both must be Diamond, within two divisions
            elif caller_tier == 6 or user_tier == 6:
                if caller_tier == user_tier == 6:
                    caller_div = div_map.get(caller_rank["rank"], 4)
                    user_div = div_map.get(user_rank["rank"], 4)
                    can_duo = abs(caller_div - user_div) <= 2
            # Iron: can duo up to two tiers above
            elif caller_tier == 1 or user_tier == 1:
                can_duo = abs(caller_tier - user_tier) <= 2
            # Bronze–Platinum: within one tier
            elif caller_tier < 6 and user_tier < 6:
                can_duo = abs(caller_tier - user_tier) <= 1

        elif queue_type.value == "flex":
            # Master+ must both be at least Platinum (tier 5)
            if caller_tier >= 7 or user_tier >= 7:
                can_duo = caller_tier >= 5 and user_tier >= 5
            else:
                can_duo = True

        if can_duo:
            eligible_users.append((discord_id, riot_id))

    if not eligible_users:
        await interaction.followup.send("Sorry! You're either too garbage or godlike to duo with someone in this server.")
        return

    # Create the notification message
    queue_display = {
        "ranked": "Ranked Solo/Duo",
        "flex": "Ranked Flex",
        "unranked": "an unranked gamemode"
    }[queue_type.value]

    notification = f"**{interaction.user.mention}** ({caller_riot_id}) is looking for someone to play with in {queue_display}!\n\n Notice to all eligible summoners:"
    for discord_id, riot_id in eligible_users:
        notification += f"\n• <@{discord_id}> ({riot_id})"

    try:
        # Send the image and notification
        file = discord.File('DuoCheck.png', filename='DuoCheck.png')
        await interaction.followup.send(content=notification, file=file)
    except FileNotFoundError:
        await interaction.followup.send("⚠️ DuoCheck.png image not found. Please add it to the bot directory.")
    except Exception as e:
        print(f"Error sending duo check: {e}")
        await interaction.followup.send("Error sending duo check notification. Please try again.", ephemeral=True)

@bot.tree.command(name="arenagod", description="Show Arena 'Adapt to all Situations' challenge stats for a player.")
async def arenagod(interaction: discord.Interaction, riot_id: str):
    await interaction.response.defer()
    
    if "#" not in riot_id:
        await interaction.followup.send("Please use format: GameName#TAG", ephemeral=True)
        return
    
    arena_data = await get_arena_challenges(DEFAULT_REGION, riot_id)
    
    if not arena_data:
        await interaction.followup.send(f"No Arena challenge data found for {riot_id} or they haven't played Arena mode.")
        return
    
    embed = discord.Embed(
        title=f"Arena God Stats for {riot_id}",
        description="🔄 **Adapt to All Situations** Challenge",
        color=discord.Color(0x00FFFF)
    )
    
    embed.add_field(
        name="Unique 1st Place Champions",
        value=f"**{arena_data['uniqueChampionWins']}** different champions",
        inline=True
    )
    
    embed.add_field(
        name="Challenge Level",
        value=f"**{arena_data['adaptLevel']}**",
        inline=True
    )
    
    embed.add_field(
        name="Percentile",
        value=f"Top **{arena_data['adaptPercentile']:.1f}%**",
        inline=True
    )
    
    # Show champion names if available
    if arena_data.get('championNames'):
        champ_list = ", ".join(arena_data['championNames'][:10])  # Limit to first 10
        if len(arena_data['championNames']) > 10:
            champ_list += f" and {len(arena_data['championNames']) - 10} more"
        
        embed.add_field(
            name="Champions Won With",
            value=champ_list,
            inline=False
        )
    
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    embed.set_footer(text=f"Last updated: {timestamp}")
    
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="clear", description="Clears all tracked players from the leaderboard.")
@app_commands.choices(confirm=[
    app_commands.Choice(name="Yes", value="Y"),
    app_commands.Choice(name="No", value="N")
])
async def clear(interaction: discord.Interaction, confirm: app_commands.Choice[str]):
    await interaction.response.defer()
    
    if confirm.value == "N":
        await interaction.followup.send("Leaderboard clear cancelled.")
        return
    
    try:
        # Get all mapped players before clearing
        mapped_players = await get_all_mapped_players(str(interaction.guild.id))
        
        # Clear tracked players
        await clear_tracked_players(str(interaction.guild.id))
        
        # Unlink all Discord-Riot ID mappings
        for discord_id, _ in mapped_players:
            await unlink_discord_riot(str(interaction.guild.id), discord_id)
        
        await interaction.followup.send("Successfully cleared all tracked players from the leaderboard and unlinked all Discord-Riot ID mappings.")
    except Exception as e:
        await interaction.followup.send(f"Failed to clear leaderboard: {e}")

async def fetch_app_emojis(bot):
    app_id = bot.user.id
    token = os.getenv("DISCORD_TOKEN")
    url = f"https://discord.com/api/v10/applications/{app_id}/emojis"
    headers = {"Authorization": f"Bot {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            bot.app_emojis = {e['name']: e['id'] for e in data.get('items', [])}

@bot.event
async def on_ready():
    print(f"Bot is ready! Logged in as {bot.user}")
    
    await fetch_app_emojis(bot)
    print(f"Fetched {len(getattr(bot, 'app_emojis', {}))} app emojis: {list(getattr(bot, 'app_emojis', {}).keys())[:20]}")

    await init_db()
    await ensure_puuid_table()
    await ensure_match_data_table()
    # Check for corrupted PUUID cache on startup
    print("Checking for corrupted PUUID cache entries...")
    corrupted_count = await clear_corrupted_puuid_cache()
    if corrupted_count > 0:
        print(f"Cleared {corrupted_count} corrupted PUUID entries on startup")
    # Clear corrupted match data cache entries on startup
    corrupted_match_data = await clear_corrupted_match_data_cache()
    if corrupted_match_data > 0:
        print(f"Cleared {corrupted_match_data} corrupted match_data entries on startup")
    # Clear expired match data cache entries on startup
    await clear_expired_match_data_cache()
    print("Pre-fetching PUUIDs...")
    await prefetch_puuids()
    check_streaks.start()
    check_strongest.start()
    clean_puuid_cache.start()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands.")
    except Exception as e:
        print(f"Error syncing commands: {e}")

# Add cleanup on shutdown
@bot.event
async def on_shutdown():
    print("Bot is shutting down...")
    await cleanup()  # Close the persistent session
    await bot.close()

bot.run(DISCORD_TOKEN)