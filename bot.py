import os
import re
import time
import logging
import threading
import asyncio
import random
import string
import zlib
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from flask import Flask, request as flask_request
from supabase import create_client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot, ChatMemberAdministrator, ChatMemberOwner
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler,
                          ChatMemberHandler, ChatJoinRequestHandler, ContextTypes, filters)

load_dotenv()

BOT_TOKEN          = os.getenv("BOT_TOKEN")
BOT_USERNAME       = os.getenv("BOT_USERNAME", "subplanhubbot")
ADMIN_IDS          = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
WELCOME_FILE_ID    = os.getenv("WELCOME_FILE_ID")
ONBOARDING_FILE_ID = os.getenv("ONBOARDING_FILE_ID")
FLYER_FILE_ID      = os.getenv("FLYER_FILE_ID")
CRON_SECRET        = os.getenv("CRON_SECRET")  # no default: cron routes refuse to run if unset
db                 = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))

logging.basicConfig(format="%(asctime)s %(message)s", level=logging.INFO)

# ── Platform payment account (override via env) ────────────────────────────────
PLATFORM_BANK_NAME   = os.getenv("PLATFORM_BANK_NAME", "Kuda Bank")
PLATFORM_ACCT_NUMBER = os.getenv("PLATFORM_ACCT_NUMBER", "3004015285")
PLATFORM_ACCT_NAME   = os.getenv("PLATFORM_ACCT_NAME", "Pradage Ventures")
PLATFORM_PRICE       = os.getenv("PLATFORM_PRICE", "3,000")

# How often cron-job.org calls the /cron/remind-* endpoints. Reminder windows are exactly this
# wide, so each person is reminded once per milestone. Change both together.
CRON_INTERVAL_HOURS = int(os.getenv("CRON_INTERVAL_HOURS", "1"))
DEMO_PKG_NAME       = "5 Min Demo"

# ── In-memory state ────────────────────────────────────────────────────────────
CLIENT_STATE     = {}   # onboarding / settings / payment-target state per user
ADMIN_STATE      = {}   # "type your denial reason" / "type new ref code" state
RECEIPT_NOTICES  = {}   # key -> [(chat_id, message_id)] of Approve/Deny messages still showing buttons
IN_FLIGHT        = set()  # approvals currently being processed (double-tap guard)
RECENT_APPROVALS = {}   # key -> unix time of last approval (duplicate-attempt guard)
RIGHTS_ALERTED   = {}   # client_id -> unix time of last "I need rights" alert
RESTORING        = set()  # client_ids whose lapse-evicted members are being restored right now

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

def _authorized() -> bool:
    return bool(CRON_SECRET) and flask_request.args.get("secret") == CRON_SECRET

def _run_bg(fn):
    """Run the job in a background thread so the cron caller doesn't time out on big jobs."""
    threading.Thread(target=fn, daemon=True).start()
    return "Started", 202

@flask_app.route("/")
def health():
    return "SubPlanBot is alive ✅", 200

@flask_app.route("/cron/kick-members", methods=["GET", "POST"])
def cron_kick_members():
    if not _authorized():
        return "Unauthorized", 401
    return _run_bg(_kick_expired_members_sync)

@flask_app.route("/cron/kick-clients", methods=["GET", "POST"])
def cron_kick_clients():
    if not _authorized():
        return "Unauthorized", 401
    return _run_bg(_kick_expired_clients_sync)

@flask_app.route("/cron/remind-members", methods=["GET", "POST"])
def cron_remind_members():
    if not _authorized():
        return "Unauthorized", 401
    return _run_bg(_remind_members_sync)

@flask_app.route("/cron/remind-clients", methods=["GET", "POST"])
def cron_remind_clients():
    if not _authorized():
        return "Unauthorized", 401
    return _run_bg(_remind_clients_sync)

@flask_app.route("/cron/inactivity", methods=["GET", "POST"])
def cron_inactivity():
    if not _authorized():
        return "Unauthorized", 401
    return _run_bg(_inactivity_sync)

@flask_app.route("/cron/rights", methods=["GET", "POST"])
def cron_rights():
    if not _authorized():
        return "Unauthorized", 401
    return _run_bg(_rights_check_sync)


# ── Helpers: clients ───────────────────────────────────────────────────────────
def get_client(user_id: int):
    res = db.table("clients").select("*").eq("client_id", user_id).execute().data
    return res[0] if res else None

def get_client_by_ref(ref_code: str):
    res = db.table("clients").select("*").eq("ref_code", ref_code.upper()).eq("removed", False).execute().data
    return res[0] if res else None

def find_client(token: str):
    """Platform-admin lookup by client_id, @username or ref code (includes removed clients)."""
    token = token.strip()
    if token.lstrip("-").isdigit():
        return get_client(int(token))
    if token.startswith("@"):
        res = db.table("clients").select("*").eq("username", token).execute().data
    else:
        res = db.table("clients").select("*").eq("ref_code", token.upper()).execute().data
    return res[0] if res else None

def is_client(user_id: int) -> bool:
    c = get_client(user_id)
    return c is not None and not c.get("removed", False)

def _client_is_active(c) -> bool:
    if not c or c.get("removed") or c.get("suspended"):
        return False
    return datetime.fromisoformat(c["expiry"]) > datetime.now(timezone.utc)

def is_active_client(user_id: int) -> bool:
    return _client_is_active(get_client(user_id))

def update_last_seen(user_id: int):
    db.table("clients").update({"last_seen": datetime.now(timezone.utc).isoformat()}).eq("client_id", user_id).execute()


# ── Helpers: text & validation ─────────────────────────────────────────────────
def escape_md(text) -> str:
    """Escape Telegram legacy-Markdown special chars in dynamic text."""
    text = str(text)
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text

_REF_RE = re.compile(r"[A-Z0-9-]{3,20}")

def valid_ref_code(code: str) -> bool:
    """Deep links only accept a restricted charset, so keep ref codes inside it."""
    return bool(_REF_RE.fullmatch(code))

def generate_ref_code(brand_name: str) -> str:
    prefix = "".join(c for c in (brand_name or "").upper() if c.isalpha() and c.isascii())[:4] or "SPB"
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

def clean_price(value: str):
    """'5,000' -> '5000'. Returns None if it isn't a positive whole number."""
    v = value.strip().replace(",", "")
    return v if v.isdigit() and int(v) > 0 else None

def _expiry_window(now, offset):
    """[now+offset, now+offset+cron interval): each person lands in exactly one cron run."""
    start = now + offset
    return start.isoformat(), (start + timedelta(hours=CRON_INTERVAL_HOURS)).isoformat()


# ── Helpers: members ───────────────────────────────────────────────────────────
def package_delta(pkg: dict) -> timedelta:
    if pkg.get("is_demo") or pkg.get("duration_minutes"):
        return timedelta(minutes=int(pkg.get("duration_minutes") or 5))
    return timedelta(days=int(pkg.get("duration_days") or 0))

def pkg_tag(pkg: dict) -> str:
    """Short fingerprint of a package. Buttons carry it so a stale button can tell the list changed."""
    raw = f"{pkg.get('name')}|{pkg.get('price')}|{pkg.get('duration_days')}"
    return format(zlib.crc32(raw.encode()) & 0xFFFFFF, "06x")

def update_member_row(user_id: int, client_id: int, fields: dict) -> bool:
    """Update one members row. If the paused_seconds column hasn't been added yet, saves everything
    else and returns False (so callers know nothing was saved for a later restore)."""
    def _run(f):
        db.table("members").update(f).eq("user_id", user_id).eq("client_id", client_id).execute()
    try:
        _run(fields)
        return True
    except Exception as e:
        if "paused_seconds" not in fields or "paused_seconds" not in str(e):
            raise
        logging.warning("members.paused_seconds is missing. Run: ALTER TABLE members ADD COLUMN paused_seconds integer;")
        _run({k: v for k, v in fields.items() if k != "paused_seconds"})
        return False

def paused_seconds_for(m: dict, now):
    """What to save for a member evicted because their owner's plan lapsed.
    0 = approved but hadn't joined (goes back to pending), >0 = seconds they had left,
    None = nothing to restore (demo package, or under an hour left)."""
    if m.get("package") == DEMO_PKG_NAME:
        return None
    if not m.get("expiry"):
        return 0
    left = int((datetime.fromisoformat(m["expiry"]) - now).total_seconds())
    return left if left >= 3600 else None

def member_is_currently_active(row: dict, now=None) -> bool:
    now = now or datetime.now(timezone.utc)
    if row.get("removed") or not row.get("expiry"):
        return False
    return datetime.fromisoformat(row["expiry"]) > now

def member_is_pending(row: dict) -> bool:
    """Approved, invite sent, hasn't joined yet (timer starts on join)."""
    return bool(row) and not row.get("removed") and not row.get("expiry")

def get_pay_target(uid: int):
    """'platform' or a client_id. Expires after 30 min so a stale choice can't misroute a receipt."""
    state = CLIENT_STATE.get(uid, {})
    t = state.get("pay_target")
    if t is not None and time.time() - state.get("pay_target_at", 0) < 1800:
        return t
    return None

def set_pay_target(uid: int, target):
    state = CLIENT_STATE.setdefault(uid, {})
    state["pay_target"]    = target
    state["pay_target_at"] = time.time()

def set_member_target(uid: int, client_id: int):
    state = CLIENT_STATE.setdefault(uid, {})
    state["subscribing_to_client_id"] = client_id
    state["step"] = None
    set_pay_target(uid, client_id)

def resolve_member_client(uid: int):
    """Which channel/group is this person subscribed to (as a member)? Never their own."""
    cid = CLIENT_STATE.get(uid, {}).get("subscribing_to_client_id")
    if cid and cid != uid:
        return cid
    rows = (db.table("members").select("client_id, removed, expiry, added_at")
            .eq("user_id", uid).order("added_at", desc=True).execute().data)
    rows = [r for r in rows if r["client_id"] != uid]
    if not rows:
        return None
    for r in rows:
        if member_is_currently_active(r) or member_is_pending(r):
            return r["client_id"]
    return rows[0]["client_id"]


# ── Helpers: Telegram ──────────────────────────────────────────────────────────
async def resolve_name(bot, user_id: int) -> str:
    """Display name looked up on demand (never carried through callback_data)."""
    try:
        chat = await bot.get_chat(user_id)
        if chat.username:
            return f"@{chat.username}"
        return chat.full_name or str(user_id)
    except Exception:
        return str(user_id)

async def user_is_chat_admin(bot, chat_id, uid: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, uid)
        return m.status in ("creator", "administrator")
    except Exception:
        return False

async def user_in_chat(bot, chat_id, uid: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, uid)
        return m.status in ("member", "administrator", "creator", "restricted")
    except Exception:
        return False

def channel_taken(channel_id, uid: int) -> bool:
    rows = (db.table("clients").select("client_id").eq("channel_id", channel_id)
            .neq("client_id", uid).eq("removed", False).execute().data)
    return bool(rows)

async def create_member_invite(bot, channel_id: int, user_id: int) -> str:
    """Create a one-seat invite; fall back to join-request mode if needed."""
    try:
        return (await bot.create_chat_invite_link(
            channel_id, member_limit=1, name=f"user_{user_id}"
        )).invite_link
    except Exception:
        return (await bot.create_chat_invite_link(
            channel_id, creates_join_request=True, name=f"user_{user_id}"
        )).invite_link

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

async def send_long(message, lines, parse_mode="Markdown", limit=4000):
    """Telegram caps messages at 4096 chars; send big ledgers in chunks."""
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > limit:
            await message.reply_text(chunk, parse_mode=parse_mode)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await message.reply_text(chunk, parse_mode=parse_mode)


# ── Helpers: bot admin rights ──────────────────────────────────────────────────
def _rights_problem_from_member(m):
    """None if the bot has what it needs, else a plain-English description of what's missing."""
    if isinstance(m, ChatMemberOwner):
        return None
    if not isinstance(m, ChatMemberAdministrator):
        return "I'm no longer an admin here (or I was removed)."
    missing = []
    if not m.can_restrict_members:
        missing.append("Ban Members")
    if not m.can_invite_users:
        missing.append("Invite Users")
    return ("Missing permission: " + ", ".join(missing) + ".") if missing else None

async def bot_rights_problem(bot, chat_id):
    try:
        m = await bot.get_chat_member(chat_id, bot.id)
    except Exception:
        return "I can't see this channel/group. Please add me again as an admin."
    return _rights_problem_from_member(m)

def looks_like_rights_error(e) -> bool:
    msg = str(e).lower()
    return any(k in msg for k in (
        "not enough rights", "chat_admin_required", "need to be an administrator",
        "administrator rights", "bot was kicked", "bot is not a member",
        "chat not found", "forbidden"))

async def alert_rights(bot, client_id: int, brand, problem: str, force: bool = False):
    """Tell the channel owner AND the platform admins that the bot needs rights.
    Throttled to once per 24h per client unless `force` (real-time permission changes)."""
    now = time.time()
    if not force and now - RIGHTS_ALERTED.get(client_id, 0) < 86400:
        return
    RIGHTS_ALERTED[client_id] = now
    try:
        await bot.send_message(client_id,
            "⚠️ *I need admin rights in your channel/group.*\n\n"
            f"{escape_md(problem)}\n\n"
            "Open your channel/group → *Administrators* → *SubPlanBot* and enable *Ban Members* "
            "and *Invite Users*. Until then I can't approve new members or remove expired ones.",
            parse_mode="Markdown")
    except Exception:
        pass
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin,
                f"⚠️ *Rights problem:* {escape_md(brand or client_id)} (`{client_id}`) — {escape_md(problem)}",
                parse_mode="Markdown")
        except Exception:
            pass


# ── Helpers: approval notices / duplicate guard ────────────────────────────────
def register_notice(key, chat_id: int, message_id: int):
    RECEIPT_NOTICES.setdefault(key, []).append((chat_id, message_id))

async def strip_buttons(bot, key):
    """Remove the Approve/Deny buttons from every outstanding notice for this payment."""
    for chat_id, mid in RECEIPT_NOTICES.pop(key, []):
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=mid, reply_markup=None)
        except Exception:
            pass

def mark_approved(key):
    RECENT_APPROVALS[key] = time.time()

def recently_approved(key, seconds: int = 300) -> bool:
    t = RECENT_APPROVALS.get(key)
    return t is not None and (time.time() - t) < seconds


# ── Helpers: messages ──────────────────────────────────────────────────────────
async def send_member_welcome(bot, user_id: int, client: dict):
    brand   = escape_md(client.get("brand_name") or "the channel/group")
    logo_id = client.get("brand_logo_id")
    admin_u = escape_md(client.get("username") or "the admin")
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
    """Clean payment details for a member — mirrors the old single-client bot format."""
    admin_u   = escape_md(client.get("username") or "the admin")
    packages  = [p for p in (client.get("packages") or []) if not p.get("is_demo")]
    pkg_lines = "\n".join([
        f"• *{escape_md(p['name'])}* — ₦{escape_md(p['price'])} ({format_duration(p['duration_days'])})"
        for p in packages
    ])
    caption = (
        f"💳 *Payment Details — {escape_md(client.get('brand_name') or '')}*\n\n"
        f"📦 *Packages:*\n{pkg_lines}\n\n"
        f"🏦 Bank: {escape_md(client.get('bank_name') or '—')}\n"
        f"💰 Account Number: `{client.get('account_number') or '—'}`\n"
        f"👤 Account Name: {escape_md(client.get('account_name') or '—')}\n\n"
        "After payment kindly send your receipt here.\n\n"
        f"_For non-Nigerians, kindly DM {admin_u} for a different payment method._"
    )
    if client.get("flyer_file_id"):
        await message.reply_photo(photo=client["flyer_file_id"], caption=caption, parse_mode="Markdown")
    else:
        await message.reply_text(caption, parse_mode="Markdown")

def client_home_text(c: dict) -> str:
    """What a channel owner sees on /start, based on their account state."""
    if c.get("plan") == "pending":
        return ("⏳ *Your payment hasn't been confirmed yet.*\n\n"
                "Use /pay for payment details, then send your receipt here.\n\n"
                "🧪 Want to try it first? Tap below for a free 24-hour test.\n\n"
                "❓ Got questions? Reach out to @GizmoBrymez")
    if c.get("suspended"):
        return ("🚫 *Your account is suspended.*\n\n"
                "Contact @GizmoBrymez to resolve this.")
    if _client_is_active(c):
        ref = c.get("ref_code", "N/A")
        return (f"👋 *Welcome back, {escape_md(c.get('brand_name') or 'there')}!*\n\n"
                "📋 /list — view active members\n"
                "❌ /remove — remove a member\n"
                "⚙️ /settings — manage brand, packages & payment info\n"
                "💳 /pay — renew your SubPlanBot subscription\n"
                "🎟️ /subscribe — join another channel/group as a member\n\n"
                f"🔑 *Your Ref Code:* `{ref}`\n"
                f"🔗 *Deep Link:* `{ref_deep_link(ref)}`\n"
                "_Share either with your members so they can subscribe instantly._\n\n"
                "❓ Got questions? Reach out to @GizmoBrymez")
    return ("⚠️ *Your SubPlanBot subscription has expired.*\n\n"
            "Use /pay to renew and restore your channel/group management.\n\n"
            "❓ Got questions? Reach out to @GizmoBrymez")

def trial_offer_markup(c):
    """An owner who tapped Make Payment but isn't approved yet can still take the free test."""
    if c and c.get("plan") == "pending":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🧪 Try the Test Environment first", callback_data="onboard:trial")
        ]])
    return None


# ── Cron jobs ──────────────────────────────────────────────────────────────────
def _kick_expired_members_sync():
    async def _do():
        async with Bot(token=BOT_TOKEN) as bot:
            now = datetime.now(timezone.utc).isoformat()
            # Any non-removed client (active, or lapsed but not yet cleared by the client job)
            clients = db.table("clients").select(
                "client_id, channel_id, username, brand_name"
            ).eq("removed", False).execute().data
            for client in clients:
                expired = db.table("members").select("user_id, username, package").lte(
                    "expiry", now
                ).eq("removed", False).eq("client_id", client["client_id"]).execute().data
                evicted      = []
                rights_issue = None
                for m in expired:
                    try:
                        if client.get("channel_id"):
                            await bot.ban_chat_member(client["channel_id"], m["user_id"])
                            await bot.unban_chat_member(client["channel_id"], m["user_id"])
                        db.table("members").update({"removed": True}).eq(
                            "user_id", m["user_id"]
                        ).eq("client_id", client["client_id"]).execute()
                        if m.get("package") != DEMO_PKG_NAME:
                            evicted.append(escape_md(m["username"] or m["user_id"]))
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
                        if looks_like_rights_error(e):
                            rights_issue = str(e)
                    await asyncio.sleep(0.05)  # stay under Telegram's rate limits

                if evicted:  # one summary per client instead of one message per member
                    shown = ", ".join(evicted[:20]) + (f" and {len(evicted) - 20} more" if len(evicted) > 20 else "")
                    try:
                        await bot.send_message(client["client_id"],
                            f"🚪 *Evicted ({len(evicted)}):* {shown} — subscription expired.",
                            parse_mode="Markdown")
                    except Exception:
                        pass
                if rights_issue:
                    await alert_rights(bot, client["client_id"], client.get("brand_name"),
                                       "I couldn't remove expired members. " + rights_issue[:150])
    asyncio.run(_do())

def _kick_expired_clients_sync():
    async def _do():
        async with Bot(token=BOT_TOKEN) as bot:
            now = datetime.now(timezone.utc).isoformat()
            # 'pending' = onboarded but platform payment not yet approved. Never evict those.
            expired = db.table("clients").select(
                "client_id, username, channel_id"
            ).lte("expiry", now).eq("removed", False).neq("plan", "pending").execute().data
            for c in expired:
                try:
                    # Evict all members from this client's channel first
                    members = db.table("members").select("user_id, username, expiry, package").eq(
                        "client_id", c["client_id"]
                    ).eq("removed", False).execute().data
                    channel_id = c.get("channel_id")
                    now_dt     = datetime.now(timezone.utc)
                    for m in members:
                        try:
                            if channel_id:
                                await bot.ban_chat_member(channel_id, m["user_id"])
                                await bot.unban_chat_member(channel_id, m["user_id"])
                            # Save what they had, so they can be brought back when the owner renews
                            paused = paused_seconds_for(m, now_dt)
                            fields = {"removed": True}
                            if paused is not None:
                                fields["paused_seconds"] = paused
                            saved = update_member_row(m["user_id"], c["client_id"], fields)
                            if saved and paused is not None:
                                text = ("⏸️ *Access paused.*\n\n"
                                        "The channel/group admin's SubPlanBot subscription expired, "
                                        "so managed access has been removed for now.\n\n"
                                        + ("Your remaining time is saved. " if paused > 0 else "")
                                        + "If the admin renews, I'll bring you back automatically "
                                          "and send you a new invite link.")
                            else:
                                text = ("🚪 *Access ended.*\n\n"
                                        "The channel/group admin's SubPlanBot subscription expired, "
                                        "so managed access has been removed.\n\n"
                                        "Contact the admin if you need to rejoin later.")
                            try:
                                await bot.send_message(m["user_id"], text, parse_mode="Markdown")
                            except Exception:
                                pass
                        except Exception as me:
                            logging.error(
                                f"Failed to evict member {m['user_id']} for expired client {c['client_id']}: {me}"
                            )
                        await asyncio.sleep(0.05)

                    db.table("clients").update({"removed": True}).eq("client_id", c["client_id"]).execute()
                    try:
                        await bot.send_message(c["client_id"],
                            "🚪 *Your SubPlanBot subscription has expired.*\n\n"
                            "Your channel/group is no longer being managed, and active members "
                            "have been removed from access.\n\n"
                            "Use /pay to renew. When you do, I'll bring those members back "
                            "with the time they had left. 🙏",
                            parse_mode="Markdown")
                    except Exception:
                        pass
                    for admin in ADMIN_IDS:
                        try:
                            await bot.send_message(admin,
                                f"🚪 *Client evicted:* {escape_md(c['username'] or c['client_id'])} "
                                f"— subscription expired. {len(members)} member(s) cleared.",
                                parse_mode="Markdown")
                        except Exception as notify_err:
                            logging.error(f"Failed to notify admin {admin}: {notify_err}")
                except Exception as e:
                    logging.error(f"Client kick failed for {c['client_id']}: {e}")
    asyncio.run(_do())

def _remind_members_sync():
    async def _do():
        async with Bot(token=BOT_TOKEN) as bot:
            now    = datetime.now(timezone.utc)
            brands = {}
            for days in [7, 3, 1]:
                start, end = _expiry_window(now, timedelta(days=days))
                members = db.table("members").select(
                    "user_id, username, expiry, client_id"
                ).gte("expiry", start).lt("expiry", end).eq("removed", False).execute().data

                for m in members:
                    cid = m["client_id"]
                    if cid not in brands:
                        brands[cid] = get_client(cid)
                    client = brands[cid]
                    brand  = escape_md(client["brand_name"]) if client and client.get("brand_name") else "your channel/group"
                    expiry = datetime.fromisoformat(m["expiry"]).strftime("%b %d, %Y")
                    try:
                        await bot.send_message(m["user_id"],
                            f"⏰ *Heads up!* Your *{brand}* subscription expires in "
                            f"*{days} day{'s' if days > 1 else ''}* ({expiry}).\n\n"
                            "Renew now → /pay 🙏",
                            parse_mode="Markdown")
                    except Exception as e:
                        logging.error(f"Reminder failed for {m['user_id']}: {e}")
                        if client:
                            try:
                                await bot.send_message(client["client_id"],
                                    f"⚠️ Couldn't DM {escape_md(m.get('username') or m['user_id'])} "
                                    f"— their sub expires in {days}d.",
                                    parse_mode="Markdown")
                            except Exception:
                                pass
                    await asyncio.sleep(0.05)
    asyncio.run(_do())

def _remind_clients_sync():
    async def _do():
        async with Bot(token=BOT_TOKEN) as bot:
            now = datetime.now(timezone.utc)
            for days in [3, 1]:
                start, end = _expiry_window(now, timedelta(days=days))
                clients = db.table("clients").select("client_id, expiry").gte(
                    "expiry", start).lt("expiry", end).eq("removed", False).execute().data
                for c in clients:
                    expiry = datetime.fromisoformat(c["expiry"]).strftime("%b %d, %Y")
                    try:
                        await bot.send_message(c["client_id"],
                            f"⏰ *Heads up!* Your SubPlanBot subscription expires in *{days} day{'s' if days > 1 else ''}* ({expiry}).\n\n"
                            "Renew now → /pay to keep your channel/group running 🙏",
                            parse_mode="Markdown")
                    except Exception:
                        pass

            # Final nudge: test users get a conversion prompt, paid users a renewal warning
            start, end = _expiry_window(now, timedelta(hours=1))
            rows = db.table("clients").select("client_id, plan").gte(
                "expiry", start).lt("expiry", end).eq("removed", False).execute().data
            for c in rows:
                if c.get("plan") == "test":
                    text = ("🧪 *Your test environment ends soon.*\n\n"
                            "Happy with what you saw? Use /pay to go live before it ends ⚡")
                else:
                    text = ("🚨 *Your SubPlanBot subscription expires within 2 hours.*\n\n"
                            "Renew now → /pay to avoid interruption ⚡")
                try:
                    await bot.send_message(c["client_id"], text, parse_mode="Markdown")
                except Exception:
                    pass
    asyncio.run(_do())

def _inactivity_sync():
    async def _do():
        async with Bot(token=BOT_TOKEN) as bot:
            now       = datetime.now(timezone.utc)
            threshold = (now - timedelta(days=30)).isoformat()
            inactive  = db.table("clients").select("client_id").lt(
                "last_seen", threshold
            ).eq("removed", False).neq("plan", "pending").execute().data
            for c in inactive:
                try:
                    await bot.send_message(c["client_id"],
                        "👋 *Hey! We miss you.*\n\n"
                        "We noticed you haven't used SubPlanBot in a while.\n\n"
                        "Your channel/group management is still running — just wanted to check in! "
                        "Type /start if you need anything 😊",
                        parse_mode="Markdown")
                    # Reset the clock: one nudge per 30 days (and it survives restarts)
                    db.table("clients").update({"last_seen": now.isoformat()}).eq("client_id", c["client_id"]).execute()
                except Exception:
                    pass
    asyncio.run(_do())

def _rights_check_sync():
    """Daily sweep: tell owners (and us) if the bot has lost the rights it needs, and finish any
    member restores that couldn't complete earlier."""
    async def _do():
        async with Bot(token=BOT_TOKEN) as bot:
            clients = db.table("clients").select("client_id, channel_id, brand_name").eq("removed", False).execute().data
            for c in clients:
                if not c.get("channel_id"):
                    continue
                problem = await bot_rights_problem(bot, c["channel_id"])
                if problem:
                    await alert_rights(bot, c["client_id"], c.get("brand_name"), problem)
                else:
                    await _restore_paused_members(bot, c["client_id"])
                await asyncio.sleep(0.05)
    asyncio.run(_do())


# ── Restoring members after an owner renews ───────────────────────────────────
async def _restore_paused_members(bot, client_id: int):
    """Bring back members who were evicted because this owner's plan lapsed.
    Members who had time left get exactly that much, starting now. Approved members who hadn't
    joined go back to pending (timer starts when they join). Anything that can't be finished
    (e.g. I'm missing rights) stays saved and is retried by the daily rights sweep, or as soon
    as my rights come back."""
    if client_id in RESTORING:
        return
    RESTORING.add(client_id)
    try:
        client = get_client(client_id)
        if not _client_is_active(client) or not client.get("channel_id"):
            return
        try:
            rows = (db.table("members").select("user_id, username, package, paused_seconds")
                    .eq("client_id", client_id).eq("removed", True)
                    .not_.is_("paused_seconds", "null").execute().data)
        except Exception as e:
            logging.error(f"Restore lookup failed for {client_id} (is members.paused_seconds missing?): {e}")
            return
        if not rows:
            return

        channel_id = client["channel_id"]
        problem    = await bot_rights_problem(bot, channel_id)
        if problem:
            await alert_rights(bot, client_id, client.get("brand_name"),
                f"I can't bring back {len(rows)} member(s) who lost access when your plan lapsed. {problem}")
            return

        brand       = escape_md(client.get("brand_name") or "the channel/group")
        packages    = client.get("packages") or []
        restored    = 0
        unreachable = []
        for r in rows:
            uid  = r["user_id"]
            secs = int(r.get("paused_seconds") or 0)
            now  = datetime.now(timezone.utc)
            try:
                inside = await user_in_chat(bot, channel_id, uid)   # rejoined some other way during the lapse
                link   = None if inside else await create_member_invite(bot, channel_id, uid)
            except Exception as e:
                logging.error(f"Restore: invite for {uid} in client {client_id} failed: {e}")
                if looks_like_rights_error(e):
                    await alert_rights(bot, client_id, client.get("brand_name"),
                        "I couldn't create invite links to bring back your members. "
                        "I need the Invite Users permission.")
                    break   # the rest stay saved and are retried later
                continue

            if secs > 0:
                expiry = now + timedelta(seconds=secs)
            elif inside:   # approved but never joined, and already in: start their package now
                pkg    = next((p for p in packages if p.get("name") == r.get("package")), None)
                expiry = now + package_delta(pkg or {"duration_days": 1})
            else:
                expiry = None   # pending again; the timer starts when they join
            try:
                update_member_row(uid, client_id, {
                    "removed": False, "paused_seconds": None,
                    "expiry": expiry.isoformat() if expiry else None,
                    "joined_at": now.isoformat() if inside else None,
                })
            except Exception as e:
                logging.error(f"Restore: saving {uid} in client {client_id} failed: {e}")
                continue
            restored += 1

            if inside:
                text = (f"🎉 *{brand} is back!*\n\n"
                        "The admin renewed, so your access is tracked again until "
                        f"`{expiry.strftime('%Y-%m-%d %H:%M UTC')}`.")
            elif expiry:
                text = (f"🎉 *{brand} is back!*\n\n"
                        f"The admin renewed, so you get back the *{format_expiry_countdown(expiry.isoformat())}* "
                        "you had left, starting now.\n\n"
                        f"👇 Tap below to rejoin:\n{escape_md(link)}")
            else:
                text = (f"🎉 *{brand} is back!*\n\n"
                        "The admin renewed, and your approved subscription is waiting.\n\n"
                        f"👇 Tap below to join. Your timer starts when you enter:\n{escape_md(link)}")
            try:
                await bot.send_message(uid, text, parse_mode="Markdown")
            except Exception:
                unreachable.append(escape_md(r.get("username") or uid))
            await asyncio.sleep(0.1)

        if restored:
            msg = (f"♻️ *Brought back {restored} member(s)* who lost access when your plan lapsed. "
                   "Anyone with time left gets exactly what they had, starting now.")
            if unreachable:
                shown = ", ".join(unreachable[:20]) + (
                    f" and {len(unreachable) - 20} more" if len(unreachable) > 20 else "")
                msg += (f"\n\n⚠️ I couldn't message {shown}. Send them your channel/group's invite link. "
                        "I'll keep tracking their time.")
            try:
                await bot.send_message(client_id, msg, parse_mode="Markdown")
            except Exception:
                pass
    finally:
        RESTORING.discard(client_id)


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

    # Forwarded channel messages
    if getattr(update.message, "forward_from_chat", None) and update.message.forward_from_chat.type == "channel":
        channel_identifier = update.message.forward_from_chat.id
        chat_title = update.message.forward_from_chat.title
    elif getattr(update.message, "forward_origin", None) and getattr(update.message.forward_origin, "chat", None):
        if getattr(update.message.forward_origin.chat, "type", None) == "channel":
            channel_identifier = update.message.forward_origin.chat.id
            chat_title = update.message.forward_origin.chat.title
        else:
            await update.message.reply_text(
                "⚠️ Forwarding is for *channels* only.\n\n"
                "For *groups*, please use the *Add me to your group* button below.",
                parse_mode="Markdown",
                reply_markup=_channel_keyboard())
            return True
    else:
        await update.message.reply_text(
            "📌 Use the buttons below to add me to your channel or group.\n\n"
            "If you are setting up a *channel*, you can also forward any message from it here.",
            parse_mode="Markdown",
            reply_markup=_channel_keyboard())
        return True

    await context.bot.send_chat_action(uid, "typing")
    try:
        problem = await bot_rights_problem(context.bot, channel_identifier)
        if problem:
            await update.message.reply_text(
                f"⚠️ {problem}\n\nFix it in your channel's admin settings, then forward a message again.")
            return True
        # The person linking must actually own/admin that channel
        if not await user_is_chat_admin(context.bot, channel_identifier, uid):
            await update.message.reply_text("⚠️ Only an admin or owner of that channel can link it.")
            return True
        # One channel, one client
        if channel_taken(channel_identifier, uid):
            await update.message.reply_text(
                "⚠️ That channel is already linked to another SubPlanBot account.\n\n"
                "If this is a mistake, contact @GizmoBrymez.")
            return True

        state["channel_id"]   = channel_identifier
        state["channel_name"] = chat_title
        CLIENT_STATE[uid]     = state
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
        InlineKeyboardButton("🧪 Test Environment", callback_data="onboard:trial"),
        InlineKeyboardButton("💳 Make Payment",     callback_data="onboard:pay")
    ]])
    await bot.send_message(uid,
        f"✅ *Channel/group verified:* {escape_md(channel_name)}\n\nI've been added as admin successfully! 🎉",
        parse_mode="Markdown")
    await send_media_or_text(bot, uid, ONBOARDING_FILE_ID, caption, reply_markup=keyboard)


# ── /start ─────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    chat = update.effective_chat

    # Group branch first: never print dashboards into a group chat
    if chat.type in ("group", "supergroup"):
        state = CLIENT_STATE.get(uid, {})
        if state.get("step") == STEP_CHANNEL:
            try:
                problem = await bot_rights_problem(context.bot, chat.id)
                if problem:
                    await update.message.reply_text(
                        f"⚠️ {problem} Please fix it in the group's admin settings and run /start again.")
                    return
                if not await user_is_chat_admin(context.bot, chat.id, uid):
                    await update.message.reply_text("⚠️ Only a group admin can link this group.")
                    return
                if channel_taken(chat.id, uid):
                    await update.message.reply_text("⚠️ This group is already linked to another SubPlanBot account.")
                    return
                state["channel_id"]   = chat.id
                state["channel_name"] = chat.title
                CLIENT_STATE[uid]     = state
                await _show_plan_selection(context.bot, uid, chat.title)
            except Exception as e:
                logging.error(f"Group start detection failed: {e}")
        return

    await context.bot.send_chat_action(uid, "typing")

    # 1. Deep link: works for everyone, including channel owners joining someone else's channel
    if context.args and context.args[0].lower() != "setup":
        ref_code = context.args[0].upper()
        client   = get_client_by_ref(ref_code)
        if not client:
            await update.message.reply_text(
                "❌ *Invalid ref link.*\n\nPlease check the link or ask your channel/group admin for a new one.",
                parse_mode="Markdown")
            return
        if client["client_id"] != uid:   # your own link just falls through to your dashboard
            if is_active_client(client["client_id"]):
                await _show_subscription_info(update, context, uid, client)
            else:
                await update.message.reply_text(
                    "⚠️ This channel/group isn't accepting subscriptions right now. Please contact the admin.")
            return

    # 2. Existing client (pending / suspended / active / expired)
    existing_client = get_client(uid)
    if existing_client:
        if _client_is_active(existing_client):
            update_last_seen(uid)
        await update.message.reply_text(client_home_text(existing_client), parse_mode="Markdown",
                                        reply_markup=trial_offer_markup(existing_client))
        return

    # 3. Platform admin
    if uid in ADMIN_IDS:
        await update.message.reply_text(
            "👾 *SubPlan Bot Super Admin Panel*\n\n"
            "/clientlist — view all clients\n"
            "/suspend — suspend a client\n"
            "/unsuspend — lift a suspension\n"
            "/remove — remove a member from a client's channel\n"
            "/getfileid — get file ID",
            parse_mode="Markdown")
        return

    # 4. New visitor
    welcome_text = (
        "🚀 *Welcome to SubPlan Bot!*\n\n"
        "The easiest way to run paid Telegram channels & groups.\n\n"
        "It handles payments, provides paid access, and automatically tracks and removes expired subscriptions — "
        "You focus on growing your community📈\n\n"
        "What are you here for? 👇"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏢 I'm a Channel/Group Admin", callback_data="welcome:admin")],
        [InlineKeyboardButton("🎟️ Subscribe to a Channel/Group", callback_data="welcome:subscribe")]
    ])
    if WELCOME_FILE_ID:
        try:
            await update.message.reply_photo(photo=WELCOME_FILE_ID, caption=welcome_text, parse_mode="Markdown", reply_markup=keyboard)
            return
        except Exception:
            pass
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=keyboard)


async def _show_subscription_info(update, context, uid: int, client: dict):
    packages = [p for p in (client.get("packages") or []) if not p.get("is_demo")]
    if not packages:
        await update.message.reply_text(
            "⚠️ This channel/group has no active packages yet. Contact the admin.",
            parse_mode="Markdown")
        return
    set_member_target(uid, client["client_id"])
    await send_member_welcome(context.bot, uid, client)


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lets anyone (including channel owners) join another channel/group by ref code."""
    uid = update.effective_user.id
    CLIENT_STATE.setdefault(uid, {})["step"] = STEP_REFCODE_INPUT
    await update.message.reply_text(
        "🎟️ *Subscribe to a Channel/Group*\n\n"
        "Send the *Ref Code* (or deep link) shared by the channel/group admin.\n\n"
        "It looks something like: `TECH-X7K2`",
        parse_mode="Markdown")


# ── Callback: welcome ──────────────────────────────────────────────────────────
async def callback_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    uid    = query.from_user.id
    action = query.data.split(":")[1]

    await context.bot.send_chat_action(uid, "typing")

    if action == "admin":
        existing_client = get_client(uid)
        if existing_client:
            if _client_is_active(existing_client):
                update_last_seen(uid)
            msg = client_home_text(existing_client)
            kb  = trial_offer_markup(existing_client)
            if query.message.photo:
                await query.edit_message_caption(caption=msg, parse_mode="Markdown", reply_markup=kb)
            else:
                await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)
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
        brand = text.strip()
        if not brand or len(brand) > 60:
            await update.message.reply_text("⚠️ Please send a brand name (up to 60 characters):")
            return
        state["brand_name"] = brand
        state["step"]       = STEP_BRAND_LOGO
        CLIENT_STATE[uid]   = state
        await update.message.reply_text(
            f"✅ *Brand name saved:* {escape_md(brand)}\n\n"
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
        pkg_name, price, duration = parts[0], clean_price(parts[1]), parts[2]
        if not pkg_name or price is None or not duration.isdigit() or int(duration) < 1:
            await update.message.reply_text(
                "⚠️ Price and duration must be whole numbers (duration at least 1 day).\n"
                "Example: `1 Month, 5000, 30`",
                parse_mode="Markdown")
            return
        packages = state.get("packages", [])
        packages.append({"name": pkg_name, "price": price, "duration_days": int(duration)})
        state["packages"] = packages
        CLIENT_STATE[uid] = state
        pkg_list = "\n".join([f"• {escape_md(p['name'])} — ₦{p['price']} / {format_duration(p['duration_days'])}" for p in packages])
        await update.message.reply_text(
            f"✅ *Package added!*\n\n*Your packages so far:*\n{pkg_list}\n\nAdd another or continue?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Add another", callback_data="onboard:add_pkg"),
                InlineKeyboardButton("✅ Done",         callback_data="onboard:pkg_done")
            ]]))

    elif step == STEP_REF_CODE:
        custom = text.strip().upper().replace(" ", "-")
        if not valid_ref_code(custom):
            await update.message.reply_text(
                "⚠️ Use 3–20 characters: letters, numbers or hyphens only. Try again:")
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


# ── Bot added to / changed in a channel/group ──────────────────────────────────
async def handle_bot_added(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result:
        return
    chat       = result.chat
    new_status = result.new_chat_member

    # 1) A channel/group that's already linked: if the bot lost its rights, say so right away
    linked = db.table("clients").select("client_id, brand_name").eq(
        "channel_id", chat.id).eq("removed", False).execute().data
    if linked:
        problem = _rights_problem_from_member(new_status)
        if problem:
            await alert_rights(context.bot, linked[0]["client_id"], linked[0].get("brand_name"), problem, force=True)
        else:
            # Rights are fine (maybe just given back): finish any member restore that was waiting on them
            context.application.create_task(_restore_paused_members(context.bot, linked[0]["client_id"]))
        return

    # 2) Onboarding: attach the channel to the person who actually added the bot
    if chat.type != "channel":
        return
    if not isinstance(new_status, (ChatMemberAdministrator, ChatMemberOwner)):
        return

    adder = result.from_user.id
    state = CLIENT_STATE.get(adder)
    if not state or state.get("step") != STEP_CHANNEL:
        return

    problem = _rights_problem_from_member(new_status)
    if problem:
        try:
            await context.bot.send_message(adder,
                f"⚠️ I've been added to your channel/group but: {escape_md(problem)}\n\n"
                "Please go to admin settings, find me, enable *Ban Members* and *Invite Users*, "
                "then tap *I've added the bot* again.",
                parse_mode="Markdown",
                reply_markup=_channel_keyboard())
        except Exception as e:
            logging.error(f"Could not notify client {adder}: {e}")
        return

    if channel_taken(chat.id, adder):
        try:
            await context.bot.send_message(adder,
                "⚠️ That channel is already linked to another SubPlanBot account. Contact @GizmoBrymez if this is a mistake.")
        except Exception:
            pass
        return

    state["channel_id"]   = chat.id
    state["channel_name"] = chat.title
    CLIENT_STATE[adder]   = state
    try:
        await _show_plan_selection(context.bot, adder, chat.title)
    except Exception as e:
        logging.error(f"Could not notify client {adder}: {e}")


# ── Member joins channel/group — start subscription timer ─────────────────────
async def handle_member_joined(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approved members are saved as 'pending' (expiry NULL). Their timer starts when they join.
    Also removes anyone who comes in through one of my invite links without a valid subscription
    (a forwarded link, or an old link after being removed).
    Stateless: everything comes from the database, so it survives bot restarts."""
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

    clients = db.table("clients").select("*").eq("channel_id", chat_id).eq("removed", False).execute().data
    if not clients:
        return
    client    = clients[0]
    client_id = client["client_id"]

    rows = db.table("members").select("*").eq("user_id", user_id).eq("client_id", client_id).execute().data
    row  = rows[0] if rows else None

    # My links are named user_<id of the member they were made for>. Join-request links are skipped:
    # with those, someone (me or the owner) already approved the person before they got in.
    link_owner = None
    link       = result.invite_link
    if link and not link.creates_join_request and (link.name or "").startswith("user_"):
        try:
            link_owner = int(link.name[5:])
        except ValueError:
            link_owner = None
    if link_owner and link_owner != user_id:
        await _reissue_member_link(context.bot, client, link_owner)   # their one-seat link is used up

    paused    = row.get("paused_seconds") if row and row.get("removed") else None
    owed_seat = paused is not None   # evicted when the owner lapsed and not restored yet
    if not row or not (member_is_pending(row) or owed_seat):
        if link_owner and not (row and member_is_currently_active(row)):
            await _remove_stray_joiner(context.bot, client, user_id, link_owner, row)
        return

    name     = row.get("username") or str(user_id)
    pkg_name = row.get("package")
    missing_pkg = False
    if paused:   # > 0: exactly the time they had left when the owner lapsed
        delta = timedelta(seconds=int(paused))
    else:
        pkg = next((p for p in (client.get("packages") or []) if p.get("name") == pkg_name), None)
        missing_pkg = pkg is None
        if missing_pkg:
            # Owner deleted the package after approving. Give 1 day and tell them to fix it.
            pkg = {"name": pkg_name or "Approved", "duration_days": 1}
        delta = package_delta(pkg)

    now    = datetime.now(timezone.utc)
    expiry = now + delta   # timer starts at join, not at approval

    fields = {"expiry": expiry.isoformat(), "joined_at": now.isoformat(), "removed": False}
    if owed_seat:
        fields["paused_seconds"] = None
    update_member_row(user_id, client_id, fields)

    logging.info(f"Member {user_id} joined client {client_id} — expiry {expiry.isoformat()}")
    try:
        extra = ("\n⚠️ Their package no longer exists, so I gave them 1 day. "
                 "Approve them again on the right package to fix it.") if missing_pkg else ""
        await context.bot.send_message(client_id,
            f"✅ {escape_md(name)} joined — timer started. Expires `{expiry.strftime('%Y-%m-%d %H:%M UTC')}`.{extra}",
            parse_mode="Markdown")
    except Exception:
        pass


async def _reissue_member_link(bot, client: dict, intended_id: int):
    """Someone else used a member's one-seat link. If that member still hasn't joined, send a new one."""
    rows = db.table("members").select("expiry, removed").eq(
        "user_id", intended_id).eq("client_id", client["client_id"]).execute().data
    if not rows or not member_is_pending(rows[0]):
        return
    try:
        link = await create_member_invite(bot, client["channel_id"], intended_id)
    except Exception as e:
        logging.error(f"Couldn't reissue an invite link for {intended_id}: {e}")
        return
    try:
        await bot.send_message(intended_id,
            "⚠️ *Someone else joined with your invite link*, so here's a new one, just for you.\n\n"
            f"👇 Tap below to join. Your timer starts when you enter:\n{escape_md(link)}",
            parse_mode="Markdown")
    except Exception:
        name = await resolve_name(bot, intended_id)
        try:
            await bot.send_message(client["client_id"],
                f"⚠️ Someone used {escape_md(name)}'s invite link, and I couldn't DM them a new one. "
                f"Please send them this link:\n{escape_md(link)}",
                parse_mode="Markdown")
        except Exception:
            pass


async def _remove_stray_joiner(bot, client: dict, user_id: int, link_owner: int, row):
    """Someone came in through one of my invite links without a valid subscription. Remove them."""
    client_id = client["client_id"]
    try:
        await bot.ban_chat_member(client["channel_id"], user_id)
        await bot.unban_chat_member(client["channel_id"], user_id)
    except Exception as e:
        logging.error(f"Couldn't remove stray joiner {user_id} from client {client_id}: {e}")
        if looks_like_rights_error(e):
            await alert_rights(bot, client_id, client.get("brand_name"),
                               "I couldn't remove someone who joined without a subscription. " + str(e)[:150])
        return
    if row and not row.get("removed"):   # expired but not kicked yet: record it now
        try:
            update_member_row(user_id, client_id, {"removed": True})
        except Exception as e:
            logging.error(f"Couldn't mark stray joiner {user_id} removed: {e}")

    name = await resolve_name(bot, user_id)
    if link_owner == user_id:
        why = "their access had ended, and they used an old invite link"
    else:
        why = f"they used the invite link I made for {await resolve_name(bot, link_owner)}"
    try:
        await bot.send_message(client_id, f"🚫 Removed {escape_md(name)}: {escape_md(why)}.",
                               parse_mode="Markdown")
    except Exception:
        pass

    # Point them at this channel so /pay shows the right payment details
    CLIENT_STATE.setdefault(user_id, {})["subscribing_to_client_id"] = client_id
    brand = escape_md(client.get("brand_name") or "the channel/group")
    try:
        await bot.send_message(user_id,
            f"🚫 *You've been removed from {brand}.*\n\n"
            "That invite link wasn't valid for you. To get access, use /pay for payment details "
            "and send your receipt here.",
            parse_mode="Markdown")
    except Exception:
        pass


async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """For channels with 'Approve New Members' on: auto-approve people who've been approved/paid.
    Everyone else stays pending for the owner to decide."""
    req = update.chat_join_request
    if not req:
        return
    clients = db.table("clients").select("client_id").eq("channel_id", req.chat.id).eq("removed", False).execute().data
    if not clients:
        return
    rows = db.table("members").select("*").eq(
        "user_id", req.from_user.id).eq("client_id", clients[0]["client_id"]).execute().data
    r = rows[0] if rows else None
    owed_seat = bool(r and r.get("removed") and r.get("paused_seconds") is not None)
    if r and (member_is_pending(r) or member_is_currently_active(r) or owed_seat):
        try:
            await req.approve()
        except Exception as e:
            logging.error(f"Join request approve failed for {req.from_user.id}: {e}")


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
        problem = await bot_rights_problem(context.bot, found_id)
        if problem:
            await context.bot.send_message(uid,
                f"⚠️ {escape_md(problem)}\n\n"
                "Please fix it in your channel/group admin settings, then tap *I've added the bot* again.",
                parse_mode="Markdown", reply_markup=RETRY_KB)
            return
        try:
            await query.edit_message_text(
                f"✅ *Channel/group confirmed:* {escape_md(found_name)}\n\nNow choose how to get started 👇",
                parse_mode="Markdown")
        except Exception:
            pass
        await _show_plan_selection(context.bot, uid, found_name)

    elif action == "trial":
        existing    = get_client(uid)
        test_expiry = datetime.now(timezone.utc) + timedelta(days=1)
        if existing and existing.get("plan") != "pending":   # double-tap, or already set up
            await context.bot.send_message(uid, "ℹ️ Your account is already set up. Use /start to see your dashboard.")
            return
        if existing:
            # Tapped Make Payment first, then chose the test. Their setup is already saved.
            try:
                res = db.table("clients").update({"plan": "test", "expiry": test_expiry.isoformat()}).eq(
                    "client_id", uid).eq("plan", "pending").execute()
            except Exception as e:
                logging.error(f"Pending -> test switch failed for {uid}: {e}")
                res = None
            if res is not None and not res.data:   # payment got approved in the meantime
                await context.bot.send_message(uid, "ℹ️ Your account is already set up. Use /start to see your dashboard.")
                return
            ref_code = existing.get("ref_code") if res is not None else None
        else:
            if not state.get("channel_id"):  # setup state lost (bot restarted mid-setup)
                await context.bot.send_message(uid,
                    "⚠️ Your setup session expired. Please tap /start and run through setup again. It only takes a minute.")
                return
            ref_code = _save_new_client(uid, query.from_user.username, state, plan="test", expiry=test_expiry)
        if not ref_code:
            await context.bot.send_message(uid, "⚠️ Couldn't activate your test environment. Please contact @GizmoBrymez.")
            return
        CLIENT_STATE.pop(uid, None)
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
            "💡 The test member's timer starts when they join, and they're removed 5 minutes later."
        )
        try:
            if query.message.photo:
                await query.edit_message_caption(caption=sandbox_msg, parse_mode="Markdown")
            else:
                await query.edit_message_text(sandbox_msg, parse_mode="Markdown")
        except Exception:
            await context.bot.send_message(uid, sandbox_msg, parse_mode="Markdown")

    elif action == "pay":
        if not get_client(uid):
            if not state.get("channel_id"):
                await context.bot.send_message(uid,
                    "⚠️ Your setup session expired. Please tap /start and run through setup again. It only takes a minute.")
                return
            # Save now (as 'pending') so a restart before approval can't lose their setup
            ok = _save_new_client(uid, query.from_user.username, state, plan="pending",
                                  expiry=datetime.now(timezone.utc))
            if not ok:
                await context.bot.send_message(uid, "⚠️ Something went wrong. Please contact @GizmoBrymez.")
                return
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
        set_pay_target(uid, "platform")
        trial_kb = trial_offer_markup(get_client(uid))   # still pending: they can take the test instead
        try:
            if query.message.photo:
                await query.edit_message_caption(caption=pay_msg, parse_mode="Markdown", reply_markup=trial_kb)
            else:
                await query.edit_message_text(pay_msg, parse_mode="Markdown", reply_markup=trial_kb)
        except Exception:
            await context.bot.send_message(uid, pay_msg, parse_mode="Markdown", reply_markup=trial_kb)


def _save_new_client(uid, username, state, plan, expiry, ref_code=None):
    """Insert the client. Returns the ref code it was saved with, or None on failure."""
    username_str = f"@{username}" if username else str(uid)
    # Keep the code the owner confirmed during onboarding: they may have shared that link already
    ref_code = ref_code or state.get("ref_code")
    if ref_code and db.table("clients").select("client_id").eq("ref_code", ref_code).neq(
            "client_id", uid).execute().data:
        ref_code = None   # someone else took it in the meantime
    ref_code = ref_code or generate_ref_code(state.get("brand_name", "SPB"))
    packages = list(state.get("packages", []))
    packages.insert(0, {"name": DEMO_PKG_NAME, "price": "0", "duration_days": 0, "duration_minutes": 5, "is_demo": True})
    data = {
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
    }
    try:
        result = db.table("clients").insert(data).execute()
        logging.info(f"Client insert success: {result}")
        return ref_code
    except Exception as e:
        logging.exception(f"Client insert FAILED for {uid}: {e}")
        return None


# ── Ref code input ─────────────────────────────────────────────────────────────
async def handle_refcode_input(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    raw = (update.message.text or "").strip()
    if "start=" in raw:                      # they pasted the whole deep link
        raw = raw.split("start=")[-1]
    ref_code = raw.upper()

    await context.bot.send_chat_action(uid, "typing")

    client = get_client_by_ref(ref_code)
    if not client:
        await update.message.reply_text(
            "❌ *Invalid Ref Code.*\n\n"
            "Please double-check the code from your channel/group admin and try again.\n"
            "_(It looks like: `TECH-X7K2`)_",
            parse_mode="Markdown")
        return

    if client["client_id"] == uid:
        CLIENT_STATE.setdefault(uid, {})["step"] = None
        await update.message.reply_text("ℹ️ That's your own channel/group's ref code. Use someone else's to subscribe.")
        return

    if not is_active_client(client["client_id"]):
        await update.message.reply_text(
            "⚠️ This channel/group's subscription is currently *inactive*.\n\n"
            "Please contact the admin for assistance.",
            parse_mode="Markdown")
        CLIENT_STATE.setdefault(uid, {})["step"] = None
        return

    packages = [p for p in (client.get("packages") or []) if not p.get("is_demo")]
    if not packages:
        await update.message.reply_text(
            "⚠️ This channel/group has no active packages yet. Contact the admin.",
            parse_mode="Markdown")
        CLIENT_STATE.setdefault(uid, {})["step"] = None
        return

    set_member_target(uid, client["client_id"])
    await send_member_welcome(context.bot, uid, client)


# ── /pay ──────────────────────────────────────────────────────────────────────
async def _send_pay_info(message, context, uid: int, target):
    """Payment details for either the platform ('platform') or a channel owner (client_id)."""
    if target == "platform":
        existing = get_client(uid)
        title = "Activate" if (not existing or existing.get("plan") in ("pending", "test")) else "Renew"
        caption = (
            f"💳 *{title} Your SubPlanBot Subscription*\n\n"
            f"💰 Amount: ₦{PLATFORM_PRICE}/month\n\n"
            f"🏦 Bank: {PLATFORM_BANK_NAME}\n"
            f"💰 Account Number: `{PLATFORM_ACCT_NUMBER}`\n"
            f"👤 Account Name: {PLATFORM_ACCT_NAME}\n\n"
            "📸 Make payment and send your receipt here.\n"
            "Our team will confirm it shortly ✅\n\n"
            "❓ Got questions? Reach out to @GizmoBrymez"
        )
        await send_media_or_text(context.bot, uid, FLYER_FILE_ID, caption,
                                 reply_markup=trial_offer_markup(existing))
        return

    client = get_client(int(target))
    if not client or not is_active_client(client["client_id"]):
        await message.reply_text(
            "⚠️ This channel/group isn't accepting payments right now. Please contact the admin.")
        return
    await _send_member_payment_info(message, client)


async def cmd_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await context.bot.send_chat_action(uid, "typing")

    if uid in ADMIN_IDS:
        await update.message.reply_text("👑 You're a platform admin. No payment needed!")
        return

    existing_client = get_client(uid)
    target          = get_pay_target(uid)
    member_cid      = resolve_member_client(uid)

    if target is None:
        if existing_client and member_cid:
            # Channel owner who is also a member somewhere: ask which one
            other  = get_client(member_cid)
            brand  = (other or {}).get("brand_name") or "my other subscription"
            await update.message.reply_text(
                "💳 What are you paying for?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 My SubPlanBot plan", callback_data="paytarget:platform")],
                    [InlineKeyboardButton(f"🎟️ {brand[:40]}",      callback_data=f"paytarget:{member_cid}")],
                ]))
            return
        target = "platform" if existing_client else member_cid

    if target is None:
        await update.message.reply_text(
            "⚠️ You're not linked to a channel/group yet.\n\n"
            "Use /start and select *Subscribe to a Channel/Group* to get started.",
            parse_mode="Markdown")
        return

    set_pay_target(uid, target)
    await _send_pay_info(update.message, context, uid, target)


async def callback_paytarget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    raw   = query.data.split(":")[1]
    target = "platform" if raw == "platform" else int(raw)
    set_pay_target(uid, target)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _send_pay_info(query.message, context, uid, target)


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
    now = datetime.now(timezone.utc)
    blocks = []

    # A channel owner's own SubPlanBot plan
    client = get_client(uid)
    if client:
        expiry_dt = datetime.fromisoformat(client["expiry"])
        if client.get("plan") == "pending":
            status = "⏳ Awaiting payment approval"
        elif client.get("suspended"):
            status = "🚫 Suspended"
        elif not client.get("removed") and expiry_dt > now:
            status = "✅ Active" if client.get("plan") != "test" else "🧪 Test (active)"
        else:
            status = "❌ Expired"
        blocks.append(
            f"👑 *Your SubPlanBot Plan*\n"
            f"🔰 Status: {status}\n"
            f"📅 Expires: {expiry_dt.strftime('%b %d, %Y')}\n"
            f"⏳ Time left: {format_expiry_countdown(client['expiry'])}\n"
            f"🔑 Ref Code: `{client.get('ref_code', 'N/A')}`\n")

    # Subscriptions they hold as a member of other channels/groups
    rows = db.table("members").select("client_id, package, expiry, removed").eq("user_id", uid).eq("removed", False).execute().data
    for row in rows:
        c_info = get_client(row["client_id"])
        brand  = escape_md(c_info["brand_name"]) if c_info and c_info.get("brand_name") else "Unknown"
        if not row.get("expiry"):
            status, expiry_str, countdown = "⏳ Approved — join to start your timer", "N/A", "N/A"
        else:
            expiry_dt  = datetime.fromisoformat(row["expiry"])
            status     = "✅ Active" if expiry_dt > now else "❌ Expired"
            expiry_str = expiry_dt.strftime("%b %d, %Y")
            countdown  = format_expiry_countdown(row["expiry"])
        blocks.append(
            f"📢 *{brand}*\n"
            f"📦 Plan: {escape_md(row.get('package') or 'N/A')}\n"
            f"🔰 Status: {status}\n"
            f"📅 Expires: {expiry_str}\n"
            f"⏳ Time left: {countdown}\n")

    if not blocks:
        await update.message.reply_text("ℹ️ You don't have an active subscription.", parse_mode="Markdown")
        return
    await update.message.reply_text("📊 *Your Subscription Status*\n\n" + "\n".join(blocks), parse_mode="Markdown")


# ── Receipts ───────────────────────────────────────────────────────────────────
async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid  = user.id
    await context.bot.send_chat_action(uid, "typing")

    target = get_pay_target(uid)
    if target is None:
        existing_client = get_client(uid)
        member_cid      = resolve_member_client(uid)
        if existing_client and member_cid:
            # Owner who is also a member somewhere: don't guess where the money goes
            mid = update.message.message_id
            await update.message.reply_text(
                "📨 Who is this payment for?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 My SubPlanBot plan",               callback_data=f"rcpt:platform:{mid}")],
                    [InlineKeyboardButton("🎟️ A channel/group I subscribed to",  callback_data=f"rcpt:{member_cid}:{mid}")],
                ]))
            return
        target = "platform" if existing_client else member_cid

    if target is None:
        await update.message.reply_text(
            "⚠️ *You're not linked to a channel/group yet.*\n\n"
            "Use /start → *Subscribe to a Channel/Group* (or open your admin's deep link), "
            "then send your receipt again so it reaches the right admin.",
            parse_mode="Markdown")
        return

    await _deliver_receipt(context, user, update.effective_chat.id, update.message.message_id, target)


async def callback_rcpt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-who-is-also-a-member picked where a receipt should go."""
    query  = update.callback_query
    await query.answer()
    parts  = query.data.split(":")
    target = "platform" if parts[1] == "platform" else int(parts[1])
    msg_id = int(parts[2])
    uid    = query.from_user.id
    set_pay_target(uid, target)
    try:
        await query.edit_message_text("📨 Sending your receipt…")
    except Exception:
        pass
    await _deliver_receipt(context, query.from_user, uid, msg_id, target)


async def _deliver_receipt(context, user, chat_id: int, message_id: int, target):
    bot      = context.bot
    uid      = user.id
    raw_name = f"@{user.username}" if user.username else (user.first_name or str(uid))

    # ── Channel owner paying SubPlanBot ──
    if target == "platform":
        key = ("c", uid)
        if recently_approved(key, 120):
            await bot.send_message(chat_id, "✅ Your previous payment was just approved. You're all set!")
            return
        existing = get_client(uid)
        action   = "activate" if (not existing or existing.get("plan") in ("pending", "test")) else "renew"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"client_approve:{uid}"),
            InlineKeyboardButton("❌ Deny",    callback_data=f"client_deny:{uid}")
        ]])
        delivered = 0
        for admin in ADMIN_IDS:
            try:
                await bot.forward_message(admin, chat_id, message_id)
                sent = await bot.send_message(admin,
                    f"💳 *Client payment receipt*\n\n"
                    f"👤 {escape_md(raw_name)} wants to {action} their SubPlanBot subscription.",
                    parse_mode="Markdown", reply_markup=keyboard)
                register_notice(key, admin, sent.message_id)
                delivered += 1
            except Exception as e:
                logging.error(f"Failed to notify platform admin {admin}: {e}")
        if delivered:
            await bot.send_message(chat_id,
                "✅ *Receipt received!* 📩\n\nOur team will review shortly. You'll hear back once confirmed.",
                parse_mode="Markdown")
        else:
            await bot.send_message(chat_id, "⚠️ We couldn't deliver your receipt right now. Please contact @GizmoBrymez directly.")
        return

    # ── Member paying a channel owner ──
    client_id = int(target)
    client    = get_client(client_id)
    if not client or not is_active_client(client_id):
        await bot.send_message(chat_id,
            "⚠️ This channel/group isn't accepting payments right now. Please contact the admin.")
        return

    key = ("m", client_id, uid)
    if recently_approved(key):
        await bot.send_message(chat_id,
            "✅ Your previous receipt was just approved. Check your messages for your invite link or extension.")
        return

    rows = db.table("members").select("expiry, removed").eq("user_id", uid).eq("client_id", client_id).execute().data
    if rows and member_is_pending(rows[0]):
        await bot.send_message(chat_id,
            "✅ *You're already approved!*\n\n"
            "Use the invite link I sent you to join. Your timer starts when you enter. "
            f"Lost the link? Contact {escape_md(client.get('username') or 'the admin')}.",
            parse_mode="Markdown")
        return

    admin_u  = escape_md(client.get("username") or "the admin")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"member_approve:{uid}:{client_id}"),
        InlineKeyboardButton("❌ Deny",    callback_data=f"member_deny:{uid}:{client_id}")
    ]])
    try:
        await bot.forward_message(client_id, chat_id, message_id)
        sent = await bot.send_message(client_id,
            f"💳 *New Payment Receipt!*\n\n"
            f"👤 {escape_md(raw_name)} just sent a payment receipt for *{escape_md(client.get('brand_name') or '')}*.",
            parse_mode="Markdown", reply_markup=keyboard)
        register_notice(key, client_id, sent.message_id)
    except Exception as e:
        logging.error(f"Failed to notify client {client_id} of receipt: {e}")
        await bot.send_message(chat_id,
            f"⚠️ We couldn't reach the admin automatically. Please DM {admin_u} your receipt directly.",
            parse_mode="Markdown")
        return

    await bot.send_message(chat_id,
        f"✅ *Receipt received!* 📩\n\n"
        f"The admin ({admin_u}) will review and send your invite link within 2 hours. "
        f"If you don't hear back, contact them with your payment details.",
        parse_mode="Markdown")


# ── Member approve ─────────────────────────────────────────────────────────────
# Callback formats (no names, so they can never overflow Telegram's 64-byte limit):
#   member_approve:{user_id}:{client_id}
#   member_deny:{user_id}:{client_id}
#   member_pkg:{user_id}:{pkg_idx}:{client_id}:{pkg_tag}   (the tag catches a package list that changed)
def _package_picker(client: dict, user_id: int, name: str, row=None, note: str = ""):
    """Text + buttons for choosing which package to approve a member on."""
    client_id = client["client_id"]
    packages  = client.get("packages") or []
    current   = ""
    if row and member_is_currently_active(row):
        current = (f"📌 *Currently on:* {escape_md(row.get('package') or '?')} "
                   f"({format_expiry_countdown(row['expiry'])} left)\n\n")
    is_test  = client.get("plan") == "test"
    keyboard = []
    for i, p in enumerate(packages):
        is_demo = p.get("is_demo", False)
        label   = (
            f"{p['name']} — {'Free' if is_demo else '₦' + str(p['price'])} "
            f"({format_duration(0 if is_demo else p['duration_days'])})"
        )
        if is_test and not is_demo:
            keyboard.append([InlineKeyboardButton(f"🔒 {label} (test mode only)", callback_data=f"noop:{i}")])
        else:
            keyboard.append([InlineKeyboardButton(
                label, callback_data=f"member_pkg:{user_id}:{i}:{client_id}:{pkg_tag(p)}")])
    text = (f"{note}{current}📦 Select package for {escape_md(name)}:"
            + ("\n\n_🔒 Custom packages are disabled during the Test Environment._" if is_test else ""))
    return text, InlineKeyboardMarkup(keyboard)


async def callback_member_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query     = update.callback_query
    await query.answer()
    parts     = query.data.split(":")
    user_id   = int(parts[1])
    client_id = int(parts[2])

    if query.from_user.id != client_id and query.from_user.id not in ADMIN_IDS:
        return

    key = ("m", client_id, user_id)
    if recently_approved(key):
        await strip_buttons(context.bot, key)
        await query.edit_message_text("✅ Already approved a moment ago. Nothing more to do.")
        return

    await context.bot.send_chat_action(query.from_user.id, "typing")
    client = get_client(client_id)
    if not client or not is_active_client(client_id):
        await query.edit_message_text("⚠️ This account isn't active right now (expired or suspended), so I can't approve members.")
        return

    packages = client.get("packages") or []
    if not packages:
        await query.edit_message_text("⚠️ No packages set up. Use /settings to add packages.")
        return

    name     = await resolve_name(context.bot, user_id)
    existing = db.table("members").select("package, expiry, removed").eq(
        "user_id", user_id).eq("client_id", client_id).execute().data

    if existing and member_is_pending(existing[0]):
        await strip_buttons(context.bot, key)
        await query.edit_message_text(
            f"✅ {escape_md(name)} was already approved and hasn't joined yet. Nothing more to do.",
            parse_mode="Markdown")
        return

    text, keyboard = _package_picker(client, user_id, name, existing[0] if existing else None)
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)


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
    tag       = parts[4] if len(parts) > 4 else None   # buttons sent before this update have no tag

    if query.from_user.id != client_id and query.from_user.id not in ADMIN_IDS:
        return

    key = ("m", client_id, user_id)
    if key in IN_FLIGHT:          # double-tap while the first one is still running
        return
    if recently_approved(key):
        await strip_buttons(context.bot, key)
        await query.edit_message_text("✅ Already approved a moment ago. Nothing more to do.")
        return

    IN_FLIGHT.add(key)
    try:
        await context.bot.send_chat_action(query.from_user.id, "typing")
        await _approve_member_package(query, context, user_id, pkg_idx, client_id, tag)
    finally:
        IN_FLIGHT.discard(key)


async def _approve_member_package(query, context, user_id: int, pkg_idx: int, client_id: int, tag=None):
    key    = ("m", client_id, user_id)
    client = get_client(client_id)
    if not client or not is_active_client(client_id):
        await query.edit_message_text("⚠️ This account isn't active right now (expired or suspended), so I can't approve members.")
        return

    packages = client.get("packages") or []
    if not packages:
        await query.edit_message_text("⚠️ No packages set up. Use /settings to add packages.")
        return

    name     = await resolve_name(context.bot, user_id)
    existing = db.table("members").select("package, expiry, removed").eq(
        "user_id", user_id).eq("client_id", client_id).execute().data
    row = existing[0] if existing else None

    # The package list changed after these buttons were drawn (one was deleted): redraw, don't guess
    pkg = packages[pkg_idx] if 0 <= pkg_idx < len(packages) else None
    if pkg is None or (tag is not None and pkg_tag(pkg) != tag):
        text, keyboard = _package_picker(client, user_id, name, row,
            note="⚠️ Your packages changed since this list was shown, so nothing was approved. Pick again:\n\n")
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    now        = datetime.now(timezone.utc)
    delta      = package_delta(pkg)
    channel_id = client.get("channel_id")

    # Already approved but hasn't joined: decline, don't issue a second invite
    if row and member_is_pending(row):
        await strip_buttons(context.bot, key)
        await query.edit_message_text(
            f"✅ {escape_md(name)} was already approved and hasn't joined yet. Nothing more to do.",
            parse_mode="Markdown")
        return

    # "subscribed" = has a timer (active, or lapsed but not yet kicked). Still in the channel → just extend.
    subscribed = bool(row and not row.get("removed") and row.get("expiry"))
    link   = None
    expiry = None

    # 1) Invite link FIRST. Nothing is saved if this fails, so retrying is always safe.
    try:
        if subscribed:
            base   = datetime.fromisoformat(row["expiry"])
            expiry = (base if base > now else now) + delta
            if not await user_in_chat(context.bot, channel_id, user_id):   # left on their own → send a fresh link
                link = await create_member_invite(context.bot, channel_id, user_id)
        else:
            link = await create_member_invite(context.bot, channel_id, user_id)
    except Exception as e:
        if looks_like_rights_error(e):
            await alert_rights(context.bot, client_id, client.get("brand_name"),
                               "I couldn't create an invite link. I need the Invite Users permission.")
        await query.edit_message_text(
            "⚠️ Couldn't generate an invite link — *nothing was saved*.\n\n"
            f"`{e}`\n\n"
            "Make sure I'm an admin with *Invite Users* permission, then pick the package again.",
            parse_mode="Markdown",
            reply_markup=query.message.reply_markup)
        return

    # 2) Save
    try:
        if subscribed:
            db.table("members").update({
                "expiry": expiry.isoformat(), "package": pkg["name"],
                "removed": False, "username": name,
            }).eq("user_id", user_id).eq("client_id", client_id).execute()
        elif row:    # previously removed → pending again; timer starts when they join
            # paused_seconds cleared: a fresh payment replaces anything saved from an owner lapse
            update_member_row(user_id, client_id, {
                "expiry": None, "package": pkg["name"], "removed": False,
                "username": name, "joined_at": None, "paused_seconds": None,
            })
        else:
            db.table("members").insert({
                "user_id": user_id, "client_id": client_id, "username": name,
                "package": pkg["name"], "expiry": None,
                "added_at": now.isoformat(), "removed": False,
            }).execute()
    except Exception as db_err:
        logging.error(f"Member save failed: {db_err}")
        await query.edit_message_text(
            f"⚠️ Database error. *Nothing was sent or saved.*\n\n`{db_err}`\n\n"
            "If this mentions `expiry` and NOT NULL, run in Supabase:\n"
            "`ALTER TABLE members ALTER COLUMN expiry DROP NOT NULL;`",
            parse_mode="Markdown",
            reply_markup=query.message.reply_markup)
        return

    # Saved: from here on, repeat attempts are declined and other receipts' buttons disappear
    mark_approved(key)
    await strip_buttons(context.bot, key)

    # 3) Tell the member + the owner
    pkg_label = escape_md(pkg["name"])
    try:
        if subscribed and link is None:
            await context.bot.send_message(user_id,
                f"🎉 *Subscription Extended!*\n\n"
                f"Extended by *{pkg_label}*.\n"
                f"New expiry: `{expiry.strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
                "You do not need to rejoin. Keep enjoying the content! 🙌",
                parse_mode="Markdown")
        elif subscribed:
            await context.bot.send_message(user_id,
                f"🎉 *Subscription Extended!*\n\n"
                f"Extended by *{pkg_label}*.\n"
                f"New expiry: `{expiry.strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
                f"You're not in the channel/group right now. Tap below to rejoin:\n{escape_md(link)}",
                parse_mode="Markdown")
        else:
            await send_member_welcome(context.bot, user_id, client)
            await context.bot.send_message(user_id,
                f"🎉 *Payment Approved!*\n\n"
                f"Package: *{pkg_label}*\n\n"
                f"👇 Tap below to join — your timer starts when you enter:\n{escape_md(link)}",
                parse_mode="Markdown")
            if pkg.get("is_demo"):
                await context.bot.send_message(client_id,
                    "🎊 *Invite sent to your test member!*\n\n"
                    "Their timer starts when they *join* the channel/group "
                    "(≈5 minutes of access after joining).\n\n"
                    "Ready to go live? Use /pay to activate your full monthly plan 💳",
                    parse_mode="Markdown")
        await query.edit_message_text(
            f"✅ {escape_md(name)} {'extended' if subscribed else 'approved'} on *{pkg_label}*."
            + ("" if subscribed else " Invite sent — timer starts when they join."),
            parse_mode="Markdown")
    except Exception as e:
        # Already saved: do NOT approve again. Hand the owner the link to send manually.
        extra = f"\n\nSend them this link manually:\n{escape_md(link)}" if link else ""
        await query.edit_message_text(
            f"✅ *Already saved* — do not tap Approve again.\n\n"
            f"Couldn't DM {escape_md(name)}:\n`{e}`{extra}",
            parse_mode="Markdown")


async def callback_member_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query     = update.callback_query
    await query.answer()
    parts     = query.data.split(":")
    user_id   = int(parts[1])
    client_id = int(parts[2])
    if query.from_user.id != client_id and query.from_user.id not in ADMIN_IDS:
        return
    key = ("m", client_id, user_id)
    if recently_approved(key):
        await strip_buttons(context.bot, key)
        await query.edit_message_text("✅ This payment was already approved a moment ago, so it can't be denied.")
        return
    name = await resolve_name(context.bot, user_id)
    ADMIN_STATE[query.from_user.id] = {
        "action": "member_deny", "user_id": user_id,
        "username": name, "client_id": client_id
    }
    await query.edit_message_text(f"✏️ Type your reason for denying {name}:")


# ── Client approve/deny (platform admins) ──────────────────────────────────────
async def callback_client_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return

    client_id = int(query.data.split(":")[1])
    key       = ("c", client_id)
    if key in IN_FLIGHT:
        return
    if recently_approved(key, 120):
        await strip_buttons(context.bot, key)
        await query.edit_message_text("✅ Already approved a moment ago. Nothing more to do.")
        return

    IN_FLIGHT.add(key)
    try:
        await context.bot.send_chat_action(query.from_user.id, "typing")
        name     = await resolve_name(context.bot, client_id)
        now      = datetime.now(timezone.utc)
        existing = get_client(client_id)
        state    = CLIENT_STATE.get(client_id, {})

        if existing:
            # Stack onto an existing PAID period only; trial/pending time isn't carried over
            base     = datetime.fromisoformat(existing["expiry"]) if existing.get("plan") == "paid" else now
            expiry   = (base if base > now else now) + timedelta(days=30)
            ref_code = existing.get("ref_code") or generate_ref_code(existing.get("brand_name", "SPB"))
            db.table("clients").update({
                "expiry": expiry.isoformat(), "plan": "paid",
                "removed": False, "ref_code": ref_code
            }).eq("client_id", client_id).execute()
        else:
            if not state.get("brand_name"):
                await query.edit_message_text(
                    f"⚠️ No saved setup for {escape_md(name)} (the bot restarted before they finished). "
                    "Ask them to /start again, then resend the receipt.",
                    parse_mode="Markdown")
                return
            expiry   = now + timedelta(days=30)
            ref_code = _save_new_client(client_id, name.lstrip("@"), state, plan="paid", expiry=expiry)
            if not ref_code:
                await query.edit_message_text("⚠️ Database insert failed. Check the logs. Nothing was approved.")
                return
            CLIENT_STATE.pop(client_id, None)

        mark_approved(key)
        await strip_buttons(context.bot, key)

        try:
            await context.bot.send_message(client_id,
                f"🎉 *Payment Approved!*\n\n"
                f"✅ Your SubPlanBot subscription is active until `{expiry.strftime('%b %d, %Y')}`\n\n"
                f"🔑 *Your Ref Code:* `{ref_code}`\n"
                f"🔗 *Deep Link:* `{ref_deep_link(ref_code)}`\n"
                "_Share these with your members so they can subscribe instantly._\n\n"
                "❓ Got questions? Reach out to @GizmoBrymez",
                parse_mode="Markdown")
            await query.edit_message_text(
                f"✅ {escape_md(name)} approved. Subscription active until {expiry.strftime('%b %d, %Y')}.",
                parse_mode="Markdown")
        except Exception as e:
            await query.edit_message_text(
                f"✅ *Saved* for {escape_md(name)} until {expiry.strftime('%b %d, %Y')}, "
                f"but DM failed:\n`{e}`",
                parse_mode="Markdown")

        if existing:
            # Bring back members evicted when this owner's plan lapsed (does nothing if there are none).
            # Runs in the background so a big channel doesn't hold up the bot.
            context.application.create_task(_restore_paused_members(context.bot, client_id))
    finally:
        IN_FLIGHT.discard(key)


async def callback_client_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    client_id = int(query.data.split(":")[1])
    key = ("c", client_id)
    if recently_approved(key, 120):
        await strip_buttons(context.bot, key)
        await query.edit_message_text("✅ This payment was already approved a moment ago, so it can't be denied.")
        return
    name = await resolve_name(context.bot, client_id)
    ADMIN_STATE[query.from_user.id] = {
        "action": "client_deny", "user_id": client_id, "username": name
    }
    await query.edit_message_text(f"✏️ Type your reason for denying {name}:")


# ── Text handler ───────────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id

    # Only process channel verification if they are NOT a known client
    if not is_client(uid):
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
                    f"❌ *Payment Denied*\n\nReason: _{escape_md(text)}_\n\n"
                    "Contact your channel/group admin for help.",
                    parse_mode="Markdown")
                await update.message.reply_text(f"✅ Reason sent to {s['username']}.")
            except Exception:
                await update.message.reply_text(f"⚠️ Couldn't DM {s['username']}.")
            await strip_buttons(context.bot, ("m", s["client_id"], s["user_id"]))
            return
        if a == "client_deny":
            s = ADMIN_STATE.pop(uid)
            try:
                await context.bot.send_message(s["user_id"],
                    f"❌ *Payment Denied*\n\nReason: _{escape_md(text)}_\n\n"
                    "Contact @GizmoBrymez for help.",
                    parse_mode="Markdown")
                await update.message.reply_text(f"✅ Reason sent to {s['username']}.")
            except Exception:
                await update.message.reply_text(f"⚠️ Couldn't DM {s['username']}.")
            await strip_buttons(context.bot, ("c", s["user_id"]))
            return
        if a == "edit_ref":
            custom = text.strip().upper().replace(" ", "-")
            if not valid_ref_code(custom):
                await update.message.reply_text("⚠️ Use 3–20 characters: letters, numbers or hyphens only. Try again:")
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
                f"✅ Brand name updated to: *{escape_md(text.strip())}*", parse_mode="Markdown")
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
            price = clean_price(text)
            if price is None:
                await update.message.reply_text("⚠️ Please enter the price as a whole number (e.g. 5000)")
                return
            state["new_pkg_price"] = price
            state["step"]          = SETTINGS_ADD_PKG_DUR
            CLIENT_STATE[uid]      = state
            await update.message.reply_text(
                "📅 How many days does this package last? _(e.g. 30)_",
                parse_mode="Markdown")
            return
        elif step == SETTINGS_ADD_PKG_DUR:
            if not text.strip().isdigit() or int(text.strip()) < 1:
                await update.message.reply_text("⚠️ Please enter a number of days, at least 1 (e.g. 30)")
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
                f"✅ Package *{escape_md(state['new_pkg_name'])}* added! 🎉\n\nUse /settings to manage all packages.",
                parse_mode="Markdown")
            return

    c = get_client(uid)
    if c and c.get("plan") != "pending":
        if c.get("suspended"):
            await update.message.reply_text(client_home_text(c), parse_mode="Markdown")
            return
        if not _client_is_active(c):
            await update.message.reply_text(
                "⚠️ *Your SubPlanBot subscription has expired.*\n\nUse /pay to renew.\n\n"
                "❓ Got questions? Reach out to @GizmoBrymez",
                parse_mode="Markdown")
            return

    await update.message.reply_text(
        "⚠️ We only accept a *screenshot* or *PDF* as payment proof.\n\n"
        "Go to your bank app, screenshot the successful transaction, and send it here. 📸",
        parse_mode="Markdown")


# ── Photo / PDF handler ────────────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # ADMIN BYPASS: platform admins get file IDs, never receipts
    if uid in ADMIN_IDS:
        await handle_getfileid_media(update, context)
        return

    if await process_channel_verification(update, context, uid):
        return

    state = CLIENT_STATE.get(uid, {})
    step  = state.get("step")
    if update.message.photo and step in [STEP_BRAND_LOGO, STEP_FLYER, SETTINGS_EDIT_BRAND_LOGO, SETTINGS_EDIT_FLYER]:
        await handle_onboarding_photo(update, context, uid)
        return

    await handle_receipt(update, context)


# ── Forwarded messages ─────────────────────────────────────────────────────────
async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # ADMIN BYPASS: platform admin forwards media to get an ID
    if uid in ADMIN_IDS and (update.message.photo or update.message.document):
        await handle_getfileid_media(update, context)
        return

    # Only process channel verification if they are NOT a known client
    if not is_client(uid):
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
            pkg_text += f"  • {escape_md(p['name'])} — Free (5 mins after joining)\n"
        pkg_text += "\n"
    if custom_pkgs:
        pkg_text += "*📦 Your Packages:*\n"
        for p in custom_pkgs:
            pkg_text += f"  • {escape_md(p['name'])} — ₦{p['price']} / {format_duration(p['duration_days'])}\n"
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
        f"⚙️ *Settings — {escape_md(client.get('brand_name') or '')}*\n\n"
        f"🏦 Bank: {escape_md(client.get('bank_name') or '—')}\n"
        f"💳 Account: `{client.get('account_number') or '—'}`\n"
        f"👤 Name: {escape_md(client.get('account_name') or '—')}\n"
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
            callback_data=f"del_pkg:{uid}:{i}:{pkg_tag(p)}"
        )] for i, p in enumerate(full_pkgs) if not p.get("is_demo")]
        await query.edit_message_text(
            "Which package do you want to delete? 🗑️",
            reply_markup=InlineKeyboardMarkup(keyboard))


async def callback_del_pkg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    parts   = query.data.split(":")
    uid     = int(parts[1])
    pkg_idx = int(parts[2])
    tag     = parts[3] if len(parts) > 3 else None   # buttons sent before this update have no tag
    if query.from_user.id != uid:
        return
    client   = get_client(uid)
    packages = (client or {}).get("packages") or []
    if not (0 <= pkg_idx < len(packages)) or packages[pkg_idx].get("is_demo") \
            or (tag is not None and pkg_tag(packages[pkg_idx]) != tag):
        await query.edit_message_text("⚠️ Your packages changed since this list was shown. Use /settings to try again.")
        return
    removed = packages.pop(pkg_idx)
    db.table("clients").update({"packages": packages}).eq("client_id", uid).execute()
    await query.edit_message_text(
        f"✅ Package *{escape_md(removed['name'])}* deleted.\n\n"
        "Existing members on this package will still be managed until their expiry. ✔️",
        parse_mode="Markdown")


# ── /list ─────────────────────────────────────────────────────────────────────
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in ADMIN_IDS and not is_active_client(uid):
        await cmd_clientlist(update, context)
        return
    if not is_active_client(uid):
        await update.message.reply_text("⚠️ No active subscription.")
        return

    await context.bot.send_chat_action(uid, "typing")
    update_last_seen(uid)

    members = db.table("members").select("username, expiry, removed, package") \
                                 .eq("client_id", uid).execute().data

    if not members:
        await update.message.reply_text("📭 No members found.")
        return

    now = datetime.now(timezone.utc)
    active, pending, removed = [], [], []
    for m in members:
        if m.get("removed"):
            removed.append(m)
        elif not m.get("expiry"):
            pending.append(m)
        elif datetime.fromisoformat(m["expiry"]) > now:
            active.append(m)
        else:
            removed.append(m)  # expired but not yet kicked

    active.sort(key=lambda m: m["expiry"])

    divider = "━━━━━━━━━━━━━━━━"
    lines = ["👥 *MEMBER LEDGER*", divider]
    sn = 1

    if active:
        lines.append("🟢 *ACTIVE*")
        for m in active:
            lines.append(
                f"{sn}. 👤 {escape_md(m['username'] or 'Unknown')} — "
                f"📦 {escape_md(m.get('package') or '?')} · "
                f"⏳ {format_expiry_countdown(m['expiry'])} left"
            )
            sn += 1

    if pending:
        if active:
            lines.append(divider)
        lines.append("⏳ *PENDING JOIN*")
        for m in pending:
            lines.append(
                f"{sn}. 👤 {escape_md(m['username'] or 'Unknown')} — "
                f"📦 {escape_md(m.get('package') or '?')}"
            )
            sn += 1

    if removed:
        if active or pending:
            lines.append(divider)
        lines.append("🗃 *REMOVED / EXPIRED*")
        for m in removed:
            lines.append(
                f"{sn}. 👤 {escape_md(m['username'] or 'Unknown')} — "
                f"📦 {escape_md(m.get('package') or '?')}"
            )
            sn += 1

    await send_long(update.message, lines)


# ── /remove ───────────────────────────────────────────────────────────────────
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_active_client(uid) and uid not in ADMIN_IDS:
        await update.message.reply_text("⚠️ No active subscription.")
        return
    if not context.args:
        if uid in ADMIN_IDS and not is_active_client(uid):
            await update.message.reply_text("Usage: /remove [user_id] [client_id]")
        else:
            await update.message.reply_text("Usage: /remove [user_id]")
        return

    await context.bot.send_chat_action(uid, "typing")
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ user_id must be numeric.")
        return

    if uid in ADMIN_IDS and not is_active_client(uid):
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /remove [user_id] [client_id]")
            return
        try:
            owner_id = int(context.args[1])
        except ValueError:
            await update.message.reply_text("⚠️ client_id must be numeric.")
            return
        client = get_client(owner_id)
    else:
        owner_id = uid
        client = get_client(uid)

    channel_id = client.get("channel_id") if client else None
    if not channel_id:
        await update.message.reply_text("⚠️ No channel/group linked.")
        return

    try:
        await context.bot.ban_chat_member(channel_id, target_id)
        await context.bot.unban_chat_member(channel_id, target_id)
        # Clearing paused_seconds means someone you removed is never brought back by a restore
        update_member_row(target_id, owner_id, {"removed": True, "paused_seconds": None})
        await update.message.reply_text(
            f"✅ `{target_id}` removed from the channel/group.",
            parse_mode="Markdown")
    except Exception as e:
        if looks_like_rights_error(e):
            await alert_rights(context.bot, owner_id, (client or {}).get("brand_name"),
                               "I couldn't remove a member. I need the Ban Members permission.")
        await update.message.reply_text(
            f"⚠️ Couldn't remove `{target_id}`:\n`{e}`",
            parse_mode="Markdown")
    if is_active_client(uid):
        update_last_seen(uid)


# ── Platform admin commands ────────────────────────────────────────────────────
async def cmd_clientlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await context.bot.send_chat_action(update.effective_user.id, "typing")

    clients = db.table("clients").select("*").order("removed").execute().data
    if not clients:
        await update.message.reply_text("📭 No clients found in the database.")
        return

    now   = datetime.now(timezone.utc)
    lines = ["📋 *Full Client Ledger:*\n"]
    for c in clients:
        if c.get("suspended"):
            status_str = "🚫 Suspended"
        elif c.get("plan") == "pending":
            status_str = "⏳ Awaiting payment approval"
        elif c.get("removed"):
            status_str = "❌ Removed/Inactive"
        else:
            expiry_dt = datetime.fromisoformat(c["expiry"])
            if expiry_dt > now:
                status_str = f"✅ Active ({format_expiry_countdown(c['expiry'])} left)"
            else:
                status_str = "⚠️ Expired"
        lines.append(
            f"• {escape_md(c['username'])} — {escape_md(c.get('brand_name') or '?')} | "
            f"{status_str} | 🔑 `{c.get('ref_code', 'N/A')}` | 🆔 `{c['client_id']}`"
        )

    await send_long(update.message, lines)


async def cmd_suspend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/suspend <client_id | ref code | @username> [reason]
    Stops new subscriptions, payment details and approvals for that client. Existing members keep
    their access until their own expiry."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /suspend <client_id | ref code | @username> [reason]")
        return
    c = find_client(context.args[0])
    if not c:
        await update.message.reply_text("⚠️ Client not found. Use /clientlist to see IDs and ref codes.")
        return
    if c.get("suspended"):
        await update.message.reply_text("ℹ️ That client is already suspended.")
        return

    reason = " ".join(context.args[1:]).strip()
    try:
        db.table("clients").update({"suspended": True}).eq("client_id", c["client_id"]).execute()
    except Exception as e:
        await update.message.reply_text(
            "⚠️ Couldn't suspend. The `suspended` column probably doesn't exist yet. Run in Supabase:\n"
            "`ALTER TABLE clients ADD COLUMN suspended boolean NOT NULL DEFAULT false;`\n\n"
            f"Error: `{e}`",
            parse_mode="Markdown")
        return

    try:
        await context.bot.send_message(c["client_id"],
            "🚫 *Your SubPlanBot account has been suspended.*\n\n"
            + (f"Reason: {escape_md(reason)}\n\n" if reason else "")
            + "New subscriptions and approvals are paused. Contact @GizmoBrymez to resolve this.",
            parse_mode="Markdown")
    except Exception:
        pass
    await update.message.reply_text(
        f"🚫 *Suspended:* {escape_md(c.get('brand_name') or c['client_id'])} (`{c['client_id']}`)\n"
        "Use /unsuspend to lift it.",
        parse_mode="Markdown")


async def cmd_unsuspend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /unsuspend <client_id | ref code | @username>")
        return
    c = find_client(context.args[0])
    if not c:
        await update.message.reply_text("⚠️ Client not found.")
        return
    if not c.get("suspended"):
        await update.message.reply_text("ℹ️ That client isn't suspended.")
        return
    try:
        db.table("clients").update({"suspended": False}).eq("client_id", c["client_id"]).execute()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Couldn't unsuspend: `{e}`", parse_mode="Markdown")
        return
    try:
        await context.bot.send_message(c["client_id"],
            "✅ *Your SubPlanBot account is active again.* Thanks for your patience!",
            parse_mode="Markdown")
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ *Unsuspended:* {escape_md(c.get('brand_name') or c['client_id'])} (`{c['client_id']}`)",
        parse_mode="Markdown")


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
    app.add_handler(CommandHandler("subscribe",  cmd_subscribe))
    app.add_handler(CommandHandler("pay",        cmd_pay))
    app.add_handler(CommandHandler("renew",      cmd_renew))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("list",       cmd_list))
    app.add_handler(CommandHandler("remove",     cmd_remove))
    app.add_handler(CommandHandler("settings",   cmd_settings))
    app.add_handler(CommandHandler("clientlist", cmd_clientlist))
    app.add_handler(CommandHandler("suspend",    cmd_suspend))
    app.add_handler(CommandHandler("unsuspend",  cmd_unsuspend))
    app.add_handler(CommandHandler("getfileid",  cmd_getfileid))

    app.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, handle_forward))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.PDF) & filters.ChatType.PRIVATE, handle_photo))
    # Videos are only ever for admins fetching file IDs; they are never treated as receipts
    app.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE & filters.User(user_id=ADMIN_IDS), handle_getfileid_media))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_text))

    app.add_handler(ChatMemberHandler(handle_bot_added,     chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(handle_member_joined, chat_member_types=ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    app.add_handler(CallbackQueryHandler(callback_welcome,        pattern=r"^welcome:"))
    app.add_handler(CallbackQueryHandler(callback_onboard,        pattern=r"^onboard:"))
    app.add_handler(CallbackQueryHandler(callback_paytarget,      pattern=r"^paytarget:"))
    app.add_handler(CallbackQueryHandler(callback_rcpt,           pattern=r"^rcpt:"))
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