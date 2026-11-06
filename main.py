import asyncio, random, logging, aiosqlite, time, os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from telethon import TelegramClient
from telethon.errors import FloodWaitError, UsernameNotOccupiedError, ChannelPrivateError
from telethon.tl.functions.messages import ReportRequest
from telethon.tl.types import InputReportReasonSpam

# Создаем папку data, если ее нет (решает проблему с Railway)
os.makedirs("data", exist_ok=True)

# --- НАСТРОЙКИ ---
# Секреты берем из переменных окружения Railway (Variables)
BOT_TOKEN = os.getenv("BOT_TOKEN", "9095309503:AAF4C9JBKiW6uwEzypXJB9RNjjXEcGbACEM")
API_ID = int(os.getenv("API_ID", "39846788"))
API_HASH = os.getenv("API_HASH", "269a368139d68b036b46377f36813bbb")
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "fsyovz")

SESSION = "data/reporter"
DB_NAME = "data/users.db"

DELAY_MIN, DELAY_MAX = 10, 20
MAX_REPORTS = 150

# Цены (в звездах)
PRICE_DAY = 85
PRICE_WEEK = 250
PRICE_MONTH = 500
PRICE_ONE_TIME = 15

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("bot")

class Form(StatesGroup):
    waiting_username = State()
    waiting_admin_id = State()
    waiting_sub_days = State()
    waiting_admin_username = State()

dp = Dispatcher()

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, 
            username TEXT,
            is_admin INTEGER DEFAULT 0, 
            sub_until REAL DEFAULT 0,
            one_time_paid INTEGER DEFAULT 0
        )""")
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def update_user(user_id: int, username: str = None, is_admin: int = None, sub_until: float = None, one_time_paid: int = None):
    async with aiosqlite.connect(DB_NAME) as db:
        user = await get_user(user_id)
        if not user:
            await db.execute("INSERT INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
            await db.commit()
        if username is not None:
            await db.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        if is_admin is not None:
            await db.execute("UPDATE users SET is_admin = ? WHERE user_id = ?", (is_admin, user_id))
        if sub_until is not None:
            await db.execute("UPDATE users SET sub_until = ? WHERE user_id = ?", (sub_until, user_id))
        if one_time_paid is not None:
            await db.execute("UPDATE users SET one_time_paid = ? WHERE user_id = ?", (one_time_paid, user_id))
        await db.commit()

async def get_user_by_username(username: str):
    username = username.lstrip("@")
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM users WHERE username = ?", (username,)) as cursor:
            return await cursor.fetchone()

async def is_owner_or_admin(user_id: int, username: str):
    if username and username.lower() == OWNER_USERNAME.lower():
        await update_user(user_id, is_admin=1)
        return True
    u = await get_user(user_id)
    return bool(u and u[2] == 1)

# --- КЛАВИАТУРЫ ---
def main_menu(is_admin=False):
    kb = [
        [InlineKeyboardButton(text="🚀 Сделать рассылку", callback_data="report")],
        [InlineKeyboardButton(text="💎 Купить доступ / Подписку", callback_data="buy_menu")]
    ]
    if is_admin:
        kb.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def buy_menu():
    kb = [
        [InlineKeyboardButton(text=f"1 разовый запуск — {PRICE_ONE_TIME} ⭐", callback_data="buy_onetime")],
        [InlineKeyboardButton(text=f"Подписка на 1 день — {PRICE_DAY} ⭐", callback_data="buy_day")],
        [InlineKeyboardButton(text=f"Подписка на 7 дней — {PRICE_WEEK} ⭐", callback_data="buy_week")],
        [InlineKeyboardButton(text=f"Подписка на 30 дней — {PRICE_MONTH} ⭐", callback_data="buy_month")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def admin_menu():
    kb = [
        [InlineKeyboardButton(text="🎁 Выдать подписку по @username", callback_data="admin_give_sub")],
        [InlineKeyboardButton(text="👑 Назначить админа по @username", callback_data="admin_add_admin")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

# --- ХЭНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(msg: types.Message):
    await update_user(msg.from_user.id, username=msg.from_user.username)
    is_adm = await is_owner_or_admin(msg.from_user.id, msg.from_user.username)
    await msg.answer("Главное меню:", reply_markup=main_menu(is_adm))

@dp.callback_query(F.data == "back_main")
async def back_main(cb: types.CallbackQuery, state: FSMContext):
    await state.clear()
    is_adm = await is_owner_or_admin(cb.from_user.id, cb.from_user.username)
    await cb.message.edit_text("Главное меню:", reply_markup=main_menu(is_adm))
    await cb.answer()

@dp.callback_query(F.data == "buy_menu")
async def show_buy_menu(cb: types.CallbackQuery):
    await cb.message.edit_text("Выбери тариф:", reply_markup=buy_menu())
    await cb.answer()

# --- ОПЛАТА ---
async def send_invoice(cb: types.CallbackQuery, title: str, payload: str, price: int):
    await cb.message.answer_invoice(
        title=title,
        description=f"Оплата {price} звезд",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=price)]
    )

@dp.callback_query(F.data.in_({"buy_onetime", "buy_day", "buy_week", "buy_month"}))
async def process_buy(cb: types.CallbackQuery):
    if cb.data == "buy_onetime":
        await send_invoice(cb, "Разовый запуск", "onetime", PRICE_ONE_TIME)
    elif cb.data == "buy_day":
        await send_invoice(cb, "Подписка на 1 день", "sub_1", PRICE_DAY)
    elif cb.data == "buy_week":
        await send_invoice(cb, "Подписка на 7 дней", "sub_7", PRICE_WEEK)
    elif cb.data == "buy_month":
        await send_invoice(cb, "Подписка на 30 дней", "sub_30", PRICE_MONTH)
    await cb.answer()

@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_q: PreCheckoutQuery):
    await pre_checkout_q.answer(ok=True)

@dp.message(F.successful_payment)
async def success_payment(msg: types.Message, state: FSMContext):
    payload = msg.successful_payment.invoice_payload
    user_id = msg.from_user.id
    now = time.time()

    if payload == "onetime":
        await update_user(user_id, one_time_paid=1)
        await msg.answer("✅ Оплата 15 ⭐ получена! Введи юзернейм цели (без @):")
        await state.set_state(Form.waiting_username)
    else:
        days = 1 if payload == "sub_1" else (7 if payload == "sub_7" else 30)
        u = await get_user(user_id)
        current_sub = u[3] if u and u[3] > now else now
        new_sub = current_sub + (days * 86400)
        await update_user(user_id, sub_until=new_sub)
        dt = datetime.fromtimestamp(new_sub).strftime('%Y-%m-%d %H:%M')
        await msg.answer(f"✅ Подписка активирована до {dt}! Можешь делать рассылки.", reply_markup=main_menu(await is_owner_or_admin(user_id, msg.from_user.username)))

# --- РАССЫЛКА ---
@dp.callback_query(F.data == "report")
async def start_report(cb: types.CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    u = await get_user(user_id)
    now = time.time()
    
    has_sub = u and u[3] > now
    has_one_time = u and u[4] == 1

    if has_sub or has_one_time or await is_owner_or_admin(user_id, cb.from_user.username):
        await cb.message.answer("Введите юзернейм цели (без @):")
        await state.set_state(Form.waiting_username)
        if not has_sub:
            await update_user(user_id, one_time_paid=0)
    else:
        await cb.message.answer("У вас нет активной подписки или разового запуска. Купите доступ.", reply_markup=buy_menu())
    await cb.answer()

@dp.message(Form.waiting_username)
async def do_report(msg: types.Message, state: FSMContext):
    username = msg.text.strip().lstrip("@")
    await state.clear()
    await msg.answer("Началась рассылка...")

    async with TelegramClient(SESSION, API_ID, API_HASH) as client:
        await client.start()
        sent = 0
        for i in range(1, MAX_REPORTS + 1):
            try:
                target = await client.get_entity(username)
            except UsernameNotOccupiedError:
                await msg.answer("Юзернейм не существует"); return
            except ChannelPrivateError:
                await msg.answer("Приватный канал/группа, жалоба невозможна"); return
            try:
                await client(ReportRequest(
                    peer=target,
                    reason=InputReportReasonSpam(),
                    message="Нарушение правил Telegram"
                ))
                sent += 1
            except FloodWaitError as e:
                await msg.answer(f"FloodWait: подожди {e.seconds} сек."); break
            except Exception as e:
                await msg.answer(f"Ошибка: {e}"); break
            if i < MAX_REPORTS:
                await asyncio.sleep(random.randint(DELAY_MIN, DELAY_MAX))
        await msg.answer(f"Готово. Отправлено жалоб: {sent}")

# --- АДМИН ПАНЕЛЬ ---
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: types.CallbackQuery):
    if not await is_owner_or_admin(cb.from_user.id, cb.from_user.username):
        await cb.answer("Нет прав", show_alert=True); return
    await cb.message.edit_text("Админ-панель:", reply_markup=admin_menu())
    await cb.answer()

@dp.callback_query(F.data == "admin_give_sub")
async def adm_give_sub(cb: types.CallbackQuery, state: FSMContext):
    if not await is_owner_or_admin(cb.from_user.id, cb.from_user.username): return
    await cb.message.answer("Введи @username пользователя и количество дней через пробел (например: @vasya 30):")
    await state.set_state(Form.waiting_sub_days)
    await cb.answer()

@dp.message(Form.waiting_sub_days)
async def process_give_sub(msg: types.Message, state: FSMContext):
    try:
        parts = msg.text.split()
        target_username = parts[0].lstrip("@")
        days = int(parts[1])
        target = await get_user_by_username(target_username)
        if not target:
            await msg.answer("Пользователь не найден в базе. Пусть он сначала напишет /start."); return
        
        user_id = target[0]
        now = time.time()
        current_sub = target[3] if target[3] > now else now
        new_sub = current_sub + (days * 86400)
        await update_user(user_id, sub_until=new_sub)
        await msg.answer(f"✅ Выдана подписка на {days} дней для @{target_username}.")
    except Exception:
        await msg.answer("Ошибка формата. Введи: @username дни")
    await state.clear()

@dp.callback_query(F.data == "admin_add_admin")
async def adm_add_admin(cb: types.CallbackQuery, state: FSMContext):
    if not await is_owner_or_admin(cb.from_user.id, cb.from_user.username): return
    await cb.message.answer("Введи @username пользователя, которого нужно сделать админом:")
    await state.set_state(Form.waiting_admin_username)
    await cb.answer()

@dp.message(Form.waiting_admin_username)
async def process_add_admin(msg: types.Message, state: FSMContext):
    target_username = msg.text.strip().lstrip("@")
    target = await get_user_by_username(target_username)
    if not target:
        await msg.answer("Пользователь не найден в базе. Пусть он напишет /start."); return
    
    await update_user(target[0], is_admin=1)
    await msg.answer(f"✅ @{target_username} теперь админ.")
    await state.clear()

async def main():
    await init_db()
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
