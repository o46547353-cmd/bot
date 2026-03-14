### humanize.py
import time, random, logging
from datetime import datetime
try:
    import pytz
    HAS_PYTZ = True
except ImportError:
    HAS_PYTZ = False

logger = logging.getLogger(__name__)

# Безопасные лимиты в день
LIMITS_A = {'likes': (8, 25),  'follows': (3, 15),  'reposts': (0, 3),  'scroll_min': (10, 20)}
LIMITS_B = {'likes': (15, 30), 'follows': (8, 20),  'reposts': (1, 5),  'scroll_min': (15, 30)}

PAUSES = {
    'like':             (30,  90),
    'follow':           (60,  150),
    'repost':           (120, 300),
    'scroll':           (5,   20),
    'view_profile':     (10,  40),
    'between_sessions': (3600, 10800),
}


def human_sleep(min_sec, max_sec):
    """Пауза с нормальным распределением + jitter ±10%."""
    mu    = (min_sec + max_sec) / 2
    sigma = (max_sec - min_sec) / 4
    delay = max(min_sec, min(max_sec, random.gauss(mu, sigma)))
    delay *= random.uniform(0.9, 1.1)
    logger.debug(f"Пауза {delay:.1f}s")
    time.sleep(delay)


def jitter(base, pct=0.2):
    return base * random.uniform(1 - pct, 1 + pct)


def pause_after(action_type):
    lo, hi = PAUSES.get(action_type, (10, 30))
    human_sleep(lo, hi)


def is_active_hour(timezone='Europe/Moscow', start=9, end=23):
    try:
        if HAS_PYTZ:
            tz  = pytz.timezone(timezone)
            now = datetime.now(tz)
        else:
            now = datetime.now()
        return start <= now.hour < end
    except Exception:
        return 9 <= datetime.now().hour < 23


def random_daily_limits(preset='A'):
    """Каждый день разные лимиты — никогда не одинаковые."""
    src = LIMITS_A if preset == 'A' else LIMITS_B
    return {k: random.randint(lo, hi) for k, (lo, hi) in src.items()}


def should_skip_today():
    """~14% шанс пропустить день как выходной."""
    return random.random() < 0.14


def random_session_actions(limits):
    """Случайный план сессии — нелинейный порядок действий."""
    actions = [{'type': 'scroll', 'duration': random.randint(30, 90)}]

    likes = min(limits['likes'], random.randint(3, 10))
    for _ in range(likes):
        actions.append({'type': 'scroll', 'duration': random.randint(15, 60)})
        if random.random() < 0.4:
            actions.append({'type': 'view_profile'})
        actions.append({'type': 'like'})

    follows = min(limits['follows'], random.randint(1, 5))
    for _ in range(follows):
        idx = random.randint(1, max(1, len(actions) - 1))
        actions.insert(idx, {'type': 'view_profile'})
        actions.insert(idx + 1, {'type': 'follow'})

    if limits.get('reposts', 0) > 0 and random.random() < 0.5:
        idx = random.randint(len(actions) // 2, len(actions))
        actions.insert(idx, {'type': 'repost'})

    actions.append({'type': 'scroll', 'duration': random.randint(20, 60)})
    return actions
