"""Команды и кнопки пользователя, оплата, админка."""
import asyncio
import logging
import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMemberUpdated,
    Message,
    PreCheckoutQuery,
)

from core import DAY, Services
from db import now

log = logging.getLogger(__name__)
router = Router()

DAILY_PRESETS = ["08:00", "09:00", "12:00", "19:00", "21:00", "22:00"]
TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")


async def ensure_user(svc: Services, tg_user, source: str | None = None):
    """Регистрирует пользователя (и ставит в воронку, если он новый)."""
    created = await svc.db.add_user(
        tg_user.id, tg_user.username, tg_user.first_name, source, svc.first_delay("funnel")
    )
    return await svc.db.get_user(tg_user.id), created


# ── старт и меню ──────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, svc: Services):
    source = (command.args or "").strip()[:64] or None
    user, _ = await ensure_user(svc, message.from_user, source)
    await svc.send_block(user, svc.content["welcome"])
    if source == "pay":  # ссылка t.me/<bot>?start=pay сразу открывает оплату
        await svc.send_offer(user)


@router.message(Command("help"))
async def cmd_help(message: Message, svc: Services):
    await message.answer(svc.content["help"])


@router.message(Command("terms"))
async def cmd_terms(message: Message, svc: Services):
    await message.answer(svc.content["terms"])


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message, svc: Services):
    await message.answer(svc.content["paysupport"])


# ── подписка ──────────────────────────────────────────────
@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message, svc: Services):
    user, _ = await ensure_user(svc, message.from_user)
    await svc.send_offer(user)


@router.callback_query(F.data == "subscribe")
async def cb_subscribe(call: CallbackQuery, svc: Services):
    await call.answer()
    user, _ = await ensure_user(svc, call.from_user)
    await svc.send_offer(user)


def status_view(svc: Services, user):
    c = svc.content
    if not user["sub_active"]:
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💛 Оформить подписку", callback_data="subscribe")
        ]])
        return c["status_inactive"], markup

    rows = []
    if svc.stars and user["last_charge_id"]:
        renew_state = svc.render(c["renew_on"] if user["autorenew"] else c["renew_off"], user)
        rows.append([InlineKeyboardButton(
            text="Отключить автопродление" if user["autorenew"] else "Включить автопродление",
            callback_data="renew:off" if user["autorenew"] else "renew:on",
        )])
    else:
        renew_state = ""
        rows.append([InlineKeyboardButton(text="Продлить заранее", callback_data="subscribe")])
    text = svc.render(c["status_active"], user, renew_state=renew_state)
    return text.strip(), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("status"))
async def cmd_status(message: Message, svc: Services):
    user, _ = await ensure_user(svc, message.from_user)
    text, markup = status_view(svc, user)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "status")
async def cb_status(call: CallbackQuery, svc: Services):
    await call.answer()
    user, _ = await ensure_user(svc, call.from_user)
    text, markup = status_view(svc, user)
    await call.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("renew:"))
async def cb_renew(call: CallbackQuery, bot: Bot, svc: Services):
    user = await svc.db.get_user(call.from_user.id)
    if not user or not user["sub_active"] or not user["last_charge_id"]:
        await call.answer("Активной подписки нет", show_alert=True)
        return
    enable = call.data == "renew:on"
    try:
        await bot.edit_user_star_subscription(
            user_id=user["id"], telegram_payment_charge_id=user["last_charge_id"],
            is_canceled=not enable,
        )
    except TelegramBadRequest as e:
        log.warning("edit_user_star_subscription: %s", e)
        await call.answer("Не получилось. Напиши в /paysupport", show_alert=True)
        return
    await svc.db.set_autorenew(user["id"], enable)
    user = await svc.db.get_user(user["id"])
    text, markup = status_view(svc, user)
    await call.answer("Готово")
    await call.message.edit_text(text, reply_markup=markup)


# ── оплата ────────────────────────────────────────────────
@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    if query.invoice_payload == f"sub:{query.from_user.id}":
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Счёт устарел. Нажми «Подписка» ещё раз.")


@router.message(F.successful_payment)
async def on_paid(message: Message, svc: Services):
    sp = message.successful_payment
    user, _ = await ensure_user(svc, message.from_user)
    fresh = await svc.db.add_payment(
        user["id"], sp.total_amount, sp.currency, sp.telegram_payment_charge_id,
        sp.provider_payment_charge_id, bool(sp.is_recurring),
    )
    if not fresh:
        return

    if sp.subscription_expiration_date:  # подписка Stars: срок знает Telegram
        until, autorenew = int(sp.subscription_expiration_date), True
    else:  # разовая оплата периода: дни складываются с остатком
        base = max(now(), user["sub_until"] or 0) if user["sub_active"] else now()
        until, autorenew = base + svc.period_days * DAY, False

    await svc.grant(
        user["id"], until=until, charge_id=sp.telegram_payment_charge_id, autorenew=autorenew,
        renewal=bool(sp.is_recurring and not sp.is_first_recurring),
    )
    for admin in svc.cfg.admin_ids:
        await svc.send(admin, f"💰 Оплата: {sp.total_amount} {sp.currency} от "
                              f"{message.from_user.full_name} (id <code>{user['id']}</code>)\n"
                              f"charge_id: <code>{sp.telegram_payment_charge_id}</code>")


# ── ежедневные напоминания ────────────────────────────────
def remind_markup():
    rows = [
        [InlineKeyboardButton(text=t, callback_data=f"daily:{t}") for t in DAILY_PRESETS[i:i + 3]]
        for i in range(0, len(DAILY_PRESETS), 3)
    ]
    rows.append([InlineKeyboardButton(text="Выключить", callback_data="daily:off")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def apply_daily(svc: Services, user_id: int, hhmm: str | None) -> str:
    if hhmm is None:
        await svc.db.set_daily(user_id, None)
        return svc.content["remind_off"]
    local = datetime.now(svc.cfg.tz)
    # если это время сегодня уже прошло — первое напоминание придёт завтра
    last = local.strftime("%Y-%m-%d") if hhmm <= local.strftime("%H:%M") else None
    await svc.db.set_daily(user_id, hhmm, last)
    return svc.render(svc.content["remind_set"], time=hhmm)


@router.message(Command("remind"))
async def cmd_remind(message: Message, command: CommandObject, svc: Services):
    await ensure_user(svc, message.from_user)
    arg = (command.args or "").strip().lower()
    if arg in ("off", "выкл", "стоп"):
        await message.answer(await apply_daily(svc, message.from_user.id, None))
        return
    m = TIME_RE.match(arg)
    if m:
        hhmm = f"{int(m.group(1)):02d}:{m.group(2)}"
        await message.answer(await apply_daily(svc, message.from_user.id, hhmm))
        return
    await message.answer(svc.render(svc.content["remind_prompt"]), reply_markup=remind_markup())


@router.callback_query(F.data == "remind")
async def cb_remind(call: CallbackQuery, svc: Services):
    await call.answer()
    await ensure_user(svc, call.from_user)
    await call.message.answer(svc.render(svc.content["remind_prompt"]),
                              reply_markup=remind_markup())


@router.callback_query(F.data.startswith("daily:"))
async def cb_daily(call: CallbackQuery, svc: Services):
    value = call.data.split(":", 1)[1]
    text = await apply_daily(svc, call.from_user.id, None if value == "off" else value)
    await call.answer()
    await call.message.edit_text(text)


# ── админка ───────────────────────────────────────────────
admin = Router()


@admin.message(Command("stats"))
async def cmd_stats(message: Message, svc: Services):
    s = await svc.db.stats()
    money = ", ".join(f"{r['s']} {r['currency']} ({r['n']} опл.)" for r in s["revenue_30d"]) or "—"
    names = {"funnel": "Прогрев", "winback": "Возврат"}
    funnel = "\n".join(
        f"  {names.get(r['funnel'], r['funnel'])}, ждут письмо {r['funnel_step'] + 1}: {r['n']}"
        for r in s["funnel"]
    ) or "  —"
    sources = "\n".join(
        f"  {r['src']}: {r['n']} чел., оплатили {r['paid'] or 0}" for r in s["sources"]
    ) or "  —"
    conv = f"{s['ever_paid'] / s['users'] * 100:.1f}%" if s["users"] else "—"
    await message.answer(
        "<b>📊 Статистика</b>\n\n"
        f"Всего в боте: {s['users']} (заблокировали: {s['blocked']})\n"
        f"Новых за 30 дней: {s['new_30d']}\n"
        f"Активных подписок: <b>{s['active']}</b>\n"
        f"Платили хоть раз: {s['ever_paid']} (конверсия {conv})\n"
        f"Выручка за 30 дней: {money}\n\n"
        f"<b>Воронка сейчас:</b>\n{funnel}\n\n"
        f"<b>Источники (?start=метка):</b>\n{sources}"
    )


@admin.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject, bot: Bot, svc: Services):
    segment = (command.args or "").strip() or "all"
    if segment not in ("all", "subs", "free") or not message.reply_to_message:
        await message.answer(
            "Ответь командой на сообщение, которое нужно разослать:\n"
            "<code>/broadcast all</code> — всем\n"
            "<code>/broadcast subs</code> — подписчикам\n"
            "<code>/broadcast free</code> — без подписки"
        )
        return
    ids = [r["id"] for r in await svc.db.audience(segment)]
    await message.answer(f"Рассылка на {len(ids)} чел. началась…")

    async def run():
        ok = 0
        for uid in ids:
            try:
                await bot.copy_message(uid, message.chat.id, message.reply_to_message.message_id)
                ok += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramForbiddenError:
                await svc.db.set_blocked(uid)
            except TelegramBadRequest:
                pass
            await asyncio.sleep(0.05)  # лимит Telegram ~30 сообщений/сек
        await message.answer(f"✅ Рассылка завершена: доставлено {ok} из {len(ids)}")

    asyncio.create_task(run())


@admin.message(Command("give"))
async def cmd_give(message: Message, command: CommandObject, svc: Services):
    """Выдать доступ вручную (например, оплатили переводом): /give <id> <дней>"""
    try:
        uid, days = (int(x) for x in (command.args or "").split())
    except ValueError:
        await message.answer("Формат: <code>/give 123456789 30</code>")
        return
    user = await svc.db.get_user(uid)
    if not user:
        await message.answer("Этот человек ещё не запускал бота.")
        return
    base = max(now(), user["sub_until"] or 0) if user["sub_active"] else now()
    await svc.grant(uid, until=base + days * DAY, charge_id=None, autorenew=False, renewal=False)
    user = await svc.db.get_user(uid)
    await message.answer(f"Готово: доступ до {svc.fmt_date(user['sub_until'])}")


@admin.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject, bot: Bot, svc: Services):
    """Вернуть Stars: /refund <id> <charge_id> (charge_id — в уведомлении об оплате)"""
    try:
        uid, charge_id = (command.args or "").split()
        await bot.refund_star_payment(int(uid), charge_id)
    except ValueError:
        await message.answer("Формат: <code>/refund 123456789 charge_id</code>")
        return
    except TelegramBadRequest as e:
        await message.answer(f"Не получилось: {e.message}")
        return
    await svc.db.deactivate(int(uid))
    await svc.remove_from_channel(int(uid))
    await message.answer("Звёзды возвращены, доступ закрыт.")


# ── подключение «Точки сборки» ────────────────────────────
def rights_text(can_invite: bool, can_ban: bool) -> str:
    mark = lambda ok: "✅" if ok else "❌"  # noqa: E731
    text = (f"{mark(can_invite)} приглашать по ссылкам\n"
            f"{mark(can_ban)} удалять участников")
    if not (can_invite and can_ban):
        text += ("\n\n⚠️ Выдай боту недостающие права в настройках администраторов чата, "
                 "иначе он не сможет пускать или убирать людей.")
    return text


async def connect_chat(svc: Services, chat, reply_to: int):
    await svc.set_channel(chat.id)
    try:
        can_invite, can_ban = await svc.channel_rights(chat.id)
    except TelegramBadRequest:
        can_invite = can_ban = False
    await svc.send(reply_to, f"🔗 Подключено: <b>{chat.title}</b> (<code>{chat.id}</code>)\n\n"
                             + rights_text(can_invite, can_ban))


@router.message(Command("connect"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_connect_group(message: Message, bot: Bot, svc: Services):
    if message.from_user.id not in svc.cfg.admin_ids:
        return
    await connect_chat(svc, message.chat, message.from_user.id)
    try:
        await message.delete()  # убираем служебную команду из чата
    except TelegramBadRequest:
        pass


@router.channel_post(Command("connect"))
async def cmd_connect_channel(message: Message, svc: Services):
    # в канале писать могут только его администраторы — этого достаточно
    for admin_id in svc.cfg.admin_ids:
        await connect_chat(svc, message.chat, admin_id)
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


@router.my_chat_member()
async def on_added(event: ChatMemberUpdated, svc: Services):
    """Бота сделали администратором где-то — подсказываем, как подключить."""
    if event.chat.type == "private" or event.new_chat_member.status != "administrator":
        return
    if event.chat.id == svc.channel_id:
        return
    for admin_id in svc.cfg.admin_ids:
        await svc.send(admin_id,
                       f"Меня добавили администратором в <b>{event.chat.title}</b>.\n\n"
                       "Чтобы подписчики попадали именно сюда, напиши в этом чате команду "
                       "<code>/connect</code>")


@admin.message(Command("channel"))
async def cmd_channel(message: Message, svc: Services):
    if not svc.channel_id:
        await message.answer("Чат для подписчиков не подключён. Напиши <code>/connect</code> "
                             "в «Точке сборки» (бот должен быть там администратором).")
        return
    try:
        chat = await message.bot.get_chat(svc.channel_id)
        can_invite, can_ban = await svc.channel_rights(svc.channel_id)
        await message.answer(f"Подключено: <b>{chat.title}</b> (<code>{chat.id}</code>)\n\n"
                             + rights_text(can_invite, can_ban))
    except TelegramBadRequest as e:
        await message.answer(f"Чат <code>{svc.channel_id}</code> недоступен: {e.message}\n"
                             "Проверь, что бот всё ещё администратор.")
