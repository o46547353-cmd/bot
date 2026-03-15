### warmup.py — v3: GraphQL-powered warmup
"""
Прогрев аккаунта Threads.

Стратегия:
  1. Найти юзеров: search → recommended → seed (через threads_api)
  2. Получить их посты: GraphQL (не требует Bearer, обходит 403)
  3. Действия: like чужих постов, follow, repost, scroll
  4. Fallback: свои посты из архива

Все действия с рандомными паузами, разным порядком, пропуском дней.
"""

import asyncio, random, logging, time
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
        logger.info(f"[{account_login}] Прогрев завершён: {stats}")
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


# ══════════════════════════════════════════════════════════════════════════════
#  СБОР ПОСТОВ ДЛЯ ЛАЙКОВ
# ══════════════════════════════════════════════════════════════════════════════

def _collect_posts_pool(targets: list, account_login: str) -> list:
    """
    Собирает пул post_id для лайков из разных источников.
    GraphQL не требует Bearer — самый надёжный.
    """
    all_posts = []

    # 1. Посты таргетов через GraphQL (get_user_threads использует GraphQL первым)
    sampled = random.sample(targets, min(5, len(targets)))
    for user in sampled:
        uid = str(user.get('pk') or user.get('id', ''))
        if not uid:
            continue
        try:
            posts = threads_api.get_user_threads(uid, account_login)
            if posts:
                for p in posts[:5]:
                    pid = str(p.get('pk') or p.get('id', ''))
                    if pid:
                        all_posts.append({'pid': pid, 'uid': uid, 'source': 'graphql'})
                logger.debug(f"_collect_posts: @{user.get('username','?')} → {len(posts)} постов")
        except Exception:
            pass
        time.sleep(random.uniform(1, 3))

    # 2. Свои посты из архива (fallback)
    try:
        archive = storage.get_archive(20)
        for item in archive:
            for pid in (item.get('post_ids') or []):
                all_posts.append({'pid': str(pid), 'uid': '', 'source': 'archive'})
    except Exception:
        pass

    random.shuffle(all_posts)
    logger.info(f"[{account_login}] Пул постов: {len(all_posts)} "
                f"(graphql: {sum(1 for p in all_posts if p['source']=='graphql')}, "
                f"archive: {sum(1 for p in all_posts if p['source']=='archive')})")
    return all_posts


# ══════════════════════════════════════════════════════════════════════════════
#  ОСНОВНАЯ СЕССИЯ
# ══════════════════════════════════════════════════════════════════════════════

def _run_session_sync(account_login, limits, keywords):
    """Синхронная сессия прогрева."""
    stats   = {'likes': 0, 'follows': 0, 'reposts': 0, 'scrolls': 0}
    actions = random_session_actions(limits)

    # Шаг 1: найти юзеров (search → recommended → seed)
    targets = threads_api.find_warmup_targets(keywords, account_login)
    if not targets:
        logger.warning(f"[{account_login}] Нет юзеров для прогрева")
        return stats

    logger.info(f"[{account_login}] Найдено {len(targets)} юзеров")

    # Шаг 2: собрать пул постов через GraphQL
    posts_pool = _collect_posts_pool(targets, account_login)
    post_idx = 0  # индекс для последовательного использования

    def _next_post():
        """Следующий пост из пула (без повторов подряд)."""
        nonlocal post_idx
        if not posts_pool:
            return None
        post = posts_pool[post_idx % len(posts_pool)]
        post_idx += 1
        return post

    # Шаг 3: выполнять действия
    for action in actions:
        t = action['type']
        try:
            if t == 'scroll':
                time.sleep(jitter(action.get('duration', 30)))
                stats['scrolls'] += 1

            elif t == 'like':
                post = _next_post()
                if post:
                    if threads_api.like_thread(post['pid'], account_login):
                        stats['likes'] += 1
                        logger.debug(f"Like ✓ pk={post['pid'][:15]} ({post['source']})")

            elif t == 'follow':
                user = random.choice(targets)
                uid  = str(user.get('pk') or user.get('id', ''))
                if uid and threads_api.follow_user(uid, account_login):
                    stats['follows'] += 1
                    logger.debug(f"Follow ✓ @{user.get('username','?')}")

            elif t == 'repost':
                post = _next_post()
                if post:
                    if threads_api.repost_thread(post['pid'], account_login):
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