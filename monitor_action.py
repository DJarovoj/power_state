import asyncio
import hashlib
import hmac
import json
import os
import time
from datetime import datetime
import requests
from telegram import Bot

# --- ЗМІННІ ОТОЧЕННЯ ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

TUYA_CLIENT_ID = os.environ["TUYA_CLIENT_ID"]
TUYA_CLIENT_SECRET = os.environ["TUYA_CLIENT_SECRET"]
TUYA_DEVICE_ID = os.environ["TUYA_DEVICE_ID"]
TUYA_BASE_URL = os.environ.get("TUYA_BASE_URL", "https://openapi.tuyaeu.com")

GIST_ID = os.environ["GIST_ID"]
GIST_TOKEN = os.environ["GIST_TOKEN"]
GIST_FILENAME = "power_state.json"

CHECK_INTERVAL = 30       # секунд між перевірками всередині запуску
RUNS_PER_JOB = 8          # 8 * 30 сек = 4 хв
OFFLINE_THRESHOLD = 2     # перевірок офлайн поспіль для фіксації вимкнення
MAX_HISTORY = 10

tuya_token_cache = {"token": None, "obtained_at": 0}


def log(msg: str) -> None:
    now_str = datetime.now().strftime("%H:%M:%S")
    print(f"[{now_str}] {msg}", flush=True)


def _sign(access_id: str, secret: str, t: str, method: str, path: str, body: str = "", token: str = "") -> str:
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    string_to_sign = f"{method}\n{content_hash}\n\n{path}"
    sign_str = f"{access_id}{token}{t}{string_to_sign}"
    return hmac.new(secret.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha256).hexdigest().upper()


def get_tuya_token() -> str | None:
    now = time.time()
    if tuya_token_cache["token"] and (now - tuya_token_cache["obtained_at"] < 3000):
        return tuya_token_cache["token"]

    t = str(int(now * 1000))
    path = "/v1.0/token?grant_type=1"
    sign = _sign(TUYA_CLIENT_ID, TUYA_CLIENT_SECRET, t, "GET", path)
    headers = {
        "client_id": TUYA_CLIENT_ID,
        "sign": sign,
        "t": t,
        "sign_method": "HMAC-SHA256",
    }
    try:
        resp = requests.get(f"{TUYA_BASE_URL}{path}", headers=headers, timeout=10)
        data = resp.json()
        if data.get("success"):
            token = data["result"]["access_token"]
            tuya_token_cache["token"] = token
            tuya_token_cache["obtained_at"] = now
            log("Tuya токен успішно отримано.")
            return token
        log(f"Помилка токена Tuya (HTTP {resp.status_code}): {data}")
    except Exception as e:
        log(f"Виняток при отриманні токена Tuya: {e}")
    return None


def is_plug_online() -> bool | None:
    token = get_tuya_token()
    if not token:
        return None

    t = str(int(time.time() * 1000))
    path = f"/v1.0/devices/{TUYA_DEVICE_ID}"
    sign = _sign(TUYA_CLIENT_ID, TUYA_CLIENT_SECRET, t, "GET", path, token=token)
    headers = {
        "client_id": TUYA_CLIENT_ID,
        "access_token": token,
        "sign": sign,
        "t": t,
        "sign_method": "HMAC-SHA256",
    }
    try:
        resp = requests.get(f"{TUYA_BASE_URL}{path}", headers=headers, timeout=10)
        data = resp.json()
        if data.get("success") and "result" in data:
            online = bool(data["result"].get("online", False))
            log(f"Стан розетки в Tuya: online={online}")
            return online
        log(f"Помилка запиту пристрою Tuya (HTTP {resp.status_code}): {data}")
    except Exception as e:
        log(f"Виняток при запиті пристрою Tuya: {e}")
    return None


def format_duration(seconds: int) -> str:
    hours, rem = divmod(max(0, int(seconds)), 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours} год")
    if minutes:
        parts.append(f"{minutes} хв")
    if secs or not parts:
        parts.append(f"{secs} сек")
    return " ".join(parts)


def format_ts(ts: int | None) -> str:
    if not ts:
        return "невідомо"
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M:%S")


def load_state_from_gist() -> dict:
    log(f"Завантажуємо стан із Gist {GIST_ID} (файл {GIST_FILENAME})...")
    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    files = resp.json().get("files", {})
    if GIST_FILENAME not in files:
        log(f"Увага: файл {GIST_FILENAME} не знайдено в Gist. Доступні файли: {list(files.keys())}")
    content = files.get(GIST_FILENAME, {}).get("content", "")
    if content.strip():
        data = json.loads(content)
        if isinstance(data, dict) and "has_power" in data:
            data.setdefault("history", [])
            data.setdefault("offline_streak", 0)
            log(f"Завантажено стан: has_power={data.get('has_power')}, offline_streak={data.get('offline_streak')}")
            return data

    log("Початковий стан порожній — буде ініціалізовано при першій перевірці.")
    return {
        "has_power": None,
        "changed_at": None,
        "offline_streak": 0,
        "history": [],
    }


def save_state_to_gist(state: dict) -> None:
    log("Зберігаємо оновлений стан у Gist...")
    url = f"https://api.github.com/gists/{GIST_ID}"
    headers = {
        "Authorization": f"Bearer {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "files": {
            GIST_FILENAME: {
                "content": json.dumps(state, ensure_ascii=False, indent=2)
            }
        }
    }
    resp = requests.patch(url, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()
    log("Стан успішно збережено в Gist.")


async def send_tg(bot: Bot, text: str) -> None:
    log("Надсилаємо повідомлення в Telegram...")
    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")
    log("Повідомлення в Telegram надіслано.")


async def run_monitor_wave() -> None:
    log(f"Старт хвилі моніторингу ({RUNS_PER_JOB} ітерацій по {CHECK_INTERVAL} сек).")
    bot = Bot(token=BOT_TOKEN)
    state = load_state_from_gist()
    dirty = False

    for i in range(RUNS_PER_JOB):
        log(f"Ітерація {i + 1}/{RUNS_PER_JOB}")
        online = is_plug_online()
        if online is not None:
            now_ts = int(time.time())
            now_str = datetime.fromtimestamp(now_ts).strftime("%d.%m.%Y %H:%M:%S")
            prev_power = state.get("has_power")

            if prev_power is None:
                state["has_power"] = online
                state["changed_at"] = now_ts
                state["offline_streak"] = 0 if online else OFFLINE_THRESHOLD
                dirty = True
                status_text = "є ✅" if online else "немає ❌"
                await send_tg(
                    bot,
                    f"🤖 Моніторинг запущено ({now_str}).\nСвітло зараз: <b>{status_text}</b>",
                )

            elif online:
                if state.get("offline_streak", 0) > 0:
                    dirty = True
                state["offline_streak"] = 0
                if prev_power is False:
                    went_off_at = state.get("changed_at")
                    duration_sec = (now_ts - went_off_at) if went_off_at else 0
                    duration_str = format_duration(duration_sec) if went_off_at else "невідомо"

                    state["has_power"] = True
                    state["changed_at"] = now_ts
                    state["history"].append({
                        "off_at": went_off_at,
                        "on_at": now_ts,
                        "duration_sec": duration_sec,
                    })
                    state["history"] = state["history"][-MAX_HISTORY:]
                    dirty = True

                    await send_tg(
                        bot,
                        f"✅ <b>З'явилося світло</b>\n"
                        f"🕒 Час: {now_str}\n"
                        f"⏳ Його не було: <b>{duration_str}</b> (з {format_ts(went_off_at)})",
                    )

            else:
                state["offline_streak"] = state.get("offline_streak", 0) + 1
                dirty = True
                log(f"Розетка офлайн, серія невдалих перевірок: {state['offline_streak']}/{OFFLINE_THRESHOLD}")
                if prev_power is True and state["offline_streak"] >= OFFLINE_THRESHOLD:
                    actual_off_ts = now_ts - (OFFLINE_THRESHOLD - 1) * CHECK_INTERVAL
                    actual_off_str = datetime.fromtimestamp(actual_off_ts).strftime("%d.%m.%Y %H:%M:%S")

                    state["has_power"] = False
                    state["changed_at"] = actual_off_ts

                    await send_tg(
                        bot,
                        f"❌ <b>Зникло світло</b>\n🕒 Час: {actual_off_str}",
                    )
        else:
            log("Перевірку пропущено через помилку Tuya API.")

        if i < RUNS_PER_JOB - 1:
            await asyncio.sleep(CHECK_INTERVAL)

    if dirty:
        save_state_to_gist(state)
    else:
        log("Змін стану не було, запис у Gist не потрібен.")

    log("Хвилю моніторингу завершено.")


if __name__ == "__main__":
    asyncio.run(run_monitor_wave())
