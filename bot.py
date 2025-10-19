import logging
import os
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
import gspread
from google.oauth2.service_account import Credentials
import json
from flask import Flask, request
import asyncio
import threading
import queue

# === НАСТРОЙКИ ===
TOKEN = os.environ["TELEGRAM_TOKEN"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
PORT = int(os.environ.get("PORT", 10000))

# === GOOGLE AUTH ===
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds_dict = json.loads(CREDENTIALS_JSON)
credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(credentials)
sheet = gc.open_by_key(SHEET_ID)

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def get_worksheet(chat_id: int):
    try:
        return sheet.worksheet(str(chat_id))
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=str(chat_id), rows="100", cols="2")
        ws.update('A1', 'Артикул')
        return ws

def get_items(chat_id: int):
    ws = get_worksheet(chat_id)
    return [item for item in ws.col_values(1)[1:] if item.strip()]

def add_item_to_sheet(chat_id: int, item: str):
    get_worksheet(chat_id).append_row([item])

def remove_item_from_sheet(chat_id: int, index: int):
    ws = get_worksheet(chat_id)
    ws.delete_rows(index + 2)

# === ОБРАБОТЧИКИ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logging.info("✅ Обработчик /start вызван для %s", user.id)
    await update.message.reply_text(
        "🛒 Бот для закупок\n\n"
        "Команды:\n"
        "/add <артикул> — добавить\n"
        "/list — показать список\n"
        "/clear — очистить"
    )

async def add_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    args = context.args
    logging.info("📥 /add от %s: %s", user.id, args)
    if not args:
        await update.message.reply_text("UsageId: /add <артикул>")
        return
    item = " ".join(args).strip()
    add_item_to_sheet(chat_id, item)
    await update.message.reply_text(f"✅ Добавлено: {item}")

async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    items = get_items(chat_id)
    if not items:
        await update.message.reply_text("Список пуст 🛒")
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = [[InlineKeyboardButton(f"✅ Куплено: {item}", callback_data=f"remove_{i}")]
                for i, item in enumerate(items)]
    await update.message.reply_text("Список закупок:", reply_markup=InlineKeyboardMarkup(keyboard))

async def clear_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ws = get_worksheet(chat_id)
    rows = len(ws.col_values(1))
    if rows > 1:
        ws.delete_rows(2, rows)
    await update.message.reply_text("Список очищен 🧹")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data
    if data.startswith("remove_"):
        try:
            index = int(data.split("_")[1])
            items = get_items(chat_id)
            if 0 <= index < len(items):
                remove_item_from_sheet(chat_id, index)
                await query.edit_message_text(f"✅ Убрано: {items[index]}")
            else:
                await query.edit_message_text("Позиция не найдена.")
        except Exception as e:
            logging.error("Ошибка удаления: %s", e)

# === МЕЖПОТОЧНАЯ ОЧЕРЕДЬ ===
cross_thread_queue = queue.Queue()

# === ФОНОВЫЙ TELEGRAM WORKER ===
async def telegram_worker():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_item))
    app.add_handler(CommandHandler("list", show_list))
    app.add_handler(CommandHandler("clear", clear_list))
    app.add_handler(CallbackQueryHandler(button_handler))

    await app.initialize()
    await app.start()
    logging.info("✅ Telegram worker запущен")

    # Устанавливаем webhook
    BOT_ID = TOKEN.split(':')[0]
    webhook_url = f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}/webhook-{BOT_ID}"
    await app.bot.set_webhook(url=webhook_url)
    logging.info(f"✅ Webhook установлен: {webhook_url}")

    # Передаём app.bot в глобальную область для Flask
    global bot_instance
    bot_instance = app.bot

    # Цикл: читаем из thread-safe очереди и обрабатываем
    while True:
        try:
            update = cross_thread_queue.get(timeout=1)
            await app.process_update(update)
        except queue.Empty:
            continue
        except Exception as e:
            logging.error("Ошибка в worker: %s", e)

def run_telegram_worker():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(telegram_worker())

# === FLASK ===
bot_instance = None
flask_app = Flask(__name__)

@flask_app.route("/webhook-<bot_id>", methods=["POST"])
def telegram_webhook(bot_id):
    expected_id = TOKEN.split(':')[0]
    if bot_id != expected_id:
        return "OK"
    json_data = request.get_json()
    if json_data is None:
        return "OK"
    update_id = json_data.get("update_id", "unknown")
    logging.info("📥 Получено обновление: %s", update_id)
    try:
        update = Update.de_json(json_data, bot_instance)
        cross_thread_queue.put(update)  # ✅ thread-safe!
    except Exception as e:
        logging.error("Ошибка при постановке в очередь: %s", e)
    return "OK"

@flask_app.route("/")
def hello():
    return "✅ Bot is running"

# === MAIN ===
def main():
    # Запускаем Telegram в фоне
    threading.Thread(target=run_telegram_worker, daemon=True).start()
    # Ждём, пока bot_instance инициализируется
    import time
    while bot_instance is None:
        time.sleep(0.1)
    # Запускаем Flask
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == '__main__':
    main()
