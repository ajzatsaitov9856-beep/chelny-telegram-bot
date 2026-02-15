import os
import re
import time
import asyncio
import sqlite3
import hashlib
import random
from typing import List, Tuple, Optional

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

load_dotenv()

# ====== ENV ======
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

DESTINATION = os.getenv("DESTINATION", "").strip()
SOURCES_RAW = os.getenv("SOURCES", "").strip()

INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "20"))
START_FROM_NOW = os.getenv("START_FROM_NOW", "1").strip() == "1"  # 1 = не брать старые

DEDUP_DB = os.getenv("DEDUP_DB", "dedup.sqlite").strip()
DEDUP_TEXT = os.getenv("DEDUP_TEXT", "1").strip() == "1"
DEDUP_TEXT_TTL_HOURS = int(os.getenv("DEDUP_TEXT_TTL_HOURS", "72"))

MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "1200"))
MAX_CAPTION_CHARS = int(os.getenv("MAX_CAPTION_CHARS", "900"))

# ВАЖНО: источник обязателен (мы его показываем)
CREDIT = os.getenv("CREDIT", "1").strip() == "1"  # 1 = добавлять "Источник"
CREDIT_STYLE = os.getenv("CREDIT_STYLE", "link").strip().lower()  # link | mention | name


def parse_sources(s: str) -> List[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


SOURCES = parse_sources(SOURCES_RAW)

# ====== DB (антидубликаты) ======
def db_init() -> None:
    con = sqlite3.connect(DEDUP_DB)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_msg (
            chat_id INTEGER NOT NULL,
            msg_id  INTEGER NOT NULL,
            ts      INTEGER NOT NULL,
            PRIMARY KEY (chat_id, msg_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_text (
            h   TEXT PRIMARY KEY,
            ts  INTEGER NOT NULL
        )
    """)
    con.commit()
    con.close()


def db_seen_msg(chat_id: int, msg_id: int) -> bool:
    con = sqlite3.connect(DEDUP_DB)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM seen_msg WHERE chat_id=? AND msg_id=? LIMIT 1", (chat_id, msg_id))
    row = cur.fetchone()
    con.close()
    return row is not None


def db_mark_msgs(pairs: List[Tuple[int, int]]) -> None:
    if not pairs:
        return
    now = int(time.time())
    con = sqlite3.connect(DEDUP_DB)
    cur = con.cursor()
    cur.executemany(
        "INSERT OR IGNORE INTO seen_msg(chat_id, msg_id, ts) VALUES(?,?,?)",
        [(c, m, now) for (c, m) in pairs],
    )
    con.commit()
    con.close()


def db_cleanup_text_ttl() -> None:
    if not DEDUP_TEXT:
        return
    cutoff = int(time.time()) - DEDUP_TEXT_TTL_HOURS * 3600
    con = sqlite3.connect(DEDUP_DB)
    cur = con.cursor()
    cur.execute("DELETE FROM seen_text WHERE ts < ?", (cutoff,))
    con.commit()
    con.close()


def db_seen_text(h: str) -> bool:
    if not DEDUP_TEXT:
        return False
    con = sqlite3.connect(DEDUP_DB)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM seen_text WHERE h=? LIMIT 1", (h,))
    row = cur.fetchone()
    con.close()
    return row is not None


def db_mark_text(h: str) -> None:
    if not DEDUP_TEXT:
        return
    now = int(time.time())
    con = sqlite3.connect(DEDUP_DB)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO seen_text(h, ts) VALUES(?,?)", (h, now))
    con.commit()
    con.close()


# ====== REWRITE (офлайн, “обширнее”) ======
_BAD_LINE_RE = re.compile(r"(подпис|подпиш|репост|реклама|конкурс|розыгрыш|promo|скидк|акци|промокод)", re.IGNORECASE)
_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/\S+", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_AT_RE = re.compile(r"@\w+")
_HASH_RE = re.compile(r"#\w+")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def cleanup_text(text: str) -> str:
    t = (text or "").replace("\u200b", "").strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = _MULTI_SPACE.sub(" ", t)
    return t.strip()


def strip_ads_links_mentions(text: str) -> str:
    t = cleanup_text(text)
    if not t:
        return ""

    t = _LINK_RE.sub("", t)
    t = _URL_RE.sub("", t)
    t = _AT_RE.sub("", t)
    t = _HASH_RE.sub("", t)

    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not _BAD_LINE_RE.search(ln)]
    t = " ".join(lines)

    t = re.sub(r"\s+([,.!?;:])", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()


def sentence_split(text: str) -> List[str]:
    t = cleanup_text(text)
    if not t:
        return []
    parts = re.split(r"(?<=[.!?])\s+", t)
    return [p.strip() for p in parts if len(p.strip()) >= 3]


def score_sentence(s: str) -> int:
    score = 0
    if re.search(r"\d", s):
        score += 4
    if re.search(r"\b(руб|₽|км|м|час|мин|ул\.|улиц|просп|пр\.|дом|№|район)\b", s, re.IGNORECASE):
        score += 3
    if re.search(r"\b(сегодня|вчера|завтра|утром|вечером|ночью|сейчас)\b", s, re.IGNORECASE):
        score += 2
    if 40 <= len(s) <= 180:
        score += 2
    return score


def paraphrase_phrases_ru(t: str, rng: random.Random) -> str:
    # больше замен, чтобы “обширнее”
    mapping = [
        ("стало известно", ["появилась информация", "выяснилось", "сообщают"]),
        ("сообщается", ["по данным на сейчас", "по сообщениям", "по информации"]),
        ("в настоящее время", ["сейчас", "на данный момент"]),
        ("на данный момент", ["сейчас", "в данный момент"]),
        ("по предварительным данным", ["предварительно", "по первичным данным"]),
        ("в ближайшее время", ["в скором времени", "в ближайшие дни"]),
        ("проводится проверка", ["идёт проверка", "проверяют обстоятельства"]),
        ("произошло", ["случилось", "зафиксировали"]),
        ("обнаружили", ["нашли", "выявили"]),
        ("в результате", ["в итоге", "по итогу"]),
        ("власти", ["администрация", "городские службы"]),
        ("жители", ["горожане", "местные"]),
        ("обращаются", ["сообщают", "пишут"]),
        ("из-за", ["по причине", "в связи с"]),
        ("отмечают", ["говорят", "уточняют"]),
        ("по словам", ["как заявили", "как сообщили"]),
        ("напоминаем", ["важно помнить", "на всякий случай"]),
    ]

    out = t
    for a, variants in mapping:
        # случайный вариант замены, но стабильно по rng
        b = rng.choice(variants)
        out = re.sub(rf"\b{re.escape(a)}\b", b, out, flags=re.IGNORECASE)

    # мелкие перестановки
    out = re.sub(r"\bне\s+исключено\b", rng.choice(["возможно", "есть вероятность"]), out, flags=re.IGNORECASE)
    out = re.sub(r"\bмогут\b", rng.choice(["могут", "вполне могут", "способны"]), out, flags=re.IGNORECASE)
    return out


def build_lead_ru(rng: random.Random) -> str:
    leads = [
        "Коротко по ситуации:",
        "Главное за минуту:",
        "Что известно сейчас:",
        "Сводка:",
        "По фактам:",
        "Обновление:",
    ]
    return rng.choice(leads)


def norm_for_hash(text: str) -> str:
    t = strip_ads_links_mentions(text).lower()
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"[^\w\d]+", "", t)
    return t


def text_hash(text: str) -> str:
    n = norm_for_hash(text)
    return hashlib.sha1(n.encode("utf-8")).hexdigest() if n else ""


def clamp(text: str, max_len: int) -> str:
    t = cleanup_text(text)
    if len(t) <= max_len:
        return t
    t = t[:max_len].rsplit(" ", 1)[0].rstrip()
    return t + "…"


def free_rewrite_ru(original: str) -> str:
    """
    Более “обширная” бесплатная переработка:
    - чистим ссылки/@/# и рекламные строки
    - выбираем 2–4 фактовых предложения
    - меняем порядок + перефразируем фразы
    - добавляем лид
    """
    base = strip_ads_links_mentions(original)
    if not base:
        return ""

    # rng стабильный: один и тот же текст -> одинаковая переработка
    seed = int(hashlib.md5(base.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)

    base2 = paraphrase_phrases_ru(base, rng)

    sents = sentence_split(base2)
    if not sents:
        return ""

    ranked = sorted(sents, key=score_sentence, reverse=True)

    chosen: List[str] = []
    seen = set()
    for s in ranked:
        key = re.sub(r"\W+", "", s.lower())
        if key in seen:
            continue
        seen.add(key)
        chosen.append(s)
        if len(chosen) >= 4:
            break

    # переставим порядок: часто “детали” после “главного”
    if len(chosen) >= 2:
        # случайно выберем вариант перестановки
        if rng.random() < 0.7:
            chosen[0], chosen[1] = chosen[1], chosen[0]

    # лёгкая склейка
    out = " ".join(chosen).strip()
    out = re.sub(r"\s{2,}", " ", out)

    # лид
    lead = build_lead_ru(rng)
    if out and not out.lower().startswith(lead.lower()):
        out = f"{lead} {out}"

    return cleanup_text(out)


# ====== SOURCE CREDIT ======
def build_credit(chat) -> str:
    if not CREDIT:
        return ""

    title = getattr(chat, "title", None) or "источник"
    username = getattr(chat, "username", None)

    if CREDIT_STYLE == "mention" and username:
        return f"Источник: @{username}"
    if CREDIT_STYLE == "link" and username:
        return f"Источник: https://t.me/{username}"
    return f"Источник: {title}"


# ====== MAIN ======
async def main():
    if API_ID <= 0 or not API_HASH:
        raise RuntimeError("Заполни API_ID и API_HASH")
    if not SESSION_STRING:
        raise RuntimeError("Заполни SESSION_STRING")
    if not DESTINATION:
        raise RuntimeError("Заполни DESTINATION (например @my_channel)")
    if not SOURCES:
        raise RuntimeError("Заполни SOURCES (например @src1,@src2,...)")

    db_init()
    db_cleanup_text_ttl()

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

    started_at = int(time.time())
    send_lock = asyncio.Lock()

    def is_old(event_ts: int) -> bool:
        return START_FROM_NOW and (event_ts < started_at)

    async def paced_sleep():
        if INTERVAL_SECONDS > 0:
            await asyncio.sleep(INTERVAL_SECONDS)

    @client.on(events.Album(chats=SOURCES))
    async def on_album(event):
        msgs = list(event.messages or [])
        if not msgs:
            return

        event_ts = int(event.date.timestamp()) if event.date else int(time.time())
        if is_old(event_ts):
            return

        pairs = [(m.chat_id, m.id) for m in msgs]
        if any(db_seen_msg(c, mid) for (c, mid) in pairs):
            return

        chat = await event.get_chat()
        credit = build_credit(chat)

        caption_src = (msgs[0].raw_text or "")
        caption_new = free_rewrite_ru(caption_src)
        if credit:
            caption_new = (caption_new + "\n\n" + credit).strip() if caption_new else credit
        caption_new = clamp(caption_new, MAX_CAPTION_CHARS)

        # антидубль по смыслу
        if DEDUP_TEXT and caption_src.strip():
            h = text_hash(caption_src)
            if h and db_seen_text(h):
                db_mark_msgs(pairs)
                return

        try:
            async with send_lock:
                media_msgs = [m for m in msgs if getattr(m, "media", None)]
                if not media_msgs:
                    return

                # Перепубликация: медиа сохраняется, текст меняем
                await client.send_file(
                    DESTINATION,
                    files=media_msgs,
                    caption=caption_new if caption_new else None
                )

                db_mark_msgs(pairs)
                if DEDUP_TEXT and caption_src.strip():
                    h = text_hash(caption_src)
                    if h:
                        db_mark_text(h)

                await paced_sleep()

        except FloodWaitError as e:
            await asyncio.sleep(int(getattr(e, "seconds", 60)))
        except Exception as e:
            print("Ошибка альбома:", e)

    @client.on(events.NewMessage(chats=SOURCES))
    async def on_message(event):
        if event.out:
            return
        if getattr(event.message, "grouped_id", None):
            return  # альбом обработает on_album

        event_ts = int(event.date.timestamp()) if event.date else int(time.time())
        if is_old(event_ts):
            return

        chat_id = event.chat_id
        msg_id = event.id

        if db_seen_msg(chat_id, msg_id):
            return

        chat = await event.get_chat()
        credit = build_credit(chat)

        original_text = event.raw_text or ""
        new_text = free_rewrite_ru(original_text)
        if credit:
            new_text = (new_text + "\n\n" + credit).strip() if new_text else credit
        new_text = clamp(new_text, MAX_TEXT_CHARS)

        # антидубль по смыслу
        if DEDUP_TEXT and original_text.strip():
            h = text_hash(original_text)
            if h and db_seen_text(h):
                db_mark_msgs([(chat_id, msg_id)])
                return

        try:
            async with send_lock:
                if getattr(event.message, "media", None):
                    await client.send_file(
                        DESTINATION,
                        file=event.message,
                        caption=new_text if new_text else None
                    )
                else:
                    if not new_text:
                        db_mark_msgs([(chat_id, msg_id)])
                        return
                    await client.send_message(DESTINATION, new_text)

                db_mark_msgs([(chat_id, msg_id)])
                if DEDUP_TEXT and original_text.strip():
                    h = text_hash(original_text)
                    if h:
                        db_mark_text(h)

                await paced_sleep()

        except FloodWaitError as e:
            await asyncio.sleep(int(getattr(e, "seconds", 60)))
        except Exception as e:
            print("Ошибка сообщения:", e)

    print("✅ Запуск. Ждём новые посты…")
    await client.start()
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
