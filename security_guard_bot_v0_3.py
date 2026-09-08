import os
import json
import logging
import random
from pathlib import Path
from datetime import datetime, timezone
from html import escape

import requests
from flask import Flask, request, jsonify

# =========================================================
# SECURITY GUARD BOT v0.3
# Тестова версія БЕЗ PostgreSQL.
#
# ГОЛОВНЕ У v0.3:
# - повноцінний модуль "Навчання та перевірка знань"
# - 30 питань за одну спробу
# - 6 тематичних блоків по 5 питань
# - поріг 80% використовується як корпоративний цільовий KPI
# - визначення слабких тем
# - історія власних результатів
# - адмін бачить результати працівників
#
# УВАГА:
# Дані поки зберігаються у локальному JSON-файлі Render.
# На Free Web Service це НЕ надійне постійне сховище.
# Після redeploy/restart дані можуть бути втрачені.
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
WEBHOOK_PATH = "/telegram-webhook"
PASS_PERCENT = 80

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
    "next_request_id": 1,
    "next_attempt_id": 1
}

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def load_data():
    if not DATA_FILE.exists():
        data = json.loads(json.dumps(DEFAULT_DATA))
        save_data(data)
        return data

    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        for k, v in DEFAULT_DATA.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data
    except Exception:
        log.exception("Не вдалося прочитати локальне сховище")
        return json.loads(json.dumps(DEFAULT_DATA))

def save_data(data):
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def get_user(tg_id):
    return load_data()["users"].get(str(tg_id))

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
    item = load_data()["states"].get(str(tg_id))
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
    return load_data()["access_requests"].get(str(req_id))

def update_access_request(req_id, status):
    data = load_data()
    item = data["access_requests"].get(str(req_id))
    if item:
        item["status"] = status
        data["access_requests"][str(req_id)] = item
        save_data(data)

def save_training_attempt(tg_id, attempt):
    data = load_data()
    attempt_id = int(data.get("next_attempt_id", 1))
    data["next_attempt_id"] = attempt_id + 1
    attempt["id"] = attempt_id
    attempt["telegram_id"] = tg_id
    attempt["finished_at"] = utc_now_iso()

    key = str(tg_id)
    attempts = data["training_attempts"].setdefault(key, [])
    attempts.append(attempt)
    data["training_attempts"][key] = attempts[-20:]
    save_data(data)
    return attempt_id

def get_training_attempts(tg_id):
    return load_data()["training_attempts"].get(str(tg_id), [])

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

def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return tg("editMessageText", payload)

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

    return {"keyboard": rows, "resize_keyboard": True}

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
        "keyboard": [[{"text": "🔐 Подати заявку на доступ"}]],
        "resize_keyboard": True
    }

def training_menu():
    return {
        "keyboard": [
            [{"text": "📝 Первинна оцінка знань"}],
            [{"text": "🔄 Повторне тестування"}],
            [{"text": "📚 Навчальні матеріали"}],
            [{"text": "🧠 Мої результати"}],
            [{"text": "⬅️ Головне меню"}],
        ],
        "resize_keyboard": True
    }

# =========================================================
# QUESTION BANK
# Джерела: завантажені службові документи користувача:
# - Процедура з організації охорони об'єктів, ред. 9, квітень 2026
# - Посадова інструкція старшого охоронника
# - Посадова інструкція охоронника-пожежного
# - Інструкція з організації пропускного режиму та переміщення ТМЦ
#
# 6 тем, у тест береться по 5 питань з кожної теми = 30.
# =========================================================

QUESTION_BANK = [
    # 1. НЕСЕННЯ СЛУЖБИ
    {
        "id": "S01", "topic": "Несення служби",
        "q": "Що під час приймання/здачі чергування потрібно перевірити щодо постової документації?",
        "options": [
            "Тільки наявність чистих бланків",
            "Наявність документації та обладнання постів і повноту заповнення документації попередньою зміною",
            "Лише підписи керівників",
            "Тільки журнал відвідувачів"
        ],
        "correct": 1,
        "explain": "Перевіряються наявність документації й обладнання постів та повнота заповнення службової документації зміною, що здає чергування."
    },
    {
        "id": "S02", "topic": "Несення служби",
        "q": "Яким способом, згідно Процедури, здійснюється охорона об'єкта під час патрулювання?",
        "options": [
            "Лише відеоспостереженням",
            "Пішим патрулюванням за встановленим у Схемі об'єкта маршрутом",
            "Патрулюванням довільним маршрутом",
            "Тільки автомобілем"
        ],
        "correct": 1,
        "explain": "Охорона здійснюється шляхом пішого патрулювання за маршрутом, встановленим у Схемі об'єкта."
    },
    {
        "id": "S03", "topic": "Несення служби",
        "q": "Що працівник охорони повинен мати при собі під час патрулювання?",
        "options": [
            "Особистий телефон",
            "Службовий телефон із ПЗ «Віалон» та ПЗ «МОХ»",
            "Тільки рацію",
            "Ноутбук поста №1"
        ],
        "correct": 1,
        "explain": "Під час патрулювання необхідний службовий телефон із ПЗ «Віалон» та «МОХ»."
    },
    {
        "id": "S04", "topic": "Несення служби",
        "q": "Що робити під час патрулювання у випадку несправності ПЗ «Віалон»?",
        "options": [
            "Припинити патрулювання",
            "Вимкнути телефон",
            "Проводити відмітки в точках КНС за допомогою ПЗ «МОХ»",
            "Чекати завершення зміни"
        ],
        "correct": 2,
        "explain": "У разі несправності «Віалон» відмітки в точках КНС виконуються за допомогою ПЗ «МОХ»."
    },
    {
        "id": "S05", "topic": "Несення служби",
        "q": "Чи дозволяється вимикати ПЗ «Віалон» під час несення служби?",
        "options": [
            "Так, на 15 хвилин",
            "Так, якщо розряджається телефон",
            "Ні, категорично забороняється",
            "Так, з дозволу напарника"
        ],
        "correct": 2,
        "explain": "Процедура прямо встановлює: вимикати ПЗ «Віалон» категорично забороняється."
    },
    {
        "id": "S06", "topic": "Несення служби",
        "q": "На що потрібно звертати увагу під час обходу об'єкта?",
        "options": [
            "Тільки на рух транспорту",
            "На місцевість, огорожу, стан об'єктів і наявність/відповідність пломб",
            "Тільки на освітлення",
            "Тільки на людей біля КПП"
        ],
        "correct": 1,
        "explain": "Патрулювання передбачає огляд місцевості, огорожі та перевірку пломб на об'єктах."
    },

    # 2. ПРОПУСКНИЙ РЕЖИМ
    {
        "id": "P01", "topic": "Пропускний режим",
        "q": "Для чого призначені КПП на об'єктах, що охороняються?",
        "options": [
            "Тільки для працівників охорони",
            "Для пропуску людей, транспорту, с/г техніки, агрегатів та ТМЦ",
            "Тільки для вантажного транспорту",
            "Тільки для відвідувачів"
        ],
        "correct": 1,
        "explain": "КПП призначені для пропуску людей, транспорту, техніки, агрегатів і ТМЦ."
    },
    {
        "id": "P02", "topic": "Пропускний режим",
        "q": "Яке нормальне положення воріт та вхідних дверей на об'єкт?",
        "options": [
            "Відкрите",
            "Напіввідкрите",
            "Закрите",
            "Залежить від погоди"
        ],
        "correct": 2,
        "explain": "Нормальним положенням воріт та вхідних дверей вважається закрите."
    },
    {
        "id": "P03", "topic": "Пропускний режим",
        "q": "Коли охоронник відкриває ворота для відвідувача?",
        "options": [
            "Одразу після усного прохання",
            "Після реєстрації відвідувача у відповідному журналі",
            "Якщо відвідувач знайомий",
            "Після дзвінка іншому охороннику"
        ],
        "correct": 1,
        "explain": "Відвідувач допускається після встановленої реєстрації."
    },
    {
        "id": "P04", "topic": "Пропускний режим",
        "q": "Що потрібно зробити при прибутті представників правоохоронних або інших контролюючих органів?",
        "options": [
            "Одразу пропустити на територію",
            "Уточнити мету, перевірити документи, зареєструвати та повідомити безпосереднього начальника",
            "Відмовити у будь-якому разі",
            "Лише переписати номер автомобіля"
        ],
        "correct": 1,
        "explain": "Процедура передбачає уточнення мети, перевірку документів, реєстрацію та інформування керівництва."
    },
    {
        "id": "P05", "topic": "Пропускний режим",
        "q": "До чиєї вказівки не допускаються представники правоохоронних/контролюючих органів на територію об'єкта?",
        "options": [
            "Будь-якого працівника",
            "Водія підприємства",
            "Безпосереднього начальника або керівника підприємства/кластера",
            "Відвідувача, якого вони супроводжують"
        ],
        "correct": 2,
        "explain": "Допуск здійснюється лише з дозволу відповідного керівника згідно Процедури."
    },
    {
        "id": "P06", "topic": "Пропускний режим",
        "q": "Що категорично заборонено під час несення служби щодо ТЗ і ТМЦ?",
        "options": [
            "Фіксувати номер ТЗ",
            "Перевіряти документи",
            "Випускати ТЗ або ТМЦ без відповідних дозвільних документів",
            "Реєструвати в 1С"
        ],
        "correct": 2,
        "explain": "Випуск ТЗ і ТМЦ без відповідних дозвільних документів заборонений."
    },

    # 3. РУХ ПРОДУКЦІЇ ТА ТМЦ
    {
        "id": "T01", "topic": "Рух продукції та ТМЦ",
        "q": "Що охоронник перевіряє в 1С при вивезенні/виносі/переміщенні ТМЦ?",
        "options": [
            "Тільки прізвище водія",
            "Наявність затвердженої заявки на охорону з переліком ТМЦ",
            "Тільки час виїзду",
            "Лише номер воріт"
        ],
        "correct": 1,
        "explain": "Потрібно перевірити наявність у 1С затвердженої заявки з переліком ТМЦ."
    },
    {
        "id": "T02", "topic": "Рух продукції та ТМЦ",
        "q": "Що потрібно звірити перед вивезенням ТМЦ?",
        "options": [
            "Колір автомобіля",
            "Фактичну номенклатуру і кількість ТМЦ з даними РТЗ та супровідного документа",
            "Тільки кількість місць",
            "Тільки назву підприємства"
        ],
        "correct": 1,
        "explain": "Фактична номенклатура і кількість повинні відповідати РТЗ та супровідному документу."
    },
    {
        "id": "T03", "topic": "Рух продукції та ТМЦ",
        "q": "Що додатково потрібно вказати в РТЗ при вивезенні ТМЦ автотранспортом?",
        "options": [
            "Марку телефону водія",
            "Номер транспортного засобу",
            "Номер поста охорони",
            "Погодні умови"
        ],
        "correct": 1,
        "explain": "При вивозі ТМЦ автотранспортом в РТЗ зазначається номер транспортного засобу."
    },
    {
        "id": "T04", "topic": "Рух продукції та ТМЦ",
        "q": "Що робить охоронник, якщо при вивезенні ТМЦ виявив порушення?",
        "options": [
            "Самостійно дозволяє виїзд",
            "Інформує безпосереднього начальника та діє за отриманими інструкціями",
            "Ігнорує, якщо порушення незначне",
            "Видаляє РТЗ"
        ],
        "correct": 1,
        "explain": "При виявленні порушень охоронник інформує безпосереднього начальника і діє згідно його інструкцій."
    },
    {
        "id": "T05", "topic": "Рух продукції та ТМЦ",
        "q": "Що робити за відсутності порушень після перевірки ТМЦ?",
        "options": [
            "Провести реєстрацію операції та здійснити випуск ТЗ",
            "Залишити ТЗ на території до кінця зміни",
            "Передати документи водію без реєстрації",
            "Попросити повторно оформити заявку"
        ],
        "correct": 0,
        "explain": "За відсутності порушень операція реєструється і транспортний засіб випускається."
    },
    {
        "id": "T06", "topic": "Рух продукції та ТМЦ",
        "q": "Про які випадки при роботі з ТМЦ повинно бути повідомлено керівництво?",
        "options": [
            "Тільки про нестачу",
            "Про нестачу, псування та порушення пакування ТМЦ",
            "Тільки про затримку транспорту",
            "Тільки про відсутність водія"
        ],
        "correct": 1,
        "explain": "Інструкція окремо визначає факти нестачі, псування та порушення пакування ТМЦ."
    },

    # 4. ОХОРОНА ОБ'ЄКТІВ
    {
        "id": "O01", "topic": "Охорона об'єктів",
        "q": "Що перевіряється щодо об'єктів, зданих під охорону, під час приймання чергування?",
        "options": [
            "Тільки назва об'єкта",
            "Кількість, стан, цілісність конструкцій, відповідність номерів пломб та стан в Ajax",
            "Тільки номер пломби",
            "Лише наявність освітлення"
        ],
        "correct": 1,
        "explain": "Приймання включає комплексну перевірку стану об'єктів, пломб і статусу Ajax."
    },
    {
        "id": "O02", "topic": "Охорона об'єктів",
        "q": "Що потрібно перевірити перед зняттям приміщення з охорони?",
        "options": [
            "Тільки температуру в приміщенні",
            "Сліди несанкціонованого проникнення, цілісність і відповідність номерів пломб",
            "Лише наявність ключів",
            "Тільки ПІБ МВО"
        ],
        "correct": 1,
        "explain": "Перед зняттям з охорони проводиться зовнішній огляд і перевіряються пломби."
    },
    {
        "id": "O03", "topic": "Охорона об'єктів",
        "q": "Чи дозволяється передавати NFC-картку іншій особі при закритті/відкритті об'єктів?",
        "options": [
            "Так, старшому зміни",
            "Так, якщо особа працює на підприємстві",
            "Ні, забороняється",
            "Так, у нічний час"
        ],
        "correct": 2,
        "explain": "Передача NFC-картки іншим особам забороняється."
    },
    {
        "id": "O04", "topic": "Охорона об'єктів",
        "q": "Що використовується МВО/відповідальною особою для постановки/зняття об'єкта в Ajax?",
        "options": [
            "Загальний пароль поста",
            "Особистий код доступу або особиста магнітна картка Ajax",
            "Будь-яка NFC-картка",
            "Пароль від Wi-Fi"
        ],
        "correct": 1,
        "explain": "Для Ajax використовується особистий код доступу або магнітна картка; передавати картку іншим заборонено."
    },
    {
        "id": "O05", "topic": "Охорона об'єктів",
        "q": "Де виконується постановка/зняття об'єкта з охорони Ajax згідно Процедури?",
        "options": [
            "З особистого телефону будь-де",
            "На прохідній через бездротову клавіатуру KeyPad",
            "У кабінеті директора",
            "На території біля об'єкта без клавіатури"
        ],
        "correct": 1,
        "explain": "Процедура визначає постановку/зняття через KeyPad на прохідній."
    },
    {
        "id": "O06", "topic": "Охорона об'єктів",
        "q": "Що заборонено робити з датчиками СОС Ajax?",
        "options": [
            "Перевіряти їх стан",
            "Блокувати, закривати, демонтувати або перекривати сторонніми предметами",
            "Повідомляти про несправності",
            "Контролювати їх через пост №1"
        ],
        "correct": 1,
        "explain": "Датчики не можна блокувати, закривати, демонтувати чи обмежувати їх зону сторонніми предметами."
    },

    # 5. ТЕХНІЧНІ СИСТЕМИ
    {
        "id": "A01", "topic": "Технічні системи",
        "q": "Які системи перевіряються на справність під час приймання чергування?",
        "options": [
            "Тільки Ajax",
            "СВС, СКУД, ТЗО, СОС Ajax та засоби зв'язку",
            "Тільки рації",
            "Тільки відеокамери"
        ],
        "correct": 1,
        "explain": "Процедура вимагає перевірки СВС, СКУД, ТЗО, Ajax і засобів зв'язку."
    },
    {
        "id": "A02", "topic": "Технічні системи",
        "q": "Що повинен контролювати співробітник охорони в системі Ajax на посту №1?",
        "options": [
            "Тільки час",
            "Статус постановки/зняття об'єктів і тривожні спрацювання",
            "Тільки список працівників",
            "Лише температуру"
        ],
        "correct": 1,
        "explain": "Контролюються статуси охорони об'єктів та всі тривожні спрацювання."
    },
    {
        "id": "A03", "topic": "Технічні системи",
        "q": "Які події Ajax підлягають контролю?",
        "options": [
            "Лише відкриття дверей",
            "Рух, відкриття дверей, пожежа, витік води, втрата зв'язку, саботаж та інші тривоги",
            "Тільки пожежні спрацювання",
            "Тільки втрата електроживлення"
        ],
        "correct": 1,
        "explain": "Процедура перелічує різні типи тривог, втрату зв'язку та саботаж."
    },
    {
        "id": "A04", "topic": "Технічні системи",
        "q": "Що потрібно зробити при тривозі або несправності СОС Ajax?",
        "options": [
            "Чекати наступної зміни",
            "Негайно повідомити визначених відповідальних, провести візуальну перевірку об'єкта та зафіксувати подію в рапорті",
            "Самостійно вимкнути систему",
            "Лише зробити скріншот"
        ],
        "correct": 1,
        "explain": "При тривозі/несправності потрібні негайне інформування, візуальна перевірка та фіксація в рапорті."
    },
    {
        "id": "A05", "topic": "Технічні системи",
        "q": "Кого потрібно повідомити при тривозі або несправності Ajax?",
        "options": [
            "Тільки МВО",
            "Чергову зміну охорони, НВО/ЗНВО/СОП та МЦ ДЗ",
            "Тільки директора",
            "Лише адміністратора Telegram-бота"
        ],
        "correct": 1,
        "explain": "Процедура визначає інформування чергової зміни, НВО/ЗНВО/СОП та МЦ ДЗ."
    },
    {
        "id": "A06", "topic": "Технічні системи",
        "q": "Що робить старший охоронник при виявленні недоліків у роботі системи відеоспостереження?",
        "options": [
            "Вимикає систему",
            "Негайно доповідає НВО та інформує моніторинговий центр",
            "Чекає завершення зміни",
            "Тільки записує в особисті нотатки"
        ],
        "correct": 1,
        "explain": "Посадова інструкція передбачає негайну доповідь НВО та інформування моніторингового центру."
    },

    # 6. ПОЖЕЖНА БЕЗПЕКА ТА НС
    {
        "id": "F01", "topic": "Пожежна безпека та НС",
        "q": "Що перевіряється під час приймання чергування щодо пожежної безпеки?",
        "options": [
            "Тільки наявність вогнегасника на КПП",
            "Наявність і комплектність пожежних щитів та справність пожежної техніки",
            "Тільки пожежна сигналізація",
            "Лише журнал інструктажу"
        ],
        "correct": 1,
        "explain": "При прийманні чергування перевіряються пожежні щити та справність пожежної техніки."
    },
    {
        "id": "F02", "topic": "Пожежна безпека та НС",
        "q": "Що заборонено під час патрулювання об'єкта?",
        "options": [
            "Робити короткі зупинки для огляду",
            "Палити",
            "Перевіряти пломби",
            "Доповідати по зв'язку"
        ],
        "correct": 1,
        "explain": "Паління під час патрулювання заборонено."
    },
    {
        "id": "F03", "topic": "Пожежна безпека та НС",
        "q": "Чи дозволено використовувати в приміщенні охорони саморобні електронагрівальні прилади?",
        "options": [
            "Так, узимку",
            "Так, у нічну зміну",
            "Ні, заборонено",
            "Так, якщо є вогнегасник"
        ],
        "correct": 2,
        "explain": "Посадові інструкції забороняють використання саморобних електронагрівальних приладів."
    },
    {
        "id": "F04", "topic": "Пожежна безпека та НС",
        "q": "Що робити у разі спроби проникнення на об'єкт?",
        "options": [
            "Не повідомляти нікого до завершення зміни",
            "Повідомити керівництво та вжити заходів для недопущення проникнення і збереження ТМЦ",
            "Самовільно залишити пост",
            "Відкрити ворота для з'ясування обставин"
        ],
        "correct": 1,
        "explain": "При спробі проникнення необхідно інформувати керівництво та діяти для попередження проникнення і збереження ТМЦ."
    },
    {
        "id": "F05", "topic": "Пожежна безпека та НС",
        "q": "Що робити при загрозі життю або здоров'ю під час проникнення?",
        "options": [
            "Тільки зробити запис у журналі",
            "Подати сигнал тривоги та сповістити відповідні служби/відповідальних осіб",
            "Вимкнути зв'язок",
            "Залишити об'єкт без повідомлення"
        ],
        "correct": 1,
        "explain": "При загрозі життю/здоров'ю Процедура передбачає подання тривоги й сповіщення відповідальних служб та осіб."
    },
    {
        "id": "F06", "topic": "Пожежна безпека та НС",
        "q": "Який номер використовується для негайного інформування чергової частини поліції при небезпечному проникненні?",
        "options": [
            "101",
            "102",
            "103",
            "112 лише через адміністратора"
        ],
        "correct": 1,
        "explain": "У Процедурі прямо зазначено інформування поліції по лінії «102»."
    },
]

QUESTION_BY_ID = {q["id"]: q for q in QUESTION_BANK}
TOPICS = [
    "Несення служби",
    "Пропускний режим",
    "Рух продукції та ТМЦ",
    "Охорона об'єктів",
    "Технічні системи",
    "Пожежна безпека та НС",
]

# =========================================================
# TRAINING ENGINE
# =========================================================

def build_test():
    selected = []
    for topic in TOPICS:
        pool = [q for q in QUESTION_BANK if q["topic"] == topic]
        selected.extend(random.sample(pool, 5))
    random.shuffle(selected)
    return [q["id"] for q in selected]

def quiz_keyboard(question_index, q):
    rows = []
    letters = ["А", "Б", "В", "Г"]
    for i, opt in enumerate(q["options"]):
        rows.append([{
            "text": f"{letters[i]}. {opt}",
            "callback_data": f"quiz:{question_index}:{i}"
        }])
    return {"inline_keyboard": rows}

def send_quiz_question(tg_id, payload, edit_message_id=None):
    idx = int(payload["current"])
    qids = payload["question_ids"]

    if idx >= len(qids):
        finish_quiz(tg_id, payload, edit_message_id)
        return

    q = QUESTION_BY_ID[qids[idx]]
    text = (
        f"🎓 <b>Перевірка знань</b>\n"
        f"Питання <b>{idx + 1}/{len(qids)}</b>\n"
        f"Тема: <b>{escape(q['topic'])}</b>\n\n"
        f"{escape(q['q'])}"
    )
    kb = quiz_keyboard(idx, q)

    if edit_message_id:
        edit_message(tg_id, edit_message_id, text, kb)
    else:
        result = send_message(tg_id, text, kb)
        if result.get("ok"):
            payload["quiz_message_id"] = result["result"]["message_id"]
            set_state(tg_id, "quiz", payload)

def start_quiz(tg_id, mode="primary"):
    user = get_user(tg_id)
    if not user or user.get("status") != "active":
        return

    qids = build_test()
    payload = {
        "mode": mode,
        "question_ids": qids,
        "current": 0,
        "correct": 0,
        "answers": [],
        "topic_stats": {
            topic: {"correct": 0, "total": 0}
            for topic in TOPICS
        },
        "started_at": utc_now_iso()
    }
    set_state(tg_id, "quiz", payload)

    send_message(
        tg_id,
        "📝 <b>Первинна оцінка знань</b>\n\n"
        "У тесті <b>30 питань</b> — по 5 з кожного тематичного блоку.\n"
        f"Цільовий результат: <b>{PASS_PERCENT}% або більше</b>.\n\n"
        "На кожне питання обери одну відповідь.\n"
        "Після вибору змінити відповідь уже не можна."
    )
    send_quiz_question(tg_id, payload)

def finish_quiz(tg_id, payload, message_id=None):
    total = len(payload["question_ids"])
    correct = int(payload["correct"])
    percent = round(correct * 100 / total) if total else 0
    passed = percent >= PASS_PERCENT

    topic_results = {}
    weak_topics = []

    for topic, stats in payload["topic_stats"].items():
        topic_percent = round(
            stats["correct"] * 100 / stats["total"]
        ) if stats["total"] else 0

        topic_results[topic] = {
            "correct": stats["correct"],
            "total": stats["total"],
            "percent": topic_percent
        }

        if topic_percent < PASS_PERCENT:
            weak_topics.append(topic)

    attempt = {
        "mode": payload.get("mode", "primary"),
        "started_at": payload.get("started_at"),
        "total": total,
        "correct": correct,
        "percent": percent,
        "passed": passed,
        "weak_topics": weak_topics,
        "topic_results": topic_results
    }
    save_training_attempt(tg_id, attempt)
    clear_state(tg_id)

    status = "✅ <b>Результат достатній</b>" if passed else "🔄 <b>Потрібне повторне навчання</b>"

    lines = [
        "🎓 <b>Тестування завершено</b>",
        "",
        f"Правильних відповідей: <b>{correct}/{total}</b>",
        f"Результат: <b>{percent}%</b>",
        f"Цільовий поріг: <b>{PASS_PERCENT}%</b>",
        "",
        status,
        "",
        "<b>Результати за темами:</b>"
    ]

    for topic in TOPICS:
        tr = topic_results[topic]
        icon = "✅" if tr["percent"] >= PASS_PERCENT else "⚠️"
        lines.append(
            f"{icon} {escape(topic)} — "
            f"{tr['correct']}/{tr['total']} ({tr['percent']}%)"
        )

    if weak_topics:
        lines.extend([
            "",
            "<b>Слабкі теми:</b>",
            "• " + "\n• ".join(escape(x) for x in weak_topics),
            "",
            "Рекомендовано пройти навчальні матеріали "
            "за цими темами та повторити тест."
        ])

    text = "\n".join(lines)

    if message_id:
        edit_message(tg_id, message_id, text, {"inline_keyboard": []})
    else:
        send_message(tg_id, text)

    user = get_user(tg_id)
    if user:
        send_message(tg_id, "🎓 Меню навчання", training_menu())

    # Коротке повідомлення адміну
    if tg_id != ADMIN_ID:
        user = get_user(tg_id) or {}
        send_message(
            ADMIN_ID,
            "🎓 <b>Працівник завершив тест</b>\n\n"
            f"👤 {escape(user.get('full_name') or str(tg_id))}\n"
            f"📊 Результат: <b>{percent}%</b>\n"
            f"{'✅ Порогове значення досягнуто' if passed else '⚠️ Потрібне повторне навчання'}"
        )

def handle_quiz_callback(cq):
    tg_id = cq["from"]["id"]
    data = cq.get("data", "")
    state, payload = get_state(tg_id)

    if state != "quiz":
        answer_callback(cq["id"], "Це тестування вже завершено.")
        return

    try:
        _, idx_s, option_s = data.split(":", 2)
        idx = int(idx_s)
        option = int(option_s)
    except Exception:
        answer_callback(cq["id"], "Некоректна відповідь.")
        return

    current = int(payload.get("current", 0))
    if idx != current:
        answer_callback(cq["id"], "Це питання вже опрацьовано.")
        return

    qid = payload["question_ids"][current]
    q = QUESTION_BY_ID[qid]

    if option < 0 or option >= len(q["options"]):
        answer_callback(cq["id"], "Некоректна відповідь.")
        return

    is_correct = option == q["correct"]
    topic = q["topic"]

    payload["topic_stats"][topic]["total"] += 1
    if is_correct:
        payload["correct"] += 1
        payload["topic_stats"][topic]["correct"] += 1

    payload["answers"].append({
        "qid": qid,
        "selected": option,
        "correct": q["correct"],
        "ok": is_correct
    })
    payload["current"] = current + 1
    set_state(tg_id, "quiz", payload)

    answer_callback(
        cq["id"],
        "✅ Правильно" if is_correct else "❌ Неправильно"
    )

    message_id = cq["message"]["message_id"]

    if payload["current"] >= len(payload["question_ids"]):
        finish_quiz(tg_id, payload, message_id)
        return

    # короткий фідбек + наступне питання
    if is_correct:
        feedback = "✅ <b>Правильно.</b>"
    else:
        correct_text = q["options"][q["correct"]]
        feedback = (
            "❌ <b>Неправильно.</b>\n"
            f"Правильна відповідь: <b>{escape(correct_text)}</b>"
        )

    feedback += f"\n\nℹ️ {escape(q['explain'])}"
    edit_message(tg_id, message_id, feedback, {
        "inline_keyboard": [[
            {
                "text": "➡️ Наступне питання",
                "callback_data": f"nextq:{payload['current']}"
            }
        ]]
    })

def handle_next_question(cq):
    tg_id = cq["from"]["id"]
    state, payload = get_state(tg_id)

    if state != "quiz":
        answer_callback(cq["id"], "Тестування вже завершено.")
        return

    try:
        _, idx_s = cq.get("data", "").split(":", 1)
        requested = int(idx_s)
    except Exception:
        requested = -1

    if requested != int(payload.get("current", 0)):
        answer_callback(cq["id"], "Перехід уже виконано.")
        return

    answer_callback(cq["id"])
    send_quiz_question(
        tg_id,
        payload,
        edit_message_id=cq["message"]["message_id"]
    )

def format_my_results(tg_id):
    attempts = get_training_attempts(tg_id)
    if not attempts:
        return "📭 Завершених тестувань поки немає."

    last = attempts[-5:]
    lines = ["🧠 <b>Мої результати</b>", ""]

    for a in reversed(last):
        icon = "✅" if a.get("passed") else "⚠️"
        date = (a.get("finished_at") or "")[:10]
        lines.append(
            f"{icon} {date or '—'} — "
            f"<b>{a.get('percent', 0)}%</b> "
            f"({a.get('correct', 0)}/{a.get('total', 0)})"
        )

    best = max(a.get("percent", 0) for a in attempts)
    lines.extend([
        "",
        f"🏆 Найкращий результат: <b>{best}%</b>",
        f"📝 Кількість спроб: <b>{len(attempts)}</b>"
    ])
    return "\n".join(lines)

def admin_training_report():
    data = load_data()
    users = [
        u for u in data["users"].values()
        if u.get("status") == "active"
        and u.get("telegram_id") != ADMIN_ID
    ]

    if not users:
        return "🎓 Активних працівників поки немає."

    completed = 0
    below = 0
    latest_scores = []
    rows = []

    for u in users:
        tg_id = u.get("telegram_id")
        attempts = data["training_attempts"].get(str(tg_id), [])
        if attempts:
            completed += 1
            latest = attempts[-1]
            score = latest.get("percent", 0)
            latest_scores.append(score)
            if score < PASS_PERCENT:
                below += 1
            icon = "✅" if score >= PASS_PERCENT else "⚠️"
            rows.append(
                f"{icon} {escape(u.get('full_name') or str(tg_id))} — "
                f"<b>{score}%</b> ({len(attempts)} спроб)"
            )
        else:
            rows.append(
                f"⬜ {escape(u.get('full_name') or str(tg_id))} — не проходив"
            )

    coverage = round(completed * 100 / len(users)) if users else 0
    avg = round(sum(latest_scores) / len(latest_scores)) if latest_scores else 0

    lines = [
        "🎓 <b>Навчання — результати</b>",
        "",
        f"👥 Працівників: <b>{len(users)}</b>",
        f"📝 Пройшли оцінку: <b>{completed}</b>",
        f"📊 Охоплення: <b>{coverage}%</b>",
        f"📈 Середній останній результат: <b>{avg}%</b>",
        f"⚠️ Нижче {PASS_PERCENT}%: <b>{below}</b>",
        "",
        "<b>Працівники:</b>",
        *rows
    ]
    return "\n".join(lines)

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
        f"👤 {escape(full_name)}\n"
        f"💼 Посада: {escape(position)}\n"
        f"🆔 Telegram ID: <code>{tg_id}</code>\n"
        f"🔗 Username: @{escape(username) if username else 'немає'}",
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
        upsert_user_basic(tg_id, role=role, status="active")

        role_name = (
            "Старший охоронник"
            if role == "senior_guard"
            else "Охоронник-пожежник"
        )

        send_message(
            tg_id,
            "✅ <b>Доступ надано</b>\n\n"
            f"👤 {escape(full_name)}\n"
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
        upsert_user_basic(tg_id, status="rejected")

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
            "Режим адміністратора.\n"
            "Версія: <b>v0.3</b>\n\n"
            "🧪 Тимчасове локальне сховище без PostgreSQL.",
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
        f"👤 {escape(user.get('full_name') or '')}\n"
        f"💼 {escape(role_text)}",
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
            attempts = sum(
                len(v) for v in data["training_attempts"].values()
            )

            send_message(
                tg_id,
                "✅ <b>Бот працює</b>\n"
                "Версія: <b>v0.3</b>\n"
                "🧪 Сховище: локальний JSON\n"
                "⚠️ PostgreSQL поки не підключено\n\n"
                f"👥 Активних користувачів: {active_users}\n"
                f"🔐 Нових заявок: {new_requests}\n"
                f"🎓 Завершених тестувань: {attempts}",
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
                    f"#{r['id']} — {escape(r['full_name'])}\n"
                    f"💼 {escape(r['position'])}\n"
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
                    f"👤 {escape(u.get('full_name') or 'Без ПІБ')}\n"
                    f"💼 {escape(role_names.get(u.get('role'), u.get('role') or '—'))}\n"
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
                admin_training_report(),
                admin_menu()
            )
            return

    # USER STATE
    state, payload = get_state(tg_id)

    if state == "quiz":
        send_message(
            tg_id,
            "🎓 Тестування вже триває.\n"
            "Відповідай кнопками під поточним питанням."
        )
        return

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

    if text == "🎓 Навчання":
        send_message(
            tg_id,
            "🎓 <b>Навчання та перевірка знань</b>\n\n"
            "Первинна оцінка: 30 питань.\n"
            f"Цільовий результат: {PASS_PERCENT}%.\n"
            "Питання сформовані за службовими документами.",
            training_menu()
        )
        return

    if text == "📝 Первинна оцінка знань":
        start_quiz(tg_id, mode="primary")
        return

    if text == "🔄 Повторне тестування":
        attempts = get_training_attempts(tg_id)
        if not attempts:
            send_message(
                tg_id,
                "Спочатку пройди первинну оцінку знань.",
                training_menu()
            )
        else:
            start_quiz(tg_id, mode="repeat")
        return

    if text == "📚 Навчальні матеріали":
        send_message(
            tg_id,
            "📚 <b>Навчальні матеріали</b>\n\n"
            "У поточному модулі перевіряються 6 блоків:\n"
            "1. Несення служби\n"
            "2. Пропускний режим\n"
            "3. Рух продукції та ТМЦ\n"
            "4. Охорона об'єктів\n"
            "5. Технічні системи\n"
            "6. Пожежна безпека та НС\n\n"
            "Наступним кроком додамо короткі навчальні картки "
            "окремо для кожної теми.",
            training_menu()
        )
        return

    if text == "🧠 Мої результати":
        send_message(
            tg_id,
            format_my_results(tg_id),
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
            "🟡 Модуль зміни буде наступним етапом.",
        "🚶 Обхід":
            "🟡 Погодинні обходи підключимо окремим етапом.",
        "🔎 Знайти об'єкт":
            "🟡 Довідник Табеля постів підключимо окремим етапом.",
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

def handle_callback(cq):
    data = cq.get("data", "")

    if data.startswith("quiz:"):
        handle_quiz_callback(cq)
        return

    if data.startswith("nextq:"):
        handle_next_question(cq)
        return

    if data.startswith("approve:") or data.startswith("reject:"):
        process_admin_callback(cq)
        return

    answer_callback(cq["id"], "Невідома дія")

# =========================================================
# WEBHOOK
# =========================================================

@app.get("/")
def health():
    return jsonify({
        "ok": True,
        "service": "security_guard_bot",
        "version": "0.3",
        "storage": "local_json_test_only",
        "training_questions": len(QUESTION_BANK)
    })

@app.post(WEBHOOK_PATH)
def telegram_webhook():
    update = request.get_json(silent=True) or {}

    try:
        if "callback_query" in update:
            handle_callback(update["callback_query"])

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
    result = tg("setWebhook", {"url": webhook_url})
    log.info("setWebhook: %s", result)

if __name__ == "__main__":
    set_webhook()

    port = int(os.getenv("PORT", "10000"))
    log.info(
        "Starting Security Guard Bot v0.3 on port %s",
        port
    )
    app.run(
        host="0.0.0.0",
        port=port
    )
