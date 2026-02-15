import os
import re
import json
import time
import asyncio
import hashlib
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, RPCError

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

DESTINATION = os.getenv("DESTINATION", "").strip()
SOURCES_RAW = os.getenv("SOURCES", "").strip()

STATE_FILE = os.getenv("STATE_FILE", "state.json").strip()
PER_SOURCE_LIMIT = int(os.getenv("PER_SOURCE_LIMIT", "30"))  # сколько сообщений смотреть за запуск на каждый источник
SLEEP_BETWEEN_SENDS = float(os.getenv("SLEEP_BETWEEN_SENDS", "1.5"))  # пауза между отправками
MAX_CAPTION = int(os.getenv("MAX_CAPTION", "850"))

# --- аккуратная очистка текста ---
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_TME_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/\S+", re.IGNORECASE)
_AT_RE = re.compile(r"@\w+")
_HASH_RE = re.compile(r"#\w+")
_MULTI_SPACE = re.compile(r"\s{2,}")
_BAD_LINES_RE = re.compile(r"(подпис|подпиш|реклама|конкурс|розыгрыш|promo|скидк|репост)", re.IGNORECASE)

def parse_sources(s: str) -> List[str]:
    return [x.strip() for x in s.split(",") if x.strip()]

SOURCES = parse_sources(SOURCES_RAW)

def clean_text(text: str) -> str:
    t = (text or "").replace("\u200b", "").strip()
    if not t:
        return ""
    t = _TME_RE.sub("", t)
    t = _URL_RE.sub("", t)
    t = _AT_RE.sub("", t)
    t = _HASH_RE.sub("", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = _MULTI_SPACE.sub(" ", t)
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
    if len(s) <= 160:
        score += 1
    return score

def make_summary(text: str) -> str:
    """
    Без “маскировки”, просто короткое резюме:
    - чистим ссылки/упоминания/хэштеги
    - берем 1–3 самых фактовых предложения
    """
    # выкинуть строки-рекламу
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines if not _BAD_LINES_RE.search(ln)]
    base = " ".join(lines).strip()

    sents = sentence_split(base)
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

    out = " ".join(chosen).strip()
    if out and not out.lower().startswith("коротко"):
        out = "Коротко: " + out
    return out.strip()

def clamp(text: str, max_len: int) -> str:
    t = (text or "").strip()
    if len(t) <= max_len:
        return t
    t = t[:max_len].rsplit(" ", 1)[0].rstrip()
    return t + "…"

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"last_ids": {}, "sent_hashes": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("last_ids", {})
        data.setdefault("sent_hashes", [])
        return data
    except Exception:
        return {"last_ids": {}, "sent_hashes": []}

def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

async def safe_sleep(seconds: float):
    if seconds and seconds > 0:
        await asyncio.sleep(seconds)

async def main():
    if API_ID <= 0 or not API_HASH or not SESSION_STRING:
        raise RuntimeError("Нужны API_ID, API_HASH, SESSION_STRING (в Secrets).")
    if not DESTINATION:
        raise RuntimeError("Нужен DESTINATION (например @my_channel).")
    if not SOURCES:
        raise RuntimeError("Нужен SOURCES (например @src1,@src2).")

    state = load_state()
    sent_hashes = set(state.get("sent_hashes", [])[-500:])  # ограничим память

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("SESSION_STRING не авторизован. Пересоздай SESSION_STRING.")

    dest = await client.get_entity(DESTINATION)

    # 1) Первый запуск: ничего НЕ отправляем, просто запоминаем последние id,
    # чтобы не улетели старые посты.
    first_run = not state.get("last_ids")
    if first_run:
        last_ids = {}
        for src in SOURCES:
            ent = await client.get_entity(src)
            msgs = await client.get_messages(ent, limit=1)
            last_ids[src] = int(msgs[0].id) if msgs else 0
        state["last_ids"] = last_ids
        state["sent_hashes"] = list(sent_hashes)
        save_state(state)
        print("✅ Первый запуск: запомнил последние посты. Старое НЕ отправляю. Следующий запуск будет отправлять только новое.")
        await client.disconnect()
        return

    # 2) Обычный запуск: ищем посты новее last_id
    updated_any = False
    for src in SOURCES:
        ent = await client.get_entity(src)
        last_id = int(state["last_ids"].get(src, 0))

        msgs = await client.get_messages(ent, limit=PER_SOURCE_LIMIT, min_id=last_id)
        msgs = [m for m in msgs if m and m.id and m.id > last_id]
        if not msgs:
            continue

        # сортируем по id, чтобы отправлять по порядку
        msgs.sort(key=lambda m: m.id)

        # группируем альбомы
        albums: Dict[int, List] = {}
        singles: List = []
        for m in msgs:
            if getattr(m, "grouped_id", None):
                albums.setdefault(m.grouped_id, []).append(m)
            else:
                singles.append(m)

        items: List[Tuple[int, str, List]] = []
        for gid, group in albums.items():
            group.sort(key=lambda m: m.id)
            items.append((group[0].id, "album", group))
        for m in singles:
            items.append((m.id, "single", [m]))
        items.sort(key=lambda x: x[0])

        max_sent_id = last_id

        for first_id, kind, pack in items:
            # текст берём с первого сообщения
            src_text = (pack[0].raw_text or "").strip()
            summary = clamp(make_summary(src_text), MAX_CAPTION)

            # честный источник (минимально)
            src_name = getattr(ent, "title", None) or src
            caption = summary
            if caption:
                caption = f"{caption}\n\nИсточник: {src_name}"
            else:
                caption = f"Источник: {src_name}"

            # антидубль (по резюме)
            h = sha1(caption.lower())
            if h in sent_hashes:
                max_sent_id = max(max_sent_id, max(m.id for m in pack))
                continue

            try:
                has_media = any(getattr(m, "media", None) for m in pack)
                if has_media:
                    files = [m for m in pack if getattr(m, "media", None)]
                    await client.send_file(dest, file=files, caption=caption)
                else:
                    await client.send_message(dest, caption, link_preview=False)

                sent_hashes.add(h)
                max_sent_id = max(max_sent_id, max(m.id for m in pack))
                updated_any = True

                await safe_sleep(SLEEP_BETWEEN_SENDS)

            except FloodWaitError as e:
                await asyncio.sleep(int(getattr(e, "seconds", 30)))
            except RPCError as e:
                print("RPCError:", e)
            except Exception as e:
                print("Ошибка отправки:", e)

        state["last_ids"][src] = int(max_sent_id)

    # сохраняем state
    state["sent_hashes"] = list(sent_hashes)[-500:]
    save_state(state)

    if updated_any:
        print("✅ Отправил новое и обновил state.json")
    else:
        print("✅ Новых постов нет (state.json проверен/обновлён)")

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
