import os
import asyncio
import random
import logging
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InputMediaPhoto, FSInputFile
import aiohttp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "0").split(",")))

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ─── QR режим ───────────────────────────────────────────────────────────────
qr_mode = False          # включён ли режим QR
pending_qr = None        # путь / file_id к текущему QR-изображению

# ─── Состояния ──────────────────────────────────────────────────────────────
class DepositStates(StatesGroup):
    choose_casino   = State()
    enter_id        = State()
    choose_bank     = State()
    enter_amount    = State()
    wait_payment    = State()

class WithdrawStates(StatesGroup):
    choose_casino   = State()
    choose_bank     = State()
    enter_phone     = State()
    send_qr         = State()
    enter_casino_id = State()
    enter_code      = State()

class AdminStates(StatesGroup):
    set_qr          = State()
    send_cob        = State()
    cob_message     = State()

# ─── Клавиатуры ─────────────────────────────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💰 Пополнить"), KeyboardButton(text="💸 Вывести")],
            [KeyboardButton(text="📖 Инструкция"), KeyboardButton(text="🌐 Язык")],
        ],
        resize_keyboard=True,
    )

def cancel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Операция отменена")]],
        resize_keyboard=True,
    )

def casino_keyboard(prefix: str):
    casinos = ["1xBet", "Melbet", "1win", "mostbet"]
    buttons = [[InlineKeyboardButton(text=f"1️⃣ {c}" if c=="1xBet" else
                                         f"🎰 {c}", callback_data=f"{prefix}_casino_{c}")]
               for c in casinos]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def deposit_bank_keyboard():
    banks = [
        ("🏦 O!Bank",      "dep_bank_obank"),
        ("🏦 MBank",       "dep_bank_mbank"),
        ("🏦 Optima Bank", "dep_bank_optima"),
        ("🏦 Demir Bank",  "dep_bank_demir"),
        ("🏦 Bakai Bank",  "dep_bank_bakai"),
        ("🏦 MegaPay",     "dep_bank_mega"),
    ]
    rows = []
    for name, cbd in banks:
        rows.append([InlineKeyboardButton(text=f"{name}  🚫 Не доступно", callback_data=cbd)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def withdraw_bank_keyboard():
    banks = [
        ("🏦 Компаньон",  "wd_bank_kompanyon"),
        ("🏦 O банк",     "wd_bank_obank"),
        ("🏦 Bakai",      "wd_bank_bakai"),
        ("🏦 Balance.kg", "wd_bank_balance"),
        ("🏦 MegaPay",    "wd_bank_megapay"),
        ("🏦 MBank",      "wd_bank_mbank"),
    ]
    rows = []
    for i in range(0, len(banks), 2):
        row = []
        for name, cbd in banks[i:i+2]:
            row.append(InlineKeyboardButton(text=name, callback_data=cbd))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def amount_keyboard():
    amounts = [100, 500, 1000, 5000, 10000]
    rows = []
    row = []
    for a in amounts:
        row.append(InlineKeyboardButton(text=str(a), callback_data=f"amount_{a}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_user_keyboard(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✍️ Написать",      callback_data=f"adm_write_{user_id}"),
            InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"adm_block_{user_id}"),
            InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"adm_unblock_{user_id}"),
        ]
    ])

def language_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Русский",   callback_data="lang_ru")],
        [InlineKeyboardButton(text="🇰🇬 Кыргызча", callback_data="lang_ky")],
        [InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang_uz")],
    ])

# ─── Вспомогательные ─────────────────────────────────────────────────────────
blocked_users: set[int] = set()

def is_blocked(user_id: int) -> bool:
    return user_id in blocked_users

async def send_admin_notification(text: str, user_id: int):
    if ADMIN_CHAT_ID:
        try:
            await bot.send_message(
                ADMIN_CHAT_ID,
                text,
                reply_markup=admin_user_keyboard(user_id),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Admin notify error: {e}")

# ─── /start ──────────────────────────────────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    if is_blocked(msg.from_user.id):
        await msg.answer("❌ Вы заблокированы.")
        return
    name = msg.from_user.full_name
    text = (
        f"Привет, {name}\n\n"
        "⚡ Авто-пополнение: 0%\n"
        "⚡ Авто-вывод: 0%\n"
        "🌟 Работаем: 24/7\n\n"
        "💬 Служба поддержки: @aurapay_support_bot"
    )
    await msg.answer(text, reply_markup=main_keyboard())

# ─── Инструкция ──────────────────────────────────────────────────────────────
@router.message(F.text == "📖 Инструкция")
async def cmd_instruction(msg: Message):
    text = (
        "📖 <b>Инструкция по использованию бота</b>\n\n"
        "💰 <b>ПОПОЛНЕНИЕ:</b>\n"
        "1. Нажмите «Пополнить»\n"
        "2. Выберите казино\n"
        "3. Введите ID вашего счёта\n"
        "4. Введите сумму пополнения\n"
        "5. Перейдите по ссылке и оплатите\n\n"
        "💸 <b>ВЫВОД:</b>\n"
        "1. Нажмите «Вывести»\n"
        "2. Выберите казино\n"
        "3. Выберите банк\n"
        "4. Введите номер телефона (+996)\n"
        "5. Отправьте фото QR кода от банка\n"
        "6. Введите ID счёта в казино\n"
        "7. Введите код с сайта казино\n\n"
        "Ваша заявка будет обработана в ближайшее время!"
    )
    await msg.answer(text, parse_mode="HTML", reply_markup=main_keyboard())

# ─── Язык ────────────────────────────────────────────────────────────────────
@router.message(F.text == "🌐 Язык")
async def cmd_language(msg: Message):
    await msg.answer("Выберите язык:", reply_markup=language_keyboard())

@router.callback_query(F.data.startswith("lang_"))
async def cb_language(cb: CallbackQuery):
    langs = {"lang_ru": "🇷🇺 Русский", "lang_ky": "🇰🇬 Кыргызча", "lang_uz": "🇺🇿 O'zbekcha"}
    await cb.answer(f"Выбран: {langs.get(cb.data, '')}", show_alert=False)
    await cb.message.edit_reply_markup()

# ─── ПОПОЛНЕНИЕ ──────────────────────────────────────────────────────────────
@router.message(F.text == "💰 Пополнить")
async def dep_start(msg: Message, state: FSMContext):
    if is_blocked(msg.from_user.id):
        await msg.answer("❌ Вы заблокированы.")
        return
    await state.clear()
    await msg.answer("🎰 Выберите букмекер:", reply_markup=casino_keyboard("dep"))
    await state.set_state(DepositStates.choose_casino)

@router.callback_query(F.data.startswith("dep_casino_"))
async def dep_casino(cb: CallbackQuery, state: FSMContext):
    casino = cb.data.split("dep_casino_")[1]
    await state.update_data(casino=casino)
    await cb.message.edit_text(
        f"🎰 Казино: {casino}\n\n"
        "📋 Проверьте ваш ID ещё раз\n"
        "❌ Отменить пополнение нельзя!!\n\n"
        f"▶▶▶ Отправьте ID вашего счёта {casino}:"
    )
    await state.set_state(DepositStates.enter_id)

@router.message(DepositStates.enter_id)
async def dep_enter_id(msg: Message, state: FSMContext):
    await state.update_data(casino_id=msg.text)
    await msg.answer(
        "🏦 Выберите банк для оплаты:",
        reply_markup=deposit_bank_keyboard(),
    )
    await state.set_state(DepositStates.choose_bank)

# Недоступные банки — показываем алерт
@router.callback_query(F.data.startswith("dep_bank_"))
async def dep_bank(cb: CallbackQuery, state: FSMContext):
    await cb.answer("⛔ Данный банк недоступен!", show_alert=True)

# Переход к сумме — через специальную кнопку или автоматически если QR включён
# Поскольку все банки недоступны, сделаем кнопку «Продолжить без банка» — нет.
# По логике оригинала пополнение идёт через QR. Добавим отдельный шаг.
@router.message(DepositStates.choose_bank)
async def dep_skip_bank(msg: Message, state: FSMContext):
    # пользователь написал что-то вручную — просим ввести сумму напрямую
    await dep_ask_amount(msg, state)

async def dep_ask_amount(msg: Message, state: FSMContext):
    global pending_qr
    data = await state.get_data()
    casino = data.get("casino", "")
    casino_id = data.get("casino_id", "")

    await msg.answer(
        "🚀 Введите сумму пополнения:\n"
        "📌 Min: 100\n"
        "📌 Max: 100 000",
        reply_markup=amount_keyboard(),
    )
    await state.set_state(DepositStates.enter_amount)

@router.callback_query(F.data.startswith("amount_"), DepositStates.enter_amount)
async def dep_amount_btn(cb: CallbackQuery, state: FSMContext):
    amount = cb.data.split("amount_")[1]
    await state.update_data(amount=amount)
    await cb.message.edit_reply_markup()
    await process_deposit_payment(cb.message, state, amount)

@router.message(DepositStates.enter_amount)
async def dep_amount_text(msg: Message, state: FSMContext):
    if not msg.text.isdigit():
        await msg.answer("⚠️ Введите число.")
        return
    amount = msg.text
    await state.update_data(amount=amount)
    await process_deposit_payment(msg, state, amount)

async def process_deposit_payment(msg: Message, state: FSMContext, amount: str):
    global pending_qr, qr_mode
    data = await state.get_data()
    casino    = data.get("casino", "")
    casino_id = data.get("casino_id", "")

    # Анимация генерации QR
    gen_msg = await msg.answer("⏳ Генерирую QR...")
    delay = random.uniform(1, 3)
    await asyncio.sleep(delay)
    await gen_msg.delete()

    if qr_mode and pending_qr:
        # Отправляем QR
        if pending_qr.startswith("AgAC") or len(pending_qr) > 60:
            await msg.answer_photo(
                photo=pending_qr,
                caption=(
                    f"💳 Сумма к оплате: <b>{amount} сом</b>\n"
                    f"🎰 Казино: {casino}\n"
                    f"🆔 ID: {casino_id}\n\n"
                    "📌 Проверьте ваш ID ещё раз\n"
                    "❌ Отменить пополнение нельзя!!\n\n"
                    "✅ После оплаты нажмите «Пополнить» снова."
                ),
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )
        else:
            await msg.answer_photo(
                photo=FSInputFile(pending_qr),
                caption=(
                    f"💳 Сумма к оплате: <b>{amount} сом</b>\n"
                    f"🎰 Казино: {casino}\n"
                    f"🆔 ID: {casino_id}\n\n"
                    "📌 Проверьте ваш ID ещё раз\n"
                    "❌ Отменить пополнение нельзя!!\n\n"
                    "✅ После оплаты нажмите «Пополнить» снова."
                ),
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )
    else:
        await msg.answer(
            f"💳 Сумма к оплате: <b>{amount} сом</b>\n"
            f"🎰 Казино: {casino}\n"
            f"🆔 ID: {casino_id}\n\n"
            "📌 Проверьте ваш ID ещё раз\n"
            "❌ Отменить пополнение нельзя!!\n\n"
            "⚠️ QR не настроен, обратитесь к администратору.",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )

    # Уведомление админу
    notif = (
        f"🆕 <b>ЗАЯВКА НА ПОПОЛНЕНИЕ</b>\n\n"
        f"👤 Пользователь: <a href='tg://user?id={msg.from_user.id}'>{msg.from_user.full_name}</a>\n"
        f"🆔 Chat ID: <code>{msg.from_user.id}</code>\n"
        f"🎰 Казино: {casino}\n"
        f"🎫 ID счёта: <code>{casino_id}</code>\n"
        f"💰 Сумма: {amount} сом"
    )
    await send_admin_notification(notif, msg.from_user.id)
    await state.clear()

# ─── ВЫВОД ───────────────────────────────────────────────────────────────────
@router.message(F.text == "💸 Вывести")
async def wd_start(msg: Message, state: FSMContext):
    if is_blocked(msg.from_user.id):
        await msg.answer("❌ Вы заблокированы.")
        return
    await state.clear()
    await msg.answer("🎰 Выберите букмекер:", reply_markup=casino_keyboard("wd"))
    await state.set_state(WithdrawStates.choose_casino)

@router.callback_query(F.data.startswith("wd_casino_"))
async def wd_casino(cb: CallbackQuery, state: FSMContext):
    casino = cb.data.split("wd_casino_")[1]
    await state.update_data(casino=casino)
    await cb.message.edit_text(
        f"🎰 Казино: {casino}\n\n🏦 Выберите банк:",
        reply_markup=withdraw_bank_keyboard(),
    )
    await state.set_state(WithdrawStates.choose_bank)

@router.callback_query(F.data.startswith("wd_bank_"), WithdrawStates.choose_bank)
async def wd_bank(cb: CallbackQuery, state: FSMContext):
    bank_map = {
        "wd_bank_kompanyon": "Компаньон",
        "wd_bank_obank":     "O банк",
        "wd_bank_bakai":     "Bakai",
        "wd_bank_balance":   "Balance.kg",
        "wd_bank_megapay":   "MegaPay",
        "wd_bank_mbank":     "MBank",
    }
    bank = bank_map.get(cb.data, cb.data)
    await state.update_data(bank=bank)
    await cb.message.edit_text(
        f"🎰 Казино: {(await state.get_data()).get('casino','')}\n"
        f"🏦 Банк: {bank}\n\n"
        "📱 Введите номер телефона (+996):"
    )
    await state.set_state(WithdrawStates.enter_phone)

@router.message(WithdrawStates.enter_phone)
async def wd_phone(msg: Message, state: FSMContext):
    await state.update_data(phone=msg.text)
    await msg.answer("📷 Отправьте фото QR кода от банка:")
    await state.set_state(WithdrawStates.send_qr)

@router.message(WithdrawStates.send_qr, F.photo)
async def wd_qr(msg: Message, state: FSMContext):
    file_id = msg.photo[-1].file_id
    await state.update_data(qr_file_id=file_id)
    data = await state.get_data()
    casino = data.get("casino", "")
    await msg.answer(
        f"▶▶▶ Заходим 👇\n"
        "1. Настройки!\n"
        "2. Вывести со счёта!\n"
        "3. Касса\n"
        "4. Сумму для Вывода!\n"
        "г. Бишкек, офис AuraPay\n"
        "5. Подтвердить\n"
        "6. Получить Код!\n"
        "7. Отправить его нам\n\n"
        f"🆔 Введите ID вашего счёта {casino}:"
    )
    await state.set_state(WithdrawStates.enter_casino_id)

@router.message(WithdrawStates.send_qr)
async def wd_qr_no_photo(msg: Message):
    await msg.answer("📷 Пожалуйста, отправьте именно фото QR кода.")

@router.message(WithdrawStates.enter_casino_id)
async def wd_casino_id(msg: Message, state: FSMContext):
    await state.update_data(casino_id=msg.text)
    await msg.answer("🔑 Введите код с сайта казино:")
    await state.set_state(WithdrawStates.enter_code)

@router.message(WithdrawStates.enter_code)
async def wd_code(msg: Message, state: FSMContext):
    # Анимация проверки
    check_msg = await msg.answer("🔍 Проверяю код...")
    delay = random.uniform(1, 2)
    await asyncio.sleep(delay)
    await check_msg.delete()

    await msg.answer(
        "✅ Заявка отправлена на рассмотр.\n"
        "⏳ Ожидайте ответа бота.",
        reply_markup=main_keyboard(),
    )

    data = await state.get_data()
    casino    = data.get("casino", "")
    bank      = data.get("bank", "")
    phone     = data.get("phone", "")
    casino_id = data.get("casino_id", "")
    qr_fid    = data.get("qr_file_id", "")
    code      = msg.text

    notif = (
        f"🆕 <b>ЗАЯВКА НА ВЫВОД</b>\n\n"
        f"👤 Пользователь: <a href='tg://user?id={msg.from_user.id}'>{msg.from_user.full_name}</a>\n"
        f"🆔 Chat ID: <code>{msg.from_user.id}</code>\n"
        f"🎰 Казино: {casino}\n"
        f"🏦 Банк: {bank}\n"
        f"📱 Телефон: {phone}\n"
        f"🎫 ID счёта: <code>{casino_id}</code>\n"
        f"🔑 Код: <code>{code}</code>"
    )
    if qr_fid:
        try:
            await bot.send_photo(
                ADMIN_CHAT_ID,
                photo=qr_fid,
                caption=notif,
                reply_markup=admin_user_keyboard(msg.from_user.id),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Admin photo error: {e}")
            await send_admin_notification(notif, msg.from_user.id)
    else:
        await send_admin_notification(notif, msg.from_user.id)

    await state.clear()

# ─── Отмена ──────────────────────────────────────────────────────────────────
@router.message(F.text == "❌ Операция отменена")
async def cancel_op(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("❌ Операция отменена.", reply_markup=main_keyboard())

# ─── ADMIN: /qr ──────────────────────────────────────────────────────────────
@router.message(Command("qr"))
async def cmd_qr(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    global qr_mode
    qr_mode = True
    await msg.answer(
        "📲 Режим QR включён.\n"
        "Отправьте изображение QR-кода, который будет показываться пользователям при оплате:"
    )
    await state.set_state(AdminStates.set_qr)

@router.message(AdminStates.set_qr, F.photo)
async def admin_set_qr(msg: Message, state: FSMContext):
    global pending_qr
    pending_qr = msg.photo[-1].file_id
    await msg.answer("✅ QR-код сохранён! Теперь пользователи будут видеть его при пополнении.")
    await state.clear()

@router.message(AdminStates.set_qr)
async def admin_set_qr_no_photo(msg: Message):
    await msg.answer("📷 Пожалуйста, отправьте именно фото QR-кода.")

# ─── ADMIN: /cob ─────────────────────────────────────────────────────────────
@router.message(Command("cob"))
async def cmd_cob(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    parts = msg.text.split(maxsplit=2)
    # /cob <chat_id> <сообщение>
    if len(parts) >= 3:
        target_id = int(parts[1])
        text      = parts[2]
        try:
            await bot.send_message(target_id, f"📩 Сообщение от администратора:\n\n{text}")
            await msg.answer(f"✅ Сообщение отправлено пользователю {target_id}.")
        except Exception as e:
            await msg.answer(f"❌ Ошибка: {e}")
    else:
        await msg.answer(
            "📝 Использование: /cob <chat_id> <сообщение>\n"
            "Пример: /cob 123456789 Ваша заявка одобрена!"
        )

# ─── ADMIN: кнопки ───────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("adm_block_"))
async def adm_block(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        return
    uid = int(cb.data.split("adm_block_")[1])
    blocked_users.add(uid)
    await cb.answer(f"🚫 Пользователь {uid} заблокирован.", show_alert=True)

@router.callback_query(F.data.startswith("adm_unblock_"))
async def adm_unblock(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        return
    uid = int(cb.data.split("adm_unblock_")[1])
    blocked_users.discard(uid)
    await cb.answer(f"✅ Пользователь {uid} разблокирован.", show_alert=True)

@router.callback_query(F.data.startswith("adm_write_"))
async def adm_write(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return
    uid = int(cb.data.split("adm_write_")[1])
    await state.update_data(write_target=uid)
    await cb.message.answer(
        f"✍️ Введите сообщение для пользователя {uid}.\n"
        "(или используйте /cob <chat_id> <текст>)"
    )
    await state.set_state(AdminStates.cob_message)

@router.message(AdminStates.cob_message)
async def adm_cob_msg(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS:
        return
    data = await state.get_data()
    uid  = data.get("write_target")
    if uid:
        try:
            await bot.send_message(uid, f"📩 Сообщение от администратора:\n\n{msg.text}")
            await msg.answer(f"✅ Отправлено пользователю {uid}.")
        except Exception as e:
            await msg.answer(f"❌ Ошибка: {e}")
    await state.clear()

# ─── Запуск ──────────────────────────────────────────────────────────────────
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
