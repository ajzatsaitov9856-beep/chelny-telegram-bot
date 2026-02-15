import os
import re
import json
import time
import asyncio
import hashlib
from typing import Dict, List, Tuple, Optional
from datetime import datetime

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

# На GitHub Actions .env обычно нет — но локально может быть, так что не мешает
load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

DESTINATION = os.getenv("DESTINATION", "").strip()
SOURCES_RAW = os.getenv("SOURCES", "").strip()

# Пауза между отправками (чтобы не ловить флуд)
SEND_DELAY_SECONDS = int(os.getenv("SEND_DELAY_SECONDS", "3"))

# Сколько новых сообщений максимум брать за один запуск с 1 источника
MAX_PER_SOURCE = int(os.getenv("MAX_PER_SOURCE", "30"))

# Лимиты текста
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "900"))
MAX_CAPTION_CHARS = int(os.getenv("MAX_CAPTION_CHARS", "900"))

STATE_PATH = os.getenv("STATE_PATH", "state.json")


def parse_sources(s: str) -> List[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


SOURCES = parse_sources(SOURCES_RAW)

# -------------------- Простая бесплатная переформулировка --------------------
_BAD_LINE_RE = re.compile(r"(подпис|подпиш|репост|реклама|конкурс|розыгрыш|promo|скидк)", re.IGNORECASE)
_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/\S+", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_AT_RE = re.compile(r"@\w+")
_HASH_RE = re.compile(r"#\w+")


def cleanup_text(text: str) -> str:
    t = (text or "").replace("\u200b", "").strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def strip_links_ads_tags(text: str) -> str:
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


def norm_for_hash(text: str) -> str:
    t = strip_links_ads_tags(text).lower()
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"[^\w\d]+", "", t)
    return t


def stable_seed(text: str) -> int:
    n = norm_for_hash(text)
    if not n:
        return 0
    h = hashlib.sha1(n.encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def replace_phrases_ru(t: str) -> str:
    mapping = {
        "стало известно": "появилась информация",
        "сообщается": "по данным на сейчас",
        "в настоящее время": "сейчас",
        "на данный момент": "сейчас",
        "по предварительным данным": "предварительно",
        "в ближайшее время": "в скором времени",
        "проводится проверка": "идёт проверка",
        "произошло": "случилось",
        "задержан": "был задержан",
        "обнаружили": "нашли",
        "пострадали": "есть пострадавшие",
        "восстановили": "вернули в работу",
        "ограничено движение": "движение временно ограничили",
    }
    for a, b in mapping.items():
        t = re.sub(rf"\b{re.escape(a)}\b", b, t, flags=re.IGNORECASE)
    return t


def score_sentence(s: str) -> int:
    score = 0
    if re.search(r"\d", s):
        score += 3
    if re.search(r"\b(руб|₽|км|м|час|мин|ул\.|просп|пр\.|дом|№)\b", s, re.IGNORECASE):
        score += 2
    if re.search(r"\b(сегодня|вчера|завтра|утром|вечером|ночью)\b", s, re.IGNORECASE):
        score += 1
    if len(s) <= 160:
        score += 1
    return score


def clamp(text: str, max_len: int) -> str:
    t = cleanup_text(text)
    if len(t) <= max_len:
        return t
    t = t[:max_len].rsplit(" ", 1)[0].rstrip()
    return t + "…"


def free_rewrite_ru(text: str) -> str:
    """
    Бесплатная переформулировка без ИИ:
    - чистим мусор
    - выбираем 2-3 самых "фактовых" предложения
    - меняем порядок
    - немного заменяем фразы
    """
    t0 = strip_links_ads_tags(text)
    if not t0:
        return ""

    t0 = replace_phrases_ru(t0)
    sents = sentence_split(t0)
    if not sents:
        return ""

    ranked = sorted(sents, key=score_sentence, reverse=True)
    chosen = []
    seen = set()

    for s in ranked:
        key = re.sub(r"\W+", "", s.lower())
        if key in seen:
            continue
        seen.add(key)
        chosen.append(s)
        if len(chosen) >= 3:
            break

    # если мало — доберём
    if len(chosen) < 2:
        for s in sents:
            if s not in chosen:
                chosen.append(s)
            if len(chosen) >= 2:
                break

    # порядок слегка меняем “стабильно” (чтобы не было хаоса)
    seed = stable_seed(text)
    if seed % 2 == 1 and len(chosen) >= 2:
        chosen[0], chosen[1] = chosen[1], chosen[0]

    out = " ".join(chosen).strip()

    # аккуратный лид
    leads = ["Коротко:", "По городу:", "Что произошло:"]
    out = f"{leads[seed % len(leads)]} {out}".strip()

    return cleanup_text(out)


# -------------------- State (чтобы не слать старое) --------------------
def load_state() -> Dict[str, int]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): int(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def save_state(state: Dict[str, int]) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# -------------------- Telegram copy helpers --------------------
def is_album(msg) -> bool:
    return bool(getattr(msg, "grouped_id", None))


async def send_one(client: TelegramClient, msg, caption_text: str) -> None:
    if getattr(msg, "media", None):
        await client.send_file(
            DESTINATION,
            file=msg,  # Message -> перезальёт медиа как новое сообщение
            caption=caption_text if caption_text else None
        )
    else:
        if caption_text:
            await client.send_message(DESTINATION, caption_text)


async def send_album(client: TelegramClient, msgs: List, caption_text: str) -> None:
    files = [m for m in msgs if getattr(m, "media", None)]
    if not files:
        return
    await client.send_file(
        DESTINATION,
        files=files,
        caption=caption_text if caption_text else None
    )


async def main():
    if API_ID <= 0 or not API_HASH:
        raise RuntimeError("❌ Не заполнены API_ID / API_HASH (Secrets).")
    if not SESSION_STRING:
        raise RuntimeError("❌ Не заполнен SESSION_STRING (Secrets).")
    if not DESTINATION:
        raise RuntimeError("❌ Не заполнен DESTINATION (Secrets).")
    if not SOURCES:
        raise RuntimeError("❌ Не заполнен SOURCES (Secrets).")

    state = load_state()
    changed = False

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError("❌ SESSION_STRING недействителен. Сгенерируй заново.")

    # 1) Bootstrap: если по источнику нет state — запоминаем последний пост и НЕ постим старьё
    bootstrapped = []
    for src in SOURCES:
        if src not in state:
            last = 0
            msgs = await client.get_messages(src, limit=1)
            if msgs:
                last = int(msgs[0].id)
            state[src] = last
            bootstrapped.append(src)
            changed = True

    if bootstrapped:
        save_state(state)
        print("✅ Первый запуск (bootstrap). Ничего не отправляю, просто запомнил последние посты:")
        for s in bootstrapped:
            print(" -", s, "last_id=", state[s])
        await client.disconnect()
        return

    # 2) Забираем только новые, отправляем, обновляем state
    total_sent = 0

    for src in SOURCES:
        last_id = int(state.get(src, 0))
        new_msgs = []

        # reverse=True -> от старых к новым (удобно отправлять по порядку)
        async for m in client.iter_messages(src, min_id=last_id, reverse=True):
            # пропускаем служебное
            if getattr(m, "message", None) is None and not getattr(m, "media", None):
                continue
            new_msgs.append(m)
            if len(new_msgs) >= MAX_PER_SOURCE:
                break

        if not new_msgs:
            continue

        # группируем альбомы
        albums: Dict[int, List] = {}
        singles = []
        max_seen = last_id

        for m in new_msgs:
            max_seen = max(max_seen, int(m.id))
            gid = getattr(m, "grouped_id", None)
            if gid:
                albums.setdefault(int(gid), []).append(m)
            else:
                singles.append(m)

        # отправляем одиночные
        for m in singles:
            original_text = (m.raw_text or "").strip()
            rewritten = clamp(free_rewrite_ru(original_text), MAX_TEXT_CHARS)

            try:
                await send_one(client, m, rewritten)
                total_sent += 1
                await asyncio.sleep(SEND_DELAY_SECONDS)
            except FloodWaitError as e:
                await asyncio.sleep(int(getattr(e, "seconds", 60)))
            except Exception as e:
                print("Ошибка отправки:", src, "msg", m.id, e)

        # отправляем альбомы
        for gid, msgs in albums.items():
            msgs_sorted = sorted(msgs, key=lambda x: x.id)
            caption_src = (msgs_sorted[0].raw_text or "").strip()
            caption_new = clamp(free_rewrite_ru(caption_src), MAX_CAPTION_CHARS)

            try:
                await send_album(client, msgs_sorted, caption_new)
                total_sent += 1
                await asyncio.sleep(SEND_DELAY_SECONDS)
            except FloodWaitError as e:
                await asyncio.sleep(int(getattr(e, "seconds", 60)))
            except Exception as e:
                print("Ошибка альбома:", src, "gid", gid, e)

        # обновляем last_id
        state[src] = max_seen
        changed = True

    if changed:
        save_state(state)

    print(f"✅ Готово. Отправлено постов: {total_sent}. Время: {datetime.now().isoformat(timespec='seconds')}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
