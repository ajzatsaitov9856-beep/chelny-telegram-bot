import os
import re
import json
import time
import asyncio
import hashlib
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon import events
from telethon.errors import FloodWaitError, AuthKeyDuplicatedError

# ------------------- ENV -------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

DESTINATION = os.getenv("DESTINATION", "").strip()          # например: @my_channel
SOURCES_RAW = os.getenv("SOURCES", "").strip()              # например: @src1,@src2
INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "10")) # пауза между публикациями
RUN_MINUTES = int(os.getenv("RUN_MINUTES", "330"))          # сколько минут живёт один запуск (330 ~ 5.5 часов)

# антидубли по тексту
DEDUP_TEXT = os.getenv("DEDUP_TEXT", "1").strip() == "1"
DEDUP_TTL_HOURS = int(os.getenv("DEDUP_TTL_HOURS", "72"))

STATE_FILE = "state.json"

# ------------------- helpers -------------------
def parse_sources(s: str) -> List[str]:
    return [x.strip() for x in s.split(",") if x.strip()]

SOURCES = parse_sources(SOURCES_RAW)

def now_ts() -> int:
    return int(time.time())

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

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
    cutoff = now_ts() - DEDUP_TTL_HOURS * 3600
    seen = state.get("seen_text", {})
    to_del = [h for h, ts in seen.items() if ts < cutoff]
    for h in to_del:
        del seen[h]
    state["seen_text"] = seen

# ------------------- FREE rewrite (offline) -------------------
_BAD_LINE_RE = re.compile(r"(подпис|подпиш|репост|реклама|конкурс|розыгрыш|promo|скидк)", re.IGNORECASE)
_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/\S+", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_AT_RE = re.compile(r"@\w+")
_HASH_RE = re.compile(r"#\w+")

def _norm(text: str) -> str:
    t = (text or "").replace("\u200b", "").strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()

def strip_noise(text: str) -> str:
    t = _norm(text)
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
    t = _norm(text)
    if not t:
        return []
    parts = re.split(r"(?<=[.!?])\s+", t)
    return [p.strip() for p in parts if len(p.strip()) >= 3]

def score_sentence(s: str) -> int:
    sc = 0
    if re.search(r"\d", s):
        sc += 3
    if re.search(r"\b(руб|₽|км|м|час|мин|ул\.|просп|пр\.|дом|№)\b", s, re.IGNORECASE):
        sc += 2
    if re.search(r"\b(сегодня|вчера|завтра|утром|вечером|ночью|днём)\b", s, re.IGNORECASE):
        sc += 1
    if len(s) <= 160:
        sc += 1
    return sc

def paraphrase_ru(text: str) -> str:
    """OFFLINE переформулировка: без ИИ, просто расширенные замены + перестановка фактов."""
    t = strip_noise(text)
    if not t:
        return ""

    # расширенный словарь замен
    mapping = {
        "стало известно": "появилась информация",
        "сообщается": "по данным на сейчас",
        "в настоящее время": "сейчас",
        "на данный момент": "сейчас",
        "по предварительным данным": "предварительно",
        "в ближайшее время": "в скором времени",
        "проводится проверка": "идёт проверка",
        "произошло": "случилось",
        "обнаружили": "нашли",
        "задержали": "взяли",
        "пострадали": "получили травмы",
        "не уточняется": "деталей пока нет",
        "по словам очевидцев": "со слов очевидцев",
        "в результате": "итогом стало",
        "из-за": "по причине",
        "на месте": "на точке",
        "спасатели": "службы спасения",
        "полиция": "правоохранители",
        "ГИБДД": "дорожная полиция",
        "ДТП": "авария",
    }
    for a, b in mapping.items():
        t = re.sub(rf"\b{re.escape(a)}\b", b, t, flags=re.IGNORECASE)

    sents = sentence_split(t)
    if not sents:
        return t

    ranked = sorted(sents, key=score_sentence, reverse=True)
    chosen = []
    used = set()

    for s in ranked:
        k = re.sub(r"\W+", "", s.lower())
        if k in used:
            continue
        used.add(k)
        chosen.append(s)
        if len(chosen) >= 3:
            break

    if len(chosen) >= 2:
        chosen[0], chosen[1] = chosen[1], chosen[0]

    out = " ".join(chosen).strip()
    if out and not out.lower().startswith(("коротко", "обновление")):
        out = f"Коротко: {out}"

    return _norm(out)

def text_hash(text: str) -> str:
    n = strip_noise(text).lower()
    n = re.sub(r"\s+", " ", n).strip()
    n = re.sub(r"[^\w\d]+", "", n)
    return sha1(n) if n else ""

# ------------------- main logic -------------------
async def main():
    if API_ID <= 0 or not API_HASH:
        raise RuntimeError("Нет API_ID / API_HASH (добавь в GitHub Secrets)")
    if not SESSION_STRING:
        raise RuntimeError("Нет SESSION_STRING (добавь в GitHub Secrets)")
    if not DESTINATION:
        raise RuntimeError("Нет DESTINATION (добавь в GitHub Secrets)")
    if not SOURCES:
        raise RuntimeError("Нет SOURCES (добавь в GitHub Secrets)")

    state = load_state()
    cleanup_seen_text(state)

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("SESSION_STRING не авторизован. Сгенерируй заново.")

    # 1) На первом запуске: запоминаем текущие last_id по каждому источнику и НЕ публикуем историю
    # (Это убирает проблему “отправил все старые посты”)
    for src in SOURCES:
        try:
            entity = await client.get_entity(src)
            chat_id = str(entity.id)
            if chat_id not in state["last_id"]:
                last = await client.get_messages(entity, limit=1)
                if last and last[0]:
                    state["last_id"][chat_id] = last[0].id
                else:
                    state["last_id"][chat_id] = 0
        except Exception as e:
            print(f"❌ Не могу открыть источник {src}: {e}")

    save_state(state)

    send_lock = asyncio.Lock()
    stop_at = datetime.now(timezone.utc) + timedelta(minutes=RUN_MINUTES)

    async def paced_sleep():
        if INTERVAL_SECONDS > 0:
            await asyncio.sleep(INTERVAL_SECONDS)

    async def upload_and_send_media(dest: str, msgs: List, caption: Optional[str]):
        tmpdir = tempfile.mkdtemp(prefix="tgmedia_")
        paths = []
        try:
            for m in msgs:
                if not getattr(m, "media", None):
                    continue
                p = await client.download_media(m, file=tmpdir)
                if p:
                    paths.append(p)
            if not paths:
                return
            await client.send_file(dest, files=paths, caption=caption if caption else None)
        finally:
            # чистим временные файлы
            for p in paths:
                try:
                    os.remove(p)
                except Exception:
                    pass
            try:
                os.rmdir(tmpdir)
            except Exception:
                pass

    def is_dup_text(state_: Dict, original: str) -> bool:
        if not DEDUP_TEXT:
            return False
        h = text_hash(original)
        if not h:
            return False
        return h in state_.get("seen_text", {})

    def mark_text(state_: Dict, original: str):
        if not DEDUP_TEXT:
            return
        h = text_hash(original)
        if h:
            state_.setdefault("seen_text", {})[h] = now_ts()

    def get_last_id(state_: Dict, chat_id: int) -> int:
        return int(state_.get("last_id", {}).get(str(chat_id), 0))

    def set_last_id(state_: Dict, chat_id: int, msg_id: int):
        state_.setdefault("last_id", {})[str(chat_id)] = max(get_last_id(state_, chat_id), msg_id)

    @client.on(events.Album(chats=SOURCES))
    async def on_album(event):
        if datetime.now(timezone.utc) >= stop_at:
            return

        msgs = list(event.messages or [])
        if not msgs:
            return

        chat_id = event.chat_id
        last_id = get_last_id(state, chat_id)
        max_id = max(m.id for m in msgs)

        # пропускаем всё старое
        if max_id <= last_id:
            return

        caption_src = (msgs[0].raw_text or "")
        if is_dup_text(state, caption_src):
            set_last_id(state, chat_id, max_id)
            return

        caption_new = paraphrase_ru(caption_src)

        try:
            async with send_lock:
                await upload_and_send_media(DESTINATION, msgs, caption_new)
                set_last_id(state, chat_id, max_id)
                mark_text(state, caption_src)
                save_state(state)
                await paced_sleep()
                print(f"✅ Альбом отправлен (chat={chat_id}, up_to={max_id})")
        except FloodWaitError as e:
            await asyncio.sleep(int(getattr(e, "seconds", 60)))
        except Exception as e:
            print("❌ Ошибка альбома:", e)

    @client.on(events.NewMessage(chats=SOURCES))
    async def on_message(event):
        if datetime.now(timezone.utc) >= stop_at:
            return

        # альбомы обрабатывает on_album
        if getattr(event.message, "grouped_id", None):
            return

        chat_id = event.chat_id
        msg_id = event.id
        last_id = get_last_id(state, chat_id)

        if msg_id <= last_id:
            return

        original_text = event.raw_text or ""
        if is_dup_text(state, original_text):
            set_last_id(state, chat_id, msg_id)
            return

        new_text = paraphrase_ru(original_text)

        try:
            async with send_lock:
                if getattr(event.message, "media", None):
                    await upload_and_send_media(DESTINATION, [event.message], new_text if new_text else None)
                else:
                    if new_text:
                        await client.send_message(DESTINATION, new_text)

                set_last_id(state, chat_id, msg_id)
                mark_text(state, original_text)
                save_state(state)
                await paced_sleep()
                print(f"✅ Сообщение отправлено (chat={chat_id}, id={msg_id})")

        except FloodWaitError as e:
            await asyncio.sleep(int(getattr(e, "seconds", 60)))
        except Exception as e:
            print("❌ Ошибка сообщения:", e)

    print("✅ Запуск. Историю НЕ шлём (first-run skip). Публикуем только новое.")
    try:
        while datetime.now(timezone.utc) < stop_at:
            await asyncio.sleep(5)
        print("⏹ Время RUN_MINUTES вышло, отключаемся.")
    except AuthKeyDuplicatedError:
        raise RuntimeError(
            "AuthKeyDuplicatedError: этот SESSION_STRING запущен где-то ещё. "
            "Останови другие запуски и оставь только один."
        )
    finally:
        save_state(state)
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())


