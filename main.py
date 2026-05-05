import asyncio
import base64
import html
import io
import json
import logging
import os
import re
import sys
import time
import tempfile
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite

TELEGRAM_BOT_TOKEN = "ВАШ_ТОКЕН_БОТА"
OPENAI_API_KEY     = "ВАШ_КЛЮЧ_OPENAI"
ADMIN_IDS          = [123456789]
DATABASE_PATH      = "bot_database.db"

FREE_DAILY_LIMIT        = 30
FREE_IMAGE_DAILY_LIMIT  = 5
FREE_MEDIA_DAILY_LIMIT  = 5
MAX_CONTEXT_MSGS        = 20
MAX_FILE_SIZE_MB        = 20

GPT_MODEL      = "gpt-4o"
WHISPER_MODEL  = "whisper-1"
IMAGE_MODEL    = "gpt-image-1.5"
IMAGE_FALLBACKS = ("gpt-image-1", "dall-e-3")
VISION_DETAIL  = "high"

SYSTEM_PROMPT = """Ты — умный, дружелюбный и полезный AI-ассистент в Telegram.
Отвечай на русском языке, если пользователь пишет по-русски.
Используй форматирование Markdown для структурирования ответов.
Для кода используй блоки с указанием языка.
Будь лаконичен, но информативен. Используй эмодзи там, где это уместно."""

IMAGE_TRIGGER_RE = re.compile(
    r"(?:^|\b)(?:сгенерируй|создай|нарисуй|сделай)"
    r"(?:\s+мне)?\s+(?:фото|фотографию|картин(?:ку|ка)|изображение)\b[:\s,-]*(.*)",
    flags=re.IGNORECASE,
)
IMAGE_EDIT_TRIGGER_RE = re.compile(
    r"\b(добавь|добавить|измени|изменить|отредактируй|отредактировать|убери|убрать|замени|заменить|сделай)\b",
    flags=re.IGNORECASE,
)
PRIVACY_POLICY_URL   = "https://example.com/privacy"
TERMS_OF_SERVICE_URL = "https://example.com/terms"

try:
    from telegram import (
        Update, InlineKeyboardButton, InlineKeyboardMarkup,
        BotCommand, InputFile, ReplyKeyboardMarkup
    )
    from telegram.ext import (
        Application, CommandHandler, MessageHandler, CallbackQueryHandler,
        filters, ContextTypes
    )
    from telegram.constants import ParseMode, ChatAction
    from telegram.error import TelegramError
except ImportError:
    print("  Установите: pip install python-telegram-bot")
    sys.exit(1)

try:
    from openai import AsyncOpenAI
except ImportError:
    print("  Установите: pip install openai")
    sys.exit(1)

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("ChatGPTBot")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


async def init_db():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY,
                username      TEXT,
                full_name     TEXT,
                joined_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_banned     INTEGER DEFAULT 0,
                referred_by   INTEGER,
                referral_code TEXT UNIQUE,
                language      TEXT DEFAULT 'ru',
                gpt_model     TEXT DEFAULT 'gpt-4o',
                system_prompt TEXT
            );

            CREATE TABLE IF NOT EXISTS usage (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                date        TEXT,
                msg_count   INTEGER DEFAULT 0,
                tokens_used INTEGER DEFAULT 0,
                image_count INTEGER DEFAULT 0,
                media_count INTEGER DEFAULT 0,
                UNIQUE(user_id, date)
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                role        TEXT,
                content     TEXT,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS promo_codes (
                code        TEXT PRIMARY KEY,
                discount    INTEGER DEFAULT 100,
                uses_left   INTEGER DEFAULT 1,
                plan_days   INTEGER DEFAULT 30,
                created_by  INTEGER,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id  INTEGER,
                referred_id  INTEGER PRIMARY KEY,
                bonus_given  INTEGER DEFAULT 0,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS broadcasts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id    INTEGER,
                text        TEXT,
                sent_to     INTEGER DEFAULT 0,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS app_config (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL
            );
        """)

        for migration in (
            "ALTER TABLE usage ADD COLUMN image_count INTEGER DEFAULT 0",
            "ALTER TABLE usage ADD COLUMN media_count INTEGER DEFAULT 0",
        ):
            try:
                await db.execute(migration)
            except Exception:
                pass

        defaults = {
            "free_daily_limit":       str(FREE_DAILY_LIMIT),
            "free_image_daily_limit": str(FREE_IMAGE_DAILY_LIMIT),
            "free_media_daily_limit": str(FREE_MEDIA_DAILY_LIMIT),
        }
        for key, value in defaults.items():
            await db.execute(
                "INSERT OR IGNORE INTO app_config (key, value) VALUES (?, ?)",
                (key, value),
            )
        await db.commit()
    logger.info("  База данных инициализирована")


async def get_or_create_user(user_id: int, username: str, full_name: str) -> dict:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        user = await cursor.fetchone()
        if not user:
            ref_code = f"REF{user_id}"
            await db.execute(
                """INSERT INTO users (user_id, username, full_name, referral_code)
                   VALUES (?, ?, ?, ?)""",
                (user_id, username, full_name, ref_code)
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            user = await cursor.fetchone()
        return dict(user)


async def get_config_value(key: str, default: str) -> str:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("SELECT value FROM app_config WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_config_value(key: str, value: str):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO app_config (key, value)
               VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, value),
        )
        await db.commit()


async def load_runtime_config():
    global FREE_DAILY_LIMIT, FREE_IMAGE_DAILY_LIMIT, FREE_MEDIA_DAILY_LIMIT

    def safe_int(value: str, fallback: int) -> int:
        try:
            return int(value)
        except Exception:
            return fallback

    FREE_DAILY_LIMIT       = safe_int(await get_config_value("free_daily_limit",       str(FREE_DAILY_LIMIT)),       FREE_DAILY_LIMIT)
    FREE_IMAGE_DAILY_LIMIT = safe_int(await get_config_value("free_image_daily_limit", str(FREE_IMAGE_DAILY_LIMIT)), FREE_IMAGE_DAILY_LIMIT)
    FREE_MEDIA_DAILY_LIMIT = safe_int(await get_config_value("free_media_daily_limit", str(FREE_MEDIA_DAILY_LIMIT)), FREE_MEDIA_DAILY_LIMIT)


async def get_daily_usage_stats(user_id: int) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT msg_count, tokens_used, image_count, media_count FROM usage WHERE user_id=? AND date=?",
            (user_id, today),
        )
        row = await cursor.fetchone()
        if not row:
            return {"msg_count": 0, "tokens_used": 0, "image_count": 0, "media_count": 0}
        return {
            "msg_count":   row[0] or 0,
            "tokens_used": row[1] or 0,
            "image_count": row[2] or 0,
            "media_count": row[3] or 0,
        }


async def get_daily_count(user_id: int) -> int:
    return (await get_daily_usage_stats(user_id))["msg_count"]


async def increment_usage(user_id: int, tokens: int = 0):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO usage (user_id, date, msg_count, tokens_used, image_count, media_count)
               VALUES (?, ?, 1, ?, 0, 0)
               ON CONFLICT(user_id, date) DO UPDATE SET
               msg_count   = msg_count + 1,
               tokens_used = tokens_used + ?""",
            (user_id, today, tokens, tokens)
        )
        await db.commit()


async def increment_image_usage(user_id: int):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO usage (user_id, date, msg_count, tokens_used, image_count, media_count)
               VALUES (?, ?, 0, 0, 1, 0)
               ON CONFLICT(user_id, date) DO UPDATE SET
               image_count = image_count + 1""",
            (user_id, today),
        )
        await db.commit()


async def increment_media_usage(user_id: int):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO usage (user_id, date, msg_count, tokens_used, image_count, media_count)
               VALUES (?, ?, 0, 0, 0, 1)
               ON CONFLICT(user_id, date) DO UPDATE SET
               media_count = media_count + 1""",
            (user_id, today),
        )
        await db.commit()


async def get_context(user_id: int) -> list:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """SELECT role, content FROM conversations
               WHERE user_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (user_id, MAX_CONTEXT_MSGS)
        )
        rows = await cursor.fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


async def add_to_context(user_id: int, role: str, content: str):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO conversations (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content)
        )
        await db.execute(
            """DELETE FROM conversations WHERE user_id = ? AND id NOT IN (
               SELECT id FROM conversations WHERE user_id = ?
               ORDER BY created_at DESC LIMIT ?)""",
            (user_id, user_id, MAX_CONTEXT_MSGS * 2)
        )
        await db.commit()


async def clear_context(user_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "DELETE FROM conversations WHERE user_id = ?", (user_id,)
        )
        await db.commit()


async def get_user_stats(user_id: int) -> dict:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT SUM(msg_count), SUM(tokens_used) FROM usage WHERE user_id=?",
            (user_id,)
        )
        row = await cursor.fetchone()
        total_msgs   = row[0] or 0
        total_tokens = row[1] or 0

        cursor = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (user_id,)
        )
        ref_count = (await cursor.fetchone())[0]

    return {
        "total_msgs":   total_msgs,
        "total_tokens": total_tokens,
        "referrals":    ref_count,
    }


async def get_global_stats() -> dict:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM users WHERE joined_at >= date('now', '-1 day')"
        )
        new_today = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT SUM(msg_count) FROM usage WHERE date = date('now')"
        )
        msgs_today = (await cursor.fetchone())[0] or 0

    return {
        "total_users": total_users,
        "new_today":   new_today,
        "msgs_today":  msgs_today,
    }


async def get_all_users() -> list:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id FROM users WHERE is_banned = 0"
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]


async def ban_user(user_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))
        await db.commit()


async def unban_user(user_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))
        await db.commit()


async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT is_banned FROM users WHERE user_id=?", (user_id,)
        )
        row = await cursor.fetchone()
        return bool(row and row[0])


async def create_promo(code: str, discount: int, uses: int, days: int, admin_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO promo_codes
               (code, discount, uses_left, plan_days, created_by) VALUES (?, ?, ?, ?, ?)""",
            (code.upper(), discount, uses, days, admin_id)
        )
        await db.commit()


async def use_promo(code: str, user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM promo_codes WHERE code=? AND uses_left > 0",
            (code.upper(),)
        )
        promo = await cursor.fetchone()
        if not promo:
            return None
        await db.execute(
            "UPDATE promo_codes SET uses_left = uses_left - 1 WHERE code=?",
            (code.upper(),)
        )
        await db.commit()
        return dict(promo)


async def get_user_model(user_id: int) -> str:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT gpt_model FROM users WHERE user_id=?", (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else GPT_MODEL


async def set_user_model(user_id: int, model: str):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE users SET gpt_model=? WHERE user_id=?", (model, user_id)
        )
        await db.commit()


async def get_user_system_prompt(user_id: int) -> str:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT system_prompt FROM users WHERE user_id=?", (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else SYSTEM_PROMPT


async def set_user_system_prompt(user_id: int, prompt: str):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "UPDATE users SET system_prompt=? WHERE user_id=?", (prompt, user_id)
        )
        await db.commit()


def markdown_to_html(text: str) -> str:
    if not text:
        return ""

    def escape_html(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    code_blocks = {}
    counter = [0]

    def save_code_block(m):
        lang = m.group(1).strip() if m.group(1) else ""
        code = escape_html(m.group(2))
        key  = f"\x00CODEBLOCK{counter[0]}\x00"
        lang_label = f"<code>{lang}</code>\n" if lang else ""
        code_blocks[key] = f"<pre>{lang_label}<code>{code}</code></pre>"
        counter[0] += 1
        return key

    text = re.sub(r"```(\w*)\n?(.*?)```", save_code_block, text, flags=re.DOTALL)

    inline_codes = {}

    def save_inline_code(m):
        code = escape_html(m.group(1))
        key  = f"\x00INLINE{counter[0]}\x00"
        inline_codes[key] = f"<code>{code}</code>"
        counter[0] += 1
        return key

    text = re.sub(r"`([^`\n]+)`", save_inline_code, text)

    lines = text.split("\n")
    escaped_lines = []
    for line in lines:
        if "\x00" in line:
            escaped_lines.append(line)
        else:
            escaped_lines.append(escape_html(line))
    text = "\n".join(escaped_lines)

    text = re.sub(r"^###\s+(.+)$", r"<b>📌 \1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^##\s+(.+)$",  r"<b>🔷 \1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"^#\s+(.+)$",   r"<b>⭐ \1</b>", text, flags=re.MULTILINE)

    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__",     r"<b>\1</b>", text)

    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)",       r"<i>\1</i>", text)

    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    text = re.sub(r"^[\-\*\+•]\s+(.+)$", r"  • \1", text, flags=re.MULTILINE)
    text = re.sub(r"^\d+\.\s+(.+)$",     r"  \1",   text, flags=re.MULTILINE)
    text = re.sub(r"^[-_*]{3,}$", "──────────────────", text, flags=re.MULTILINE)

    for key, val in code_blocks.items():
        text = text.replace(key, val)
    for key, val in inline_codes.items():
        text = text.replace(key, val)

    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def split_long_message(text: str, max_len: int = 4000) -> list:
    if len(text) <= max_len:
        return [text]

    parts   = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            if current:
                parts.append(current)
            current = line
        else:
            current += ("\n" if current else "") + line
    if current:
        parts.append(current)
    return parts


async def ask_gpt(user_id: int, user_message: str,
                  image_base64: str = None, extra_context: str = None) -> tuple[str, int]:
    model   = await get_user_model(user_id)
    system  = await get_user_system_prompt(user_id)
    history = await get_context(user_id)

    messages = [{"role": "system", "content": system}]
    messages.extend(history)

    if image_base64:
        content = [
            {"type": "text", "text": user_message},
            {
                "type": "image_url",
                "image_url": {
                    "url":    f"data:image/jpeg;base64,{image_base64}",
                    "detail": VISION_DETAIL,
                },
            },
        ]
        messages.append({"role": "user", "content": content})
    elif extra_context:
        messages.append({
            "role":    "user",
            "content": f"{extra_context}\n\n{user_message}",
        })
    else:
        messages.append({"role": "user", "content": user_message})

    response = await openai_client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=2048,
        temperature=0.7,
    )

    answer = response.choices[0].message.content
    tokens = response.usage.total_tokens if response.usage else 0

    await add_to_context(user_id, "user",      user_message)
    await add_to_context(user_id, "assistant", answer)

    return answer, tokens


async def transcribe_audio(file_path: str) -> str:
    with open(file_path, "rb") as f:
        transcript = await openai_client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=f,
            response_format="text",
        )
    return transcript


def extract_image_prompt(text: str) -> Optional[str]:
    raw_text = (text or "").strip()
    if not raw_text:
        return None

    match = IMAGE_TRIGGER_RE.search(raw_text)
    if match:
        candidate = match.group(1).strip(" \t\n\r:,-")
        if candidate:
            candidate = re.sub(r"\bна\s+фото\b", "", candidate, flags=re.IGNORECASE).strip(" \t\n\r:,-")
            return candidate
        return ""

    lowered       = raw_text.lower()
    verbs         = ("сгенерируй", "сгенериру", "создай", "нарисуй", "сделай", "generate", "draw")
    image_markers = ("фото", "фотограф", "картин", "изображен", "арт", "рисунок", "на фото", "image")
    if not any(v in lowered for v in verbs):
        return None
    if not any(m in lowered for m in image_markers):
        return None

    candidate = re.sub(r"(?i)\b(пожалуйста|плиз)\b", "", raw_text)
    candidate = re.sub(
        r"(?i)(сгенерируй(?:те)?|создай(?:те)?|нарисуй(?:те)?|сделай(?:те)?|generate|draw)",
        "", candidate, count=1,
    )
    candidate = re.sub(
        r"(?i)\b(мне|пожалуйста|на фото|фото|картинку|картинка|изображение|арт|рисунок)\b",
        " ", candidate,
    )
    candidate = re.sub(r"\s{2,}", " ", candidate).strip(" \t\n\r:,-")
    return candidate or ""


def extract_image_edit_prompt(text: str) -> Optional[str]:
    raw_text = (text or "").strip()
    if not raw_text:
        return None
    if not IMAGE_EDIT_TRIGGER_RE.search(raw_text):
        return None
    return raw_text[:500]


async def generate_image(prompt: str) -> tuple[str | bytes, str]:
    last_error     = None
    models_to_try  = [IMAGE_MODEL, *IMAGE_FALLBACKS]

    for model_name in models_to_try:
        try:
            quality  = "high" if model_name.startswith("gpt-image") else "hd"
            response = await openai_client.images.generate(
                model=model_name,
                prompt=prompt,
                size="1024x1024",
                quality=quality,
                n=1,
            )

            if not response.data:
                raise RuntimeError("Пустой ответ от модели изображений.")

            item     = response.data[0]
            b64_data = getattr(item, "b64_json", None)
            if b64_data:
                return base64.b64decode(b64_data), model_name

            image_url = getattr(item, "url", None)
            if image_url:
                return image_url, model_name

            raise RuntimeError("В ответе нет ни b64_json, ни url.")
        except Exception as e:
            last_error = e
            logger.warning(f"Image model fallback: {model_name} failed: {e}")

    raise RuntimeError(f"Не удалось сгенерировать изображение: {last_error}")


async def generate_image_from_payload(prompt: str) -> tuple[str | bytes, str]:
    return await generate_image(prompt)


async def edit_image(
    original_image: bytes,
    edit_prompt: str,
    original_image_b64: str,
) -> tuple[str | bytes, str]:
    edit_models_to_try = [IMAGE_MODEL, *IMAGE_FALLBACKS, "dall-e-2"]
    last_error = None

    for model_name in edit_models_to_try:
        try:
            response = await openai_client.images.edit(
                model=model_name,
                image=("source.jpg", original_image, "image/jpeg"),
                prompt=edit_prompt,
                size="1024x1024",
                n=1,
            )

            if not response.data:
                raise RuntimeError("Пустой ответ от API редактирования.")

            item     = response.data[0]
            b64_data = getattr(item, "b64_json", None)
            if b64_data:
                return base64.b64decode(b64_data), model_name

            image_url = getattr(item, "url", None)
            if image_url:
                return image_url, model_name

            raise RuntimeError("В ответе нет ни b64_json, ни url.")
        except Exception as e:
            last_error = e
            logger.warning(f"Image edit fallback: {model_name} failed: {e}")

    vision_response = await openai_client.chat.completions.create(
        model=GPT_MODEL,
        messages=[
            {
                "role":    "system",
                "content": (
                    "Ты создаешь промпт для генерации изображения. "
                    "Кратко опиши исходное фото и внеси правку пользователя, "
                    "сохранив композицию и объекты максимально близко."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Правка: {edit_prompt}"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url":    f"data:image/jpeg;base64,{original_image_b64}",
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
        max_tokens=600,
        temperature=0.4,
    )
    generated_prompt = (vision_response.choices[0].message.content or "").strip()
    if not generated_prompt:
        generated_prompt = (
            f"Фотореалистичное изображение, максимально повторяющее исходное фото, "
            f"но с изменением: {edit_prompt}"
        )

    edited_payload, model_used = await generate_image_from_payload(generated_prompt)
    return edited_payload, f"{model_used} (regenerate)"


async def process_image_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    prompt: str,
) -> None:
    if user_id not in ADMIN_IDS:
        usage = await get_daily_usage_stats(user_id)
        if usage["image_count"] >= FREE_IMAGE_DAILY_LIMIT:
            await update.message.reply_text(
                f"Лимит генерации изображений исчерпан: {FREE_IMAGE_DAILY_LIMIT}/день.",
                parse_mode=ParseMode.HTML,
            )
            return

    await send_typing(context, update.effective_chat.id)
    status = await update.message.reply_text(
        "Создаю изображение, подождите...",
        parse_mode=ParseMode.HTML,
    )

    try:
        image_payload, model_used = await generate_image(prompt)
        try:
            await context.bot.delete_message(update.effective_chat.id, status.message_id)
        except TelegramError:
            pass

        safe_prompt = html.escape(prompt[:200])
        photo = image_payload
        if isinstance(image_payload, bytes):
            photo = InputFile(io.BytesIO(image_payload), filename="generated.png")

        await context.bot.send_photo(
            update.effective_chat.id,
            photo=photo,
            caption=f"<b>Изображение готово</b> • <code>{model_used}</code>\n<i>{safe_prompt}</i>",
            parse_mode=ParseMode.HTML,
        )
        await increment_image_usage(user_id)
        await increment_usage(user_id, 100)
    except Exception as e:
        logger.error(f"Image generation error: {e}")
        await status.edit_text(
            f"Ошибка генерации: {html.escape(str(e)[:250])}",
            parse_mode=ParseMode.HTML,
        )


async def extract_text_from_file(file_content: bytes, filename: str) -> str:
    text         = ""
    filename_lower = filename.lower()

    try:
        if filename_lower.endswith((".txt", ".md", ".py", ".js", ".ts",
                                    ".html", ".css", ".json", ".xml",
                                    ".yaml", ".yml", ".csv", ".log",
                                    ".sh", ".bash", ".rs", ".go",
                                    ".java", ".cpp", ".c", ".h",
                                    ".php", ".rb", ".swift", ".kt")):
            text = file_content.decode("utf-8", errors="replace")

        elif filename_lower.endswith(".pdf"):
            try:
                import io
                import PyPDF2
                pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_content))
                pages = []
                for page in pdf_reader.pages:
                    pages.append(page.extract_text() or "")
                text = "\n".join(pages)
            except ImportError:
                text = "[PDF: установите PyPDF2 для чтения PDF]"

        elif filename_lower.endswith((".doc", ".docx")):
            try:
                import io
                from docx import Document
                doc  = Document(io.BytesIO(file_content))
                text = "\n".join(p.text for p in doc.paragraphs)
            except ImportError:
                text = "[DOCX: установите python-docx для чтения Word]"

        else:
            text = file_content.decode("utf-8", errors="replace")
    except Exception as e:
        text = f"[Ошибка чтения файла: {e}]"

    return text[:15000]


async def check_access(user_id: int) -> tuple[bool, str]:
    if await is_banned(user_id):
        return False, "  Вы заблокированы. Обратитесь в поддержку."

    if user_id in ADMIN_IDS:
        return True, ""

    count = await get_daily_count(user_id)
    if count >= FREE_DAILY_LIMIT:
        return False, (
            f"⚠️ <b>Лимит исчерпан!</b>\n\n"
            f"На бесплатном тарифе: <b>{FREE_DAILY_LIMIT} сообщений/день</b>\n"
            f"Использовано сегодня: <b>{count}/{FREE_DAILY_LIMIT}</b>\n\n"
            f"Лимит сбросится в <b>00:00</b> по вашему времени."
        )
    return True, ""


async def send_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass


async def send_safe(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                    text: str, **kwargs):
    parts = split_long_message(markdown_to_html(text))
    sent  = None
    for part in parts:
        try:
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=part,
                parse_mode=ParseMode.HTML,
                **kwargs,
            )
        except TelegramError as e:
            logger.warning(f"HTML send failed: {e}, falling back to plain text")
            plain = re.sub(r"<[^>]+>", "", part)
            sent  = await context.bot.send_message(
                chat_id=chat_id,
                text=plain,
                **{k: v for k, v in kwargs.items() if k != "parse_mode"},
            )
    return sent


def make_main_keyboard(is_admin: bool = False) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("Новый диалог", callback_data="new_chat"),
            InlineKeyboardButton("Статистика",   callback_data="my_stats"),
        ],
        [
            InlineKeyboardButton("Настройки", callback_data="settings"),
        ],
        [
            InlineKeyboardButton("Промокод", callback_data="use_promo"),
            InlineKeyboardButton("Реферал",  callback_data="referral"),
        ],
        [
            InlineKeyboardButton("Помощь", callback_data="help"),
        ],
    ]
    if is_admin:
        kb.append([InlineKeyboardButton("Панель Admin", callback_data="admin_panel")])
    return InlineKeyboardMarkup(kb)


def make_reply_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    kb = [
        ["Новый диалог", "Статистика"],
        ["Настройки"],
        ["Промокод", "Реферал"],
        ["Помощь"],
    ]
    if is_admin:
        kb.append(["Панель Admin"])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True, is_persistent=True)


def make_settings_keyboard(current_model: str) -> InlineKeyboardMarkup:
    models = [
        ("gpt-4o",       "GPT-4o"),
        ("gpt-4o-mini",  "GPT-4o Mini"),
        ("gpt-4-turbo",  "GPT-4 Turbo"),
    ]
    kb  = []
    row = []
    for model_id, label in models:
        check = "• " if current_model == model_id else ""
        row.append(InlineKeyboardButton(f"{check}{label}", callback_data=f"set_model:{model_id}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    kb.append([InlineKeyboardButton("✏️ Свой системный промпт", callback_data="set_system_prompt")])
    kb.append([InlineKeyboardButton("Сбросить промпт",          callback_data="reset_system_prompt")])
    kb.append([InlineKeyboardButton("Назад",                    callback_data="main_menu")])
    return InlineKeyboardMarkup(kb)


def make_admin_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("Статистика", callback_data="admin_stats"),
            InlineKeyboardButton("Рассылка",   callback_data="admin_broadcast"),
        ],
        [
            InlineKeyboardButton("Промокод", callback_data="admin_create_promo"),
        ],
        [
            InlineKeyboardButton("Лимиты", callback_data="admin_tariffs"),
        ],
        [
            InlineKeyboardButton("Бан",   callback_data="admin_ban"),
            InlineKeyboardButton("Разбан", callback_data="admin_unban"),
        ],
        [InlineKeyboardButton("Назад", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(kb)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await get_or_create_user(user.id, user.username or "", user.full_name or "")

    if context.args:
        ref_code = context.args[0]
        if ref_code.startswith("REF") and ref_code != f"REF{user.id}":
            try:
                ref_uid = int(ref_code[3:])
                async with aiosqlite.connect(DATABASE_PATH) as db:
                    await db.execute(
                        """INSERT OR IGNORE INTO referrals (referrer_id, referred_id)
                           VALUES (?, ?)""",
                        (ref_uid, user.id)
                    )
                    await db.commit()
                try:
                    await context.bot.send_message(
                        ref_uid,
                        "По вашей реферальной ссылке присоединился новый пользователь.",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            except (ValueError, Exception) as e:
                logger.warning(f"Referral error: {e}")

    is_admin = user.id in ADMIN_IDS

    welcome = (
        f"Привет, <b>{user.first_name}</b>.\n\n"
        f"Я — умный AI-ассистент на базе <b>ChatGPT {GPT_MODEL}</b>.\n\n"
        f"<b>Что я умею:</b>\n"
        f"  • Отвечать на любые вопросы\n"
        f"  • Анализировать фотографии\n"
        f"  • Расшифровывать голосовые\n"
        f"  • Читать документы и код\n"
        f"  • Генерировать изображения обычной фразой: «сгенерируй мне ... на фото»\n"
        f"  • Писать и объяснять код\n\n"
        f"Лимит: <b>{FREE_DAILY_LIMIT} сообщений/день</b>\n\n"
        f"Просто напишите мне что-нибудь или выберите действие:"
    )

    await update.message.reply_text(
        welcome,
        parse_mode=ParseMode.HTML,
        reply_markup=make_reply_keyboard(is_admin=is_admin),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "<b>Справка по командам</b>\n\n"
        "<b>Основные:</b>\n"
        "  /start — Начало работы\n"
        "  /new — Новый диалог (очистить контекст)\n"
        "  /stats — Ваша статистика\n"
        "  /settings — Настройки бота\n"
        "  /promo [код] — Применить промокод\n"
        "  /ref — Реферальная ссылка\n"
        "  /help — Эта справка\n\n"
        "<b>Что умеет бот:</b>\n"
        "  <b>Текст</b> — просто пишите\n"
        "  <b>Голосовые / кружочки</b> — отправьте голосовое\n"
        "  <b>Фото</b> — отправьте с вопросом или без\n"
        "  <b>Документы</b> — .txt .pdf .py .js и др.\n"
        "  <b>Аудио/видео</b> — расшифровка речи\n\n"
        "<b>Генерация картинок без команды:</b>\n"
        "  <code>сгенерируй фото ретро-машины у океана</code>\n\n"
        "<b>Форматирование в ответах:</b>\n"
        "  • Код с подсветкой\n"
        "  • Жирный, курсив, заголовки\n"
        "  • Списки и таблицы"
    )
    await update.message.reply_text(
        help_text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Политика конфиденциальности", url=PRIVACY_POLICY_URL)],
            [InlineKeyboardButton("Пользовательское соглашение", url=TERMS_OF_SERVICE_URL)],
        ]),
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await clear_context(user_id)
    await update.message.reply_text(
        "  <b>Диалог очищен!</b>\nНачинаем с чистого листа ✨",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("  Главное меню", callback_data="main_menu")
        ]]),
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id     = update.effective_user.id
    stats       = await get_user_stats(user_id)
    today_count = await get_daily_count(user_id)
    model       = await get_user_model(user_id)

    plan_text = f"  <b>Бесплатный</b>\nСообщений сегодня: <b>{today_count}/{FREE_DAILY_LIMIT}</b>"

    msg = (
        f"  <b>Ваша статистика</b>\n\n"
        f"  Всего сообщений: <b>{stats['total_msgs']}</b>\n"
        f"🔤 Токенов использовано: <b>{stats['total_tokens']:,}</b>\n"
        f"  Рефералов: <b>{stats['referrals']}</b>\n"
        f"🤖 Текущая модель: <b>{model}</b>\n\n"
        f"  Тариф: {plan_text}"
    )
    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("  Главное меню", callback_data="main_menu")
        ]]),
    )


async def cmd_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Введите промокод:\n<code>/promo КОД</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    code  = context.args[0]
    promo = await use_promo(code, user_id)
    if not promo:
        await update.message.reply_text(
            "  Промокод не найден или исчерпан.", parse_mode=ParseMode.HTML
        )
        return

    await update.message.reply_text(
        f"  <b>Промокод активирован!</b>\n\nПромокод принят.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    bot_info = await context.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=REF{user_id}"
    stats    = await get_user_stats(user_id)

    msg = (
        f"  <b>Реферальная программа</b>\n\n"
        f"Приглашайте друзей!\n\n"
        f"  Приглашено: <b>{stats['referrals']}</b> чел.\n\n"
        f"  Ваша ссылка:\n<code>{ref_link}</code>"
    )
    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "  Поделиться",
                url=f"https://t.me/share/url?url={ref_link}&text=Попробуй%20AI-ассистента!",
            )
        ]]),
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    model   = await get_user_model(user_id)
    await update.message.reply_text(
        "  <b>Настройки</b>\n\nВыберите модель GPT:",
        parse_mode=ParseMode.HTML,
        reply_markup=make_settings_keyboard(model),
    )


async def cmd_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Доступ запрещён.")
        return
    await update.message.reply_text(
        "<b>Панель администратора</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=make_admin_keyboard(),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    await get_or_create_user(user.id, user.username or "", user.full_name or "")

    user_state = context.user_data.get("state")

    if user_state == "waiting_promo":
        context.user_data.pop("state", None)
        promo = await use_promo(text.strip(), user.id)
        if not promo:
            await update.message.reply_text(
                "  Промокод не найден или исчерпан.", parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text(
                f"  <b>Промокод активирован!</b>",
                parse_mode=ParseMode.HTML,
            )
        return

    if user_state == "waiting_system_prompt":
        context.user_data.pop("state", None)
        await set_user_system_prompt(user.id, text.strip())
        await update.message.reply_text(
            "  <b>Системный промпт сохранён!</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("  Настройки", callback_data="settings")
            ]]),
        )
        return

    if user_state and user_state.startswith("admin_"):
        await handle_admin_input(update, context, user_state, text)
        return

    quick_actions = {
        "новый диалог": cmd_new,
        "статистика":   cmd_stats,
        "настройки":    cmd_settings,
        "промокод":     cmd_promo,
        "реферал":      cmd_ref,
        "помощь":       cmd_help,
        "панель admin": cmd_admin_panel,
    }
    action = quick_actions.get((text or "").strip().lower())
    if action:
        await action(update, context)
        return

    allowed, err = await check_access(user.id)
    if not allowed:
        await update.message.reply_text(err, parse_mode=ParseMode.HTML)
        return

    image_prompt = extract_image_prompt(text)
    if image_prompt is not None:
        if not image_prompt:
            await update.message.reply_text(
                "  Напишите, что именно сгенерировать.\n"
                "Пример: <code>сгенерируй фото киберпанк-улицы под дождём</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        await process_image_request(update, context, user.id, image_prompt)
        return

    await send_typing(context, update.effective_chat.id)

    try:
        answer, tokens = await ask_gpt(user.id, text)
        await increment_usage(user.id, tokens)
        await send_safe(context, update.effective_chat.id, answer)
    except Exception as e:
        logger.error(f"GPT error for user {user.id}: {e}")
        await update.message.reply_text(
            f"  <b>Ошибка запроса к GPT:</b>\n<code>{str(e)[:300]}</code>\n\nПопробуйте ещё раз.",
            parse_mode=ParseMode.HTML,
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await get_or_create_user(user.id, user.username or "", user.full_name or "")

    allowed, err = await check_access(user.id)
    if not allowed:
        await update.message.reply_text(err, parse_mode=ParseMode.HTML)
        return

    if user.id not in ADMIN_IDS:
        usage = await get_daily_usage_stats(user.id)
        if usage["media_count"] >= FREE_MEDIA_DAILY_LIMIT:
            await update.message.reply_text(
                f"Лимит загрузки фото/документов исчерпан: {FREE_MEDIA_DAILY_LIMIT}/день.",
                parse_mode=ParseMode.HTML,
            )
            return

    await send_typing(context, update.effective_chat.id)

    caption    = update.message.caption or "Что на этом изображении? Опиши подробно."
    edit_prompt = extract_image_edit_prompt(caption)

    try:
        photo      = update.message.photo[-1]
        file       = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()

        import base64
        image_b64 = base64.b64encode(bytes(file_bytes)).decode("utf-8")

        if edit_prompt:
            if user.id not in ADMIN_IDS:
                usage = await get_daily_usage_stats(user.id)
                if usage["image_count"] >= FREE_IMAGE_DAILY_LIMIT:
                    await update.message.reply_text(
                        f"Лимит генерации изображений исчерпан: {FREE_IMAGE_DAILY_LIMIT}/день.",
                        parse_mode=ParseMode.HTML,
                    )
                    return

            status = await update.message.reply_text(
                "Редактирую изображение по вашему запросу...",
                parse_mode=ParseMode.HTML,
            )
            edited_payload, model_used = await edit_image(bytes(file_bytes), edit_prompt, image_b64)
            try:
                await context.bot.delete_message(update.effective_chat.id, status.message_id)
            except Exception:
                pass

            photo_out = edited_payload
            if isinstance(edited_payload, bytes):
                photo_out = InputFile(io.BytesIO(edited_payload), filename="edited.png")

            await context.bot.send_photo(
                update.effective_chat.id,
                photo=photo_out,
                caption=f"<b>Фото отредактировано</b> • <code>{model_used}</code>\n<i>{html.escape(edit_prompt[:220])}</i>",
                parse_mode=ParseMode.HTML,
            )
            await increment_media_usage(user.id)
            await increment_image_usage(user.id)
            await increment_usage(user.id, 100)
            return

        answer, tokens = await ask_gpt(user.id, caption, image_base64=image_b64)
        await increment_media_usage(user.id)
        await increment_usage(user.id, tokens)
        await send_safe(context, update.effective_chat.id, answer)

    except Exception as e:
        logger.error(f"Photo processing error: {e}")
        await update.message.reply_text(
            f"  Ошибка обработки изображения: {str(e)[:200]}",
            parse_mode=ParseMode.HTML,
        )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await get_or_create_user(user.id, user.username or "", user.full_name or "")

    allowed, err = await check_access(user.id)
    if not allowed:
        await update.message.reply_text(err, parse_mode=ParseMode.HTML)
        return

    status_msg = await update.message.reply_text(
        "  <b>Распознаю речь...</b>", parse_mode=ParseMode.HTML
    )
    await send_typing(context, update.effective_chat.id)

    try:
        if update.message.voice:
            file_obj = update.message.voice
        elif update.message.video_note:
            file_obj = update.message.video_note
        else:
            raise ValueError("Нет аудио в сообщении")

        tg_file = await context.bot.get_file(file_obj.file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        await tg_file.download_to_drive(tmp_path)

        transcript = await transcribe_audio(tmp_path)
        os.unlink(tmp_path)

        if not transcript.strip():
            await status_msg.edit_text("  Не удалось распознать речь.")
            return

        await status_msg.edit_text(
            f"  <b>Вы сказали:</b>\n<i>{transcript}</i>\n\n  Получаю ответ...",
            parse_mode=ParseMode.HTML,
        )

        answer, tokens = await ask_gpt(user.id, transcript)
        await increment_usage(user.id, tokens)

        await status_msg.edit_text(
            f"  <b>Вы:</b> <i>{transcript}</i>",
            parse_mode=ParseMode.HTML,
        )
        await send_safe(context, update.effective_chat.id, answer)

    except Exception as e:
        logger.error(f"Voice processing error: {e}")
        await status_msg.edit_text(
            f"  Ошибка обработки голоса: {str(e)[:200]}", parse_mode=ParseMode.HTML
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await get_or_create_user(user.id, user.username or "", user.full_name or "")

    allowed, err = await check_access(user.id)
    if not allowed:
        await update.message.reply_text(err, parse_mode=ParseMode.HTML)
        return

    if user.id not in ADMIN_IDS:
        usage = await get_daily_usage_stats(user.id)
        if usage["media_count"] >= FREE_MEDIA_DAILY_LIMIT:
            await update.message.reply_text(
                f"Лимит загрузки фото/документов исчерпан: {FREE_MEDIA_DAILY_LIMIT}/день.",
                parse_mode=ParseMode.HTML,
            )
            return

    doc     = update.message.document
    caption = update.message.caption or "Проанализируй этот документ и дай краткое резюме."

    if doc.file_size and doc.file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(
            f"  Файл слишком большой (макс. {MAX_FILE_SIZE_MB} МБ)",
            parse_mode=ParseMode.HTML,
        )
        return

    status_msg = await update.message.reply_text(
        f"  <b>Читаю файл:</b> <code>{doc.file_name}</code>...",
        parse_mode=ParseMode.HTML,
    )
    await send_typing(context, update.effective_chat.id)

    try:
        tg_file    = await context.bot.get_file(doc.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        file_text  = await extract_text_from_file(bytes(file_bytes), doc.file_name or "file.txt")

        if not file_text.strip():
            await status_msg.edit_text(
                "  Не удалось извлечь текст из файла.",
                parse_mode=ParseMode.HTML,
            )
            return

        extra = f"Содержимое файла «{doc.file_name}»:\n\n{file_text}"

        await status_msg.edit_text(
            f"  Файл прочитан ({len(file_text)} симв.). Анализирую...",
            parse_mode=ParseMode.HTML,
        )

        answer, tokens = await ask_gpt(user.id, caption, extra_context=extra)
        await increment_media_usage(user.id)
        await increment_usage(user.id, tokens)

        await status_msg.delete()
        await send_safe(context, update.effective_chat.id, answer)

    except Exception as e:
        logger.error(f"Document processing error: {e}")
        await status_msg.edit_text(
            f"  Ошибка обработки файла: {str(e)[:200]}", parse_mode=ParseMode.HTML
        )


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await get_or_create_user(user.id, user.username or "", user.full_name or "")

    allowed, err = await check_access(user.id)
    if not allowed:
        await update.message.reply_text(err, parse_mode=ParseMode.HTML)
        return

    audio = update.message.audio or update.message.video

    status_msg = await update.message.reply_text(
        "  <b>Транскрибирую аудио...</b>", parse_mode=ParseMode.HTML
    )

    try:
        tg_file = await context.bot.get_file(
            audio.file_id if hasattr(audio, "file_id") else audio.file_id
        )
        suffix = ".mp3" if update.message.audio else ".mp4"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)

        transcript = await transcribe_audio(tmp_path)
        os.unlink(tmp_path)

        await status_msg.edit_text(
            f"📝 <b>Транскрипция:</b>\n\n{transcript}",
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:
        logger.error(f"Audio processing error: {e}")
        await status_msg.edit_text(
            f"  Ошибка: {str(e)[:200]}", parse_mode=ParseMode.HTML
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user     = update.effective_user
    data     = query.data
    is_admin = user.id in ADMIN_IDS

    await get_or_create_user(user.id, user.username or "", user.full_name or "")

    if data == "main_menu":
        await query.edit_message_text(
            "  <b>Главное меню</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=make_main_keyboard(is_admin),
        )

    elif data == "new_chat":
        await clear_context(user.id)
        await query.edit_message_text(
            "  <b>Диалог очищен!</b>\nНачинаем с чистого листа ✨",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("  Главное меню", callback_data="main_menu")
            ]]),
        )

    elif data == "my_stats":
        stats       = await get_user_stats(user.id)
        today_count = await get_daily_count(user.id)
        model       = await get_user_model(user.id)
        plan_text   = f"  Бесплатный ({today_count}/{FREE_DAILY_LIMIT} сегодня)"

        msg = (
            f"  <b>Статистика</b>\n\n"
            f"  Всего сообщений: <b>{stats['total_msgs']}</b>\n"
            f"🔤 Токенов использовано: <b>{stats['total_tokens']:,}</b>\n"
            f"  Рефералов: <b>{stats['referrals']}</b>\n"
            f"🤖 Модель: <b>{model}</b>\n\n"
            f"  Тариф: {plan_text}"
        )
        await query.edit_message_text(
            msg,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("  Назад", callback_data="main_menu")
            ]]),
        )

    elif data == "settings":
        model = await get_user_model(user.id)
        await query.edit_message_text(
            "  <b>Настройки</b>\n\nВыберите модель GPT:",
            parse_mode=ParseMode.HTML,
            reply_markup=make_settings_keyboard(model),
        )

    elif data.startswith("set_model:"):
        model = data.split(":")[1]
        await set_user_model(user.id, model)
        await query.edit_message_text(
            f"  Модель изменена на <b>{model}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=make_settings_keyboard(model),
        )

    elif data == "set_system_prompt":
        context.user_data["state"] = "waiting_system_prompt"
        current = await get_user_system_prompt(user.id)
        await query.edit_message_text(
            f"✏️ <b>Системный промпт</b>\n\n"
            f"Текущий:\n<i>{current[:500]}</i>\n\n"
            f"Напишите новый промпт в следующем сообщении:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("  Отмена", callback_data="settings")
            ]]),
        )

    elif data == "reset_system_prompt":
        await set_user_system_prompt(user.id, SYSTEM_PROMPT)
        await query.edit_message_text(
            "  <b>Системный промпт сброшен</b> до стандартного.",
            parse_mode=ParseMode.HTML,
            reply_markup=make_settings_keyboard(await get_user_model(user.id)),
        )

    elif data == "use_promo":
        context.user_data["state"] = "waiting_promo"
        await query.edit_message_text(
            "<b>Введите промокод</b>\n\nНапишите код в следующем сообщении:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("  Отмена", callback_data="main_menu")
            ]]),
        )

    elif data == "referral":
        bot_info = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=REF{user.id}"
        stats    = await get_user_stats(user.id)
        msg = (
            f"  <b>Реферальная программа</b>\n\n"
            f"Приглашайте друзей!\n\n"
            f"Приглашено: <b>{stats['referrals']}</b> чел.\n\n"
            f"  Ваша ссылка:\n<code>{ref_link}</code>"
        )
        await query.edit_message_text(
            msg,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "  Поделиться",
                    url=f"https://t.me/share/url?url={ref_link}&text=Попробуй%20AI-ассистента!",
                )],
                [InlineKeyboardButton("  Назад", callback_data="main_menu")],
            ]),
        )

    elif data == "help":
        await query.edit_message_text(
            "<b>Справка</b>\n\n"
            "Просто напишите мне что угодно!\n\n"
            "Команды:\n"
            "/new — новый диалог\n"
            "/stats — статистика\n"
            "/settings — настройки\n"
            "/promo — промокод\n"
            "/ref — реферальная ссылка\n\n"
            "Генерация без команды:\n"
            "сгенерируй мне футуристичный город на фото\n\n"
            "Поддерживаю: текст, фото, голосовые, кружочки, документы, аудио.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("  Назад", callback_data="main_menu")
            ]]),
        )

    elif data == "admin_panel" and is_admin:
        await query.edit_message_text(
            "  <b>Панель администратора</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=make_admin_keyboard(),
        )

    elif data == "admin_stats" and is_admin:
        stats = await get_global_stats()
        msg = (
            f"  <b>Глобальная статистика</b>\n\n"
            f"👤 Всего пользователей: <b>{stats['total_users']}</b>\n"
            f"🆕 Новых за 24ч: <b>{stats['new_today']}</b>\n"
            f"  Сообщений сегодня: <b>{stats['msgs_today']}</b>\n"
        )
        await query.edit_message_text(
            msg,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("  Обновить", callback_data="admin_stats"),
                InlineKeyboardButton("  Назад",   callback_data="admin_panel"),
            ]]),
        )

    elif data == "admin_broadcast" and is_admin:
        context.user_data["state"] = "admin_broadcast"
        await query.edit_message_text(
            "  <b>Рассылка</b>\n\nНапишите сообщение для рассылки всем пользователям:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("  Отмена", callback_data="admin_panel")
            ]]),
        )

    elif data == "admin_create_promo" and is_admin:
        context.user_data["state"] = "admin_create_promo"
        await query.edit_message_text(
            "  <b>Создать промокод</b>\n\n"
            "Формат: <code>КОД КОЛИЧЕСТВО_ДНЕЙ ИСПОЛЬЗОВАНИЙ</code>\n"
            "Пример: <code>TESTPROMO 30 10</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("  Отмена", callback_data="admin_panel")
            ]]),
        )

    elif data == "admin_tariffs" and is_admin:
        context.user_data["state"] = "admin_tariffs"
        await query.edit_message_text(
            "<b>Лимиты</b>\n\n"
            f"Free сообщений/день: <b>{FREE_DAILY_LIMIT}</b>\n"
            f"Free генераций фото/день: <b>{FREE_IMAGE_DAILY_LIMIT}</b>\n"
            f"Free загрузок фото+dok/день: <b>{FREE_MEDIA_DAILY_LIMIT}</b>\n\n"
            "Введите новые значения:\n"
            "<code>FREE_MSG FREE_IMG FREE_MEDIA</code>\n"
            "Пример: <code>30 5 5</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Отмена", callback_data="admin_panel")
            ]]),
        )

    elif data == "admin_ban" and is_admin:
        context.user_data["state"] = "admin_ban"
        await query.edit_message_text(
            "  <b>Забанить пользователя</b>\n\nВведите User ID:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("  Отмена", callback_data="admin_panel")
            ]]),
        )

    elif data == "admin_unban" and is_admin:
        context.user_data["state"] = "admin_unban"
        await query.edit_message_text(
            "  <b>Разбанить пользователя</b>\n\nВведите User ID:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("  Отмена", callback_data="admin_panel")
            ]]),
        )


async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              state: str, text: str):
    user_id = update.effective_user.id
    context.user_data.pop("state", None)

    if state == "admin_broadcast":
        users = await get_all_users()
        sent, failed = 0, 0
        status_msg = await update.message.reply_text(
            f"  Рассылка запущена... (0/{len(users)})",
            parse_mode=ParseMode.HTML,
        )
        for i, uid in enumerate(users, 1):
            try:
                await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML)
                sent += 1
            except Exception:
                failed += 1
            if i % 20 == 0:
                try:
                    await status_msg.edit_text(
                        f"  Рассылка... {i}/{len(users)}",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            await asyncio.sleep(0.05)
        await status_msg.edit_text(
            f"  <b>Рассылка завершена!</b>\n"
            f"Отправлено: {sent}, Ошибок: {failed}",
            parse_mode=ParseMode.HTML,
        )

    elif state == "admin_create_promo":
        parts = text.strip().split()
        if len(parts) < 2:
            await update.message.reply_text(
                "  Формат: КОД ДНИ [ИСПОЛЬЗОВАНИЙ]", parse_mode=ParseMode.HTML
            )
            return
        code = parts[0].upper()
        days = int(parts[1]) if parts[1].isdigit() else 30
        uses = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        await create_promo(code, 100, uses, days, user_id)
        await update.message.reply_text(
            f"  <b>Промокод создан!</b>\n"
            f"Код: <code>{code}</code>\n"
            f"Дней: <b>{days}</b>\n"
            f"Использований: <b>{uses}</b>",
            parse_mode=ParseMode.HTML,
        )

    elif state == "admin_tariffs":
        global FREE_DAILY_LIMIT, FREE_IMAGE_DAILY_LIMIT, FREE_MEDIA_DAILY_LIMIT

        parts = text.strip().split()
        if len(parts) != 3:
            await update.message.reply_text("Формат: FREE_MSG FREE_IMG FREE_MEDIA")
            return
        try:
            free_msg   = int(parts[0])
            free_img   = int(parts[1])
            free_media = int(parts[2])

            if min(free_msg, free_img, free_media) < 0:
                raise ValueError("Значения не могут быть отрицательными")

            FREE_DAILY_LIMIT       = free_msg
            FREE_IMAGE_DAILY_LIMIT = free_img
            FREE_MEDIA_DAILY_LIMIT = free_media

            await set_config_value("free_daily_limit",       str(FREE_DAILY_LIMIT))
            await set_config_value("free_image_daily_limit", str(FREE_IMAGE_DAILY_LIMIT))
            await set_config_value("free_media_daily_limit", str(FREE_MEDIA_DAILY_LIMIT))

            await update.message.reply_text(
                "<b>Лимиты обновлены.</b>\n"
                f"Free сообщений/день: <b>{FREE_DAILY_LIMIT}</b>\n"
                f"Free генераций фото/день: <b>{FREE_IMAGE_DAILY_LIMIT}</b>\n"
                f"Free загрузок фото+dok/день: <b>{FREE_MEDIA_DAILY_LIMIT}</b>",
                parse_mode=ParseMode.HTML,
            )
        except ValueError as e:
            await update.message.reply_text(f"Ошибка: {e}")

    elif state == "admin_ban":
        try:
            target_id = int(text.strip())
            await ban_user(target_id)
            await update.message.reply_text(
                f"  Пользователь <b>{target_id}</b> заблокирован.",
                parse_mode=ParseMode.HTML,
            )
        except ValueError:
            await update.message.reply_text("  Неверный ID")

    elif state == "admin_unban":
        try:
            target_id = int(text.strip())
            await unban_user(target_id)
            await update.message.reply_text(
                f"  Пользователь <b>{target_id}</b> разблокирован.",
                parse_mode=ParseMode.HTML,
            )
        except ValueError:
            await update.message.reply_text("  Неверный ID")


async def post_init(application):
    commands = [
        BotCommand("start",    "Начало работы"),
        BotCommand("new",      "Новый диалог"),
        BotCommand("stats",    "Статистика"),
        BotCommand("settings", "Настройки"),
        BotCommand("promo",    "Промокод"),
        BotCommand("ref",      "Реферальная ссылка"),
        BotCommand("help",     "Помощь"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("  Команды бота установлены")


def main():
    logger.info("  Запуск бота...")

    if TELEGRAM_BOT_TOKEN in ("ВАШ_ТОКЕН_БОТА", ""):
        logger.error("  Укажите TELEGRAM_BOT_TOKEN!")
        sys.exit(1)

    if OPENAI_API_KEY in ("ВАШ_КЛЮЧ_OPENAI", ""):
        logger.error("  Укажите OPENAI_API_KEY!")
        sys.exit(1)

    asyncio.get_event_loop().run_until_complete(init_db())
    asyncio.get_event_loop().run_until_complete(load_runtime_config())

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("new",      cmd_new))
    app.add_handler(CommandHandler("stats",    cmd_stats))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("promo",    cmd_promo))
    app.add_handler(CommandHandler("ref",      cmd_ref))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.VOICE,                   handle_voice))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE,              handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL,            handle_document))
    app.add_handler(MessageHandler(filters.AUDIO,                   handle_audio))
    app.add_handler(MessageHandler(filters.VIDEO,                   handle_audio))

    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("  Бот запущен! Нажмите Ctrl+C для остановки.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
