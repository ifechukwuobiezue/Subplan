import os
import logging
import threading
import asyncio
import random
import string
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from flask import Flask, request as flask_request
from supabase import create_client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot, ChatMemberAdministrator, ChatMemberOwner
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, ChatMemberHandler, ContextTypes, filters)

load_dotenv()

BOT_TOKEN          = os.getenv("BOT_TOKEN")
ADMIN_IDS          = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
WELCOME_FILE_ID    = os.getenv("WELCOME_FILE_ID")        # Photo/video shown on /start welcome screen
ONBOARDING_FILE_ID = os.getenv("ONBOARDING_FILE_ID")     # Photo/video shown during client onboarding plan selection
CRON_SECRET        = os.getenv("CRON_SECRET", "change-me-secret")
db                 = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

logging.basicConfig(format="%(asctime)s %(message)s", level=logging.INFO)

# ── Payment account (SubPlanBot platform) ─────────────────────────────────────
PLATFORM_BANK_NAME    = "Kuda Bank"
PLATFORM_ACCT_NUMBER  = "2003661688"
PLATFORM_ACCT_NAME    = "Paul-Mary Chukwuka Omile"

# ── In-memory state ────────────────────────────────────────────────────────────
CLIENT_STATE = {}   # { user_id: { "step": ..., ...data } }
ADMIN_STATE  = {}   # { admin_id: { "action": ..., ... } }

# Onboarding steps
STEP_BRAND     = "brand"
STEP_BANK      = "bank"
STEP_ACCT_NUM  = "acct_num"
STEP_ACCT_NAME = "acct_name"
STEP_FLYER     = "flyer"
STEP_PACKAGES  = "packages"
STEP_CHANNEL   = "channel"
STEP_DONE      = "done"

# Member subscribe step
STEP_REFCODE = "refcode"

# Settings sub-steps
SETTINGS_EDIT_BRAND     = "edit_brand"
SETTINGS_EDIT_BANK      = "edit_bank"
SETTINGS_EDIT_ACCT_NUM  = "edit_acct_num"
SETTINGS_EDIT_ACCT_NAME = "edit_acct_name"
SETTINGS_EDIT_FLYER     = "edit_flyer"
SETTINGS_ADD_PKG        = "add_pkg_name"
SETTINGS_ADD_PKG_PRICE  = "add_pkg_price"
SETTINGS_ADD_PKG_DUR    = "add_pkg_dur"

# ── Flask ──────────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return "SubPlanBot is alive ✅", 200

@flask_app.route("/cron/kick-members", methods=["GET", "POST"])
def cron_kick_members():
    if flask_request.args.get("secret") != CRON_SECRET:
        return "Unauthorized", 401
    bot = Bot(token=BOT_TOKEN)
    _kick_expired_members_sync(bot)
    return "Member kick done", 200

@flask_app.route("/cron/kick-clients", methods=["GET", "POST"])
def cron_kick_clients():
    if flask_request.args.get("secret") != CRON_SECRET:
        return "Unauthorized", 401
    bot = Bot(token=BOT_TOKEN)
    _kick_expired_clients_sync(bot)
    return "Client kick done", 200

@flask_app.route("/cron/remind-members", methods=["GET", "POST"])
def cron_remind_members():
    if flask_request.args.get("secret") != CRON_SECRET:
        return "Unauthorized", 401
    bot = Bot(token=BOT_TOKEN)
    _remind_members_sync(bot)
    return "Member reminders done", 200

@flask_app.route("/cron/remind-clients", methods=["GET", "POST"])
def cron_remind_clients():
    if flask_request.args.get("secret") != CRON_SECRET:
        return "Unauthorized", 401
    bot = Bot(token=BOT_TOKEN)
    _remind_clients_sync(bot)
    return "Client reminders done", 200

@flask_app.route("/cron/inactivity", methods=["GET", "POST"])
def cron_inactivity():
    if flask_request.args.get("secret") != CRON_SECRET:
        return "Unauthorized", 401
    bot = Bot(token=BOT_TOKEN)
    _inactivity_sync(bot)
    return "Inactivity check done", 200


# ── Helpers ────────────────────────────────────────────────────────────────────
def get_client(user_id: int):
    res = db.table("clients").select("*").eq("client_id", user_id).execute().data
    return res[0] if res else None

def get_client_by_ref(ref_code: str):
    res = db.table("clients").select("*").eq("ref_code", ref_code.upper()).eq("removed", False).execute().data
    return res[0] if res else None

def is_client(user_id: int) -> bool:
    c = get_client(user_id)
    return c is not None and not c.get("removed", False)

def is_active_client(user_id: int) -> bool:
    c = get_client(user_id)
    if not c or c.get("removed", False):
        return False
    expiry = datetime.fromisoformat(c["expiry"])
    return expiry > datetime.now(timezone.utc)

def update_last_seen(user_id: int):
    db.table("clients").update({"last_seen": datetime.now(timezone.utc).isoformat()}).eq("client_id", user_id).execute()

def generate_ref_code(brand_name: str) -> str:
    """Generate a unique ref code like BRAND-AB3X"""
    prefix = "".join(c for c in brand_name.upper() if c.isalpha())[:4]
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    code   = f"{prefix}-{suffix}"
    # Ensure uniqueness
    while db.table("clients").select("ref_code").eq("ref_code", code).execute().data:
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        code   = f"{prefix}-{suffix}"
    return code

async def send_media_or_text(bot_or_context, chat_id: int, file_id: str | None,
                              caption: str, reply_markup=None, parse_mode="Markdown"):
    """Send photo if file_id exists, otherwise plain text."""
    kwargs = dict(parse_mode=parse_mode, reply_markup=reply_markup)
    if file_id:
        # Try as photo first, fall back to text
        try:
            await bot_or_context.send_photo(chat_id, photo=file_id, caption=caption, **kwargs)
            return
        except Exception:
            pass
    await bot_or_context.send_message(chat_id, caption, **kwargs)


# ── Cron: kick expired members ─────────────────────────────────────────────────
def _kick_expired_members_sync(bot: Bot):
    async def _do():
        now = datetime.now(timezone.utc).isoformat()
        active_clients = db.table("clients").select("client_id, channel_id").gt("expiry", now).eq("removed", False).execute().data
        for client in active_clients:
            expired = db.table("members").select("user_id, username").lte("expiry", now).eq("removed", False).eq("client_id", client["client_id"]).execute().data
            for m in expired:
                try:
                    await bot.ban_chat_member(client["channel_id"], m["user_id"])
                    await bot.unban_chat_member(client["channel_id"], m["user_id"])
                    db.table("members").update({"removed": True}).eq("user_id", m["user_id"]).eq("client_id", client["client_id"]).execute()
                    try:
                        await bot.send_message(m["user_id"],
                            "🚪 *Your subscription has expired.*\n\n"
                            "You've been removed from the channel.\n\n"
                            "To regain access, renew your subscription via /pay 🙏",
                            parse_mode="Markdown")
                    except Exception:
                        pass
                except Exception as e:
                    logging.error(f"Member kick failed for {m['user_id']}: {e}")
    asyncio.run(_do())


# ── Cron: kick expired clients ─────────────────────────────────────────────────
def _kick_expired_clients_sync(bot: Bot):
    async def _do():
        now = datetime.now(timezone.utc).isoformat()
        expired = db.table("clients").select("client_id, username").lte("expiry", now).eq("removed", False).execute().data
        for c in expired:
            try:
                db.table("clients").update({"removed": True}).eq("client_id", c["client_id"]).execute()
                try:
                    await bot.send_message(c["client_id"],
                        "🚪 *Your SubPlanBot subscription has expired.*\n\n"
                        "Your channel is no longer being managed.\n\n"
                        "Use /pay to renew and restore full service. 🙏",
                        parse_mode="Markdown")
                except Exception:
                    pass
                for admin in ADMIN_IDS:
                    await bot.send_message(admin,
                        f"🚪 *Client evicted:* {c['username'] or c['client_id']} — subscription expired.",
                        parse_mode="Markdown")
            except Exception as e:
                logging.error(f"Client kick failed for {c['client_id']}: {e}")
    asyncio.run(_do())


# ── Cron: remind members ───────────────────────────────────────────────────────
def _remind_members_sync(bot: Bot):
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
                        f"Renew now via /pay 🙏",
                        parse_mode="Markdown")
                except Exception:
                    pass
    asyncio.run(_do())


# ── Cron: remind clients ───────────────────────────────────────────────────────
def _remind_clients_sync(bot: Bot):
    async def _do():
        now = datetime.now(timezone.utc)
        for days in [3, 1]:
            start   = (now + timedelta(days=days, hours=-1)).isoformat()
            end     = (now + timedelta(days=days, hours=1)).isoformat()
            clients = db.table("clients").select("client_id, username, expiry").gte("expiry", start).lte("expiry", end).eq("removed", False).execute().data
            for c in clients:
                expiry = datetime.fromisoformat(c["expiry"]).strftime("%Y-%m-%d")
                try:
                    await bot.send_message(c["client_id"],
                        f"⏰ *Heads up!* Your SubPlanBot subscription expires in *{days} day{'s' if days > 1 else ''}* ({expiry}).\n\n"
                        f"Renew now → /pay to keep your channel running 🙏",
                        parse_mode="Markdown")
                except Exception:
                    pass
        start_1h = (now + timedelta(hours=1, minutes=-10)).isoformat()
        end_1h   = (now + timedelta(hours=1, minutes=10)).isoformat()
        clients_1h = db.table("clients").select("client_id, username, expiry").gte("expiry", start_1h).lte("expiry", end_1h).eq("removed", False).execute().data
        for c in clients_1h:
            try:
                await bot.send_message(c["client_id"],
                    "🚨 *1 hour left!* Your SubPlanBot subscription expires very soon.\n\n"
                    "Renew immediately → /pay to avoid service interruption ⚡",
                    parse_mode="Markdown")
            except Exception:
                pass
    asyncio.run(_do())


# ── Cron: inactivity (30 days) ─────────────────────────────────────────────────
def _inactivity_sync(bot: Bot):
    async def _do():
        threshold = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        inactive  = db.table("clients").select("client_id, username, last_seen").lt("last_seen", threshold).eq("removed", False).execute().data
        for c in inactive:
            try:
                await bot.send_message(c["client_id"],
                    "👋 *Hey! We miss you.*\n\n"
                    "We noticed you haven't used SubPlanBot in a while.\n\n"
                    "Your channel management is still running — just wanted to check in! "
                    "Type /start if you need anything 😊",
                    parse_mode="Markdown")
            except Exception:
                pass
    asyncio.run(_do())


# ── Universal Channel Verification Helper ──────────────────────────────────────
async def process_channel_verification(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    state = CLIENT_STATE.get(uid, {})
    if state.get("step") != STEP_CHANNEL:
        return False

    channel_identifier = None

    if getattr(update.message, "forward_from_chat", None) and update.message.forward_from_chat.type == "channel":
        channel_identifier = update.message.forward_from_chat.id
    elif getattr(update.message, "forward_origin", None) and getattr(update.message.forward_origin, "type", None) == "channel":
        channel_identifier = update.message.forward_origin.chat.id
    elif update.message.text and update.message.text.startswith("@"):
        channel_identifier = update.message.text.strip()

    if not channel_identifier:
        await update.message.reply_text("⚠️ Please either forward a message from your channel, or send your public @username.")
        return True

    await context.bot.send_chat_action(uid, "typing")
    try:
        chat       = await context.bot.get_chat(channel_identifier)
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)

        if not isinstance(bot_member, (ChatMemberAdministrator, ChatMemberOwner)):
            await update.message.reply_text("⚠️ I found the channel, but I am not an admin yet. Please add me as an admin and try again.")
            return True

        state["channel_id"]   = chat.id
        state["channel_name"] = chat.title
        CLIENT_STATE[uid]     = state

        await _show_plan_selection(context.bot, uid, chat.title)

    except Exception as e:
        logging.error(f"Channel verification failed: {e}")
        await update.message.reply_text("⚠️ I couldn't access that channel. Make sure the username is correct or try forwarding a message instead.")

    return True


async def _show_plan_selection(bot, uid: int, channel_name: str):
    """Show the Sandbox / Pay plan selection after channel is confirmed."""
    caption = (
        "🚀 *SubPlanBot Plans*\n\n"
        "🧪 *Admin Sandbox* — 5 mins, test with another Telegram account\n"
        "💳 *Monthly Plan* — ₦5,000/month\n\n"
        "_Use the Sandbox to see exactly how your members' experience will look before going live!_\n\n"
        "Choose an option below 👇"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🧪 Admin Sandbox",  callback_data=f"onboard:trial:{uid}"),
        InlineKeyboardButton("💳 Make Payment",   callback_data=f"onboard:pay:{uid}")
    ]])
    await bot.send_message(
        uid,
        f"✅ *Channel verified:* {channel_name}\n\nI've been added as admin successfully! 🎉",
        parse_mode="Markdown"
    )
    await send_media_or_text(bot, uid, ONBOARDING_FILE_ID, caption, reply_markup=keyboard)


# ── /start ─────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid  = user.id

    await context.bot.send_chat_action(uid, "typing")

    # Platform admin
    if uid in ADMIN_IDS:
        await update.message.reply_text(
            "👑 *SubPlanBot Admin Panel*\n\n"
            "/clientlist — view all active clients\n"
            "/removeclient [id] — evict a client\n"
            "/check — trigger expiry check manually",
            parse_mode="Markdown")
        return

    # Existing active client
    if is_active_client(uid):
        update_last_seen(uid)
        client = get_client(uid)
        await update.message.reply_text(
            f"👋 *Welcome back, {client.get('brand_name', 'there')}!*\n\n"
            "Here's what you can do:\n\n"
            "📋 /list — view your active members\n"
            "❌ /remove [id] — remove a member\n"
            "⚙️ /settings — manage your brand, packages & payment info\n"
            "💳 /pay — renew your SubPlanBot subscription\n"
            f"\n🔑 *Your Ref Code:* `{client.get('ref_code', 'N/A')}`\n"
            "_Share this code with your members so they can subscribe via this bot._",
            parse_mode="Markdown")
        return

    # Existing but expired client
    if is_client(uid):
        await update.message.reply_text(
            "⚠️ *Your SubPlanBot subscription has expired.*\n\n"
            "Use /pay to renew and restore your channel management.",
            parse_mode="Markdown")
        return

    # New user — show welcome screen with two-path buttons
    welcome_text = (
        "👋 *Welcome to SubPlanBot!* 🎉\n\n"
        "The easiest way to run a *paid Telegram subscription channel* — no coding, no stress.\n\n"
        "What brings you here today? 👇"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏢 I'm a Channel Admin",      callback_data="welcome:admin")],
        [InlineKeyboardButton("🎟️ Subscribe to a Channel", callback_data="welcome:subscribe")]
    ])

    if WELCOME_FILE_ID:
        try:
            await update.message.reply_photo(photo=WELCOME_FILE_ID, caption=welcome_text,
                                             parse_mode="Markdown", reply_markup=keyboard)
            return
        except Exception:
            pass
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=keyboard)


# ── Callback: welcome screen ───────────────────────────────────────────────────
async def callback_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    action = query.data.split(":")[1]

    await context.bot.send_chat_action(uid, "typing")

    if action == "admin":
        # Check if already a client (active or expired)
        if is_active_client(uid):
            update_last_seen(uid)
            client = get_client(uid)
            await query.edit_message_caption(
                caption=f"👋 *Welcome back, {client.get('brand_name', 'there')}!*\n\n"
                        "Here's what you can do:\n\n"
                        "📋 /list — view your active members\n"
                        "❌ /remove [id] — remove a member\n"
                        "⚙️ /settings — manage your brand, packages & payment info\n"
                        "💳 /pay — renew your SubPlanBot subscription\n"
                        f"\n🔑 *Your Ref Code:* `{client.get('ref_code', 'N/A')}`",
                parse_mode="Markdown") if query.message.photo else None
            if not query.message.photo:
                await query.edit_message_text(
                    f"👋 *Welcome back, {client.get('brand_name', 'there')}!*\n\n"
                    "Here's what you can do:\n\n"
                    "📋 /list — view your active members\n"
                    "❌ /remove [id] — remove a member\n"
                    "⚙️ /settings — manage your brand, packages & payment info\n"
                    "💳 /pay — renew your SubPlanBot subscription\n"
                    f"\n🔑 *Your Ref Code:* `{client.get('ref_code', 'N/A')}`",
                    parse_mode="Markdown")
            return

        if is_client(uid):
            msg = "⚠️ *Your SubPlanBot subscription has expired.*\n\nUse /pay to renew."
            if query.message.photo:
                await query.edit_message_caption(caption=msg, parse_mode="Markdown")
            else:
                await query.edit_message_text(msg, parse_mode="Markdown")
            return

        # Start onboarding
        CLIENT_STATE[uid] = {"step": STEP_BRAND}
        onboard_text = (
            "🏢 *Great! Let's get your channel set up.*\n\n"
            "It only takes a couple of minutes and your channel will be fully automated. 🚀\n\n"
            "First things first — what's your *brand name*?\n"
            "_(e.g. Athena's Hub, TechSignals Pro)_"
        )
        if query.message.photo:
            await query.edit_message_caption(caption=onboard_text, parse_mode="Markdown")
        else:
            await query.edit_message_text(onboard_text, parse_mode="Markdown")

    elif action == "subscribe":
        CLIENT_STATE[uid] = {"step": STEP_REFCODE}
        sub_text = (
            "🎟️ *Subscribe to a Channel*\n\n"
            "Please enter the *Ref Code* shared by your channel admin.\n\n"
            "It looks something like: `ACME-X7K2`\n\n"
            "👉 Type or paste it below:"
        )
        if query.message.photo:
            await query.edit_message_caption(caption=sub_text, parse_mode="Markdown")
        else:
            await query.edit_message_text(sub_text, parse_mode="Markdown")


# ── Onboarding state machine ───────────────────────────────────────────────────
async def handle_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    state = CLIENT_STATE.get(uid, {})
    step  = state.get("step")
    text  = update.message.text or ""

    if step == STEP_BRAND:
        state["brand_name"] = text.strip()
        state["step"]       = STEP_BANK
        CLIENT_STATE[uid]   = state
        await update.message.reply_text(
            f"✅ *Brand name saved:* {state['brand_name']}\n\n"
            "💼 Now, what's your *bank name*? _(e.g. Kuda Bank, GTBank)_",
            parse_mode="Markdown")

    elif step == STEP_BANK:
        state["bank_name"] = text.strip()
        state["step"]      = STEP_ACCT_NUM
        CLIENT_STATE[uid]  = state
        await update.message.reply_text("🔢 What's your *account number*?", parse_mode="Markdown")

    elif step == STEP_ACCT_NUM:
        state["account_number"] = text.strip()
        state["step"]           = STEP_ACCT_NAME
        CLIENT_STATE[uid]       = state
        await update.message.reply_text("👤 What's the *account name*?", parse_mode="Markdown")

    elif step == STEP_ACCT_NAME:
        state["account_name"] = text.strip()
        state["step"]         = STEP_FLYER
        CLIENT_STATE[uid]     = state
        await update.message.reply_text(
            "🖼️ Now send your *payment flyer image*!\n\n"
            "This is the image your members will see when they use /pay to subscribe.",
            parse_mode="Markdown")

    elif step == STEP_PACKAGES:
        parts = [p.strip() for p in text.split(",")]
        if len(parts) != 3:
            await update.message.reply_text(
                "⚠️ Please use this format:\n`Package Name, Price, Duration in days`\n\nExample: `1 Month, 5000, 30`",
                parse_mode="Markdown")
            return
        pkg_name, price, duration = parts[0], parts[1], parts[2]
        if not duration.isdigit():
            await update.message.reply_text("⚠️ Duration must be a number (days). Example: `30`", parse_mode="Markdown")
            return
        packages = state.get("packages", [])
        packages.append({"name": pkg_name, "price": price, "duration_days": int(duration)})
        state["packages"] = packages
        CLIENT_STATE[uid] = state
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Add another package", callback_data="onboard:add_pkg"),
            InlineKeyboardButton("✅ Done",                callback_data="onboard:pkg_done")
        ]])
        pkg_list = "\n".join([f"• {p['name']} — ₦{p['price']} / {p['duration_days']}d" for p in packages])
        await update.message.reply_text(
            f"✅ *Package added!*\n\n*Your packages so far:*\n{pkg_list}\n\nAdd another or continue?",
            parse_mode="Markdown",
            reply_markup=keyboard)

    elif step == STEP_CHANNEL:
        # This is now handled by process_channel_verification, but show hint if raw text
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "📢 Add me to your channel",
                url="https://t.me/subplanhubbot?startchannel=setup&admin=invite_users+ban_users+manage_chat"
            )],
            [InlineKeyboardButton("✅ I've added the bot", callback_data="onboard:confirm_channel")]
        ])
        await update.message.reply_text(
            "Almost there! 🎉\n\n"
            "1️⃣ Tap *Add me to your channel* below and select your channel\n"
            "2️⃣ You may see a Telegram error — *ignore it*, I still get added!\n"
            "3️⃣ Click *I've added the bot* once done.",
            parse_mode="Markdown",
            reply_markup=keyboard)


# ── Onboarding: flyer photo ────────────────────────────────────────────────────
async def handle_onboarding_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    state = CLIENT_STATE.get(uid, {})
    step  = state.get("step")

    if step == STEP_FLYER:
        file_id           = update.message.photo[-1].file_id
        state["flyer_file_id"] = file_id
        state["step"]     = STEP_PACKAGES
        CLIENT_STATE[uid] = state
        await update.message.reply_photo(
            photo=file_id,
            caption=f"✅ *Flyer saved!*\n\n`File ID: {file_id}`\n\n"
                    "📦 Now let's set up your *subscription packages*.\n\n"
                    "Send each package in this format:\n`Package Name, Price, Duration in days`\n\n"
                    "Example: `1 Month, 5000, 30`",
            parse_mode="Markdown")

    elif step == SETTINGS_EDIT_FLYER:
        file_id = update.message.photo[-1].file_id
        db.table("clients").update({"flyer_file_id": file_id}).eq("client_id", uid).execute()
        CLIENT_STATE.pop(uid, None)
        await update.message.reply_photo(
            photo=file_id,
            caption=f"✅ *Flyer updated!*\n`File ID: {file_id}`",
            parse_mode="Markdown")


# ── Bot added to channel event ────────────────────────────────────────────────
async def handle_bot_added(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result:
        return

    chat       = result.chat
    new_status = result.new_chat_member

    if chat.type != "channel":
        return
    if not isinstance(new_status, (ChatMemberAdministrator, ChatMemberOwner)):
        return

    channel_id   = chat.id
    channel_name = chat.title

    for uid, state in list(CLIENT_STATE.items()):
        if state.get("step") == STEP_CHANNEL:
            state["channel_id"]   = channel_id
            state["channel_name"] = channel_name
            CLIENT_STATE[uid]     = state
            try:
                await _show_plan_selection(context.bot, uid, channel_name)
            except Exception as e:
                logging.error(f"Could not notify client {uid} of channel add: {e}")
            break


# ── Callback: onboarding buttons ──────────────────────────────────────────────
async def callback_onboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    state  = CLIENT_STATE.get(uid, {})
    parts  = query.data.split(":")
    action = parts[1]

    RETRY_KEYBOARD = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📢 Add me to your channel",
            url="https://t.me/subplanhubbot?startchannel=setup&admin=invite_users+ban_users+manage_chat"
        )],
        [InlineKeyboardButton("✅ I've added the bot", callback_data="onboard:confirm_channel")]
    ])

    await context.bot.send_chat_action(uid, "typing")

    if action == "add_pkg":
        state["step"] = STEP_PACKAGES
        CLIENT_STATE[uid] = state
        await query.edit_message_text(
            "📦 Send the next package:\n`Package Name, Price, Duration in days`",
            parse_mode="Markdown")

    elif action == "pkg_done":
        state["step"] = STEP_CHANNEL
        CLIENT_STATE[uid] = state
        await query.edit_message_text(
            "🔗 *Almost there!* 🎉\n\n"
            "1️⃣ Tap *Add me to your channel* below and select your channel\n"
            "2️⃣ You may see a Telegram error — *ignore it*, I still get added!\n"
            "3️⃣ Click *I've added the bot* once done.",
            parse_mode="Markdown",
            reply_markup=RETRY_KEYBOARD)

    elif action == "confirm_channel":
        state = CLIENT_STATE.get(uid, {})
        found_channel_id   = state.get("channel_id")
        found_channel_name = state.get("channel_name")

        if not found_channel_id:
            await context.bot.send_message(
                chat_id=uid,
                text="⚠️ I haven't detected your channel yet.\n\n"
                     "To finish setup, do ONE of the following:\n"
                     "👉 *Option A:* Forward ANY message from your channel to me here.\n"
                     "👉 *Option B:* If your channel is public, type its username (e.g. @YourChannelName).",
                parse_mode="Markdown")
            return

        await context.bot.send_chat_action(uid, "typing")
        try:
            bot_member = await context.bot.get_chat_member(found_channel_id, context.bot.id)
            if not isinstance(bot_member, (ChatMemberAdministrator, ChatMemberOwner)):
                await context.bot.send_message(
                    chat_id=uid,
                    text="⚠️ I'm in the channel but not as an admin yet.\n\n"
                         "Please make sure *Admin Rights* is toggled on when adding me, then try again.",
                    parse_mode="Markdown",
                    reply_markup=RETRY_KEYBOARD)
                return
        except Exception:
            await context.bot.send_message(
                chat_id=uid,
                text="⚠️ Couldn't verify your channel yet.\n\n"
                     "👉 *Option A:* Forward ANY message from your channel here.\n"
                     "👉 *Option B:* Type your public channel username (e.g. @YourChannelName).",
                parse_mode="Markdown")
            return

        try:
            await query.edit_message_text(
                f"✅ *Channel confirmed:* {found_channel_name}\n\nNow choose how to get started 👇",
                parse_mode="Markdown")
        except Exception:
            pass

        await _show_plan_selection(context.bot, uid, found_channel_name)
        return

    elif action == "trial":
        uid_target = int(parts[2]) if len(parts) > 2 else uid
        state = CLIENT_STATE.get(uid_target, {})
        ref_code = generate_ref_code(state.get("brand_name", "SPB"))
        _save_new_client(uid_target, query.from_user.username, state, plan="sandbox",
                         expiry=datetime.now(timezone.utc) + timedelta(days=1),
                         ref_code=ref_code)
        CLIENT_STATE.pop(uid_target, None)

        # Edit the message if it has a caption (photo) or text
        sandbox_msg = (
            "🧪 *Admin Sandbox Activated!*\n\n"
            "You have *1 day* to test everything using a secondary Telegram account.\n\n"
            "Here's how to test:\n"
            "1️⃣ Copy your *Ref Code* below\n"
            "2️⃣ Open this bot on *another Telegram account*\n"
            "3️⃣ Click *Subscribe to a Channel* and enter your Ref Code\n"
            "4️⃣ Send a mock payment screenshot — approve it and watch the magic! ✨\n\n"
            f"🔑 *Your Ref Code:* `{ref_code}`\n\n"
            "Use /start to access your full dashboard 🚀"
        )
        try:
            if query.message.photo:
                await query.edit_message_caption(caption=sandbox_msg, parse_mode="Markdown")
            else:
                await query.edit_message_text(sandbox_msg, parse_mode="Markdown")
        except Exception:
            await context.bot.send_message(uid_target, sandbox_msg, parse_mode="Markdown")

    elif action == "pay":
        pay_msg = (
            f"💳 *Payment Details — SubPlanBot*\n\n"
            f"💰 Amount: ₦5,000/month\n\n"
            f"🏦 Bank: {PLATFORM_BANK_NAME}\n"
            f"💳 Account Number: `{PLATFORM_ACCT_NUMBER}`\n"
            f"👤 Account Name: {PLATFORM_ACCT_NAME}\n\n"
            "📸 Make payment and send your receipt here.\n"
            "Our team will activate your account shortly ✅"
        )
        state["awaiting_payment"] = True
        CLIENT_STATE[uid] = state
        try:
            if query.message.photo:
                await query.edit_message_caption(caption=pay_msg, parse_mode="Markdown")
            else:
                await query.edit_message_text(pay_msg, parse_mode="Markdown")
        except Exception:
            await context.bot.send_message(uid, pay_msg, parse_mode="Markdown")


def _save_new_client(uid, username, state, plan, expiry, ref_code=None):
    username_str = f"@{username}" if username else str(uid)
    if not ref_code:
        ref_code = generate_ref_code(state.get("brand_name", "SPB"))

    # Add a default "5 Min Demo" package so clients can immediately see the flow
    packages = list(state.get("packages", []))
    packages.insert(0, {"name": "5 Min Demo", "price": "0", "duration_days": 0, "duration_minutes": 5, "is_demo": True})

    db.table("clients").insert({
        "client_id":      uid,
        "username":       username_str,
        "brand_name":     state.get("brand_name"),
        "channel_id":     state.get("channel_id"),
        "channel_name":   state.get("channel_name"),
        "bank_name":      state.get("bank_name"),
        "account_number": state.get("account_number"),
        "account_name":   state.get("account_name"),
        "flyer_file_id":  state.get("flyer_file_id"),
        "packages":       packages,
        "ref_code":       ref_code,
        "plan":           plan,
        "expiry":         expiry.isoformat(),
        "added_at":       datetime.now(timezone.utc).isoformat(),
        "removed":        False,
        "last_seen":      datetime.now(timezone.utc).isoformat(),
    }).execute()


# ── Ref Code: member subscribe flow ───────────────────────────────────────────
async def handle_refcode_input(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    """Called when a user at STEP_REFCODE sends their ref code."""
    state    = CLIENT_STATE.get(uid, {})
    ref_code = (update.message.text or "").strip().upper()

    await context.bot.send_chat_action(uid, "typing")

    client = get_client_by_ref(ref_code)
    if not client:
        await update.message.reply_text(
            "❌ *Invalid Ref Code.*\n\n"
            "Please double-check the code shared by your channel admin and try again.\n\n"
            "_(It looks like: `ACME-X7K2`)_",
            parse_mode="Markdown")
        return

    if not is_active_client(client["client_id"]):
        await update.message.reply_text(
            "⚠️ This channel's subscription is currently *inactive*.\n\n"
            "Please contact the channel admin for assistance.",
            parse_mode="Markdown")
        CLIENT_STATE.pop(uid, None)
        return

    # Store which client/channel this member wants to subscribe to
    state["subscribing_to_client_id"] = client["client_id"]
    CLIENT_STATE[uid] = state

    packages = [p for p in (client.get("packages") or []) if not p.get("is_demo")]
    if not packages:
        await update.message.reply_text(
            "⚠️ This channel has no active packages yet. Contact the admin.",
            parse_mode="Markdown")
        CLIENT_STATE.pop(uid, None)
        return

    pkg_lines = "\n".join([f"• *{p['name']}* — ₦{p['price']} ({p['duration_days']}d)" for p in packages])
    caption   = (
        f"🎉 *{client['brand_name']}* — Subscribe\n\n"
        f"Available packages:\n{pkg_lines}\n\n"
        f"🏦 Bank: {client['bank_name']}\n"
        f"💳 Account Number: `{client['account_number']}`\n"
        f"👤 Account Name: {client['account_name']}\n\n"
        "📸 Make payment to the account above and send your *receipt (screenshot/PDF)* here."
    )
    if client.get("flyer_file_id"):
        await update.message.reply_photo(photo=client["flyer_file_id"], caption=caption, parse_mode="Markdown")
    else:
        await update.message.reply_text(caption, parse_mode="Markdown")

    CLIENT_STATE[uid] = {**state, "step": None}  # Done with ref step; waiting for receipt


# ── /pay ──────────────────────────────────────────────────────────────────────
async def cmd_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    await context.bot.send_chat_action(uid, "typing")

    if uid in ADMIN_IDS:
        await update.message.reply_text("👑 You're a platform admin. No payment needed!")
        return

    if is_client(uid):
        update_last_seen(uid)
        caption = (
            f"💳 *Renew Your SubPlanBot Subscription*\n\n"
            f"💰 Amount: ₦5,000/month\n\n"
            f"🏦 Bank: {PLATFORM_BANK_NAME}\n"
            f"💳 Account Number: `{PLATFORM_ACCT_NUMBER}`\n"
            f"👤 Account Name: {PLATFORM_ACCT_NAME}\n\n"
            "📸 Make payment and send your receipt here.\n"
            "Our team will extend your subscription once confirmed ✅"
        )
        client = get_client(uid)
        if client and client.get("flyer_file_id"):
            await update.message.reply_photo(photo=client["flyer_file_id"], caption=caption, parse_mode="Markdown")
        else:
            await update.message.reply_text(caption, parse_mode="Markdown")
        return

    # Check if they are a member linked to a channel
    member_rows = db.table("members").select("client_id").eq("user_id", uid).eq("removed", False).execute().data
    if not member_rows:
        await update.message.reply_text(
            "⚠️ You don't seem to be linked to a channel yet.\n\n"
            "Use /start and select *Subscribe to a Channel* to get started.",
            parse_mode="Markdown")
        return

    # Let them pick which channel to pay for (in case of multiple)
    if len(member_rows) == 1:
        client_id = member_rows[0]["client_id"]
    else:
        # Show selector — for now just pick first
        client_id = member_rows[0]["client_id"]

    client = get_client(client_id)
    if not client:
        await update.message.reply_text("⚠️ Couldn't find payment details. Contact your admin.")
        return

    caption = (
        f"💳 *Payment Details — {client['brand_name']}*\n\n"
        f"🏦 Bank: {client['bank_name']}\n"
        f"💳 Account Number: `{client['account_number']}`\n"
        f"👤 Account Name: {client['account_name']}\n\n"
        "📸 Send your receipt here after payment ✅"
    )
    if client.get("flyer_file_id"):
        await update.message.reply_photo(photo=client["flyer_file_id"], caption=caption, parse_mode="Markdown")
    else:
        await update.message.reply_text(caption, parse_mode="Markdown")


# ── /renew ─────────────────────────────────────────────────────────────────────
async def cmd_renew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_client(uid):
        update_last_seen(uid)
    await update.message.reply_text(
        "🔄 To renew, make your payment and send the receipt here.\n\n"
        "Need payment details? Use /pay 💳",
        parse_mode="Markdown")


# ── Receipt handler ────────────────────────────────────────────────────────────
async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid  = user.id
    name = f"@{user.username}" if user.username else user.first_name

    await context.bot.send_chat_action(uid, "typing")

    state = CLIENT_STATE.get(uid, {})
    is_onboarding_payment = state.get("awaiting_payment", False)

    if is_onboarding_payment or is_client(uid):
        await update.message.reply_text(
            "✅ *Receipt received!* 📩\n\nOur team will review shortly. You'll hear back once confirmed.",
            parse_mode="Markdown")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"client_approve:{uid}:{name}"),
            InlineKeyboardButton("❌ Deny",    callback_data=f"client_deny:{uid}:{name}")
        ]])
        for admin in ADMIN_IDS:
            await context.bot.forward_message(admin, update.effective_chat.id, update.message.message_id)
            await context.bot.send_message(admin,
                f"💳 *Client payment receipt*\n\n"
                f"👤 {name} wants to {'activate' if is_onboarding_payment else 'renew'} their SubPlanBot subscription.",
                parse_mode="Markdown",
                reply_markup=keyboard)
        return

    # Member sending receipt for a channel subscription
    client_id = state.get("subscribing_to_client_id")
    if not client_id:
        member_rows = db.table("members").select("client_id").eq("user_id", uid).execute().data
        if member_rows:
            client_id = member_rows[0]["client_id"]

    if not client_id:
        await update.message.reply_text(
            "✅ Receipt received! The channel admin will review and grant you access.",
            parse_mode="Markdown")
        return

    client = get_client(client_id)
    if not client or not is_active_client(client_id):
        await update.message.reply_text(
            "⚠️ This channel's subscription is currently inactive. Please contact the admin.",
            parse_mode="Markdown")
        return

    await update.message.reply_text(
        "✅ *Receipt received!* 📩\n\nThe admin will review and send your invite link shortly.",
        parse_mode="Markdown")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"member_approve:{uid}:{name}:{client_id}"),
        InlineKeyboardButton("❌ Deny",    callback_data=f"member_deny:{uid}:{name}:{client_id}")
    ]])
    await context.bot.forward_message(client_id, update.effective_chat.id, update.message.message_id)
    await context.bot.send_message(client_id,
        f"💳 *New Payment Receipt!*\n\n👤 {name} just sent a payment receipt for *{client['brand_name']}*.",
        parse_mode="Markdown",
        reply_markup=keyboard)


# ── Client approves a member ───────────────────────────────────────────────────
async def callback_member_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query     = update.callback_query
    await query.answer()
    parts     = query.data.split(":")
    user_id   = int(parts[1])
    name      = parts[2]
    client_id = int(parts[3])

    if query.from_user.id != client_id and query.from_user.id not in ADMIN_IDS:
        return

    await context.bot.send_chat_action(query.from_user.id, "typing")

    client = get_client(client_id)
    if not client:
        await query.edit_message_text("⚠️ Client not found.")
        return

    packages = [p for p in (client.get("packages") or [])]
    if not packages:
        await query.edit_message_text("⚠️ You have no packages set up. Use /settings to add packages.")
        return

    keyboard = [[InlineKeyboardButton(
        f"{p['name']} — {'Free' if p.get('is_demo') else '₦'+p['price']} ({'5 mins' if p.get('is_demo') else str(p['duration_days'])+'d'})",
        callback_data=f"member_pkg:{user_id}:{i}:{client_id}:{name}"
    )] for i, p in enumerate(packages)]
    await query.edit_message_text(f"📦 Select package for {name}:", reply_markup=InlineKeyboardMarkup(keyboard))


async def callback_member_pkg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query     = update.callback_query
    await query.answer()
    parts     = query.data.split(":")
    user_id   = int(parts[1])
    pkg_idx   = int(parts[2])
    client_id = int(parts[3])
    name      = parts[4]

    await context.bot.send_chat_action(query.from_user.id, "typing")

    client = get_client(client_id)
    pkg    = client["packages"][pkg_idx]
    now    = datetime.now(timezone.utc)

    # Demo package = 5 minutes
    if pkg.get("is_demo"):
        delta  = timedelta(minutes=5)
    else:
        delta  = timedelta(days=pkg["duration_days"])

    expiry = now + delta

    existing = db.table("members").select("expiry").eq("user_id", user_id).eq("client_id", client_id).execute().data
    if existing:
        base   = datetime.fromisoformat(existing[0]["expiry"])
        expiry = (base if base > now else now) + delta
        db.table("members").update({
            "expiry": expiry.isoformat(), "package": pkg["name"], "removed": False, "username": name
        }).eq("user_id", user_id).eq("client_id", client_id).execute()
    else:
        db.table("members").insert({
            "user_id":   user_id,
            "client_id": client_id,
            "username":  name,
            "package":   pkg["name"],
            "expiry":    expiry.isoformat(),
            "added_at":  now.isoformat(),
            "removed":   False
        }).execute()

    try:
        link = (await context.bot.create_chat_invite_link(
            client["channel_id"], member_limit=1, name=f"user_{user_id}"
        )).invite_link

        if pkg.get("is_demo"):
            expiry_str = "5 minutes from now (Demo)"
        else:
            expiry_str = expiry.strftime('%Y-%m-%d %H:%M UTC')

        await context.bot.send_message(user_id,
            f"🎉 *Payment Approved!*\n\n"
            f"📦 Package: *{pkg['name']}*\n"
            f"⏳ Expires: `{expiry_str}`\n\n"
            f"👇 Tap below to join the channel:\n{link}\n\n"
            f"Welcome aboard! 🙌",
            parse_mode="Markdown")
        await query.edit_message_text(f"✅ {name} approved on *{pkg['name']}*. Invite sent! 🎉", parse_mode="Markdown")
    except Exception as e:
        await query.edit_message_text(f"✅ Saved but couldn't DM {name}:\n`{e}`", parse_mode="Markdown")


async def callback_member_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query     = update.callback_query
    await query.answer()
    parts     = query.data.split(":")
    user_id   = int(parts[1])
    name      = parts[2]
    client_id = int(parts[3])

    if query.from_user.id != client_id and query.from_user.id not in ADMIN_IDS:
        return

    ADMIN_STATE[query.from_user.id] = {"action": "member_deny", "user_id": user_id, "username": name, "client_id": client_id}
    await query.edit_message_text(f"✏️ Type your reason for denying {name}:")


# ── Platform admin approves a client ──────────────────────────────────────────
async def callback_client_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return

    await context.bot.send_chat_action(query.from_user.id, "typing")

    parts     = query.data.split(":")
    client_id = int(parts[1])
    name      = parts[2]
    now       = datetime.now(timezone.utc)
    delta     = timedelta(days=30)
    existing  = get_client(client_id)
    state     = CLIENT_STATE.get(client_id, {})

    if existing:
        base   = datetime.fromisoformat(existing["expiry"])
        expiry = (base if base > now else now) + delta
        db.table("clients").update({
            "expiry": expiry.isoformat(), "plan": "paid", "removed": False
        }).eq("client_id", client_id).execute()
        ref_code = existing.get("ref_code") or generate_ref_code(existing.get("brand_name", "SPB"))
        if not existing.get("ref_code"):
            db.table("clients").update({"ref_code": ref_code}).eq("client_id", client_id).execute()
    else:
        expiry   = now + delta
        ref_code = generate_ref_code(state.get("brand_name", "SPB"))
        _save_new_client(client_id, name.lstrip("@"), state, plan="paid", expiry=expiry, ref_code=ref_code)
        CLIENT_STATE.pop(client_id, None)

    await context.bot.send_message(client_id,
        f"🎉 *Payment Approved!*\n\n"
        f"✅ Your SubPlanBot subscription is active until `{expiry.strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
        f"🔑 *Your Ref Code:* `{ref_code}`\n"
        f"_Share this with your members so they can subscribe through the bot._\n\n"
        f"Use /start to access your dashboard 🚀",
        parse_mode="Markdown")
    await query.edit_message_text(
        f"✅ Client {name} approved. Subscription extended to {expiry.strftime('%Y-%m-%d')}.",
        parse_mode="Markdown")


async def callback_client_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return

    parts     = query.data.split(":")
    client_id = int(parts[1])
    name      = parts[2]

    ADMIN_STATE[query.from_user.id] = {"action": "client_deny", "user_id": client_id, "username": name}
    await query.edit_message_text(f"✏️ Type your reason for denying {name}:")


# ── Text handler ───────────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id

    # Channel verification check
    if await process_channel_verification(update, context, uid):
        return

    text  = update.message.text or ""
    state = CLIENT_STATE.get(uid, {})

    # Admin deny reason inputs
    if uid in ADMIN_STATE and ADMIN_STATE[uid].get("action") == "member_deny":
        s       = ADMIN_STATE.pop(uid)
        user_id = s["user_id"]
        name    = s["username"]
        try:
            await context.bot.send_message(user_id,
                f"❌ *Payment Not Approved*\n\nReason: _{text}_\n\nContact your channel admin for help.",
                parse_mode="Markdown")
            await update.message.reply_text(f"✅ Reason sent to {name}.")
        except Exception:
            await update.message.reply_text(f"⚠️ Couldn't DM {name}.")
        return

    if uid in ADMIN_STATE and ADMIN_STATE[uid].get("action") == "client_deny":
        s         = ADMIN_STATE.pop(uid)
        client_id = s["user_id"]
        name      = s["username"]
        try:
            await context.bot.send_message(client_id,
                f"❌ *Payment Not Approved*\n\nReason: _{text}_\n\nContact @SubPlanBotSupport for help.",
                parse_mode="Markdown")
            await update.message.reply_text(f"✅ Reason sent to {name}.")
        except Exception:
            await update.message.reply_text(f"⚠️ Couldn't DM {name}.")
        return

    # Ref code input
    if state.get("step") == STEP_REFCODE:
        await handle_refcode_input(update, context, uid)
        return

    # Onboarding steps
    if uid in CLIENT_STATE:
        step = state.get("step")
        if step == STEP_CHANNEL:
            await update.message.reply_text(
                "📌 Please either *forward a message* from your channel, or type your public *@username*.",
                parse_mode="Markdown")
            return
        if step in [STEP_BRAND, STEP_BANK, STEP_ACCT_NUM, STEP_ACCT_NAME, STEP_PACKAGES]:
            await handle_onboarding(update, context, uid)
            return

    # Active client settings editing
    if is_active_client(uid) and uid in CLIENT_STATE:
        step = state.get("step")
        update_last_seen(uid)

        if step == SETTINGS_EDIT_BRAND:
            db.table("clients").update({"brand_name": text.strip()}).eq("client_id", uid).execute()
            CLIENT_STATE.pop(uid)
            await update.message.reply_text(f"✅ Brand name updated to: *{text.strip()}*", parse_mode="Markdown")
            return
        elif step == SETTINGS_EDIT_BANK:
            db.table("clients").update({"bank_name": text.strip()}).eq("client_id", uid).execute()
            CLIENT_STATE.pop(uid)
            await update.message.reply_text("✅ *Bank name updated.*", parse_mode="Markdown")
            return
        elif step == SETTINGS_EDIT_ACCT_NUM:
            db.table("clients").update({"account_number": text.strip()}).eq("client_id", uid).execute()
            CLIENT_STATE.pop(uid)
            await update.message.reply_text("✅ *Account number updated.*", parse_mode="Markdown")
            return
        elif step == SETTINGS_EDIT_ACCT_NAME:
            db.table("clients").update({"account_name": text.strip()}).eq("client_id", uid).execute()
            CLIENT_STATE.pop(uid)
            await update.message.reply_text("✅ *Account name updated.*", parse_mode="Markdown")
            return
        elif step == SETTINGS_ADD_PKG:
            state["new_pkg_name"] = text.strip()
            state["step"]         = SETTINGS_ADD_PKG_PRICE
            CLIENT_STATE[uid]     = state
            await update.message.reply_text("💰 What's the price for this package? _(e.g. 5000)_", parse_mode="Markdown")
            return
        elif step == SETTINGS_ADD_PKG_PRICE:
            state["new_pkg_price"] = text.strip()
            state["step"]          = SETTINGS_ADD_PKG_DUR
            CLIENT_STATE[uid]      = state
            await update.message.reply_text("📅 How many days does this package last? _(e.g. 30)_", parse_mode="Markdown")
            return
        elif step == SETTINGS_ADD_PKG_DUR:
            if not text.strip().isdigit():
                await update.message.reply_text("⚠️ Please enter a number of days (e.g. 30)")
                return
            client   = get_client(uid)
            packages = client.get("packages") or []
            packages.append({
                "name":          state["new_pkg_name"],
                "price":         state["new_pkg_price"],
                "duration_days": int(text.strip())
            })
            db.table("clients").update({"packages": packages}).eq("client_id", uid).execute()
            CLIENT_STATE.pop(uid)
            await update.message.reply_text(
                f"✅ Package *{state['new_pkg_name']}* added! 🎉\n\nUse /settings to manage all packages.",
                parse_mode="Markdown")
            return

    if is_client(uid) and not is_active_client(uid):
        await update.message.reply_text(
            "⚠️ *Your SubPlanBot subscription has expired.*\n\nUse /pay to renew and restore access.",
            parse_mode="Markdown")
        return

    # Default: treat as receipt if they're linked to a channel
    await update.message.reply_text(
        "⚠️ We only accept a *screenshot* or *PDF* as payment proof.\n\n"
        "Go to your bank app, take a screenshot of the successful transaction, and send it here. 📸",
        parse_mode="Markdown")


# ── Photo handler ──────────────────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if await process_channel_verification(update, context, uid):
        return

    state = CLIENT_STATE.get(uid, {})
    step  = state.get("step")

    if step in [STEP_FLYER, SETTINGS_EDIT_FLYER]:
        await handle_onboarding_photo(update, context, uid)
        return

    await handle_receipt(update, context)


# ── Forwarded messages ─────────────────────────────────────────────────────────
async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if await process_channel_verification(update, context, uid):
        return

    if update.message.document:
        await handle_receipt(update, context)
    elif update.message.photo:
        await handle_photo(update, context)
    else:
        await update.message.reply_text("⚠️ Please only forward messages for channel verification.")


# ── /settings ─────────────────────────────────────────────────────────────────
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_active_client(uid):
        await update.message.reply_text("⚠️ No active subscription. Use /pay to get started.")
        return
    update_last_seen(uid)
    client   = get_client(uid)
    pkg_list = "\n".join([f"• {p['name']} — {'Free' if p.get('is_demo') else '₦'+p['price']} ({'5 mins' if p.get('is_demo') else str(p['duration_days'])+'d'})" for p in (client.get("packages") or [])])
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Brand Name",      callback_data="settings:brand")],
        [InlineKeyboardButton("🏦 Bank Name",        callback_data="settings:bank")],
        [InlineKeyboardButton("💳 Account Number",   callback_data="settings:acct_num")],
        [InlineKeyboardButton("👤 Account Name",     callback_data="settings:acct_name")],
        [InlineKeyboardButton("🖼️ Payment Flyer",    callback_data="settings:flyer")],
        [InlineKeyboardButton("📦 Add Package",      callback_data="settings:add_pkg")],
        [InlineKeyboardButton("🗑️ Delete a Package", callback_data="settings:del_pkg")],
    ])
    await update.message.reply_text(
        f"⚙️ *Settings — {client['brand_name']}*\n\n"
        f"🏦 Bank: {client['bank_name']}\n"
        f"💳 Account: `{client['account_number']}`\n"
        f"👤 Name: {client['account_name']}\n"
        f"🔑 Ref Code: `{client.get('ref_code', 'N/A')}`\n\n"
        f"📦 *Packages:*\n{pkg_list or 'None set'}\n\n"
        "What would you like to edit? 👇",
        parse_mode="Markdown",
        reply_markup=keyboard)


async def callback_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    action = query.data.split(":")[1]

    if not is_active_client(uid):
        await query.edit_message_text("⚠️ No active subscription.")
        return

    update_last_seen(uid)

    if action == "brand":
        CLIENT_STATE[uid] = {"step": SETTINGS_EDIT_BRAND}
        await query.edit_message_text("✏️ Send your new brand name:")
    elif action == "bank":
        CLIENT_STATE[uid] = {"step": SETTINGS_EDIT_BANK}
        await query.edit_message_text("🏦 Send your new bank name:")
    elif action == "acct_num":
        CLIENT_STATE[uid] = {"step": SETTINGS_EDIT_ACCT_NUM}
        await query.edit_message_text("💳 Send your new account number:")
    elif action == "acct_name":
        CLIENT_STATE[uid] = {"step": SETTINGS_EDIT_ACCT_NAME}
        await query.edit_message_text("👤 Send your new account name:")
    elif action == "flyer":
        CLIENT_STATE[uid] = {"step": SETTINGS_EDIT_FLYER}
        await query.edit_message_text("🖼️ Send your new payment flyer image:")
    elif action == "add_pkg":
        CLIENT_STATE[uid] = {"step": SETTINGS_ADD_PKG}
        await query.edit_message_text("📦 What's the name of the new package? _(e.g. 1 Month)_", parse_mode="Markdown")
    elif action == "del_pkg":
        client   = get_client(uid)
        packages = [p for p in (client.get("packages") or []) if not p.get("is_demo")]
        if not packages:
            await query.edit_message_text("ℹ️ You have no custom packages to delete.")
            return
        # Rebuild indices against full list for safe deletion
        full_packages = client.get("packages") or []
        keyboard = [[InlineKeyboardButton(
            f"🗑️ {p['name']} — ₦{p['price']}", callback_data=f"del_pkg:{uid}:{full_packages.index(p)}"
        )] for p in packages]
        await query.edit_message_text("Which package do you want to delete? 🗑️",
                                      reply_markup=InlineKeyboardMarkup(keyboard))


async def callback_del_pkg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    parts   = query.data.split(":")
    uid     = int(parts[1])
    pkg_idx = int(parts[2])

    if query.from_user.id != uid:
        return

    client   = get_client(uid)
    packages = client.get("packages") or []
    if pkg_idx >= len(packages):
        await query.edit_message_text("⚠️ Package not found.")
        return

    removed_pkg = packages.pop(pkg_idx)
    db.table("clients").update({"packages": packages}).eq("client_id", uid).execute()
    await query.edit_message_text(
        f"✅ Package *{removed_pkg['name']}* deleted.\n\n"
        "Existing members on this package will still be managed until their expiry. ✔️",
        parse_mode="Markdown")


# ── /list ─────────────────────────────────────────────────────────────────────
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if uid in ADMIN_IDS:
        await cmd_clientlist(update, context)
        return

    if not is_active_client(uid):
        await update.message.reply_text("⚠️ No active subscription. Use /pay to get started.")
        return

    await context.bot.send_chat_action(uid, "typing")
    update_last_seen(uid)
    now     = datetime.now(timezone.utc).isoformat()
    members = db.table("members").select("user_id, username, expiry, package").eq("client_id", uid).gt("expiry", now).eq("removed", False).order("expiry").execute().data

    if not members:
        await update.message.reply_text("📭 No active members yet.")
        return

    lines = ["👥 *Your Active Members:*\n"]
    for m in members:
        days_left = (datetime.fromisoformat(m["expiry"]) - datetime.now(timezone.utc)).days
        lines.append(f"• {m['username'] or m['user_id']} — {m.get('package','?')} ({days_left}d left)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /remove ───────────────────────────────────────────────────────────────────
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if not is_active_client(uid) and uid not in ADMIN_IDS:
        await update.message.reply_text("⚠️ No active subscription.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /remove [user_id]")
        return

    await context.bot.send_chat_action(uid, "typing")

    target_id  = int(context.args[0])
    client     = get_client(uid) if uid not in ADMIN_IDS else None
    channel_id = client["channel_id"] if client else None
    if not channel_id:
        await update.message.reply_text("⚠️ No channel linked.")
        return

    await context.bot.ban_chat_member(channel_id, target_id)
    await context.bot.unban_chat_member(channel_id, target_id)
    db.table("members").update({"removed": True}).eq("user_id", target_id).eq("client_id", uid).execute()
    await update.message.reply_text(f"✅ `{target_id}` removed from your channel.", parse_mode="Markdown")
    if uid not in ADMIN_IDS:
        update_last_seen(uid)


# ── Platform admin commands ────────────────────────────────────────────────────
async def cmd_clientlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await context.bot.send_chat_action(update.effective_user.id, "typing")
    now     = datetime.now(timezone.utc).isoformat()
    clients = db.table("clients").select("client_id, username, expiry, plan, brand_name, ref_code").gt("expiry", now).eq("removed", False).order("expiry").execute().data

    if not clients:
        await update.message.reply_text("📭 No active clients.")
        return

    lines = ["👥 *Active Clients:*\n"]
    for c in clients:
        days_left = (datetime.fromisoformat(c["expiry"]) - datetime.now(timezone.utc)).days
        lines.append(f"• {c['username']} — {c['brand_name']} ({c['plan']}, {days_left}d left) | 🔑 `{c.get('ref_code','N/A')}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_removeclient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS or not context.args:
        return
    client_id = int(context.args[0])
    db.table("clients").update({"removed": True}).eq("client_id", client_id).execute()
    await update.message.reply_text(f"✅ Client `{client_id}` removed.", parse_mode="Markdown")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    bot = Bot(token=BOT_TOKEN)
    _kick_expired_members_sync(bot)
    _kick_expired_clients_sync(bot)
    await update.message.reply_text("✅ Expiry check done for members and clients.")


async def cmd_getfileid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text("📸 Send me a photo or video and I'll return its file ID.")


# ── Boot ───────────────────────────────────────────────────────────────────────
def run_flask():
    port = int(os.getenv("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("pay",          cmd_pay))
    app.add_handler(CommandHandler("renew",        cmd_renew))
    app.add_handler(CommandHandler("list",         cmd_list))
    app.add_handler(CommandHandler("remove",       cmd_remove))
    app.add_handler(CommandHandler("settings",     cmd_settings))
    app.add_handler(CommandHandler("check",        cmd_check))
    app.add_handler(CommandHandler("clientlist",   cmd_clientlist))
    app.add_handler(CommandHandler("removeclient", cmd_removeclient))
    app.add_handler(CommandHandler("getfileid",    cmd_getfileid))

    app.add_handler(MessageHandler(
        filters.FORWARDED & filters.ChatType.PRIVATE,
        handle_forward))

    app.add_handler(MessageHandler(
        filters.PHOTO & filters.ChatType.PRIVATE,
        handle_photo))

    app.add_handler(MessageHandler(
        filters.Document.PDF & filters.ChatType.PRIVATE,
        handle_receipt))

    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_text))

    app.add_handler(ChatMemberHandler(handle_bot_added, chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER))

    app.add_handler(CallbackQueryHandler(callback_welcome,        pattern=r"^welcome:"))
    app.add_handler(CallbackQueryHandler(callback_onboard,        pattern=r"^onboard:"))
    app.add_handler(CallbackQueryHandler(callback_member_approve, pattern=r"^member_approve:"))
    app.add_handler(CallbackQueryHandler(callback_member_pkg,     pattern=r"^member_pkg:"))
    app.add_handler(CallbackQueryHandler(callback_member_deny,    pattern=r"^member_deny:"))
    app.add_handler(CallbackQueryHandler(callback_client_approve, pattern=r"^client_approve:"))
    app.add_handler(CallbackQueryHandler(callback_client_deny,    pattern=r"^client_deny:"))
    app.add_handler(CallbackQueryHandler(callback_settings,       pattern=r"^settings:"))
    app.add_handler(CallbackQueryHandler(callback_del_pkg,        pattern=r"^del_pkg:"))

    print("🤖 SubPlanBot polling...")
    app.run_polling()

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()