import os
import re
import json
import time
import asyncio
import hashlib
from typing import Dict, List, Tuple, Optional

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError


# ---------------- Настройки из переменных среды (GitHub Secrets) ----------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

DESTINATION = os.getenv("DESTINATION", "").strip()  # например: @my_channel
SOURCES_RAW = os.getenv("SOURCES", "").strip()      # например: @src1,@src2

# Как часто “тормозить” между отправками (чтобы не словить флуд)
INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "2"))

# Чтобы НЕ слал старые посты при первом запуске:
# 1 = первый запуск просто “запомнит последний пост” и ничего не отправит
SKIP_OLD_ON_FIRST_RUN = os.getenv("SKIP_OLD_ON_FIRST_RUN", "1").strip() == "1"

# Насколько активно перефразировать (1..3)
REWRITE_LEVEL = int(os.getenv("REWRITE_LEVEL", "3"))

# Дедуп по смыслу (чтобы одинаковые новости не повторялись)
DEDUP_TEXT = os.getenv("DEDUP_TEXT", "1").strip() == "1"
DEDUP_TTL_HOURS = int(os.getenv("DEDUP_TTL_HOURS", "72"))

STATE_FILE = os.getenv("STATE_FILE", "state.json")


def parse_sources(s: str) -> List[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


SOURCES = parse_sources(SOURCES_RAW)


# ---------------- state.json (память между запусками) ----------------
def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"last_id": {}, "seen_text": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_id": {}, "seen_text": {}}


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def cleanup_seen_text(state: Dict) -> None:
    if not DEDUP_TEXT:
        return
    cutoff = int(time.time()) - DEDUP_TTL_HOURS * 3600
    seen = state.get("seen_text", {})
    seen = {h: ts for h, ts in seen.items() if int(ts) >= cutoff}
    state["seen_text"] = seen


def text_hash(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"(?:https?://)?(?:t\.me|telegram\.me)/\S+", "", t)
    t = re.sub(r"@\w+", "", t)
    t = re.sub(r"#\w+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"[^\w\d]+", "", t)
    if not t:
        return ""
    return hashlib.sha1(t.encode("utf-8")).hexdigest()


# ---------------- Перефразирование (оффлайн, без ключей) ----------------
_BAD_LINE_RE = re.compile(r"(подпис|подпиш|репост|реклама|конкурс|розыгрыш|promo|скидк)", re.IGNORECASE)
_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/\S+", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_AT_RE = re.compile(r"@\w+")
_HASH_RE = re.compile(r"#\w+")


def clean_text(text: str) -> str:
    t = (text or "").replace("\u200b", "").strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def strip_ads_and_sources(text: str) -> str:
    t = clean_text(text)
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
    t = clean_text(text)
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
    if len(s) <= 170:
        score += 1
    return score


def stable_pick(options: List[str], seed: str, i: int = 0) -> str:
    if not options:
        return ""
    h = hashlib.sha1((seed + str(i)).encode("utf-8")).hexdigest()
    idx = int(h[:8], 16) % len(options)
    return options[idx]


def rewrite_ru(text: str, level: int = 3) -> str:
    base = strip_ads_and_sources(text)
    if not base:
        return ""

    sents = sentence_split(base)
    if not sents:
        return base

    ranked = sorted(sents, key=score_sentence, reverse=True)
    chosen: List[str] = []
    used = set()

    need = 2 if level == 1 else (3 if level == 2 else 4)
    for s in ranked:
        key = re.sub(r"\W+", "", s.lower())
        if key in used:
            continue
        used.add(key)
        chosen.append(s)
        if len(chosen) >= need:
            break

    seed = text_hash(text) or base[:50]

    # небольшие замены фраз (чем выше level — тем больше)
    mappings = [
        ("стало известно", ["появилась информация", "сообщают", "по данным на сейчас"]),
        ("сообщается", ["говорят", "по данным", "по информации"]),
        ("в настоящее время", ["сейчас", "на текущий момент"]),
        ("на данный момент", ["сейчас", "на текущий момент"]),
        ("по предварительным данным", ["предварительно", "по первым данным"]),
        ("произошло", ["случилось", "зафиксировали"]),
        ("проводится проверка", ["идёт проверка", "разбираются"]),
    ]

    out = " ".join(chosen).strip()
    if level >= 2:
        for a, opts in mappings:
            out = re.sub(rf"\b{re.escape(a)}\b", stable_pick(opts, seed), out, flags=re.IGNORECASE)

    # лид (заголовок-подводка)
    if level >= 3:
        leads = ["Коротко:", "Обновление:", "Что известно:", "Сводка:"]
        out = f"{stable_pick(leads, seed)} {out}"

    out = clean_text(out)
    return out


def clamp(text: str, max_len: int) -> str:
    t = clean_text(text)
    if len(t) <= max_len:
        return t
    t = t[:max_len].rsplit(" ", 1)[0].rstrip()
    return t + "…"


# ---------------- Основная логика: НЕ слушаем часами, а “проверили и вышли” ----------------
async def run_once():
    if API_ID <= 0 or not API_HASH:
        raise RuntimeError("Заполни API_ID и API_HASH (в GitHub Secrets).")
    if not SESSION_STRING:
        raise RuntimeError("Заполни SESSION_STRING (в GitHub Secrets).")
    if not DESTINATION:
        raise RuntimeError("Заполни DESTINATION (в GitHub Secrets).")
    if not SOURCES:
        raise RuntimeError("Заполни SOURCES (в GitHub Secrets).")

    state = load_state()
    cleanup_seen_text(state)

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.connect()

    # важно: если сессия занята где-то ещё, тут часто и вылезает ошибка
    if not await client.is_user_authorized():
        raise RuntimeError("Сессия не авторизована. Сгенерируй SESSION_STRING заново.")

    # destination entity
    dest = DESTINATION

    for src in SOURCES:
        entity = await client.get_entity(src)
        chat_key = str(getattr(entity, "id", src))
        last_id = int(state.get("last_id", {}).get(chat_key, 0))

        # Первый запуск: просто “запомнить последний пост”, ничего не слать
        if SKIP_OLD_ON_FIRST_RUN and last_id == 0:
            last_msg = await client.get_messages(entity, limit=1)
            if last_msg and last_msg[0]:
                state["last_id"][chat_key] = int(last_msg[0].id)
                save_state(state)
            continue

        # Берём новые сообщения (после last_id)
        msgs = []
        async for m in client.iter_messages(entity, min_id=last_id, reverse=True, limit=50):
            # пропускаем пустое
            if not m:
                continue
            msgs.append(m)

        if not msgs:
            continue

        # Группируем альбомы
        msgs.sort(key=lambda x: (x.date, x.id))
        grouped: Dict[int, List] = {}
        singles: List = []
        for m in msgs:
            if getattr(m, "grouped_id", None):
                grouped.setdefault(m.grouped_id, []).append(m)
            else:
                singles.append(m)

        # Список “пакетов” к отправке по времени
        items: List[Tuple[float, Optional[List], Optional[object]]] = []
        for gid, pack in grouped.items():
            pack.sort(key=lambda x: x.id)
            items.append((pack[0].date.timestamp(), pack, None))
        for m in singles:
            items.append((m.date.timestamp(), None, m))

        items.sort(key=lambda x: x[0])

        newest_id = last_id

        for _, album, msg in items:
            try:
                if album:
                    caption_src = (album[0].raw_text or "")
                    new_caption = clamp(rewrite_ru(caption_src, REWRITE_LEVEL), 900)

                    if DEDUP_TEXT and caption_src.strip():
                        h = text_hash(caption_src)
                        if h and h in state.get("seen_text", {}):
                            newest_id = max(newest_id, max(x.id for x in album))
                            continue

                    files = [x for x in album if getattr(x, "media", None)]
                    if files:
                        await client.send_file(
                            dest,
                            files=files,
                            caption=new_caption if new_caption else None
                        )

                    if DEDUP_TEXT and caption_src.strip():
                        h = text_hash(caption_src)
                        if h:
                            state["seen_text"][h] = int(time.time())

                    newest_id = max(newest_id, max(x.id for x in album))
                    await asyncio.sleep(INTERVAL_SECONDS)

                else:
                    assert msg is not None
                    original_text = msg.raw_text or ""
                    new_text = clamp(rewrite_ru(original_text, REWRITE_LEVEL), 900)

                    if DEDUP_TEXT and original_text.strip():
                        h = text_hash(original_text)
                        if h and h in state.get("seen_text", {}):
                            newest_id = max(newest_id, msg.id)
                            continue

                    if getattr(msg, "media", None):
                        await client.send_file(
                            dest,
                            file=msg,
                            caption=new_text if new_text else None
                        )
                    else:
                        if new_text:
                            await client.send_message(dest, new_text)

                    if DEDUP_TEXT and original_text.strip():
                        h = text_hash(original_text)
                        if h:
                            state["seen_text"][h] = int(time.time())

                    newest_id = max(newest_id, msg.id)
                    await asyncio.sleep(INTERVAL_SECONDS)

            except FloodWaitError as e:
                await asyncio.sleep(int(getattr(e, "seconds", 60)))
            except Exception as e:
                # не падаем целиком из-за одной новости
                print("Ошибка отправки:", e)

        state["last_id"][chat_key] = newest_id
        save_state(state)

    await client.disconnect()


def main():
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
