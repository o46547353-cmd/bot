### monitor.py  —  FIXED
# BUG-18 FIX: get_archive(limit=20) -> limit=50 чтобы мониторить больше постов

import asyncio, logging, re
from datetime import datetime, timezone
import storage, threads_api

logger       = logging.getLogger(__name__)
PLUS_PATTERN = re.compile(r'^\s*\+\s*$')
_tg_app      = None
_admin_ids   = []


def set_telegram(app, admin_ids):
    global _tg_app, _admin_ids
    _tg_app    = app
    _admin_ids = admin_ids


# ─── Мониторинг "+" ──────────────────────────────────────────────────────────

async def check_all_comments():
    archive = storage.get_archive(50)   # BUG-18 FIX: было 20
    for item in archive:
        login    = item['account_login']
        post_ids = item.get('post_ids', [])
        if not post_ids:
            continue
        await check_post_comments(login, post_ids[0], item['topic'])


async def check_post_comments(account_login: str, post_id: str, topic: str = ''):
    if not post_id:
        return
    try:
        replies = await asyncio.to_thread(
            threads_api.get_thread_replies, post_id, account_login
        )
    except Exception as e:
        logger.warning(f"Реплаи {post_id}: {e}")
        return

    for reply in (replies or []):
        comment_id = str(reply.get('pk') or reply.get('id', ''))
        if not comment_id:
            continue
        if storage.is_comment_processed(comment_id):
            continue
        text = _get_text(reply)
        if not _is_plus(text):
            continue
        commenter = _get_username(reply)
        logger.info(f"[{account_login}] '+' от @{commenter}")
        await asyncio.to_thread(threads_api.like_thread, comment_id, account_login)
        storage.log_monitor_action(account_login, post_id, comment_id, commenter, 'liked')
        await _notify_admin(account_login, commenter, topic, post_id)
        storage.log_monitor_action(account_login, post_id, comment_id, commenter, 'notified')


async def _notify_admin(account_login: str, commenter: str, topic: str, post_id: str):
    if not _tg_app or not _admin_ids:
        return
    acc      = storage.get_account(account_login)
    username = acc.get('username', account_login) if acc else account_login
    text = (
        f"🔔 *Новый \"+\" в Threads*\n\n"
        f"Аккаунт: @{username}\n"
        f"От: @{commenter}\n"
        f"Тема: {topic}\n"
        f"Пост ID: `{post_id}`\n\n"
        f"💬 Напиши ему в Threads или отправь ссылку на бот!"
    )
    for admin_id in _admin_ids:
        try:
            await _tg_app.bot.send_message(admin_id, text, parse_mode='Markdown')
        except Exception as e:
            logger.warning(f"Уведомление: {e}")


def _is_plus(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    return t == '+' or PLUS_PATTERN.match(t) is not None


def _get_text(reply: dict) -> str:
    if isinstance(reply, dict):
        return (reply.get('caption', {}) or {}).get('text', '') or reply.get('text', '')
    return ''


def _get_username(reply: dict) -> str:
    if isinstance(reply, dict):
        return (reply.get('user', {}) or {}).get('username', 'unknown')
    return 'unknown'


# ─── Статистика постов ───────────────────────────────────────────────────────

async def check_post_stats():
    """Проверяем статистику постов через 3ч и 24ч после публикации (UTC-safe)."""
    archive = storage.get_archive(50)   # BUG-18 FIX: было 20
    now     = datetime.now(timezone.utc)

    for item in archive:
        login    = item['account_login']
        post_ids = item.get('post_ids', [])
        if not post_ids or not item.get('posted_at'):
            continue
        try:
            posted_dt = datetime.fromisoformat(item['posted_at'])
            if posted_dt.tzinfo is None:
                posted_dt = posted_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        hours = (now - posted_dt).total_seconds() / 3600

        for target_hours in [3, 24]:
            if not (target_hours - 0.5 <= hours <= target_hours + 0.5):
                continue
            pid   = post_ids[0]
            stats = await asyncio.to_thread(
                threads_api.get_thread_stats, pid, login
            )
            if not stats:
                continue
            storage.save_post_stat(
                login, pid, item['topic'],
                stats.get('likes', 0), stats.get('replies', 0),
                stats.get('reposts', 0), target_hours
            )
            await _send_stats_report(login, item['topic'], pid, stats, target_hours)


async def _send_stats_report(account_login: str, topic: str, post_id: str,
                              stats: dict, hours_after: int):
    if not _tg_app or not _admin_ids:
        return
    acc      = storage.get_account(account_login)
    username = acc.get('username', account_login) if acc else account_login
    emoji    = '📊' if hours_after == 3 else '📈'
    text = (
        f"{emoji} *Статистика поста ({hours_after}ч)*\n\n"
        f"Аккаунт: @{username}\n"
        f"Тема: {topic}\n\n"
        f"❤️ Лайки: {stats.get('likes', 0)}\n"
        f"💬 Ответы: {stats.get('replies', 0)}\n"
        f"🔁 Репосты: {stats.get('reposts', 0)}\n"
        f"Пост ID: `{post_id}`"
    )
    for admin_id in _admin_ids:
        try:
            await _tg_app.bot.send_message(admin_id, text, parse_mode='Markdown')
        except Exception as e:
            logger.warning(f"Статистика: {e}")
