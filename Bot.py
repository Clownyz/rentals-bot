import discord
from discord.ext import commands, tasks
from discord.utils import get
from discord import ButtonStyle, Interaction
from discord.ui import Button, View
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, render_template_string
import os
from threading import Thread

# -----------------------------
# Flask setup (web panel)
# -----------------------------
app = Flask(__name__)

# -----------------------------
# Database setup
# -----------------------------
conn = sqlite3.connect('rentals.db', check_same_thread=False)
cursor = conn.cursor()

# Create tables if not exists
cursor.execute('''
CREATE TABLE IF NOT EXISTS items (
    name TEXT PRIMARY KEY,
    price REAL,
    currency TEXT,
    rented_by TEXT,
    expires_at TEXT
)
''')
cursor.execute('''
CREATE TABLE IF NOT EXISTS blacklist (
    user TEXT PRIMARY KEY
)
''')
conn.commit()

# -----------------------------
# Discord bot setup
# -----------------------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='?', intents=intents)

# -----------------------------
# Helper functions
# -----------------------------
def parse_duration(duration: str):
    try:
        amount, unit = int(duration[:-1]), duration[-1]
        if unit == 'h':
            return timedelta(hours=amount)
        elif unit == 'd':
            return timedelta(days=amount)
        elif unit == 'w':
            return timedelta(weeks=amount)
        else:
            return None
    except:
        return None

def is_blacklisted(user):
    cursor.execute("SELECT 1 FROM blacklist WHERE user=?", (user,))
    return cursor.fetchone() is not None

# -----------------------------
# Discord commands
# -----------------------------
@bot.command()
async def item_add(ctx, name, price: float, currency):
    cursor.execute("INSERT OR REPLACE INTO items (name, price, currency) VALUES (?, ?, ?)", (name, price, currency))
    conn.commit()
    await ctx.send(f"Item `{name}` added with price {price} {currency}.")

@bot.command()
async def item_delete(ctx, name):
    cursor.execute("DELETE FROM items WHERE name=?", (name,))
    conn.commit()
    await ctx.send(f"Item `{name}` deleted.")

@bot.command()
async def list_items(ctx):
    cursor.execute("SELECT name, price, currency, rented_by FROM items")
    rows = cursor.fetchall()
    if not rows:
        await ctx.send("No items found.")
        return
    msg = "**Available Rentals:**\n"
    for row in rows:
        status = "Rented" if row[3] else "Available"
        msg += f"- {row[0]}: {row[1]} {row[2]} ({status})\n"
    await ctx.send(msg)

@bot.command()
async def item_rent(ctx, name, duration: str):
    if is_blacklisted(ctx.author.name):
        await ctx.send("You are blacklisted and cannot rent items.")
        return

    cursor.execute("SELECT rented_by, price, currency FROM items WHERE name=?", (name,))
    item = cursor.fetchone()
    if not item:
        await ctx.send("Item not found.")
        return
    if item[0]:
        await ctx.send("Item is already rented.")
        return

    td = parse_duration(duration)
    if not td:
        await ctx.send("Invalid duration. Use format like 1h, 2d, 1w.")
        return

    expires = datetime.utcnow() + td
    cursor.execute("UPDATE items SET rented_by=?, expires_at=? WHERE name=?", (ctx.author.name, expires.isoformat(), name))
    conn.commit()
    await ctx.send(f"{ctx.author.name} rented `{name}` for {duration}.")

    # DM confirmation
    try:
        await ctx.author.send(f"Your rental of `{name}` is confirmed until {expires} UTC.")
    except:
        pass

# -----------------------------
# Background task to check expired rentals
# -----------------------------
@tasks.loop(minutes=1)
async def check_expired():
    now = datetime.utcnow().isoformat()
    cursor.execute("SELECT name, rented_by FROM items WHERE rented_by IS NOT NULL AND expires_at < ?", (now,))
    expired = cursor.fetchall()
    for name, user in expired:
        channel = get(bot.get_all_channels(), name='rental-logs')
        if channel:
            await channel.send(f"Rental expired: `{name}` returned by {user}.")
        # clear rental
        cursor.execute("UPDATE items SET rented_by=NULL, expires_at=NULL WHERE name=?", (name,))
        conn.commit()
        # DM user
        try:
            member = get(bot.get_all_members(), name=user)
            if member:
                await member.send(f"Your rental `{name}` has expired and been returned automatically.")
        except:
            pass

# -----------------------------
# Blacklist commands
# -----------------------------
@bot.command()
async def blacklist_add(ctx, user):
    cursor.execute("INSERT OR REPLACE INTO blacklist (user) VALUES (?)", (user,))
    conn.commit()
    await ctx.send(f"User `{user}` added to blacklist.")

@bot.command()
async def blacklist_remove(ctx, user):
    cursor.execute("DELETE FROM blacklist WHERE user=?", (user,))
    conn.commit()
    await ctx.send(f"User `{user}` removed from blacklist.")

@bot.command()
async def blacklist_list(ctx):
    cursor.execute("SELECT user FROM blacklist")
    users = cursor.fetchall()
    if not users:
        await ctx.send("No users in blacklist.")
        return
    msg = "**Blacklisted Users:**\n" + "\n".join([u[0] for u in users])
    await ctx.send(msg)

# -----------------------------
# Flask routes
# -----------------------------
@app.route('/')
def panel():
    cursor.execute("SELECT name, price, currency, rented_by, expires_at FROM items")
    rows = cursor.fetchall()
    html = "<h1>Rentals INC Panel</h1><table border=1><tr><th>Item</th><th>Price</th><th>Currency</th><th>Status</th><th>Expires At</th></tr>"
    for row in rows:
        status = "Rented" if row[3] else "Available"
        html += f"<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td><td>{status}</td><td>{row[4]}</td></tr>"
    html += "</table>"
    return render_template_string(html)

# -----------------------------
# Bot ready event
# -----------------------------
@bot.event
async def on_ready():
    print("FULL SYSTEM ONLINE")
    check_expired.start()

# -----------------------------
# Run Flask in separate thread
# -----------------------------
if __name__ == "__main__":
    Thread(target=lambda: app.run(host='0.0.0.0', port=10000)).start()
    bot.run(os.getenv("DISCORD_TOKEN"))
