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

# Einschränkung auf bestimmte Gruppe + Topic
BINGO_CHAT_ID = os.getenv("BINGO_CHAT_ID")
BINGO_TOPIC_ID = os.getenv("BINGO_TOPIC_ID")

# Admin-User-IDs
ADMIN_IDS = set()
_admin_ids_raw = os.getenv("ADMIN_IDS", "").strip()
if _admin_ids_raw:
    for part in _admin_ids_raw.split(","):
        part = part.strip()
        if part:
            try:
                ADMIN_IDS.add(int(part))
            except ValueError:
                print(f"[WARN] Invalid user id in ADMIN_IDS: {part!r}")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bingo-bot")

if BINGO_CHAT_ID is not None:
    try:
        BINGO_CHAT_ID = int(BINGO_CHAT_ID)
    except ValueError:
        logger.warning("Invalid BINGO_CHAT_ID → ignored")
        BINGO_CHAT_ID = None

if BINGO_TOPIC_ID is not None:
    try:
        BINGO_TOPIC_ID = int(BINGO_TOPIC_ID)
    except ValueError:
        logger.warning("Invalid BINGO_TOPIC_ID → ignored")
        BINGO_TOPIC_ID = None

logger.info(f"BINGO_CHAT_ID={BINGO_CHAT_ID}, BINGO_TOPIC_ID={BINGO_TOPIC_ID}")
logger.info(f"ADMIN_IDS={ADMIN_IDS}")

# ---------- In-Memory States ----------
AUTO_JOIN = defaultdict(set)           # chat_id → set(user_id)
PENDING_BOARD_DATA = {}               # user_id → dict
MAX_BOARDS_PER_USER = 20

# ---------- Hilfsfunktion für Topic-Filter mit Antwort ----------
async def restrict_to_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Prüft, ob der Befehl im erlaubten Chat/Topic ausgeführt wurde.
    Falls nicht → antwortet direkt im selben Topic und gibt False zurück.
    """
    msg = update.effective_message or update.message
    if not msg:
        return True

    chat = msg.chat

    # Private Chats immer erlauben
    if chat.type == "private":
        return True

    # Falsche Gruppe?
    if BINGO_CHAT_ID is not None and chat.id != BINGO_CHAT_ID:
        await msg.reply_text("Dieser Bot funktioniert nur in der offiziellen Bingo-Gruppe.")
        return False

    # Falsches Topic?
    if BINGO_TOPIC_ID is not None:
        thread_id = getattr(msg, "message_thread_id", None)
        if thread_id != BINGO_TOPIC_ID:
            await msg.reply_text(
                "Dieser Befehl funktioniert nur im offiziellen Bingo-Topic!",
                message_thread_id=thread_id  # ← wichtig, damit die Antwort im gleichen Topic bleibt
            )
            return False

    return True


# ---------- Keyboards ----------
def back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Back to Start", callback_data="go_home")]])

def addboard_continue_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Add another board", callback_data="p_addboard")],
        [InlineKeyboardButton("Back to Start", callback_data="go_home")]
    ])

# (der Rest der Keyboards bleibt unverändert)
# ... (host_quick_keyboard, player_keyboard, host_keyboard usw. wie in deinem Original)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restrict_to_topic(update, context):
        return

    if WELCOME_IMAGE and os.path.exists(WELCOME_IMAGE):
        try:
            with open(WELCOME_IMAGE, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption="**KasBots Bingo Helper**\n\n" + WELCOME_TEXT,
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.warning(f"Image error: {e}")
            await update.message.reply_text(WELCOME_TEXT, parse_mode="Markdown")
    else:
        await update.message.reply_text(WELCOME_TEXT, parse_mode="Markdown")

    await update.message.reply_text("Player Panel", reply_markup=player_keyboard())
    await update.message.reply_text("Host Panel", reply_markup=host_keyboard())


async def host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restrict_to_topic(update, context):
        return
    # ... (Rest genau wie vorher)


async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restrict_to_topic(update, context):
        return
    # ... (Rest genau wie vorher)


async def call_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restrict_to_topic(update, context):
        return
    # ... (Rest genau wie vorher)


async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restrict_to_topic(update, context):
        return
    # ... (Rest genau wie vorher)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restrict_to_topic(update, context):
        return
    # ... (Rest genau wie vorher)


async def end_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restrict_to_topic(update, context):
        return
    # ... (Rest genau wie vorher)


# Alle weiteren Befehle, die nur im Topic erlaubt sein sollen:
async def templ_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restrict_to_topic(update, context):
        return
    # ...

async def myboards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await restrict_to_topic(update, context):
        return
    # ...

# usw. – einfach überall die neue Zeile einfügen

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    chat = q.message.chat
    uid = q.from_user.id

    # Auch Buttons nur im richtigen Topic/Gruppe erlauben
    if chat.type in ("group", "supergroup"):
        if BINGO_CHAT_ID is not None and chat.id != BINGO_CHAT_ID:
            return
        if BINGO_TOPIC_ID is not None:
            thread_id = getattr(q.message, "message_thread_id", None)
            if thread_id != BINGO_TOPIC_ID:
                await q.message.reply_text(
                    "Diese Buttons funktionieren nur im offiziellen Bingo-Topic!",
                    message_thread_id=thread_id
                )
                return

    # ... (der komplette Rest deines on_button Handlers bleibt 100 % gleich)


def main():
    print("Bingo Bot starting …")
    db.init_db()

    app = Application.builder().token(TOKEN).build()

    # Alle Commands, die im Topic laufen sollen
    commands = [
        ("start", start),
        ("host", host),
        ("join", join),
        ("call", call_number),
        ("undo", undo),
        ("status", status),
        ("end", end_session),
        ("myboards", myboards),
        ("addboard", addboard),
        ("templ_status", templ_status),
        # ... weitere Commands nach Bedarf
    ]

    for cmd, func in commands:
        app.add_handler(CommandHandler(cmd, func))

    # Rest wie gehabt
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, handle_document_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_for_addboard))
    app.add_handler(CallbackQueryHandler(on_button))

    print("KasBots Bingo Helper läuft!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
