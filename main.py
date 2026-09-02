import asyncio
import os
import sqlite3
import time
import logging
import sys
from contextlib import contextmanager
from typing import List, Optional, Callable
from functools import wraps

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton,
    InputMediaPhoto, ReplyKeyboardRemove, FSInputFile,
    BotCommand, BotCommandScopeDefault
)
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter, TelegramNetworkError
from aiogram.utils.media_group import MediaGroupBuilder
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

# ========== КОНФИГУРАЦИЯ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

# Получаем список админов из .env (через запятую)
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "5126213888").split(",")]

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env!")
if not CHANNEL_ID:
    raise ValueError("CHANNEL_ID не найден в .env!")
if not ADMIN_IDS:
    raise ValueError("ADMIN_IDS не найден в .env!")

logger.info(f"Администраторы: {ADMIN_IDS}")

# ========== ГЛОБАЛЬНЫЙ ФЛАГ ДЛЯ УПРАВЛЕНИЯ БОТОМ ==========
bot_running = True

# ========== БАЗА ДАННЫХ ==========
class Database:
    """Класс для работы с базой данных SQLite"""
    
    def __init__(self, db_path: str = "bot_data.db"):
        self.db_path = db_path
        self._init_db()
    
    @contextmanager
    def get_connection(self):
        """Контекстный менеджер для подключения к БД"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    
    def _init_db(self):
        """Создание таблиц при первом запуске и миграция"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Создаем таблицу users
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    joined_at INTEGER
                )
            """)
            
            # Проверяем существование таблицы clicks
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='clicks'")
            table_exists = cursor.fetchone()
            
            if not table_exists:
                cursor.execute("""
                    CREATE TABLE clicks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        button_type TEXT,
                        button_label TEXT,
                        clicked_at INTEGER
                    )
                """)
                logger.info("Таблица clicks создана с новыми полями")
            else:
                cursor.execute("PRAGMA table_info(clicks)")
                columns = [col[1] for col in cursor.fetchall()]
                
                if 'button_label' not in columns:
                    cursor.execute("ALTER TABLE clicks ADD COLUMN button_label TEXT")
                    logger.info("Добавлена колонка button_label в таблицу clicks")
                
                if 'button_type' not in columns:
                    cursor.execute("ALTER TABLE clicks ADD COLUMN button_type TEXT")
                    logger.info("Добавлена колонка button_type в таблицу clicks")
                
                if 'clicked_at' not in columns:
                    cursor.execute("ALTER TABLE clicks ADD COLUMN clicked_at INTEGER")
                    logger.info("Добавлена колонка clicked_at в таблицу clicks")
            
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_clicks_user_id ON clicks(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_clicks_button_type ON clicks(button_type)")
            
            conn.commit()
            logger.info("База данных инициализирована")
    
    def save_user(self, user_id: int, username: Optional[str], first_name: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO users (user_id, username, first_name, joined_at) VALUES (?, ?, ?, ?)",
                (user_id, username, first_name, int(time.time()))
            )
            conn.commit()
    
    def get_all_users(self) -> List[int]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users")
            return [row[0] for row in cursor.fetchall()]
    
    def get_users_count(self) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            return cursor.fetchone()[0]
    
    def save_click(self, user_id: int, button_type: str, button_label: str = ""):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO clicks (user_id, button_type, button_label, clicked_at) VALUES (?, ?, ?, ?)",
                (user_id, button_type, button_label, int(time.time()))
            )
            conn.commit()
    
    def get_clicks_count(self, button_type: Optional[str] = None, button_label: Optional[str] = None) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if button_type and button_label:
                cursor.execute(
                    "SELECT COUNT(*) FROM clicks WHERE button_type = ? AND button_label = ?",
                    (button_type, button_label)
                )
            elif button_type:
                cursor.execute("SELECT COUNT(*) FROM clicks WHERE button_type = ?", (button_type,))
            else:
                cursor.execute("SELECT COUNT(*) FROM clicks")
            return cursor.fetchone()[0]
    
    def get_adv_clicks_stats(self) -> List[dict]:
        """Получить статистику кликов по рекламным кнопкам"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    SELECT button_label, COUNT(*) as count 
                    FROM clicks 
                    WHERE button_type = 'advertising' 
                    GROUP BY button_label
                    ORDER BY count DESC
                """)
                return [dict(row) for row in cursor.fetchall()]
            except sqlite3.OperationalError:
                return []

db = Database()

# ========== ИНИЦИАЛИЗАЦИЯ БОТА ==========
bot = Bot(token=BOT_TOKEN, timeout=60)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== СОСТОЯНИЯ FSM ==========
class AdForm(StatesGroup):
    category = State()
    photo = State()
    description = State()
    price = State()
    preview = State()
    edit_choice = State()
    edit_category = State()
    edit_description = State()
    edit_price = State()
    edit_photo = State()

class BroadcastForm(StatesGroup):
    media_type = State()
    media = State()
    text = State()

# ========== КЛАВИАТУРЫ ==========

photo_done_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="✅ Готово")]],
    resize_keyboard=True
)

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📝 Предложить пост/товар")]
    ],
    resize_keyboard=True
)

admin_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📝 Предложить пост/товар")],
        [KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="📨 Рассылка")],
        [KeyboardButton(text="🔄 Перезагрузить бота")],
        [KeyboardButton(text="🔴 Выключить бота"), KeyboardButton(text="🟢 Включить бота")],
        [KeyboardButton(text="◀️ Выйти из админки")]
    ],
    resize_keyboard=True
)

cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True
)

broadcast_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Только текст")],
        [KeyboardButton(text="Текст + фото")],
        [KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True
)

category_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Электроника", callback_data="cat_электроника")],
        [InlineKeyboardButton(text="Жидкости", callback_data="cat_жидкости")],
        [InlineKeyboardButton(text="Расходники", callback_data="cat_расходники")],
        [InlineKeyboardButton(text="Прочее", callback_data="cat_прочее")]
    ]
)

# ========== INLINE-КЛАВИАТУРЫ ==========

def get_welcome_inline_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с кнопками под фото (Inline)"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="📝 Предложить пост/товар",
                callback_data="create_ad"
            )],
            [InlineKeyboardButton(
                text="📢 По вопросам рекламы",
                url="https://t.me/Smoke6745"
            )],
            [InlineKeyboardButton(
                text="💬 Наш чат",
                url="https://t.me/+Wcc6CkBVEOM1MmQy"
            )],
            [InlineKeyboardButton(
                text="📢 Все объявления",
                url="https://t.me/BTMSarapul"
            )]
        ]
    )

def get_subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="📢 Подписаться на канал", 
                url=f"https://t.me/{CHANNEL_ID.replace('@', '')}"
            )],
            [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_subscribe")]
        ]
    )

def get_channel_post_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для поста в канале"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Добавить объявление",
                    url="https://t.me/bh1230bot"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Реклама",
                    url="https://t.me/Smoke6745"
                )
            ]
        ]
    )

def get_preview_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для предпросмотра объявления"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Опубликовать", callback_data="publish_ad")],
            [InlineKeyboardButton(text="✏️ Изменить", callback_data="edit_ad")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_ad")]
        ]
    )

def get_edit_options_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для выбора что изменить"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📁 Категория", callback_data="edit_category")],
            [InlineKeyboardButton(text="📸 Фото", callback_data="edit_photos")],
            [InlineKeyboardButton(text="📝 Описание", callback_data="edit_description")],
            [InlineKeyboardButton(text="💰 Цена", callback_data="edit_price")],
            [InlineKeyboardButton(text="◀️ Назад к предпросмотру", callback_data="back_to_preview")]
        ]
    )

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

def is_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь администратором"""
    return user_id in ADMIN_IDS

async def check_subscription(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except TelegramAPIError as e:
        logger.error(f"Ошибка проверки подписки для {user_id}: {e}")
        return False

def require_subscription():
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(message: Message, *args, **kwargs):
            global bot_running
            if not bot_running:
                await message.answer("⛔ Бот временно отключен администратором. Попробуйте позже.")
                return
            
            if await check_subscription(message.from_user.id):
                return await func(message, *args, **kwargs)
            else:
                await message.answer(
                    "❌ Для использования бота необходимо подписаться на наш канал!\n\n"
                    "Подпишись и нажми '✅ Проверить подписку'.",
                    reply_markup=get_subscribe_keyboard()
                )
        return wrapper
    return decorator

def admin_only():
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(message: Message, *args, **kwargs):
            if not is_admin(message.from_user.id):
                await message.answer(
                    "⛔ Эта функция доступна только администратору.",
                    reply_markup=main_keyboard
                )
                return
            return await func(message, *args, **kwargs)
        return wrapper
    return decorator

def get_user_link(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.full_name

# ========== УСТАНОВКА КОМАНД ДЛЯ МЕНЮ ==========

async def set_commands():
    """Установка команд для меню бота"""
    commands = [
        BotCommand(command="start", description="Открыть главное меню"),
        BotCommand(command="cancel", description="Отменить создание товара"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    logger.info("Команды для меню установлены")

# ========== ФУНКЦИЯ ПОКАЗА ПРЕДПРОСМОТРА ==========

def format_ad_text(category: str, description: str, price: str, author: str) -> str:
    """Форматирует текст объявления в нужном стиле"""
    return (
        f"• <b>Категория</b>: {category}\n"
        f"• <b>Описание товара</b>:\n{description}\n"
        f"<b>Цена</b>: {price} ₽\n"
        f"<b>Отправитель</b>: {author}"
    )

async def show_preview(message: Message, state: FSMContext):
    """Показывает предпросмотр объявления с кнопками"""
    data = await state.get_data()
    category = data.get("category")
    photo = data.get("photo")
    description = data.get("description")
    price = data.get("price")
    
    author = get_user_link(message.from_user)
    
    # Текст предпросмотра
    preview_text = format_ad_text(category, description, price, author)
    
    # Удаляем старую клавиатуру
    await message.answer(
        "✅ Данные сохранены! Вот как будет выглядеть твоё объявление:",
        reply_markup=ReplyKeyboardRemove()
    )
    
    # Показываем предпросмотр с кнопками
    if photo:
        await message.answer_photo(
            photo=photo,
            caption=preview_text,
            parse_mode="HTML",
            reply_markup=get_preview_keyboard()
        )
    else:
        await message.answer(
            preview_text,
            parse_mode="HTML",
            reply_markup=get_preview_keyboard()
        )
    
    await state.set_state(AdForm.preview)

# ========== КОМАНДА /START ==========

@dp.message(Command("start"))
async def cmd_start(message: Message):
    global bot_running
    user = message.from_user
    db.save_user(user.id, user.username, user.first_name)
    
    if not await check_subscription(user.id):
        await message.answer(
            "👋 Привет! Для использования бота необходимо подписаться на наш канал.\n\n"
            "Подпишись и нажми '✅ Проверить подписку'.",
            reply_markup=get_subscribe_keyboard()
        )
        return
    
    welcome_text = (
        "ВНИМАНИЕ!\n"
        "Рекламные посты - платные\n"
        "по вопросам рекламы обращайтесь - @Smoke6745"
    )
    
    photo_path = "first.jpg"
    if not os.path.exists(photo_path):
        logger.warning(f"Файл {photo_path} не найден!")
        if is_admin(user.id):
            await message.answer(welcome_text, reply_markup=admin_keyboard)
        else:
            if not bot_running:
                await message.answer("⛔ Бот временно отключен администратором. Попробуйте позже.", reply_markup=main_keyboard)
                return
            await message.answer(welcome_text, reply_markup=main_keyboard)
        return
    
    if is_admin(user.id):
        await message.answer_photo(
            photo=FSInputFile(photo_path),
            caption=welcome_text,
            reply_markup=get_welcome_inline_keyboard()
        )
        await message.answer(
            "👋 Добро пожаловать в админ-панель!",
            reply_markup=admin_keyboard
        )
    else:
        if not bot_running:
            await message.answer("⛔ Бот временно отключен администратором. Попробуйте позже.", reply_markup=main_keyboard)
            return
        
        await message.answer_photo(
            photo=FSInputFile(photo_path),
            caption=welcome_text,
            reply_markup=get_welcome_inline_keyboard()
        )

# ========== ПРОВЕРКА ПОДПИСКИ ==========

@dp.callback_query(F.data == "check_subscribe")
async def check_subscribe_callback(callback: CallbackQuery):
    global bot_running
    user_id = callback.from_user.id
    
    if await check_subscription(user_id):
        await callback.message.delete()
        
        welcome_text = (
            "ВНИМАНИЕ!\n"
            "Рекламные посты - платные\n"
            "по вопросам рекламы обращайтесь - @Smoke6745"
        )
        
        photo_path = "first.jpg"
        if os.path.exists(photo_path):
            if is_admin(user_id):
                await callback.message.answer_photo(
                    photo=FSInputFile(photo_path),
                    caption=welcome_text,
                    reply_markup=get_welcome_inline_keyboard()
                )
                await callback.message.answer(
                    "👋 Добро пожаловать в админ-панель!",
                    reply_markup=admin_keyboard
                )
            else:
                if not bot_running:
                    await callback.message.answer(
                        "⛔ Бот временно отключен администратором. Попробуйте позже.",
                        reply_markup=main_keyboard
                    )
                else:
                    await callback.message.answer_photo(
                        photo=FSInputFile(photo_path),
                        caption=welcome_text,
                        reply_markup=get_welcome_inline_keyboard()
                    )
        else:
            if is_admin(user_id):
                await callback.message.answer(welcome_text, reply_markup=admin_keyboard)
            else:
                if not bot_running:
                    await callback.message.answer("⛔ Бот временно отключен администратором. Попробуйте позже.", reply_markup=main_keyboard)
                else:
                    await callback.message.answer(welcome_text, reply_markup=main_keyboard)
        await callback.answer()
    else:
        await callback.answer("❌ Ты ещё не подписан на канал!", show_alert=True)

# ========== КОМАНДА /CANCEL ==========

@dp.message(Command("cancel"))
@dp.message(F.text == "❌ Отмена")
async def cmd_cancel(message: Message, state: FSMContext):
    current_state = await state.get_state()
    
    if current_state in [AdForm.preview, AdForm.edit_choice, AdForm.edit_category, AdForm.edit_description, AdForm.edit_price, AdForm.edit_photo]:
        await state.clear()
        keyboard = admin_keyboard if is_admin(message.from_user.id) else main_keyboard
        await message.answer(
            "❌ Действие отменено.",
            reply_markup=keyboard
        )
        return
    
    await state.clear()
    keyboard = admin_keyboard if is_admin(message.from_user.id) else main_keyboard
    await message.answer(
        "❌ Действие отменено.",
        reply_markup=keyboard
    )

# ========== УПРАВЛЕНИЕ БОТОМ (ТОЛЬКО ДЛЯ АДМИНОВ) ==========

@dp.message(F.text == "🔄 Перезагрузить бота")
@admin_only()
async def restart_bot(message: Message):
    await message.answer(
        "🔄 Перезагрузка бота...\n\n"
        "Бот будет перезапущен через несколько секунд.",
        reply_markup=admin_keyboard
    )
    logger.info(f"Бот перезагружается по команде администратора {message.from_user.id}")
    
    await asyncio.sleep(2)
    os.execv(sys.executable, ['python'] + sys.argv)

@dp.message(F.text == "🔴 Выключить бота")
@admin_only()
async def disable_bot(message: Message):
    global bot_running
    if not bot_running:
        await message.answer("ℹ️ Бот уже выключен.", reply_markup=admin_keyboard)
        return
    
    bot_running = False
    await message.answer(
        "🔴 Бот выключен!\n\n"
        "Теперь бот не будет отвечать пользователям.\n"
        "Чтобы включить, нажми '🟢 Включить бота'.",
        reply_markup=admin_keyboard
    )
    logger.warning(f"Бот выключен администратором {message.from_user.id}")

@dp.message(F.text == "🟢 Включить бота")
@admin_only()
async def enable_bot(message: Message):
    global bot_running
    if bot_running:
        await message.answer("ℹ️ Бот уже включен.", reply_markup=admin_keyboard)
        return
    
    bot_running = True
    await message.answer(
        "🟢 Бот включен!\n\n"
        "Теперь бот снова отвечает пользователям.",
        reply_markup=admin_keyboard
    )
    logger.info(f"Бот включен администратором {message.from_user.id}")

# ========== АДМИН-ПАНЕЛЬ (ТОЛЬКО ДЛЯ АДМИНОВ) ==========

@dp.message(F.text == "◀️ Выйти из админки")
@admin_only()
async def exit_admin(message: Message):
    exit_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Предложить пост/товар")],
            [KeyboardButton(text="⚙️ Войти в админку")]
        ],
        resize_keyboard=True
    )
    await message.answer(
        "Вы вышли из админ-панели.\n\n"
        "Чтобы вернуться в админку, нажми '⚙️ Войти в админку'.",
        reply_markup=exit_keyboard
    )

@dp.message(F.text == "⚙️ Войти в админку")
@admin_only()
async def enter_admin(message: Message):
    await message.answer(
        "👋 Добро пожаловать в админ-панель!\n\n"
        "Выберите действие:",
        reply_markup=admin_keyboard
    )

@dp.message(F.text == "📊 Статистика")
@dp.message(Command("stats"))
@admin_only()
async def admin_stats(message: Message):
    try:
        users_count = db.get_users_count()
        total_clicks = db.get_clicks_count()
        bot_clicks = db.get_clicks_count("bot")
        adv_clicks = db.get_clicks_count("advertising")
        
        adv_stats = db.get_adv_clicks_stats()
        
        adv_details = ""
        if adv_stats:
            adv_details = "\n\n📊 Детали по рекламным кнопкам:"
            for stat in adv_stats:
                label = stat.get('button_label', 'Без названия') or "Без названия"
                adv_details += f"\n• {label}: {stat['count']} переходов"
        
        await message.answer(
            f"📊 Статистика бота:\n\n"
            f"👥 Всего пользователей: {users_count}\n"
            f"🔄 Всего переходов: {total_clicks}\n\n"
            f"🤖 Переходов на бота: {bot_clicks}\n"
            f"📢 Переходов на рекламу: {adv_clicks}"
            f"{adv_details}",
            reply_markup=admin_keyboard
        )
    except Exception as e:
        logger.error(f"Ошибка при получении статистики: {e}")
        await message.answer(
            "❌ Ошибка при получении статистики. Пожалуйста, попробуйте позже.",
            reply_markup=admin_keyboard
        )

# ========== РАССЫЛКА (ТОЛЬКО ДЛЯ АДМИНОВ) ==========

@dp.message(F.text == "📨 Рассылка")
@dp.message(Command("broadcast"))
@admin_only()
async def admin_broadcast(message: Message, state: FSMContext):
    await state.set_state(BroadcastForm.media_type)
    await message.answer("Выберите тип рассылки:", reply_markup=broadcast_keyboard)

@dp.message(StateFilter(BroadcastForm.media_type))
@admin_only()
async def process_broadcast_type(message: Message, state: FSMContext):
    choice = message.text
    
    if choice == "❌ Отмена":
        await state.clear()
        await message.answer("Рассылка отменена.", reply_markup=admin_keyboard)
        return
    
    if choice == "Только текст":
        await state.update_data(media_type="text")
        await state.set_state(BroadcastForm.text)
        await message.answer("Введите текст для рассылки:", reply_markup=cancel_keyboard)
    elif choice == "Текст + фото":
        await state.update_data(media_type="photo")
        await state.set_state(BroadcastForm.media)
        await message.answer("Отправьте фото для рассылки:", reply_markup=cancel_keyboard)
    else:
        await message.answer("Пожалуйста, выберите тип рассылки:", reply_markup=broadcast_keyboard)

@dp.message(StateFilter(BroadcastForm.media), F.photo)
@admin_only()
async def process_broadcast_photo(message: Message, state: FSMContext):
    photo = message.photo[-1]
    await state.update_data(media_file_id=photo.file_id)
    await state.set_state(BroadcastForm.text)
    await message.answer("Фото принято! Теперь введите текст для рассылки:", reply_markup=cancel_keyboard)

@dp.message(StateFilter(BroadcastForm.media))
@admin_only()
async def broadcast_media_error(message: Message):
    await message.answer("Пожалуйста, отправьте фото.")

@dp.message(StateFilter(BroadcastForm.text), F.text)
@admin_only()
async def process_broadcast_text(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text:
        await message.answer("Текст не может быть пустым!")
        return
    
    data = await state.get_data()
    media_type = data.get("media_type")
    media_file_id = data.get("media_file_id")
    
    status_msg = await message.answer("Начинаю рассылку...")
    
    users = db.get_all_users()
    success = 0
    failed = 0
    total = len(users)
    
    if total == 0:
        await status_msg.edit_text("Нет пользователей для рассылки.")
        await state.clear()
        return
    
    for i, user_id in enumerate(users, 1):
        try:
            if media_type == "photo" and media_file_id:
                await bot.send_photo(chat_id=user_id, photo=media_file_id, caption=text)
            else:
                await bot.send_message(user_id, text)
            success += 1
            if i % 50 == 0:
                try:
                    await status_msg.edit_text(f"Отправлено {i}/{total} сообщений...")
                except:
                    pass
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            logger.warning(f"Flood control, ждём {e.retry_after} сек")
            await asyncio.sleep(e.retry_after)
            try:
                if media_type == "photo" and media_file_id:
                    await bot.send_photo(user_id, media_file_id, caption=text)
                else:
                    await bot.send_message(user_id, text)
                success += 1
            except:
                failed += 1
        except Exception as e:
            logger.error(f"Ошибка при отправке {user_id}: {e}")
            failed += 1
    
    try:
        await status_msg.delete()
    except:
        pass
    
    await message.answer(
        f"Рассылка завершена!\n\n"
        f"Отправлено: {success}\n"
        f"Не доставлено: {failed}\n"
        f"Всего пользователей: {total}\n"
        f"Тип рассылки: {'Текст + фото' if media_type == 'photo' else 'Только текст'}",
        reply_markup=admin_keyboard
    )
    await state.clear()

@dp.message(StateFilter(BroadcastForm.text))
@admin_only()
async def broadcast_text_error(message: Message):
    await message.answer("Пожалуйста, введите текст сообщения.")

# ========== ОБРАБОТЧИК КНОПКИ "Предложить пост/товар" (Inline) ==========

@dp.callback_query(F.data == "create_ad")
@require_subscription()
async def create_ad_callback(callback: CallbackQuery, state: FSMContext):
    global bot_running
    if not bot_running:
        await callback.answer("⛔ Бот временно отключен", show_alert=True)
        return
    
    await callback.answer()
    await state.set_state(AdForm.category)
    await state.update_data(photo=None)
    
    await callback.message.answer(
        "Выбери категорию:",
        reply_markup=category_keyboard
    )
    await callback.message.answer(
        "Чтобы отменить, нажми кнопку ниже или используй команду /cancel в меню:",
        reply_markup=cancel_keyboard
    )

# ========== ОБРАБОТЧИКИ КНОПОК ==========

@dp.message(F.text == "📝 Предложить пост/товар")
@require_subscription()
async def start_ad_creation(message: Message, state: FSMContext):
    global bot_running
    if not bot_running:
        await message.answer("⛔ Бот временно отключен администратором. Попробуйте позже.")
        return
    
    await state.set_state(AdForm.category)
    await state.update_data(photo=None)
    
    await message.answer(
        "Выбери категорию:",
        reply_markup=category_keyboard
    )
    await message.answer(
        "Чтобы отменить, нажми кнопку ниже или используй команду /cancel в меню:",
        reply_markup=cancel_keyboard
    )

# ========== СОЗДАНИЕ ОБЪЯВЛЕНИЯ ==========

@dp.callback_query(StateFilter(AdForm.category))
async def process_category(callback: CallbackQuery, state: FSMContext):
    global bot_running
    if not bot_running:
        await callback.answer("⛔ Бот временно отключен", show_alert=True)
        return
    
    if not await check_subscription(callback.from_user.id):
        await callback.answer("Подпишись на канал!", show_alert=True)
        return
    
    category = callback.data.replace("cat_", "")
    await state.update_data(category=category)
    
    await callback.message.delete()
    await callback.message.answer(
        f"Категория: {category}\n\n"
        "Отправь фото товара.",
        reply_markup=cancel_keyboard
    )
    await state.set_state(AdForm.photo)
    await callback.answer()

@dp.message(StateFilter(AdForm.photo), F.photo)
async def process_photo(message: Message, state: FSMContext):
    global bot_running
    if not bot_running:
        await message.answer("⛔ Бот временно отключен администратором. Попробуйте позже.")
        return
    
    if not await check_subscription(message.from_user.id):
        await message.answer("Подпишись на канал!", reply_markup=get_subscribe_keyboard())
        return
    
    photo = message.photo[-1]
    await state.update_data(photo=photo.file_id)
    
    await message.answer(
        "✅ Фото принято!",
        reply_markup=cancel_keyboard
    )
    await state.set_state(AdForm.description)
    await message.answer(
        "Теперь напиши описание товара.\n"
        "Чтобы отменить, используй /cancel в меню.",
        reply_markup=cancel_keyboard
    )

@dp.message(StateFilter(AdForm.photo))
async def photo_error(message: Message):
    await message.answer(
        "Пожалуйста, отправь фото.\n"
        "Чтобы отменить, используй /cancel в меню.",
        reply_markup=cancel_keyboard
    )

@dp.message(StateFilter(AdForm.description), F.text)
async def process_description(message: Message, state: FSMContext):
    if len(message.text) > 400:
        await message.answer(
            f"❌ Слишком длинное описание! Максимум 400 символов.\n"
            f"Сейчас: {len(message.text)} символов.\n"
            "Пожалуйста, сократи описание.\n"
            "Чтобы отменить, используй /cancel в меню.",
            reply_markup=cancel_keyboard
        )
        return
    
    await state.update_data(description=message.text)
    await message.answer(
        "✅ Описание сохранено!\n\n"
        "Теперь укажи цену товара (только цифры).\n"
        "Чтобы отменить, используй /cancel в меню.",
        reply_markup=cancel_keyboard
    )
    await state.set_state(AdForm.price)

@dp.message(StateFilter(AdForm.description))
async def description_error(message: Message):
    await message.answer(
        "Пожалуйста, напиши текстовое описание.\n"
        "Чтобы отменить, используй /cancel в меню.",
        reply_markup=cancel_keyboard
    )

# ========== ОБРАБОТЧИК ЦЕНЫ ==========

@dp.message(StateFilter(AdForm.price), F.text)
async def process_price(message: Message, state: FSMContext):
    global bot_running
    if not bot_running:
        await message.answer("⛔ Бот временно отключен администратором. Попробуйте позже.")
        return
    
    if not await check_subscription(message.from_user.id):
        await message.answer("Подпишись на канал!", reply_markup=get_subscribe_keyboard())
        return
    
    price = message.text.strip()
    
    if not price.isdigit():
        await message.answer(
            "❌ Цена должна содержать только цифры!\n"
            "Пожалуйста, введи цену правильно (например: 1000, 2500).\n"
            "Чтобы отменить, используй /cancel в меню.",
            reply_markup=cancel_keyboard
        )
        return
    
    if len(price) > 10:
        await message.answer(
            f"❌ Цена слишком большая! Максимум 10 цифр.\n"
            f"Сейчас: {len(price)} цифр.\n"
            "Пожалуйста, введи цену до 10 цифр.\n"
            "Чтобы отменить, используй /cancel в меню.",
            reply_markup=cancel_keyboard
        )
        return
    
    if int(price) <= 0:
        await message.answer(
            "❌ Цена должна быть больше 0!\n"
            "Пожалуйста, введи корректную цену.\n"
            "Чтобы отменить, используй /cancel в меню.",
            reply_markup=cancel_keyboard
        )
        return
    
    await state.update_data(price=price)
    await show_preview(message, state)

# ========== ОБРАБОТЧИКИ КНОПОК ПРЕДПРОСМОТРА ==========

@dp.callback_query(StateFilter(AdForm.preview), F.data == "publish_ad")
async def publish_ad(callback: CallbackQuery, state: FSMContext):
    """Публикация объявления в канал с автоудалением предпросмотра"""
    await callback.answer()
    
    data = await state.get_data()
    category = data.get("category")
    photo = data.get("photo")
    description = data.get("description")
    price = data.get("price")
    author = get_user_link(callback.from_user)
    
    # Текст объявления
    ad_text = format_ad_text(category, description, price, author)
    
    try:
        if photo:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=photo,
                caption=ad_text,
                parse_mode="HTML",
                reply_markup=get_channel_post_keyboard()
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=ad_text,
                parse_mode="HTML",
                reply_markup=get_channel_post_keyboard()
            )
        
        keyboard = admin_keyboard if is_admin(callback.from_user.id) else main_keyboard
        
        # Удаляем сообщение с предпросмотром
        try:
            await callback.message.delete()
        except Exception as e:
            logger.warning(f"Не удалось удалить сообщение с предпросмотром: {e}")
        
        await callback.message.answer(
            "✅ Объявление опубликовано в канале!\n"
            "Спасибо, что пользуешься нашим ботом.",
            reply_markup=keyboard
        )
        
        db.save_click(callback.from_user.id, "bot", "publish_ad")
        await state.clear()
        
    except TelegramAPIError as e:
        logger.error(f"Ошибка публикации объявления: {e}")
        try:
            await callback.message.delete()
        except:
            pass
        
        await callback.message.answer(
            f"❌ Ошибка при публикации в канале:\n{e}\n"
            "Пожалуйста, свяжись с администратором.",
            reply_markup=main_keyboard
        )
        await state.clear()

@dp.callback_query(StateFilter(AdForm.preview), F.data == "edit_ad")
async def edit_ad_options(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AdForm.edit_choice)
    
    await callback.message.answer(
        "✏️ Что ты хочешь изменить?",
        reply_markup=get_edit_options_keyboard()
    )

@dp.callback_query(StateFilter(AdForm.preview), F.data == "cancel_ad")
async def cancel_ad_from_preview(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    
    keyboard = admin_keyboard if is_admin(callback.from_user.id) else main_keyboard
    
    try:
        await callback.message.delete()
    except:
        pass
    
    await callback.message.answer(
        "❌ Создание объявления отменено.",
        reply_markup=keyboard
    )

# ========== ОБРАБОТЧИКИ ИЗМЕНЕНИЯ ОБЪЯВЛЕНИЯ ==========

@dp.callback_query(StateFilter(AdForm.edit_choice), F.data == "back_to_preview")
async def back_to_preview_from_edit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AdForm.preview)
    
    try:
        await callback.message.delete()
    except:
        pass
    
    await show_preview(callback.message, state)

@dp.callback_query(StateFilter(AdForm.edit_choice), F.data == "edit_category")
async def edit_category(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AdForm.edit_category)
    
    try:
        await callback.message.delete()
    except:
        pass
    
    await callback.message.answer(
        "Выбери новую категорию:",
        reply_markup=category_keyboard
    )
    await callback.message.answer(
        "Чтобы отменить, нажми кнопку ниже или используй команду /cancel в меню:",
        reply_markup=cancel_keyboard
    )

@dp.callback_query(StateFilter(AdForm.edit_choice), F.data == "edit_photos")
async def edit_photos(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    await state.update_data(photo=None)
    await state.set_state(AdForm.edit_photo)
    
    try:
        await callback.message.delete()
    except:
        pass
    
    await callback.message.answer(
        "📸 Отправь новое фото товара.\n"
        "Чтобы отменить, используй /cancel в меню.",
        reply_markup=cancel_keyboard
    )

@dp.callback_query(StateFilter(AdForm.edit_choice), F.data == "edit_description")
async def edit_description(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AdForm.edit_description)
    
    try:
        await callback.message.delete()
    except:
        pass
    
    await callback.message.answer(
        "📝 Напиши новое описание товара.\n"
        "Чтобы отменить, используй /cancel в меню.",
        reply_markup=cancel_keyboard
    )

@dp.callback_query(StateFilter(AdForm.edit_choice), F.data == "edit_price")
async def edit_price(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AdForm.edit_price)
    
    try:
        await callback.message.delete()
    except:
        pass
    
    await callback.message.answer(
        "💰 Введи новую цену товара (только цифры).\n"
        "Чтобы отменить, используй /cancel в меню.",
        reply_markup=cancel_keyboard
    )

# ========== ОБРАБОТЧИКИ ДЛЯ РЕДАКТИРОВАНИЯ КАТЕГОРИИ ==========

@dp.callback_query(StateFilter(AdForm.edit_category))
async def process_category_edit(callback: CallbackQuery, state: FSMContext):
    global bot_running
    if not bot_running:
        await callback.answer("⛔ Бот временно отключен", show_alert=True)
        return
    
    if not await check_subscription(callback.from_user.id):
        await callback.answer("Подпишись на канал!", show_alert=True)
        return
    
    category = callback.data.replace("cat_", "")
    await state.update_data(category=category)
    
    await callback.message.delete()
    await callback.message.answer(f"✅ Категория обновлена: {category}")
    
    await show_preview(callback.message, state)
    await callback.answer()

# ========== ОБРАБОТЧИКИ ДЛЯ РЕДАКТИРОВАНИЯ ФОТО ==========

@dp.message(StateFilter(AdForm.edit_photo), F.photo)
async def process_photo_edit(message: Message, state: FSMContext):
    global bot_running
    if not bot_running:
        await message.answer("⛔ Бот временно отключен администратором. Попробуйте позже.")
        return
    
    if not await check_subscription(message.from_user.id):
        await message.answer("Подпишись на канал!", reply_markup=get_subscribe_keyboard())
        return
    
    photo = message.photo[-1]
    await state.update_data(photo=photo.file_id)
    
    await message.answer("✅ Фото обновлено!")
    await show_preview(message, state)

@dp.message(StateFilter(AdForm.edit_photo))
async def photo_error_edit(message: Message):
    await message.answer(
        "Пожалуйста, отправь фото.\n"
        "Чтобы отменить, используй /cancel в меню.",
        reply_markup=cancel_keyboard
    )

# ========== ОБРАБОТЧИКИ ДЛЯ РЕДАКТИРОВАНИЯ ОПИСАНИЯ ==========

@dp.message(StateFilter(AdForm.edit_description), F.text)
async def process_description_edit(message: Message, state: FSMContext):
    if len(message.text) > 400:
        await message.answer(
            f"❌ Слишком длинное описание! Максимум 400 символов.\n"
            f"Сейчас: {len(message.text)} символов.\n"
            "Пожалуйста, сократи описание.\n"
            "Чтобы отменить, используй /cancel в меню.",
            reply_markup=cancel_keyboard
        )
        return
    
    await state.update_data(description=message.text)
    await message.answer("✅ Описание обновлено!")
    await show_preview(message, state)

@dp.message(StateFilter(AdForm.edit_description))
async def description_error_edit(message: Message):
    await message.answer(
        "Пожалуйста, напиши текстовое описание.\n"
        "Чтобы отменить, используй /cancel в меню.",
        reply_markup=cancel_keyboard
    )

# ========== ОБРАБОТЧИКИ ДЛЯ РЕДАКТИРОВАНИЯ ЦЕНЫ ==========

@dp.message(StateFilter(AdForm.edit_price), F.text)
async def process_price_edit(message: Message, state: FSMContext):
    price = message.text.strip()
    
    if not price.isdigit():
        await message.answer(
            "❌ Цена должна содержать только цифры!\n"
            "Пожалуйста, введи цену правильно (например: 1000, 2500).\n"
            "Чтобы отменить, используй /cancel в меню.",
            reply_markup=cancel_keyboard
        )
        return
    
    if len(price) > 10:
        await message.answer(
            f"❌ Цена слишком большая! Максимум 10 цифр.\n"
            f"Сейчас: {len(price)} цифр.\n"
            "Пожалуйста, введи цену до 10 цифр.\n"
            "Чтобы отменить, используй /cancel в меню.",
            reply_markup=cancel_keyboard
        )
        return
    
    if int(price) <= 0:
        await message.answer(
            "❌ Цена должна быть больше 0!\n"
            "Пожалуйста, введи корректную цену.\n"
            "Чтобы отменить, используй /cancel в меню.",
            reply_markup=cancel_keyboard
        )
        return
    
    await state.update_data(price=price)
    await message.answer(f"✅ Цена обновлена: {price} ₽")
    await show_preview(message, state)

@dp.message(StateFilter(AdForm.edit_price))
async def price_error_edit(message: Message):
    await message.answer(
        "Пожалуйста, введи цену только цифрами.\n"
        "Например: 1000, 2500\n"
        "Чтобы отменить, используй /cancel в меню.",
        reply_markup=cancel_keyboard
    )

# ========== ОБРАБОТЧИК ВСЕХ ОСТАЛЬНЫХ СООБЩЕНИЙ ==========

@dp.message()
async def echo(message: Message, state: FSMContext):
    global bot_running
    current_state = await state.get_state()
    
    if current_state is not None:
        return
    
    if is_admin(message.from_user.id):
        await message.answer("Используй кнопки меню:", reply_markup=admin_keyboard)
    else:
        if not bot_running:
            await message.answer("⛔ Бот временно отключен администратором. Попробуйте позже.", reply_markup=main_keyboard)
            return
        await message.answer(
            "Нажми '📝 Предложить пост/товар', чтобы создать объявление.\n"
            "Или используй меню (☰) для команд.",
            reply_markup=main_keyboard
        )

# ========== ЗАПУСК БОТА ==========

async def main():
    await set_commands()
    
    retry_count = 0
    max_retries = 5
    
    while retry_count < max_retries:
        try:
            me = await bot.get_me()
            logger.info(f"Бот запущен: @{me.username}")
            logger.info(f"Администраторы: {ADMIN_IDS}")
            logger.info(f"Канал: {CHANNEL_ID}")
            logger.info(f"Пользователей в БД: {db.get_users_count()}")
            logger.info("-" * 30)
            
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                skip_updates=True
            )
            break
            
        except TelegramNetworkError as e:
            retry_count += 1
            wait_time = min(2 ** retry_count, 60)
            logger.error(f"Ошибка сети (попытка {retry_count}/{max_retries}): {e}")
            logger.info(f"Переподключение через {wait_time} секунд...")
            await asyncio.sleep(wait_time)
            
        except Exception as e:
            logger.error(f"Критическая ошибка: {e}")
            if "Server disconnected" in str(e):
                retry_count += 1
                wait_time = min(2 ** retry_count, 60)
                logger.info(f"Переподключение через {wait_time} секунд...")
                await asyncio.sleep(wait_time)
            else:
                raise
    
    if retry_count >= max_retries:
        logger.error("Превышено максимальное количество попыток подключения")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Фатальная ошибка: {e}")
