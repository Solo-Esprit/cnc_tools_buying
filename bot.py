import logging
import os
import time
from typing import List, Optional
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
import re

# === НАСТРОЙКИ ===
TOKEN = os.environ["TELEGRAM_TOKEN"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
PORT = int(os.environ.get("PORT", 10000))

# === GOOGLE AUTH ===
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"  # Обязательно для add_worksheet!
]

# === ЛОГИРОВАНИЕ ===
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# === КЛАСС ДЛЯ РАБОТЫ С GOOGLE ТАБЛИЦЕЙ ===
class GoogleSheetsManager:
    def __init__(self, sheet_id: str, credentials_json: str):
        creds_dict = json.loads(credentials_json)
        credentials = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        gc = gspread.authorize(credentials)
        self.sheet = gc.open_by_key(sheet_id)
        self._worksheet_cache = {}  # кэш для листов

    def get_worksheet(self, chat_id: int):
        if chat_id in self._worksheet_cache:
            return self._worksheet_cache[chat_id]
        try:
            ws = self.sheet.worksheet(str(chat_id))
            self._worksheet_cache[chat_id] = ws
            return ws
        except gspread.WorksheetNotFound:
            try:
                ws = self.sheet.add_worksheet(title=str(chat_id), rows="100", cols="2")
                ws.update('A1', 'Артикул')
                self._worksheet_cache[chat_id] = ws
                logging.info("🆕 Создан лист для чата %s", chat_id)
                return ws
            except Exception as e:
                logging.error("💥 Не удалось создить лист для чата %s: %s", chat_id, e, exc_info=True)
                raise
        except Exception as e:
            logging.error("💥 Ошибка доступа к таблице при получении листа %s: %s", chat_id, e, exc_info=True)
            raise

    def get_items(self, chat_id: int) -> List[str]:
        try:
            ws = self.get_worksheet(chat_id)
            items = [item for item in ws.col_values(1)[1:] if item.strip()]
            logging.info("📋 Получено %d позиций для чата %s", len(items), chat_id)
            return items
        except Exception as e:
            logging.error("💥 Ошибка получения списка для чата %s: %s", chat_id, e, exc_info=True)
            return []

    def add_item(self, chat_id: int, item: str, quantity: int = 1):
        try:
            ws = self.get_worksheet(chat_id)
            # Проверяем, есть ли уже такой артикул (без количества)
            items = self.get_items(chat_id)
            existing_index = None
            existing_quantity = 0
            for i, row in enumerate(items):
                # Извлекаем артикул и количество из строки "Артикул (N)"
                parsed_item, parsed_qty = self._parse_item(row)
                if parsed_item == item:
                    existing_index = i
                    existing_quantity = parsed_qty
                    break

            if existing_index is not None:
                # Обновляем строку: складываем количество
                new_quantity = existing_quantity + quantity
                new_row_value = f"{item} ({new_quantity})" if new_quantity > 1 else item
                # Обновляем ячейку (строка = индекс + 2, т.к. A1 = заголовок)
                cell = f"A{existing_index + 2}"
                ws.update(cell, [new_row_value])
                logging.info("🔄 Обновлена позиция: чат %s, '%s' -> '%s'", chat_id, items[existing_index], new_row_value)
            else:
                # Добавляем новую строку
                row_value = f"{item} ({quantity})" if quantity > 1 else item
                ws.append_row([row_value])
                logging.info("✅ Запись в Google Таблицу: чат %s, '%s'", chat_id, row_value)
        except Exception as e:
            logging.error("💥 Ошибка записи в Google Таблицу для чата %s: %s", chat_id, e, exc_info=True)
            raise

    def remove_item(self, chat_id: int, index: int):
        try:
            start = time.perf_counter()
            ws = self.get_worksheet(chat_id)
            ws.delete_rows(index + 2)
            logging.info("🗑️ Удалена позиция %d из чата %s (время: %.2fс)", index, chat_id, time.perf_counter() - start)
        except Exception as e:
            logging.error("💥 Ошибка удаления из таблицы для чата %s: %s", chat_id, e, exc_info=True)
            raise

    def _parse_item(self, row: str):
        """Извлекает артикул и количество из строки 'Артикул (N)' или 'Артикул'"""
        match = re.match(r'^(.+?)\s*\((\d+)\)\s*$', row.strip())
        if match:
            item = match.group(1).strip()
            quantity = int(match.group(2))
            return item, quantity
        else:
            return row.strip(), 1


# === ИНИЦИАЛИЗАЦИЯ МЕНЕДЖЕРА ===
gs_manager = GoogleSheetsManager(SHEET_ID, CREDENTIALS_JSON)

# === ОБРАБОТЧИКИ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logging.info("✅ Обработчик /start вызван для %s", user.id)
    await update.message.reply_text(
        "🛒 Бот для закупок\n\n"
        "Команды:\n"
        "/add <артикул> (кол-во) — добавить (например: /add Ключ 10мм (5) или /add 12345-KEY (2))\n"
        "/list — показать список\n"
        "/clear — очистить\n"
        "/stats — статистика\n"
        "/help — помощь"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Помощь по боту:\n\n"
        "/start — приветствие\n"
        "/add <артикул> (кол-во) — добавить позицию (например: /add Ключ 10мм (5))\n"
        "/list — показать список\n"
        "/clear — очистить список\n"
        "/stats — статистика\n"
        "/help — это сообщение"
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    items = gs_manager.get_items(chat_id)
    total_items = sum(gs_manager._parse_item(item)[1] for item in items)
    await update.message.reply_text(f"📊 Всего позиций в списке: {len(items)}\n"
                                    f"📦 Общее количество: {total_items}")

async def add_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    args = context.args
    logging.info("📥 /add от %s в чате %s: args=%s", user.id, chat_id, args)
    if not args:
        await update.message.reply_text("UsageId: /add <артикул> (кол-во)\nПример: /add Ключ 10мм (5)")
        return

    full_text = " ".join(args)
    # Ищем паттерн " (N)" в конце строки
    match = re.search(r'\s*\((\d+)\)\s*$', full_text)
    if match:
        quantity_str = match.group(1)
        quantity = int(quantity_str)
        item = full_text[:match.start()].strip() # текст до "(N)"
        if not item:
            await update.message.reply_text("Артикул не может быть пустым.")
            return
    else:
        quantity = 1
        item = full_text.strip()
        if not item:
            await update.message.reply_text("Артикул не может быть пустым.")
            return

    try:
        gs_manager.add_item(chat_id, item, quantity)
        formatted_item = f"{item} ({quantity})" if quantity > 1 else item
        await update.message.reply_text(f"✅ Добавлено: {formatted_item}")
    except gspread.exceptions.APIError:
        await update.message.reply_text("❌ Ошибка Google API. Проверьте права доступа.")
    except Exception as e:
        await update.message.reply_text("❌ Неизвестная ошибка. Попробуйте позже.")
        logging.error("Ошибка в /add: %s", e)

async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        items = gs_manager.get_items(chat_id)
        if not items:
            await update.message.reply_text("Список пуст 🛒")
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = [[InlineKeyboardButton(f"✅ Куплено: {item}", callback_data=f"remove_{i}")]
                    for i, item in enumerate(items)]
        await update.message.reply_text("Список закупок:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text("❌ Ошибка при загрузке списка.")
        logging.error("Ошибка в /list: %s", e)

async def clear_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        ws = gs_manager.get_worksheet(chat_id)
        rows = len(ws.col_values(1))
        if rows > 1:
            ws.delete_rows(2, rows)
        await update.message.reply_text("Список очищен 🧹")
        logging.info("🧹 Список очищен для чата %s", chat_id)
    except Exception as e:
        await update.message.reply_text("❌ Ошибка при очистке.")
        logging.error("Ошибка в /clear: %s", e)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data
    if data.startswith("remove_"):
        try:
            index = int(data.split("_")[1])
            items = gs_manager.get_items(chat_id)
            if 0 <= index < len(items):
                gs_manager.remove_item(chat_id, index)
                await query.edit_message_text(f"✅ Убрано: {items[index]}")
            else:
                await query.edit_message_text("Позиция уже удалена.")
        except Exception as e:
            await query.edit_message_text("❌ Ошибка удаления.")
            logging.error("Ошибка в кнопке: %s", e)

# === МЕЖПОТОЧНАЯ ОЧЕРЕДЬ ===
cross_thread_queue = queue.Queue()
bot_instance = None

# === ФОНОВЫЙ TELEGRAM WORKER ===
async def telegram_worker():
    global bot_instance
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("add", add_item))
    app.add_handler(CommandHandler("list", show_list))
    app.add_handler(CommandHandler("clear", clear_list))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(button_handler))

    await app.initialize()
    await app.start()
    bot_instance = app.bot
    logging.info("✅ Telegram worker запущен")

    # Устанавливаем webhook
    hostname = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
    if not hostname:
        logging.error("❌ RENDER_EXTERNAL_HOSTNAME не установлено!")
        return
    BOT_ID = TOKEN.split(':')[0]
    webhook_url = f"https://{hostname}/webhook-{BOT_ID}"
    await app.bot.set_webhook(url=webhook_url)
    logging.info(f"✅ Webhook установлен: {webhook_url}")

    # Обработка обновлений из очереди
    while True:
        try:
            update = cross_thread_queue.get(timeout=1)
            await app.process_update(update)
        except queue.Empty:
            continue
        except Exception as e:
            logging.error("Ошибка обработки обновления в worker: %s", e)

def run_telegram_worker():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(telegram_worker())

# === FLASK ===
flask_app = Flask(__name__)

@flask_app.route("/webhook-<bot_id>", methods=["POST"])
def telegram_webhook(bot_id):
    expected_id = TOKEN.split(':')[0]
    if bot_id != expected_id:
        logging.warning("⚠️ Неверный bot_id: %s", bot_id)
        return "OK"
    json_data = request.get_json()
    if json_data is None:
        logging.warning("⚠️ Пустой JSON")
        return "OK"
    update_id = json_data.get("update_id", "unknown")
    logging.info("📥 Получено обновление: %s", update_id)
    try:
        update = Update.de_json(json_data, bot_instance)
        cross_thread_queue.put(update)
    except Exception as e:
        logging.error("💥 Ошибка при постановке в очередь: %s", e, exc_info=True)
    return "OK"

@flask_app.route("/")
def hello():
    return "✅ Telegram Purchase Bot is running on Render"

# === MAIN ===
def main():
    # Запускаем Telegram в фоне
    threading.Thread(target=run_telegram_worker, daemon=True).start()
    # Ждём инициализации bot_instance
    import time
    while bot_instance is None:
        time.sleep(0.1)
    # Запускаем Flask на 0.0.0.0:PORT (обязательно для Render)
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == '__main__':
    main()
