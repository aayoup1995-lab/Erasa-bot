import logging
import os
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

from dotenv import load_dotenv
from telegram import ChatPermissions, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN غير موجود في متغيرات البيئة")
DB_PATH = os.getenv("DB_PATH", "erasa.db")
WELCOME_ENABLED = os.getenv("WELCOME_ENABLED", "true").lower() == "true"
DUPLICATE_WINDOW = int(os.getenv("DUPLICATE_WINDOW_SECONDS", "45"))
URL_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/)", re.I)
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("erasa")


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings(chat_id INTEGER PRIMARY KEY, welcome_enabled INTEGER DEFAULT 1, links_blocked INTEGER DEFAULT 1, max_warnings INTEGER DEFAULT 3, banned_words TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS warnings(chat_id INTEGER, user_id INTEGER, count INTEGER DEFAULT 0, PRIMARY KEY(chat_id,user_id));
        CREATE TABLE IF NOT EXISTS points(chat_id INTEGER, user_id INTEGER, name TEXT, score INTEGER DEFAULT 0, PRIMARY KEY(chat_id,user_id));
        """)


def settings(chat_id):
    with db() as con:
        row = con.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
        if not row:
            con.execute("INSERT INTO settings(chat_id) VALUES(?)", (chat_id,))
            row = con.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
        return dict(row)


def set_setting(chat_id, key, value):
    if key in {"welcome_enabled", "links_blocked", "max_warnings", "banned_words"}:
        with db() as con:
            con.execute(f"UPDATE settings SET {key}=? WHERE chat_id=?", (value, chat_id))


def add_points(chat_id, user, amount=1):
    if not user or user.is_bot:
        return
    with db() as con:
        con.execute("INSERT INTO points(chat_id,user_id,name,score) VALUES(?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET name=excluded.name, score=score+excluded.score", (chat_id, user.id, user.full_name, amount))


def is_admin(handler):
    @wraps(handler)
    async def wrapper(update, context):
        member = await update.effective_chat.get_member(update.effective_user.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await update.effective_message.reply_text("هذا الأمر للمشرفين فقط.")
            return
        return await handler(update, context)
    return wrapper


def target(update):
    return update.effective_message.reply_to_message.from_user if update.effective_message.reply_to_message else None


async def start(update, context):
    await update.message.reply_text("مرحبًا، أنا Erasa للحماية والردود والألعاب والنقاط. أرسل /help لرؤية الأوامر.")


async def help_cmd(update, context):
    await update.message.reply_text("""أوامر Erasa:

الحماية والإدارة:
/warn /warnings /mute 10m /unmute /ban /unban ID /del
/links on|off /welcome on|off /word add كلمة /word remove كلمة

النقاط والألعاب:
/points — نقاطك
/top — ترتيب الأعضاء
/dice — نرد
/guess — لعبة تخمين رقم
/rps حجر أو ورق أو مقص
/quiz — سؤال معلومات

الردود التلقائية والترحيب تعملان تلقائيًا.""")


async def warn(update, context):
    user = target(update)
    if not user:
        return await update.message.reply_text("استخدم الأمر بالرد على رسالة العضو.")
    chat_id = update.effective_chat.id
    with db() as con:
        con.execute("INSERT INTO warnings VALUES(?,?,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1", (chat_id, user.id))
        count = con.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user.id)).fetchone()[0]
    limit = settings(chat_id)["max_warnings"]
    if count >= limit:
        await update.effective_chat.ban_member(user.id)
        text = f"تم حظر {user.mention_html} بعد {count} تحذيرات."
    else:
        text = f"تم تحذير {user.mention_html}: {count}/{limit}."
    await update.message.reply_html(text)


async def warnings(update, context):
    user = target(update) or update.effective_user
    with db() as con:
        row = con.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (update.effective_chat.id, user.id)).fetchone()
    await update.message.reply_text(f"تحذيرات {user.full_name}: {row[0] if row else 0}")


async def mute(update, context):
    user = target(update)
    if not user:
        return await update.message.reply_text("استخدم الأمر بالرد على رسالة العضو، مثل /mute 10m")
    value = context.args[0] if context.args else "10m"
    match = re.fullmatch(r"(\d+)([mhd])", value.lower())
    if not match:
        return await update.message.reply_text("الصيغة: /mute 10m أو /mute 2h أو /mute 1d")
    n, unit = int(match.group(1)), match.group(2)
    until = datetime.now(timezone.utc) + timedelta(seconds=n * {"m": 60, "h": 3600, "d": 86400}[unit])
    await update.effective_chat.restrict_member(user.id, ChatPermissions(can_send_messages=False), until_date=until)
    await update.message.reply_html(f"تم كتم {user.mention_html} لمدة {value}.")


async def unmute(update, context):
    user = target(update)
    if not user: return await update.message.reply_text("استخدم الأمر بالرد على رسالة العضو.")
    await update.effective_chat.restrict_member(user.id, ChatPermissions(can_send_messages=True, can_send_photos=True, can_send_videos=True, can_send_documents=True, can_send_audios=True, can_send_voice_notes=True, can_send_polls=True, can_add_web_page_previews=True, can_send_other_messages=True))
    await update.message.reply_html(f"تم فك كتم {user.mention_html}.")


async def ban(update, context):
    user = target(update)
    if not user: return await update.message.reply_text("استخدم الأمر بالرد على رسالة العضو.")
    await update.effective_chat.ban_member(user.id)
    await update.message.reply_html(f"تم حظر {user.mention_html}.")


async def unban(update, context):
    if not context.args: return await update.message.reply_text("استخدم /unban USER_ID")
    await update.effective_chat.unban_member(int(context.args[0]))
    await update.message.reply_text("تم فك الحظر.")


async def delete(update, context):
    if update.message.reply_to_message: await update.message.reply_to_message.delete()
    await update.message.delete()


async def settings_cmd(update, context):
    s = settings(update.effective_chat.id)
    await update.message.reply_text(f"الروابط: {'ممنوعة' if s['links_blocked'] else 'مسموحة'}\nالترحيب: {'مفعل' if s['welcome_enabled'] else 'معطل'}\nحد التحذيرات: {s['max_warnings']}\nالكلمات الممنوعة: {s['banned_words'] or 'لا توجد'}")


@is_admin
async def links(update, context):
    if not context.args or context.args[0] not in ("on", "off"): return await update.message.reply_text("استخدم /links on أو /links off")
    set_setting(update.effective_chat.id, "links_blocked", int(context.args[0] == "on"))
    await update.message.reply_text("تم تحديث منع الروابط.")


@is_admin
async def welcome(update, context):
    if not context.args or context.args[0] not in ("on", "off"): return await update.message.reply_text("استخدم /welcome on أو /welcome off")
    set_setting(update.effective_chat.id, "welcome_enabled", int(context.args[0] == "on"))
    await update.message.reply_text("تم تحديث الترحيب.")


@is_admin
async def word(update, context):
    if len(context.args) < 2 or context.args[0] not in ("add", "remove"): return await update.message.reply_text("استخدم /word add كلمة أو /word remove كلمة")
    action, term = context.args[0], " ".join(context.args[1:]).lower()
    current = {x for x in settings(update.effective_chat.id)["banned_words"].split(",") if x}
    current.add(term) if action == "add" else current.discard(term)
    set_setting(update.effective_chat.id, "banned_words", ",".join(current))
    await update.message.reply_text("تم تحديث الكلمات الممنوعة.")


async def points(update, context):
    add_points(update.effective_chat.id, update.effective_user, 1)
    with db() as con:
        row = con.execute("SELECT score FROM points WHERE chat_id=? AND user_id=?", (update.effective_chat.id, update.effective_user.id)).fetchone()
    await update.message.reply_text(f"نقاطك: {row[0] if row else 0}")


async def top(update, context):
    with db() as con:
        rows = con.execute("SELECT name,score FROM points WHERE chat_id=? ORDER BY score DESC LIMIT 10", (update.effective_chat.id,)).fetchall()
    text = "🏆 ترتيب النقاط:\n" + ("\n".join(f"{i}. {r['name']} — {r['score']}" for i, r in enumerate(rows, 1)) or "لا توجد نقاط بعد.")
    await update.message.reply_text(text)


async def dice(update, context):
    add_points(update.effective_chat.id, update.effective_user, 2)
    await update.message.reply_text(f"🎲 نتيجتك: {random.randint(1, 6)} — حصلت على نقطتين!")


async def guess(update, context):
    number = random.randint(1, 5)
    context.chat_data["guess"] = number
    await update.message.reply_text("خمنت رقمًا من 1 إلى 5. أرسل رقمك الآن.")


async def rps(update, context):
    choices = ["حجر", "ورق", "مقص"]
    user = " ".join(context.args).strip() if context.args else ""
    if user not in choices: return await update.message.reply_text("استخدم مثلًا: /rps حجر")
    bot = random.choice(choices)
    win = (user == "حجر" and bot == "مقص") or (user == "ورق" and bot == "حجر") or (user == "مقص" and bot == "ورق")
    if user == bot: result = "تعادل!"
    elif win: add_points(update.effective_chat.id, update.effective_user, 5); result = "فزت وحصلت على 5 نقاط!"
    else: result = "خسرت هذه المرة."
    await update.message.reply_text(f"أنا اخترت {bot}. {result}")


async def quiz(update, context):
    questions = [("ما عاصمة المغرب؟", "الرباط"), ("كم عدد أيام الأسبوع؟", "7"), ("ما الكوكب الأحمر؟", "المريخ")]
    q, a = random.choice(questions)
    context.chat_data["quiz"] = a
    await update.message.reply_text(f"سؤال: {q}\nأول إجابة صحيحة تحصل على 10 نقاط!")


async def new_member(update, context):
    if not update.message.new_chat_members or not settings(update.effective_chat.id)["welcome_enabled"]: return
    names = ", ".join(x.mention_html for x in update.message.new_chat_members)
    await update.message.reply_html(f"أهلًا وسهلًا {names}! اكتب /help لمعرفة ألعاب ونقاط Erasa.")


async def moderation(update, context):
    msg = update.effective_message
    if not msg or not msg.text or not update.effective_chat or update.effective_chat.type == "private" or msg.from_user.is_bot: return
    chat_id = update.effective_chat.id
    add_points(chat_id, msg.from_user, 1)
    text = msg.text.strip().lower()
    s = settings(chat_id)
    if s["links_blocked"] and URL_RE.search(text):
        await msg.delete(); return
    if any(w and w in text for w in s["banned_words"].split(",")):
        await msg.delete(); return
    recent = context.chat_data.setdefault("recent", {})
    now = datetime.now(timezone.utc)
    if msg.from_user.id in recent and recent[msg.from_user.id][0] == text and (now - recent[msg.from_user.id][1]).total_seconds() < DUPLICATE_WINDOW:
        await msg.delete(); return
    recent[msg.from_user.id] = (text, now)
    if context.chat_data.get("guess") and text.isdigit():
        if int(text) == context.chat_data["guess"]:
            add_points(chat_id, msg.from_user, 10); context.chat_data.pop("guess", None)
            await msg.reply_text(f"إجابة صحيحة يا {msg.from_user.first_name}! حصلت على 10 نقاط.")
    if context.chat_data.get("quiz") and text == context.chat_data["quiz"].lower():
        add_points(chat_id, msg.from_user, 10); context.chat_data.pop("quiz", None)
        await msg.reply_text(f"إجابة صحيحة يا {msg.from_user.first_name}! حصلت على 10 نقاط.")
    replies = {"السلام عليكم": "وعليكم السلام ورحمة الله وبركاته", "مرحبا": "أهلًا بك!", "القوانين": "احترم الأعضاء، امنع السبام، ولا ترسل روابط دون إذن."}
    for key, value in replies.items():
        if key in text:
            await msg.reply_text(value); break


async def error_handler(update, context):
    logger.error("Unhandled error: %s", context.error)


def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    for command, handler in [("start", start), ("help", help_cmd), ("warn", is_admin(warn)), ("warnings", warnings), ("mute", is_admin(mute)), ("unmute", is_admin(unmute)), ("ban", is_admin(ban)), ("unban", is_admin(unban)), ("del", is_admin(delete)), ("settings", settings_cmd), ("links", links), ("welcome", welcome), ("word", word), ("points", points), ("top", top), ("dice", dice), ("guess", guess), ("rps", rps), ("quiz", quiz)]:
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, moderation))
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
