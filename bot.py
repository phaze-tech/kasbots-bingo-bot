
# --- AUTO-BOOTSTRAP (optional, for local use) ---
import os, sys

USE_AUTO_BOOTSTRAP = os.getenv("USE_AUTO_BOOTSTRAP", "1") == "1"

if USE_AUTO_BOOTSTRAP:
    import subprocess, venv

    ROOT = os.path.dirname(os.path.abspath(__file__))
    VENV_DIR = os.path.join(ROOT, ".venv")
    VENV_PY  = os.path.join(VENV_DIR, "Scripts", "python.exe") if os.name == "nt" else os.path.join(VENV_DIR, "bin", "python")

    def _ensure_venv():
        if not os.path.exists(VENV_PY):
            venv.EnvBuilder(with_pip=True).create(VENV_DIR)
            subprocess.check_call([VENV_PY, "-m", "pip", "install", "-U", "pip", "setuptools", "wheel"])
            req = os.path.join(ROOT, "requirements.txt")
            if os.path.exists(req):
                subprocess.check_call([VENV_PY, "-m", "pip", "install", "-r", req])

    if os.path.normcase(sys.executable) != os.path.normcase(VENV_PY):
        _ensure_venv()
        os.execv(VENV_PY, [VENV_PY] + sys.argv)
# --- /AUTO-BOOTSTRAP ---

import logging
import random
from collections import defaultdict


import logging
import random
from collections import defaultdict

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

import db
from utils import mark_hits, check_bingo, has_bingo_corners
from ocr import image_to_grid, train_templates_from_board, templates_available

# ---------- ENV + Logging ----------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
IMAGES_DIR = os.getenv("IMAGES_DIR", "storage/images")
WELCOME_IMAGE = os.getenv("WELCOME_IMAGE", "storage/images/welcome.jpg")
BINGO_IMAGE = os.getenv("BINGO_IMAGE", "storage/images/bingo.jpg")
CARD_HELP_IMAGE = os.getenv("CARD_HELP_IMAGE", "storage/images/card_number_example.jpg")

os.makedirs(IMAGES_DIR, exist_ok=True)

# Einschränkung auf bestimmte Gruppe + Topic (optional)
BINGO_CHAT_ID = os.getenv("BINGO_CHAT_ID")
BINGO_TOPIC_ID = os.getenv("BINGO_TOPIC_ID")

# Admin-User-IDs (dürfen kritische Kommandos wie /resetall benutzen)
ADMIN_IDS = set()
_admin_ids_raw = os.getenv("ADMIN_IDS", "").strip()
if _admin_ids_raw:
    for part in _admin_ids_raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ADMIN_IDS.add(int(part))
        except ValueError:
            print(f"[WARN] Invalid user id in ADMIN_IDS env: {part!r}")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bingo-bot")

if BINGO_CHAT_ID is not None:
    try:
        BINGO_CHAT_ID = int(BINGO_CHAT_ID)
    except ValueError:
        logger.warning("Invalid BINGO_CHAT_ID in .env, ignoring.")
        BINGO_CHAT_ID = None

if BINGO_TOPIC_ID is not None:
    try:
        BINGO_TOPIC_ID = int(BINGO_TOPIC_ID)
    except ValueError:
        logger.warning("Invalid BINGO_TOPIC_ID in .env, ignoring.")
        BINGO_TOPIC_ID = None

logger.info(f"Using WELCOME_IMAGE={WELCOME_IMAGE}, exists={os.path.exists(WELCOME_IMAGE)}")
logger.info(f"Using BINGO_IMAGE={BINGO_IMAGE}, exists={os.path.exists(BINGO_IMAGE)}")
logger.info(f"Using CARD_HELP_IMAGE={CARD_HELP_IMAGE}, exists={os.path.exists(CARD_HELP_IMAGE)}")
logger.info(f"Images directory: {IMAGES_DIR} (exists={os.path.exists(IMAGES_DIR)})")
logger.info(f"BINGO_CHAT_ID={BINGO_CHAT_ID}, BINGO_TOPIC_ID={BINGO_TOPIC_ID}")
logger.info(f"ADMIN_IDS={ADMIN_IDS}")

# ---------- In-Memory Auto-Join ----------
# AUTO_JOIN[chat_id] -> set(user_id)
AUTO_JOIN = defaultdict(set)

# Pending boards per user_id:
# Flow:
#   1) Grid erkannt -> {"grid": grid}
#   2) Card Number erhalten -> {"grid": grid, "card_number": text}
PENDING_BOARD_DATA = {}  # user_id -> {"grid": ..., "card_number": ...}

MAX_BOARDS_PER_USER = 20

# ---------- Helper ----------

def in_allowed_topic(update: Update) -> bool:
    """
    Filtert nach:
    - BINGO_CHAT_ID (Gruppe)
    - BINGO_TOPIC_ID (Topic innerhalb dieser Gruppe, optional)
    Private Chats bleiben immer erlaubt.
    """
    msg = update.effective_message or getattr(update, "message", None)
    if not msg:
        return True

    chat = msg.chat

    # Nur diese Gruppe (falls gesetzt)
    if BINGO_CHAT_ID is not None and chat.type in ("group", "supergroup"):
        if chat.id != BINGO_CHAT_ID:
            return False

    # Nur dieses Topic in der Gruppe (falls gesetzt)
    if BINGO_TOPIC_ID is not None and chat.type in ("group", "supergroup"):
        thread_id = getattr(msg, "message_thread_id", None)
        if thread_id != BINGO_TOPIC_ID:
            return False

    return True  # private und erlaubte Gruppe/Topic

def back_button():
    """Creates a universal 'Back to Start' button."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back to Start", callback_data="go_home")]])

def addboard_continue_keyboard():
    """Keyboard nach dem Speichern eines Boards: neues Board + zurück."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add another board", callback_data="p_addboard")],
        [InlineKeyboardButton("🏠 Back to Start", callback_data="go_home")]
    ])

def host_quick_keyboard():
    """Inline-Leiste für den Host nach jedem Call/Undo/Status."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔢 Enter Number (/call)", callback_data="h_call"),
            InlineKeyboardButton("↩️ Undo Last", callback_data="h_undo"),
        ],
        [
            InlineKeyboardButton("📋 Status", callback_data="h_status"),
            InlineKeyboardButton("🏠 Back to Start", callback_data="go_home"),
        ],
    ])

async def _display_name(update: Update, uid: int) -> str:
    try:
        member = await update.effective_chat.get_member(uid)
        if member and member.user:
            if member.user.username:
                return f"@{member.user.username}"
            name_parts = [member.user.first_name or "", member.user.last_name or ""]
            name = " ".join(p for p in name_parts if p).strip()
            if name:
                return name
    except Exception as e:
        logger.debug(f"Could not resolve display name for {uid}: {e}")
    return f"id:{uid}"

def player_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧩 Add Board", callback_data="p_addboard"),
         InlineKeyboardButton("📋 My Boards", callback_data="p_myboards")],
        [InlineKeyboardButton("🎮 Join Session", callback_data="p_join")],
        [InlineKeyboardButton("📈 Live Progress", callback_data="p_score"),
         InlineKeyboardButton("🏆 Leaderboard", callback_data="p_leaderboard")],
        [InlineKeyboardButton("📊 My Stats", callback_data="p_mystats")],
        [InlineKeyboardButton("📜 Game Rules", callback_data="show_rules")]
    ])

def host_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Start Session", callback_data="h_host"),
         InlineKeyboardButton("🧪 Train Templates", callback_data="h_train")],
        [InlineKeyboardButton("🔢 Enter Number (/call)", callback_data="h_call"),
         InlineKeyboardButton("↩️ Undo Last", callback_data="h_undo")],
        [InlineKeyboardButton("📋 Status", callback_data="h_status")],
        [InlineKeyboardButton("🛑 End Session", callback_data="h_end")],
        [InlineKeyboardButton("📜 Game Rules", callback_data="show_rules")]
    ])

WELCOME_TEXT = (
    "🤖 **KasBots Bingo Helper**\n\n"
    "Welcome to fully degen-automated Bingo:\n"
    "• Upload your boards once (OCR reads the numbers).\n"
    "• The host enters drawn numbers.\n"
    "• Bot checks Bingos, tracks stats and screams when someone wins.\n\n"
    "✨ **Winning patterns**\n"
    "• Standard Bingo: full row, column or diagonal.\n"
    "• X-Pattern (optional, host setting).\n"
    "• Four Corners: ALWAYS counts as Bingo on top of everything.\n\n"
    "💡 Tip: Send board images as **FILE (Document)** for best OCR accuracy.\n"
    "Center tile is always **FREE** and your card has its own **Card Number** – "
    "I'll ask for it after upload (this is *not* the rank).\n\n"
    "🔢 Each player can store up to **20 boards**."
)

RULES_TEXT = (
    "📜 **Game Rules**\n\n"
    "🔢 **Board & Numbers**\n"
    "• 5×5 grid, center tile is always **FREE**.\n"
    "• Numbers range from 1 to 75.\n"
    "• The bot reads your board via OCR – please send it as FILE (Document).\n\n"
    "🏷 **Card Number (not Rank)**\n"
    "• After uploading a board, the bot asks for the **Card Number**.\n"
    "• This is the printed ID on the card, *not* the game rank or placement.\n\n"
    "💼 **Wallet Address**\n"
    "• When you add a board, you must provide the wallet where you hold your bingo NFTs.\n"
    "• If you move your cards to a different wallet later, delete the boards and add them again with the correct wallet.\n\n"
    "🏆 **Winning Patterns**\n"
    "1) **Standard Bingo**\n"
    "   • Any full horizontal row\n"
    "   • Any full vertical column\n"
    "   • Any full diagonal\n\n"
    "2) **X-Pattern** (if selected by host)\n"
    "   • Both diagonals are fully marked.\n\n"
    "3) **Four Corners (always active)**\n"
    "   • All 4 corner tiles are marked.\n"
    "   • This always counts as Bingo on top of any selected pattern.\n\n"
    "🍀 If in doubt: if it looks spicy and hits a full line, an X, or all corners – "
    "the bot will shout BINGO for you."
)

def _grid_to_text(grid):
    return "\n".join(" ".join("FREE" if c is None else str(c) for c in row) for row in grid)

def _board_label(bid: int) -> str:
    """Beschriftung eines Boards inkl. Card Number (falls vorhanden)."""
    card = None
    try:
        card = db.get_board_token(bid)
    except Exception as e:
        logger.debug(f"get_board_token failed for board {bid}: {e}")
        card = None
    if card:
        return f"Board #{bid} (Card {card})"
    return f"Board #{bid}"

def _user_board_count(user_id: int) -> int:
    """Hilfsfunktion: wie viele Boards hat ein User bereits?"""
    try:
        ids = db.get_user_board_ids(user_id)
        return len(ids)
    except Exception as e:
        logger.warning(f"Could not get board count for user {user_id}: {e}")
        return 0

# ---------- Grundbefehle ----------

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Sends the welcome image + text + both panels."""
    if not in_allowed_topic(update):
        return

    # sichere Referenz auf die Nachricht (funktioniert auch bei Topics/Fotos/etc.)
    msg = update.effective_message or update.message

    if WELCOME_IMAGE and os.path.exists(WELCOME_IMAGE):
        try:
            with open(WELCOME_IMAGE, "rb") as f:
                await msg.reply_photo(
                    photo=f,
                    caption=WELCOME_TEXT,
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.warning(f"Couldn't send image '{WELCOME_IMAGE}': {e}")
            await msg.reply_text(WELCOME_TEXT, parse_mode="Markdown")
    else:
        logger.warning(f"WELCOME_IMAGE not found at '{WELCOME_IMAGE}'")
        await msg.reply_text(WELCOME_TEXT, parse_mode="Markdown")

    # Panels IMMER an die gleiche Nachricht anhängen
    await msg.reply_text("🎯 Player Panel", reply_markup=player_keyboard())
    await msg.reply_text("🛠 Host Panel", reply_markup=host_keyboard())



async def templ_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_topic(update):
        return

    ok = templates_available()
    msg = "Templates: ✅ ready" if ok else "Templates: ❌ not trained yet (/train)"
    await update.message.reply_text(msg, reply_markup=back_button())

async def addboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_topic(update):
        return

    # Boards nur im Privat-Chat hinzufügen
    if update.effective_chat.type != "private":
        return await update.message.reply_text(
            "📥 To add or manage your boards, please DM me directly.\n"
            "Boards can only be added in a private chat with me.\n"
            "This keeps the game chat clean and your boards private.",
            reply_markup=back_button()
        )

    uid = update.effective_user.id
    current = _user_board_count(uid)
    if current >= MAX_BOARDS_PER_USER:
        return await update.message.reply_text(
            f"🧩 You already have {MAX_BOARDS_PER_USER} boards saved.\n"
            "Please delete some boards before adding new ones.",
            reply_markup=back_button()
        )

    ctx.user_data["awaiting_board_text"] = True
    await update.message.reply_text(
        "🧩 Add a board (private only):\n"
        "• Send the ORIGINAL IMAGE as a FILE (Document) for best accuracy.\n"
        "• Or send 25 values (numbers/'FREE') row by row.\n\n"
        "After upload I'll ask you for the **Card Number** printed on the card – not the rank.\n"
        "Then you must provide the **wallet address** where you hold this bingo card.\n\n"
        "🔢 You can store up to **20 boards** per player.\n"
        "💡 If you later move your cards to another wallet, please delete the boards and add them again with the new wallet.",
        reply_markup=addboard_continue_keyboard()
    )

async def myboards(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Listet Boards mit Buttons (View/Delete) auf, inkl. Card Number im Text."""
    if not in_allowed_topic(update):
        return

    uid = update.effective_user.id
    ids = db.get_user_board_ids(uid)
    if not ids:
        return await update.message.reply_text(
            "📋 You have no boards yet. Use /addboard or send an image (in private).",
            reply_markup=back_button()
        )

    lines = [
        "🧩 **Your Boards**",
        "Tap a board to view or delete it.\n",
    ]
    keyboard_rows = []

    for bid in ids:
        try:
            card = db.get_board_token(bid)
        except Exception as e:
            logger.debug(f"Cannot get token for board {bid}: {e}")
            card = None

        if card:
            lines.append(f"• Board #{bid} – Card {card}")
            label = f"Board #{bid} (Card {card})"
        else:
            lines.append(f"• Board #{bid} – (no Card Number)")
            label = f"Board #{bid}"

        keyboard_rows.append([
            InlineKeyboardButton(f"🧩 {label}", callback_data=f"view_board:{bid}"),
            InlineKeyboardButton("🗑️ Delete", callback_data=f"del_board:{bid}")
        ])

    keyboard_rows.append([InlineKeyboardButton("🗑️ Delete All Boards", callback_data="del_all_boards")])
    keyboard_rows.append([InlineKeyboardButton("🏠 Back to Start", callback_data="go_home")])

    keyboard = InlineKeyboardMarkup(keyboard_rows)
    text = "\n".join(lines)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

# ---------- Board anzeigen & löschen (Commands) ----------

async def showboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_topic(update):
        return

    uid = update.effective_user.id
    if not ctx.args:
        return await update.message.reply_text(
            "Usage: /showboard <board_id>",
            reply_markup=back_button()
        )

    try:
        bid = int(ctx.args[0])
    except ValueError:
        return await update.message.reply_text("Invalid board ID.", reply_markup=back_button())

    if db.get_board_owner(bid) != uid:
        return await update.message.reply_text(
            "Board not found or not owned by you.",
            reply_markup=back_button()
        )

    grid = db.load_board(bid)
    card = None
    try:
        card = db.get_board_token(bid)
    except Exception:
        card = None

    header = f"🧩 Board #{bid}"
    if card:
        header += f" (Card {card})"
    text = f"{header}:\n{_grid_to_text(grid)}"
    await update.message.reply_text(text, reply_markup=back_button())

async def deleteboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_topic(update):
        return

    uid = update.effective_user.id
    if not ctx.args:
        return await update.message.reply_text(
            "Usage: /deleteboard <board_id> or /deleteboard all",
            reply_markup=back_button()
        )

    arg = ctx.args[0].lower()
    if arg == "all":
        count = db.delete_all_boards(uid)
        msg = f"🗑️ Deleted {count} of your boards." if count else "No boards found."
        return await update.message.reply_text(msg, reply_markup=back_button())

    try:
        bid = int(arg)
    except ValueError:
        return await update.message.reply_text("Invalid board ID.", reply_markup=back_button())

    ok = db.delete_board(bid, uid)
    msg = f"🗑️ Board {bid} deleted." if ok else "Board not found or not owned by you."
    await update.message.reply_text(msg, reply_markup=back_button())

# ---------- Host & Join ----------

async def host(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Startet eine neue Session und zeigt direkt das Host-Panel mit Buttons."""
    if not in_allowed_topic(update):
        return

    chat = update.effective_chat
    user = update.effective_user
    uid = user.id

    # Nur in Gruppen
    if chat.type == "private":
        return await update.message.reply_text(
            "Please use this in a group chat.", reply_markup=back_button()
        )

    # ✅ Nur Admins dürfen hosten (falls ADMIN_IDS gesetzt ist)
    if ADMIN_IDS and uid not in ADMIN_IDS:
        return await update.message.reply_text(
            "⛔ Only designated Bingo hosts can start a new session.\n"
            "Ask an admin if you want to host a game.",
            reply_markup=back_button()
        )

    chat_id = chat.id
    sid = db.create_session(chat_id, uid)
    uname = user.username or uid

    # Auto-Join: alle Nutzer, die in diesem Chat Auto-Join aktiviert haben, automatisch hinzufügen
    auto_users = AUTO_JOIN.get(chat_id, set())
    for auto_uid in auto_users:
        db.add_player(sid, auto_uid)
        user_board_ids = db.get_user_board_ids(auto_uid)
        for bid in user_board_ids:
            db.add_session_board(sid, bid)
        db.bump_participation(sid, auto_uid, len(user_board_ids))

    text = (
        f"🚀 Session #{sid} started by @{uname}.\n\n"
        "Players: /join or use the Player Panel to connect their boards.\n\n"
        "📋 **Host Controls:**\n"
        "• 🔢 Enter Number – use `/call <number>` (e.g. `/call 42`)\n"
        "• ↩️ Undo Last – remove the last number\n"
        "• 📋 Status – see pattern, players, boards, last numbers\n"
        "• 🛑 End Session – finish this round\n\n"
        "💡 Four Corners is always a valid Bingo on top of the main pattern."
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=host_keyboard()
    )


async def join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_topic(update):
        return

    if update.effective_chat.type == "private":
        return await update.message.reply_text(
            "Please use this in a group chat.", reply_markup=back_button()
        )

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    sid = db.get_live_session(chat_id)
    if not sid:
        return await update.message.reply_text("No active session.", reply_markup=back_button())

    db.add_player(sid, user_id)
    user_board_ids = db.get_user_board_ids(user_id)
    for bid in user_board_ids:
        db.add_session_board(sid, bid)
    count = len(user_board_ids)
    db.bump_participation(sid, user_id, count)
    await update.message.reply_text(f"✅ Joined with {count} boards.", reply_markup=back_button())

# ---------- Zahlen eingeben + Bingo prüfen ----------

FUNNY_BINGO_LINES = [
    "🍀 RNGesus has spoken!",
    "🧠 Galaxy-brain Bingo detected!",
    "🚀 Your board just went full degen green!",
    "🤖 KasBot approved: numbers aligned perfectly.",
    "💎 Diamond hands? More like diamond rows!"
]

FUNNY_HOST_DENIED = [
    "🛠 Host panel is for the Bingo overlord only. Nice try, degen.",
    "🚫 Access denied. You don’t have the Bingo Infinity Gauntlet.",
    "😏 Those buttons only obey the host.",
    "🧱 Host controls are read-only for mortals.",
]

async def call_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_topic(update):
        return

    chat_id, uid = update.effective_chat.id, update.effective_user.id
    sid = db.get_live_session(chat_id)
    if not sid:
        return await update.message.reply_text("No active session.", reply_markup=back_button())
    if db.get_session_host(sid) != uid:
        return await update.message.reply_text("Only host can call numbers.", reply_markup=back_button())
    if not ctx.args:
        return await update.message.reply_text("Usage: /call 42", reply_markup=back_button())

    try:
        n = int(ctx.args[0])
    except ValueError:
        return await update.message.reply_text("Please enter a number (1–75).", reply_markup=back_button())

    return await _process_called_number(update, ctx, sid, n)

async def _process_called_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE, sid: int, n: int):
    if n < 1 or n > 75:
        return await update.message.reply_text(
            "Number must be between 1 and 75.",
            reply_markup=host_quick_keyboard()
        )

    if db.draw_exists(sid, n):
        return await update.message.reply_text(
            f"{n} already entered.",
            reply_markup=host_quick_keyboard()
        )

    db.insert_draw(sid, n, db.next_draw_index(sid))
    drawn = set(db.get_drawn_numbers(sid))

    pattern = db.get_pattern(sid)

    winner_entries = []  # [{owner, bid}]
    winner_ids = set()

    for bid in db.get_session_board_ids(sid):
        grid = db.load_board(bid)
        hit = mark_hits(grid, drawn)

        # Hauptmuster ODER immer aktive Four Corners
        has_win = check_bingo(hit, pattern) or has_bingo_corners(hit)

        if has_win and not db.claim_exists(sid, bid):
            owner = db.get_board_owner(bid)
            if owner is None:
                continue
            db.insert_claim(sid, bid, owner, pattern)
            db.bump_bingo(owner)
            winner_entries.append({"owner": owner, "bid": bid})
            winner_ids.add(owner)

            # 🎯 Private Nachricht an Gewinner – lustiger Gratulationstext
            try:
                line = random.choice(FUNNY_BINGO_LINES)
                user_chat = await ctx.bot.get_chat(owner)
                await user_chat.send_message(
                    f"🎯 *BINGO!* 🎉\n{line}\n\n"
                    "Your board just hit a winning pattern.\n"
                    "Go flex that card number in chat. 😎",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.warning(f"Cannot DM winner {owner}: {e}")

    chat = update.effective_chat
    msg_obj = update.effective_message or update.message  # Nachricht im Topic/Thread
    msg = f"📣 Entered: *{n}*\n"

    if winner_entries:
        line = random.choice(FUNNY_BINGO_LINES)
        ats = [await _display_name(update, w) for w in winner_ids]
        msg += f"\n🏆 *BINGO!* 🎉\n{line}\n" + "\n".join([f"👉 {a}" for a in ats])

        # 📸 Bingo-Bild im gleichen Topic/Thread senden
        if BINGO_IMAGE:
            if os.path.exists(BINGO_IMAGE):
                try:
                    logger.info(f"Sending BINGO_IMAGE from '{BINGO_IMAGE}' via reply_photo")
                    with open(BINGO_IMAGE, "rb") as f:
                        await msg_obj.reply_photo(
                            photo=f,
                            caption="🎉 **BINGO!** We have a winner on the board!",
                            parse_mode="Markdown"
                        )
                except Exception as e:
                    logger.exception(f"Couldn't send BINGO_IMAGE '{BINGO_IMAGE}': {e}")
                    # Fallback, damit im Chat sichtbar ist, dass das Bild failed
                    await msg_obj.reply_text(
                        "🎉 BINGO! (image failed to send, check logs / file path)"
                    )
            else:
                logger.warning(f"BINGO_IMAGE path does not exist: '{BINGO_IMAGE}'")
        else:
            logger.warning("BINGO_IMAGE is not set (empty).")

        # 🔔 Extra: Host privat informieren (Winner + Board + Wallet)
        try:
            host_id = db.get_session_host(sid)
            if host_id:
                host_chat = await ctx.bot.get_chat(host_id)
                lines = ["👀 *Bingo check for this session:*"]
                for entry in winner_entries:
                    owner = entry["owner"]
                    bid = entry["bid"]
                    card = None
                    try:
                        card = db.get_board_token(bid)
                    except Exception:
                        card = None
                    wallet = db.get_user_wallet(owner)
                    name = f"id:{owner}"
                    try:
                        name = await _display_name(update, owner)
                    except Exception:
                        pass

                    line_board = f"• {name} – Board #{bid}"
                    if card:
                        line_board += f" (Card {card})"
                    if wallet:
                        line_board += f" | Wallet: `{wallet}`"
                    else:
                        line_board += " | Wallet: (not set)"

                    lines.append(line_board)

                await host_chat.send_message(
                    "\n".join(lines),
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.warning(f"Couldn't DM host with bingo info: {e}")
    else:
        msg += "No bingo yet."

    await msg_obj.reply_markdown(msg, reply_markup=host_quick_keyboard())

async def undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_topic(update):
        return

    chat_id, uid = update.effective_chat.id, update.effective_user.id
    sid = db.get_live_session(chat_id)
    if not sid:
        return await update.message.reply_text("No session.", reply_markup=back_button())
    if db.get_session_host(sid) != uid:
        return await update.message.reply_text("Only host can undo.", reply_markup=back_button())

    last = db.get_last_draw(sid)
    if not last:
        return await update.message.reply_text("Nothing to undo.", reply_markup=back_button())

    db.delete_draw(sid, last.idx)
    await update.message.reply_text(
        f"↩️ Removed {last.number}.",
        reply_markup=host_quick_keyboard()
    )

async def end_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_topic(update):
        return

    chat = update.effective_chat
    user = update.effective_user
    chat_id, uid = chat.id, user.id

    sid = db.get_live_session(chat_id)
    if not sid:
        return await update.message.reply_text("No session.", reply_markup=back_button())

    host_id = db.get_session_host(sid)

    # Nur Host ODER Admin darf beenden
    if uid != host_id and uid not in ADMIN_IDS:
        return await update.message.reply_text(
            "Only the current host (or an admin) can end the session.",
            reply_markup=back_button()
        )

    db.end_session(sid)
    await update.message.reply_text("🛑 Session ended.", reply_markup=back_button())


# ---------- Status ----------

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_topic(update):
        return

    chat = update.effective_chat
    if chat.type == "private":
        return await update.message.reply_text(
            "Use /status in the group where the session is running.",
            reply_markup=back_button()
        )

    chat_id = chat.id
    sid = db.get_live_session(chat_id)
    if not sid:
        return await update.message.reply_text("No active session.", reply_markup=back_button())

    drawn = db.get_drawn_numbers(sid)
    players = db.count_players(sid)
    boards = db.count_session_boards(sid)
    pattern = db.get_pattern(sid)

    last_numbers = ", ".join(map(str, drawn[-10:])) if drawn else "—"
    total = len(drawn)

    msg = (
        "📋 **Session Status**\n"
        f"• Pattern: `{pattern}` (Four Corners always active)\n"
        f"• Players: {players}\n"
        f"• Boards in play: {boards}\n"
        f"• Numbers drawn: {total}\n"
        f"• Last numbers: {last_numbers}"
    )
    await update.message.reply_markdown(msg, reply_markup=host_quick_keyboard())

async def debug_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message

    chat_id = chat.id
    thread_id = getattr(msg, "message_thread_id", None)

    text = (
        f"chat_id = {chat_id}\n"
        f"message_thread_id (topic) = {thread_id}"
    )
    await update.message.reply_text(text)

# ---------- OCR & Board Upload ----------

async def _save_board_from_grid(update: Update, ctx: ContextTypes.DEFAULT_TYPE, grid, uid: int):
    """
    Nimmt ein erkanntes Grid, speichert es zunächst nur im RAM und
    fragt die Card Number ab. Erst nach Card Number + Wallet wird
    das Board in die Datenbank geschrieben.
    """
    if any(v == "ERR" for row in grid for v in row):
        return await update.message.reply_text(
            "⚠️ OCR uncertain – please resend as FILE.",
            reply_markup=back_button()
        )

    # Board-Limit prüfen
    current = _user_board_count(uid)
    if current >= MAX_BOARDS_PER_USER:
        return await update.message.reply_text(
            f"🧩 You already have {MAX_BOARDS_PER_USER} boards saved.\n"
            "Please delete some boards before adding new ones.",
            reply_markup=back_button()
        )

    global PENDING_BOARD_DATA
    PENDING_BOARD_DATA[uid] = {"grid": grid}

    await update.message.reply_text(
        f"📖 Detected grid for your new board:\n{_grid_to_text(grid)}"
    )

    # Card Number abfragen – mit Beispielbild falls vorhanden
    if CARD_HELP_IMAGE and os.path.exists(CARD_HELP_IMAGE):
        try:
            with open(CARD_HELP_IMAGE, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=(
                        "🏷 **Card Number needed**\n\n"
                        "Please enter the **Card Number** printed on your bingo card now.\n"
                        "👉 This is *not* the rank, not a game position – just the ID printed on the card."
                    ),
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.warning(f"Couldn't send CARD_HELP_IMAGE '{CARD_HELP_IMAGE}': {e}")
            await update.message.reply_text(
                "🏷 Please enter the **Card Number** printed on your bingo card now "
                "(not the rank).",
                parse_mode="Markdown"
            )
    else:
        await update.message.reply_text(
            "🏷 Please enter the **Card Number** printed on your bingo card now "
            "(not the rank).",
            parse_mode="Markdown"
        )

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_topic(update):
        return

    if update.effective_chat.type != "private":
        return await update.message.reply_text(
            "📥 Please send your bingo board as FILE (Document) in a private chat with me.\n"
            "Boards can’t be added inside the game group.",
            reply_markup=back_button()
        )

    uid = update.effective_user.id
    if _user_board_count(uid) >= MAX_BOARDS_PER_USER:
        return await update.message.reply_text(
            f"🧩 You already have {MAX_BOARDS_PER_USER} boards saved.\n"
            "Please delete some boards before adding new ones.",
            reply_markup=back_button()
        )

    f = await update.message.photo[-1].get_file()
    path = os.path.join(IMAGES_DIR, f"u{uid}.jpg")
    await f.download_to_drive(path)
    try:
        grid = image_to_grid(path)
    except Exception as e:
        logger.warning(f"OCR failed for photo: {e}")
        return await update.message.reply_text(f"OCR failed: {e}", reply_markup=back_button())
    await _save_board_from_grid(update, ctx, grid, uid)

async def handle_document_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_topic(update):
        return

    if update.effective_chat.type != "private":
        return await update.message.reply_text(
            "📥 Please send your bingo board as FILE (Document) in a private chat with me.\n"
            "Boards can’t be added inside the game group.",
            reply_markup=back_button()
        )

    uid = update.effective_user.id
    if _user_board_count(uid) >= MAX_BOARDS_PER_USER:
        return await update.message.reply_text(
            f"🧩 You already have {MAX_BOARDS_PER_USER} boards saved.\n"
            "Please delete some boards before adding new ones.",
            reply_markup=back_button()
        )

    f = await update.message.document.get_file()
    ext = os.path.splitext(update.message.document.file_name or ".png")[1]
    path = os.path.join(IMAGES_DIR, f"u{uid}{ext}")
    await f.download_to_drive(path)
    try:
        grid = image_to_grid(path)
    except Exception as e:
        logger.warning(f"OCR failed for document image: {e}")
        return await update.message.reply_text(f"OCR failed: {e}", reply_markup=back_button())
    await _save_board_from_grid(update, ctx, grid, uid)

# ---------- Training ----------

async def train(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_topic(update):
        return

    ctx.user_data["train_wait_image"] = True
    await update.message.reply_text(
        "🧪 **Training Mode**\n"
        "1️⃣ Send a clean board as FILE.\n"
        "2️⃣ Then send the 25 values (numbers/FREE) row by row.",
        parse_mode="Markdown",
        reply_markup=back_button()
    )

async def handle_text_for_addboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not in_allowed_topic(update):
        return

    text = update.message.text.strip()
    uid = update.effective_user.id

    global PENDING_BOARD_DATA

    # 0) Wallet wird erwartet?
    if ctx.user_data.get("awaiting_wallet"):
        ctx.user_data["awaiting_wallet"] = False

        if uid not in PENDING_BOARD_DATA or "grid" not in PENDING_BOARD_DATA[uid] or "card_number" not in PENDING_BOARD_DATA[uid]:
            return await update.message.reply_text(
                "Something went wrong while saving your board. Please start /addboard again.",
                reply_markup=back_button()
            )

        data = PENDING_BOARD_DATA.pop(uid)
        grid = data["grid"]
        card_number = data["card_number"]
        wallet = text  # frei eingegebene Wallet-Adresse

        # Board speichern
        bid = db.create_board(uid, card_number, True)
        db.save_board_numbers(bid, grid)

        # Wallet als Default für User speichern
        try:
            db.set_user_wallet(uid, wallet)
        except Exception as e:
            logger.warning(f"Could not save user wallet for {uid}: {e}")

        await update.message.reply_text(
            f"🏷 Card Number saved for board #{bid}.\n"
            "✅ Board fully added with this wallet address.\n\n"
            "💡 If you move this card to another wallet later, delete this board and add it again with the correct wallet.",
            reply_markup=addboard_continue_keyboard()
        )

        # Optionaler Hinweis, dass diese Wallet für weitere Boards benutzt wird
        await update.message.reply_text(
            "💼 I will use this wallet as your default for future boards.\n"
            "You can always enter a different wallet next time if needed.",
            reply_markup=back_button()
        )
        return

    # 1) Card Number erfassen, falls für diesen User ein Board im RAM liegt (aber noch keine Card Number)
    if uid in PENDING_BOARD_DATA and "card_number" not in PENDING_BOARD_DATA[uid]:
        PENDING_BOARD_DATA[uid]["card_number"] = text  # z.B. "852" oder "#852"

        # Gibt es bereits eine gespeicherte Wallet für diesen User?
        existing_wallet = db.get_user_wallet(uid)
        if existing_wallet:
            # User kann wählen, ob die gespeicherte Wallet für dieses Board benutzt wird
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Use saved wallet", callback_data="wallet_use_default")],
                [InlineKeyboardButton("✏️ Enter another wallet", callback_data="wallet_enter_new")],
                [InlineKeyboardButton("🏠 Back to Start", callback_data="go_home")]
            ])
            return await update.message.reply_text(
                "💼 You already have a saved wallet address for your boards:\n"
                f"`{existing_wallet}`\n\n"
                "Do you want to use this wallet for this new board?",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        else:
            # Noch keine Wallet – direkt nach Wallet fragen
            ctx.user_data["awaiting_wallet"] = True
            return await update.message.reply_text(
                "💼 Please enter the **wallet address** where you hold this bingo card.\n\n"
                "If you later move this card to another wallet, delete this board and add it again with the correct wallet.",
                parse_mode="Markdown",
                reply_markup=back_button()
            )

    # 2) 25 Werte per Text für ein neues Board
    if ctx.user_data.get("awaiting_board_text"):
        vals = text.replace(",", " ").split()
        if len(vals) != 25:
            return await update.message.reply_text(
                "Need 25 values.",
                reply_markup=back_button()
            )
        try:
            flat = [None if v.lower() in ("free", "x") else int(v) for v in vals]
        except ValueError:
            return await update.message.reply_text(
                "Use only numbers or 'FREE'.",
                reply_markup=back_button()
            )

        grid = [flat[i * 5:(i + 1) * 5] for i in range(5)]
        ctx.user_data["awaiting_board_text"] = False

        # Grid nur im RAM merken, noch nicht speichern
        PENDING_BOARD_DATA[uid] = {"grid": grid}

        await update.message.reply_text(
            f"📖 Detected grid for your new board:\n{_grid_to_text(grid)}"
        )

        # Card Number abfragen – gleicher Text wie oben
        if CARD_HELP_IMAGE and os.path.exists(CARD_HELP_IMAGE):
            try:
                with open(CARD_HELP_IMAGE, "rb") as f:
                    await update.message.reply_photo(
                        photo=f,
                        caption=(
                            "🏷 **Card Number needed**\n\n"
                            "Please enter the **Card Number** printed on your bingo card now.\n"
                            "👉 This is *not* the rank, not a game position – just the ID printed on the card."
                        ),
                        parse_mode="Markdown"
                    )
            except Exception as e:
                logger.warning(f"Couldn't send CARD_HELP_IMAGE '{CARD_HELP_IMAGE}': {e}")
                await update.message.reply_text(
                    "🏷 Please enter the **Card Number** printed on your bingo card now "
                    "(not the rank).",
                    parse_mode="Markdown"
                )
        else:
            await update.message.reply_text(
                "🏷 Please enter the **Card Number** printed on your bingo card now "
                "(not the rank).",
                parse_mode="Markdown"
            )

        # Wallet kommt im nächsten Schritt
        return

# ---------- Buttons / Callbacks ----------

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    chat = q.message.chat
    uid = q.from_user.id

    # Gruppen-/Topic-Filter für Buttons
    if chat.type in ("group", "supergroup"):
        if BINGO_CHAT_ID is not None and chat.id != BINGO_CHAT_ID:
            return
        if BINGO_TOPIC_ID is not None:
            thread_id = getattr(q.message, "message_thread_id", None)
            if thread_id != BINGO_TOPIC_ID:
                return

    # ---------- Wallet-Auswahl für neues Board ----------
    if data == "wallet_use_default":
        # Default-Wallet aus DB holen
        wallet = db.get_user_wallet(uid)
        if not wallet or uid not in PENDING_BOARD_DATA or "grid" not in PENDING_BOARD_DATA[uid] or "card_number" not in PENDING_BOARD_DATA[uid]:
            return await q.message.reply_text(
                "Something went wrong while using the saved wallet. Please start /addboard again.",
                reply_markup=back_button()
            )

        data_pending = PENDING_BOARD_DATA.pop(uid)
        grid = data_pending["grid"]
        card_number = data_pending["card_number"]

        # Board speichern
        bid = db.create_board(uid, card_number, True)
        db.save_board_numbers(bid, grid)

        await q.message.reply_text(
            f"🏷 Card Number saved for board #{bid}.\n"
            "✅ Board fully added using your saved wallet address.\n\n"
            "💡 If you move this card to another wallet later, delete this board and add it again with the correct wallet.",
            reply_markup=addboard_continue_keyboard()
        )
        return

    if data == "wallet_enter_new":
        if uid not in PENDING_BOARD_DATA or "grid" not in PENDING_BOARD_DATA[uid] or "card_number" not in PENDING_BOARD_DATA[uid]:
            return await q.message.reply_text(
                "Something went wrong while preparing your board. Please start /addboard again.",
                reply_markup=back_button()
            )

        ctx.user_data["awaiting_wallet"] = True
        return await q.message.reply_text(
            "💼 Please enter the **wallet address** where you hold this bingo card.\n\n"
            "If you later move this card to another wallet, delete this board and add it again with the correct wallet.",
            parse_mode="Markdown",
            reply_markup=back_button()
        )

    # ---------- Game Rules ----------
    if data == "show_rules":
        return await q.message.reply_markdown(RULES_TEXT, reply_markup=back_button())

    # ---------- Spieler-Buttons ----------
    if data == "p_addboard":
        return await q.message.reply_text(
            "📥 Boards can only be added in a private chat with me.\n\n"
            "🧩 **Add Board**\n"
            "Send a board as FILE (Document) or use /addboard to paste 25 values.\n"
            "Center tile is always **FREE**.\n\n"
            "After upload I'll ask you for the **Card Number** and the **wallet address** where you hold your bingo cards.\n"
            f"You can store up to **{MAX_BOARDS_PER_USER} boards**.\n\n"
            "If you move your cards to another wallet later, please delete the boards and add them again with the new wallet.",
            parse_mode="Markdown",
            reply_markup=addboard_continue_keyboard()
        )

    if data == "p_myboards":
        # Trigger gleiche Logik wie /myboards
        class DummyUpdate:
            effective_user = q.from_user
            message = q.message
            effective_message = q.message
        dummy_update = DummyUpdate()
        return await myboards(dummy_update, ctx)

    if data == "p_join":
        # Join-Steuerung für den Spieler mit Auto-Join
        if chat.type not in ("group", "supergroup"):
            return await q.message.reply_text(
                "Use this button inside the group where Bingo is played.",
                reply_markup=back_button()
            )

        chat_id = chat.id
        auto_on = uid in AUTO_JOIN[chat_id]
        auto_label = "🚫 Disable Auto-Join" if auto_on else "✅ Enable Auto-Join"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 Join this session now", callback_data="join_now")],
            [InlineKeyboardButton(auto_label, callback_data="toggle_autojoin")],
            [InlineKeyboardButton("🏠 Back to Start", callback_data="go_home")]
        ])

        text = (
            "🎮 **Join Session**\n\n"
            "• *Join this session now*: connect all your saved boards to the current game.\n"
            "• *Enable Auto-Join*: you will automatically join every new session in this chat with all your boards."
        )
        return await q.message.reply_markdown(text, reply_markup=keyboard)

    if data == "join_now":
        # Gleiche Logik wie /join
        class DummyUpdate:
            effective_chat = chat
            effective_user = q.from_user
            message = q.message
            effective_message = q.message
        dummy = DummyUpdate()
        return await join(dummy, ctx)

    if data == "toggle_autojoin":
        chat_id = chat.id
        if uid in AUTO_JOIN[chat_id]:
            AUTO_JOIN[chat_id].remove(uid)
            msg = "🚫 Auto-Join disabled in this chat. You will only join when you tap *Join*."
        else:
            AUTO_JOIN[chat_id].add(uid)
            msg = "✅ Auto-Join enabled in this chat.\nYou will automatically join every new session with all your boards."

        # UI erneut aufbauen, damit Button-Label passt
        auto_on = uid in AUTO_JOIN[chat_id]
        auto_label = "🚫 Disable Auto-Join" if auto_on else "✅ Enable Auto-Join"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 Join this session now", callback_data="join_now")],
            [InlineKeyboardButton(auto_label, callback_data="toggle_autojoin")],
            [InlineKeyboardButton("🏠 Back to Start", callback_data="go_home")]
        ])
        await q.message.edit_reply_markup(reply_markup=keyboard)
        return await q.message.reply_markdown(msg, reply_markup=keyboard)

    if data == "p_score":
        row = db.get_user_stats_row(uid)
        if not row:
            return await q.message.reply_text(
                "📈 No stats yet. Join a session and play at least one Bingo game.",
                reply_markup=back_button()
            )
        _, total_bingos, total_boards, total_sessions, last_played = row
        msg = (
            "📈 **Your Progress**\n"
            f"• Sessions joined: {total_sessions}\n"
            f"• Boards used: {total_boards}\n"
            f"• Bingos: {total_bingos}\n"
            f"• Last played: {last_played}"
        )
        return await q.message.reply_markdown(msg, reply_markup=back_button())

    if data == "p_leaderboard":
        rows = db.get_leaderboard(limit=10)
        if not rows:
            return await q.message.reply_text(
                "🏆 No leaderboard yet. Play some games first!",
                reply_markup=back_button()
            )
        lines = []
        for i, (user_id, total_bingos, total_boards, total_sessions, last_played) in enumerate(rows, start=1):
            # Namen/Usernamen holen
            name = f"id:{user_id}"
            try:
                member = await chat.get_member(user_id)
                if member and member.user:
                    if member.user.username:
                        name = f"@{member.user.username}"
                    else:
                        name_parts = [member.user.first_name or "", member.user.last_name or ""]
                        pretty = " ".join(p for p in name_parts if p).strip()
                        if pretty:
                            name = pretty
            except Exception as e:
                logger.debug(f"Cannot resolve leaderboard name for {user_id}: {e}")

            lines.append(
                f"{i}. {name} – 🏆 {total_bingos} Bingos, 🎟️ {total_boards} boards, 🎮 {total_sessions} sessions"
            )
        msg = "🏆 **Global Leaderboard**\n" + "\n".join(lines)
        return await q.message.reply_markdown(msg, reply_markup=back_button())

    if data == "p_mystats":
        row = db.get_user_stats_row(uid)
        if not row:
            return await q.message.reply_text(
                "📊 No stats yet. Join a session and play at least one Bingo game.",
                reply_markup=back_button()
            )
        _, total_bingos, total_boards, total_sessions, last_played = row
        msg = (
            "📊 **Your Stats**\n"
            f"• Sessions joined: {total_sessions}\n"
            f"• Boards used: {total_boards}\n"
            f"• Bingos: {total_bingos}\n"
            f"• Last played: {last_played}"
        )
        return await q.message.reply_markdown(msg, reply_markup=back_button())

    # ---------- Board-Buttons ----------
    if data.startswith("view_board:"):
        try:
            bid = int(data.split(":", 1)[1])
        except ValueError:
            return await q.message.reply_text("Invalid board ID.", reply_markup=back_button())

        if db.get_board_owner(bid) != uid:
            return await q.message.reply_text("Board not found or not owned by you.", reply_markup=back_button())

        grid = db.load_board(bid)
        card = None
        try:
            card = db.get_board_token(bid)
        except Exception:
            card = None
        header = f"🧩 Board #{bid}"
        if card:
            header += f" (Card {card})"
        text = f"{header}:\n{_grid_to_text(grid)}"
        return await q.message.reply_text(text, reply_markup=back_button())

    if data.startswith("del_board:"):
        try:
            bid = int(data.split(":", 1)[1])
        except ValueError:
            return await q.message.reply_text("Invalid board ID.", reply_markup=back_button())

        ok = db.delete_board(bid, uid)
        msg = f"🗑️ Board {bid} deleted." if ok else "Board not found or not owned by you."
        return await q.message.reply_text(msg, reply_markup=back_button())

    if data == "del_all_boards":
        count = db.delete_all_boards(uid)
        msg = f"🗑️ Deleted {count} of your boards." if count else "You have no boards to delete."
        return await q.message.reply_text(msg, reply_markup=back_button())

    if data == "cancel":
        return await q.message.reply_text("❌ Action cancelled.", reply_markup=back_button())

    # ---------- Host-Buttons ----------
    if data == "h_host":
        if chat.type == "private":
            return await q.message.reply_text(
                "Use /host in a group chat to start a session.",
                reply_markup=back_button()
            )
        # Neue Session starten (hier darf jeder – es wird der neue Host)
        class DummyUpdate:
            effective_chat = chat
            effective_user = q.from_user
            message = q.message
            effective_message = q.message
        dummy = DummyUpdate()
        return await host(dummy, ctx)

    if data == "h_train":
        # Training ist nicht sicherheitskritisch, darf jeder
        class DummyUpdate:
            message = q.message
            effective_message = q.message
        dummy = DummyUpdate()
        return await train(dummy, ctx)

    # Ab hier: Buttons, die nur der Host einer laufenden Session drücken darf
    if data in ("h_call", "h_status", "h_undo", "h_end"):
        sid = db.get_live_session(chat.id)
        if not sid:
            return await q.message.reply_text("No active session.", reply_markup=back_button())

        host_id = db.get_session_host(sid)
        if host_id != uid:
            # Lustiger Spruch für Nicht-Hosts
            return await q.message.reply_text(
                random.choice(FUNNY_HOST_DENIED),
                reply_markup=back_button()
            )

        if data == "h_call":
            return await q.message.reply_text(
                "🔢 Host info:\n"
                "Use the command `/call <number>` in this chat to enter the next number.\n"
                "Example: `/call 42`.\n\n"
                "Always write the command *and* the number together.",
                parse_mode="Markdown",
                reply_markup=host_quick_keyboard()
            )

        if data == "h_status":
            class DummyUpdate:
                effective_chat = chat
                message = q.message
                effective_message = q.message
            dummy = DummyUpdate()
            return await status(dummy, ctx)

        if data == "h_undo":
            class DummyUpdate:
                effective_chat = chat
                effective_user = q.from_user
                message = q.message
                effective_message = q.message
            dummy = DummyUpdate()
            return await undo(dummy, ctx)

        if data == "h_end":
            class DummyUpdate:
                effective_chat = chat
                effective_user = q.from_user
                message = q.message
                effective_message = q.message
            dummy = DummyUpdate()
            return await end_session(dummy, ctx)

    # ---------- Back to Start ----------
    if data == "go_home":
        await q.message.reply_text("🏠 Returning to main menu ...")
        await q.message.reply_text("🎯 Player Panel", reply_markup=player_keyboard())
        await q.message.reply_text("🛠 Host Panel", reply_markup=host_keyboard())
        return

# ---------- Admin/Test: resetall ----------

async def resetall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Löscht alle Spiel-Daten – nur im Private Chat nutzbar.
    Nur Admin-User (ADMIN_IDS) dürfen das ausführen.
    Erwartet eine Funktion db.reset_all().
    """
    chat = update.effective_chat
    user = update.effective_user

    # Nur im Private Chat
    if chat.type != "private":
        return await update.message.reply_text(
            "⚠️ /resetall is only available in a private chat for testing.",
            reply_markup=back_button()
        )

    # Admin-Check
    if not ADMIN_IDS:
        # Falls du ADMIN_IDS noch nicht gesetzt hast → Niemand darf es nutzen
        return await update.message.reply_text(
            "⛔ /resetall is disabled because no ADMIN_IDS are configured.",
            reply_markup=back_button()
        )

    if user.id not in ADMIN_IDS:
        return await update.message.reply_text(
            "⛔ You are not allowed to use /resetall.\n"
            "This command is restricted to admins.",
            reply_markup=back_button()
        )

    # --- In-Memory-State resetten ---
    global PENDING_BOARD_DATA
    PENDING_BOARD_DATA.clear()

    # Optionaler Wallet-Cache (falls definiert)
    try:
        global LAST_WALLET_FOR_USER
        LAST_WALLET_FOR_USER.clear()
    except NameError:
        # Falls die Variable in deiner Version noch nicht existiert, einfach ignorieren
        pass

    # User-spezifische Flags des ausführenden Admins löschen
    ctx.user_data.clear()

    # --- Datenbank resetten ---
    db.reset_all()

    await update.message.reply_text(
        "🧹 All Bingo data has been reset for *all* users.\n"
        "• All boards, sessions and stats are gone.\n"
        "• Board numbering will start again from #1.\n"
        "• All stored wallets have been cleared.\n\n"
        "Fresh start – as if no degen ever played. 😇",
        parse_mode="Markdown",
        reply_markup=back_button()
    )


# ---------- Main ----------

def main():
    print("✅ Bingo Bot starting …")
    logging.info("Bootstrapping database & Telegram application …")
    db.init_db()

    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("templ_status", templ_status))
    app.add_handler(CommandHandler("addboard", addboard))
    app.add_handler(CommandHandler("myboards", myboards))
    app.add_handler(CommandHandler("showboard", showboard))
    app.add_handler(CommandHandler("deleteboard", deleteboard))
    app.add_handler(CommandHandler("host", host))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("call", call_number))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("end", end_session))
    app.add_handler(CommandHandler("train", train))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("id", debug_id))
    app.add_handler(CommandHandler("resetall", resetall))

    # Image + Document Handlers
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document_image))

    # Text-Handler für Card Number + Board-Text + Wallet
    app.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            handle_text_for_addboard,
        )
    )

    # Buttons
    app.add_handler(CallbackQueryHandler(on_button))

    print("🤖 Running KasBots Bingo Helper …")
    app.run_polling(poll_interval=1.0, drop_pending_updates=True)


if __name__ == "__main__":
    main()
