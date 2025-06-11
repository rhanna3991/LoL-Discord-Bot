import aiosqlite

async def init_db():
    async with aiosqlite.connect("riot_bot.db") as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tracked_players (
                guild_id TEXT,
                summoner_name TEXT,
                region TEXT,
                last_match_id TEXT
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS highest_ranked (
                guild_id TEXT PRIMARY KEY,
                summoner_name TEXT,
                region TEXT,
                tier TEXT,
                rank TEXT,
                lp INTEGER
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tiltcheck_settings (
                guild_id TEXT PRIMARY KEY,
                enabled INTEGER
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tiltcheck_cooldowns (
                guild_id TEXT,
                summoner_name TEXT,
                last_match_id TEXT,
                last_tiltcheck_time TIMESTAMP,
                last_streak_length INTEGER,
                PRIMARY KEY (guild_id, summoner_name)
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS winstreak_cooldowns (
                guild_id TEXT,
                summoner_name TEXT,
                last_match_id TEXT,
                last_winstreak_time TIMESTAMP,
                last_streak_length INTEGER,
                PRIMARY KEY (guild_id, summoner_name)
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS wincheck_settings (
                guild_id TEXT PRIMARY KEY,
                enabled INTEGER
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS notification_channels (
                guild_id TEXT PRIMARY KEY,
                channel_id TEXT
            )
        ''')

        await conn.execute('''
            CREATE TABLE IF NOT EXISTS discord_riot_mapping (
                guild_id TEXT,
                discord_id TEXT,
                riot_id TEXT,
                PRIMARY KEY (guild_id, discord_id)
            )
        ''')

        await conn.commit()

async def add_tracked_player(guild_id, summoner_name, region):
    async with aiosqlite.connect("riot_bot.db") as conn:
        # Check if player already exists (case-insensitive)
        async with conn.execute('''
            SELECT summoner_name FROM tracked_players
            WHERE guild_id = ? AND LOWER(summoner_name) = LOWER(?)
        ''', (guild_id, summoner_name)) as cursor:
            existing_player = await cursor.fetchone()
            if existing_player:
                raise ValueError(f"Player {summoner_name} is already being tracked in this server.")

        await conn.execute('''
            INSERT INTO tracked_players (guild_id, summoner_name, region, last_match_id)
            VALUES (?, ?, ?, NULL)
        ''', (guild_id, summoner_name, region.lower()))

        await conn.commit()

async def get_tracked_players(guild_id):
    async with aiosqlite.connect("riot_bot.db") as conn:
        async with conn.execute('''
            SELECT summoner_name, region FROM tracked_players
            WHERE guild_id = ?
        ''', (guild_id,)) as cursor:
            rows = await cursor.fetchall()
            return rows

async def remove_tracked_player(guild_id, summoner_name):
    async with aiosqlite.connect("riot_bot.db") as conn:
        await conn.execute('''
            DELETE FROM tracked_players
            WHERE guild_id = ? AND LOWER(summoner_name) = LOWER(?)
        ''', (guild_id, summoner_name))
        await conn.commit()

async def toggle_tiltcheck(guild_id):
    async with aiosqlite.connect("riot_bot.db") as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tiltcheck_settings (
                guild_id TEXT PRIMARY KEY,
                enabled INTEGER
            )
        ''')

        async with conn.execute('SELECT enabled FROM tiltcheck_settings WHERE guild_id = ?', (guild_id,)) as cursor:
            row = await cursor.fetchone()

        if row:
            new_status = 0 if row[0] else 1
            await conn.execute('UPDATE tiltcheck_settings SET enabled = ? WHERE guild_id = ?', (new_status, guild_id))
        else:
            new_status = 1
            await conn.execute('INSERT INTO tiltcheck_settings (guild_id, enabled) VALUES (?, ?)', (guild_id, new_status))

        await conn.commit()
        return new_status == 1

async def is_tiltcheck_enabled(guild_id):
    async with aiosqlite.connect("riot_bot.db") as conn:
        async with conn.execute('SELECT enabled FROM tiltcheck_settings WHERE guild_id = ?', (guild_id,)) as cursor:
            row = await cursor.fetchone()
            return row and row[0] == 1

async def update_tiltcheck_cooldown(guild_id, summoner_name, match_id, streak_length):
    async with aiosqlite.connect("riot_bot.db") as conn:
        await conn.execute('''
            INSERT OR REPLACE INTO tiltcheck_cooldowns 
            (guild_id, summoner_name, last_match_id, last_tiltcheck_time, last_streak_length)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
        ''', (guild_id, summoner_name, match_id, streak_length))
        await conn.commit()

async def get_tiltcheck_cooldown(guild_id, summoner_name):
    async with aiosqlite.connect("riot_bot.db") as conn:
        async with conn.execute('''
            SELECT last_match_id, last_tiltcheck_time, last_streak_length 
            FROM tiltcheck_cooldowns 
            WHERE guild_id = ? AND summoner_name = ?
        ''', (guild_id, summoner_name)) as cursor:
            result = await cursor.fetchone()
            return result if result else (None, None, 0)

async def update_winstreak_cooldown(guild_id, summoner_name, match_id, streak_length):
    async with aiosqlite.connect("riot_bot.db") as conn:
        await conn.execute('''
            INSERT OR REPLACE INTO winstreak_cooldowns 
            (guild_id, summoner_name, last_match_id, last_winstreak_time, last_streak_length)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
        ''', (guild_id, summoner_name, match_id, streak_length))
        await conn.commit()

async def get_winstreak_cooldown(guild_id, summoner_name):
    async with aiosqlite.connect("riot_bot.db") as conn:
        async with conn.execute('''
            SELECT last_match_id, last_winstreak_time, last_streak_length
            FROM winstreak_cooldowns 
            WHERE guild_id = ? AND summoner_name = ?
        ''', (guild_id, summoner_name)) as cursor:
            result = await cursor.fetchone()
            return result if result else (None, None, 0)

async def toggle_wincheck(guild_id):
    async with aiosqlite.connect("riot_bot.db") as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS wincheck_settings (
                guild_id TEXT PRIMARY KEY,
                enabled INTEGER
            )
        ''')

        async with conn.execute('SELECT enabled FROM wincheck_settings WHERE guild_id = ?', (guild_id,)) as cursor:
            row = await cursor.fetchone()

        if row:
            new_status = 0 if row[0] else 1
            await conn.execute('UPDATE wincheck_settings SET enabled = ? WHERE guild_id = ?', (new_status, guild_id))
        else:
            new_status = 1
            await conn.execute('INSERT INTO wincheck_settings (guild_id, enabled) VALUES (?, ?)', (guild_id, new_status))

        await conn.commit()
        return new_status == 1

async def is_wincheck_enabled(guild_id):
    async with aiosqlite.connect("riot_bot.db") as conn:
        async with conn.execute('SELECT enabled FROM wincheck_settings WHERE guild_id = ?', (guild_id,)) as cursor:
            row = await cursor.fetchone()
            return row and row[0] == 1

async def set_notification_channel(guild_id, channel_id):
    async with aiosqlite.connect("riot_bot.db") as conn:
        await conn.execute('''
            INSERT OR REPLACE INTO notification_channels (guild_id, channel_id)
            VALUES (?, ?)
        ''', (guild_id, channel_id))
        await conn.commit()

async def get_notification_channel(guild_id):
    async with aiosqlite.connect("riot_bot.db") as conn:
        async with conn.execute('''
            SELECT channel_id FROM notification_channels
            WHERE guild_id = ?
        ''', (guild_id,)) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else None

async def link_discord_riot(guild_id, discord_id, riot_id):
    async with aiosqlite.connect("riot_bot.db") as conn:
        # Verify the Riot ID exists in tracked_players
        async with conn.execute('''
            SELECT summoner_name FROM tracked_players
            WHERE guild_id = ? AND LOWER(summoner_name) = LOWER(?)
        ''', (guild_id, riot_id)) as cursor:
            if not await cursor.fetchone():
                raise ValueError(f"Riot ID {riot_id} is not being tracked in this server.")

        # Store the mapping
        await conn.execute('''
            INSERT OR REPLACE INTO discord_riot_mapping (guild_id, discord_id, riot_id)
            VALUES (?, ?, ?)
        ''', (guild_id, discord_id, riot_id))
        await conn.commit()

async def get_riot_id_for_discord(guild_id, discord_id):
    async with aiosqlite.connect("riot_bot.db") as conn:
        async with conn.execute('''
            SELECT riot_id FROM discord_riot_mapping
            WHERE guild_id = ? AND discord_id = ?
        ''', (guild_id, discord_id)) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else None

async def get_discord_id_for_riot(guild_id, riot_id):
    async with aiosqlite.connect("riot_bot.db") as conn:
        async with conn.execute('''
            SELECT discord_id FROM discord_riot_mapping
            WHERE guild_id = ? AND LOWER(riot_id) = LOWER(?)
        ''', (guild_id, riot_id)) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else None

async def get_all_mapped_players(guild_id):
    async with aiosqlite.connect("riot_bot.db") as conn:
        async with conn.execute(
            "SELECT discord_id, riot_id FROM discord_riot_mapping WHERE guild_id = ?",
            (guild_id,)
        ) as cursor:
            return await cursor.fetchall()

async def unlink_discord_riot(guild_id, discord_id):
    """Unlink a Discord account from its Riot ID mapping."""
    async with aiosqlite.connect("riot_bot.db") as conn:
        await conn.execute(
            "DELETE FROM discord_riot_mapping WHERE guild_id = ? AND discord_id = ?",
            (guild_id, discord_id)
        )
        await conn.commit()
