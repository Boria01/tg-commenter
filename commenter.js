import { TelegramClient } from "telegram";
import { StringSession } from "telegram/sessions/index.js";
import fs from "fs";

// ==========================================
// 1. НАСТРОЙКИ И СЕКРЕТЫ (из GitHub Secrets)
// ==========================================
const API_ID = Number(process.env.TG_API_ID);
const API_HASH = String(process.env.TG_API_HASH || "").trim();
const SESSION_STRING = String(process.env.TG_SESSION || "").trim();
const GEMINI_API_KEY = String(process.env.GEMINI_API_KEY || "").trim();
const DEFAULT_MODEL = "gemini-3.5-flash-lite";

// Список юзернеймов российских каналов БЕЗ @ (аккаунт должен быть на них подписан)
const TARGET_CHANNELS = [
  "rian_ru",
  "rbc_news"
];

// ==========================================
// 2. ЛИМИТЫ И ФИЛЬТРЫ БЕЗОПАСНОСТИ
// ==========================================
const MIN_POST_LENGTH = 180;          // не комментировать короткие посты
const MAX_POST_AGE_HOURS = 3;         // не комментировать посты старше 3 часов
const MAX_DAILY_ACTIONS = 10;         // максимум действий (комменты + ответы) за сутки
const MAX_COMMENTS_PER_CHANNEL = 1;   // не больше 1 комментария в одном канале в сутки
const SLEEP_START_HOUR = 23;          // ночной сон с 23:00 МСК
const SLEEP_END_HOUR = 8;             // до 08:00 МСК
const MAX_REPLIES_PER_RUN = 2;        // максимум ответов на реплаи за один запуск
const STATE_FILE = "./bot_state.json";

const SYSTEM_PROMPT = [
  "Ты — обычный живой читатель российских новостных Telegram-каналов.",
  "Напиши ОЧЕНЬ короткий комментарий по теме поста — одно предложение, от 5 до 15 слов (строго до 110 символов).",
  "Передавай искреннюю поддержку России, гордость за наших людей, развитие и спокойную уверенность — только по сути конкретной новости.",
  "Пиши как реальный человек в комментариях с телефона: просто, тепло, без пафосных штампов, без канцелярита, без агрессии, без хэштегов и восклицательных знаков в каждом слове.",
  "Если тебе ответили на твой комментарий — ответь так же коротко (1 предложение, до 100 символов), доброжелательно и по делу."
].join(" ");

// ==========================================
// 3. РАБОТА С СОСТОЯНИЕМ (bot_state.json)
// ==========================================
function getMskDateInfo() {
  const mskString = new Date().toLocaleString("en-US", { timeZone: "Europe/Moscow" });
  const mskDate = new Date(mskString);
  const dateKey = `${mskDate.getFullYear()}-${mskDate.getMonth() + 1}-${mskDate.getDate()}`;
  return { hour: mskDate.getHours(), dateKey };
}

function loadState() {
  try {
    if (fs.existsSync(STATE_FILE)) {
      const parsed = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
      return {
        dateKey: parsed.dateKey || "",
        dailyCount: Number(parsed.dailyCount || 0),
        channelCounts: parsed.channelCounts || {},
        commentedPosts: Array.isArray(parsed.commentedPosts) ? parsed.commentedPosts : [],
        myComments: parsed.myComments || {}
      };
    }
  } catch (e) {
    console.warn("Ошибка чтения state:", e.message);
  }
  return {
    dateKey: "",
    dailyCount: 0,
    channelCounts: {},
    commentedPosts: [],
    myComments: {}
  };
}

function saveState(state) {
  if (state.commentedPosts.length > 500) {
    state.commentedPosts = state.commentedPosts.slice(-500);
  }
  const keys = Object.keys(state.myComments);
  if (keys.length > 200) {
    for (const oldKey of keys.slice(0, keys.length - 200)) {
      delete state.myComments[oldKey];
    }
  }
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

function isAdOrNoise(text) {
  return /(#реклама|erid:|рекламодатель|подписывайся|подписывайтесь|розыгрыш|промокод|скидк[аиу]\s+\d+%)/i.test(text);
}

function fitCommentText(raw) {
  let text = String(raw || "")
    .replace(/[\r\n\t]+/g, " ")
    .replace(/#\S+/g, "")
    .replace(/^["«]|["»]$/g, "")
    .replace(/\s{2,}/g, " ")
    .trim();

  if (!text) return null;

  if (text.length > 130) {
    const firstEnd = text.search(/[.!?](\s|$)/);
    if (firstEnd > 15 && firstEnd <= 130) {
      text = text.slice(0, firstEnd + 1).trim();
    } else {
      return null;
    }
  }
  return text;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ==========================================
// 4. ЗАПРОС К GEMINI FLASH-LITE
// ==========================================
async function askGemini(promptText, history = []) {
  const model = String(process.env.GEMINI_MODEL || DEFAULT_MODEL).trim();
  const contents = history.map((item) => ({
    role: item.role,
    parts: [{ text: item.text }]
  }));
  contents.push({ role: "user", parts: [{ text: promptText }] });

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);

  try {
    const response = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-goog-api-key": GEMINI_API_KEY
        },
        body: JSON.stringify({
          systemInstruction: { parts: [{ text: SYSTEM_PROMPT }] },
          contents,
          generationConfig: {
            temperature: 0.75,
            maxOutputTokens: 60
          }
        }),
        signal: controller.signal
      }
    );

    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(`Gemini HTTP ${response.status}: ${JSON.stringify(data)}`);
    }

    const answer = (data?.candidates?.[0]?.content?.parts || [])
      .map((p) => (typeof p.text === "string" ? p.text : ""))
      .join("");

    return fitCommentText(answer);
  } finally {
    clearTimeout(timer);
  }
}

// ==========================================
// 5. РАЗОВЫЙ ПРОХОД (ЗАШЁЛ -> ПРОВЕРИЛ -> ВЫШЕЛ)
// ==========================================
async function runOnce() {
  if (!API_ID || !API_HASH || !SESSION_STRING || !GEMINI_API_KEY) {
    console.error("Не заданы обязательные секреты (TG_API_ID, TG_API_HASH, TG_SESSION, GEMINI_API_KEY).");
    process.exit(1);
  }

  const state = loadState();
  const { hour, dateKey } = getMskDateInfo();

  if (state.dateKey !== dateKey) {
    state.dateKey = dateKey;
    state.dailyCount = 0;
    state.channelCounts = {};
    saveState(state);
  }

  if (hour >= SLEEP_START_HOUR || hour < SLEEP_END_HOUR) {
    console.log("Ночной режим (сон). Завершаем работу.");
    return;
  }

  if (state.dailyCount >= MAX_DAILY_ACTIONS) {
    console.log(`Суточный лимит (${MAX_DAILY_ACTIONS}) уже выполнен. Завершаем работу.`);
    return;
  }

  const client = new TelegramClient(new StringSession(SESSION_STRING), API_ID, API_HASH, {
    connectionRetries: 3
  });

  await client.connect();
  console.log("Подключились к Telegram.");

  try {
    // --- ШАГ 1: ПРОВЕРЯЕМ РЕПЛАИ НА НАШИ ПРОШЛЫЕ КОММЕНТАРИИ ---
    let repliesSent = 0;
    const trackedEntries = Object.entries(state.myComments).filter(([, info]) => !info.replied);

    for (const [key, info] of trackedEntries) {
      if (repliesSent >= MAX_REPLIES_PER_RUN || state.dailyCount >= MAX_DAILY_ACTIONS) break;

      try {
        const recentMsgs = await client.getMessages(info.discussionChatId, { limit: 25 });
        const replyToMe = recentMsgs.find(
          (m) => !m.out && m.text && m.replyTo?.replyToMsgId === info.msgId
        );

        if (replyToMe) {
          console.log(`Найден ответ на наш комментарий: "${replyToMe.text.slice(0, 60)}"`);
          await sleep(4000 + Math.floor(Math.random() * 5000));

          const history = [
            { role: "user", text: `Текст поста:\n${info.postText}` },
            { role: "model", text: info.commentText }
          ];
          const replyText = await askGemini(
            `Читатель ответил на твой комментарий: "${replyToMe.text.slice(0, 350)}". Напиши короткий ответ.`,
            history
          );

          if (replyText) {
            await client.sendMessage(info.discussionChatId, {
              message: replyText,
              replyTo: replyToMe.id
            });
            info.replied = true;
            state.dailyCount += 1;
            repliesSent += 1;
            console.log(`[Ответ отправлен] -> ${replyText}`);
          }
        }
      } catch (e) {
        console.warn(`Ошибка проверки ветки ${key}:`, e.message);
      }
    }

    // --- ШАГ 2: ИЩЕМ МАКСИМУМ 1 СВЕЖИЙ ПОСТ ДЛЯ КОММЕНТАРИЯ ---
    if (state.dailyCount < MAX_DAILY_ACTIONS) {
      const shuffled = [...TARGET_CHANNELS]
        .map((c) => c.replace(/^@/, "").toLowerCase())
        .sort(() => Math.random() - 0.5);

      const nowSec = Math.floor(Date.now() / 1000);

      for (const username of shuffled) {
        if ((state.channelCounts[username] || 0) >= MAX_COMMENTS_PER_CHANNEL) continue;

        try {
          const msgs = await client.getMessages(username, { limit: 3 });
          const candidate = msgs.find((m) => {
            if (!m || !m.text || m.text.length < MIN_POST_LENGTH) return false;
            if (isAdOrNoise(m.text)) return false;
            if (nowSec - m.date > MAX_POST_AGE_HOURS * 3600) return false;
            const postKey = `${username}_${m.id}`;
            return !state.commentedPosts.includes(postKey);
          });

          if (!candidate) continue;

          console.log(`Выбран пост в @${username} (id=${candidate.id})`);
          await sleep(5000 + Math.floor(Math.random() * 7000));

          const postSnippet = candidate.text.slice(0, 1500);
          const commentText = await askGemini(`Текст поста:\n${postSnippet}`);
          if (!commentText) continue;

          const sent = await client.sendMessage(username, {
            message: commentText,
            commentTo: candidate.id
          });

          const postKey = `${username}_${candidate.id}`;
          state.commentedPosts.push(postKey);
          state.dailyCount += 1;
          state.channelCounts[username] = (state.channelCounts[username] || 0) + 1;

          if (sent?.id && sent?.chatId) {
            const trackKey = `${sent.chatId.toString()}_${sent.id}`;
            state.myComments[trackKey] = {
              discussionChatId: sent.chatId.toString(),
              msgId: sent.id,
              postText: postSnippet.slice(0, 400),
              commentText,
              replied: false
            };
          }

          console.log(`[Комментарий отправлен] @${username} -> ${commentText}`);
          break;
        } catch (e) {
          console.warn(`Пропуск канала @${username}:`, e.message);
        }
      }
    }

    saveState(state);
  } finally {
    await client.disconnect();
    console.log("Сессия закрыта, работа завершена.");
  }
}

runOnce().catch((err) => {
  console.error("Критическая ошибка:", err);
  process.exit(1);
});
