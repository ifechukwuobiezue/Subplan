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
BOT_USERNAME       = os.getenv("BOT_USERNAME", "subplanhubbot")
ADMIN_IDS          = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
WELCOME_FILE_ID    = os.getenv("WELCOME_FILE_ID")
ONBOARDING_FILE_ID = os.getenv("ONBOARDING_FILE_ID")
FLYER_FILE_ID      = os.getenv("FLYER_FILE_ID")
CRON_SECRET        = os.getenv("CRON_SECRET", "change-me-secret")
db                 = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

logging.basicConfig(format="%(asctime)s %(message)s", level=logging.INFO)

# ── Platform payment account ───────────────────────────────────────────────────
PLATFORM_BANK_NAME   = "Kuda Bank"
PLATFORM_ACCT_NUMBER = "2003661688"
PLATFORM_ACCT_NAME   = "Paul-Mary Chukwuka Omile"
PLATFORM_PRICE       = "3,000"

# ── In-memory state ────────────────────────────────────────────────────────────
CLIENT_STATE      = {}
ADMIN_STATE       = {}
PENDING_APPROVALS = {}  # { user_id: { "client_id": ..., "pkg": ..., "name": ... } }

# Onboarding steps
STEP_BRAND        = "brand"
STEP_BRAND_LOGO   = "brand_logo"
STEP_BANK         = "bank"
STEP_ACCT_NUM     = "acct_num"
STEP_ACCT_NUM2    = "acct_num2"
STEP_ACCT_NAME    = "acct_name"
STEP_FLYER        = "flyer"
STEP_PACKAGES     = "packages"
STEP_REF_CODE     = "ref_code_choice"
STEP_CHANNEL      = "channel"

# Member step
STEP_REFCODE_INPUT = "refcode_input"

# Settings sub-steps
SETTINGS_EDIT_BRAND      = "edit_brand"
SETTINGS_EDIT_BRAND_LOGO = "edit_brand_logo"
SETTINGS_EDIT_BANK       = "edit_bank"
SETTINGS_EDIT_ACCT_NUM   = "edit_acct_num"
SETTINGS_EDIT_ACCT_NUM2  = "edit_acct_num2"
SETTINGS_EDIT_ACCT_NAME  = "edit_acct_name"
SETTINGS_EDIT_FLYER      = "edit_flyer"
SETTINGS_ADD_PKG         = "add_pkg_name"
SETTINGS_ADD_PKG_PRICE   = "add_pkg_price"
SETTINGS_ADD_PKG_DUR     = "add_pkg_dur"

# ── Flask ──────────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return "SubPlanBot is alive ✅", 200

@flask_app.route("/cron/kick-members", methods=["GET", "POST"])
def cron_kick_members():
    if flask_request.args.get("secret") != CRON_SECRET:
        return "Unauthorized", 401
    _kick_expired_members_sync()
    return "Member kick done", 200

@flask_app.route("/cron/kick-clients", methods=["GET", "POST"])
def cron_kick_clients():
    if flask_request.args.get("secret") != CRON_SECRET:
        return "Unauthorized", 401
    _kick_expired_clients_sync()
    return "Client kick done", 200

@flask_app.route("/cron/remind-members", methods=["GET", "POST"])
def cron_remind_members():
    if flask_request.args.get("secret") != CRON_SECRET:
        return "Unauthorized", 401
    _remind_members_sync()
    return "Member reminders done", 200

@flask_app.route("/cron/remind-clients", methods=["GET", "POST"])
def cron_remind_clients():
    if flask_request.args.get("secret") != CRON_SECRET:
        return "Unauthorized", 401
    _remind_clients_sync()
    return "Client reminders done", 200

@flask_app.route("/cron/inactivity", methods=["GET", "POST"])
def cron_inactivity():
    if flask_request.args.get("secret") != CRON_SECRET:
        return "Unauthorized", 401
    _inactivity_sync()
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
    return datetime.fromisoformat(c["expiry"]) > datetime.now(timezone.utc)

def update_last_seen(user_id: int):
    db.table("clients").update({"last_seen": datetime.now(timezone.utc).isoformat()}).eq("client_id", user_id).execute()

def generate_ref_code(brand_name: str) -> str:
    prefix = "".join(c for c in brand_name.upper() if c.isalpha())[:4]
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    code   = f"{prefix}-{suffix}"
    while db.table("clients").select("ref_code").eq("ref_code", code).execute().data:
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        code   = f"{prefix}-{suffix}"
    return code

def ref_deep_link(ref_code: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start={ref_code}"

def format_duration(days: int) -> str:
    if days == 0:
        return "5 mins"
    if days % 365 == 0:
        years = days // 365
        return f"{years} year{'s' if years > 1 else ''}"
    if days % 30 == 0:
        months = days // 30
        return f"{months} month{'s' if months > 1 else ''}"
    if days % 7 == 0:
        weeks = days // 7
        return f"{weeks} week{'s' if weeks > 1 else ''}"
    return f"{days} day{'s' if days > 1 else ''}"

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

def is_valid_account_number(value: str) -> bool:
    return value.isdigit() and len(value) == 10

async def send_media_or_text(bot_or_ctx, chat_id: int, file_id, caption: str,
                              reply_markup=None, parse_mode="Markdown"):
    kwargs = dict(parse_mode=parse_mode, reply_markup=reply_markup)
    if file_id:
        try:
            await bot_or_ctx.send_photo(chat_id, photo=file_id, caption=caption, **kwargs)
            return
        except Exception:
            pass
    await bot_or_ctx.send_message(chat_id, caption, **kwargs)

async def send_member_welcome(bot, user_id: int, client: dict):
    brand   = client.get("brand_name", "the channel/group")
    logo_id = client.get("brand_logo_id")
    admin_u = client.get("username", "the admin")
    text = (
        f"👋 *Welcome to {brand}!*\n\n"
        "Here are your quick action commands:\n\n"
        "🔷 /pay — see pricing and payment details\n"
        "🔷 /renew — extend your access\n"
        "🔷 /status — view your current plan and expiry\n\n"
        f"❓ Got questions? Reach out to {admin_u}"
    )
    if logo_id:
        try:
            await bot.send_photo(user_id, photo=logo_id, caption=text, parse_mode="Markdown")
            return
        except Exception:
            pass
    await bot.send_message(user_id, text, parse_mode="Markdown")

async def _send_member_payment_info(message, client: dict):
    """Send clean payment details to a member — mirrors the old single-client bot format."""
    admin_u   = client.get("username", "the admin")
    packages  = [p for p in (client.get("packages") or []) if not p.get("is_demo")]
    pkg_lines = "\n".join([f"• *{p['name']}* — ₦{p['price']} ({format_duration(p['duration_days'])})" for p in packages])

    caption = (
        f"💳 *Payment Details — {client['brand_name']}*\n\n"
        f"📦 *Packages:*\n{pkg_lines}\n\n"
        f"🏦 Bank: {client['bank_name']}\n"
        f"💰 Account Number: `{client['account_number']}`\n"
        f"👤 Account Name: {client['account_name']}\n\n"
        "After payment kindly send your receipt here.\n\n"
        f"_For non-Nigerians, kindly DM {admin_u} for a different payment method._"
    )
    if client.get("flyer_file_id"):
        await message.reply_photo(photo=client["flyer_file_id"], caption=caption, parse_mode="Markdown")
    else:
        await message.reply_text(caption, parse_mode="Markdown")


# ── Cron helpers ───────────────────────────────────────────────────────────────
def _kick_expired_members_sync():
    async def _do():
        async with Bot(token=BOT_TOKEN) as bot:
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
                                "You've been removed from the channel/group.\n\n"
                                "To regain access, renew your subscription via /pay 🙏",
                                parse_mode="Markdown")
                        except Exception:
                            pass
                    except Exception as e:
                        logging.error(f"Member kick failed for {m['user_id']}: {e}")
    asyncio.run(_do())

def _kick_expired_clients_sync():
    async def _do():
        async with Bot(token=BOT_TOKEN) as bot:
            now = datetime.now(timezone.utc).isoformat()
            expired = db.table("clients").select("client_id, username").lte("expiry", now).eq("removed", False).execute().data
            for c in expired:
                try:
                    db.table("clients").update({"removed": True}).eq("client_id", c["client_id"]).execute()
                    try:
                        await bot.send_message(c["client_id"],
                            "🚪 *Your SubPlanBot subscription has expired.*\n\n"
                            "Your channel/group is no longer being managed.\n\n"
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

def _remind_members_sync():
    async def _do():
        # This handles the connection lifecycle correctly
        async with Bot(token=BOT_TOKEN) as bot:
            now = datetime.now(timezone.utc)
            for days in [7, 3, 1]:
                # ... [The Window-Check logic we discussed] ...
                target_time = now + timedelta(days=days)
                start = (target_time - timedelta(hours=1)).isoformat()
                end   = (target_time + timedelta(hours=1)).isoformat()
                
                members = db.table("members").select("user_id, expiry").gte("expiry", start).lte("expiry", end).eq("removed", False).execute().data
                
                for m in members:
                    try:
                        await bot.send_message(m["user_id"], "⏰ Your sub expires soon. /pay", parse_mode="Markdown")
                    except Exception as e:
                        logging.error(f"Reminder failed: {e}")
    asyncio.run(_do())

def _remind_clients_sync():
    async def _do():
        async with Bot(token=BOT_TOKEN) as bot:
            now = datetime.now(timezone.utc)
            for days in [3, 1]:
                start   = (now + timedelta(days=days, hours=-1)).isoformat()
                end     = (now + timedelta(days=days, hours=1)).isoformat()
                clients = db.table("clients").select("client_id, expiry").gte("expiry", start).lte("expiry", end).eq("removed", False).execute().data
                for c in clients:
                    expiry = datetime.fromisoformat(c["expiry"]).strftime("%b %d, %Y")
                    try:
                        await bot.send_message(c["client_id"],
                            f"⏰ *Heads up!* Your SubPlanBot subscription expires in *{days} day{'s' if days > 1 else ''}* ({expiry}).\n\n"
                            "Renew now → /pay to keep your channel/group running 🙏",
                            parse_mode="Markdown")
                    except Exception:
                        pass
            start_1h = (now + timedelta(hours=1, minutes=-10)).isoformat()
            end_1h   = (now + timedelta(hours=1, minutes=10)).isoformat()
            for c in db.table("clients").select("client_id").gte("expiry", start_1h).lte("expiry", end_1h).eq("removed", False).execute().data:
                try:
                    await bot.send_message(c["client_id"],
                        "🚨 *1 hour left!* Your SubPlanBot subscription expires very soon.\n\n"
                        "Renew immediately → /pay to avoid interruption ⚡",
                        parse_mode="Markdown")
                except Exception:
                    pass
    asyncio.run(_do())

def _inactivity_sync():
    async def _do():
        async with Bot(token=BOT_TOKEN) as bot:
            threshold = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            inactive  = db.table("clients").select("client_id").lt("last_seen", threshold).eq("removed", False).execute().data
            for c in inactive:
                try:
                    await bot.send_message(c["client_id"],
                        "👋 *Hey! We miss you.*\n\n"
                        "We noticed you haven't used SubPlanBot in a while.\n\n"
                        "Your channel/group management is still running — just wanted to check in! "
                        "Type /start if you need anything 😊",
                        parse_mode="Markdown")
                except Exception:
                    pass
    asyncio.run(_do())


# ── Channel/group keyboard ─────────────────────────────────────────────────────
def _channel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Add me to your channel",
            url=f"https://t.me/{BOT_USERNAME}?startchannel=setup&admin=invite_users+restrict_members+manage_chat")],
        [InlineKeyboardButton("👥 Add me to your group",
            url=f"https://t.me/{BOT_USERNAME}?startgroup=setup&admin=invite_users+restrict_members+manage_chat")],
        [InlineKeyboardButton("✅ I've added the bot", callback_data="onboard:confirm_channel")]
    ])


# ── Channel/group verification helper ─────────────────────────────────────────
async def process_channel_verification(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    state = CLIENT_STATE.get(uid, {})
    if state.get("step") != STEP_CHANNEL:
        return False

    channel_identifier = None
    chat_title = None

    # 1. Handling Channels (Forwarding)
    # Check for forwarded channel messages specifically
    if getattr(update.message, "forward_from_chat", None) and update.message.forward_from_chat.type == "channel":
        channel_identifier = update.message.forward_from_chat.id
        chat_title = update.message.forward_from_chat.title
    elif getattr(update.message, "forward_origin", None) and getattr(update.message.forward_origin, "chat", None):
        if getattr(update.message.forward_origin.chat, "type", None) == "channel":
            channel_identifier = update.message.forward_origin.chat.id
            chat_title = update.message.forward_origin.chat.title
        else:
            # If it's a group forward, redirect to the button-based method
            await update.message.reply_text(
                "⚠️ Forwarding is for *channels* only.\n\n"
                "For *groups*, please use the *Add me to your group* button below.",
                parse_mode="Markdown",
                reply_markup=_channel_keyboard())
            return True
    else:
        # If no valid channel forward, prompt for the button-based setup
        await update.message.reply_text(
            "📌 Use the buttons below to add me to your channel or group.\n\n"
            "If you are setting up a *channel*, you can also forward any message from it here.",
            parse_mode="Markdown",
            reply_markup=_channel_keyboard())
        return True

    # 2. Verification for detected channel
    await context.bot.send_chat_action(uid, "typing")
    try:
        bot_member = await context.bot.get_chat_member(channel_identifier, context.bot.id)
        if not isinstance(bot_member, (ChatMemberAdministrator, ChatMemberOwner)):
            await update.message.reply_text("⚠️ I'm not an admin in that channel yet. Please add me and try again.")
            return True
        if isinstance(bot_member, ChatMemberAdministrator) and not bot_member.can_restrict_members:
            await update.message.reply_text("⚠️ I am an admin, but I don't have *Ban Members* permission. Please enable it.")
            return True
            
        state["channel_id"] = channel_identifier
        state["channel_name"] = chat_title
        CLIENT_STATE[uid] = state
        await _show_plan_selection(context.bot, uid, chat_title)
    except Exception as e:
        logging.error(f"Channel verification failed: {e}")
        await update.message.reply_text("⚠️ Verification failed. Please ensure I have admin rights in the channel.")
    return True


async def _show_plan_selection(bot, uid: int, channel_name: str):
    caption = (
        "🚀 *SubPlanBot Plans*\n\n"
        "🧪 *24hrs Test Environment* — test by inviting a secondary account to your channel or group using your invite link.\n"
        f"💳 *Monthly Plan* — ₦{PLATFORM_PRICE}/month\n\n"
        "_Use the Test Environment to see exactly how your members' experience looks before going live!_\n\n"
        "Choose an option below 👇"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🧪 Test Environment", callback_data=f"onboard:trial:{uid}"),
        InlineKeyboardButton("💳 Make Payment",     callback_data=f"onboard:pay:{uid}")
    ]])
    await bot.send_message(uid,
        f"✅ *Channel/group verified:* {channel_name}\n\nI've been added as admin successfully! 🎉",
        parse_mode="Markdown")
    await send_media_or_text(bot, uid, ONBOARDING_FILE_ID, caption, reply_markup=keyboard)


# ── /start ─────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    chat = update.effective_chat
    
    # 1. THE GUARD: Handle all existing/expired clients first
    if is_client(uid):
        if is_active_client(uid):
            update_last_seen(uid)
            client = get_client(uid)
            ref    = client.get("ref_code", "N/A")
            await update.message.reply_text(
                f"👋 *Welcome back, {client.get('brand_name', 'there')}!*\n\n"
                "📋 /list — view active members\n"
                "❌ /remove — remove a member\n"
                "⚙️ /settings — manage account\n"
                "💳 /pay — renew your subscription\n\n"
                f"🔑 *Ref Code:* `{ref}`\n"
                "❓ Got questions? Reach out to @GizmoBrymez",
                parse_mode="Markdown")
        else:
            await update.message.reply_text(
                "⚠️ *Your SubPlanBot subscription has expired.*\n\n"
                "Use /pay to renew and restore your channel/group management.\n\n"
                "❓ Got questions? Reach out to @GizmoBrymez",
                parse_mode="Markdown")
        return

    # 2. GROUP/SUPERGROUP SETUP
    if chat.type in ("group", "supergroup"):
        state = CLIENT_STATE.get(uid, {})
        if state.get("step") == STEP_CHANNEL:
            try:
                bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
                if not isinstance(bot_member, (ChatMemberAdministrator, ChatMemberOwner)):
                    await update.message.reply_text("⚠️ I need to be an admin with *Ban Users* enabled.", parse_mode="Markdown")
                    return
                if isinstance(bot_member, ChatMemberAdministrator) and not bot_member.can_restrict_members:
                    await update.message.reply_text("⚠️ *Ban Users* permission is off. Please enable it and run /start again.", parse_mode="Markdown")
                    return
                state["channel_id"]   = chat.id
                state["channel_name"] = chat.title
                CLIENT_STATE[uid]     = state
                await _show_plan_selection(context.bot, uid, chat.title)
            except Exception as e:
                logging.error(f"Group start detection failed: {e}")
        return

    # 3. NEW USER FLOW
    await context.bot.send_chat_action(uid, "typing")

    if context.args:
        ref_code = context.args[0].upper()
        client   = get_client_by_ref(ref_code)
        if client and is_active_client(client["client_id"]):
            await _show_subscription_info(update, context, uid, client)
            return

    if uid in ADMIN_IDS:
        await update.message.reply_text("👾 *SubPlan Bot Admin Panel*", parse_mode="Markdown")
        return

    welcome_text = "🚀 *Welcome to SubPlan Bot!*\n\nWhat are you here for? 👇"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏢 I'm a Channel/Group Admin", callback_data="welcome:admin")],
        [InlineKeyboardButton("🎟️ Subscribe to a Channel/Group", callback_data="welcome:subscribe")]
    ])
    if WELCOME_FILE_ID:
        try:
            await update.message.reply_photo(photo=WELCOME_FILE_ID, caption=welcome_text, parse_mode="Markdown", reply_markup=keyboard)
            return
        except Exception: pass
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=keyboard)


async def _show_subscription_info(update, context, uid: int, client: dict):
    packages = [p for p in (client.get("packages") or []) if not p.get("is_demo")]
    CLIENT_STATE[uid] = {"subscribing_to_client_id": client["client_id"], "step": None}
    if not packages:
        await update.message.reply_text(
            "⚠️ This channel/group has no active packages yet. Contact the admin.",
            parse_mode="Markdown")
        return
    await send_member_welcome(context.bot, uid, client)



# ── Callback: welcome ──────────────────────────────────────────────────────────
async def callback_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    action = query.data.split(":")[1]

    await context.bot.send_chat_action(uid, "typing")

    if action == "admin":
        if is_active_client(uid):
            update_last_seen(uid)
            client = get_client(uid)
            ref    = client.get("ref_code", "N/A")
            msg    = (
                f"👋 *Welcome back, {client.get('brand_name', 'there')}!*\n\n"
                "📋 /list — view your active members\n"
                "❌ /remove [id] — remove a member\n"
                "⚙️ /settings — manage brand, packages & payment info\n"
                "💳 /pay — renew your SubPlanBot subscription\n\n"
                f"🔑 *Your Ref Code:* `{ref}`\n"
                f"🔗 *Deep Link:* `{ref_deep_link(ref)}`\n\n"
                "❓ Got questions? Reach out to @GizmoBrymez"
            )
            if query.message.photo:
                await query.edit_message_caption(caption=msg, parse_mode="Markdown")
            else:
                await query.edit_message_text(msg, parse_mode="Markdown")
            return

        if is_client(uid):
            msg = (
                "⚠️ *Your SubPlanBot subscription has expired.*\n\n"
                "Use /pay to renew.\n\n"
                "❓ Got questions? Reach out to @GizmoBrymez"
            )
            if query.message.photo:
                await query.edit_message_caption(caption=msg, parse_mode="Markdown")
            else:
                await query.edit_message_text(msg, parse_mode="Markdown")
            return

        CLIENT_STATE[uid] = {"step": STEP_BRAND}
        onboard_text = (
            "😌 *Great! Let's get your channel/group set up.*\n\n"
            "You can change anything later using /settings ⚙️\n\n"
            "First — what's your *brand name*?\n"
            "_(e.g. TechSignals Pro, MoneyMoves Hub)_"
        )
        if query.message.photo:
            await query.edit_message_caption(caption=onboard_text, parse_mode="Markdown")
        else:
            await query.edit_message_text(onboard_text, parse_mode="Markdown")

    elif action == "subscribe":
        CLIENT_STATE[uid] = {"step": STEP_REFCODE_INPUT}
        sub_text = (
            "🎟️ *Subscribe to a Channel/Group*\n\n"
            "Please enter the *Ref Code* shared by your channel/group admin.\n\n"
            "It looks something like: `TECH or TECH-X7K2`\n\n"
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
        state["step"]       = STEP_BRAND_LOGO
        CLIENT_STATE[uid]   = state
        await update.message.reply_text(
            f"✅ *Brand name saved:* {state['brand_name']}\n\n"
            "🖼️ Would you like to add a *brand logo*?\n\n"
            "Send an image now, or tap *Skip* to continue.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭️ Skip", callback_data="onboard:skip_logo")
            ]]))

    elif step == STEP_BANK:
        state["bank_name"] = text.strip()
        state["step"]      = STEP_ACCT_NUM
        CLIENT_STATE[uid]  = state
        await update.message.reply_text(
            "🔢 What's your *account number*?",
            parse_mode="Markdown")

    elif step == STEP_ACCT_NUM:
        val = text.strip()
        if not is_valid_account_number(val):
            await update.message.reply_text(
                "⚠️ Account number must be *exactly 10 digits* and contain only numbers.\n\nPlease try again:",
                parse_mode="Markdown")
            return
        state["account_number_tmp"] = val
        state["step"]               = STEP_ACCT_NUM2
        CLIENT_STATE[uid]           = state
        await update.message.reply_text(
            "🔢 Please *confirm* your account number by entering it again:",
            parse_mode="Markdown")

    elif step == STEP_ACCT_NUM2:
        val = text.strip()
        if not is_valid_account_number(val):
            await update.message.reply_text(
                "⚠️ Account number must be *exactly 10 digits* and contain only numbers.\n\nPlease try again:",
                parse_mode="Markdown")
            return
        if val != state.get("account_number_tmp"):
            await update.message.reply_text(
                "⚠️ Account numbers don't match. Please enter your account number again:",
                parse_mode="Markdown")
            state["step"] = STEP_ACCT_NUM
            CLIENT_STATE[uid] = state
            return
        state["account_number"] = val
        state.pop("account_number_tmp", None)
        state["step"]           = STEP_ACCT_NAME
        CLIENT_STATE[uid]       = state
        await update.message.reply_text("👤 What's the *account name*?", parse_mode="Markdown")

    elif step == STEP_ACCT_NAME:
        state["account_name"] = text.strip()
        state["step"]         = STEP_FLYER
        CLIENT_STATE[uid]     = state
        flyer_msg = (
            "🖼️ *Payment Flyer* _(optional)_\n\n"
            "This is the image your members see when they use /pay.\n\n"
        )
        skip_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⏭️ Skip", callback_data="onboard:skip_flyer")
        ]])
        if FLYER_FILE_ID:
            flyer_msg += "Here's a sample flyer for reference 👆\n\nSend your own flyer image, or tap *Skip*."
            await context.bot.send_photo(
                uid, photo=FLYER_FILE_ID,
                caption=flyer_msg, parse_mode="Markdown",
                reply_markup=skip_kb)
        else:
            flyer_msg += "Send your flyer image now, or tap *Skip*."
            await update.message.reply_text(flyer_msg, parse_mode="Markdown", reply_markup=skip_kb)

    elif step == STEP_PACKAGES:
        parts = [p.strip() for p in text.split(",")]
        if len(parts) != 3:
            await update.message.reply_text(
                "⚠️ Please use this format:\n`Package Name, Price, Duration in days`\n\nExamples:\n`1 Month, 5000, 30`\n`3 Months, 12000, 90`",
                parse_mode="Markdown")
            return
        pkg_name, price, duration = parts[0], parts[1], parts[2]
        if not duration.isdigit():
            await update.message.reply_text(
                "⚠️ Duration must be a number of days. Example: `30`",
                parse_mode="Markdown")
            return
        packages = state.get("packages", [])
        packages.append({"name": pkg_name, "price": price, "duration_days": int(duration)})
        state["packages"] = packages
        CLIENT_STATE[uid] = state
        pkg_list = "\n".join([f"• {p['name']} — ₦{p['price']} / {format_duration(p['duration_days'])}" for p in packages])
        await update.message.reply_text(
            f"✅ *Package added!*\n\n*Your packages so far:*\n{pkg_list}\n\nAdd another or continue?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Add another", callback_data="onboard:add_pkg"),
                InlineKeyboardButton("✅ Done",         callback_data="onboard:pkg_done")
            ]]))

    elif step == STEP_REF_CODE:
        custom = text.strip().upper().replace(" ", "-")
        if len(custom) < 3 or len(custom) > 20:
            await update.message.reply_text(
                "⚠️ Ref code must be between 3 and 20 characters. Try again:")
            return
        if db.table("clients").select("ref_code").eq("ref_code", custom).execute().data:
            await update.message.reply_text(
                "⚠️ That code is already taken. Please try a different one:")
            return
        state["ref_code"] = custom
        state["step"]     = STEP_CHANNEL
        CLIENT_STATE[uid] = state
        await update.message.reply_text(
            f"✅ *Ref Code set:* `{custom}`\n"
            f"🔗 *Deep Link:* `{ref_deep_link(custom)}`\n\n"
            "Now let's link your channel or group! 👇",
            parse_mode="Markdown",
            reply_markup=_channel_keyboard())

    elif step == STEP_CHANNEL:
        await update.message.reply_text(
            "📌 Use the buttons below to add me to your channel or group.\n\n"
            "For *channels* you can also forward any message from your channel here.",
            parse_mode="Markdown",
            reply_markup=_channel_keyboard())


# ── Onboarding photos ──────────────────────────────────────────────────────────
async def handle_onboarding_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    state = CLIENT_STATE.get(uid, {})
    step  = state.get("step")

    if step == STEP_BRAND_LOGO:
        file_id                = update.message.photo[-1].file_id
        state["brand_logo_id"] = file_id
        state["step"]          = STEP_BANK
        CLIENT_STATE[uid]      = state
        await update.message.reply_text(
            "✅ *Brand logo saved!*\n\n"
            "Now let's set up your *payment details* 💼\n\n"
            "What's your *bank name*? _(e.g. Kuda Bank, GTBank)_",
            parse_mode="Markdown")

    elif step == STEP_FLYER:
        file_id                = update.message.photo[-1].file_id
        state["flyer_file_id"] = file_id
        state["step"]          = STEP_PACKAGES
        CLIENT_STATE[uid]      = state
        await update.message.reply_text(
            "✅ *Flyer saved!*\n\n"
            "📦 Now let's set up your *subscription packages*.\n\n"
            "Send each package one after the other in this format:\n"
            "`Package Name, Price, Duration in days`\n\n"
            "We'll automatically convert the duration into months or years where needed — just send it in days.\n\n"
            "Examples:\n"
            "`1 Month, 5000, 30`\n"
            "`Diamond Plan, 12000, 90`\n\n"
            "Enter your first package below 👇",
            parse_mode="Markdown")

    elif step == SETTINGS_EDIT_BRAND_LOGO:
        file_id = update.message.photo[-1].file_id
        db.table("clients").update({"brand_logo_id": file_id}).eq("client_id", uid).execute()
        CLIENT_STATE.pop(uid, None)
        await update.message.reply_photo(
            photo=file_id, caption="✅ *Brand logo updated!*", parse_mode="Markdown")

    elif step == SETTINGS_EDIT_FLYER:
        file_id = update.message.photo[-1].file_id
        db.table("clients").update({"flyer_file_id": file_id}).eq("client_id", uid).execute()
        CLIENT_STATE.pop(uid, None)
        await update.message.reply_photo(
            photo=file_id, caption="✅ *Payment flyer updated!*", parse_mode="Markdown")

    else:
        await handle_receipt(update, context)


# ── Bot added to channel/group ─────────────────────────────────────────────────
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

    if isinstance(new_status, ChatMemberAdministrator) and not new_status.can_restrict_members:
        for uid, state in list(CLIENT_STATE.items()):
            if state.get("step") == STEP_CHANNEL:
                try:
                    await context.bot.send_message(uid,
                        "⚠️ I've been added to your channel/group but I don't have *Ban Members* permission.\n\n"
                        "Please go to admin settings, find me, enable *Ban Members*, then tap *I've added the bot* again.",
                        parse_mode="Markdown",
                        reply_markup=_channel_keyboard())
                except Exception as e:
                    logging.error(f"Could not notify client {uid}: {e}")
                break
        return

    for uid, state in list(CLIENT_STATE.items()):
        if state.get("step") == STEP_CHANNEL:
            state["channel_id"]   = chat.id
            state["channel_name"] = chat.title
            CLIENT_STATE[uid]     = state
            try:
                await _show_plan_selection(context.bot, uid, chat.title)
            except Exception as e:
                logging.error(f"Could not notify client {uid}: {e}")
            break


# ── Member joins channel/group — start subscription timer ─────────────────────
async def handle_member_joined(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if not result:
        return

    new_member = result.new_chat_member
    chat_id    = result.chat.id
    user_id    = new_member.user.id

    if new_member.status not in ("member", "restricted"):
        return
    if user_id == context.bot.id:
        return

    pending = PENDING_APPROVALS.get(user_id)
    if not pending:
        return

    client_id = pending["client_id"]
    pkg       = pending["pkg"]
    name      = pending["name"]

    client = get_client(client_id)
    if not client or client["channel_id"] != chat_id:
        return

    now    = datetime.now(timezone.utc)
    delta  = timedelta(minutes=5) if pkg.get("is_demo") else timedelta(days=pkg["duration_days"])
    expiry = now + delta

    existing = db.table("members").select("expiry").eq("user_id", user_id).eq("client_id", client_id).execute().data
    if existing:
        base   = datetime.fromisoformat(existing[0]["expiry"]) if existing[0]["expiry"] else now
        expiry = (base if base > now else now) + delta
        db.table("members").update({
            "expiry": expiry.isoformat(), "joined_at": now.isoformat(),
            "package": pkg["name"], "removed": False, "username": name
        }).eq("user_id", user_id).eq("client_id", client_id).execute()
    else:
        db.table("members").insert({
            "user_id":   user_id,
            "client_id": client_id,
            "username":  name,
            "package":   pkg["name"],
            "expiry":    expiry.isoformat(),
            "joined_at": now.isoformat(),
            "added_at":  now.isoformat(),
            "removed":   False
        }).execute()

    PENDING_APPROVALS.pop(user_id, None)
    logging.info(f"Member {user_id} joined — expiry set to {expiry.isoformat()}")


# ── Callback: onboarding ───────────────────────────────────────────────────────
async def callback_onboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    state  = CLIENT_STATE.get(uid, {})
    parts  = query.data.split(":")
    action = parts[1]

    await context.bot.send_chat_action(uid, "typing")
    RETRY_KB = _channel_keyboard()

    if action == "skip_logo":
        state["brand_logo_id"] = None
        state["step"]          = STEP_BANK
        CLIENT_STATE[uid]      = state
        try:
            if query.message.photo:
                await query.edit_message_caption(
                    caption="👍 No logo — that's fine!\n\n"
                            "Now let's set up your *payment details* 💼\n\n"
                            "What's your *bank name*? _(e.g. Kuda Bank, GTBank)_",
                    parse_mode="Markdown")
            else:
                await query.edit_message_text(
                    "👍 No logo — that's fine!\n\n"
                    "Now let's set up your *payment details* 💼\n\n"
                    "What's your *bank name*? _(e.g. Kuda Bank, GTBank)_",
                    parse_mode="Markdown")
        except Exception:
            await context.bot.send_message(uid,
                "👍 No logo — that's fine!\n\n"
                "Now let's set up your *payment details* 💼\n\n"
                "What's your *bank name*? _(e.g. Kuda Bank, GTBank)_",
                parse_mode="Markdown")

    elif action == "skip_flyer":
        state["flyer_file_id"] = None
        state["step"]          = STEP_PACKAGES
        CLIENT_STATE[uid]      = state
        pkg_prompt = (
            "👍 No flyer — that's fine!\n\n"
            "📦 Now let's set up your *subscription packages*.\n\n"
            "Send each package one after the other in this format:\n"
            "`Package Name, Price, Duration in days`\n\n"
            "We'll automatically convert the duration into months or years where needed — just send it in days.\n\n"
            "Examples:\n"
            "`1 Month, 5000, 30`\n"
            "`Diamond Plan, 12000, 90`\n\n"
            "Enter your first package below 👇"
        )
        try:
            if query.message.photo:
                await query.edit_message_caption(caption=pkg_prompt, parse_mode="Markdown")
            else:
                await query.edit_message_text(pkg_prompt, parse_mode="Markdown")
        except Exception:
            await context.bot.send_message(uid, pkg_prompt, parse_mode="Markdown")

    elif action == "add_pkg":
        state["step"] = STEP_PACKAGES
        CLIENT_STATE[uid] = state
        await query.edit_message_text(
            "📦 Send the next package:\n`Package Name, Price, Duration in days`",
            parse_mode="Markdown")

    elif action == "pkg_done":
        auto_code         = generate_ref_code(state.get("brand_name", "SPB"))
        state["step"]     = STEP_REF_CODE
        state["auto_ref"] = auto_code
        CLIENT_STATE[uid] = state
        await query.edit_message_text(
            "🔑 *Almost there! Let's set your Ref Code.*\n\n"
            "Your *Ref Code* is what your members use to find and subscribe to your channel/group.\n\n"
            f"We've auto-generated one for you: `{auto_code}`\n\n"
            "Would you like to use this or customize it?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Use this code", callback_data="onboard:use_auto_ref"),
                InlineKeyboardButton("✏️ Customize",    callback_data="onboard:custom_ref")
            ]]))

    elif action == "use_auto_ref":
        auto_code         = state.get("auto_ref", generate_ref_code(state.get("brand_name", "SPB")))
        state["ref_code"] = auto_code
        state["step"]     = STEP_CHANNEL
        CLIENT_STATE[uid] = state
        await query.edit_message_text(
            f"✅ *Ref Code confirmed:* `{auto_code}`\n"
            f"🔗 *Deep Link:* `{ref_deep_link(auto_code)}`\n\n"
            "Now let's link your channel or group! 👇\n\n"
            "Tap the button for your channel or group below.",
            parse_mode="Markdown",
            reply_markup=RETRY_KB)

    elif action == "custom_ref":
        state["step"] = STEP_REF_CODE
        CLIENT_STATE[uid] = state
        await query.edit_message_text(
            "✏️ Type your custom ref code:\n\n"
            "_Letters, numbers and hyphens only. Max 20 chars._\n"
            "Example: `PAUL-VIP` or `MYHUB`",
            parse_mode="Markdown")

    elif action == "confirm_channel":
        state      = CLIENT_STATE.get(uid, {})
        found_id   = state.get("channel_id")
        found_name = state.get("channel_name")
        if not found_id:
            await context.bot.send_message(uid,
                "⚠️ I haven't detected your channel or group yet.\n\n"
                "Use the buttons below to add me, then tap *I've added the bot* once done.",
                parse_mode="Markdown",
                reply_markup=RETRY_KB)
            return
        await context.bot.send_chat_action(uid, "typing")
        try:
            bot_member = await context.bot.get_chat_member(found_id, context.bot.id)
            if not isinstance(bot_member, (ChatMemberAdministrator, ChatMemberOwner)):
                await context.bot.send_message(uid,
                    "⚠️ I'm in the channel/group but not as an admin yet.\n\n"
                    "Please make sure *Admin Rights* is enabled, then try again.",
                    parse_mode="Markdown", reply_markup=RETRY_KB)
                return
            if isinstance(bot_member, ChatMemberAdministrator) and not bot_member.can_restrict_members:
                await context.bot.send_message(uid,
                    "⚠️ I'm an admin but I don't have *Ban Members* permission.\n\n"
                    "Please go to admin settings, find me, enable *Ban Members*, then try again.",
                    parse_mode="Markdown", reply_markup=RETRY_KB)
                return
        except Exception:
            await context.bot.send_message(uid,
                "⚠️ Couldn't verify your channel/group yet.\n\n"
                "Please use the buttons below to add me, then tap *I've added the bot*.",
                parse_mode="Markdown",
                reply_markup=RETRY_KB)
            return
        try:
            await query.edit_message_text(
                f"✅ *Channel/group confirmed:* {found_name}\n\nNow choose how to get started 👇",
                parse_mode="Markdown")
        except Exception:
            pass
        await _show_plan_selection(context.bot, uid, found_name)

    elif action == "trial":
        uid_target = int(parts[2]) if len(parts) > 2 else uid
        state      = CLIENT_STATE.get(uid_target, {})
        ref_code   = state.get("ref_code") or generate_ref_code(state.get("brand_name", "SPB"))
        _save_new_client(uid_target, query.from_user.username, state, plan="test",
                         expiry=datetime.now(timezone.utc) + timedelta(days=1),
                         ref_code=ref_code)
        CLIENT_STATE.pop(uid_target, None)
        sandbox_msg = (
            "🧪 *Test Environment Activated!*\n\n"
            "SubPlanBot will manage your channel/group for *24 hours*.\n\n"
            "Here's how to test the full member experience:\n"
            "1️⃣ Copy your *Ref Code* or *Deep Link* below\n"
            "2️⃣ Open this bot on *another Telegram account*\n"
            "3️⃣ Click *Subscribe to a Channel/Group* (or use the deep link directly)\n"
            "4️⃣ Send a mock payment screenshot — approve it as admin and watch the magic! ✨\n\n"
            f"🔑 *Your Ref Code:* `{ref_code}`\n"
            f"🔗 *Deep Link:* `{ref_deep_link(ref_code)}`\n\n"
            "💡 The test member will be automatically removed 5 minutes after joining."
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
            f"💰 Amount: ₦{PLATFORM_PRICE}/month\n\n"
            f"🏦 Bank: {PLATFORM_BANK_NAME}\n"
            f"💰 Account Number: `{PLATFORM_ACCT_NUMBER}`\n"
            f"👤 Account Name: {PLATFORM_ACCT_NAME}\n\n"
            "📸 Make payment and send your receipt here.\n"
            "Our team will activate your account shortly ✅\n\n"
            "❓ Got questions? Reach out to @GizmoBrymez"
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
    packages = list(state.get("packages", []))
    packages.insert(0, {"name": "5 Min Demo", "price": "0", "duration_days": 0, "duration_minutes": 5, "is_demo": True})
    db.table("clients").insert({
        "client_id":      uid,
        "username":       username_str,
        "brand_name":     state.get("brand_name"),
        "brand_logo_id":  state.get("brand_logo_id"),
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


# ── Ref code input ─────────────────────────────────────────────────────────────
async def handle_refcode_input(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    state    = CLIENT_STATE.get(uid, {})
    ref_code = (update.message.text or "").strip().upper()

    await context.bot.send_chat_action(uid, "typing")

    client = get_client_by_ref(ref_code)
    if not client:
        await update.message.reply_text(
            "❌ *Invalid Ref Code.*\n\n"
            "Please double-check the code from your channel/group admin and try again.\n"
            "_(It looks like: `TECH-X7K2`)_",
            parse_mode="Markdown")
        return

    if not is_active_client(client["client_id"]):
        await update.message.reply_text(
            "⚠️ This channel/group's subscription is currently *inactive*.\n\n"
            "Please contact the admin for assistance.",
            parse_mode="Markdown")
        CLIENT_STATE.pop(uid, None)
        return

    state["subscribing_to_client_id"] = client["client_id"]
    CLIENT_STATE[uid] = {**state, "step": None}

    packages = [p for p in (client.get("packages") or []) if not p.get("is_demo")]
    if not packages:
        await update.message.reply_text(
            "⚠️ This channel/group has no active packages yet. Contact the admin.",
            parse_mode="Markdown")
        CLIENT_STATE.pop(uid, None)
        return

    await send_member_welcome(context.bot, uid, client)


# ── /pay ──────────────────────────────────────────────────────────────────────
async def cmd_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await context.bot.send_chat_action(uid, "typing")

    if uid in ADMIN_IDS:
        await update.message.reply_text("👑 You're a platform admin. No payment needed!")
        return

    # PRIORITIZE CLIENT RENEWAL
    if is_client(uid):
        update_last_seen(uid)
        caption = (
            f"💳 *Renew Your SubPlanBot Subscription*\n\n"
            f"💰 Amount: ₦{PLATFORM_PRICE}/month\n\n"
            f"🏦 Bank: {PLATFORM_BANK_NAME}\n"
            f"💰 Account Number: `{PLATFORM_ACCT_NUMBER}`\n"
            f"👤 Account Name: {PLATFORM_ACCT_NAME}\n\n"
            "📸 Make payment and send your receipt here.\n"
            "Our team will extend your subscription once confirmed ✅"
        )
        await update.message.reply_text(caption, parse_mode="Markdown")
        return

    # EXISTING MEMBER PAYMENT FLOW
    state     = CLIENT_STATE.get(uid, {})
    client_id = state.get("subscribing_to_client_id")
    if not client_id:
        rows = db.table("members").select("client_id").eq("user_id", uid).eq("removed", False).execute().data
        if rows: client_id = rows[0]["client_id"]

    if not client_id:
        await update.message.reply_text("⚠️ You're not linked to a channel/group yet. Use /start.", parse_mode="Markdown")
        return

    client = get_client(client_id)
    if not client:
        await update.message.reply_text("⚠️ Couldn't find payment details. Contact your admin.")
        return

    await _send_member_payment_info(update.message, client)


# ── /renew ─────────────────────────────────────────────────────────────────────
async def cmd_renew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_client(uid):
        update_last_seen(uid)
    await update.message.reply_text(
        "🔄 To renew, make your payment and send the receipt here.\n\n"
        "Need payment details? Use /pay 💳",
        parse_mode="Markdown")


# ── /status ────────────────────────────────────────────────────────────────────
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await context.bot.send_chat_action(uid, "typing")

    # 1. Check if user is a Client
    client = get_client(uid)
    if client and not client.get("removed", False):
        expiry_dt = datetime.fromisoformat(client["expiry"])
        status    = "✅ Active" if expiry_dt > datetime.now(timezone.utc) else "❌ Expired"
        countdown = format_expiry_countdown(client["expiry"])
        
        await update.message.reply_text(
            f"👑 *Your Subscription Status*\n\n"
            f"🔰 Status: {status}\n"
            f"📅 Expires: {expiry_dt.strftime('%b %d, %Y')}\n"
            f"⏳ Time left: {countdown}\n"
            f"🔑 Ref Code: `{client.get('ref_code', 'N/A')}`",
            parse_mode="Markdown")
        return

    # 2. Check if user is a Member
    rows = db.table("members").select("client_id, package, expiry").eq("user_id", uid).eq("removed", False).execute().data
    
    if not rows:
        await update.message.reply_text("ℹ️ You don't have an active subscription.", parse_mode="Markdown")
        return

    lines = ["📊 *Your Subscription Status*\n"]
    for row in rows:
        c_info = get_client(row["client_id"])
        brand  = c_info["brand_name"] if c_info else "Unknown"
        
        expiry_dt = datetime.fromisoformat(row["expiry"]) if row.get("expiry") else None
        if not expiry_dt:
            status = "⏳ Pending"
            expiry_str = "N/A"
            countdown = "N/A"
        else:
            status = "✅ Active" if expiry_dt > datetime.now(timezone.utc) else "❌ Expired"
            expiry_str = expiry_dt.strftime("%b %d, %Y")
            countdown = format_expiry_countdown(row["expiry"])
            
        lines.append(
            f"📢 *{brand}*\n"
            f"📦 Plan: {row.get('package', 'N/A')}\n"
            f"🔰 Status: {status}\n"
            f"📅 Expires: {expiry_str}\n"
            f"⏳ Time left: {countdown}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Receipt handler ────────────────────────────────────────────────────────────
async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid  = user.id
    name = f"@{user.username}" if user.username else user.first_name

    await context.bot.send_chat_action(uid, "typing")

    state                 = CLIENT_STATE.get(uid, {})
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
                parse_mode="Markdown", reply_markup=keyboard)
        return

    client_id = state.get("subscribing_to_client_id")
    if not client_id:
        rows = db.table("members").select("client_id").eq("user_id", uid).execute().data
        if rows:
            client_id = rows[0]["client_id"]

    if not client_id:
        await update.message.reply_text(
            "✅ Receipt received! The Admin will review and grant you access within 2hrs. "
            "If you don't hear back, contact the admin with your payment details.",
            parse_mode="Markdown")
        return

    client = get_client(client_id)
    if not client or not is_active_client(client_id):
        await update.message.reply_text(
            "⚠️ This channel/group's subscription is currently inactive. Please contact the admin.",
            parse_mode="Markdown")
        return

    admin_u = client.get("username", "the admin")

    await update.message.reply_text(
        f"✅ *Receipt received!* 📩\n\n"
        f"The admin ({admin_u}) will review and send your invite link within 2 hours. "
        f"If you don't hear back, contact them with your payment details.",
        parse_mode="Markdown")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"member_approve:{uid}:{name}:{client_id}"),
        InlineKeyboardButton("❌ Deny",    callback_data=f"member_deny:{uid}:{name}:{client_id}")
    ]])
    await context.bot.forward_message(client_id, update.effective_chat.id, update.message.message_id)
    await context.bot.send_message(client_id,
        f"💳 *New Payment Receipt!*\n\n👤 {name} just sent a payment receipt for *{client['brand_name']}*.",
        parse_mode="Markdown", reply_markup=keyboard)


# ── Member approve ─────────────────────────────────────────────────────────────
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

    packages = client.get("packages") or []
    if not packages:
        await query.edit_message_text("⚠️ No packages set up. Use /settings to add packages.")
        return

    is_test  = client.get("plan") == "test"
    keyboard = []
    for i, p in enumerate(packages):
        is_demo = p.get("is_demo", False)
        label   = f"{p['name']} — {'Free' if is_demo else '₦'+p['price']} ({format_duration(p['duration_days'] if not is_demo else 0)})"
        if is_test and not is_demo:
            keyboard.append([InlineKeyboardButton(
                f"🔒 {label} (test mode only)",
                callback_data=f"noop:{i}"
            )])
        else:
            keyboard.append([InlineKeyboardButton(
                label,
                callback_data=f"member_pkg:{user_id}:{i}:{client_id}:{name}"
            )])

    await query.edit_message_text(
        f"📦 Select package for {name}:"
        + ("\n\n_🔒 Custom packages are disabled during the Test Environment._" if is_test else ""),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard))


async def callback_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(
        "🔒 This package is disabled during the Test Environment.", show_alert=True)


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

    # 1. Calculate the new time to add (delta)
    delta = timedelta(minutes=5) if pkg.get("is_demo") else timedelta(days=pkg["duration_days"])
    
    # 2. Look for existing record in THIS specific channel
    existing = db.table("members").select("expiry, removed").eq("user_id", user_id).eq("client_id", client_id).execute().data
    
    is_active = False
    if existing:
        m = existing[0]
        is_active = not m["removed"]
        
        # Cumulative Math: Add time to existing expiry or 'now'
        base   = datetime.fromisoformat(m["expiry"]) if m.get("expiry") else now
        expiry = (base if base > now else now) + delta
        
        db.table("members").update({
            "expiry": expiry.isoformat(),
            "removed": False
        }).eq("user_id", user_id).eq("client_id", client_id).execute()
    else:
        # New record creation
        expiry = now + delta
        db.table("members").insert({
            "user_id": user_id, "client_id": client_id, "username": name,
            "package": pkg["name"], "expiry": expiry.isoformat(),
            "added_at": now.isoformat(), "removed": False
        }).execute()
        is_active = False

    # 3. Decision: Send Message/Link + Handle Demo Notification
    try:
        if is_active:
            # Silent extension for existing members
            await context.bot.send_message(user_id,
                f"🎉 *Subscription Extended!*\n\n"
                f"Your subscription has been successfully extended.\n"
                f"New expiry: `{expiry.strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
                "You do not need to rejoin. Keep enjoying the content! 🙌",
                parse_mode="Markdown")
        else:
            # New/Re-joining members get a fresh link
            link = (await context.bot.create_chat_invite_link(
                client["channel_id"], member_limit=1, name=f"user_{user_id}"
            )).invite_link
            
            await send_member_welcome(context.bot, user_id, client)
            await context.bot.send_message(user_id,
                f"🎉 *Payment Approved!*\n\n"
                f"Your subscription is active until `{expiry.strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
                f"👇 Tap below to join:\n{link}",
                parse_mode="Markdown")

            # Demo notification to the Client (Admin)
            if pkg.get("is_demo"):
                await context.bot.send_message(client_id,
                    "🎊 *Onboarding complete!*\n\n"
                    "Your test member has received their invite link. ✅\n\n"
                    "The test member will be automatically removed 5 minutes after joining.\n\n"
                    "Ready to go live? Use /pay to activate your full monthly plan 💳",
                    parse_mode="Markdown")

        await query.edit_message_text(f"✅ {name} extended/updated successfully.", parse_mode="Markdown")
    except Exception as e:
        await query.edit_message_text(f"✅ Processed for {name}, but messaging failed:\n`{e}`", parse_mode="Markdown")


async def callback_member_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query     = update.callback_query
    await query.answer()
    parts     = query.data.split(":")
    user_id   = int(parts[1])
    name      = parts[2]
    client_id = int(parts[3])
    if query.from_user.id != client_id and query.from_user.id not in ADMIN_IDS:
        return
    ADMIN_STATE[query.from_user.id] = {
        "action": "member_deny", "user_id": user_id,
        "username": name, "client_id": client_id
    }
    await query.edit_message_text(f"✏️ Type your reason for denying {name}:")


# ── Client approve/deny ────────────────────────────────────────────────────────
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
    existing  = get_client(client_id)
    state     = CLIENT_STATE.get(client_id, {})

    if existing:
        base     = datetime.fromisoformat(existing["expiry"])
        expiry   = (base if base > now else now) + timedelta(days=30)
        ref_code = existing.get("ref_code") or generate_ref_code(existing.get("brand_name", "SPB"))
        db.table("clients").update({
            "expiry": expiry.isoformat(), "plan": "paid",
            "removed": False, "ref_code": ref_code
        }).eq("client_id", client_id).execute()
    else:
        expiry   = now + timedelta(days=30)
        ref_code = generate_ref_code(state.get("brand_name", "SPB"))
        _save_new_client(client_id, name.lstrip("@"), state, plan="paid", expiry=expiry, ref_code=ref_code)
        CLIENT_STATE.pop(client_id, None)

    await context.bot.send_message(client_id,
        f"🎉 *Payment Approved!*\n\n"
        f"✅ Your SubPlanBot subscription is active until `{expiry.strftime('%b %d, %Y')}`\n\n"
        f"🔑 *Your Ref Code:* `{ref_code}`\n"
        f"🔗 *Deep Link:* `{ref_deep_link(ref_code)}`\n"
        "_Share these with your members so they can subscribe instantly._\n\n"
        "❓ Got questions? Reach out to @GizmoBrymez",
        parse_mode="Markdown")
    await query.edit_message_text(
        f"✅ {name} approved. Subscription active until {expiry.strftime('%b %d, %Y')}.",
        parse_mode="Markdown")


async def callback_client_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    parts     = query.data.split(":")
    client_id = int(parts[1])
    name      = parts[2]
    ADMIN_STATE[query.from_user.id] = {
        "action": "client_deny", "user_id": client_id, "username": name
    }
    await query.edit_message_text(f"✏️ Type your reason for denying {name}:")


# ── Text handler ───────────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    if await process_channel_verification(update, context, uid):
        return

    text  = update.message.text or ""
    state = CLIENT_STATE.get(uid, {})

    if uid in ADMIN_STATE:
        a = ADMIN_STATE[uid].get("action")
        if a == "member_deny":
            s = ADMIN_STATE.pop(uid)
            try:
                await context.bot.send_message(s["user_id"],
                    f"❌ *Payment Denied*\n\nReason: _{text}_\n\nContact your channel/group admin for help.",
                    parse_mode="Markdown")
                await update.message.reply_text(f"✅ Reason sent to {s['username']}.")
            except Exception:
                await update.message.reply_text(f"⚠️ Couldn't DM {s['username']}.")
            return
        if a == "client_deny":
            s = ADMIN_STATE.pop(uid)
            try:
                await context.bot.send_message(s["user_id"],
                    f"❌ *Payment Denied*\n\nReason: _{text}_\n\nContact @GizmoBrymez for help.",
                    parse_mode="Markdown")
                await update.message.reply_text(f"✅ Reason sent to {s['username']}.")
            except Exception:
                await update.message.reply_text(f"⚠️ Couldn't DM {s['username']}.")
            return
        if a == "edit_ref":
            custom = text.strip().upper().replace(" ", "-")
            if len(custom) < 3 or len(custom) > 20:
                await update.message.reply_text("⚠️ Ref code must be 3–20 characters. Try again:")
                return
            if db.table("clients").select("ref_code").eq("ref_code", custom).execute().data:
                await update.message.reply_text("⚠️ That code is already taken. Try a different one:")
                return
            ADMIN_STATE.pop(uid)
            db.table("clients").update({"ref_code": custom}).eq("client_id", uid).execute()
            await update.message.reply_text(
                f"✅ *Ref Code updated to:* `{custom}`\n"
                f"🔗 *Deep Link:* `{ref_deep_link(custom)}`",
                parse_mode="Markdown")
            return

    if state.get("step") == STEP_REFCODE_INPUT:
        await handle_refcode_input(update, context, uid)
        return

    if uid in CLIENT_STATE:
        step = state.get("step")
        if step == STEP_CHANNEL:
            await update.message.reply_text(
                "📌 Use the buttons below to add me to your channel or group.\n\n"
                "For *channels* you can also forward any message from your channel here.",
                parse_mode="Markdown",
                reply_markup=_channel_keyboard())
            return
        if step in [STEP_BRAND, STEP_BANK, STEP_ACCT_NUM, STEP_ACCT_NUM2,
                    STEP_ACCT_NAME, STEP_PACKAGES, STEP_REF_CODE]:
            await handle_onboarding(update, context, uid)
            return
        if step == STEP_FLYER:
            await update.message.reply_text(
                "📸 Please send an *image* for your payment flyer, or tap *Skip* below.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⏭️ Skip", callback_data="onboard:skip_flyer")
                ]]))
            return
        if step == STEP_BRAND_LOGO:
            await update.message.reply_text(
                "📸 Please send an *image* for your brand logo, or tap *Skip* below.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⏭️ Skip", callback_data="onboard:skip_logo")
                ]]))
            return

    if is_active_client(uid) and uid in CLIENT_STATE:
        step = state.get("step")
        update_last_seen(uid)
        if step == SETTINGS_EDIT_BRAND:
            db.table("clients").update({"brand_name": text.strip()}).eq("client_id", uid).execute()
            CLIENT_STATE.pop(uid)
            await update.message.reply_text(
                f"✅ Brand name updated to: *{text.strip()}*", parse_mode="Markdown")
            return
        elif step == SETTINGS_EDIT_BANK:
            db.table("clients").update({"bank_name": text.strip()}).eq("client_id", uid).execute()
            CLIENT_STATE.pop(uid)
            await update.message.reply_text("✅ *Bank name updated.*", parse_mode="Markdown")
            return
        elif step == SETTINGS_EDIT_ACCT_NUM:
            val = text.strip()
            if not is_valid_account_number(val):
                await update.message.reply_text(
                    "⚠️ Account number must be *exactly 10 digits* and contain only numbers.\n\nPlease try again:",
                    parse_mode="Markdown")
                return
            state["account_number_tmp"] = val
            state["step"]               = SETTINGS_EDIT_ACCT_NUM2
            CLIENT_STATE[uid]           = state
            await update.message.reply_text(
                "🔢 Please *confirm* by entering the account number again:",
                parse_mode="Markdown")
            return
        elif step == SETTINGS_EDIT_ACCT_NUM2:
            val = text.strip()
            if not is_valid_account_number(val):
                await update.message.reply_text(
                    "⚠️ Account number must be *exactly 10 digits* and contain only numbers.\n\nPlease try again:",
                    parse_mode="Markdown")
                return
            if val != state.get("account_number_tmp"):
                await update.message.reply_text(
                    "⚠️ Numbers don't match. Please enter the account number again:")
                state["step"] = SETTINGS_EDIT_ACCT_NUM
                CLIENT_STATE[uid] = state
                return
            db.table("clients").update({"account_number": val}).eq("client_id", uid).execute()
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
            await update.message.reply_text(
                "💰 What's the price for this package? _(e.g. 5000)_",
                parse_mode="Markdown")
            return
        elif step == SETTINGS_ADD_PKG_PRICE:
            state["new_pkg_price"] = text.strip()
            state["step"]          = SETTINGS_ADD_PKG_DUR
            CLIENT_STATE[uid]      = state
            await update.message.reply_text(
                "📅 How many days does this package last? _(e.g. 30)_",
                parse_mode="Markdown")
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
            "⚠️ *Your SubPlanBot subscription has expired.*\n\nUse /pay to renew.\n\n"
            "❓ Got questions? Reach out to @GizmoBrymez",
            parse_mode="Markdown")
        return

    await update.message.reply_text(
        "⚠️ We only accept a *screenshot* or *PDF* as payment proof.\n\n"
        "Go to your bank app, screenshot the successful transaction, and send it here. 📸",
        parse_mode="Markdown")


# ── Photo handler ──────────────────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    
    # ADMIN BYPASS: If an admin sends a photo, treat it as a file ID request
    if uid in ADMIN_IDS:
        await handle_getfileid_media(update, context)
        return

    if await process_channel_verification(update, context, uid):
        return
        
    state = CLIENT_STATE.get(uid, {})
    step  = state.get("step")
    if step in [STEP_BRAND_LOGO, STEP_FLYER, SETTINGS_EDIT_BRAND_LOGO, SETTINGS_EDIT_FLYER]:
        await handle_onboarding_photo(update, context, uid)
        return
        
    await handle_receipt(update, context)


# ── Forwarded messages ─────────────────────────────────────────────────────────
async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    
    # ADMIN BYPASS: If an admin forwards media to get an ID
    if uid in ADMIN_IDS and (update.message.photo or update.message.document):
        await handle_getfileid_media(update, context)
        return

    if await process_channel_verification(update, context, uid):
        return
    if update.message.document:
        await handle_receipt(update, context)
    elif update.message.photo:
        await handle_photo(update, context)
    else:
        await update.message.reply_text(
            "⚠️ Forwarding only works for channel verification.\n\n"
            "For groups, use the *Add me to your group* button during setup.")


# ── /settings ─────────────────────────────────────────────────────────────────
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_active_client(uid):
        await update.message.reply_text("⚠️ No active subscription. Use /pay to get started.")
        return
    update_last_seen(uid)
    client = get_client(uid)
    ref    = client.get("ref_code", "N/A")

    demo_pkgs   = [p for p in (client.get("packages") or []) if p.get("is_demo")]
    custom_pkgs = [p for p in (client.get("packages") or []) if not p.get("is_demo")]

    pkg_text = ""
    if demo_pkgs:
        pkg_text += "*🧪 Test Environment Package:*\n"
        for p in demo_pkgs:
            pkg_text += f"  • {p['name']} — Free (5 mins after joining)\n"
        pkg_text += "\n"
    if custom_pkgs:
        pkg_text += "*📦 Your Packages:*\n"
        for p in custom_pkgs:
            pkg_text += f"  • {p['name']} — ₦{p['price']} / {format_duration(p['duration_days'])}\n"
    if not pkg_text:
        pkg_text = "None set"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Brand Name",      callback_data="settings:brand")],
        [InlineKeyboardButton("🖼️ Brand Logo",       callback_data="settings:brand_logo")],
        [InlineKeyboardButton("🏦 Bank Name",        callback_data="settings:bank")],
        [InlineKeyboardButton("💳 Account Number",   callback_data="settings:acct_num")],
        [InlineKeyboardButton("👤 Account Name",     callback_data="settings:acct_name")],
        [InlineKeyboardButton("🖼️ Payment Flyer",    callback_data="settings:flyer")],
        [InlineKeyboardButton("🔑 Edit Ref Code",    callback_data="settings:ref")],
        [InlineKeyboardButton("📦 Add Package",      callback_data="settings:add_pkg")],
        [InlineKeyboardButton("🗑️ Delete a Package", callback_data="settings:del_pkg")],
    ])
    await update.message.reply_text(
        f"⚙️ *Settings — {client['brand_name']}*\n\n"
        f"🏦 Bank: {client['bank_name']}\n"
        f"💳 Account: `{client['account_number']}`\n"
        f"👤 Name: {client['account_name']}\n"
        f"🔑 Ref Code: `{ref}`\n"
        f"🔗 Deep Link: `{ref_deep_link(ref)}`\n\n"
        f"{pkg_text}\n"
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
    elif action == "brand_logo":
        CLIENT_STATE[uid] = {"step": SETTINGS_EDIT_BRAND_LOGO}
        await query.edit_message_text("🖼️ Send your new brand logo image:")
    elif action == "bank":
        CLIENT_STATE[uid] = {"step": SETTINGS_EDIT_BANK}
        await query.edit_message_text("🏦 Send your new bank name:")
    elif action == "acct_num":
        CLIENT_STATE[uid] = {"step": SETTINGS_EDIT_ACCT_NUM}
        await query.edit_message_text(
            "💳 Send your new account number: _(must be exactly 10 digits)_",
            parse_mode="Markdown")
    elif action == "acct_name":
        CLIENT_STATE[uid] = {"step": SETTINGS_EDIT_ACCT_NAME}
        await query.edit_message_text("👤 Send your new account name:")
    elif action == "flyer":
        CLIENT_STATE[uid] = {"step": SETTINGS_EDIT_FLYER}
        await query.edit_message_text("🖼️ Send your new payment flyer image:")
    elif action == "ref":
        ADMIN_STATE[uid] = {"action": "edit_ref"}
        await query.edit_message_text(
            "🔑 *Edit Ref Code*\n\n"
            "Type your new custom ref code:\n"
            "_Letters, numbers and hyphens only. Max 20 chars._\n"
            "Example: `PAUL-VIP` or `MYHUB`",
            parse_mode="Markdown")
    elif action == "add_pkg":
        CLIENT_STATE[uid] = {"step": SETTINGS_ADD_PKG}
        await query.edit_message_text(
            "📦 What's the name of the new package? _(e.g. 1 Month)_",
            parse_mode="Markdown")
    elif action == "del_pkg":
        client      = get_client(uid)
        full_pkgs   = client.get("packages") or []
        custom_pkgs = [p for p in full_pkgs if not p.get("is_demo")]
        if not custom_pkgs:
            await query.edit_message_text("ℹ️ You have no custom packages to delete.")
            return
        keyboard = [[InlineKeyboardButton(
            f"🗑️ {p['name']} — ₦{p['price']} / {format_duration(p['duration_days'])}",
            callback_data=f"del_pkg:{uid}:{full_pkgs.index(p)}"
        )] for p in custom_pkgs]
        await query.edit_message_text(
            "Which package do you want to delete? 🗑️",
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
    removed = packages.pop(pkg_idx)
    db.table("clients").update({"packages": packages}).eq("client_id", uid).execute()
    await query.edit_message_text(
        f"✅ Package *{removed['name']}* deleted.\n\n"
        "Existing members on this package will still be managed until their expiry. ✔️",
        parse_mode="Markdown")


# ── /list ─────────────────────────────────────────────────────────────────────
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in ADMIN_IDS:
        await cmd_clientlist(update, context)
        return
    if not is_active_client(uid):
        await update.message.reply_text("⚠️ No active subscription.")
        return
        
    await context.bot.send_chat_action(uid, "typing")
    update_last_seen(uid)
    
    members = db.table("members").select("username, expiry, removed, package") \
                                 .eq("client_id", uid).order("removed").execute().data
                                 
    if not members:
        await update.message.reply_text("📭 No members found.")
        return
        
    lines = ["👥 *Your Member Ledger:*\n"]
    for m in members:
        if m["removed"]:
            status_str = "🚪 Removed"
        else:
            # Show countdown for active members
            countdown = format_expiry_countdown(m["expiry"]) if m["expiry"] else "Pending"
            status_str = f"✅ Active ({countdown} left)"
            
        lines.append(f"• {m['username'] or 'Unknown'} — {m.get('package','?')} | {status_str}")
        
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
        await update.message.reply_text("⚠️ No channel/group linked.")
        return
    await context.bot.ban_chat_member(channel_id, target_id)
    await context.bot.unban_chat_member(channel_id, target_id)
    db.table("members").update({"removed": True}).eq("user_id", target_id).eq("client_id", uid).execute()
    PENDING_APPROVALS.pop(target_id, None)
    await update.message.reply_text(
        f"✅ `{target_id}` removed from your channel/group.",
        parse_mode="Markdown")
    if uid not in ADMIN_IDS:
        update_last_seen(uid)


# ── Admin commands ─────────────────────────────────────────────────────────────
async def cmd_clientlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await context.bot.send_chat_action(update.effective_user.id, "typing")
    
    # Fetch all clients, including removed ones
    clients = db.table("clients").select("username, expiry, plan, brand_name, ref_code, removed").order("removed").execute().data
    
    if not clients:
        await update.message.reply_text("📭 No clients found in the database.")
        return
        
    lines = ["📋 *Full Client Ledger:*\n"]
    for c in clients:
        # Determine Status and Countdown
        if c.get("removed"):
            status_str = "❌ Removed/Inactive"
        else:
            expiry_dt = datetime.fromisoformat(c["expiry"])
            if expiry_dt > datetime.now(timezone.utc):
                status_str = f"✅ Active ({format_expiry_countdown(c['expiry'])} left)"
            else:
                status_str = "⚠️ Expired"
        
        lines.append(f"• {c['username']} — {c['brand_name']} | {status_str} | 🔑 `{c.get('ref_code','N/A')}`")
        
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_getfileid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text("📸 Send me a photo or video and I'll return its file ID.")


async def handle_getfileid_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        return
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        await update.message.reply_text(f"🖼️ *Photo File ID:*\n`{file_id}`", parse_mode="Markdown")
    elif update.message.video:
        file_id = update.message.video.file_id
        await update.message.reply_text(f"🎥 *Video File ID:*\n`{file_id}`", parse_mode="Markdown")
    elif update.message.document:
        file_id = update.message.document.file_id
        await update.message.reply_text(f"📄 *Document File ID:*\n`{file_id}`", parse_mode="Markdown")


# ── Boot ───────────────────────────────────────────────────────────────────────
def run_flask():
    port = int(os.getenv("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("pay",        cmd_pay))
    app.add_handler(CommandHandler("renew",      cmd_renew))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("list",       cmd_list))
    app.add_handler(CommandHandler("remove",     cmd_remove))
    app.add_handler(CommandHandler("settings",   cmd_settings))
    app.add_handler(CommandHandler("clientlist", cmd_clientlist))
    app.add_handler(CommandHandler("getfileid",  cmd_getfileid))

    app.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, handle_forward))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_photo))
    app.add_handler(MessageHandler(filters.Document.PDF & filters.ChatType.PRIVATE, handle_receipt))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_text))

    app.add_handler(ChatMemberHandler(handle_bot_added,     chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(handle_member_joined, chat_member_types=ChatMemberHandler.CHAT_MEMBER))

    app.add_handler(CallbackQueryHandler(callback_welcome,        pattern=r"^welcome:"))
    app.add_handler(CallbackQueryHandler(callback_onboard,        pattern=r"^onboard:"))
    app.add_handler(CallbackQueryHandler(callback_member_approve, pattern=r"^member_approve:"))
    app.add_handler(CallbackQueryHandler(callback_member_pkg,     pattern=r"^member_pkg:"))
    app.add_handler(CallbackQueryHandler(callback_member_deny,    pattern=r"^member_deny:"))
    app.add_handler(CallbackQueryHandler(callback_client_approve, pattern=r"^client_approve:"))
    app.add_handler(CallbackQueryHandler(callback_client_deny,    pattern=r"^client_deny:"))
    app.add_handler(CallbackQueryHandler(callback_settings,       pattern=r"^settings:"))
    app.add_handler(CallbackQueryHandler(callback_del_pkg,        pattern=r"^del_pkg:"))
    app.add_handler(CallbackQueryHandler(callback_noop,           pattern=r"^noop:"))

    print("🤖 SubPlanBot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()