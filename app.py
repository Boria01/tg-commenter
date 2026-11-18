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
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest, LeaveChannelRequest
from telethon.errors import FloodWaitError, ChatWriteForbiddenError, UserBannedInChannelError

from ads import init_ads_tables, get_active_ad_chats, ad_boards_loop

MSK_TZ = timezone(timedelta(hours=3))
DB_PATH = "commenter.db"
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
SLEEP_START_HOUR = 1
SLEEP_END_HOUR = 8

BLACKLIST_CHANNELS = {
    "fighter_bomber", "bomber_fighter", "rybar", "wargonzo", "grey_zone",
    "milinfolive", "voenacher", "dva_majors", "vysokygovorit", "sashakots"
}

DEFAULT_SEED_CHANNELS = [
    "lentachold", "rian_ru", "gazetaru", "rbc_news",
    "vedomosti", "kommersant", "izvestia", "lenta_ru",
    "tass_agency", "moscowach", "pdmnews", "ostorozhno_novosti"
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
    init_ads_tables(conn)
    return conn


def add_candidate(conn, username):
    clean = username.lstrip("@").strip().lower()
    if not clean or len(clean) < 4 or clean.endswith("bot") or clean in BLACKLIST_CHANNELS:
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
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15
        )
    except Exception as e:
        print(f"[Notify Error]: {e}")


def trigger_next_github_run():
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/workflows/{WORKFLOW_FILENAME}/dispatches"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.post(url, headers=headers, json={"ref": "main"}, timeout=20)
        print(f"[Relay] Запуск следующей смены: статус {r.status_code}")
    except Exception as e:
        print(f"[Relay Error]: {e}")


def fetch_catalog_usernames():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"}
    found = set()
    for url in CATALOG_URLS:
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    m = re.search(r"/channels/@([a-zA-Z0-9_]{4,32})", a["href"])
                    if m:
                        found.add(m.group(1).lower())
        except Exception as e:
            print(f"[Catalog Error] {url}: {e}")
    return list(found)


def generate_comment(post_text):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
    prompt = (
        "Ты обычный читатель новостного Telegram-канала. Прочитай новость и напиши один короткий, "
        "живой комментарий по сути (1-2 предложения, до 140 символов) без ссылок, хештегов и упоминаний.\n\n"
        f"Новость:\n{post_text[:1200]}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.85,
            "maxOutputTokens": 250
        }
    }
    try:
        r = requests.post(url, json=payload, timeout=25).json()
        if "candidates" not in r:
            print(f"[Gemini Fail]: {r}")
            return None
        reply = r["candidates"][0]["content"]["parts"][0]["text"].strip().strip('"\'«»')
        print(f"[Gemini Reply]: {reply}")
        if not reply or "http" in reply.lower() or "@" in reply or "t.me" in reply.lower():
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

    chat_id, msg_id = entity.id, msg.id
    chat_user = (getattr(entity, "username", None) or "").lower()
    if chat_user in BLACKLIST_CHANNELS:
        return False

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
        print(f"[Sent Comment] {chat_title}: {comment_text}")
        notify_admin(
            f"💬 <b>Комментарий ({get_today_comments(conn, today_str)}/{DAILY_COMMENT_LIMIT})</b>\n"
            f"📢 <b>Канал:</b> {chat_title}\n"
            f"✍️ <i>{comment_text}</i>\n{link}"
        )
        return True
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds + 5)
    except (ChatWriteForbiddenError, UserBannedInChannelError):
        if chat_user:
            cur.execute("UPDATE donor_channels SET status = 'rejected' WHERE username = ?", (chat_user,))
            conn.commit()
    except Exception as e:
        print(f"[Send Error] @{chat_user}/{msg_id}: {e}")
    return False


async def morning_catchup_loop(client, conn):
    """Сразу после старта или после ночного сна отрабатывает свежие посты в активных каналах."""
    await asyncio.sleep(10)
    last_catchup_date = ""

    while True:
        try:
            now_msk = datetime.now(MSK_TZ)
            today_str = now_msk.strftime("%Y-%m-%d")

            if not (SLEEP_START_HOUR <= now_msk.hour < SLEEP_END_HOUR) and last_catchup_date != today_str:
                if get_today_comments(conn, today_str) < DAILY_COMMENT_LIMIT:
                    active_channels = get_active_channels(conn)
                    random.shuffle(active_channels)
                    print(f"[Catchup] Отрабатываем свежие посты по {len(active_channels)} активным каналам...")
                    for uname in active_channels:
                        if get_today_comments(conn, today_str) >= DAILY_COMMENT_LIMIT:
                            break
                        try:
                            entity = await client.get_entity(uname)
                            async for msg in client.iter_messages(entity, limit=3):
                                if msg.message and len(msg.message.strip()) >= 25:
                                    if await process_and_comment(client, conn, entity, msg):
                                        await asyncio.sleep(random.randint(15, 25))
                                        break
                        except Exception as e:
                            print(f"[Catchup Error] @{uname}: {e}")
                last_catchup_date = today_str
        except Exception as e:
            print(f"[Catchup Loop Error]: {e}")

        await asyncio.sleep(300)


async def scout_loop(client, conn):
    await asyncio.sleep(5)

    while True:
        try:
            now_msk = datetime.now(MSK_TZ)
            today_str = now_msk.strftime("%Y-%m-%d")
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM donor_channels WHERE status = 'candidate'")
            if cur.fetchone()[0] < 10:
                for u in await asyncio.to_thread(fetch_catalog_usernames):
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

                        if linked_chat and MIN_SUBS <= subs <= MAX_SUBS and uname not in BLACKLIST_CHANNELS:
                            await client(JoinChannelRequest(entity))
                            title = getattr(entity, "title", uname)
                            cur.execute(
                                "UPDATE donor_channels SET chat_id = ?, title = ?, status = 'active', joined_date = ? WHERE username = ?",
                                (entity.id, title, today_str, uname)
                            )
                            conn.commit()
                            print(f"[Scout] Подписались на @{uname} ({title})")
                            notify_admin(f"🔎 <b>Добавлен новый канал-донор</b>\n📢 {title} (@{uname})\n👥 Подписчиков: {subs}")

                            if get_today_comments(conn, today_str) < DAILY_COMMENT_LIMIT:
                                await asyncio.sleep(4)
                                async for last_msg in client.iter_messages(entity, limit=5):
                                    if last_msg.message and len(last_msg.message.strip()) >= 25:
                                        if await process_and_comment(client, conn, entity, last_msg):
                                            break
                        else:
                            cur.execute("UPDATE donor_channels SET status = 'rejected' WHERE username = ?", (uname,))
                            conn.commit()
                    except FloodWaitError as e:
                        await asyncio.sleep(e.seconds + 30)
                    except Exception:
                        cur.execute("UPDATE donor_channels SET status = 'rejected' WHERE username = ?", (uname,))
                        conn.commit()
        except Exception as e:
            print(f"[Scout Loop Error]: {e}")

        now_msk = datetime.now(MSK_TZ)
        if get_today_joins(conn, now_msk.strftime("%Y-%m-%d")) < DAILY_JOIN_LIMIT:
            await asyncio.sleep(random.randint(4, 8))
        else:
            await asyncio.sleep(random.randint(1800, 2400))


async def run_bot():
    print("[Init] Инициализация базы данных...")
    start_ts = time.time()
    conn = init_db()
    for seed in DEFAULT_SEED_CHANNELS:
        add_candidate(conn, seed)

    client = TelegramClient(
        StringSession(TG_SESSION), API_ID, API_HASH,
        device_model="Samsung Galaxy S24", system_version="Android 14",
        app_version="11.4.2", lang_code="ru", system_lang_code="ru-RU"
    )

    print("[Init] Подключаемся к Telegram...")
    await asyncio.wait_for(client.connect(), timeout=25)
    if not await client.is_user_authorized():
        print("[ERROR] Сессия TG_SESSION не авторизована!")
        notify_admin("❌ <b>Ошибка:</b> Сессия TG_SESSION не авторизована!")
        return

    cur = conn.cursor()
    for bad in BLACKLIST_CHANNELS:
        cur.execute("SELECT status FROM donor_channels WHERE username = ?", (bad,))
        row = cur.fetchone()
        if row and row[0] == "active":
            try:
                ent = await client.get_entity(bad)
                await client(LeaveChannelRequest(ent))
                print(f"[Blacklist] Отписались от @{bad}")
            except Exception:
                pass
        cur.execute("UPDATE donor_channels SET status = 'rejected' WHERE username = ?", (bad,))
    conn.commit()

    me = await client.get_me()
    active_list = get_active_channels(conn)
    active_ads = get_active_ad_chats(conn)
    print(f"[OK] Смена запущена: {me.first_name} | Каналов: {len(active_list)} | Пиар-чатов: {len(active_ads)}")
    notify_admin(
        f"🟢 <b>Смена запущена в GitHub Actions</b>\n"
        f"👤 Аккаунт: {me.first_name}\n"
        f"📡 Каналов для комментов: {len(active_list)}\n"
        f"📣 Бесплатных пиар-чатов: {len(active_ads)}"
    )

    @client.on(events.NewMessage())
    async def on_new_post(event):
        if not event.is_channel or event.is_group:
            return
        chat_user = (getattr(event.chat, "username", None) or "").lower()
        if not chat_user or chat_user in BLACKLIST_CHANNELS:
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

    asyncio.create_task(morning_catchup_loop(client, conn))
    asyncio.create_task(scout_loop(client, conn))
    asyncio.create_task(ad_boards_loop(client, conn, notify_admin))

    while True:
        await asyncio.sleep(30)
        if time.time() - start_ts >= MAX_SESSION_SECONDS:
            print("[Shift] 5.5 часов истекли, передаём эстафету...")
            await client.disconnect()
            trigger_next_github_run()
            return


if __name__ == "__main__":
    asyncio.run(run_bot())
                
