"""Общая логика: тексты, кнопки, отправка, оплата, доступ в канал."""
import html
import json
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    WebAppInfo,
)

from config import STARS_PERIOD_SECONDS, Config
from db import DB, now

log = logging.getLogger(__name__)
DAY = 24 * 3600


class _Keep(dict):
    """Оставляет неизвестные {метки} как есть, чтобы опечатка в тексте не роняла бота."""

    def __missing__(self, key):
        return "{" + key + "}"


class Services:
    def __init__(self, bot: Bot, db: DB, cfg: Config, content: dict):
        self.bot, self.db, self.cfg, self.content = bot, db, cfg, content

    # ── оформление ────────────────────────────────────────
    @property
    def stars(self) -> bool:
        return self.cfg.payment_mode == "stars"

    @property
    def period_days(self) -> int:
        return 30 if self.stars else int(self.content["product"]["days"])

    def price_label(self) -> str:
        p = self.content["product"]
        return f"{p['price_stars']} ⭐" if self.stars else f"{p['price_rub']} ₽"

    def fmt_date(self, ts: int | None) -> str:
        if not ts:
            return "—"
        return datetime.fromtimestamp(ts, self.cfg.tz).strftime("%d.%m.%Y")

    def render(self, text: str, user=None, **extra) -> str:
        p = self.content["product"]
        values = _Keep(
            name=html.escape((user["first_name"] if user else None) or "друг"),
            date=self.fmt_date(user["sub_until"] if user else None),
            price=self.price_label(),
            title=p["title"],
            description=p["description"],
            tz=self.cfg.tz.key,
        )
        values.update(extra)
        return text.format_map(values)

    def keyboard(self, buttons: list[dict] | None) -> InlineKeyboardMarkup | None:
        rows = []
        for b in buttons or []:
            action = b.get("action")
            if b.get("url"):
                btn = InlineKeyboardButton(text=b["text"], url=b["url"])
            elif action == "miniapp":
                if not self.cfg.miniapp_url:
                    continue  # мини-приложение не подключено — кнопку просто не показываем
                btn = InlineKeyboardButton(
                    text=b["text"], web_app=WebAppInfo(url=self.cfg.miniapp_url)
                )
            elif action in ("subscribe", "status", "remind"):
                btn = InlineKeyboardButton(text=b["text"], callback_data=action)
            else:
                log.warning("Неизвестная кнопка в content.json: %s", b)
                continue
            rows.append([btn])
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    # ── отправка ──────────────────────────────────────────
    async def send(self, user_id: int, text: str, buttons=None, markup=None) -> bool:
        try:
            await self.bot.send_message(
                user_id, text, reply_markup=markup or self.keyboard(buttons),
                disable_web_page_preview=True,
            )
            return True
        except TelegramForbiddenError:
            await self.db.set_blocked(user_id)
        except TelegramBadRequest as e:
            log.warning("Не удалось отправить %s: %r", user_id, e.message)
            if markup or buttons:
                # сломанная кнопка не должна съедать всё сообщение — шлём текст без кнопок
                return await self.send(user_id, text)
        return False

    async def send_block(self, user, block: dict, **extra) -> bool:
        return await self.send(
            user["id"], self.render(block["text"], user, **extra), block.get("buttons")
        )

    # ── оплата ────────────────────────────────────────────
    async def send_offer(self, user):
        p, o = self.content["product"], self.content["offer"]
        note = o["renew_note_stars"] if self.stars else o["renew_note_provider"]
        text = self.render(o["text"], user, renew_note=note)
        payload = f"sub:{user['id']}"
        title, description = p["title"][:32], p["description"][:255]

        if self.stars:
            link = await self.bot.create_invoice_link(
                title=title, description=description, payload=payload, currency="XTR",
                prices=[LabeledPrice(label=title, amount=int(p["price_stars"]))],
                subscription_period=STARS_PERIOD_SECONDS,
            )
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"Оплатить {self.price_label()}", url=link)
            ]])
            await self.send(user["id"], text, markup=markup)
            return

        await self.send(user["id"], text)
        amount = int(p["price_rub"]) * 100  # в копейках
        extra = {}
        if self.cfg.yookassa_receipt:
            # Данные для чека 54-ФЗ в формате ЮKassa. Сверь vat_code со своей налоговой схемой.
            extra = dict(
                need_email=True,
                send_email_to_provider=True,
                provider_data=json.dumps({"receipt": {"items": [{
                    "description": title,
                    "quantity": "1.00",
                    "amount": {"value": f"{amount / 100:.2f}", "currency": "RUB"},
                    "vat_code": 1,
                }]}}, ensure_ascii=False),
            )
        await self.bot.send_invoice(
            user["id"], title=title, description=description, payload=payload,
            provider_token=self.cfg.provider_token, currency="RUB",
            prices=[LabeledPrice(label=title, amount=amount)], **extra,
        )

    async def grant(self, user_id: int, *, until: int, charge_id: str | None, autorenew: bool,
                    renewal: bool):
        """Открывает/продлевает доступ и сообщает об этом пользователю."""
        user = await self.db.get_user(user_id)
        was_active = bool(user["sub_active"])
        # Для отмены автопродления Stars нужен id первого платежа подписки — его и храним
        keep_charge = user["last_charge_id"] if renewal and user["last_charge_id"] else charge_id
        await self.db.activate(user_id, until, keep_charge, autorenew)
        user = await self.db.get_user(user_id)

        if renewal or was_active:
            await self.send(user_id, self.render(self.content["renewed"], user))
            return
        await self.send(user_id, self.render(self.content["paid"], user))
        await self.send_invite(user)

    # ── доступ в канал ────────────────────────────────────
    async def send_invite(self, user):
        if not self.cfg.channel_id:
            return
        try:
            invite = await self.bot.create_chat_invite_link(
                self.cfg.channel_id, name=f"sub {user['id']}",
                expire_date=now() + DAY, member_limit=1,
            )
        except TelegramBadRequest as e:
            log.error("Не удалось создать ссылку в канал (бот админ?): %s", e)
            return
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=self.render(self.content["invite_button"]),
                                 url=invite.invite_link)
        ]])
        await self.send(user["id"], self.render(self.content["invite"], user), markup=markup)

    async def remove_from_channel(self, user_id: int):
        if not self.cfg.channel_id or user_id in self.cfg.admin_ids:
            return
        try:
            member = await self.bot.get_chat_member(self.cfg.channel_id, user_id)
            if member.status in ("creator", "administrator", "left", "kicked"):
                return
            # бан + разбан = исключить, но оставить возможность вернуться после оплаты
            await self.bot.ban_chat_member(self.cfg.channel_id, user_id)
            await self.bot.unban_chat_member(self.cfg.channel_id, user_id, only_if_banned=True)
        except TelegramBadRequest as e:
            log.warning("Не удалось исключить %s из канала: %s", user_id, e)

    # ── воронка ───────────────────────────────────────────
    def first_delay(self, funnel: str) -> int | None:
        steps = self.content.get(funnel) or []
        return int(steps[0]["delay_hours"] * 3600) if steps else None

    async def start_funnel(self, user_id: int, funnel: str):
        delay = self.first_delay(funnel)
        if delay is None:
            await self.db.stop_funnel(user_id)
        else:
            await self.db.set_funnel(user_id, funnel, 0, now() + delay)
