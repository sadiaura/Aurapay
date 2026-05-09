import os
import asyncio
import random
import logging
import time
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

# Хранилище заявок: uid -> {casino, amount, bank, type, sent_at}
pending_requests: dict = {}

# ── состояния ──────────────────────────────────────────────────────────────
(
    DEP_CASINO, DEP_ID, DEP_AMOUNT, DEP_BANK, DEP_RECEIPT,
    WD_CASINO, WD_BANK, WD_PHONE, WD_QR, WD_CID, WD_CODE,
    ADM_QR,
) = range(12)

# ── главная клавиатура ─────────────────────────────────────────────────────
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
    banks = ["O!Bank", "MBank", "Optima Bank", "Demir Bank", "Bakai Bank", "MegaPay"]
    rows = []
    for i in range(0, len(banks), 2):
        row = [InlineKeyboardButton(f"🏦 {b} 🚫", callback_data=f"depbank_{b}")
               for b in banks[i:i+2]]
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
def admin_ikb(uid, status=None):
    if status == "approved":
        top_row = [InlineKeyboardButton("✅ ОДОБРЕНО", callback_data="noop")]
    elif status == "declined":
        top_row = [InlineKeyboardButton("❌ ОТКЛОНЕНО", callback_data="noop")]
    else:
        top_row = [
            InlineKeyboardButton("✅ Одобрить",  callback_data=f"approve_{uid}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"decline_{uid}"),
        ]
    return InlineKeyboardMarkup([
        top_row,
        [InlineKeyboardButton("✍️ Написать",       callback_data=f"awrite_{uid}"),
         InlineKeyboardButton("🚫 Заблокировать",  callback_data=f"ablock_{uid}")],
        [InlineKeyboardButton("🔓 Разблокировать", callback_data=f"aunblock_{uid}")],
    ])

# ── красивое сообщение пользователю о статусе заявки ──────────────────────
def build_status_msg(uid: int, status: str, elapsed_sec: int) -> str:
    req = pending_requests.get(uid, {})
    req_type  = req.get("type", "пополнение")
    casino    = req.get("casino", "—")
    amount    = req.get("amount", "—")
    bank      = req.get("bank", "—")

    # Заголовок
    if status == "approved":
        header = "✅ <b>ЗАЯВКА ОДОБРЕНА</b>"
        status_line = "✅ Одобрено"
    else:
        header = "❌ <b>ЗАЯВКА ОТКЛОНЕНА</b>"
        status_line = "❌ Отклонено"

    # Тип заявки
    type_label = "💰 Пополнение" if req_type == "deposit" else "💸 Вывод"

    # Таймер
    timer = f"{elapsed_sec}с"

    lines = [
        header,
        "",
        f"{'Тип':<14} {'Букмекер':<12} {'Сумма':<10}",
        f"{'─'*14} {'─'*12} {'─'*10}",
        f"{type_label:<14} {casino:<12} {amount + ' сом':<10}",
        "",
    ]

    if req_type == "deposit":
        lines.append(f"🏦 <b>Банк:</b> {bank}")

    lines += [
        f"⏱ <b>Рассмотрено за:</b> {timer}",
        f"📋 <b>Статус:</b> {status_line}",
    ]

    if status == "declined":
        lines.append("\nЕсли есть вопросы — @aurapay_support_bot")

    return "\n".join(lines)

# ── уведомление админу ─────────────────────────────────────────────────────
async def notify_admin(app, text, uid, photo=None):
    if not ADMIN_CHAT_ID:
        return
    try:
        if photo:
            await app.bot.send_photo(
                ADMIN_CHAT_ID, photo=photo,
                caption=text, reply_markup=admin_ikb(uid),
                parse_mode="HTML"
            )
        else:
            await app.bot.send_message(
                ADMIN_CHAT_ID, text,
                reply_markup=admin_ikb(uid),
                parse_mode="HTML"
            )
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
        f"Привет, {name}!\n\n"
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
        "1. Нажмите «Пополнить»\n"
        "2. Выберите казино\n"
        "3. Введите ID вашего счёта\n"
        "4. Введите сумму\n"
        "5. Выберите банк и оплатите по QR\n"
        "6. Отправьте фото чека об оплате\n\n"
        "💸 <b>ВЫВОД:</b>\n"
        "1. Нажмите «Вывести»\n"
        "2. Выберите казино\n"
        "3. Выберите банк\n"
        "4. Введите номер телефона (+996)\n"
        "5. Отправьте фото QR кода от банка\n"
        "6. Введите ID счёта в казино\n"
        "7. Введите код с сайта казино\n\n"
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
    await _show_qr(update, ctx, amount, q.message)
    return DEP_BANK

async def dep_amount_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if not txt.isdigit():
        await update.message.reply_text("⚠️ Введите число.")
        return DEP_AMOUNT
    amount = txt
    ctx.user_data["dep_amount"] = amount
    await _show_qr(update, ctx, amount, update.message)
    return DEP_BANK

async def _show_qr(update, ctx, amount, msg_obj):
    casino    = ctx.user_data.get("dep_casino", "")
    casino_id = ctx.user_data.get("dep_id", "")

    gen = await msg_obj.reply_text("⏳ Генерирую QR...")
    await asyncio.sleep(random.uniform(2, 3))
    await gen.delete()

    caption = (
        f"💳 Сумма к оплате: <b>{amount} сом</b>\n"
        f"🎰 Казино: {casino}\n"
        f"🆔 ID: {casino_id}\n\n"
        "📌 Проверьте ваш ID ещё раз\n"
        "❌ Отменить пополнение нельзя!!\n\n"
        "🏦 Выберите банк для оплаты:"
    )

    if qr_mode and pending_qr:
        await msg_obj.reply_photo(
            photo=pending_qr,
            caption=caption,
            parse_mode="HTML",
            reply_markup=dep_bank_payment_ikb()
        )
    else:
        await msg_obj.reply_text(
            caption + "\n\n⚠️ QR не настроен. Используйте /qr чтобы загрузить QR-код.",
            parse_mode="HTML",
            reply_markup=dep_bank_payment_ikb()
        )

# ── Банк нажат → только алерт НЕ ДОСТУПНО ────────────────────────────────
async def dep_bank_alert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("⛔ Данный банк недоступен!", show_alert=True)
    return DEP_BANK

async def dep_bank_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📷 Оплатите по QR и отправьте фото чека об оплате.")
    return DEP_BANK

# ── Чек получен → отправляем заявку админу ────────────────────────────────
async def dep_receipt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("📷 Отправьте фото чека об оплате.")
        return DEP_BANK

    receipt_fid = update.message.photo[-1].file_id
    uid         = update.effective_user.id
    casino      = ctx.user_data.get("dep_casino", "")
    casino_id   = ctx.user_data.get("dep_id", "")
    amount      = ctx.user_data.get("dep_amount", "")

    # Сохраняем данные заявки + время отправки
    pending_requests[uid] = {
        "type":     "deposit",
        "casino":   casino,
        "amount":   amount,
        "bank":     "—",
        "sent_at":  time.time(),
    }

    await update.message.reply_text(
        "✅ <b>Заявка на пополнение отправлена!</b>\n\n"
        "⏳ Ожидайте — оператор обработает её в ближайшее время.\n"
        "💬 Поддержка: @aurapay_support_bot",
        parse_mode="HTML",
        reply_markup=main_kb()
    )

    notif = (
        f"🆕 <b>ЗАЯВКА НА ПОПОЛНЕНИЕ</b>\n\n"
        f"👤 <a href='tg://user?id={uid}'>{update.effective_user.full_name}</a>\n"
        f"🆔 Chat ID: <code>{uid}</code>\n"
        f"🎰 Казино: {casino}\n"
        f"🎫 ID счёта: <code>{casino_id}</code>\n"
        f"💰 Сумма: {amount} сом\n"
        f"🧾 Чек: прикреплён"
    )
    await notify_admin(ctx.application, notif, uid, photo=receipt_fid)
    return ConversationHandler.END

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
    code   = update.message.text
    uid    = update.effective_user.id
    casino = ctx.user_data.get("wd_casino", "")
    bank   = ctx.user_data.get("wd_bank", "")
    phone  = ctx.user_data.get("wd_phone", "")
    cid    = ctx.user_data.get("wd_cid", "")
    qr_fid = ctx.user_data.get("wd_qr", "")

    check = await update.message.reply_text("🔍 Проверяю код...")
    await asyncio.sleep(random.uniform(1, 2))
    await check.delete()

    # Сохраняем данные заявки + время отправки
    pending_requests[uid] = {
        "type":    "withdraw",
        "casino":  casino,
        "amount":  "—",   # при выводе сумма не вводится пользователем
        "bank":    bank,
        "sent_at": time.time(),
    }

    await update.message.reply_text(
        "✅ <b>Заявка на вывод отправлена!</b>\n\n"
        "⏳ Ожидайте — оператор обработает её в ближайшее время.\n"
        "💬 Поддержка: @aurapay_support_bot",
        parse_mode="HTML",
        reply_markup=main_kb()
    )

    notif = (
        f"🆕 <b>ЗАЯВКА НА ВЫВОД</b>\n\n"
        f"👤 <a href='tg://user?id={uid}'>{update.effective_user.full_name}</a>\n"
        f"🆔 Chat ID: <code>{uid}</code>\n"
        f"🎰 Казино: {casino}\n🏦 Банк: {bank}\n"
        f"📱 Телефон: {phone}\n"
        f"🎫 ID счёта: <code>{cid}</code>\n"
        f"🔑 Код: <code>{code}</code>"
    )
    await notify_admin(ctx.application, notif, uid, photo=qr_fid if qr_fid else None)
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
#  Отмена
# ════════════════════════════════════════════════════════════════════════════
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Операция отменена.", reply_markup=main_kb())
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
#  ADMIN /qr
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
    await update.message.reply_text("✅ QR-код сохранён!")
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
#  ADMIN /cob
# ════════════════════════════════════════════════════════════════════════════
async def cmd_cob(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    parts = update.message.text.split(maxsplit=2)
    if len(parts) >= 3:
        try:
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

    if data == "noop":
        await q.answer()
        return

    if data.startswith("approve_") or data.startswith("decline_"):
        is_approve = data.startswith("approve_")
        uid = int(data.split("_")[1])
        status = "approved" if is_approve else "declined"

        # Считаем время рассмотрения
        req = pending_requests.get(uid, {})
        sent_at = req.get("sent_at", time.time())
        elapsed = max(1, int(time.time() - sent_at))

        # Отправляем пользователю красивое сообщение со статусом
        try:
            msg = build_status_msg(uid, status, elapsed)
            await ctx.bot.send_message(uid, msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить статус пользователю {uid}: {e}")

        # Обновляем сообщение в админ-чате
        status_text = "✅ СТАТУС: ОДОБРЕНО" if is_approve else "❌ СТАТУС: ОТКЛОНЕНО"
        try:
            new_markup = admin_ikb(uid, status=status)
            suffix = f"\n\n{status_text} | ⏱ {elapsed}с"
            if q.message.photo:
                await q.message.edit_caption(
                    caption=(q.message.caption or "") + suffix,
                    parse_mode="HTML",
                    reply_markup=new_markup
                )
            else:
                await q.message.edit_text(
                    text=(q.message.text or "") + suffix,
                    parse_mode="HTML",
                    reply_markup=new_markup
                )
        except Exception as e:
            logger.error(e)

        answer_text = "✅ Одобрено!" if is_approve else "❌ Отклонено!"
        await q.answer(answer_text)

    elif data.startswith("ablock_"):
        uid = int(data.split("ablock_")[1])
        blocked_users.add(uid)
        try:
            await ctx.bot.send_message(uid, "🚫 Вы заблокированы в боте.")
        except:
            pass
        await q.answer(f"🚫 Пользователь {uid} заблокирован", show_alert=True)

    elif data.startswith("aunblock_"):
        uid = int(data.split("aunblock_")[1])
        blocked_users.discard(uid)
        try:
            await ctx.bot.send_message(uid, "✅ Ваш аккаунт разблокирован.")
        except:
            pass
        await q.answer(f"✅ Пользователь {uid} разблокирован", show_alert=True)

    elif data.startswith("awrite_"):
        uid = int(data.split("awrite_")[1])
        await q.answer()
        await q.message.reply_text(f"✍️ Используйте команду:\n/cob {uid} ваше_сообщение")

# ════════════════════════════════════════════════════════════════════════════
#  Запуск
# ════════════════════════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    dep_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💰 Пополнить$"), dep_start)],
        states={
            DEP_CASINO: [CallbackQueryHandler(dep_casino, pattern="^dep_")],
            DEP_ID:     [MessageHandler(filters.TEXT & ~filters.COMMAND, dep_id)],
            DEP_AMOUNT: [
                CallbackQueryHandler(dep_amount_cb, pattern="^amt_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, dep_amount_text),
            ],
            DEP_BANK: [
                CallbackQueryHandler(dep_bank_alert, pattern="^depbank_"),
                MessageHandler(filters.PHOTO, dep_receipt),
                MessageHandler(filters.TEXT & ~filters.COMMAND, dep_bank_text),
            ],
        },
        fallbacks=[MessageHandler(filters.Regex("^❌"), cancel)],
        allow_reentry=True,
    )

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
    app.add_handler(CallbackQueryHandler(cb_lang,  pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(cb_admin, pattern="^(approve|decline|ablock|aunblock|awrite|noop)"))

    logger.info("✅ Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
