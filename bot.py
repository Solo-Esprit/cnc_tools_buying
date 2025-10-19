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

# === ФУНКЦИИ РАБОТЫ С ТАБЛИЦЕЙ ===
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
    await update.message.reply_text(
        "🛒 Бот для закупок\n\n"
        "Команды:\n"
        "/add <артикул> — добавить\n"
        "/list — показать список\n"
        "/clear — очистить"
    )

async def add_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args
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
            logging.error("Ошибка: %s", e)
            await query.edit_message_text("Ошибка удаления.")

# === ГЛАВНАЯ ФУНКЦИЯ ===
def main():
    # Создаём Flask-приложение
    flask_app = Flask(__name__)

    @flask_app.route(f"/{TOKEN}", methods=["POST"])
    def telegram_webhook():
        # Передаём обновление в очередь бота
        flask_app.bot_app.update_queue.put_nowait(
            Update.de_json(request.get_json(force=True), flask_app.bot_app.bot)
        )
        return "OK"

    @flask_app.route("/")
    def hello():
        return "Telegram Purchase Bot is running!"

    # Инициализация Telegram-бота
    bot_app = Application.builder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("add", add_item))
    bot_app.add_handler(CommandHandler("list", show_list))
    bot_app.add_handler(CommandHandler("clear", clear_list))
    bot_app.add_handler(CallbackQueryHandler(button_handler))

    # Сохраняем ссылку на bot_app в Flask-приложении
    flask_app.bot_app = bot_app

    # Устанавливаем webhook и запускаем бота в фоне
    import asyncio
    from threading import Thread

    def run_bot():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Устанавливаем webhook
        webhook_url = f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}/{TOKEN}"
        loop.run_until_complete(bot_app.bot.set_webhook(url=webhook_url))
        logging.info(f"Webhook установлен на: {webhook_url}")

        # Запускаем polling в фоне (на самом деле webhook обрабатывает всё)
        bot_app.run_polling(close_loop=False)

    # Запускаем бота в отдельном потоке
    Thread(target=run_bot, daemon=True).start()

    # Запускаем Flask
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == '__main__':
    main()
