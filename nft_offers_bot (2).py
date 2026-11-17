# -*- coding: utf-8 -*-
"""
NFT-offers бот — сделки с NFT-подарками Telegram.
Сценарий: покупатель командует «купить <ссылка> цена <сумма>»,
продавец принимает (кнопка «Перейти к оформлению» ведёт в бота через deep link),
получает эскроу-инструкцию, передаёт подарок,
бот фиксирует получение и закрывает сделку.

ВНИМАНИЕ: перед запуском впиши свежий токен в API_TOKEN
(старый токен, светившийся в чате, отзови в BotFather: /revoke).
"""

import re
import os
import sqlite3
import datetime
import telebot
from telebot import types

# ================= НАСТРОЙКИ =================



bot = telebot.TeleBot(API_TOKEN, parse_mode='HTML')

# ---------- ЦВЕТНОЙ ЛОГЕР ----------

RESET  = "\033[0m"
RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
GRAY   = "\033[0m"

if os.name == 'nt':
    try:
        import colorama
        colorama.just_fix_windows_console()
    except ImportError:
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass

def log(msg, color=GRAY):
    ts = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]
    print(f"{GRAY}[{ts}]{RESET} {color}{msg}{RESET}", flush=True)

def log_err(msg):   log("X " + msg, RED)
def log_ok(msg):    log("OK " + msg, GREEN)
def log_warn(msg):  log("!! " + msg, YELLOW)
def log_biz(msg):   log("[biz] " + msg, CYAN)

# ---------- БАЗА ДАННЫХ ----------

def init_db():
    conn = sqlite3.connect('bot.db')
    cur = conn.cursor()

    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT)""")

    cur.execute("""CREATE TABLE IF NOT EXISTS purchases (
        purchase_id INTEGER PRIMARY KEY AUTOINCREMENT,
        buyer_id INTEGER,
        seller_id INTEGER,
        gift_name TEXT,
        gift_number TEXT,
        photo_file_id TEXT,
        price INTEGER,
        status TEXT DEFAULT 'pending')""")

    cur.execute("""CREATE TABLE IF NOT EXISTS offers (
        offer_id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER,
        seller_id INTEGER,
        gift_name TEXT,
        gift_number TEXT,
        photo_file_id TEXT,
        price INTEGER,
        status TEXT DEFAULT 'pending')""")

    cur.execute("""CREATE TABLE IF NOT EXISTS business_connections (
        user_id INTEGER PRIMARY KEY,
        connection_id TEXT)""")

    cur.execute("PRAGMA table_info(photo_cache)")
    cols = [r[1] for r in cur.fetchall()]
    if cols and 'gift_number' not in cols:
        cur.execute("DROP TABLE photo_cache")
    cur.execute("""CREATE TABLE IF NOT EXISTS photo_cache (
        gift_name TEXT,
        gift_number TEXT,
        photo_file_id TEXT,
        PRIMARY KEY (gift_name, gift_number))""")

    conn.commit()
    conn.close()

init_db()

def db(query, params=(), fetch=False):
    conn = sqlite3.connect('bot.db', timeout=10)
    cur = conn.cursor()
    try:
        cur.execute(query, params)
        result = cur.fetchall() if fetch else None
        conn.commit()
        conn.close()
        return result
    except Exception as e:
        log_err(f"DB error: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return None

def db_insert(query, params=()):
    conn = sqlite3.connect('bot.db', timeout=10)
    cur = conn.cursor()
    try:
        cur.execute(query, params)
        last_id = cur.lastrowid
        conn.commit()
        conn.close()
        return last_id
    except Exception as e:
        log_err(f"DB error: {e}")
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return None

# ---------- БАЗА ИЗБРАННЫХ (team_ID.txt) ----------

def load_team():
    """Читает team_ID.txt: строки вида '1) 123456789 - username' или '123456789 - username'."""
    team = {}
    if os.path.exists(TEAM_FILE):
        with open(TEAM_FILE, encoding='utf-8-sig') as f:
            for line in f:
                line = line.split('#')[0].strip()
                if not line:
                    continue
                m = re.match(r'(?:\d+[).]\s*)?(\d+)\s*[-—:]\s*(\S+)', line)
                if m:
                    team[int(m.group(1))] = m.group(2).lstrip('@')
                else:
                    log_warn(f"team_ID.txt: не понял строку: {line!r}")
    return team

TEAM = load_team()
log_ok(f"team_ID.txt загружен: {len(TEAM)} избранных (+ ADMIN_ID)")

def is_team(uid):
    return uid == ADMIN_ID or uid in TEAM

def save_team():
    with open(TEAM_FILE, 'w', encoding='utf-8') as f:
        f.write("# Избранные пользователи бота\n")
        f.write("# Формат: id - username\n\n")
        if ADMIN_ID in TEAM:
            f.write(f"{ADMIN_ID} - {TEAM[ADMIN_ID]}\n")
        for uid in sorted(TEAM):
            if uid != ADMIN_ID:
                f.write(f"{uid} - {TEAM[uid]}\n")

# ---------- ПОДАРКИ ИЗ ПРОФИЛЯ ----------

def fetch_user_gifts(uid, limit=50):
    result = bot.get_user_gifts(uid, limit=limit)
    log(f"get_user_gifts({uid}): получено {len(result.gifts or [])} подарков")
    return result.gifts or []

def fetch_user_unique_gifts(uid, limit=50):
    return [g for g in fetch_user_gifts(uid, limit=limit) if g.type == 'unique']

def gift_display_info(owned_gift):
    ug = owned_gift.gift
    return ug.base_name, ug.number, ug.model.sticker.file_id

def gift_slug(base_name):
    return re.sub(r'[^a-z0-9]', '', base_name.lower())

# ---------- ФОТО С FRAGMENT.COM ----------

def get_gift_photo(chat_id, base_name, number):
    cached = db("SELECT photo_file_id FROM photo_cache WHERE gift_name = ? AND gift_number = ?",
                (base_name, str(number)), fetch=True)
    if cached:
        log(f"фото {base_name} #{number}: из кэша")
        return cached[0][0]

    url = f"https://nft.fragment.com/gift/{gift_slug(base_name)}-{number}.large.jpg"
    log(f"фото {base_name} #{number}: качаю с Fragment: {url}")
    try:
        msg = bot.send_photo(chat_id, url)
        photo_file_id = msg.photo[-1].file_id
        try:
            bot.delete_message(chat_id, msg.message_id)
        except Exception:
            pass
        db("INSERT OR REPLACE INTO photo_cache VALUES (?,?,?)",
           (base_name, str(number), photo_file_id))
        log(f"фото {base_name} #{number}: закэшировано")
        return photo_file_id
    except Exception as e:
        log_warn(f"фото {base_name} #{number}: НЕ получил ({e})")
        return None

def show_gifts_with_photos(chat_id, gifts):
    for owned in gifts:
        name, number, sticker_id = gift_display_info(owned)
        photo = get_gift_photo(chat_id, name, number)
        try:
            if photo:
                bot.send_photo(chat_id, photo, caption=f"💎 {name} #{number}")
            else:
                bot.send_sticker(chat_id, sticker_id)
                bot.send_message(chat_id, f"💎 {name} #{number}")
        except Exception:
            bot.send_message(chat_id, f"💎 {name} #{number}")

# ---------- BUSINESS CONNECTION ----------

@bot.business_connection_handler
def on_business_connection(bc):
    log_biz(f"connection-апдейт: user={bc.user.id}, id={bc.id}, enabled={bc.is_enabled}")
    if bc.is_enabled:
        db("INSERT OR REPLACE INTO business_connections VALUES (?,?)",
           (bc.user.id, bc.id))
        log_ok(f"аккаунт {bc.user.id} подключён как секретарский")
    else:
        db("DELETE FROM business_connections WHERE user_id = ?", (bc.user.id,))
        log_warn(f"аккаунт {bc.user.id} отключён")

@bot.message_handler(commands=['mybusiness'])
def my_business(message):
    rows = db("SELECT connection_id FROM business_connections WHERE user_id = ?",
              (message.from_user.id,), fetch=True)
    if rows:
        bot.reply_to(message, f"OK. Аккаунт подключён (connection_id={rows[0][0][:10]}).")
    else:
        bot.reply_to(message,
            "В базе нет подключения. Возможные причины:\n"
            "1. Бот был перезапущен ПОСЛЕ подключения — Telegram шлёт connection-апдейт "
            "один раз, при оффлайн-боте он теряется. Решение: отключи и заново подключи бота "
            "в Настройки → Telegram Business → Чат-боты.\n"
            "2. Библиотека старая — pip install -U pyTelegramBotAPI")

# ---------- ПАРСИНГ КОМАНДЫ «КУПИТЬ» ----------

def parse_buy_command(text):
    text = text[len('купить'):]
    m = re.search(r'(?:https?://)?(?:t\.me/nft/|nft\.fragment\.com/gift/)([A-Za-z0-9]+?)-(\d+)',
                  text, re.I)
    if m:
        slug, number = m.group(1).lower(), m.group(2)
        log(f"parse: распознал ссылку -> slug='{slug}', number={number}")
    else:
        m = re.search(r'([A-Za-z0-9 ]+?)\s*#\s*(\d+)', text)
        if not m:
            log_warn("parse: НЕ смог распознать подарок в тексте")
            return None
        slug, number = gift_slug(m.group(1)), m.group(2)
        log(f"parse: распознал текст -> slug='{slug}', number={number}")

    pm = re.search(r'цена\s*(\d+)', text, re.I)
    if not pm:
        log_warn("parse: НЕ нашёл слово «цена» с числом")
        return None
    price = int(pm.group(1))
    log(f"parse: цена={price}")
    return slug, number, price

def find_gift_in_inventory(seller_id, slug, number):
    try:
        gifts = fetch_user_unique_gifts(seller_id)
    except Exception as e:
        log_err(f"инвентарь продавца {seller_id}: ошибка - {e}")
        return None
    log(f"инвентарь продавца {seller_id}: {len(gifts)} NFT в витрине")
    for owned in gifts:
        name, num, sticker = gift_display_info(owned)
        if gift_slug(name) == slug and str(num) == str(number):
            log_ok(f"инвентарь: нашёл {name} #{num}")
            return owned, name, num, sticker
    preview = [f"{gift_display_info(g)[0]}#{gift_display_info(g)[1]}" for g in gifts[:10]]
    log_warn(f"инвентарь: подарок {slug} #{number} НЕ найден. В витрине есть: {preview}")
    return None

def create_buy_request(buyer_id, seller_id, slug, number, price):
    found = find_gift_in_inventory(seller_id, slug, number)
    if not found:
        return None, (f"подарок {slug} #{number} не найден в инвентаре пользователя "
                      "(или его витрина скрыта)")
    owned, name, num, sticker = found
    photo = get_gift_photo(buyer_id, name, num)
    purchase_id = db_insert("""INSERT INTO purchases
          (buyer_id, seller_id, gift_name, gift_number, photo_file_id, price)
          VALUES (?,?,?,?,?,?)""",
       (buyer_id, seller_id, name, str(num), photo, price))
    log_ok(f"БД: создан purchase #{purchase_id} (buyer={buyer_id}, seller={seller_id}, "
           f"{name} #{num}, {price} звёзд)")
    return purchase_id, None

# ---------- ДОСТАВКА ОТ ЛИЧНОГО АККАУНТА (секретарь) ----------

def deliver_purchase_from_owner(conn_id, seller_id, purchase_id):
    rows = db("""SELECT buyer_id, gift_name, gift_number, photo_file_id, price, status
                 FROM purchases WHERE purchase_id = ?""", (purchase_id,), fetch=True)
    if not rows:
        log_err(f"purchase #{purchase_id}: не найден в базе при доставке")
        return False, "запрос не найден в базе"
    buyer_id, gift_name, gift_number, photo_file_id, price, status = rows[0]
    if status != 'pending':
        return False, "по этому запросу уже есть ответ"

    caption = (
        f"🎁 <b>Предлагаю сделку</b> — хочу купить твой NFT-подарок:\n\n"
        f"💎 Подарок: <b>{gift_name} #{gift_number}</b>\n"
        f"⭐️ Цена: <b>{price} звёзд</b>\n\n"
        f"<b>Доверие:</b> ✅ 98% | ❌ 2%\n"
        f"🤖 Сделки оформляются через гарант-бота"
    )
    bot_name = bot.get_me().username
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Перейти к оформлению",
            url=f"https://t.me/{bot_name}?start=acc_{purchase_id}"),
        types.InlineKeyboardButton("❌ Отклонить",
            callback_data=f"pdec_{purchase_id}"),
    )

    if photo_file_id:
        log_biz(f"purchase #{purchase_id}: шлю фото от имени владельца")
        try:
            bot.send_photo(seller_id, photo_file_id, caption=caption,
                           reply_markup=markup, business_connection_id=conn_id)
            log_ok(f"purchase #{purchase_id}: бизнес-фото отправлено")
            return True, None
        except Exception as e:
            log_err(f"purchase #{purchase_id}: бизнес-фото не отправилось: {e}")

    log_biz(f"purchase #{purchase_id}: шлю текст от имени владельца")
    try:
        bot.send_message(seller_id, caption, reply_markup=markup,
                         business_connection_id=conn_id)
        log_ok(f"purchase #{purchase_id}: бизнес-текст отправлен")
        return True, None
    except Exception as e:
        log_err(f"purchase #{purchase_id}: бизнес-текст не отправился: {e}")
        try:
            bot.send_message(seller_id, caption, business_connection_id=conn_id)
            log_warn(f"purchase #{purchase_id}: отправил текст без кнопок")
            return True, None
        except Exception as e2:
            log_err(f"purchase #{purchase_id}: бизнес-отправка не удалась: {e2}")
            return False, str(e2)

# ---------- КОМАНДА «КУПИТЬ» ЧЕРЕЗ СЕКРЕТАРЯ (только избранные) ----------

processed_business = set()  # (chat_id, message_id) — защита от повторной обработки

@bot.business_message_handler(
    func=lambda m: bool(m.text) and m.text.strip().lower().startswith('купить'))
def buy_via_business(message):
    key = (message.chat.id, message.message_id)
    if key in processed_business:
        log_warn(f"дубль сообщения {message.message_id} из чата {message.chat.id} - пропускаю")
        return
    processed_business.add(key)
    if len(processed_business) > 500:
        processed_business.clear()

    log_biz(f"business-сообщение #{message.message_id}: от={message.from_user.id}, "
            f"chat={message.chat.id}, text={message.text!r}")

    # Доступ к команде «купить» — только избранные (team_ID.txt + ADMIN_ID)
    if not is_team(message.from_user.id):
        log_warn(f"business-«купить» от {message.from_user.id} (@{message.from_user.username}) "
                 f"— нет доступа")
        try:
            bot.send_message(message.from_user.id,
                "⛔ Команда «купить…» доступна только избранным пользователям.")
        except Exception:
            pass
        return

    conn_id = message.business_connection_id

    rows = db("SELECT user_id FROM business_connections WHERE connection_id = ?",
              (conn_id,), fetch=True)
    if rows:
        owner_id = rows[0][0]
    elif is_team(message.from_user.id):
        db("INSERT OR REPLACE INTO business_connections VALUES (?,?)",
           (message.from_user.id, conn_id))
        owner_id = message.from_user.id
        log_ok(f"соединение от {message.from_user.id} не было в базе - "
               f"восстановил его из этого сообщения")
    else:
        log_warn("business-сообщение от неизвестного соединения - игнорирую")
        return

    if message.from_user.id != owner_id:
        log(f"business-сообщение не от владельца (от {message.from_user.id}) - игнорирую")
        return

    seller_id = message.chat.id
    if seller_id == owner_id:
        log("business-сообщение в чат с самим собой (Избранное) - игнорирую. "
            "Команду «купить» отправляй в чат с продавцом.")
        return

    parsed = parse_buy_command(message.text)
    if not parsed:
        try:
            bot.send_message(owner_id,
                "Не понял формат. Правильно так:\n"
                "<code>купить https://t.me/nft/PlushPepe-123456 цена 500</code>")
        except Exception:
            pass
        return

    slug, number, price = parsed
    log_biz(f"команда «купить» от владельца: seller={seller_id}, {slug} #{number}, {price} звёзд")

    # Удаляем команду из чата, чтобы не засорять переписку
    try:
        bot.delete_business_messages(conn_id, [message.message_id])
        log_ok(f"команда «купить» удалена из чата (message_id={message.message_id})")
    except Exception as e:
        log_warn(f"не смог удалить команду: {e}")

    purchase_id, err = create_buy_request(owner_id, seller_id, slug, number, price)
    if not purchase_id:
        log_err(f"оффер не создан: {err}")
        try:
            bot.send_message(owner_id, f"Не создал оффер: {err}")
        except Exception:
            pass
        return

    ok, berr = deliver_purchase_from_owner(conn_id, seller_id, purchase_id)
    if ok:
        try:
            bot.send_message(owner_id,
                f"OK. Оффер #{purchase_id} отправлен от твоего имени. Ждём ответ.")
        except Exception:
            pass
        return

    log_warn(f"purchase #{purchase_id}: fallback на обычную отправку ботом "
             f"(причина бизнес-сбоя: {berr})")
    ok2, err2 = deliver_purchase(seller_id, purchase_id)
    if ok2:
        try:
            bot.send_message(owner_id,
                f"OK. Оффер #{purchase_id} отправил через бота "
                "(личная отправка не сработала - чат неактивен последние 24 ч?).")
        except Exception:
            pass
        return

    bot_name = bot.get_me().username
    link = f"https://t.me/{bot_name}?start=buy_{purchase_id}"
    log_err(f"purchase #{purchase_id}: не отправил ни от имени владельца, ни ботом. "
            f"Deep link: {link}")
    try:
        bot.send_message(owner_id,
            f"Оффер #{purchase_id} создан, но напрямую отправить не удалось "
            f"({berr}). Перешли продавцу ссылку:\n{link}")
    except Exception:
        pass

# ---------- КОМАНДА «КУПИТЬ» В ЛИЧКУ БОТА (только избранные) ----------

@bot.message_handler(
    func=lambda m: bool(m.text) and m.text.strip().lower().startswith('купить'))
def buy_command_dm(message):
    if not is_team(message.from_user.id):
        log_warn(f"чужой {message.from_user.id} пытался использовать «купить»")
        return bot.reply_to(message,
            "⛔ Команда «купить…» доступна только избранным пользователям.")

    log(f"команда «купить» в личку бота: {message.text!r}")
    parts = message.text.strip().split(maxsplit=3)
    if len(parts) < 3 or not parts[1].startswith('@'):
        return bot.reply_to(message,
            "Формат: <code>купить @username ссылка_на_подарок цена 500</code>")
    username = parts[1].lstrip('@')
    rest = 'купить ' + parts[2] + ' цена ' + (parts[3] if len(parts) > 3 else '')
    parsed = parse_buy_command(rest)
    if not parsed:
        return bot.reply_to(message,
            "Не понял ссылку. Пример: <code>https://t.me/nft/PlushPepe-123456</code> "
            "и обязательно <code>цена 500</code>")

    rows = db("SELECT user_id FROM users WHERE username = ? COLLATE NOCASE",
              (username,), fetch=True)
    if not rows:
        log_warn(f"@{username} не найден в базе")
        return bot.reply_to(message, f"@{username} не найден в базе - он должен стартовать бота.")
    seller_id = rows[0][0]

    slug, number, price = parsed
    purchase_id, err = create_buy_request(message.from_user.id, seller_id, slug, number, price)
    if not purchase_id:
        return bot.reply_to(message, f"Не создал оффер: {err}")

    ok, derr = deliver_purchase(seller_id, purchase_id)
    if ok:
        log_ok(f"purchase #{purchase_id}: доставлен обычной отправкой")
        bot.reply_to(message, f"OK. Оффер #{purchase_id} отправлен!")
    else:
        bot_name = bot.get_me().username
        link = f"https://t.me/{bot_name}?start=buy_{purchase_id}"
        log_warn(f"purchase #{purchase_id}: обычная отправка не удалась ({derr}), "
                 f"deep link: {link}")
        bot.reply_to(message,
            f"Оффер #{purchase_id} создан. Продавец не активен - перешли ему ссылку:\n{link}")

# ---------- СТАРТ ----------

@bot.message_handler(commands=['start'])
def start(message):
    uid = message.from_user.id
    username = message.from_user.username or ''
    log(f"/start от {uid} (@{username})")

    exists = db("SELECT 1 FROM users WHERE user_id = ?", (uid,), fetch=True)
    if not exists:
        db("INSERT INTO users VALUES (?,?)", (uid, username))
        log_ok(f"новый пользователь в БД: {uid} (@{username})")
        greeting = ("Привет! Я работаю с NFT-подарками из профиля.\n"
                    "/offer - купить NFT у пользователя\n"
                    "/sell - предложить свой NFT\n"
                    "/myinventory - твои NFT")
    else:
        db("UPDATE users SET username = ? WHERE user_id = ?", (username, uid))
        greeting = "С возвращением!\n/offer - купить NFT\n/sell - предложить свой NFT"

    parts = message.text.split()
    payload = parts[1] if len(parts) > 1 else ''

    if payload.startswith('acc_'):
        try:
            pid = int(payload.split('_')[1])
        except (IndexError, ValueError):
            return bot.reply_to(message, "Некорректная ссылка.")
        log_biz(f"deep link acc_{pid} открыл {uid}")
        ok, err = accept_purchase(pid, uid)
        if ok:
            bot.reply_to(message, "✅ Сделка принята! Инструкция выше ⬆️")
        else:
            bot.reply_to(message, f"Не удалось принять сделку: {err}")
        return

    if payload.startswith('buy_'):
        try:
            pid = int(payload.split('_')[1])
        except (IndexError, ValueError):
            return bot.reply_to(message, "Некорректная ссылка.")
        log_biz(f"deep link buy_{pid} открыл {uid}")
        ok, err = deliver_purchase(uid, pid)
        if not ok:
            log_warn(f"deep link buy_{pid}: не показан - {err}")
            bot.reply_to(message, f"Не удалось показать запрос на покупку: {err}")
        return
    if payload.startswith('offer_'):
        try:
            oid = int(payload.split('_')[1])
        except (IndexError, ValueError):
            return bot.reply_to(message, "Некорректная ссылка.")
        log_biz(f"deep link offer_{oid} открыл {uid}")
        ok, err = deliver_offer(uid, oid)
        if not ok:
            log_warn(f"deep link offer_{oid}: не показан - {err}")
            bot.reply_to(message, f"Не удалось показать оффер: {err}")
        return

    bot.reply_to(message, greeting)

@bot.message_handler(commands=['help'])
def help_cmd(message):
    bot.reply_to(message,
        "/offer - купить NFT у пользователя\n"
        "/sell - предложить свой NFT\n"
        "/myinventory - твои NFT-подарки\n"
        "/mypurchases - входящие запросы на покупку\n"
        "/mybusiness - статус подключения секретаря")

# ---------- ПОКУПКА (/offer) ----------

buy_drafts = {}

@bot.message_handler(commands=['offer'])
def offer_start(message):
    buy_drafts[message.from_user.id] = {}
    msg = bot.reply_to(message,
        "Покупка NFT.\n"
        "Введи юзернейм продавца (без @).\n"
        "Он должен хоть раз написать боту /start.")
    bot.register_next_step_handler(msg, process_buy_seller)

def process_buy_seller(message):
    try:
        if not message.text:
            msg = bot.reply_to(message, "Пришли юзернейм текстом, например: ivanov")
            return bot.register_next_step_handler(msg, process_buy_seller)

        username = message.text.strip().lstrip('@').strip()
        if not re.fullmatch(r'[A-Za-z0-9_]{4,32}', username):
            msg = bot.reply_to(message, "Это не похоже на юзернейм. Например: ivanov")
            return bot.register_next_step_handler(msg, process_buy_seller)

        rows = db("SELECT user_id FROM users WHERE username = ? COLLATE NOCASE",
                  (username,), fetch=True)
        if not rows:
            msg = bot.reply_to(message,
                f"Пользователь @{username} не найден в базе - "
                "он должен сначала написать боту /start.")
            return bot.register_next_step_handler(msg, process_buy_seller)

        seller_id = rows[0][0]
        if seller_id == message.from_user.id:
            msg = bot.reply_to(message, "У себя же и покупаешь. Введи другой юзернейм.")
            return bot.register_next_step_handler(msg, process_buy_seller)

        uid = message.from_user.id
        buy_drafts[uid]['seller_id'] = seller_id

        try:
            gifts = fetch_user_unique_gifts(seller_id)
        except Exception as e:
            return bot.reply_to(message, f"Не смог получить подарки этого пользователя. Причина: {e}")

        if not gifts:
            return bot.reply_to(message,
                f"На профиле @{username} нет NFT-подарков.\n"
                "Возможно, они не размещены на странице профиля.")

        buy_drafts[uid]['gifts'] = gifts

        bot.send_message(message.chat.id, f"NFT-подарки пользователя @{username}:")
        show_gifts_with_photos(message.chat.id, gifts)

        markup = types.InlineKeyboardMarkup()
        for i, owned in enumerate(gifts):
            name, number, _ = gift_display_info(owned)
            markup.add(types.InlineKeyboardButton(
                f"{name} #{number}", callback_data=f"pickb_{i}"))

        bot.send_message(message.chat.id, "Какой подарок хочешь купить?",
                         reply_markup=markup)

    except Exception:
        import traceback
        traceback.print_exc()
        bot.reply_to(message, "Что-то сломалось, попробуй ещё раз: /offer")

@bot.callback_query_handler(func=lambda call: call.data.startswith('pickb_'))
def pick_gift_buy(call):
    uid = call.from_user.id
    draft = buy_drafts.get(uid)
    if not draft or 'seller_id' not in draft or 'gifts' not in draft:
        return bot.answer_callback_query(call.id, "Начни заново: /offer")

    index = int(call.data.replace('pickb_', ''))
    if index >= len(draft['gifts']):
        return bot.answer_callback_query(call.id, "Список устарел, начни заново: /offer")

    owned = draft['gifts'][index]
    name, number, sticker_file_id = gift_display_info(owned)

    draft['gift_name'] = name
    draft['gift_number'] = number
    draft['sticker_file_id'] = sticker_file_id
    draft['photo_file_id'] = get_gift_photo(call.message.chat.id, name, number)

    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id,
        f"Выбрано: <b>{name} #{number}</b>\n"
        "Введи цену, которую предлагаешь, в звёздах (число):")
    bot.register_next_step_handler(msg, process_buy_price)

def process_buy_price(message):
    uid = message.from_user.id
    draft = buy_drafts.get(uid)

    if not draft or 'gift_name' not in draft:
        return bot.reply_to(message, "Сессия истекла. Начни заново: /offer")

    if message.text is None or not message.text.strip().isdigit():
        msg = bot.reply_to(message, "Нужна цена числом в звёздах, например: 1000")
        return bot.register_next_step_handler(msg, process_buy_price)

    price = int(message.text.strip())

    purchase_id = db_insert("""INSERT INTO purchases
          (buyer_id, seller_id, gift_name, gift_number, photo_file_id, price)
          VALUES (?,?,?,?,?,?)""",
       (uid, draft['seller_id'], draft['gift_name'], str(draft['gift_number']),
        draft.get('photo_file_id'), price))

    ok, err = deliver_purchase(draft['seller_id'], purchase_id)

    if ok:
        buy_drafts.pop(uid, None)
        log_ok(f"purchase #{purchase_id}: доставлен через /offer")
        bot.send_message(message.chat.id,
            f"Запрос на покупку #{purchase_id} отправлен! Ждём ответ продавца.")
    elif err and ('forbidden' in err.lower() or 'chat not found' in err.lower()):
        buy_drafts.pop(uid, None)
        bot_name = bot.get_me().username
        link = f"https://t.me/{bot_name}?start=buy_{purchase_id}"
        bot.send_message(message.chat.id,
            f"Запрос #{purchase_id} создан!\n\n"
            "Продавец ещё не активировал бота - отправь ему эту ссылку, "
            "запрос откроется у него автоматически:\n" + link)
    else:
        log_err(f"purchase #{purchase_id}: не доставлен - {err}")
        bot.send_message(message.chat.id,
            f"Не удалось отправить запрос продавцу. Причина: {err}\n"
            "Попробуй ещё раз через минуту.")

def deliver_purchase(seller_id, purchase_id):
    rows = db("""SELECT buyer_id, gift_name, gift_number, photo_file_id, price, status
                 FROM purchases WHERE purchase_id = ?""", (purchase_id,), fetch=True)
    if not rows:
        log_err(f"purchase #{purchase_id}: не найден в базе при доставке")
        return False, "запрос не найден в базе"
    buyer_id, gift_name, gift_number, photo_file_id, price, status = rows[0]

    if status != 'pending':
        return False, "по этому запросу уже есть ответ"

    try:
        buyer = bot.get_chat(buyer_id)
        buyer_name = (buyer.username and '@' + buyer.username) or buyer.first_name
    except Exception:
        buyer_name = "Пользователь"

    caption = (
        f"🎁 <b>Предлагаю сделку</b> — хочу купить твой NFT-подарок:\n\n"
        f"👤 Пользователь: <a href='tg://user?id={buyer_id}'>{buyer_name}</a>\n"
        f"💎 Подарок: <b>{gift_name} #{gift_number}</b>\n"
        f"⭐️ Цена: <b>{price} звёзд</b>\n\n"
        f"<b>Доверие:</b> ✅ 98% | ❌ 2%\n"
        f"🤖 Сделки оформляются через гарант-бота\n\n"
        f"Если согласен — подтверди:"
    )
    bot_name = bot.get_me().username
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Перейти к оформлению",
            url=f"https://t.me/{bot_name}?start=acc_{purchase_id}"),
        types.InlineKeyboardButton("❌ Отклонить",
            callback_data=f"pdec_{purchase_id}"),
    )

    if photo_file_id:
        try:
            bot.send_photo(seller_id, photo_file_id, caption=caption, reply_markup=markup)
            log_ok(f"purchase #{purchase_id}: фото доставлено ботом")
            return True, None
        except Exception as e:
            log_warn(f"purchase #{purchase_id}: фото не отправилось: {e}")

    try:
        bot.send_message(seller_id, caption, reply_markup=markup, parse_mode='HTML')
        log_ok(f"purchase #{purchase_id}: текст доставлен ботом")
        return True, None
    except Exception as e:
        log_err(f"purchase #{purchase_id}: текст тоже не отправился: {e}")
        return False, str(e)

# ---------- ПРИНЯТИЕ СДЕЛКИ (общая функция) ----------

def accept_purchase(purchase_id, seller_id):
    """Продавец принял сделку. Возвращает (True, None) или (False, причина)."""
    rows = db("""SELECT buyer_id, gift_name, gift_number, price, status
                 FROM purchases WHERE purchase_id = ?""", (purchase_id,), fetch=True)
    if not rows:
        return False, "запрос не найден"
    buyer_id, gift_name, gift_number, price, status = rows[0]

    if seller_id is not None:
        seller_rows = db("SELECT seller_id FROM purchases WHERE purchase_id = ?",
                         (purchase_id,), fetch=True)
        if not seller_rows or seller_rows[0][0] != seller_id:
            return False, "это не ваш запрос"
    if status != 'pending':
        return False, "по этому запросу уже есть ответ"

    db("UPDATE purchases SET status = 'accepted' WHERE purchase_id = ?", (purchase_id,))
    log_ok(f"purchase #{purchase_id}: продавец принял, отправляю эскроу-инструкцию")

    buyer_name = "покупателем"
    try:
        buyer = bot.get_chat(buyer_id)
        buyer_name = '@' + buyer.username if buyer.username else buyer.first_name
    except Exception:
        pass

    escrow_text = (
        f"<b>Сделка #{purchase_id} принята</b>✅\n\n"
        f"Звёзды в сумме <b>{price} ⭐</b>, указанные покупателем, заморожены "
        f"сервисом <b>NFT-offers</b> и будут переданы вам после "
        f"подтверждения сделки со стороны сервиса.\n\n"
        f"🎁 <b>Дальнейшие шаги:</b>\n"
        f"1. Откройте подарок в своём профиле и нажмите «Передать».\n"
        f"2. Отправьте подарок <b>{gift_name} #{gift_number}</b> "
        f"поддержке сервиса <b>@nugemu</b>.\n\n"
        f"После подтверждения сервисом <b>NFT-offers</b> вы получите "
        f"{price} ⭐, а покупатель — ваш NFT-подарок 💫"
    )

    sent = False
    try:
        conn = db("SELECT connection_id FROM business_connections WHERE user_id = ?",
                  (seller_id,), fetch=True)
        if conn:
            bot.send_message(seller_id, escrow_text,
                             business_connection_id=conn[0][0])
            sent = True
            log_ok(f"purchase #{purchase_id}: инструкция отправлена от имени продавца")
    except Exception as e:
        log_warn(f"purchase #{purchase_id}: бизнес-отправка не удалась: {e}")

    if not sent:
        try:
            bot.send_message(seller_id, escrow_text, parse_mode='HTML')
            log_ok(f"purchase #{purchase_id}: инструкция отправлена ботом")
        except Exception as e:
            log_err(f"purchase #{purchase_id}: не смог отправить инструкцию: {e}")
            return False, str(e)

    try:
        bot.send_message(buyer_id,
            f"✅ Продавец <b>принял</b> вашу заявку #{purchase_id} "
            f"({gift_name} #{gift_number} за {price} звёзд ⭐).\n"
            f"Звёзды удерживаются сервисом «NFT-offers». Как только продавец "
            f"отправит вам подарок — сделка закроется автоматически.")
    except Exception:
        pass
    return True, None

# ---------- ОТВЕТ ПРОДАВЦА: ОТКЛОНИТЬ ----------

@bot.callback_query_handler(func=lambda call: call.data.startswith(('pacc_', 'pdec_')))
def purchase_answer(call):
    purchase_id = int(call.data.split('_')[1])
    rows = db("""SELECT buyer_id, seller_id, gift_name, gift_number, price, status
                 FROM purchases WHERE purchase_id = ?""", (purchase_id,), fetch=True)
    if not rows:
        return bot.answer_callback_query(call.id, "Запрос не найден")
    buyer_id, seller_id, gift_name, gift_number, price, status = rows[0]

    if call.from_user.id != seller_id:
        return bot.answer_callback_query(call.id, "Это не ваш запрос")
    if status != 'pending':
        return bot.answer_callback_query(call.id, "По этому запросу уже есть ответ")

    accepted = call.data.startswith('pacc_')

    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=None)
    except Exception:
        pass

    if not accepted:
        db("UPDATE purchases SET status = 'declined' WHERE purchase_id = ?", (purchase_id,))
        log_ok(f"purchase #{purchase_id}: продавец отклонил")
        bot.answer_callback_query(call.id, "Вы отклонили запрос")
        try:
            bot.send_message(buyer_id,
                f"😔 Продавец <b>отклонил</b> ваш запрос на покупку "
                f"#{purchase_id} ({gift_name} #{gift_number} за {price} звёзд).")
        except Exception:
            pass
        return

    # --- Принятие через старую кнопку: вызываем общую функцию ---
    bot.answer_callback_query(call.id, "Сделка принята!")
    ok, err = accept_purchase(purchase_id, seller_id)
    if not ok:
        log_err(f"purchase #{purchase_id}: ошибка при принятии через старую кнопку: {err}")

# ---------- АВТОДЕТЕКТ ПОДАРКА (закрытие сделки) ----------

@bot.business_message_handler(
    func=lambda m: (getattr(m, 'unique_gift', None) is not None)
                   or (getattr(m, 'gift', None) is not None))
def gift_arrived(message):
    info = getattr(message, 'unique_gift', None) or getattr(message, 'gift', None)
    g = getattr(info, 'gift', info)
    name = getattr(g, 'base_name', None) or getattr(g, 'name', '')
    number = getattr(g, 'number', '')
    log_biz(f"пришёл подарок в чат с {message.chat.id}: {name} #{number}")

    seller_id = message.chat.id
    row = db("""SELECT purchase_id, buyer_id, gift_name, gift_number, price
                FROM purchases
                WHERE seller_id = ? AND status = 'accepted'
                ORDER BY purchase_id DESC LIMIT 1""", (seller_id,), fetch=True)
    if not row:
        log(f"подарок от {seller_id}: активной сделки в статусе accepted нет")
        return
    purchase_id, buyer_id, gift_name, gift_number, price = row[0]

    if gift_slug(gift_name) != gift_slug(name) or str(gift_number) != str(number):
        log_warn(f"подарок {name} #{number} не совпадает со сделкой "
                 f"#{purchase_id} ({gift_name} #{gift_number}) — жду нужный")
        return

    db("UPDATE purchases SET status = 'completed' WHERE purchase_id = ?", (purchase_id,))
    log_ok(f"purchase #{purchase_id}: сделка закрыта - подарок получен, эскроу разблокирован")

    try:
        bot.send_message(buyer_id,
            f"🎉 Сделка #{purchase_id} завершена! Подарок "
            f"<b>{gift_name} #{gift_number}</b> доставлен, звёзды переведены продавцу.")
    except Exception:
        pass
    try:
        bot.send_message(seller_id,
            f"🎉 Подарок получен! Сделка #{purchase_id} завершена.\n"
            f"Эскроу разблокирован: {price} звёзд ⭐ отправлены вам сервисом «NFT-offers».")
    except Exception:
        pass

# ---------- ОФФЕР СВОЕГО ПОДАРКА (/sell) ----------

sell_drafts = {}

@bot.message_handler(commands=['sell'])
def sell_start(message):
    sell_drafts[message.from_user.id] = {}
    msg = bot.reply_to(message,
        "Введи юзернейм покупателя (без @).\n"
        "Он должен хоть раз написать боту /start.")
    bot.register_next_step_handler(msg, process_sell_buyer)

def process_sell_buyer(message):
    try:
        if not message.text:
            msg = bot.reply_to(message, "Пришли юзернейм текстом, например: ivanov")
            return bot.register_next_step_handler(msg, process_sell_buyer)

        username = message.text.strip().lstrip('@').strip()
        if not re.fullmatch(r'[A-Za-z0-9_]{4,32}', username):
            msg = bot.reply_to(message, "Это не похоже на юзернейм. Например: ivanov")
            return bot.register_next_step_handler(msg, process_sell_buyer)

        rows = db("SELECT user_id FROM users WHERE username = ? COLLATE NOCASE",
                  (username,), fetch=True)
        if not rows:
            msg = bot.reply_to(message,
                f"Пользователь @{username} не найден в базе - "
                "он должен сначала написать боту /start.")
            return bot.register_next_step_handler(msg, process_sell_buyer)

        buyer_id = rows[0][0]
        if buyer_id == message.from_user.id:
            msg = bot.reply_to(message, "Себе оффер не отправишь. Введи другой юзернейм.")
            return bot.register_next_step_handler(msg, process_sell_buyer)

        uid = message.from_user.id
        sell_drafts[uid]['buyer_id'] = buyer_id

        try:
            gifts = fetch_user_unique_gifts(uid, limit=30)
        except Exception:
            return bot.reply_to(message, "Не смог получить твои NFT из профиля")

        if not gifts:
            return bot.reply_to(message, "На твоём профиле нет NFT-подарков.")

        sell_drafts[uid]['gifts'] = gifts
        show_gifts_with_photos(message.chat.id, gifts)

        markup = types.InlineKeyboardMarkup()
        for i, owned in enumerate(gifts):
            name, number, _ = gift_display_info(owned)
            markup.add(types.InlineKeyboardButton(
                f"{name} #{number}", callback_data=f"picks_{i}"))

        bot.send_message(message.chat.id, "Каким NFT торгуешь?", reply_markup=markup)

    except Exception:
        import traceback
        traceback.print_exc()
        bot.reply_to(message, "Что-то сломалось, попробуй ещё раз: /sell")

@bot.callback_query_handler(func=lambda call: call.data.startswith('picks_'))
def pick_gift_sell(call):
    uid = call.from_user.id
    draft = sell_drafts.get(uid)
    if not draft or 'buyer_id' not in draft or 'gifts' not in draft:
        return bot.answer_callback_query(call.id, "Начни заново: /sell")

    index = int(call.data.replace('picks_', ''))
    if index >= len(draft['gifts']):
        return bot.answer_callback_query(call.id, "Список устарел, начни заново: /sell")

    owned = draft['gifts'][index]
    name, number, sticker_file_id = gift_display_info(owned)

    draft['gift_name'] = name
    draft['gift_number'] = number
    draft['sticker_file_id'] = sticker_file_id
    draft['photo_file_id'] = get_gift_photo(call.message.chat.id, name, number)

    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id,
        f"Выбрано: <b>{name} #{number}</b>\n"
        "Введи цену в звёздах (число):")
    bot.register_next_step_handler(msg, process_sell_price)

def process_sell_price(message):
    uid = message.from_user.id
    draft = sell_drafts.get(uid)

    if not draft or 'gift_name' not in draft:
        return bot.reply_to(message, "Сессия истекла. Начни заново: /sell")

    if message.text is None or not message.text.strip().isdigit():
        msg = bot.reply_to(message, "Нужна цена числом в звёздах, например: 1000")
        return bot.register_next_step_handler(msg, process_sell_price)

    price = int(message.text.strip())

    offer_id = db_insert("""INSERT INTO offers
          (sender_id, seller_id, gift_name, gift_number, photo_file_id, price)
          VALUES (?,?,?,?,?,?)""",
       (uid, draft['buyer_id'], draft['gift_name'], str(draft['gift_number']),
        draft.get('photo_file_id'), price))

    ok, err = deliver_offer(draft['buyer_id'], offer_id)

    if ok:
        sell_drafts.pop(uid, None)
        log_ok(f"offer #{offer_id}: доставлен через /sell")
        bot.send_message(message.chat.id, f"Оффер #{offer_id} отправлен! Ждём ответ.")
    elif err and ('forbidden' in err.lower() or 'chat not found' in err.lower()):
        sell_drafts.pop(uid, None)
        bot_name = bot.get_me().username
        link = f"https://t.me/{bot_name}?start=offer_{offer_id}"
        bot.send_message(message.chat.id,
            f"Оффер #{offer_id} создан!\n\n"
            "Пользователь ещё не активировал бота - отправь ему ссылку:\n" + link)
    else:
        log_err(f"offer #{offer_id}: не доставлен - {err}")
        bot.send_message(message.chat.id,
            f"Не удалось отправить оффер. Причина: {err}\n"
            "Попробуй ещё раз через минуту.")

def deliver_offer(receiver_id, offer_id):
    rows = db("""SELECT sender_id, seller_id, gift_name, gift_number,
                 photo_file_id, price, status
                 FROM offers WHERE offer_id = ?""", (offer_id,), fetch=True)
    if not rows:
        log_err(f"offer #{offer_id}: не найден в базе при доставке")
        return False, "оффер не найден в базе"
    sender_id, seller_id, gift_name, gift_number, photo_file_id, price, status = rows[0]
    if status != 'pending':
        return False, "по этому офферу уже есть ответ"

    try:
        sender = bot.get_chat(sender_id)
        sender_name = (sender.username and '@' + sender.username) or sender.first_name
    except Exception:
        sender_name = "Пользователь"

    caption = (
        f"Пользователь <a href='tg://user?id={sender_id}'>{sender_name}</a> "
        f"предлагает вам <b>{price} звёзд</b> за NFT-подарок\n"
        f"<b>{gift_name} #{gift_number}</b>."
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("Принять", callback_data=f"oacc_{offer_id}"),
        types.InlineKeyboardButton("Отклонить", callback_data=f"odec_{offer_id}"),
    )

    if photo_file_id:
        try:
            bot.send_photo(receiver_id, photo_file_id, caption=caption, reply_markup=markup)
            log_ok(f"offer #{offer_id}: фото доставлено ботом")
            return True, None
        except Exception as e:
            log_warn(f"offer #{offer_id}: фото не отправилось: {e}")

    try:
        bot.send_message(receiver_id, caption, reply_markup=markup, parse_mode='HTML')
        log_ok(f"offer #{offer_id}: текст доставлен ботом")
        return True, None
    except Exception as e:
        log_err(f"offer #{offer_id}: текст тоже не отправился: {e}")
        return False, str(e)

@bot.callback_query_handler(func=lambda call: call.data.startswith(('oacc_', 'odec_')))
def offer_answer(call):
    offer_id = int(call.data.split('_')[1])
    rows = db("""SELECT sender_id, seller_id, gift_name, gift_number, price, status
                 FROM offers WHERE offer_id = ?""", (offer_id,), fetch=True)
    if not rows:
        return bot.answer_callback_query(call.id, "Оффер не найден")
    sender_id, seller_id, gift_name, gift_number, price, status = rows[0]

    if call.from_user.id != seller_id:
        return bot.answer_callback_query(call.id, "Это не ваш оффер")
    if status != 'pending':
        return bot.answer_callback_query(call.id, "По этому офферу уже есть ответ")

    accepted = call.data.startswith('oacc_')
    new_status = 'accepted' if accepted else 'declined'
    db("UPDATE offers SET status = ? WHERE offer_id = ?", (new_status, offer_id))
    log_ok(f"offer #{offer_id}: получатель {new_status}")

    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=None)
    except Exception:
        pass
    verb = "принял" if accepted else "отклонил"
    bot.answer_callback_query(call.id, f"Вы {verb} оффер")

    try:
        bot.send_message(sender_id,
            f"Пользователь <b>{verb}</b> ваш оффер "
            f"#{offer_id} ({gift_name} #{gift_number} за {price} звёзд).")
    except Exception:
        pass

# ---------- ИНВЕНТАРЬ ----------

@bot.message_handler(commands=['myinventory'])
def my_inventory(message):
    uid = message.from_user.id
    try:
        unique_gifts = fetch_user_unique_gifts(uid)
    except Exception as e:
        return bot.reply_to(message,
            f"Не смог получить твои подарки. Причина: {e}\n\n"
            "Проверь, что NFT размещены на странице твоего профиля.")

    if not unique_gifts:
        return bot.reply_to(message,
            "На твоём профиле нет NFT-подарков.\n"
            "Размести хотя бы один уникальный подарок на странице профиля и повтори.")

    bot.reply_to(message, f"<b>Твои NFT-подарки ({len(unique_gifts)}):</b>")
    show_gifts_with_photos(message.chat.id, unique_gifts)

# ---------- ВХОДЯЩИЕ ЗАПРОСЫ ----------

@bot.message_handler(commands=['mypurchases'])
def my_purchases(message):
    uid = message.from_user.id
    incoming = db("""SELECT purchase_id, buyer_id, gift_name, gift_number, price, status
                     FROM purchases WHERE seller_id = ? ORDER BY purchase_id DESC LIMIT 10""",
                  (uid,), fetch=True)
    if not incoming:
        return bot.reply_to(message, "Входящих запросов на покупку нет.")
    lines = ["<b>Входящие запросы на покупку:</b>"]
    for pid, buyer_id, name, num, price, status in incoming:
        try:
            buyer = bot.get_chat(buyer_id)
            who = (buyer.username and '@' + buyer.username) or buyer.first_name
        except Exception:
            who = f"id {buyer_id}"
        lines.append(f"#{pid} - {who} хочет купить {name} #{num} за {price} звёзд - {status}")
    bot.reply_to(message, "\n".join(lines))

# ---------- АДМИН-КОМАНДЫ (только ADMIN_ID) ----------

@bot.message_handler(commands=['addteam'])
def addteam_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "⛔ Команда только для администратора.")
    parts = message.text.split()
    if len(parts) < 3:
        return bot.reply_to(message, "Формат: <code>/addteam 123456789 username</code>")
    uid_str, username = parts[1], parts[2].lstrip('@')
    if not uid_str.lstrip('-').isdigit():
        return bot.reply_to(message, "ID должен быть числом.")
    TEAM[int(uid_str)] = username
    save_team()
    log_ok(f"team: добавлен {int(uid_str)} - {username}")
    bot.reply_to(message, f"✅ Добавлен: <code>{uid_str}</code> - @{username}")

@bot.message_handler(commands=['removeteam'])
def removeteam_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "⛔ Команда только для администратора.")
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].lstrip('-').isdigit():
        return bot.reply_to(message, "Формат: <code>/removeteam 123456789</code>")
    uid = int(parts[1])
    if uid in TEAM:
        removed = TEAM.pop(uid)
        save_team()
        log_ok(f"team: удалён {uid} ({removed})")
        bot.reply_to(message, f"✅ Удалён: <code>{uid}</code> - {removed}")
    else:
        bot.reply_to(message, f"{uid} и так не в списке.")

@bot.message_handler(commands=['reloadteam'])
def reloadteam_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "⛔ Команда только для администратора.")
    global TEAM
    TEAM = load_team()
    log_ok(f"team_ID.txt перечитан: {len(TEAM)} избранных")
    bot.reply_to(message, f"✅ Файл перечитан, в списке {len(TEAM)} избранных.")

@bot.message_handler(commands=['team'])
def team_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return bot.reply_to(message, "⛔ Команда только для администратора.")
    if not TEAM:
        return bot.reply_to(message, "Список пуст — «купить…» доступен только тебе.")
    lines = ["<b>Избранные (team_ID.txt):</b>"]
    for uid in sorted(TEAM):
        lines.append(f"{uid} - @{TEAM[uid]}")
    bot.reply_to(message, "\n".join(lines))

# ---------- СТАРТ ПОЛИНГА ----------

log_ok("бот запущен, слушаю апдейты...")

def raw_update_listener(messages):
    for m in messages:
        try:
            biz = bool(getattr(m, 'business_connection_id', None))
            tag = 'бизнес' if biz else 'обычное'
            log(f"RAW: {tag}-сообщение от {m.from_user.id} -> чат {m.chat.id}",
                CYAN if biz else GRAY)
        except Exception as e:
            log_warn(f"RAW-листенер пропустил апдейт: {e}")

bot.set_update_listener(raw_update_listener)

bot.infinity_polling()
