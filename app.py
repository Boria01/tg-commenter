import os
import re
import time
import sqlite3
import asyncio
import random
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.errors import FloodWaitError, ChatWriteForbiddenError, UserBannedInChannelError

MSK_TZ = timezone(timedelta(hours=3))
DB_PATH = "commenter.db"

# Максимальное время одной смены на GitHub Actions (5 часов 30 минут = 19800 сек)
MAX_SESSION_SECONDS = 19800

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TG_SESSION = os.environ["TG_SESSION"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = os.environ.get("ADMIN_ID", "")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
WORKFLOW_FILENAME = "commenter.yml"

DAILY_COMMENT_LIMIT = 8
DAILY_JOIN_LIMIT = 3
MIN_SUBS = 1000
MAX_SUBS = 5000000
SLEEP_START_HOUR = 1   # Сон с 01:00 МСК
SLEEP_END_HOUR = 8     # Подъём в 08:00 МСК

DEFAULT_SEED_CHANNELS = [
    "lentachold",
    "rian_ru",
    "gazetaru",
    "rbc_news",
    "vedomosti",
    "kommersant",
    "izvestia",
    "lenta_ru",
    "tass_agency",
    "moscowach",
    "pdmnews",
    "ostorozhno_novosti"
]

CATALOG_URLS = [
    "https://tlgrm.ru/channels/news",
    "https://tlgrm.ru/channels/politics"
]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS commented_posts (
            chat_id INTEGER,
            msg_id INTEGER,
            date_str TEXT,
            comment_text TEXT,
            created_at TEXT,
            PRIMARY KEY (chat_id, msg_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS donor_channels (
            username TEXT PRIMARY KEY,
            chat_id INTEGER,
            title TEXT,
            status TEXT,
            joined_date TEXT
        )
    """)
    conn.commit()
    return conn


def add_candidate(conn, username):
    clean = username.lstrip("@").strip().lower()
    if not clean or len(clean) < 4 or clean.endswith("bot"):
        return False
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM donor_channels WHERE username = ?", (clean,))
    if not cur.fetchone():
        cur.execute("INSERT INTO donor_channels (username, status) VALUES (?, 'candidate')", (clean,))
        conn.commit()
        return True
    return False


def get_today_comments(conn, date_str):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM commented_posts WHERE date_str = ?", (date_str,))
    row = cur.fetchone()
    return row[0] if row else 0


def get_today_joins(conn, date_str):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM donor_channels WHERE status = 'active' AND joined_date = ?", (date_str,))
    row = cur.fetchone()
    return row[0] if row else 0


def get_active_channels(conn):
    cur = conn.cursor()
    cur.execute("SELECT username FROM donor_channels WHERE status = 'active'")
    return [r[0] for r in cur.fetchall()]


def notify_admin(text):
    if not BOT_TOKEN or not ADMIN_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": ADMIN_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=15)
    except Exception as e:
        print(f"[Notify Error]: {e}")


def trigger_next_github_run():
    """Запускает следующую смену в GitHub Actions перед завершением текущей."""
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/workflows/{WORKFLOW_FILENAME}/dispatches"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    try:
        r = requests.post(url, headers=headers, json={"ref": "main"}, timeout=20)
        print(f"[Relay] Запуск следующей смены GitHub Actions: статус {r.status_code}")
    except Exception as e:
        print(f"[Relay Error]: {e}")


def fetch_catalog_usernames():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9"
    }
    found = set()
    for url in CATALOG_URLS:
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                m = re.search(r"/channels/@([a-zA-Z0-9_]{4,32})", a["href"])
                if m:
                    found.add(m.group(1).lower())
        except Exception as e:
            print(f"[Catalog Parse Error] {url}: {e}")
    return list(found)


def generate_comment(post_text):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    prompt = (
        "Ты обычный читатель новостного Telegram-канала. Прочитай новость и напиши один короткий, "
        "живой, естественный комментарий по сути (1-2 предложения, максимум 140 символов).\n"
        "Правила:\n"
        "- Без ссылок, хештегов, упоминаний каналов и призывов подписаться.\n"
        "- Без канцелярита и штампов вроде «очень интересная новость» или «согласен с автором».\n"
        "- Пиши как живой человек в обсуждении: лёгкая ирония, точный вопрос по теме или здравый вывод.\n\n"
        f"Текст новости:\n{post_text[:1200]}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.85, "maxOutputTokens": 120}
    }
    try:
        r = requests.post(url, json=payload, timeout=25).json()
        if "candidates" not in r:
            return None
        reply = r["candidates"][0]["content"]["parts"][0]["text"].strip().strip('"\'«»')
        if "http" in reply.lower() or "@" in reply or "t.me" in reply.lower():
            return None
        return reply
    except Exception as e:
        print(f"[Gemini Error]: {e}")
        return None


async def process_and_comment(client, conn, entity, msg):
    post_text = (msg.message or "").strip()
    if len(post_text) < 25:
        return False

    now_msk = datetime.now(MSK_TZ)
    if SLEEP_START_HOUR <= now_msk.hour < SLEEP_END_HOUR:
        return False

    today_str = now_msk.strftime("%Y-%m-%d")
    if get_today_comments(conn, today_str) >= DAILY_COMMENT_LIMIT:
        return False

    chat_id = entity.id
    msg_id = msg.id
    chat_user = (getattr(entity, "username", None) or "").lower()

    cur = conn.cursor()
    cur.execute("SELECT 1 FROM commented_posts WHERE chat_id = ? AND msg_id = ?", (chat_id, msg_id))
    if cur.fetchone():
        return False

    comment_text = generate_comment(post_text)
    if not comment_text:
        return False

    try:
        await client.send_message(entity=entity, message=comment_text, comment_to=msg_id)
        cur.execute(
            "INSERT INTO commented_posts (chat_id, msg_id, date_str, comment_text, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, msg_id, today_str, comment_text, now_msk.isoformat())
        )
        conn.commit()
        chat_title = getattr(entity, "title", chat_user)
        link = f"https://t.me/{chat_user}/{msg_id}" if chat_user else ""
        print(f"[Sent] {chat_title}: {comment_text}")
        notify_admin(
            f"💬 <b>Комментарий ({get_today_comments(conn, today_str)}/{DAILY_COMMENT_LIMIT})</b>\n"
            f"📢 <b>Канал:</b> {chat_title}\n"
            f"✍️ <i>{comment_text}</i>\n"
            f"{link}"
        )
        return True
    except FloodWaitError as e:
        print(f"[FloodWait] Ждём {e.seconds} сек.")
        await asyncio.sleep(e.seconds + 5)
    except (ChatWriteForbiddenError, UserBannedInChannelError):
        if chat_user:
            cur.execute("UPDATE donor_channels SET status = 'rejected' WHERE username = ?", (chat_user,))
            conn.commit()
    except Exception as e:
        print(f"[Send Error] @{chat_user}/{msg_id}: {e}")
    return False


async def scout_loop(client, conn):
    await asyncio.sleep(5)
    first_test_done = get_today_comments(conn, datetime.now(MSK_TZ).strftime("%Y-%m-%d")) > 0

    while True:
        try:
            now_msk = datetime.now(MSK_TZ)
            today_str = now_msk.strftime("%Y-%m-%d")

            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM donor_channels WHERE status = 'candidate'")
            if cur.fetchone()[0] < 10:
                usernames = await asyncio.to_thread(fetch_catalog_usernames)
                for u in usernames:
                    add_candidate(conn, u)

            if get_today_joins(conn, today_str) < DAILY_JOIN_LIMIT:
                cur.execute("SELECT username FROM donor_channels WHERE status = 'candidate' ORDER BY RANDOM() LIMIT 1")
                row = cur.fetchone()
                if row:
                    uname = row[0]
                    try:
                        entity = await client.get_entity(uname)
                        full = await client(GetFullChannelRequest(channel=entity))
                        subs = full.full_chat.participants_count or 0
                        linked_chat = full.full_chat.linked_chat_id

                        if linked_chat and MIN_SUBS <= subs <= MAX_SUBS:
                            await client(JoinChannelRequest(entity))
                            title = getattr(entity, "title", uname)
                            cur.execute(
                                "UPDATE donor_channels SET chat_id = ?, title = ?, status = 'active', joined_date = ? WHERE username = ?",
                                (entity.id, title, today_str, uname)
                            )
                            conn.commit()
                            print(f"[Scout] Подписались на @{uname} ({title}, {subs} подп.)")
                            notify_admin(
                                f"🔎 <b>Добавлен новый канал-донор</b>\n"
                                f"📢 {title} (@{uname})\n"
                                f"👥 Подписчиков: {subs}"
                            )
                            if not first_test_done:
                                await asyncio.sleep(4)
                                async for last_msg in client.iter_messages(entity, limit=5):
                                    if last_msg.message and len(last_msg.message.strip()) >= 25:
                                        if await process_and_comment(client, conn, entity, last_msg):
                                            first_test_done = True
                                        break
                        else:
                            cur.execute("UPDATE donor_channels SET status = 'rejected' WHERE username = ?", (uname,))
                            conn.commit()
                            print(f"[Scout] Пропущен @{uname} (комменты: {bool(linked_chat)}, подп.: {subs})")
                    except FloodWaitError as e:
                        await asyncio.sleep(e.seconds + 30)
                    except Exception as e:
                        cur.execute("UPDATE donor_channels SET status = 'rejected' WHERE username = ?", (uname,))
                        conn.commit()
                        print(f"[Scout Error] @{uname}: {e}")
        except Exception as e:
            print(f"[Scout Loop Error]: {e}")

        now_msk = datetime.now(MSK_TZ)
        if get_today_joins(conn, now_msk.strftime("%Y-%m-%d")) < DAILY_JOIN_LIMIT:
            await asyncio.sleep(random.randint(4, 8))
        else:
            await asyncio.sleep(random.randint(1800, 2400))


async def run_bot():
    start_ts = time.time()
    conn = init_db()
    for seed in DEFAULT_SEED_CHANNELS:
        add_candidate(conn, seed)

    client = TelegramClient(
        StringSession(TG_SESSION),
        API_ID,
        API_HASH,
        device_model="Samsung Galaxy S24",
        system_version="Android 14",
        app_version="11.4.2",
        lang_code="ru",
        system_lang_code="ru-RU"
    )

    await client.start()
    me = await client.get_me()
    active_list = get_active_channels(conn)
    print(f"[OK] GitHub-смена запущена: {me.first_name} | Активных каналов: {len(active_list)}")
    notify_admin(
        f"🟢 <b>Первонах запущен в GitHub Actions</b>\n"
        f"👤 Аккаунт: {me.first_name}\n"
        f"📡 Активных доноров: {len(active_list)}"
    )

    @client.on(events.NewMessage())
    async def on_new_post(event):
        if not event.is_channel or event.is_group:
            return
        chat_user = (getattr(event.chat, "username", None) or "").lower()
        if not chat_user:
            return
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM donor_channels WHERE username = ? AND status = 'active'", (chat_user,))
        if not cur.fetchone():
            return

        post_text = (event.message.message or "").strip()
        for m in re.findall(r"(?:t\.me/|@)([a-zA-Z0-9_]{5,32})", post_text):
            if m.lower() != chat_user:
                add_candidate(conn, m)

        await asyncio.sleep(random.randint(5, 11))
        await process_and_comment(client, conn, event.chat, event.message)

    asyncio.create_task(scout_loop(client, conn))

    # Контроллер смены: через 5.5 часов или в 01:00 МСК корректно завершаем смену
    while True:
        await asyncio.sleep(30)
        now_msk = datetime.now(MSK_TZ)
        elapsed = time.time() - start_ts

        # Если наступила ночь (01:00 - 07:59 МСК), выходим без немедленного перезапуска — утром поднимет cron
        if SLEEP_START_HOUR <= now_msk.hour < SLEEP_END_HOUR:
            print("[Shift] Наступила ночь (01:00 МСК), сохраняем базу и уходим спать до 08:00.")
            await client.disconnect()
            return

        # Если смена отработала 5.5 часов днём — запускаем эстафету и выходим
        if elapsed >= MAX_SESSION_SECONDS:
            print("[Shift] 5.5 часов истекли, передаём эстафету следующей смене...")
            await client.disconnect()
            trigger_next_github_run()
            return


if __name__ == "__main__":
    asyncio.run(run_bot())
