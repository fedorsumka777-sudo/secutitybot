import os
import time
import json
import logging
from datetime import datetime, timezone

import requests
import psycopg
from flask import Flask, request, jsonify

# =========================================================
# SECURITY GUARD BOT v0.1
# Окремий службовий Telegram-бот охорони
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
WEBHOOK_PATH = "/telegram-webhook"

if not BOT_TOKEN:
    raise RuntimeError("Не задано BOT_TOKEN")
if not DATABASE_URL:
    raise RuntimeError("Не задано DATABASE_URL")
if not ADMIN_ID:
    raise RuntimeError("Не задано ADMIN_ID")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("security_guard_bot")

app = Flask(__name__)

# =========================================================
# DB
# =========================================================

def db():
    return psycopg.connect(DATABASE_URL)

def init_db():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    position TEXT,
                    role TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    approved_at TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS access_requests (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    full_name TEXT,
                    position TEXT,
                    username TEXT,
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    processed_at TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_states (
                    telegram_id BIGINT PRIMARY KEY,
                    state TEXT,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS training_attempts (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    training_type TEXT NOT NULL,
                    score NUMERIC(5,2),
                    passed BOOLEAN,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at TIMESTAMPTZ
                )
            """)
        conn.commit()
    log.info("DB initialized")

def get_user(tg_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT telegram_id, username, full_name, position, role, status
                FROM users WHERE telegram_id=%s
            """, (tg_id,))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "telegram_id": row[0],
                "username": row[1],
                "full_name": row[2],
                "position": row[3],
                "role": row[4],
                "status": row[5],
            }

def upsert_user_basic(tg_id, username=None, full_name=None, position=None, role=None, status=None):
    existing = get_user(tg_id)
    if not existing:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users
                    (telegram_id, username, full_name, position, role, status)
                    VALUES (%s,%s,%s,%s,%s,%s)
                """, (
                    tg_id, username, full_name, position, role,
                    status or "pending"
                ))
            conn.commit()
        return

    fields, values = [], []
    for key, value in [
        ("username", username),
        ("full_name", full_name),
        ("position", position),
        ("role", role),
        ("status", status),
    ]:
        if value is not None:
            fields.append(f"{key}=%s")
            values.append(value)

    if fields:
        values.append(tg_id)
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE users SET {', '.join(fields)} WHERE telegram_id=%s",
                    values
                )
            conn.commit()

def set_state(tg_id, state, payload=None):
    payload = payload or {}
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_states (telegram_id, state, payload, updated_at)
                VALUES (%s,%s,%s::jsonb,NOW())
                ON CONFLICT (telegram_id)
                DO UPDATE SET state=EXCLUDED.state,
                              payload=EXCLUDED.payload,
                              updated_at=NOW()
            """, (tg_id, state, json.dumps(payload, ensure_ascii=False)))
        conn.commit()

def get_state(tg_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, payload FROM user_states WHERE telegram_id=%s",
                (tg_id,)
            )
            row = cur.fetchone()
            return row if row else (None, {})

def clear_state(tg_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_states WHERE telegram_id=%s", (tg_id,))
        conn.commit()

# =========================================================
# TELEGRAM
# =========================================================

def tg(method, payload=None):
    r = requests.post(
        f"{API}/{method}",
        json=payload or {},
        timeout=20
    )
    if not r.ok:
        log.error("Telegram %s error: %s", method, r.text)
    return r.json()

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
        rows.insert(1, [{"text": "👮 Склад зміни"}, {"text": "✅ Контроль обходів"}])

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
# ACCESS FLOW
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

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO access_requests
                (telegram_id, full_name, position, username, status)
                VALUES (%s,%s,%s,%s,'new')
                RETURNING id
            """, (tg_id, full_name, position, username))
            req_id = cur.fetchone()[0]
        conn.commit()

    buttons = {
        "inline_keyboard": [
            [
                {"text": "👨‍✈️ Старший охоронник", "callback_data": f"approve:{req_id}:senior_guard"},
                {"text": "🔥 Охоронник-пожежник", "callback_data": f"approve:{req_id}:guard_firefighter"},
            ],
            [
                {"text": "❌ Відхилити", "callback_data": f"reject:{req_id}"}
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

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT telegram_id, full_name
                    FROM access_requests
                    WHERE id=%s
                """, (req_id,))
                row = cur.fetchone()

                if not row:
                    answer_callback(cq["id"], "Заявку не знайдено")
                    return

                tg_id, full_name = row

                cur.execute("""
                    UPDATE access_requests
                    SET status='approved', processed_at=NOW()
                    WHERE id=%s
                """, (req_id,))
                cur.execute("""
                    UPDATE users
                    SET role=%s, status='active', approved_at=NOW()
                    WHERE telegram_id=%s
                """, (role, tg_id))
            conn.commit()

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

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT telegram_id
                    FROM access_requests
                    WHERE id=%s
                """, (req_id,))
                row = cur.fetchone()

                if not row:
                    answer_callback(cq["id"], "Заявку не знайдено")
                    return

                tg_id = row[0]

                cur.execute("""
                    UPDATE access_requests
                    SET status='rejected', processed_at=NOW()
                    WHERE id=%s
                """, (req_id,))
                cur.execute("""
                    UPDATE users
                    SET status='rejected'
                    WHERE telegram_id=%s
                """, (tg_id,))
            conn.commit()

        send_message(
            tg_id,
            "❌ Заявку на доступ відхилено.\n"
            "Звернися до відповідальної особи."
        )
        answer_callback(cq["id"], "Заявку відхилено")

# =========================================================
# MESSAGES
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
            "Режим адміністратора.",
            admin_menu()
        )
        return

    user = get_user(tg_id)

    if not user or user["status"] != "active":
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
    }.get(user["role"], user["role"] or "Працівник")

    send_message(
        tg_id,
        f"🛡 <b>Службовий бот охорони</b>\n\n"
        f"👤 {user['full_name'] or ''}\n"
        f"💼 {role_text}",
        main_menu(user["role"])
    )

def handle_text(msg):
    tg_id = msg["from"]["id"]
    username = msg["from"].get("username")
    text = (msg.get("text") or "").strip()

    if text == "/start":
        handle_start(msg)
        return

    if tg_id == ADMIN_ID:
        if text == "📊 Стан системи":
            send_message(
                tg_id,
                "✅ Бот працює\n"
                "✅ PostgreSQL підключено\n"
                "✅ Система доступу активна\n"
                "🟡 Навчальний модуль — наступний етап",
                admin_menu()
            )
            return

        if text == "👥 Заявки на доступ":
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, full_name, position, telegram_id
                        FROM access_requests
                        WHERE status='new'
                        ORDER BY created_at
                        LIMIT 20
                    """)
                    rows = cur.fetchall()

            if not rows:
                send_message(tg_id, "✅ Нових заявок немає.", admin_menu())
            else:
                body = "\n\n".join(
                    f"#{r[0]} — {r[1]}\n💼 {r[2]}\n🆔 {r[3]}"
                    for r in rows
                )
                send_message(
                    tg_id,
                    "🔐 <b>Нові заявки</b>\n\n" + body,
                    admin_menu()
                )
            return

        if text in ("👮 Працівники", "🎓 Навчання — результати"):
            send_message(
                tg_id,
                "🟡 Цей розділ підключимо в наступній версії.",
                admin_menu()
            )
            return

    state, payload = get_state(tg_id)

    if state == "wait_full_name":
        if len(text) < 5:
            send_message(tg_id, "Введи ПІБ повністю.")
            return
        set_state(tg_id, "wait_position", {"full_name": text})
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

    if not user or user["status"] != "active":
        if text == "🔐 Подати заявку на доступ":
            start_access_request(tg_id)
        else:
            send_message(
                tg_id,
                "🔐 Доступ не надано.",
                unauthorized_menu()
            )
        return

    role = user["role"]

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
            "🟡 Бот уже готовий приймати модуль тестування.\n\n"
            "У наступній версії додамо банк запитань, "
            "підрахунок результату та повторне навчання.",
            training_menu()
        )
        return

    if text == "🧠 Мої результати":
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT training_type, score, passed, finished_at
                    FROM training_attempts
                    WHERE telegram_id=%s AND finished_at IS NOT NULL
                    ORDER BY finished_at DESC
                    LIMIT 5
                """, (tg_id,))
                rows = cur.fetchall()

        if not rows:
            send_message(
                tg_id,
                "📭 Завершених тестувань поки немає.",
                training_menu()
            )
        else:
            lines = []
            for tr_type, score, passed, finished_at in rows:
                lines.append(
                    f"{'✅' if passed else '🔄'} {tr_type}: {score}%"
                )
            send_message(
                tg_id,
                "🧠 <b>Мої результати</b>\n\n" + "\n".join(lines),
                training_menu()
            )
        return

    if text == "📚 Навчальні матеріали":
        send_message(
            tg_id,
            "📚 Матеріали додамо після формування "
            "першого тематичного плану.",
            training_menu()
        )
        return

    if text == "⬅️ Головне меню":
        send_message(tg_id, "🏠 Головне меню", main_menu(role))
        return

    placeholders = {
        "🟢 Заступити на зміну": "🟡 Модуль зміни буде наступним після навчання.",
        "🚶 Обхід": "🟡 Погодинні обходи підключимо окремим етапом.",
        "🔎 Знайти об'єкт": "🟡 Довідник Табеля постів буде імпортовано в PostgreSQL.",
        "⚠️ Повідомити про недолік": "🟡 Реєстр недоліків буде додано окремим модулем.",
        "🚨 Подія / порушення": "🟡 Журнал подій та технічних тривог буде додано.",
        "📋 Моє чергування": "🟡 Історія чергувань буде додана.",
        "📚 Інструкції": "🟡 Службові документи підключимо окремо.",
        "👮 Склад зміни": "🟡 Склад зміни буде додано.",
        "✅ Контроль обходів": "🟡 Контроль обходів буде додано.",
    }

    if text in placeholders:
        send_message(tg_id, placeholders[text], main_menu(role))
        return

    send_message(tg_id, "Оберіть дію з меню.", main_menu(role))

# =========================================================
# WEBHOOK
# =========================================================

@app.get("/")
def health():
    return jsonify({
        "ok": True,
        "service": "security_guard_bot",
        "version": "0.1"
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
        log.warning("PUBLIC_URL не задано — webhook автоматично не встановлено")
        return

    webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    result = tg("setWebhook", {"url": webhook_url})
    log.info("setWebhook: %s", result)

if __name__ == "__main__":
    init_db()
    set_webhook()

    port = int(os.getenv("PORT", "10000"))
    log.info("Starting Security Guard Bot v0.1 on port %s", port)
    app.run(host="0.0.0.0", port=port)
