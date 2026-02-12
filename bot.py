import discord
from discord.ext import commands, tasks
import os
import sqlite3
import time
import asyncio
import aiohttp
from datetime import datetime, timedelta
import logging

# Setup logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger('gtps_bot')

# Configuration
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GTPS_SERVER_URL = os.getenv('GTPS_SERVER_URL', 'http://localhost:8080')
VP_CHANNEL_ID = int(os.getenv('VP_CHANNEL_ID', '0'))
GEMS_CHANNEL_ID = int(os.getenv('GEMS_CHANNEL_ID', '0'))

# VP earning rates
VP_PER_MINUTE = 2  # 2 VP per minute in VP channel
GEMS_MULTIPLIER = 1.05  # 1.05x gems in gems channel
CHECK_INTERVAL = 60  # Check every 60 seconds

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix='/', intents=intents)

# Database setup
class Database:
    def __init__(self, db_path='gtps_vp.db'):
        self.db_path = db_path
        self.init_db()
    
    def get_connection(self):
        return sqlite3.connect(self.db_path)
    
    def init_db(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # VP tracking by Discord ID only
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vp_balance (
                discord_id INTEGER PRIMARY KEY,
                discord_name TEXT,
                vp INTEGER DEFAULT 0,
                total_earned INTEGER DEFAULT 0,
                last_seen INTEGER
            )
        ''')
        
        # Discord to GrowID links
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS discord_links (
                discord_id INTEGER PRIMARY KEY,
                growid TEXT UNIQUE,
                linked_at INTEGER,
                verified INTEGER DEFAULT 0,
                pending_code TEXT
            )
        ''')
        
        # Voice tracking table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS voice_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id INTEGER,
                channel_id INTEGER,
                joined_at INTEGER,
                left_at INTEGER,
                vp_earned INTEGER DEFAULT 0
            )
        ''')
        
        conn.commit()
        conn.close()
        logger.info("Database initialized")
    
    def get_or_create_user(self, discord_id, discord_name):
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Check if exists
        cursor.execute('SELECT * FROM vp_balance WHERE discord_id=?', (discord_id,))
        user = cursor.fetchone()
        
        if not user:
            # Create new user
            cursor.execute('''
                INSERT INTO vp_balance (discord_id, discord_name, last_seen)
                VALUES (?, ?, ?)
            ''', (discord_id, discord_name, int(time.time())))
            conn.commit()
            cursor.execute('SELECT * FROM vp_balance WHERE discord_id=?', (discord_id,))
            user = cursor.fetchone()
        
        conn.close()
        return user
    
    def add_vp(self, discord_id, amount):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE vp_balance SET vp=vp+?, total_earned=total_earned+?, last_seen=?
            WHERE discord_id=?
        ''', (amount, amount, int(time.time()), discord_id))
        conn.commit()
        
        # Get new balance
        cursor.execute('SELECT vp FROM vp_balance WHERE discord_id=?', (discord_id,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else 0
    
    def get_vp(self, discord_id):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT vp FROM vp_balance WHERE discord_id=?', (discord_id,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else 0
    
    def spend_vp(self, discord_id, amount):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT vp FROM vp_balance WHERE discord_id=?', (discord_id,))
        user = cursor.fetchone()
        if not user or user[0] < amount:
            conn.close()
            return False
        
        cursor.execute('UPDATE vp_balance SET vp=vp-? WHERE discord_id=?', (amount, discord_id))
        conn.commit()
        conn.close()
        return True
    
    def get_leaderboard(self, limit=10):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT discord_name, total_earned FROM vp_balance
            ORDER BY total_earned DESC LIMIT ?
        ''', (limit,))
        results = cursor.fetchall()
        conn.close()
        return results

# Initialize database
db = Database()

# Voice state tracking
voice_tracking = {}  # {discord_id: {'channel_id': id, 'joined_at': timestamp}}

@bot.event
async def on_ready():
    logger.info(f'Bot logged in as {bot.user}')
    logger.info(f'VP Channel ID: {VP_CHANNEL_ID}')
    logger.info(f'Gems Channel ID: {GEMS_CHANNEL_ID}')
    
    # Start background tasks
    check_voice_states.start()
    save_data.start()
    
    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

@bot.event
async def on_voice_state_update(member, before, after):
    """Track when users join/leave voice channels"""
    discord_id = member.id
    
    # User joined a voice channel
    if after.channel and not before.channel:
        voice_tracking[discord_id] = {
            'channel_id': after.channel.id,
            'joined_at': time.time()
        }
        # Create user if doesn't exist
        db.get_or_create_user(discord_id, member.name)
        logger.info(f"{member.name} joined voice channel {after.channel.name}")
    
    # User left a voice channel
    elif before.channel and not after.channel:
        if discord_id in voice_tracking:
            del voice_tracking[discord_id]
            logger.info(f"{member.name} left voice channel")
    
    # User switched channels
    elif before.channel and after.channel and before.channel.id != after.channel.id:
        voice_tracking[discord_id] = {
            'channel_id': after.channel.id,
            'joined_at': time.time()
        }
        logger.info(f"{member.name} switched to {after.channel.name}")

@tasks.loop(seconds=CHECK_INTERVAL)
async def check_voice_states():
    """Award VP to users in voice channels"""
    current_time = time.time()
    awarded_count = 0
    
    for discord_id, data in list(voice_tracking.items()):
        channel_id = data['channel_id']
        joined_at = data['joined_at']
        
        # Calculate time in channel
        minutes_elapsed = (current_time - joined_at) / 60
        
        # Only award if been in channel for at least 1 minute
        if minutes_elapsed >= 1:
            # Award VP if in VP channel
            if channel_id == VP_CHANNEL_ID:
                vp_earned = int(minutes_elapsed * VP_PER_MINUTE)
                if vp_earned > 0:
                    new_balance = db.add_vp(discord_id, vp_earned)
                    logger.info(f"[VP] Awarded {vp_earned} VP to Discord ID {discord_id} (new balance: {new_balance})")
                    awarded_count += 1
                    
                    # Reset timer
                    voice_tracking[discord_id]['joined_at'] = current_time
            
            # Log gems channel activity
            elif channel_id == GEMS_CHANNEL_ID:
                logger.info(f"[GEMS] Discord ID {discord_id} in gems channel (1.05x multiplier)")
    
    if awarded_count > 0:
        logger.info(f"[CHECK] Awarded VP to {awarded_count} user(s)")

@tasks.loop(minutes=5)
async def save_data():
    """Periodic save notification"""
    logger.info("[AUTO-SAVE] Database saved")

# Slash Commands
@bot.tree.command(name="verify", description="Verify your GrowID with the code from in-game")
async def verify_command(interaction: discord.Interaction, code: str):
    """Verify account with code from in-game /link"""
    discord_id = interaction.user.id
    code = code.strip().upper()
    
    # Check if already verified
    conn = db.get_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT growid, verified FROM discord_links WHERE discord_id=?', (discord_id,))
    existing = cursor.fetchone()
    
    if existing and existing[1] == 1:
        await interaction.response.send_message(
            f"✅ You are already verified with **{existing[0]}**!",
            ephemeral=True
        )
        conn.close()
        return
    
    # Find pending link with this code
    cursor.execute('SELECT discord_id, growid FROM discord_links WHERE pending_code=? AND verified=0', (code,))
    pending = cursor.fetchone()
    
    if not pending:
        await interaction.response.send_message(
            "❌ Invalid or expired verification code!\n"
            "Use `/link` command in-game first to get a code.",
            ephemeral=True
        )
        conn.close()
        return
    
    pending_discord_id, growid = pending
    
    # Check if this Discord ID trying to verify matches the one from game
    # Actually, we want to UPDATE the pending link to use THIS discord_id
    cursor.execute('''
        UPDATE discord_links 
        SET discord_id=?, verified=1, pending_code=NULL, linked_at=?
        WHERE pending_code=?
    ''', (discord_id, int(time.time()), code))
    conn.commit()
    
    # Create VP balance entry
    db.get_or_create_user(discord_id, interaction.user.name)
    
    await interaction.response.send_message(
        f"✅ **Verification Successful!**\n\n"
        f"**Discord:** {interaction.user.mention}\n"
        f"**GrowID:** {growid}\n\n"
        f"You can now earn VP by staying in voice channels!\n"
        f"Use `/vp` to check your balance.",
        ephemeral=True
    )
    
    logger.info(f"Verified: {interaction.user.name} ({discord_id}) → {growid}")
    conn.close()

@bot.tree.command(name="unlink", description="Unlink your Discord account from GrowID (Admin only)")
async def unlink_command(interaction: discord.Interaction, user: discord.Member = None):
    """Unlink account (admin command)"""
    # Check if admin
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ Only administrators can use this command!",
            ephemeral=True
        )
        return
    
    target_id = user.id if user else interaction.user.id
    target_name = user.mention if user else interaction.user.mention
    
    conn = db.get_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT growid FROM discord_links WHERE discord_id=?', (target_id,))
    existing = cursor.fetchone()
    
    if not existing:
        await interaction.response.send_message(
            f"❌ {target_name} is not linked!",
            ephemeral=True
        )
        conn.close()
        return
    
    cursor.execute('DELETE FROM discord_links WHERE discord_id=?', (target_id,))
    conn.commit()
    conn.close()
    
    await interaction.response.send_message(
        f"✅ Unlinked {target_name} from **{existing[0]}**",
        ephemeral=True
    )
    logger.info(f"Unlinked Discord ID {target_id} from {existing[0]}")

@bot.tree.command(name="whois", description="Check who a GrowID is linked to")
async def whois_command(interaction: discord.Interaction, growid: str):
    """Check GrowID link"""
    conn = db.get_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT discord_id, linked_at FROM discord_links WHERE growid=?', (growid,))
    result = cursor.fetchone()
    conn.close()
    
    if not result:
        await interaction.response.send_message(
            f"❌ **{growid}** is not linked to any Discord account.",
            ephemeral=True
        )
        return
    
    discord_id, linked_at = result
    user = await bot.fetch_user(discord_id)
    linked_date = datetime.fromtimestamp(linked_at).strftime('%Y-%m-%d %H:%M')
    
    embed = discord.Embed(
        title="🔗 Account Link Info",
        color=discord.Color.blue()
    )
    embed.add_field(name="GrowID", value=f"**{growid}**", inline=True)
    embed.add_field(name="Discord", value=user.mention, inline=True)
    embed.add_field(name="Linked Since", value=linked_date, inline=False)
    
    # Get VP balance
    vp = db.get_vp(discord_id)
    embed.add_field(name="VP Balance", value=f"{vp:,}", inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="mylink", description="Check your linked GrowID")
async def mylink_command(interaction: discord.Interaction):
    """Check own link"""
    conn = db.get_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT growid, linked_at, verified FROM discord_links WHERE discord_id=?', (interaction.user.id,))
    result = cursor.fetchone()
    conn.close()
    
    if not result:
        await interaction.response.send_message(
            "❌ You are not linked!\n"
            "Use `/link` command **in-game** to get a verification code.",
            ephemeral=True
        )
        return
    
    growid, linked_at, verified = result
    
    if not verified:
        await interaction.response.send_message(
            "⚠️ **Pending Verification**\n\n"
            f"GrowID: **{growid}**\n"
            "Your link is not verified yet!\n\n"
            "Check your in-game console for the verification code.",
            ephemeral=True
        )
        return
    
    linked_date = datetime.fromtimestamp(linked_at).strftime('%Y-%m-%d %H:%M')
    vp = db.get_vp(interaction.user.id)
    
    embed = discord.Embed(
        title="🔗 Your Account Link",
        color=discord.Color.green()
    )
    embed.add_field(name="GrowID", value=f"**{growid}**", inline=True)
    embed.add_field(name="VP Balance", value=f"{vp:,}", inline=True)
    embed.add_field(name="Status", value="✅ Verified", inline=True)
    embed.add_field(name="Linked Since", value=linked_date, inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="vp", description="Check your Voice Points balance")
async def vp_command(interaction: discord.Interaction):
    """Check VP balance"""
    user = db.get_or_create_user(interaction.user.id, interaction.user.name)
    
    # Get GrowID if linked
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT growid, verified FROM discord_links WHERE discord_id=?', (interaction.user.id,))
    link_result = cursor.fetchone()
    conn.close()
    
    embed = discord.Embed(
        title="💎 Voice Points Balance",
        color=discord.Color.purple()
    )
    embed.add_field(name="Discord", value=interaction.user.mention, inline=True)
    
    if link_result:
        growid, verified = link_result
        if verified:
            embed.add_field(name="GrowID", value=f"✅ **{growid}**", inline=True)
        else:
            embed.add_field(name="GrowID", value=f"⚠️ {growid} (Pending)", inline=True)
    else:
        embed.add_field(name="GrowID", value="Not linked", inline=True)
    
    embed.add_field(name="Current VP", value=f"{user[2]:,}", inline=True)
    embed.add_field(name="Total Earned", value=f"{user[3]:,}", inline=True)
    
    # Check if in voice channel
    member = interaction.guild.get_member(interaction.user.id)
    if member.voice and member.voice.channel:
        channel_name = member.voice.channel.name
        if member.voice.channel.id == VP_CHANNEL_ID:
            embed.add_field(
                name="🎙️ Active Bonus", 
                value=f"Earning {VP_PER_MINUTE} VP/min in **{channel_name}**",
                inline=False
            )
        elif member.voice.channel.id == GEMS_CHANNEL_ID:
            embed.add_field(
                name="💎 Active Bonus",
                value=f"1.05x Gems multiplier in **{channel_name}**",
                inline=False
            )
    
    if not link_result:
        embed.set_footer(text="Use /link in-game to get a verification code!")
    elif not link_result[1]:  # verified = 0
        embed.set_footer(text="Complete verification with the code from in-game!")
    else:
        embed.set_footer(text="Use /vpshop in-game to spend your VP!")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="leaderboard", description="View top VP earners")
async def leaderboard_command(interaction: discord.Interaction):
    """Show VP leaderboard"""
    top_users = db.get_leaderboard(10)
    
    embed = discord.Embed(
        title="🏆 Top Voice Point Earners",
        description="Earn VP by staying in voice channels!",
        color=discord.Color.gold()
    )
    
    medals = ["🥇", "🥈", "🥉"]
    for i, (discord_name, total_vp) in enumerate(top_users, 1):
        medal = medals[i-1] if i <= 3 else f"#{i}"
        embed.add_field(
            name=f"{medal} {discord_name}",
            value=f"**{total_vp:,}** VP",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="help", description="Show bot commands and information")
async def help_command(interaction: discord.Interaction):
    """Show help information"""
    embed = discord.Embed(
        title="🎮 GTPS Voice Points Bot",
        description="Earn Voice Points by staying in voice channels!",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="🔗 Getting Started",
        value=(
            "1️⃣ Use `/link` command **in-game**\n"
            "2️⃣ You'll receive a 6-digit code\n"
            "3️⃣ Use `/verify <code>` here in Discord\n"
            "4️⃣ Start earning VP!"
        ),
        inline=False
    )
    
    embed.add_field(
        name="📝 Account Commands",
        value=(
            "`/verify <code>` - Verify with in-game code\n"
            "`/mylink` - Check your linked GrowID\n"
            "`/whois <growid>` - Check who owns a GrowID\n"
            "`/unlink @user` - Unlink account (Admin)"
        ),
        inline=False
    )
    
    embed.add_field(
        name="💰 Voice Points",
        value=(
            "`/vp` - Check your VP balance\n"
            "`/leaderboard` - View top earners\n"
            "`/help` - Show this message"
        ),
        inline=False
    )
    
    embed.add_field(
        name="💎 Earning VP",
        value=f"Stay in <#{VP_CHANNEL_ID}> to earn **{VP_PER_MINUTE} VP per minute**",
        inline=False
    )
    
    embed.add_field(
        name="🎁 Gems Bonus",
        value=f"Stay in <#{GEMS_CHANNEL_ID}> for **1.05x gems** while playing in-game",
        inline=False
    )
    
    embed.add_field(
        name="🛒 In-Game Commands",
        value=(
            "`/link` - Get verification code\n"
            "`/vp` - Check your VP\n"
            "`/vpshop` - Browse shop\n"
            "`/vpbuy <id>` - Purchase items"
        ),
        inline=False
    )
    
    embed.set_footer(text="Start with /link in-game to get your verification code!")
    await interaction.response.send_message(embed=embed)

# Run bot
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not set!")
        exit(1)
    
    bot.run(DISCORD_TOKEN)

