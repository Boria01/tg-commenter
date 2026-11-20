import os
import time
import asyncio
import random
import requests
from datetime import datetime, timezone, timedelta
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.account import UpdateNotifySettingsRequest
from telethon.tl.types import Channel, InputPeerNotifySettings
from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    PeerFloodError,
    ChannelPrivateError
)

MSK_TZ = timezone(timedelta(hours=3))

TARGET_PROMO_CHANNEL = "@glavnoe_chasa"
DAILY_AD_LIMIT = 15
DAILY_AD_JOIN_LIMIT = 8
MIN_AD_CHAT_MEMBERS = 300
AD_REPEAT_COOLDOWN_HOURS = 24

SLEEP_START_HOUR = 1
SLEEP_END_HOUR = 8

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

DISCOVERED_MODELS = []

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
    "реклама запрещена",
    "украина",
    "україна",
    "киев",
    "харьков",
    "одесса",
    "днепр"
]


async def mute_peer(client, peer):
    """Переводит канал или чат в бессрочный беззвучный режим."""
    try:
        input_peer = await client.get_input_entity(peer)
        await client(UpdateNotifySettingsRequest(
            peer=input_peer,
            settings=InputPeerNotifySettings(
                mute_until=2147483647,
                silent=True
            )
        ))
    except Exception as e:
        print(f"[Mute Error]: {e}")


def discover_working_models():
    """Запрашивает у Google AI Studio реальный список доступных текстовых моделей для текущего API-ключа."""
    global DISCOVERED_MODELS
    if DISCOVERED_MODELS:
        return DISCOVERED_MODELS

    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    found = []
    try:
        r = requests.get(url, timeout=20).json()
        for m in r.get("models", []):
            name = m.get("name", "").replace("models/", "")
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" in methods and ("gemini" in name or "gemma" in name):
                if not any(bad in name for bad in ["embedding", "imagen", "image", "veo", "aqa", "vision", "audio", "tts", "robotics", "computer-use"]):
                    found.append(name)
    except Exception as e:
        print(f"[Models Discovery Error]: {e}")

    found.sort(key=lambda x: (
        0 if "flash-lite" in x else (1 if "flash" in x else (2 if "gemma" in x else 3)),
        x
    ))
    if not found:
        found = ["gemini-3.5-flash-lite", "gemini-3-flash-preview", "gemma-4-31b-it"]

    DISCOVERED_MODELS = found
    print(f"[Models] Доступные модели на ключе ({len(found)}): {found}")
    return found


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
    models = discover_working_models()
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

    for model_name in list(models):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
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
                    print(f"[Gemini Ad ({model_name}) OK]")
                    return ad_text
            else:
                err_code = r.get("error", {}).get("code", "")
                print(f"[Gemini Ad {model_name} Skip]: {err_code}")
                if err_code == 404 and model_name in DISCOVERED_MODELS:
                    DISCOVERED_MODELS.remove(model_name)
        except Exception as e:
            print(f"[Gemini Ad {model_name} Error]: {e}")

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
            if cur.fetchone()[0] < 12:
                query = random.choice(AD_SEARCH_QUERIES)
                try:
                    res = await client(SearchRequest(q=query, limit=30))
                    added = 0
                    for chat in res.chats:
                        if isinstance(chat, Channel) and getattr(chat, "megagroup", False) and not getattr(chat, "forum", False):
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
                        if not isinstance(entity, Channel) or not getattr(entity, "megagroup", False) or getattr(entity, "forum", False):
                            cur.execute("UPDATE ad_chats SET status = 'rejected' WHERE username = ?", (uname,))
                            conn.commit()
                        else:
                            banned = getattr(entity, "default_banned_rights", None)
                            can_send = not (banned and getattr(banned, "send_messages", False))
                            full = await client(GetFullChannelRequest(channel=entity))
                            members = full.full_chat.participants_count or 0
                            title_lower = (getattr(entity, "title", "") or "").lower()
                            about_text = (full.full_chat.about or "").lower()
                            combined_text = f"{title_lower} {about_text}"

                            if can_send and members >= MIN_AD_CHAT_MEMBERS and not any(sw in combined_text for sw in AD_STOP_WORDS):
                                await client(JoinChannelRequest(entity))
                                await mute_peer(client, entity)
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
                                    print(f"[AdScout] Вступили (без звука) в пиар-чат @{uname} ({title})")
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
                        await asyncio.sleep(random.randint(1500, 2700))
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
                        cur.execute("UPDATE ad_chats SET status = 'rejected' WHERE username = ?", (t_uname,))
                        conn.commit()

        except Exception as e:
            print(f"[AdBoards Loop Error]: {e}")

        await asyncio.sleep(random.randint(15, 25))
                        
