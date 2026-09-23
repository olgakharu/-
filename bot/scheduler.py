"""Фоновый цикл: письма воронки, напоминания о подписке, закрытие доступа, ежедневные напоминания.

Всё расписание хранится в базе, поэтому перезапуск бота ничего не теряет:
пропущенные за время простоя сообщения уйдут при следующей проверке.
"""
import asyncio
import logging
import random
from datetime import datetime

from core import DAY, Services
from db import now

log = logging.getLogger(__name__)
TICK_SECONDS = 30


async def run_funnels(svc: Services):
    for user in await svc.db.due_funnel():
        steps = svc.content.get(user["funnel"]) or []
        step = user["funnel_step"]
        # подписчикам прогрев больше не нужен
        if user["sub_active"] or step >= len(steps):
            await svc.db.stop_funnel(user["id"])
            continue
        await svc.send_block(user, steps[step])
        nxt = step + 1
        if nxt < len(steps):
            await svc.db.set_funnel(
                user["id"], user["funnel"], nxt, now() + int(steps[nxt]["delay_hours"] * 3600)
            )
        else:
            await svc.db.stop_funnel(user["id"])


async def run_subscriptions(svc: Services):
    texts = svc.content["reminders"]
    grace = svc.cfg.grace_hours * 3600
    for user in await svc.db.active_subs():
        left = user["sub_until"] - now()
        sent = set(filter(None, user["reminders_sent"].split(",")))

        # автопродление Stars может прийти с небольшой задержкой — даём запас
        if left <= -(grace if user["autorenew"] else 0):
            await svc.db.deactivate(user["id"])
            await svc.remove_from_channel(user["id"])
            await svc.send(user["id"], svc.render(texts["expired"], user),
                           [{"text": "💛 Вернуться в клуб", "action": "subscribe"}])
            await svc.start_funnel(user["id"], "winback")
            continue

        if left <= 0:
            continue
        if user["autorenew"]:
            if left <= DAY and "1d" not in sent:
                await svc.send(user["id"], svc.render(texts["autorenew_1d"], user))
                await svc.db.mark_reminder(user["id"], user["reminders_sent"], "1d")
        elif left <= DAY and "1d" not in sent:
            await svc.send(user["id"], svc.render(texts["before_1d"], user),
                           [{"text": "Продлить", "action": "subscribe"}])
            await svc.db.mark_reminder(user["id"], user["reminders_sent"], "1d")
        elif DAY < left <= 3 * DAY and "3d" not in sent:
            await svc.send(user["id"], svc.render(texts["before_3d"], user),
                           [{"text": "Продлить", "action": "subscribe"}])
            await svc.db.mark_reminder(user["id"], user["reminders_sent"], "3d")


async def run_daily(svc: Services):
    local = datetime.now(svc.cfg.tz)
    today, hhmm = local.strftime("%Y-%m-%d"), local.strftime("%H:%M")
    phrases = svc.content.get("daily") or ["Время заглянуть в себя 🌿"]
    for user in await svc.db.daily_candidates(hhmm, today):
        await svc.send(user["id"], svc.render(random.choice(phrases), user),
                       [{"text": "🧭 Барометр состояния", "action": "miniapp"}])
        await svc.db.mark_daily(user["id"], today)


async def scheduler_loop(svc: Services):
    log.info("Планировщик запущен")
    while True:
        for job in (run_funnels, run_subscriptions, run_daily):
            try:
                await job(svc)
            except Exception:  # noqa: BLE001 — сбой одной задачи не должен останавливать остальные
                log.exception("Ошибка в %s", job.__name__)
        await asyncio.sleep(TICK_SECONDS)
