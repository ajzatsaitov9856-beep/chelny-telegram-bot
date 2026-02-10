# publisher.py — БЕСПЛАТНАЯ “солянка” (без ИИ/ключей)
# ✅ Берёт новые посты из SOURCES
# ✅ Публикует в DESTINATION БЕЗ "Forwarded from…" и БЕЗ упоминаний источников
# ✅ Текст переформулируется офлайн (бесплатно)
# ✅ Фото/видео/альбомы остаются такими же
# ✅ Антидубли (по msg_id + по смыслу текста) + регулировка темпа
#
# Установка:
#   pip install telethon python-dotenv
#
# Запуск:
#   python publisher.py
#
# Важно: работает только на НОВЫХ постах (старое не подтягивает).

import os
import re
import time
import sqlite3
import hashlib
import asyncio
from typing import List, Tuple

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, PasswordHashInvalidError

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_NAME = os.getenv("SESSION_NAME", "publisher_session").strip()

DESTINATION = os.getenv("DESTINATION", "").strip()
SOURCES_RAW = os.getenv("SOURCES", "").strip()
INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "15"))
DEDUP_DB = os.getenv("DEDUP_DB", "dedup.sqlite").strip()

# оформление
ADD_PREFIX = os.getenv("ADD_PREFIX", "1").strip() == "1"
PREFIX_TEXT = os.getenv("PREFIX_TEXT", "Коротко:").strip()

MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "900"))
MAX_CAPTION_CHARS = int(os.getenv("MAX_CAPTION_CHARS", "900"))

# антидубль по смыслу (одинаковые новости из разных каналов)
DEDUP_TEXT = os.getenv("DEDUP_TEXT", "1").strip() == "1"
DEDUP_TEXT_TTL_HOURS = int(os.getenv("DEDUP_TEXT_TTL_HOURS", "72"))


def parse_sources(s: str) -> List[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


SOURCES = parse_sources(SOURCES_RAW)

# ---------------- DB ----------------
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
            h       TEXT PRIMARY KEY,
            ts      INTEGER NOT NULL
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


# ---------------- Text tools (FREE rewrite) ----------------
_BAD_LINE_RE = re.compile(r"(подпис|подпиш|репост|реклама|конкурс|розыгрыш|promo|скидк)", re.IGNORECASE)
_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/\S+", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_AT_RE = re.compile(r"@\w+")
_HASH_RE = re.compile(r"#\w+")


def cleanup_text(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    t = t.replace("\u200b", "")  # zero-width
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def strip_sources_and_ads(text: str) -> str:
    t = cleanup_text(text)
    if not t:
        return ""

    # убрать ссылки / @ / хэштеги
    t = _LINK_RE.sub("", t)
    t = _URL_RE.sub("", t)
    t = _AT_RE.sub("", t)
    t = _HASH_RE.sub("", t)

    # удалить строки с рекламой/призывами
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not _BAD_LINE_RE.search(ln)]
    t = " ".join(lines)

    # нормализовать пробелы и пунктуацию
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()


def paraphrase_phrases(t: str) -> str:
    mapping = {
        "стало известно": "появилась информация",
        "сообщается": "по данным на сейчас",
        "в настоящее время": "сейчас",
        "на данный момент": "сейчас",
        "по предварительным данным": "предварительно",
        "в ближайшее время": "в скором времени",
        "проводится проверка": "идёт проверка",
        "произошло": "случилось",
    }
    for a, b in mapping.items():
        t = re.sub(rf"\b{re.escape(a)}\b", b, t, flags=re.IGNORECASE)
    return t


def sentence_split(text: str) -> List[str]:
    t = cleanup_text(text)
    if not t:
        return []
    parts = re.split(r"(?<=[.!?])\s+", t)
    return [p.strip() for p in parts if len(p.strip()) >= 3]


def score_sentence(s: str) -> int:
    score = 0
    if re.search(r"\d", s):
        score += 3
    if re.search(r"\b(руб|₽|км|м|час|мин|ул\.|просп|пр\.|дом|№)\b", s, re.IGNORECASE):
        score += 2
    if re.search(r"\b(сегодня|вчера|завтра|утром|вечером|ночью)\b", s, re.IGNORECASE):
        score += 1
    if len(s) <= 140:
        score += 1
    return score


def free_rewrite_ru(text: str) -> str:
    """
    Бесплатная переработка:
    - чистим мусор (ссылки, @, хэштеги, реклама)
    - берём 2–3 самых “фактовых” предложения
    - слегка меняем структуру и фразы
    - добавляем аккуратный лид
    """
    t0 = strip_sources_and_ads(text)
    if not t0:
        return ""

    t0 = paraphrase_phrases(t0)
    sents = sentence_split(t0)
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
        if len(chosen) >= 3:
            break

    if len(chosen) < 2:
        for s in sents:
            if s not in chosen:
                chosen.append(s)
            if len(chosen) >= 2:
                break

    if len(chosen) >= 2:
        chosen[0], chosen[1] = chosen[1], chosen[0]

    out = " ".join(chosen).strip()

    if ADD_PREFIX and out and not out.lower().startswith((PREFIX_TEXT.lower(), "коротко", "обновление")):
        out = f"{PREFIX_TEXT} {out}".strip()

    return cleanup_text(out)


def clamp(text: str, max_len: int) -> str:
    t = cleanup_text(text)
    if len(t) <= max_len:
        return t
    t = t[:max_len].rsplit(" ", 1)[0].rstrip()
    return t + "…"


def norm_for_hash(text: str) -> str:
    t = strip_sources_and_ads(text).lower()
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"[^\w\d]+", "", t)
    return t


def text_hash(text: str) -> str:
    n = norm_for_hash(text)
    return hashlib.sha1(n.encode("utf-8")).hexdigest() if n else ""


# ---------------- Main ----------------
async def main():
    if API_ID <= 0 or not API_HASH:
        raise RuntimeError("Заполни API_ID и API_HASH в .env")
    if not DESTINATION:
        raise RuntimeError("Заполни DESTINATION в .env (например @my_channel)")
    if not SOURCES:
        raise RuntimeError("Заполни SOURCES в .env (например @src1,@src2,...)")

    db_init()
    db_cleanup_text_ttl()

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    send_lock = asyncio.Lock()

    async def paced_sleep():
        if INTERVAL_SECONDS > 0:
            await asyncio.sleep(INTERVAL_SECONDS)

    @client.on(events.Album(chats=SOURCES))
    async def on_album(event):
        msgs = list(event.messages or [])
        if not msgs:
            return

        pairs = [(m.chat_id, m.id) for m in msgs]
        if any(db_seen_msg(c, mid) for (c, mid) in pairs):
            return

        caption_src = (msgs[0].raw_text or "")
        caption_new = clamp(free_rewrite_ru(caption_src), MAX_CAPTION_CHARS)

        if DEDUP_TEXT and caption_src.strip():
            h = text_hash(caption_src)
            if h and db_seen_text(h):
                db_mark_msgs(pairs)
                return

        try:
            async with send_lock:
                files = [m for m in msgs if getattr(m, "media", None)]
                if not files:
                    return

                await client.send_file(
                    DESTINATION,
                    files=files,  # Message-объекты: медиа останется оригинальным
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
            return

        chat_id = event.chat_id
        msg_id = event.id

        if db_seen_msg(chat_id, msg_id):
            return

        original_text = event.raw_text or ""
        new_text = clamp(free_rewrite_ru(original_text), MAX_TEXT_CHARS)

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
                        file=event.message,  # Message -> медиа останется оригинальным
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

    print("✅ Запуск: копируем медиа, текст переформулируем бесплатно, источники не упоминаем.")
    try:
        await client.start()
    except PasswordHashInvalidError:
        print("❌ Неверный пароль 2FA. Запусти снова и введи правильный пароль.")
        return

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
