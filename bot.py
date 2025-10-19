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

# === НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ===
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

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ РАБОТЫ С ТАБЛИЦЕЙ ===
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
    logging.info("👤 Пользователь %s (%s) вызвал /start", user.full_name, user.id)
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
    logging.info("📥 /add от %s в чате %s: args=%s", user.id, chat_id, args)
    if not args:
        await update.message.reply_text("UsageId: /add <артикул>")
        return
    item = " ".join(args).strip()
    if not item:
        await update.message.reply_text("Артикул не может быть пустым.")
        return
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
            await query.edit_message_text("Ошибка при удалении.")

# === ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ===
app = None
loop = None
flask_app = Flask(__name__)

# === WEBHOOK ENDPOINT ===
@flask_app.route("/webhook-<bot_id>", methods=["POST"])
def telegram_webhook(bot_id):
    expected_id = TOKEN.split(':')[0]
    if bot_id != expected_id:
        logging.warning("⚠️ Неверный bot_id в webhook: %s", bot_id)
        return "OK"

    json_data = request.get_json()
    if json_data is None:
        logging.warning("⚠️ Получен пустой JSON")
        return "OK"

    logging.info("📥 Получено обновление: %s", json_data)

    try:
        update = Update.de_json(json_data, app.bot)
        asyncio.run_coroutine_threadsafe(app.process_update(update), loop)
        logging.info("📤 Обновление передано в обработку")
    except Exception as e:
        logging.error("💥 Ошибка при обработке обновления: %s", e, exc_info=True)
    return "OK"

@flask_app.route("/")
def hello():
    return "🛒 Telegram Purchase Bot is running!"

# === ИНИЦИАЛИЗАЦИЯ TELEGRAM APPLICATION ===
async def init_telegram_app():
    global app
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_item))
    app.add_handler(CommandHandler("list", show_list))
    app.add_handler(CommandHandler("clear", clear_list))
    app.add_handler(CallbackQueryHandler(button_handler))
    await app.initialize()
    logging.info("✅ Telegram Application initialized")

# === ОСНОВНАЯ ФУНКЦИЯ ===
def main():
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Инициализируем Telegram-приложение
    loop.run_until_complete(init_telegram_app())

    # Устанавливаем webhook
    BOT_ID = TOKEN.split(':')[0]
    webhook_url = f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}/webhook-{BOT_ID}"
    loop.run_until_complete(app.bot.set_webhook(url=webhook_url))
    logging.info(f"✅ Webhook установлен: {webhook_url}")

    # Запускаем Flask
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == '__main__':
    main()
