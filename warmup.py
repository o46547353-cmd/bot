### warmup.py
import asyncio, random, logging
import storage, threads_api
from humanize import (random_session_actions, random_daily_limits, should_skip_today,
                      is_active_hour, human_sleep, pause_after, jitter)

logger = logging.getLogger(__name__)


async def run_warmup_session(account_login: str):
    """Одна сессия прогрева для аккаунта."""
    acc = storage.get_account(account_login)
    if not acc:
        return

    preset   = acc.get('warmup_preset', 'A')
    keywords = _keywords(acc)
    timezone = acc.get('timezone', 'Europe/Moscow')

    if not is_active_hour(timezone):
        logger.info(f"[{account_login}] Не активное время")
        return

    if should_skip_today():
        logger.info(f"[{account_login}] Выходной день — пропуск")
        return

    limits = random_daily_limits(preset)
    logger.info(f"[{account_login}] Прогрев. Лимиты: {limits}")

    try:
        stats = await asyncio.to_thread(_run_session_sync, account_login, limits, keywords)
        storage.log_warmup(account_login, stats)
        logger.info(f"[{account_login}] Прогрев: {stats}")
        return stats
    except Exception as e:
        logger.error(f"[{account_login}] Ошибка прогрева: {e}")


async def pre_post_warmup(account_login: str):
    """Короткий прогрев за 1 час до публикации."""
    acc = storage.get_account(account_login)
    if not acc or not is_active_hour(acc.get('timezone', 'Europe/Moscow')):
        return

    limits   = {'likes': random.randint(5, 12), 'follows': random.randint(2, 6),
                'reposts': random.randint(0, 2), 'scroll_min': random.randint(5, 15)}
    keywords = _keywords(acc)
    logger.info(f"[{account_login}] Пре-постинг прогрев")
    try:
        await asyncio.to_thread(_run_session_sync, account_login, limits, keywords)
    except Exception as e:
        logger.error(f"[{account_login}] Пре-постинг ошибка: {e}")


def _run_session_sync(account_login, limits, keywords):
    """Синхронная сессия прогрева."""
    stats   = {'likes': 0, 'follows': 0, 'reposts': 0, 'scrolls': 0}
    actions = random_session_actions(limits)

    for action in actions:
        t = action['type']
        try:
            if t == 'scroll':
                import time
                time.sleep(jitter(action.get('duration', 30)))
                stats['scrolls'] += 1

            elif t == 'like':
                kw = random.choice(keywords)
                users = threads_api.search_users(kw, account_login)
                if users:
                    user = random.choice(users[:5])
                    uid  = str(user.get('pk') or user.get('id', ''))
                    posts = threads_api.get_user_threads(uid, account_login)
                    if posts:
                        post = random.choice(posts[:5])
                        pid  = str(post.get('pk') or post.get('id', ''))
                        if threads_api.like_thread(pid, account_login):
                            stats['likes'] += 1

            elif t == 'follow':
                kw = random.choice(keywords)
                users = threads_api.search_users(kw, account_login)
                if users:
                    user = random.choice(users[:10])
                    uid  = str(user.get('pk') or user.get('id', ''))
                    if threads_api.follow_user(uid, account_login):
                        stats['follows'] += 1

            elif t == 'repost':
                kw = random.choice(keywords)
                users = threads_api.search_users(kw, account_login)
                if users:
                    user = random.choice(users[:5])
                    uid  = str(user.get('pk') or user.get('id', ''))
                    posts = threads_api.get_user_threads(uid, account_login)
                    if posts:
                        post = random.choice(posts[:3])
                        pid  = str(post.get('pk') or post.get('id', ''))
                        if threads_api.repost_thread(pid, account_login):
                            stats['reposts'] += 1

            elif t == 'view_profile':
                human_sleep(10, 40)

            pause_after(t)

        except Exception as e:
            logger.warning(f"Действие {t} ошибка: {e}")
            human_sleep(15, 45)

    return stats


def _keywords(acc):
    raw = acc.get('warmup_keywords', '')
    if not raw:
        return ['vpn', 'безопасность', 'интернет', 'privacy']
    return [k.strip() for k in raw.split(',') if k.strip()]
