import discord
from discord.ext import commands, tasks
from discord.ui import Button, View
import sqlite3
import time
from flask import Flask, render_template_string, redirect, request
import threading
import os

# ---------------- CONFIG ----------------
TOKEN = "DISCORD_TOKEN"
PROOFS_CHANNEL = "proofs"
LOG_CHANNEL = "rental-logs"
DB_PATH = "rentals.db"

# ---------------- DATABASE ----------------
first_run = not os.path.exists(DB_PATH)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS items (
    name TEXT PRIMARY KEY,
    price INTEGER,
    rented_by TEXT,
    paid INTEGER,
    expires_at INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS blacklist (
    user_id TEXT PRIMARY KEY,
    reason TEXT
)
""")
db.commit()

# ---------------- DISCORD BOT ----------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="?", intents=intents)

# ---------------- BLACKLIST ----------------
def is_blacklisted(user_id):
    cursor.execute("SELECT * FROM blacklist WHERE user_id=?", (str(user_id),))
    return cursor.fetchone() is not None

@bot.command()
async def blacklist(ctx, user: discord.Member, *, reason):
    if not ctx.author.guild_permissions.administrator:
        return
    cursor.execute("INSERT OR REPLACE INTO blacklist VALUES (?,?)", (str(user.id), reason))
    db.commit()
    await ctx.send(f"{user.mention} blacklisted: {reason}")

# ---------------- ITEM COMMANDS ----------------
@bot.command()
async def item(ctx, action, name=None, price=None, duration=None):
    if not ctx.author.guild_permissions.administrator:
        return await ctx.send("Admin only.")

    if action == "add":
        if not price:
            return await ctx.send("Specify price (e.g., 1000 coins or 1000 money)")
        parts = price.split()
        if len(parts) != 2:
            return await ctx.send("Price format: <amount> <coins/money>")
        amount, currency = parts
        try:
            amount = int(amount)
        except:
            return await ctx.send("Invalid amount.")
        if currency.lower() not in ["coins", "money"]:
            return await ctx.send("Currency must be coins or money.")
        cursor.execute("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?)", (name, amount, None, 0, None))
        db.commit()
        await ctx.send(f"Added **{name}** for {amount} {currency}")

    elif action == "rent":
        if not ctx.message.mentions:
            return await ctx.send("Mention a user to rent the item to.")
        user = ctx.message.mentions[0]

        if is_blacklisted(user.id):
            return await ctx.send(f"{user.mention} is blacklisted.")

        if duration is None:
            return await ctx.send("Specify duration (e.g., 24h, 7d, 2w, 1m)")

        unit = duration[-1].lower()
        try:
            value = int(duration[:-1])
        except:
            return await ctx.send("Invalid duration format.")

        # Convert to seconds
        if unit == "h":
            seconds = value * 3600
        elif unit == "d":
            seconds = value * 86400
        elif unit == "w":
            seconds = value * 604800
        elif unit == "m":
            seconds = value * 2592000  # 30 days
        else:
            return await ctx.send("Invalid duration unit. Use h, d, w, or m.")

        expires = int(time.time()) + seconds

        cursor.execute(
            "UPDATE items SET rented_by=?, paid=0, expires_at=? WHERE name=?",
            (str(user.id), expires, name)
        )
        db.commit()

        await ctx.send(f"{name} rented to {user.mention} for {duration}")
        await user.send(f"Your set **{name}** is ready for {duration}. Send proof in **#{PROOFS_CHANNEL}**")

    elif action == "return":
        cursor.execute(
            "UPDATE items SET rented_by=NULL, paid=0, expires_at=NULL WHERE name=?",
            (name,)
        )
        db.commit()
        await ctx.send(f"{name} returned")

# ---------------- DELETE COMMAND ----------------
@bot.command()
async def delete(ctx, name):
    if not ctx.author.guild_permissions.administrator:
        return await ctx.send("Admin only.")
    cursor.execute("SELECT * FROM items WHERE name=?", (name,))
    row = cursor.fetchone()
    if row:
        cursor.execute("DELETE FROM items WHERE name=?", (name,))
        db.commit()
        await ctx.send(f"Set **{name}** deleted.")
    else:
        await ctx.send(f"Set **{name}** not found.")

# ---------------- LIST COMMAND ----------------
@bot.command()
async def list(ctx):
    cursor.execute("SELECT * FROM items")
    rows = cursor.fetchall()
    embed = discord.Embed(title="Custom Sets")
    for name, price, rented_by, paid, expires in rows:
        if rented_by is None:
            status = "Available"
        elif paid == 0:
            status = "Waiting Payment"
        else:
            if expires:
                remaining = max(int((expires - time.time()) / 3600), 0)
                status = f"RENTED ({remaining}h left)"
            else:
                status = "RENTED"
        embed.add_field(name=name, value=f"{price} | {status}", inline=False)
    await ctx.send(embed=embed)

# ---------------- BUTTON APPROVAL ----------------
class ApproveView(View):
    def __init__(self, user_id, item_name):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.item_name = item_name

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green)
    async def approve(self, interaction: discord.Interaction, button: Button):
        cursor.execute("UPDATE items SET paid=1 WHERE name=?", (self.item_name,))
        db.commit()
        user = await bot.fetch_user(int(self.user_id))
        await user.send(f"Payment approved for {self.item_name}")
        await interaction.response.send_message("Approved.", ephemeral=True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.red)
    async def reject(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("Rejected.", ephemeral=True)

# ---------------- PROOF LOGGING ----------------
@bot.event
async def on_message(message):
    if message.channel.name == PROOFS_CHANNEL and message.attachments:
        cursor.execute("SELECT name FROM items WHERE rented_by=?", (str(message.author.id),))
        item = cursor.fetchone()
        if item:
            log = discord.utils.get(message.guild.channels, name=LOG_CHANNEL)
            if log:
                await log.send(f"Proof from {message.author.mention}", view=ApproveView(message.author.id, item[0]))
                for file in message.attachments:
                    await log.send(file.url)
    await bot.process_commands(message)

# ---------------- AUTO EXPIRY ----------------
@tasks.loop(minutes=1)
async def check_expired():
    now = int(time.time())
    cursor.execute("SELECT name, rented_by FROM items WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
    expired = cursor.fetchall()
    for name, user_id in expired:
        cursor.execute(
            "UPDATE items SET rented_by=NULL, paid=0, expires_at=NULL WHERE name=?",
            (name,)
        )
        db.commit()
        try:
            user = await bot.fetch_user(int(user_id))
            await user.send(f"Your rental **{name}** has expired and was returned.")
        except:
            pass
        for guild in bot.guilds:
            log = discord.utils.get(guild.channels, name=LOG_CHANNEL)
            if log:
                await log.send(f"EXPIRED: {name} returned from <@{user_id}>")

# ---------------- WEB PANEL ----------------
app = Flask(__name__)

@app.route("/")
def panel():
    cursor.execute("SELECT * FROM items")
    items = cursor.fetchall()
    cursor.execute("SELECT * FROM blacklist")
    bl = cursor.fetchall()
    html = """..."""  # Keep same HTML as before (web panel)
    return render_template_string(html, items=items, bl=bl, time=time)

# ... Keep all the web routes the same ...

def run_web():
    app.run(port=5000)

threading.Thread(target=run_web).start()

# ---------------- ON READY ----------------
@bot.event
async def on_ready():
    print("FULL SYSTEM ONLINE")
    await bot.change_presence(activity=discord.Game(name="Renting Minecraft sets"))

    # ✅ Start background loop here
    if not check_expired.is_running():
        check_expired.start()

# ---------------- RUN BOT ----------------
bot.run(os.getenv("DISCORD_TOKEN"))

