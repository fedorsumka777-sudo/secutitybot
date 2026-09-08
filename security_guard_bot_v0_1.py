import os
import json
import logging
from pathlib import Path

import requests
from flask import Flask, request, jsonify

# =========================================================
# SECURITY GUARD BOT v0.2
# Тестова версія БЕЗ PostgreSQL.
#
# ВАЖЛИВО:
# Дані зберігаються у локальному JSON-файлі Render.
# На Free Web Service це НЕ є надійним постійним сховищем:
# після redeploy/restart дані можуть бути втрачені.
# Версія призначена лише для запуску і тестування логіки.
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
WEBHOOK_PATH = "/telegram-webhook"

if not BOT_TOKEN:
    raise RuntimeError("Не задано BOT_TOKEN")
if not ADMIN_ID:
    raise RuntimeError("Не задано ADMIN_ID")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "security_guard_test_data.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("security_guard_bot")

app = Flask(__name__)

# =========================================================
# TEMP STORAGE
# =========================================================

DEFAULT_DATA = {
    "users": {},
    "access_requests": {},
    "states": {},
    "training_attempts": {},
    "next_request_id": 1
}

def load_data():
    if not DATA_FILE.exists():
        save_data(DEFAULT_DATA.copy())
        return DEFAULT_DATA.copy()

    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Не вдалося прочитати локальне сховище")
        return DEFAULT_DATA.copy()

def save_data(data):
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def get_user(tg_id):
    data = load_data()
    return data["users"].get(str(tg_id))

def upsert_user_basic(
    tg_id,
    username=None,
    full_name=None,
    position=None,
    role=None,
    status=None
):
    data = load_data()
    key = str(tg_id)
    user = data["users"].get(key, {
        "telegram_id": tg_id,
        "username": None,
        "full_name": None,
        "position": None,
        "role": None,
        "status": "pending"
    })

    if username is not None:
        user["username"] = username
    if full_name is not None:
        user["full_name"] = full_name
    if position is not None:
        user["position"] = position
    if role is not None:
        user["role"] = role
    if status is not None:
        user["status"] = status

    data["users"][key] = user
    save_data(data)

def set_state(tg_id, state, payload=None):
    data = load_data()
    data["states"][str(tg_id)] = {
        "state": state,
        "payload": payload or {}
    }
    save_data(data)

def get_state(tg_id):
    data = load_data()
    item = data["states"].get(str(tg_id))
    if not item:
        return None, {}
    return item.get("state"), item.get("payload", {})

def clear_state(tg_id):
    data = load_data()
    data["states"].pop(str(tg_id), None)
    save_data(data)

def create_access_request(tg_id, username, full_name, position):
    data = load_data()
    req_id = int(data.get("next_request_id", 1))
    data["next_request_id"] = req_id + 1

    data["access_requests"][str(req_id)] = {
        "id": req_id,
        "telegram_id": tg_id,
        "username": username,
        "full_name": full_name,
        "position": position,
        "status": "new"
    }
    save_data(data)
    return req_id

def get_access_request(req_id):
    data = load_data()
    return data["access_requests"].get(str(req_id))

def update_access_request(req_id, status):
    data = load_data()
    item = data["access_requests"].get(str(req_id))
    if item:
        item["status"] = status
        data["access_requests"][str(req_id)] = item
        save_data(data)

# =========================================================
# TELEGRAM
# =========================================================

def tg(method, payload=None):
    try:
        r = requests.post(
            f"{API}/{method}",
            json=payload or {},
            timeout=20
        )
        if not r.ok:
            log.error("Telegram %s error: %s", method, r.text)
        return r.json()
    except Exception:
        log.exception("Telegram request failed: %s", method)
        return {"ok": False}

def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("sendMessage", payload)

def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    return tg("answerCallbackQuery", payload)

def main_menu(role):
    rows = [
        [{"text": "🟢 Заступити на зміну"}],
        [{"text": "🚶 Обхід"}, {"text": "🔎 Знайти об'єкт"}],
        [{"text": "⚠️ Повідомити про недолік"}],
        [{"text": "🚨 Подія / порушення"}],
        [{"text": "🎓 Навчання"}],
        [{"text": "📋 Моє чергування"}, {"text": "📚 Інструкції"}],
    ]

    if role == "senior_guard":
        rows.insert(
            1,
            [{"text": "👮 Склад зміни"}, {"text": "✅ Контроль обходів"}]
        )

    return {
        "keyboard": rows,
        "resize_keyboard": True
    }

def admin_menu():
    return {
        "keyboard": [
            [{"text": "👥 Заявки на доступ"}],
            [{"text": "👮 Працівники"}],
            [{"text": "🎓 Навчання — результати"}],
            [{"text": "📊 Стан системи"}],
        ],
        "resize_keyboard": True
    }

def unauthorized_menu():
    return {
        "keyboard": [
            [{"text": "🔐 Подати заявку на доступ"}]
        ],
        "resize_keyboard": True
    }

def training_menu():
    return {
        "keyboard": [
            [{"text": "📝 Первинна оцінка знань"}],
            [{"text": "📚 Навчальні матеріали"}],
            [{"text": "🧠 Мої результати"}],
            [{"text": "⬅️ Головне меню"}],
        ],
        "resize_keyboard": True
    }

# =========================================================
# ACCESS
# =========================================================

def start_access_request(tg_id):
    set_state(tg_id, "wait_full_name", {})
    send_message(
        tg_id,
        "🔐 <b>Заявка на доступ</b>\n\n"
        "Введи своє <b>ПІБ повністю</b>."
    )

def save_access_request(tg_id, username, full_name, position):
    upsert_user_basic(
        tg_id,
        username=username,
        full_name=full_name,
        position=position,
        status="pending"
    )

    req_id = create_access_request(
        tg_id=tg_id,
        username=username,
        full_name=full_name,
        position=position
    )

    buttons = {
        "inline_keyboard": [
            [
                {
                    "text": "👨‍✈️ Старший охоронник",
                    "callback_data": f"approve:{req_id}:senior_guard"
                },
                {
                    "text": "🔥 Охоронник-пожежник",
                    "callback_data": f"approve:{req_id}:guard_firefighter"
                },
            ],
            [
                {
                    "text": "❌ Відхилити",
                    "callback_data": f"reject:{req_id}"
                }
            ]
        ]
    }

    send_message(
        ADMIN_ID,
        "🔐 <b>Нова заявка на доступ</b>\n\n"
        f"👤 {full_name}\n"
        f"💼 Посада: {position}\n"
        f"🆔 Telegram ID: <code>{tg_id}</code>\n"
        f"🔗 Username: @{username if username else 'немає'}",
        buttons
    )

def process_admin_callback(cq):
    if cq["from"]["id"] != ADMIN_ID:
        answer_callback(cq["id"], "Недостатньо прав")
        return

    data = cq.get("data", "")

    if data.startswith("approve:"):
        _, req_id, role = data.split(":", 2)
        req = get_access_request(req_id)

        if not req:
            answer_callback(cq["id"], "Заявку не знайдено")
            return

        tg_id = req["telegram_id"]
        full_name = req["full_name"]

        update_access_request(req_id, "approved")
        upsert_user_basic(
            tg_id,
            role=role,
            status="active"
        )

        role_name = (
            "Старший охоронник"
            if role == "senior_guard"
            else "Охоронник-пожежник"
        )

        send_message(
            tg_id,
            "✅ <b>Доступ надано</b>\n\n"
            f"👤 {full_name}\n"
            f"🛡 Роль: <b>{role_name}</b>\n\n"
            "Можеш користуватися службовим ботом.",
            main_menu(role)
        )

        answer_callback(cq["id"], "Доступ надано")
        return

    if data.startswith("reject:"):
        _, req_id = data.split(":", 1)
        req = get_access_request(req_id)

        if not req:
            answer_callback(cq["id"], "Заявку не знайдено")
            return

        tg_id = req["telegram_id"]

        update_access_request(req_id, "rejected")
        upsert_user_basic(
            tg_id,
            status="rejected"
        )

        send_message(
            tg_id,
            "❌ Заявку на доступ відхилено.\n"
            "Звернися до відповідальної особи."
        )

        answer_callback(cq["id"], "Заявку відхилено")

# =========================================================
# HANDLERS
# =========================================================

def handle_start(msg):
    tg_id = msg["from"]["id"]
    username = msg["from"].get("username")

    if tg_id == ADMIN_ID:
        upsert_user_basic(
            tg_id,
            username=username,
            full_name="Адміністратор",
            role="admin",
            status="active"
        )

        send_message(
            tg_id,
            "🛡 <b>Службовий бот охорони</b>\n\n"
            "Режим адміністратора.\n\n"
            "🧪 Працює тестова версія без PostgreSQL.",
            admin_menu()
        )
        return

    user = get_user(tg_id)

    if not user or user.get("status") != "active":
        send_message(
            tg_id,
            "🛡 <b>Службовий бот охорони</b>\n\n"
            "Доступ до системи закритий.\n"
            "Для роботи потрібно подати заявку.",
            unauthorized_menu()
        )
        return

    role_text = {
        "senior_guard": "Старший охоронник",
        "guard_firefighter": "Охоронник-пожежник"
    }.get(user.get("role"), user.get("role") or "Працівник")

    send_message(
        tg_id,
        f"🛡 <b>Службовий бот охорони</b>\n\n"
        f"👤 {user.get('full_name') or ''}\n"
        f"💼 {role_text}",
        main_menu(user.get("role"))
    )

def handle_text(msg):
    tg_id = msg["from"]["id"]
    username = msg["from"].get("username")
    text = (msg.get("text") or "").strip()

    if text == "/start":
        handle_start(msg)
        return

    # ADMIN
    if tg_id == ADMIN_ID:
        if text == "📊 Стан системи":
            data = load_data()
            active_users = sum(
                1 for u in data["users"].values()
                if u.get("status") == "active"
            )
            new_requests = sum(
                1 for r in data["access_requests"].values()
                if r.get("status") == "new"
            )

            send_message(
                tg_id,
                "✅ <b>Бот працює</b>\n"
                "🧪 Сховище: локальний JSON\n"
                "⚠️ PostgreSQL поки не підключено\n\n"
                f"👥 Активних користувачів: {active_users}\n"
                f"🔐 Нових заявок: {new_requests}",
                admin_menu()
            )
            return

        if text == "👥 Заявки на доступ":
            data = load_data()
            rows = [
                r for r in data["access_requests"].values()
                if r.get("status") == "new"
            ]

            if not rows:
                send_message(
                    tg_id,
                    "✅ Нових заявок немає.",
                    admin_menu()
                )
            else:
                body = "\n\n".join(
                    f"#{r['id']} — {r['full_name']}\n"
                    f"💼 {r['position']}\n"
                    f"🆔 {r['telegram_id']}"
                    for r in rows[:20]
                )
                send_message(
                    tg_id,
                    "🔐 <b>Нові заявки</b>\n\n" + body,
                    admin_menu()
                )
            return

        if text == "👮 Працівники":
            data = load_data()
            rows = [
                u for u in data["users"].values()
                if u.get("status") == "active"
                and u.get("telegram_id") != ADMIN_ID
            ]

            if not rows:
                send_message(
                    tg_id,
                    "👥 Активних працівників поки немає.",
                    admin_menu()
                )
            else:
                role_names = {
                    "senior_guard": "Старший охоронник",
                    "guard_firefighter": "Охоронник-пожежник"
                }
                body = "\n\n".join(
                    f"👤 {u.get('full_name') or 'Без ПІБ'}\n"
                    f"💼 {role_names.get(u.get('role'), u.get('role') or '—')}\n"
                    f"🆔 {u.get('telegram_id')}"
                    for u in rows
                )
                send_message(
                    tg_id,
                    "👮 <b>Працівники</b>\n\n" + body,
                    admin_menu()
                )
            return

        if text == "🎓 Навчання — результати":
            send_message(
                tg_id,
                "🟡 Підключимо після запуску базової версії.",
                admin_menu()
            )
            return

    # USER STATE
    state, payload = get_state(tg_id)

    if state == "wait_full_name":
        if len(text) < 5:
            send_message(tg_id, "Введи ПІБ повністю.")
            return

        set_state(
            tg_id,
            "wait_position",
            {"full_name": text}
        )

        send_message(
            tg_id,
            "💼 Тепер введи свою <b>посаду</b>.\n\n"
            "Наприклад:\n"
            "Старший охоронник\n"
            "або\n"
            "Охоронник-пожежник"
        )
        return

    if state == "wait_position":
        full_name = payload.get("full_name", "")
        position = text

        save_access_request(
            tg_id=tg_id,
            username=username,
            full_name=full_name,
            position=position
        )

        clear_state(tg_id)

        send_message(
            tg_id,
            "✅ <b>Заявку відправлено.</b>\n\n"
            "Очікуй погодження адміністратора.",
            unauthorized_menu()
        )
        return

    user = get_user(tg_id)

    if not user or user.get("status") != "active":
        if text == "🔐 Подати заявку на доступ":
            start_access_request(tg_id)
        else:
            send_message(
                tg_id,
                "🔐 Доступ не надано.",
                unauthorized_menu()
            )
        return

    role = user.get("role")

    # TRAINING
    if text == "🎓 Навчання":
        send_message(
            tg_id,
            "🎓 <b>Навчання та перевірка знань</b>\n\n"
            "Першим підключимо первинну оцінку знань.",
            training_menu()
        )
        return

    if text == "📝 Первинна оцінка знань":
        send_message(
            tg_id,
            "🟡 Модуль тестування буде наступною версією.\n\n"
            "Після запуску v0.2 додамо банк запитань, "
            "результат у %, слабкі теми та повторне навчання.",
            training_menu()
        )
        return

    if text == "📚 Навчальні матеріали":
        send_message(
            tg_id,
            "📚 Навчальні матеріали додамо після "
            "формування першого тематичного плану.",
            training_menu()
        )
        return

    if text == "🧠 Мої результати":
        send_message(
            tg_id,
            "📭 Завершених тестувань поки немає.",
            training_menu()
        )
        return

    if text == "⬅️ Головне меню":
        send_message(
            tg_id,
            "🏠 Головне меню",
            main_menu(role)
        )
        return

    placeholders = {
        "🟢 Заступити на зміну":
            "🟡 Модуль зміни буде після навчального блоку.",
        "🚶 Обхід":
            "🟡 Погодинні обходи підключимо окремим етапом.",
        "🔎 Знайти об'єкт":
            "🟡 Довідник Табеля постів підключимо після PostgreSQL.",
        "⚠️ Повідомити про недолік":
            "🟡 Реєстр недоліків буде окремим модулем.",
        "🚨 Подія / порушення":
            "🟡 Журнал подій та технічних тривог буде додано.",
        "📋 Моє чергування":
            "🟡 Історія чергувань буде додана.",
        "📚 Інструкції":
            "🟡 Службові документи підключимо окремо.",
        "👮 Склад зміни":
            "🟡 Склад зміни буде додано.",
        "✅ Контроль обходів":
            "🟡 Контроль обходів буде додано."
    }

    if text in placeholders:
        send_message(
            tg_id,
            placeholders[text],
            main_menu(role)
        )
        return

    send_message(
        tg_id,
        "Оберіть дію з меню.",
        main_menu(role)
    )

# =========================================================
# WEBHOOK
# =========================================================

@app.get("/")
def health():
    return jsonify({
        "ok": True,
        "service": "security_guard_bot",
        "version": "0.2",
        "storage": "local_json_test_only"
    })

@app.post(WEBHOOK_PATH)
def telegram_webhook():
    update = request.get_json(silent=True) or {}

    try:
        if "callback_query" in update:
            process_admin_callback(update["callback_query"])

        elif "message" in update:
            msg = update["message"]
            if "text" in msg:
                handle_text(msg)

    except Exception:
        log.exception("Webhook processing error")

    return jsonify({"ok": True})

def set_webhook():
    if not PUBLIC_URL:
        log.warning(
            "PUBLIC_URL не задано — webhook автоматично не встановлено"
        )
        return

    webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    result = tg(
        "setWebhook",
        {"url": webhook_url}
    )
    log.info("setWebhook: %s", result)

if __name__ == "__main__":
    set_webhook()

    port = int(os.getenv("PORT", "10000"))
    log.info(
        "Starting Security Guard Bot v0.2 on port %s",
        port
    )
    app.run(
        host="0.0.0.0",
        port=port
    )
