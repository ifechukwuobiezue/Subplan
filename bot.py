import os
import logging
import threading
import asyncio
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from flask import Flask, request as flask_request
from supabase import create_client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHANNEL_ID  = int(os.getenv("CHANNEL_ID"))
ADMIN_IDS   = [int(x) for x in os.getenv("ADMIN_IDS").split(",")]
CRON_SECRET = os.getenv("CRON_SECRET", "change-me-secret")
db          = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

logging.basicConfig(format="%(asctime)s %(message)s", level=logging.INFO)

ADMIN_STATE = {}

PACKAGES = [
    {"name": "1 Min",    "duration_minutes": 1},
    {"name": "1 Month",  "duration_days": 30},
    {"name": "3 Months", "duration_days": 90},
    {"name": "6 Months", "duration_days": 180},
    {"name": "1 Year",   "duration_days": 365},
]

# ── Flask ─────────────────────────────────────────────────────────────────────

flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return "Bot is alive ✅", 200

@flask_app.route("/cron/kick", methods=["GET", "POST"])
def cron_kick():
    if flask_request.args.get("secret") != CRON_SECRET:
        return "Unauthorized", 401
    bot = Bot(token=BOT_TOKEN)
    _kick_expired_sync(bot)
    return "Kick job done", 200

@flask_app.route("/cron/remind", methods=["GET", "POST"])
def cron_remind():
    if flask_request.args.get("secret") != CRON_SECRET:
        return "Unauthorized", 401
    bot = Bot(token=BOT_TOKEN)
    _reminders_sync(bot)
    return "Reminders job done", 200


# ── Helpers ───────────────────────────────────────────────────────────────────

def format_expiry_countdown(expiry_iso: str) -> str:
    expiry = datetime.fromisoformat(expiry_iso)
    delta  = expiry - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return "Expired"
    total_days = delta.days
    if total_days >= 365:
        years = total_days // 365
        rem   = total_days % 365
        return f"{years} year{'s' if years > 1 else ''}{', '+str(rem)+' day'+('s' if rem != 1 else '') if rem else ''}"
    if total_days >= 30:
        months = total_days // 30
        days   = total_days % 30
        return f"{months} month{'s' if months > 1 else ''}{', '+str(days)+' day'+('s' if days != 1 else '') if days else ''}"
    if total_days > 0:
        return f"{total_days} day{'s' if total_days > 1 else ''}"
    hours = int(delta.total_seconds() // 3600)
    return f"{hours} hour{'s' if hours != 1 else ''}"


# ── Sync jobs ─────────────────────────────────────────────────────────────────

def _kick_expired_sync(bot: Bot):
    async def _do():
        now     = datetime.now(timezone.utc).isoformat()
        expired = db.table("members").select("user_id, username").lte("expiry", now).eq("removed", False).execute().data

        for m in expired:
            try:
                await bot.ban_chat_member(CHANNEL_ID, m["user_id"])
                await bot.unban_chat_member(CHANNEL_ID, m["user_id"])
                db.table("members").update({"removed": True}).eq("user_id", m["user_id"]).execute()
                try:
                    await bot.send_message(m["user_id"],
                        "🚪 *Your subscription has expired.*\n\n"
                        "You have been removed from Athena's Hub.\n\n"
                        "To regain access, renew your subscription via /pay and send your receipt here. 🙏",
                        parse_mode="Markdown")
                except Exception:
                    pass
                for admin in ADMIN_IDS:
                    await bot.send_message(admin,
                        f"🚪 *Evicted:* {m['username'] or m['user_id']} — subscription expired.",
                        parse_mode="Markdown")
            except Exception as e:
                logging.error(f"Kick failed for {m['user_id']}: {e}")

    asyncio.run(_do())


def _reminders_sync(bot: Bot):
    async def _do():
        now = datetime.now(timezone.utc)

        for days in [3, 1]:
            start   = (now + timedelta(days=days, hours=-1)).isoformat()
            end     = (now + timedelta(days=days, hours=1)).isoformat()
            members = db.table("members").select("user_id, username, expiry").gte("expiry", start).lte("expiry", end).eq("removed", False).execute().data

            for m in members:
                expiry = datetime.fromisoformat(m["expiry"]).strftime("%Y-%m-%d")
                try:
                    await bot.send_message(m["user_id"],
                        f"⏰ *Heads up!* Your subscription expires in *{days} day{'s' if days > 1 else ''}* ({expiry}).\n\n"
                        f"Renew now → contact @AthenasHub 🙏",
                        parse_mode="Markdown")
                except Exception:
                    for admin in ADMIN_IDS:
                        await bot.send_message(admin,
                            f"⚠️ Couldn't DM {m['username'] or m['user_id']} — sub expires in {days}d.",
                            parse_mode="Markdown")

    asyncio.run(_do())


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    await update.message.reply_text(
        "👋 Welcome to *Athena's Hub*!\n\n"
        "🔷 *Payment:* Use /pay to see pricing and payment details.\n"
        "🔷 *Renew Subscription:* Use /renew to stay active. 🔄\n\n"
        "❓ Got questions? Reach out to @AthenasHub",
        parse_mode="Markdown")


# ── /pay ──────────────────────────────────────────────────────────────────────

async def cmd_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    caption = (
        "💳 *Payment Details*\n\n"
        "🏦 *Bank:* Kuda Bank\n"
        "💰 *Account Number:* `3004015285`\n"
        "👤 *Account Name:* Pradage Ventures\n\n"
        "After payment kindly send your receipt here.\n\n"
        "_For non-Nigerians, kindly DM @AthenasHub for a different payment method._"
    )
    await update.message.reply_photo(photo=os.getenv("FLYER_FILE_ID"), caption=caption, parse_mode="Markdown")


# ── /renew ────────────────────────────────────────────────────────────────────

async def cmd_renew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    await update.message.reply_text(
        "🔄 To renew, make your payment and send the receipt *right here*.\n\n"
        "Admin will review it and your access will be extended once approved ✅\n\n"
        "Need payment details? Use /pay",
        parse_mode="Markdown")


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await context.bot.send_chat_action(uid, "typing")

    rows = db.table("members").select("package, expiry").eq("user_id", uid).eq("removed", False).execute().data

    if not rows:
        await update.message.reply_text("ℹ️ You don't have an active subscription.", parse_mode="Markdown")
        return

    row = rows[0]
    expiry_dt = datetime.fromisoformat(row["expiry"]) if row.get("expiry") else None
    if not expiry_dt:
        status, expiry_str, countdown = "⏳ Pending", "N/A", "N/A"
    else:
        status = "✅ Active" if expiry_dt > datetime.now(timezone.utc) else "❌ Expired"
        expiry_str = expiry_dt.strftime("%b %d, %Y")
        countdown = format_expiry_countdown(row["expiry"])

    await update.message.reply_text(
        f"📊 *Your Subscription Status*\n\n"
        f"📦 Plan: {row.get('package', 'N/A')}\n"
        f"🔰 Status: {status}\n"
        f"📅 Expires: {expiry_str}\n"
        f"⏳ Time left: {countdown}",
        parse_mode="Markdown")


# ── /getfileid ────────────────────────────────────────────────────────────────

async def cmd_getfileid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text("📸 Send me a photo or PDF and I'll return its file ID.")


async def handle_getfileid_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        await update.message.reply_text(f"🖼️ *Photo File ID:*\n`{file_id}`", parse_mode="Markdown")
    elif update.message.document:
        file_id = update.message.document.file_id
        await update.message.reply_text(f"📄 *Document File ID:*\n`{file_id}`", parse_mode="Markdown")


async def handle_photo_or_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMIN_IDS:
        await handle_getfileid_media(update, context)
        return
    await handle_receipt(update, context)


# ── User sends receipt ────────────────────────────────────────────────────────

async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    raw_name = f"@{user.username}" if user.username else (user.first_name or str(user.id))
    name = raw_name.replace(":", "_")

    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    await update.message.reply_text(
        "✅ *Receipt received!*\n\nOur team will review shortly. You'll get your invite link here once approved.",
        parse_mode="Markdown")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{user.id}:{name}"),
        InlineKeyboardButton("❌ Deny",    callback_data=f"deny:{user.id}:{name}")
    ]])

    for admin in ADMIN_IDS:
        await context.bot.forward_message(admin, update.effective_chat.id, update.message.message_id)
        await context.bot.send_message(admin,
            f"👋 Hey chief! {name} just sent a payment receipt.\nReceipt is above 👆",
            parse_mode="Markdown",
            reply_markup=keyboard)


# ── Admin taps Approve → show package buttons ─────────────────────────────────

async def callback_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return

    parts   = query.data.split(":")
    user_id = int(parts[1])
    name    = parts[2]

    ADMIN_STATE[query.from_user.id] = {"action": "approve", "user_id": user_id, "username": name}

    keyboard = [[InlineKeyboardButton(
        p["name"], callback_data=f"pkg:{user_id}:{i}:{name}"
    )] for i, p in enumerate(PACKAGES)]

    await query.edit_message_text(
        f"Select the package for {name}:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard))


# ── Admin picks package → save + send invite ──────────────────────────────────

async def callback_pkg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return

    parts   = query.data.split(":")
    user_id = int(parts[1])
    pkg_idx = int(parts[2])
    name    = parts[3]

    ADMIN_STATE.pop(query.from_user.id, None)

    pkg = PACKAGES[pkg_idx]
    now = datetime.now(timezone.utc)
    delta = timedelta(minutes=pkg["duration_minutes"]) if pkg.get("duration_minutes") else timedelta(days=pkg["duration_days"])

    # Cumulative Renewal Logic
    existing = db.table("members").select("expiry").eq("user_id", user_id).execute().data
    was_extended = bool(existing and existing[0]["expiry"] and datetime.fromisoformat(existing[0]["expiry"]) > now)
    if existing and existing[0]["expiry"]:
        base   = datetime.fromisoformat(existing[0]["expiry"])
        expiry = (base if base > now else now) + delta
        db.table("members").update({
            "expiry": expiry.isoformat(), "package": pkg["name"],
            "removed": False, "username": name
        }).eq("user_id", user_id).execute()
    else:
        expiry = now + delta
        db.table("members").insert({
            "user_id": user_id, "username": name, "package": pkg["name"],
            "expiry": expiry.isoformat(), "added_at": now.isoformat(), "removed": False
        }).execute()

    try:
        link = (await context.bot.create_chat_invite_link(
            CHANNEL_ID, member_limit=1, name=f"user_{user_id}"
        )).invite_link

        if was_extended:
            dm_text = (
                f"🎉 *Payment Approved!*\n\n"
                f"Your subscription has been *extended by {pkg['name']}*.\n"
                f"New expiry: `{expiry.strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
                f"Tap the link below to request access:\n{link}\n\n"
                f"Thanks for staying with Athena's Hub! 🙌"
            )
        else:
            dm_text = (
                f"🎉 *Payment Approved!*\n\n"
                f"Package: *{pkg['name']}*\n"
                f"Expires: `{expiry.strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
                f"Tap the link below to request access:\n{link}\n\n"
                f"Welcome to Athena's Hub! 🙌"
            )
        await context.bot.send_message(user_id, dm_text, parse_mode="Markdown")

        await query.edit_message_text(
            f"✅ {name} approved on *{pkg['name']}*. Invite link sent.",
            parse_mode="Markdown")

    except Exception as e:
        await query.edit_message_text(
            f"✅ Saved but couldn't DM {name}:\n`{e}`",
            parse_mode="Markdown")


# ── Admin taps Deny → ask for reason ─────────────────────────────────────────

async def callback_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return

    parts   = query.data.split(":")
    user_id = int(parts[1])
    name    = parts[2]

    ADMIN_STATE[query.from_user.id] = {"action": "deny", "user_id": user_id, "username": name}

    await query.edit_message_text(
        f"Type your reason for denying {name} and send it here.\n"
        f"The bot will forward it to them.",
        parse_mode="Markdown")


# ── Catch all text — deny reason OR wrong input ───────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id

    if admin_id in ADMIN_STATE and ADMIN_STATE[admin_id]["action"] == "deny":
        state   = ADMIN_STATE.pop(admin_id)
        user_id = state["user_id"]
        name    = state["username"]
        reason  = update.message.text
        try:
            await context.bot.send_message(user_id,
                f"❌ *Payment Not Approved*\n\nReason: _{reason}_\n\n"
                f"_For any issues please contact @AthenasHub for help_",
                parse_mode="Markdown")
            await update.message.reply_text(f"Done. Reason sent to {name}.")
        except Exception:
            await update.message.reply_text(f"Couldn't DM {name} — they may not have started the bot.")
        return

    await update.message.reply_text(
        "⚠️ We only accept a *screenshot* or *PDF* as payment proof.\n\n"
        "Go to your bank app, take a screenshot of the successful transaction, and send it here.",
        parse_mode="Markdown")


# ── Admin commands ────────────────────────────────────────────────────────────

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await context.bot.send_chat_action(update.effective_chat.id, "typing")

    members = db.table("members").select("username, expiry, removed, package") \
                                 .order("removed").execute().data

    if not members:
        await update.message.reply_text("📭 No members found.")
        return

    lines = ["👥 *Member Ledger:*\n"]
    for m in members:
        if m["removed"]:
            status_str = "🚪 Removed"
        else:
            countdown = format_expiry_countdown(m["expiry"]) if m["expiry"] else "Pending"
            status_str = f"✅ Active ({countdown} left)"

        lines.append(f"• {m['username'] or 'Unknown'} — {m.get('package', '?')} | {status_str}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS or not context.args:
        return
    user_id = int(context.args[0])
    await context.bot.ban_chat_member(CHANNEL_ID, user_id)
    await context.bot.unban_chat_member(CHANNEL_ID, user_id)
    db.table("members").update({"removed": True}).eq("user_id", user_id).execute()
    await update.message.reply_text(f"✅ `{user_id}` removed.", parse_mode="Markdown")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    bot = Bot(token=BOT_TOKEN)
    _kick_expired_sync(bot)
    await update.message.reply_text("✅ Expiry check done.")


# ── Boot ──────────────────────────────────────────────────────────────────────

def run_flask():
    port = int(os.getenv("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("pay",       cmd_pay))
    app.add_handler(CommandHandler("renew",     cmd_renew))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("list",      cmd_list))
    app.add_handler(CommandHandler("remove",    cmd_remove))
    app.add_handler(CommandHandler("check",     cmd_check))
    app.add_handler(CommandHandler("getfileid", cmd_getfileid))

    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Document.PDF) & filters.ChatType.PRIVATE,
        handle_photo_or_doc
    ))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_text
    ))

    app.add_handler(CallbackQueryHandler(callback_approve, pattern=r"^approve:"))
    app.add_handler(CallbackQueryHandler(callback_deny,    pattern=r"^deny:"))
    app.add_handler(CallbackQueryHandler(callback_pkg,     pattern=r"^pkg:"))

    print("🤖 Bot polling...")
    app.run_polling()

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()
