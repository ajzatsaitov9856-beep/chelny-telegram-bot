# main.py — автопост из Telegram-каналов в ваш канал (через аккаунт, Telethon)
#
# ✅ Работает автономно в GitHub Actions (пуллинг + state.json)
# ✅ НЕ тащит историю: на первом запуске просто "запоминает" текущую точку и НЕ постит старое
# ✅ Не форвардит (нет "Forwarded from"), но ДОБАВЛЯЕТ "Источник: ..." (прозрачно)
# ✅ Перефразирование офлайн (простое, но расширенное) + чистка ссылок/хештегов/упоминаний
# ✅ Медиа сохраняется (скачивает и перезаливает)
# ✅ Антидубли по msg_id + по хэшу текста
#
# ENV (Secrets / .env):
#   API_ID=123456
#   API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
#   SESSION_STRING=1A... (длинная строка, копировать ВСЮ, включая '=' в конце если есть)
#   DESTINATION=@your_channel
#   SOURCES=@src1,@src2,@src3
#   INTERVAL_SECONDS=15
#   RUN_MINUTES=8
#
# requirements.txt:
#   telethon
#   python-dotenv

import os
import re
import json
import time
import asyncio
import hashlib
from typing import Dict, List, Optional, Tuple

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import Message

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


# -------------------- ENV --------------------
API_ID = int(os.getenv("API_ID", "0").strip() or "0")
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

DESTINATION = os.getenv("DESTINATION", "").strip()
SOURCES_RAW = os.getenv("SOURCES", "").strip()

INTERVAL_SECONDS = int(os.getenv("INTERVAL_SECONDS", "15").strip() or "15")
RUN_MINUTES = int(os.getenv("RUN_MINUTES", "8").strip() or "8")

MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "3500").strip() or "3500")
MAX_CAPTION_CHARS = int(os.getenv("MAX_CAPTION_CHARS", "900").strip() or "900")

STATE_FILE = os.getenv("STATE_FILE", "state.json").strip()
DEDUP_TEXT_TTL_HOURS = int(os.getenv("DEDUP_TEXT_TTL_HOURS", "72").strip() or "72")


def parse_list(s: str) -> List[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


SOURCES = parse_list(SOURCES_RAW)


# -------------------- STATE --------------------
def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"last_id": {}, "seen_text": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "last_id" not in data:
            data["last_id"] = {}
        if "seen_text" not in data:
            data["seen_text"] = {}
        return data
    except Exception:
        return {"last_id": {}, "seen_text": {}}


def save_state(state: Dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def cleanup_seen_text(state: Dict) -> None:
    cutoff = int(time.time()) - DEDUP_TEXT_TTL_HOURS * 3600
    seen = state.get("seen_text", {})
    if not isinstance(seen, dict):
        state["seen_text"] = {}
        return
    # seen_text: {hash: ts}
    to_del = [h for h, ts in seen.items() if isinstance(ts, int) and ts < cutoff]
    for h in to_del:
        seen.pop(h, None)
    state["seen_text"] = seen


# -------------------- TEXT (расширенная офлайн-перефразировка) --------------------
_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/\S+", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_AT_RE = re.compile(r"@\w+")
_HASH_RE = re.compile(r"#\w+")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_MANY_NL = re.compile(r"\n{3,}")
_BAD_LINE = re.compile(
    r"(подпис|подпиш|репост|реклама|конкурс|розыгрыш|promo|скидк|акци|переходи|переходите)",
    re.IGNORECASE,
)

PHRASE_MAP = [
    (r"\bстало известно\b", "появилась информация"),
    (r"\bсообщается\b", "по данным на сейчас"),
    (r"\bв настоящее время\b", "сейчас"),
    (r"\bна данный момент\b", "сейчас"),
    (r"\bпо предварительным данным\b", "предварительно"),
    (r"\bв ближайшее время\b", "в скором времени"),
    (r"\bпроводится проверка\b", "идёт проверка"),
    (r"\bпроизошло\b", "случилось"),
    (r"\bинформация уточняется\b", "детали уточняются"),
    (r"\bпо словам очевидцев\b", "по словам свидетелей"),
    (r"\bпо данным\b", "согласно данным"),
    (r"\bобратитесь\b", "можно обратиться"),
    (r"\bперекрыто движение\b", "движение временно перекрыто"),
    (r"\bограничено движение\b", "движение частично ограничено"),
    (r"\bвозобновили\b", "вновь запустили"),
    (r"\bоткрыли\b", "снова открыли"),
]

WORD_MAP = [
    (r"\bполиция\b", "правоохранители"),
    (r"\bМЧС\b", "службы спасения"),
    (r"\bавария\b", "ДТП"),
    (r"\bпожар\b", "возгорание"),
    (r"\bпострадал\b", "получил травмы"),
    (r"\bпострадали\b", "получили травмы"),
    (r"\bзадержали\b", "взяли под стражу"),
    (r"\bзадержан\b", "взят под стражу"),
    (r"\bперенесли\b", "сдвинули"),
    (r"\bотменили\b", "не будут проводить"),
]


def cleanup_text(text: str) -> str:
    t = (text or "").replace("\u200b", "").strip()
    t = _MANY_NL.sub("\n\n", t)
    t = _MULTI_SPACE.sub(" ", t)
    return t.strip()


def strip_ads_links_mentions(text: str) -> str:
    t = cleanup_text(text)
    if not t:
        return ""

    # убрать ссылки/упоминания/хештеги
    t = _LINK_RE.sub("", t)
    t = _URL_RE.sub("", t)
    t = _AT_RE.sub("", t)
    t = _HASH_RE.sub("", t)

    # убрать рекламные/призывные строки
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not _BAD_LINE.search(ln)]
    t = " ".join(lines)

    # норм пробелы возле пунктуации
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()


def apply_maps(t: str) -> str:
    for pat, rep in PHRASE_MAP:
        t = re.sub(pat, rep, t, flags=re.IGNORECASE)
    for pat, rep in WORD_MAP:
        t = re.sub(pat, rep, t, flags=re.IGNORECASE)
    return t


def sentence_split(t: str) -> List[str]:
    t = cleanup_text(t)
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
    if re.search(r"\b(сегодня|вчера|завтра|утром|вечером|ночью|днём)\b", s, re.IGNORECASE):
        score += 1
    if len(s) <= 170:
        score += 1
    return score


def text_hash(text: str) -> str:
    t = strip_ads_links_mentions(text).lower()
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"[^\w\d]+", "", t)
    if not t:
        return ""
    return hashlib.sha1(t.encode("utf-8")).hexdigest()


def free_rewrite_ru(text: str) -> str:
    """
    Бесплатная расширенная переработка (без ИИ):
    - чистит мусор (ссылки/@/#/реклама)
    - выбирает 2–4 самых фактовых предложения
    - меняет порядок + синонимы
    """
    base = strip_ads_links_mentions(text)
    if not base:
        return ""

    base = apply_maps(base)
    sents = sentence_split(base)
    if not sents:
        return ""

    ranked = sorted(sents, key=score_sentence, reverse=True)

    chosen: List[str] = []
    seen_keys = set()
    for s in ranked:
        k = re.sub(r"\W+", "", s.lower())
        if k in seen_keys:
            continue
        seen_keys.add(k)
        chosen.append(s)
        if len(chosen) >= 4:
            break

    # поменять порядок чуть-чуть
    if len(chosen) >= 2:
        chosen = [chosen[1], chosen[0]] + chosen[2:]

    out = " ".join(chosen).strip()

    # аккуратный лид
    if out and not out.lower().startswith(("коротко", "обновление", "важно")):
        out = f"Коротко: {out}"

    return cleanup_text(out)


def clamp(t: str, max_len: int) -> str:
    t = cleanup_text(t)
    if len(t) <= max_len:
        return t
    t = t[:max_len].rsplit(" ", 1)[0].rstrip()
    return t + "…"


# -------------------- TELEGRAM HELPERS --------------------
def chat_key(source: str) -> str:
    return source.strip()


async def get_public_link(client: TelegramClient, msg: Message) -> Optional[str]:
    """
    Пытаемся сделать публичную ссылку:
    https://t.me/<username>/<msg_id>
    Если username нет (приватный канал) — вернём None.
    """
    try:
        chat = await msg.get_chat()
        username = getattr(chat, "username", None)
        if username:
            return f"https://t.me/{username}/{msg.id}"
        return None
    except Exception:
        return None


async def build_caption(client: TelegramClient, msg: Message, rewritten: str) -> str:
    link = await get_public_link(client, msg)
    if link:
        src_line = f"Источник: {link}"
    else:
        # если приватный источник — хотя бы честная подпись
        try:
            chat = await msg.get_chat()
            title = getattr(chat, "title", None) or "источник"
        except Exception:
            title = "источник"
        src_line = f"Источник: {title}"

    if rewritten:
        return f"{rewritten}\n\n{src_line}".strip()
    return src_line


def is_media(msg: Message) -> bool:
    return bool(getattr(msg, "media", None))


# -------------------- ALBUM GROUPING --------------------
def group_by_album(messages: List[Message]) -> List[List[Message]]:
    """
    Группируем сообщения по grouped_id (альбомы).
    messages должны идти по возрастанию id (старые -> новые).
    """
    groups: List[List[Message]] = []
    cur: List[Message] = []
    cur_gid = None

    for m in messages:
        gid = getattr(m, "grouped_id", None)
        if gid is None:
            if cur:
                groups.append(cur)
                cur = []
                cur_gid = None
            groups.append([m])
            continue

        if cur_gid is None:
            cur = [m]
            cur_gid = gid
        elif gid == cur_gid:
            cur.append(m)
        else:
            groups.append(cur)
            cur = [m]
            cur_gid = gid

    if cur:
        groups.append(cur)

    return groups


# -------------------- MAIN LOOP (polling) --------------------
async def main() -> None:
    if API_ID <= 0 or not API_HASH:
        raise RuntimeError("Заполни API_ID и API_HASH (Secrets или .env).")
    if not SESSION_STRING:
        raise RuntimeError("SESSION_STRING пустой. Сгенерируй заново и вставь целиком (включая '=' в конце).")
    if not DESTINATION:
        raise RuntimeError("DESTINATION пустой (например @my_channel).")
    if not SOURCES:
        raise RuntimeError("SOURCES пустой (например @src1,@src2).")

    state = load_state()
    cleanup_seen_text(state)

    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("SESSION_STRING не авторизован. Сгенерируй SESSION_STRING заново.")

    # резолвим destination и sources один раз
    dest_entity = await client.get_entity(DESTINATION)
    src_entities = []
    for s in SOURCES:
        src_entities.append((s, await client.get_entity(s)))

    # первый запуск: "прогрев" — запомнить текущие last_id и НЕ отправлять историю
    warmed_up = False
    for s_key, ent in src_entities:
        k = chat_key(s_key)
        if str(k) not in state["last_id"]:
            last = await client.get_messages(ent, limit=1)
            if last:
                state["last_id"][str(k)] = int(last[0].id)
            else:
                state["last_id"][str(k)] = 0
            warmed_up = True

    if warmed_up:
        save_state(state)
        print("✅ Первый запуск: точки сохранены (старые посты НЕ отправлялись). Следующие запуски будут ловить только новое.")

    print("✅ Запуск: читаю новые посты и публикую с подписью источника. RUN_MINUTES=", RUN_MINUTES)

    started = time.time()
    while True:
        if (time.time() - started) > RUN_MINUTES * 60:
            break

        for s_key, ent in src_entities:
            k = str(chat_key(s_key))
            last_id = int(state["last_id"].get(k, 0))

            # берём новые сообщения (id > last_id), в правильном порядке (старые -> новые)
            new_msgs: List[Message] = []
            async for m in client.iter_messages(ent, min_id=last_id, reverse=True):
                if not isinstance(m, Message):
                    continue
                # пропускаем сервисные/пустые
                if not m.id:
                    continue
                new_msgs.append(m)
                if len(new_msgs) >= 50:
                    break

            if not new_msgs:
                continue

            groups = group_by_album(new_msgs)

            for grp in groups:
                # обновим last_id по факту
                max_id = max(m.id for m in grp if m.id)
                state["last_id"][k] = max(int(state["last_id"].get(k, 0)), int(max_id))

                # dedup по тексту
                raw_text = ""
                for m in grp:
                    if (m.raw_text or "").strip():
                        raw_text = m.raw_text
                        break

                h = text_hash(raw_text) if raw_text.strip() else ""
                seen_text: Dict[str, int] = state.get("seen_text", {})
                if h and h in seen_text:
                    continue

                rewritten = clamp(free_rewrite_ru(raw_text), MAX_TEXT_CHARS)
                # подпись-источник добавим всегда (честно)
                # для caption ограничим длину
                first_msg = grp[0]
                caption = await build_caption(client, first_msg, rewritten)
                caption = clamp(caption, MAX_CAPTION_CHARS)

                try:
                    # альбом (несколько медиа)
                    media_msgs = [m for m in grp if is_media(m)]
                    if len(media_msgs) >= 2:
                        await client.send_file(
                            dest_entity,
                            files=media_msgs,  # Telethon скачает и перезальёт
                            caption=caption
                        )
                    # одиночное медиа
                    elif len(media_msgs) == 1:
                        await client.send_file(
                            dest_entity,
                            file=media_msgs[0],
                            caption=caption
                        )
                    # текст
                    else:
                        if caption.strip():
                            await client.send_message(dest_entity, caption)

                    if h:
                        seen_text[h] = int(time.time())
                        state["seen_text"] = seen_text

                    save_state(state)
                    await asyncio.sleep(max(1, INTERVAL_SECONDS))

                except FloodWaitError as e:
                    wait_s = int(getattr(e, "seconds", 60))
                    await asyncio.sleep(wait_s)
                except Exception as e:
                    # не падаем полностью
                    print("Ошибка отправки:", repr(e))

        # пауза между кругами
        await asyncio.sleep(3)

    save_state(state)
    await client.disconnect()
    print("✅ Готово: цикл завершён, state.json сохранён.")


if __name__ == "__main__":
    asyncio.run(main())


