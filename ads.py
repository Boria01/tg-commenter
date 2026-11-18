import os
import time
import asyncio
import random
import requests
from datetime import datetime, timezone, timedelta
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel
from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    PeerFloodError,
    ChannelPrivateError
)

MSK_TZ = timezone(timedelta(hours=3))

TARGET_PROMO_CHANNEL = "@glavnoe_chasa"
DAILY_AD_LIMIT = 4
DAILY_AD_JOIN_LIMIT = 3
MIN_AD_CHAT_MEMBERS = 300
AD_REPEAT_COOLDOWN_HOURS = 96

SLEEP_START_HOUR = 1
SLEEP_END_HOUR = 8

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

AD_SEARCH_QUERIES = [
    "пиар чат",
    "бесплатный пиар",
    "реклама бесплатно",
    "бесплатная реклама",
    "доска объявлений",
    "взаимный пиар",
    "пиар каналов",
    "чат пиара",
    "реклама каналов",
    "свободная реклама",
    "объявления бесплатно",
    "продвижение канала"
]

AD_STOP_WORDS = [
    "реклама платно",
    "платная реклама",
    "купить рекламу",
    "прайс",
    "по вопросам рекламы",
    "заказать рекламу",
    "обязательная подписка",
    "подпишись на канал",
    "чтобы писать в чат",
    "ссылки запрещены",
    "без ссылок",
    "реклама запрещена"
]


def init_ads_tables(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ad_chats (
            username TEXT PRIMARY KEY,
            chat_id INTEGER,
            title TEXT,
            status TEXT,
            joined_date TEXT,
            last_posted_ts INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ad_posts_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            date_str TEXT,
            ad_text TEXT,
            created_at TEXT
        )
    """)
    conn.commit()


def add_ad_candidate(conn, username):
    clean = username.lstrip("@").strip().lower()
    if not clean or len(clean) < 4 or clean.endswith("bot"):
        return False
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM ad_chats WHERE username = ?", (clean,))
    if not cur.fetchone():
        cur.execute("INSERT INTO ad_chats (username, status, last_posted_ts) VALUES (?, 'candidate', 0)", (clean,))
        conn.commit()
        return True
    return False


def get_today_ads(conn, date_str):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM ad_posts_log WHERE date_str = ?", (date_str,))
    row = cur.fetchone()
    return row[0] if row else 0


def get_today_ad_joins(conn, date_str):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM ad_chats WHERE status = 'active' AND joined_date = ?", (date_str,))
    row = cur.fetchone()
    return row[0] if row else 0


def get_active_ad_chats(conn):
    cur = conn.cursor()
    cur.execute("SELECT username FROM ad_chats WHERE status = 'active'")
    return [r[0] for r in cur.fetchall()]


def generate_unique_ad_post():
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
    styles = [
        "деловой и лаконичный анонс для занятых людей",
        "живая дружеская рекомендация для тех, кто устал от длинных лент",
        "короткий пост с акцентом на экономию времени и главные события дня",
        "непринуждённый пост в стиле «где читать новости без воды и фейков»"
    ]
    prompt = (
        f"Напиши короткий рекламный пост для Telegram-чата объявлений и пиара.\n"
        f"Стиль: {random.choice(styles)}.\n"
        f"Канал: «Главное за час» ({TARGET_PROMO_CHANNEL}) — краткие выжимки главных событий России и мира каждый час, без воды и спама.\n"
        f"Обязательно вставь ссылку {TARGET_PROMO_CHANNEL}. Длина до 230 символов, 1–2 эмодзи, без капса. Верни только готовый текст."
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.95,
            "maxOutputTokens": 300
        }
    }
    try:
        r = requests.post(url, json=payload, timeout=25).json()
        if "candidates" in r:
            ad_text = r["candidates"][0]["content"]["parts"][0]["text"].strip().strip('"\'«»')
            if TARGET_PROMO_CHANNEL.lower() in ad_text.lower() and 30 <= len(ad_text) <= 450:
                return ad_text
        else:
            print(f"[Gemini Ad Fail]: {r}")
    except Exception as e:
        print(f"[Gemini Ad Error]: {e}")

    fallbacks = [
        f"📰 Устали листать десятки каналов ради пары новостей? В {TARGET_PROMO_CHANNEL} собираем только главное за каждый час — коротко и по делу.",
        f"⚡️ Главные события дня в формате короткой выжимки без воды. Заглядывайте в {TARGET_PROMO_CHANNEL}, чтобы всегда быть в курсе.",
        f"📌 Все важные новости за час в одном посте — удобно читать в перерыве. Подписывайтесь на {TARGET_PROMO_CHANNEL}."
    ]
    return random.choice(fallbacks)


async def ad_boards_loop(client, conn, notify_fn):
    await asyncio.sleep(20)

    while True:
        try:
            now_msk = datetime.now(MSK_TZ)
            if SLEEP_START_HOUR <= now_msk.hour < SLEEP_END_HOUR:
                await asyncio.sleep(300)
                continue

            today_str = now_msk.strftime("%Y-%m-%d")
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM ad_chats WHERE status = 'candidate'")
            if cur.fetchone()[0] < 8:
                query = random.choice(AD_SEARCH_QUERIES)
                try:
                    res = await client(SearchRequest(q=query, limit=30))
                    added = 0
                    for chat in res.chats:
                        if isinstance(chat, Channel) and getattr(chat, "megagroup", False):
                            uname = getattr(chat, "username", None)
                            if uname and add_ad_candidate(conn, uname):
                                added += 1
                    print(f"[AdScout] Поиск '{query}': найдено новых чатов {added}")
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 30)
                except Exception as e:
                    print(f"[AdScout Search Error]: {e}")

            if get_today_ad_joins(conn, today_str) < DAILY_AD_JOIN_LIMIT:
                cur.execute("SELECT username FROM ad_chats WHERE status = 'candidate' ORDER BY RANDOM() LIMIT 1")
                row = cur.fetchone()
                if row:
                    uname = row[0]
                    try:
                        entity = await client.get_entity(uname)
                        if not isinstance(entity, Channel) or not getattr(entity, "megagroup", False):
                            cur.execute("UPDATE ad_chats SET status = 'rejected' WHERE username = ?", (uname,))
                            conn.commit()
                        else:
                            banned = getattr(entity, "default_banned_rights", None)
                            can_send = not (banned and getattr(banned, "send_messages", False))
                            full = await client(GetFullChannelRequest(channel=entity))
                            members = full.full_chat.participants_count or 0
                            about_text = (full.full_chat.about or "").lower()

                            if can_send and members >= MIN_AD_CHAT_MEMBERS and not any(sw in about_text for sw in AD_STOP_WORDS):
                                await client(JoinChannelRequest(entity))
                                await asyncio.sleep(5)

                                gatekeeper = False
                                async for recent in client.iter_messages(entity, limit=4):
                                    txt = (recent.message or "").lower()
                                    if any(w in txt for w in ["подпишитесь на", "обязательная подписка", "чтобы писать", "подпишись на"]):
                                        gatekeeper = True
                                        break

                                if gatekeeper:
                                    await client(LeaveChannelRequest(entity))
                                    cur.execute("UPDATE ad_chats SET status = 'rejected' WHERE username = ?", (uname,))
                                    conn.commit()
                                else:
                                    title = getattr(entity, "title", uname)
                                    cur.execute(
                                        "UPDATE ad_chats SET chat_id = ?, title = ?, status = 'active', joined_date = ? WHERE username = ?",
                                        (entity.id, title, today_str, uname)
                                    )
                                    conn.commit()
                                    print(f"[AdScout] Вступили в пиар-чат @{uname} ({title})")
                                    notify_fn(
                                        f"📣 <b>Найдена бесплатная рекламная площадка</b>\n"
                                        f"💬 {title} (@{uname})\n"
                                        f"👥 Участников: {members}"
                                    )
                            else:
                                cur.execute("UPDATE ad_chats SET status = 'rejected' WHERE username = ?", (uname,))
                                conn.commit()
                    except FloodWaitError as e:
                        await asyncio.sleep(e.seconds + 30)
                    except Exception:
                        cur.execute("UPDATE ad_chats SET status = 'rejected' WHERE username = ?", (uname,))
                        conn.commit()

            if get_today_ads(conn, today_str) < DAILY_AD_LIMIT:
                cutoff = int(time.time()) - (AD_REPEAT_COOLDOWN_HOURS * 3600)
                cur.execute(
                    "SELECT username, title FROM ad_chats WHERE status = 'active' AND last_posted_ts <= ? ORDER BY last_posted_ts ASC, RANDOM() LIMIT 1",
                    (cutoff,)
                )
                target = cur.fetchone()
                if target:
                    t_uname, t_title = target
                    ad_text = await asyncio.to_thread(generate_unique_ad_post)
                    try:
                        sent_msg = await client.send_message(entity=t_uname, message=ad_text, link_preview=False)
                        cur.execute("UPDATE ad_chats SET last_posted_ts = ? WHERE username = ?", (int(time.time()), t_uname))
                        cur.execute(
                            "INSERT INTO ad_posts_log (username, date_str, ad_text, created_at) VALUES (?, ?, ?, ?)",
                            (t_uname, today_str, ad_text, now_msk.isoformat())
                        )
                        conn.commit()
                        print(f"[AdPost] Реклама отправлена в @{t_uname}")
                        notify_fn(
                            f"🚀 <b>Реклама опубликована ({get_today_ads(conn, today_str)}/{DAILY_AD_LIMIT})</b>\n"
                            f"📍 <b>Площадка:</b> {t_title} (@{t_uname})\n"
                            f"📝 <i>{ad_text}</i>\n"
                            f"https://t.me/{t_uname}/{sent_msg.id}"
                        )
                        await asyncio.sleep(random.randint(7200, 10800))
                        continue
                    except (ChatWriteForbiddenError, UserBannedInChannelError, ChannelPrivateError):
                        cur.execute("UPDATE ad_chats SET status = 'rejected' WHERE username = ?", (t_uname,))
                        conn.commit()
                    except PeerFloodError:
                        notify_fn("⚠️ <b>Рекламный модуль:</b> Telegram ограничил частоту (PeerFlood). Пауза 6 часов.")
                        await asyncio.sleep(21600)
                        continue
                    except FloodWaitError as e:
                        await asyncio.sleep(e.seconds + 30)
                    except Exception as e:
                        print(f"[AdPost Error] @{t_uname}: {e}")

        except Exception as e:
            print(f"[AdBoards Loop Error]: {e}")

        await asyncio.sleep(random.randint(15, 25))
        
