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
BOT_USERNAME  = os.getenv("BOT_USERNAME", "YourBot")  # без @, напр. AuraPayBot

blocked_users = set()
qr_mode       = False
pending_qr    = None
bot_paused    = False

pending_requests: dict = {}
all_requests:     dict = {}

# ─── Реферальная система ─────────────────────────────────────────────────────
# referral_data[uid] = {
#   "referrer": int | None,      — кто пригласил
#   "referrals": [uid, ...],     — кого пригласил сам
#   "earned": float,             — всего заработано сомов
#   "total_dep": float,          — суммарное пополнение рефералов (для уровня %)
# }
referral_data: dict = {}

# Уровни процента по сумме пополнений реферала
REFERRAL_TIERS = [
    (100_000, 10),
    (50_000,   6),
    (30_000,   5),
    (10_000,   4),
    (0,        3),
]

def get_ref_percent(total_dep: float) -> int:
    for threshold, pct in REFERRAL_TIERS:
        if total_dep >= threshold:
            return pct
    return 3

# Вывод реферальных: мин 150, макс 100 000
REF_WD_MIN = 150
REF_WD_MAX = 100_000

# Состояния для вывода реферальных
REF_WD_CASINO, REF_WD_ID, REF_WD_AMOUNT = range(20, 23)

(
    DEP_CASINO, DEP_ID, DEP_AMOUNT, DEP_BANK, DEP_RECEIPT,
    WD_CASINO, WD_BANK, WD_PHONE, WD_QR, WD_CID, WD_CODE,
    ADM_QR,
) = range(12)

# ════════════════════════════════════════════════════════════════════════════
#  УМНАЯ СИСТЕМА УДАЛЕНИЯ СООБЩЕНИЙ
# ════════════════════════════════════════════════════════════════════════════

async def safe_delete(bot, chat_id: int, msg_id: int):
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass

async def cleanup_msgs(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, key: str = "cleanup_ids"):
    ids = ctx.user_data.pop(key, [])
    for mid in ids:
        await safe_delete(ctx.bot, chat_id, mid)

def track(ctx: ContextTypes.DEFAULT_TYPE, *msg_ids, key: str = "cleanup_ids"):
    lst = ctx.user_data.setdefault(key, [])
    for mid in msg_ids:
        if mid and mid not in lst:
            lst.append(mid)

# ── главная клавиатура ─────────────────────────────────────────────────────
def main_kb():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("💰 Пополнить"), KeyboardButton("💸 Вывести")],
         [KeyboardButton("🤝 Реферальная"), KeyboardButton("📖 Инструкция")],
         [KeyboardButton("🌐 Язык")]],
        resize_keyboard=True
    )

def casino_ikb(prefix):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ 1xBet",  callback_data=f"{prefix}_1xBet")],
        [InlineKeyboardButton("🎰 Melbet",  callback_data=f"{prefix}_Melbet"),
         InlineKeyboardButton("🎰 1win",    callback_data=f"{prefix}_1win")],
        [InlineKeyboardButton("🎰 mostbet", callback_data=f"{prefix}_mostbet")],
    ])

# Казино для вывода реферальных (только хбет и мелбет)
def ref_casino_ikb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ 1xBet",  callback_data="refwd_casino_1xBet")],
        [InlineKeyboardButton("🎰 Melbet",  callback_data="refwd_casino_Melbet")],
        [InlineKeyboardButton("❌ Отменить", callback_data="refwd_cancel")],
    ])

def amount_ikb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 100",   callback_data="amt_100"),
         InlineKeyboardButton("💵 500",   callback_data="amt_500"),
         InlineKeyboardButton("💵 1000",  callback_data="amt_1000")],
        [InlineKeyboardButton("💵 5000",  callback_data="amt_5000"),
         InlineKeyboardButton("💵 10000", callback_data="amt_10000")],
    ])

def dep_bank_payment_ikb():
    banks = ["O!Bank", "MBank", "Optima Bank", "Demir Bank", "Bakai Bank", "MegaPay"]
    rows = []
    for i in range(0, len(banks), 2):
        row = [InlineKeyboardButton(f"🏦 {b} 🚫", callback_data=f"depbank_{b}")
               for b in banks[i:i+2]]
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Отменить", callback_data="dep_cancel")])
    return InlineKeyboardMarkup(rows)

def wd_bank_ikb():
    banks = [("Компаньон","kompanyon"), ("O банк","obank"),
             ("Bakai","bakai"),         ("Balance.kg","balance"),
             ("MegaPay","megapay"),     ("MBank","mbank")]
    rows = []
    for i in range(0, len(banks), 2):
        row = [InlineKeyboardButton(f"🏦 {n}", callback_data=f"wdbank_{c}")
               for n, c in banks[i:i+2]]
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Отменить", callback_data="wd_cancel")])
    return InlineKeyboardMarkup(rows)

def lang_ikb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇷🇺 Русский",   callback_data="lang_ru")],
        [InlineKeyboardButton("🇰🇬 Кыргызча", callback_data="lang_ky")],
        [InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data="lang_uz")],
    ])

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

# ── Реферальное меню ──────────────────────────────────────────────────────
def ref_menu_ikb(uid: int):
    data = referral_data.get(uid, {})
    earned = data.get("earned", 0.0)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Вывести реферальные", callback_data="ref_withdraw")],
        [InlineKeyboardButton("🔗 Поделиться ссылкой",  callback_data="ref_share")],
        [InlineKeyboardButton("🏆 Топ-10 рефереров",    callback_data="ref_top10")],
    ])

def build_status_msg(uid: int, status: str, elapsed_sec: int) -> str:
    req = pending_requests.get(uid, {})
    req_type  = req.get("type", "пополнение")
    casino    = req.get("casino", "—")
    amount    = req.get("amount", "—")
    bank      = req.get("bank", "—")

    if status == "approved":
        header = "✅ <b>ЗАЯВКА ОДОБРЕНА</b>"
        status_line = "✅ Одобрено"
    else:
        header = "❌ <b>ЗАЯВКА ОТКЛОНЕНА</b>"
        status_line = "❌ Отклонено"

    type_label = "💰 Пополнение" if req_type == "deposit" else "💸 Вывод"
    timer = f"{elapsed_sec}с"

    lines = [
        header, "",
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
        lines.append("\n💬 Поддержка: @Aurapay_supportbot")
    return "\n".join(lines)

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
#  /commands — справка для админа
# ════════════════════════════════════════════════════════════════════════════
async def cmd_commands(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text(
        "📋 <b>Доступные команды админа</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔧 <b>Управление ботом</b>\n"
        "/status — статус бота (вкл/выкл) + кнопка паузы\n"
        "/commands — эта справка\n\n"
        "📦 <b>Заявки</b>\n"
        "/zayavki — все ожидающие заявки\n\n"
        "💬 <b>Пользователи</b>\n"
        "/cob &lt;chat_id&gt; &lt;текст&gt; — написать пользователю\n"
        "Пример: /cob 123456789 Ваша заявка одобрена!\n\n"
        "📸 <b>QR-код</b>\n"
        "/qr — загрузить новый QR-код для оплаты\n\n"
        "🤝 <b>Реферальная статистика</b>\n"
        "/refstats — топ рефереров и общая статистика\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 Одобрить/отклонить заявку можно прямо под сообщением заявки кнопками.",
        parse_mode="HTML"
    )

# ════════════════════════════════════════════════════════════════════════════
#  /status — статус бота с кнопкой паузы
# ════════════════════════════════════════════════════════════════════════════
def status_ikb():
    if bot_paused:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Включить бот", callback_data="bot_resume")
        ]])
    else:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("⏸ Поставить на паузу", callback_data="bot_pause")
        ]])

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    state_text = "🔴 <b>ПАУЗА</b> — пользователи видят заглушку" if bot_paused else "🟢 <b>АКТИВЕН</b> — бот работает в штатном режиме"
    pending_count = sum(1 for r in all_requests.values() if r.get("status") == "pending")
    await update.message.reply_text(
        f"⚙️ <b>Статус бота</b>\n\n"
        f"Режим: {state_text}\n"
        f"⏳ Заявок ожидают ответа: <b>{pending_count}</b>\n"
        f"🚫 Заблокировано пользователей: <b>{len(blocked_users)}</b>\n"
        f"📸 QR-код: {'✅ загружен' if pending_qr else '❌ не загружен'}",
        parse_mode="HTML",
        reply_markup=status_ikb()
    )

async def cb_bot_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global bot_paused
    q = update.callback_query
    if update.effective_user.id not in ADMIN_IDS:
        await q.answer("❌ Нет доступа", show_alert=True)
        return
    if q.data == "bot_pause":
        bot_paused = True
        await q.answer("⏸ Бот поставлен на паузу", show_alert=True)
    else:
        bot_paused = False
        await q.answer("▶️ Бот включён", show_alert=True)
    state_text = "🔴 <b>ПАУЗА</b> — пользователи видят заглушку" if bot_paused else "🟢 <b>АКТИВЕН</b> — бот работает в штатном режиме"
    pending_count = sum(1 for r in all_requests.values() if r.get("status") == "pending")
    await q.message.edit_text(
        f"⚙️ <b>Статус бота</b>\n\n"
        f"Режим: {state_text}\n"
        f"⏳ Заявок ожидают ответа: <b>{pending_count}</b>\n"
        f"🚫 Заблокировано пользователей: <b>{len(blocked_users)}</b>\n"
        f"📸 QR-код: {'✅ загружен' if pending_qr else '❌ не загружен'}",
        parse_mode="HTML",
        reply_markup=status_ikb()
    )

# ════════════════════════════════════════════════════════════════════════════
#  /zayavki — все ожидающие заявки
# ════════════════════════════════════════════════════════════════════════════
async def cmd_zayavki(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    pending = {uid: r for uid, r in all_requests.items() if r.get("status") == "pending"}
    if not pending:
        await update.message.reply_text("✅ Нет ожидающих заявок.")
        return
    lines = [f"📋 <b>Ожидающие заявки ({len(pending)})</b>\n"]
    for uid, r in pending.items():
        req_type  = "💰 Пополнение" if r.get("type") == "deposit" else "💸 Вывод"
        casino    = r.get("casino", "—")
        amount    = r.get("amount", "—")
        bank      = r.get("bank", "—")
        username  = r.get("username", "—")
        sent_at   = r.get("sent_at", 0)
        elapsed   = int(time.time() - sent_at)
        mins, secs = divmod(elapsed, 60)
        wait_str  = f"{mins}м {secs}с" if mins else f"{secs}с"
        lines.append(
            f"━━━━━━━━━━━━━━\n"
            f"👤 {username} | 🆔 <code>{uid}</code>\n"
            f"{req_type} | 🎰 {casino}\n"
            f"💵 {amount} сом | 🏦 {bank}\n"
            f"⏱ Ждёт: {wait_str}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ════════════════════════════════════════════════════════════════════════════
#  /refstats — реферальная статистика (для админа)
# ════════════════════════════════════════════════════════════════════════════
async def cmd_refstats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not referral_data:
        await update.message.reply_text("📊 Реферальных данных пока нет.")
        return
    sorted_users = sorted(referral_data.items(), key=lambda x: x[1].get("earned", 0), reverse=True)
    lines = ["🏆 <b>Топ рефереров (все)</b>\n"]
    for i, (uid, data) in enumerate(sorted_users[:20], 1):
        earned   = data.get("earned", 0)
        refs_cnt = len(data.get("referrals", []))
        pct      = get_ref_percent(data.get("total_dep", 0))
        lines.append(f"{i}. 🆔<code>{uid}</code> | 👥{refs_cnt} реф | 💰{earned:.0f} сом | {pct}%")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ════════════════════════════════════════════════════════════════════════════
#  /start
# ════════════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    args = ctx.args  # параметры после /start

    if uid in blocked_users:
        await update.message.reply_text("❌ Вы заблокированы.")
        return ConversationHandler.END
    if bot_paused and uid not in ADMIN_IDS:
        await update.message.reply_text(
            "⏸ <b>Бот на паузе</b>\n\n"
            "🔧 Ведутся технические работы.\n"
            "Пожалуйста, попробуйте позже.\n\n"
            "💬 Поддержка: @Aurapay_supportbot",
            parse_mode="HTML"
        )
        return ConversationHandler.END

    # ── Обработка реферальной ссылки ──────────────────────────────────────
    if args and args[0].startswith("ref"):
        try:
            referrer_id = int(args[0][3:])
            if referrer_id != uid and uid not in referral_data:
                # Регистрируем нового пользователя под рефером
                if uid not in referral_data:
                    referral_data[uid] = {"referrer": referrer_id, "referrals": [], "earned": 0.0, "total_dep": 0.0}
                else:
                    referral_data[uid]["referrer"] = referrer_id

                # Добавляем реферала к пригласившему
                if referrer_id not in referral_data:
                    referral_data[referrer_id] = {"referrer": None, "referrals": [], "earned": 0.0, "total_dep": 0.0}
                if uid not in referral_data[referrer_id]["referrals"]:
                    referral_data[referrer_id]["referrals"].append(uid)

                # Уведомляем пригласившего
                try:
                    ref_name = update.effective_user.full_name
                    await ctx.bot.send_message(
                        referrer_id,
                        f"🎉 <b>У вас новый реферал!</b>\n\n"
                        f"👤 {ref_name} зарегистрировался по вашей ссылке.\n"
                        f"💡 Вы будете получать % с каждого его пополнения!",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
        except ValueError:
            pass

    # Инициализируем реферальные данные если нет
    if uid not in referral_data:
        referral_data[uid] = {"referrer": None, "referrals": [], "earned": 0.0, "total_dep": 0.0}

    name = update.effective_user.full_name
    await update.message.reply_text(
        f"Привет, {name}!\n\n"
        "⚡ Авто-пополнение: 0%\n"
        "⚡ Авто-вывод: 0%\n"
        "🌟 Работаем: 24/7\n\n"
        "💬 Поддержка: @Aurapay_supportbot",
        reply_markup=main_kb()
    )
    return ConversationHandler.END

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
#  РЕФЕРАЛЬНЫЙ РАЗДЕЛ
# ════════════════════════════════════════════════════════════════════════════
async def cmd_referral(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    if uid in blocked_users:
        await update.message.reply_text("❌ Вы заблокированы.")
        return
    if uid not in referral_data:
        referral_data[uid] = {"referrer": None, "referrals": [], "earned": 0.0, "total_dep": 0.0}

    data     = referral_data[uid]
    earned   = data.get("earned", 0.0)
    refs_cnt = len(data.get("referrals", []))
    total_d  = data.get("total_dep", 0.0)
    pct      = get_ref_percent(total_d)

    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref{uid}"

    # Следующий уровень
    next_tier_txt = ""
    for threshold, p in sorted(REFERRAL_TIERS, key=lambda x: x[0]):
        if total_d < threshold:
            next_tier_txt = f"📈 До {p}%: пополнить ещё <b>{threshold - total_d:.0f} сом</b>\n"
            break

    await update.message.reply_text(
        f"🤝 <b>Реферальная программа</b>\n\n"
        f"🔗 Ваша ссылка:\n<code>{ref_link}</code>\n\n"
        f"👥 Приглашено рефералов: <b>{refs_cnt}</b>\n"
        f"💰 Заработано: <b>{earned:.2f} сом</b>\n"
        f"📊 Ваш % с пополнений: <b>{pct}%</b>\n\n"
        f"{next_tier_txt}"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <b>Уровни реферального %:</b>\n"
        f"  • Базовый: 3%\n"
        f"  • Рефералы пополнили 10к+: 4%\n"
        f"  • 30к+: 5%\n"
        f"  • 50к+: 6%\n"
        f"  • 100к+: 10%\n\n"
        f"Приглашай друзей, пополняй и зарабатывай!",
        parse_mode="HTML",
        reply_markup=ref_menu_ikb(uid)
    )

# ── Callback кнопок реферального меню ─────────────────────────────────────
async def cb_ref_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = update.effective_user.id
    await q.answer()

    if q.data == "ref_share":
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref{uid}"
        share_text = (
            f"💰 Пополняй и выводи деньги на букмекерах через AuraPay!\n"
            f"Быстро, надёжно, 24/7.\n\n"
            f"👉 {ref_link}"
        )
        # Кнопка для шаринга через telegram
        share_url = f"https://t.me/share/url?url={ref_link}&text=Пополняй+и+выводи+через+AuraPay!"
        await q.message.reply_text(
            f"📤 <b>Поделитесь своей ссылкой:</b>\n\n"
            f"<code>{ref_link}</code>\n\n"
            f"Скопируйте ссылку или нажмите кнопку ниже👇",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📲 Поделиться", url=share_url)],
                [InlineKeyboardButton("◀️ Назад", callback_data="ref_back")],
            ])
        )

    elif q.data == "ref_top10":
        sorted_users = sorted(
            referral_data.items(),
            key=lambda x: x[1].get("earned", 0),
            reverse=True
        )
        medals = ["🥇", "🥈", "🥉"]
        lines = ["🏆 <b>Топ-10 по реферальным заработкам</b>\n"]
        for i, (u_id, udata) in enumerate(sorted_users[:10], 1):
            earned   = udata.get("earned", 0)
            refs_cnt = len(udata.get("referrals", []))
            medal    = medals[i-1] if i <= 3 else f"{i}."
            # Показываем анонимно (только первые 4 цифры ID)
            anon_id  = str(u_id)[:4] + "****"
            lines.append(f"{medal} ID {anon_id} | 👥{refs_cnt} | 💰{earned:.0f} сом")

        if len(sorted_users) == 0:
            lines.append("Пока никто не заработал реферальных 😔")

        # Позиция текущего пользователя
        my_rank = next((i+1 for i, (u, _) in enumerate(sorted_users) if u == uid), None)
        if my_rank and my_rank > 10:
            lines.append(f"\n📌 Ваша позиция: #{my_rank}")

        await q.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="ref_back")]])
        )

    elif q.data == "ref_withdraw":
        data   = referral_data.get(uid, {})
        earned = data.get("earned", 0.0)
        if earned < REF_WD_MIN:
            await q.answer(
                f"❌ Недостаточно средств!\nМинимум для вывода: {REF_WD_MIN} сом\nВаш баланс: {earned:.2f} сом",
                show_alert=True
            )
            return
        # Начинаем диалог вывода
        ctx.user_data["refwd_balance"] = earned
        await q.message.reply_text(
            f"💸 <b>Вывод реферального заработка</b>\n\n"
            f"💰 Ваш баланс: <b>{earned:.2f} сом</b>\n"
            f"📌 Мин. вывод: {REF_WD_MIN} сом\n"
            f"📌 Макс. вывод: {REF_WD_MAX:,} сом\n\n"
            f"🎰 Выберите букмекер для вывода:",
            parse_mode="HTML",
            reply_markup=ref_casino_ikb()
        )
        return REF_WD_CASINO

    elif q.data == "ref_back":
        data     = referral_data.get(uid, {})
        earned   = data.get("earned", 0.0)
        refs_cnt = len(data.get("referrals", []))
        total_d  = data.get("total_dep", 0.0)
        pct      = get_ref_percent(total_d)
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref{uid}"
        await q.message.edit_text(
            f"🤝 <b>Реферальная программа</b>\n\n"
            f"🔗 Ваша ссылка:\n<code>{ref_link}</code>\n\n"
            f"👥 Приглашено рефералов: <b>{refs_cnt}</b>\n"
            f"💰 Заработано: <b>{earned:.2f} сом</b>\n"
            f"📊 Ваш % с пополнений: <b>{pct}%</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 <b>Уровни реферального %:</b>\n"
            f"  • Базовый: 3%\n"
            f"  • 10к+: 4% | 30к+: 5% | 50к+: 6% | 100к+: 10%",
            parse_mode="HTML",
            reply_markup=ref_menu_ikb(uid)
        )

# ════════════════════════════════════════════════════════════════════════════
#  ВЫВОД РЕФЕРАЛЬНЫХ — ConversationHandler
# ════════════════════════════════════════════════════════════════════════════
async def refwd_casino(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "refwd_cancel":
        await q.message.reply_text("❌ Вывод отменён.", reply_markup=main_kb())
        return ConversationHandler.END
    casino = q.data.replace("refwd_casino_", "")
    ctx.user_data["refwd_casino"] = casino
    balance = ctx.user_data.get("refwd_balance", 0)
    await q.edit_message_text(
        f"🎰 Букмекер: <b>{casino}</b>\n\n"
        f"💰 Ваш баланс: <b>{balance:.2f} сом</b>\n\n"
        f"🆔 Введите ваш ID на {casino}:",
        parse_mode="HTML"
    )
    return REF_WD_ID

async def refwd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["refwd_id"] = update.message.text.strip()
    balance = ctx.user_data.get("refwd_balance", 0)
    await update.message.reply_text(
        f"💰 Доступно для вывода: <b>{balance:.2f} сом</b>\n"
        f"📌 Мин: {REF_WD_MIN} | Макс: {REF_WD_MAX:,}\n\n"
        f"✏️ Введите сумму вывода:",
        parse_mode="HTML"
    )
    return REF_WD_AMOUNT

async def refwd_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    txt     = update.message.text.strip()
    balance = ctx.user_data.get("refwd_balance", 0.0)

    if not txt.replace(".", "").isdigit():
        await update.message.reply_text("⚠️ Введите корректную сумму (число).")
        return REF_WD_AMOUNT

    amount = float(txt)

    if amount < REF_WD_MIN:
        await update.message.reply_text(
            f"⚠️ Минимальная сумма вывода: <b>{REF_WD_MIN} сом</b>",
            parse_mode="HTML"
        )
        return REF_WD_AMOUNT

    if amount > REF_WD_MAX:
        await update.message.reply_text(
            f"⚠️ Максимальная сумма вывода: <b>{REF_WD_MAX:,} сом</b>",
            parse_mode="HTML"
        )
        return REF_WD_AMOUNT

    if amount > balance:
        await update.message.reply_text(
            f"❌ Недостаточно средств!\n💰 Ваш баланс: <b>{balance:.2f} сом</b>",
            parse_mode="HTML"
        )
        return REF_WD_AMOUNT

    casino = ctx.user_data.get("refwd_casino", "—")
    acc_id = ctx.user_data.get("refwd_id", "—")

    await update.message.reply_text(
        f"✅ <b>Заявка на вывод реферального заработка отправлена!</b>\n\n"
        f"🎰 Букмекер: {casino}\n"
        f"🆔 ID: {acc_id}\n"
        f"💰 Сумма: {amount:.2f} сом\n\n"
        f"⏳ Ожидайте обработки оператором.",
        parse_mode="HTML",
        reply_markup=main_kb()
    )

    # Уведомление админу
    notif = (
        f"💸 <b>ВЫВОД РЕФЕРАЛЬНОГО ЗАРАБОТКА</b>\n\n"
        f"👤 <a href='tg://user?id={uid}'>{update.effective_user.full_name}</a>\n"
        f"🆔 Chat ID: <code>{uid}</code>\n"
        f"🎰 Букмекер: {casino}\n"
        f"🎫 ID счёта: <code>{acc_id}</code>\n"
        f"💰 Сумма: {amount:.2f} сом\n"
        f"📊 Баланс до вывода: {balance:.2f} сом"
    )
    if ADMIN_CHAT_ID:
        try:
            await ctx.bot.send_message(
                ADMIN_CHAT_ID, notif,
                reply_markup=admin_ikb(uid),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(e)

    return ConversationHandler.END

async def refwd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Операция отменена.", reply_markup=main_kb())
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════
#  ПОПОЛНЕНИЕ
# ════════════════════════════════════════════════════════════════════════════
async def dep_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in blocked_users:
        await update.message.reply_text("❌ Вы заблокированы.")
        return ConversationHandler.END
    if bot_paused and uid not in ADMIN_IDS:
        await update.message.reply_text(
            "⏸ <b>Бот на паузе</b>\n\n"
            "🔧 Ведутся технические работы.\n"
            "Пожалуйста, попробуйте позже.\n\n"
            "💬 Поддержка: @Aurapay_supportbot",
            parse_mode="HTML"
        )
        return ConversationHandler.END
    active = all_requests.get(uid)
    if active and active.get("status") == "pending":
        req_type = "пополнение" if active.get("type") == "deposit" else "вывод"
        casino   = active.get("casino", "—")
        elapsed  = int(time.time() - active.get("sent_at", time.time()))
        mins, secs = divmod(elapsed, 60)
        wait_str = f"{mins} мин {secs} сек" if mins else f"{secs} сек"
        await update.message.reply_text(
            f"⏳ <b>У вас уже есть активная заявка!</b>\n\n"
            f"📋 Тип: {'💰 Пополнение' if req_type == 'пополнение' else '💸 Вывод'}\n"
            f"🎰 Казино: {casino}\n"
            f"⏱ Ожидаете: {wait_str}\n\n"
            "Дождитесь ответа оператора перед новой заявкой.\n"
            "💬 Поддержка: @Aurapay_supportbot",
            parse_mode="HTML"
        )
        return ConversationHandler.END
    await cleanup_msgs(ctx, uid)
    ctx.user_data.clear()
    msg = await update.message.reply_text("🎰 Выберите букмекер:", reply_markup=casino_ikb("dep"))
    track(ctx, update.message.message_id, msg.message_id)
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
    msg = await update.message.reply_text(
        "🚀 Введите сумму пополнения:\n"
        "📌 Min: 100\n"
        "📌 Max: 100 000",
        reply_markup=amount_ikb()
    )
    track(ctx, msg.message_id)
    return DEP_AMOUNT

async def dep_amount_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    amount = q.data.split("amt_")[1]
    ctx.user_data["dep_amount"] = amount
    await safe_delete(q.message.bot, update.effective_user.id, q.message.message_id)
    lst = ctx.user_data.get("cleanup_ids", [])
    if q.message.message_id in lst:
        lst.remove(q.message.message_id)
    await _show_qr(update, ctx, amount, q.message)
    return DEP_BANK

async def dep_amount_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not txt.isdigit():
        bad = await update.message.reply_text("⚠️ Введите число.")
        await asyncio.sleep(2)
        await safe_delete(bad.bot, update.effective_user.id, bad.message_id)
        return DEP_AMOUNT
    amount = int(txt)
    if amount < 100:
        bad = await update.message.reply_text(
            "⚠️ <b>Минимальная сумма: 100 сом</b>\n\nВведите сумму ещё раз:",
            parse_mode="HTML"
        )
        await asyncio.sleep(3)
        await safe_delete(bad.bot, update.effective_user.id, bad.message_id)
        return DEP_AMOUNT
    if amount > 100_000:
        bad = await update.message.reply_text(
            "⚠️ <b>Максимальная сумма: 100 000 сом</b>\n\nВведите сумму ещё раз:",
            parse_mode="HTML"
        )
        await asyncio.sleep(3)
        await safe_delete(bad.bot, update.effective_user.id, bad.message_id)
        return DEP_AMOUNT
    ctx.user_data["dep_amount"] = str(amount)
    await _show_qr(update, ctx, str(amount), update.message)
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
        "📌 Проверьте ваш ID ещё раз\n\n"
        "🏦 Выберите банк для оплаты:"
    )

    if qr_mode and pending_qr:
        qr_msg = await msg_obj.reply_photo(
            photo=pending_qr,
            caption=caption,
            parse_mode="HTML",
            reply_markup=dep_bank_payment_ikb()
        )
    else:
        qr_msg = await msg_obj.reply_text(
            caption + "\n\n⚠️ QR не настроен. Используйте /qr чтобы загрузить QR-код.",
            parse_mode="HTML",
            reply_markup=dep_bank_payment_ikb()
        )
    ctx.user_data["qr_msg_id"] = qr_msg.message_id
    track(ctx, qr_msg.message_id)

async def dep_bank_alert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("⛔ Данный банк недоступен!", show_alert=True)
    return DEP_BANK

async def dep_bank_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    hint = await update.message.reply_text("📷 Оплатите по QR и отправьте фото чека об оплате.")
    await asyncio.sleep(4)
    await safe_delete(hint.bot, update.effective_user.id, hint.message_id)
    return DEP_BANK

async def dep_cancel_inline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("❌ Операция отменена")
    uid = update.effective_user.id
    await safe_delete(q.message.bot, uid, q.message.message_id)
    await cleanup_msgs(ctx, uid)
    await q.message.reply_text("❌ Операция отменена.", reply_markup=main_kb())
    return ConversationHandler.END

async def wd_cancel_inline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("❌ Операция отменена")
    uid = update.effective_user.id
    await safe_delete(q.message.bot, uid, q.message.message_id)
    await cleanup_msgs(ctx, uid)
    await q.message.reply_text("❌ Операция отменена.", reply_markup=main_kb())
    return ConversationHandler.END

# ── Чек получен ───────────────────────────────────────────────────────────
async def dep_receipt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("📷 Отправьте фото чека об оплате.")
        return DEP_BANK

    receipt_fid = update.message.photo[-1].file_id
    uid         = update.effective_user.id
    casino      = ctx.user_data.get("dep_casino", "")
    casino_id   = ctx.user_data.get("dep_id", "")
    amount      = ctx.user_data.get("dep_amount", "")

    await cleanup_msgs(ctx, uid)

    pending_requests[uid] = {
        "type":     "deposit",
        "casino":   casino,
        "amount":   amount,
        "bank":     "—",
        "sent_at":  time.time(),
    }
    all_requests[uid] = {
        **pending_requests[uid],
        "username": update.effective_user.full_name,
        "status":   "pending",
    }

    await update.message.reply_text(
        "✅ <b>Заявка на пополнение отправлена!</b>\n\n"
        "⏳ Ожидайте — оператор обработает её в ближайшее время.\n"
        "💬 Поддержка: @Aurapay_supportbot",
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
    uid = update.effective_user.id
    if uid in blocked_users:
        await update.message.reply_text("❌ Вы заблокированы.")
        return ConversationHandler.END
    if bot_paused and uid not in ADMIN_IDS:
        await update.message.reply_text(
            "⏸ <b>Бот на паузе</b>\n\n"
            "🔧 Ведутся технические работы.\n"
            "Пожалуйста, попробуйте позже.\n\n"
            "💬 Поддержка: @Aurapay_supportbot",
            parse_mode="HTML"
        )
        return ConversationHandler.END
    active = all_requests.get(uid)
    if active and active.get("status") == "pending":
        req_type = "пополнение" if active.get("type") == "deposit" else "вывод"
        casino   = active.get("casino", "—")
        elapsed  = int(time.time() - active.get("sent_at", time.time()))
        mins, secs = divmod(elapsed, 60)
        wait_str = f"{mins} мин {secs} сек" if mins else f"{secs} сек"
        await update.message.reply_text(
            f"⏳ <b>У вас уже есть активная заявка!</b>\n\n"
            f"📋 Тип: {'💰 Пополнение' if req_type == 'пополнение' else '💸 Вывод'}\n"
            f"🎰 Казино: {casino}\n"
            f"⏱ Ожидаете: {wait_str}\n\n"
            "Дождитесь ответа оператора перед новой заявкой.\n"
            "💬 Поддержка: @Aurapay_supportbot",
            parse_mode="HTML"
        )
        return ConversationHandler.END
    await cleanup_msgs(ctx, uid)
    ctx.user_data.clear()
    msg = await update.message.reply_text("🎰 Выберите букмекер:", reply_markup=casino_ikb("wd"))
    track(ctx, update.message.message_id, msg.message_id)
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
    msg = await update.message.reply_text("📷 Отправьте фото QR кода от банка:")
    track(ctx, msg.message_id)
    return WD_QR

async def wd_qr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        bad = await update.message.reply_text("📷 Пожалуйста, отправьте именно фото QR кода.")
        await asyncio.sleep(3)
        await safe_delete(bad.bot, update.effective_user.id, bad.message_id)
        return WD_QR
    ctx.user_data["wd_qr"] = update.message.photo[-1].file_id
    casino = ctx.user_data.get("wd_casino", "")
    msg = await update.message.reply_text(
        "▶▶▶ Заходим 👇\n"
        "1. Настройки!\n2. Вывести со счёта!\n3. Касса\n"
        "4. Сумму для Вывода!\nг. Бишкек, офис AuraPay\n"
        f"5. Подтвердить\n6. Получить Код!\n7. Отправить его нам\n\n"
        f"🆔 Введите ID вашего счёта {casino}:"
    )
    track(ctx, msg.message_id)
    return WD_CID

async def wd_cid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["wd_cid"] = update.message.text
    msg = await update.message.reply_text("🔑 Введите код с сайта казино:")
    track(ctx, msg.message_id)
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

    await cleanup_msgs(ctx, uid)

    pending_requests[uid] = {
        "type":    "withdraw",
        "casino":  casino,
        "amount":  "—",
        "bank":    bank,
        "sent_at": time.time(),
    }
    all_requests[uid] = {
        **pending_requests[uid],
        "username": update.effective_user.full_name,
        "status":   "pending",
    }

    await update.message.reply_text(
        "✅ <b>Заявка на вывод отправлена!</b>\n\n"
        "⏳ Ожидайте — оператор обработает её в ближайшее время.\n"
        "💬 Поддержка: @Aurapay_supportbot",
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
#  Отмена (кнопка ❌ на клавиатуре)
# ════════════════════════════════════════════════════════════════════════════
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await cleanup_msgs(ctx, uid)
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

        req = pending_requests.get(uid, {})
        sent_at = req.get("sent_at", time.time())
        elapsed = max(1, int(time.time() - sent_at))

        try:
            msg = build_status_msg(uid, status, elapsed)
            await ctx.bot.send_message(uid, msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить статус пользователю {uid}: {e}")

        if uid in all_requests:
            all_requests[uid]["status"] = status
        if uid in pending_requests:
            del pending_requests[uid]

        # ── Начислить реферальный % только при ОДОБРЕНИИ пополнения ──────
        if is_approve and req.get("type") == "deposit":
            try:
                amount_int = int(req.get("amount", 0))
            except:
                amount_int = 0
            if amount_int > 0:
                ref_info = referral_data.get(uid, {})
                referrer_id = ref_info.get("referrer")
                if referrer_id and referrer_id in referral_data:
                    rdata = referral_data[referrer_id]
                    old_total = rdata.get("total_dep", 0.0)
                    new_total = old_total + amount_int
                    rdata["total_dep"] = new_total
                    old_pct = get_ref_percent(old_total)
                    new_pct = get_ref_percent(new_total)
                    bonus   = amount_int * new_pct / 100
                    rdata["earned"] = rdata.get("earned", 0.0) + bonus
                    try:
                        tier_msg = ""
                        if new_pct > old_pct:
                            tier_msg = f"\n\n🎉 <b>Ваш реферальный % вырос до {new_pct}%!</b>"
                        await ctx.bot.send_message(
                            referrer_id,
                            f"💸 <b>Ваш реферал пополнил счёт!</b>\n\n"
                            f"💰 Сумма пополнения: <b>{amount_int} сом</b>\n"
                            f"📊 Ваш %: <b>{new_pct}%</b>\n"
                            f"✅ Начислено вам: <b>+{bonus:.2f} сом</b>\n"
                            f"💼 Итого баланс: <b>{rdata['earned']:.2f} сом</b>"
                            f"{tier_msg}",
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logger.error(f"Не удалось уведомить реферера {referrer_id}: {e}")

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
            DEP_CASINO: [
                CallbackQueryHandler(dep_cancel_inline, pattern="^dep_cancel$"),
                CallbackQueryHandler(dep_casino, pattern="^dep_"),
            ],
            DEP_ID:     [MessageHandler(filters.TEXT & ~filters.COMMAND, dep_id)],
            DEP_AMOUNT: [
                CallbackQueryHandler(dep_amount_cb, pattern="^amt_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, dep_amount_text),
            ],
            DEP_BANK: [
                CallbackQueryHandler(dep_cancel_inline, pattern="^dep_cancel$"),
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
            WD_CASINO: [
                CallbackQueryHandler(wd_cancel_inline, pattern="^wd_cancel$"),
                CallbackQueryHandler(wd_casino, pattern="^wd_"),
            ],
            WD_BANK: [
                CallbackQueryHandler(wd_cancel_inline, pattern="^wd_cancel$"),
                CallbackQueryHandler(wd_bank, pattern="^wdbank_"),
            ],
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

    # Вывод реферальных — ConversationHandler
    refwd_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_ref_menu, pattern="^ref_withdraw$")],
        states={
            REF_WD_CASINO: [CallbackQueryHandler(refwd_casino, pattern="^refwd_")],
            REF_WD_ID:     [MessageHandler(filters.TEXT & ~filters.COMMAND, refwd_id)],
            REF_WD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, refwd_amount)],
        },
        fallbacks=[MessageHandler(filters.Regex("^❌"), refwd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("cob",       cmd_cob))
    app.add_handler(CommandHandler("commands",  cmd_commands))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("zayavki",   cmd_zayavki))
    app.add_handler(CommandHandler("refstats",  cmd_refstats))
    app.add_handler(qr_conv)
    app.add_handler(dep_conv)
    app.add_handler(wd_conv)
    app.add_handler(refwd_conv)
    app.add_handler(MessageHandler(filters.Regex("^🤝 Реферальная$"), cmd_referral))
    app.add_handler(MessageHandler(filters.Regex("^📖 Инструкция$"), cmd_instruction))
    app.add_handler(MessageHandler(filters.Regex("^🌐 Язык$"),       cmd_language))
    app.add_handler(MessageHandler(filters.Regex("^❌"),              cancel))
    app.add_handler(CallbackQueryHandler(cb_lang,      pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(cb_bot_pause, pattern="^bot_(pause|resume)$"))
    app.add_handler(CallbackQueryHandler(cb_ref_menu,  pattern="^ref_(share|top10|back)$"))
    app.add_handler(CallbackQueryHandler(cb_admin,     pattern="^(approve|decline|ablock|aunblock|awrite|noop)"))

    logger.info("✅ Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
