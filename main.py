import os
import json
import re
import asyncio
from typing import Dict, List

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

load_dotenv()

# --- берём настройки из переменных окружения (на GitHub они будут в Secrets) ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

DESTINATION = os.getenv("DESTINATION", "").strip()  # например: @my_channel
SOURCES_RAW = os.getenv("SOURCES", "").strip()      # например: @src1,@src2

# сколько сообщений за один запуск максимум смотреть у каждого источника
FETCH_LIMIT = int(os.getenv("FETCH_LIMIT", "10"))
# пауза между отправками (чтобы не словить ограничения)
SLEEP_BETWEEN_SEND = int(os.getenv("SLEEP_BETWEEN_SEND", "2"))

# Добавлять строку "Источник: ..." (лучше включить, чтобы не было проблем по правам)
ADD_SOURCE = os.getenv("ADD_SOURCE", "1").strip() == "1"

STATE_FILE = "state.json"

BAD_LINE = re.compile(r"(подпис|подпиш|реклама|конкурс|розыгрыш|скидк)", re.IGNORECASE)
LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/\S+", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

def parse_sources(s: str) -> List[str]:
    return [x.strip() for x in s.split(",") if x.strip()]

SOURCES = parse_sources(SOURCES_RAW)

def clean_text(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    t = LINK_RE.sub("", t)
    t = URL_RE.sub("", t)

    # убираем строки с "подпишись/реклама" и т.п.
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not BAD_LINE.search(ln)]
    t = " ".join(lines)

    t = re.sub(r"\s{2,}", " ", t).strip()
    return t

def load_state() -> Dict[str, int]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: int(v) for k, v in data.items()}
    except Exception:
        pass
    return {}

def save_state(state: Dict[str, int]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

async def main():
    if API_ID <= 0 or not API_HASH:
        raise RuntimeError("Нет API_ID / API_HASH. Добавь их в GitHub Secrets.")
    if not SESSION_STRING:
        raise RuntimeError("Нет SESSION_STRING. Добавь его в GitHub Secrets.")
    if not DESTINATION:
        raise RuntimeError("Нет DESTINATION (пример: @my_channel).")
    if not SOURCES:
        raise RuntimeError("Нет SOURCES (пример: @src1,@src2).")

    state = load_state()
    state_changed = False

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()

    print("✅ Запуск. Источники:", SOURCES, "→", DESTINATION)

    for src in SOURCES:
        last_id = int(state.get(src, 0))

        try:
            msgs = await client.get_messages(src, limit=FETCH_LIMIT)
            if not msgs:
                continue

            # берём только новые (id больше последнего)
            new_msgs = [m for m in msgs if m and m.id and m.id > last_id]
            new_msgs.sort(key=lambda m: m.id)  # отправляем от старого к новому

            for m in new_msgs:
                text = clean_text(m.text or "")
                if ADD_SOURCE:
                    text = (text + f"\n\nИсточник: {src}").strip()

                if m.media:
                    await client.send_file(
                        DESTINATION,
                        file=m,                 # перезаливает медиа (без "Forwarded from")
                        caption=text if text else None
                    )
                else:
                    if text:
                        await client.send_message(DESTINATION, text)

                last_id = max(last_id, m.id)
                await asyncio.sleep(SLEEP_BETWEEN_SEND)

            if last_id != int(state.get(src, 0)):
                state[src] = last_id
                state_changed = True

        except FloodWaitError as e:
            sec = int(getattr(e, "seconds", 60))
            print(f"⏳ FloodWait {sec}s на {src}")
            await asyncio.sleep(sec)
        except Exception as e:
            print("⚠️ Ошибка по источнику", src, ":", e)

    await client.disconnect()

    if state_changed:
        save_state(state)
        print("✅ state.json обновлён")
    else:
        print("✅ Новых постов нет")

if __name__ == "__main__":
    asyncio.run(main())
