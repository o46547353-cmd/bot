### threads_auth.py  —  FIXED: timeout + retries
"""
Авторизация через Bloks API (metathreads).
FIX: metathreads default timeout = 5 сек — слишком мало для Instagram login.
     Увеличен до 30 сек + 3 попытки с паузой между ними.
"""
import time
import logging
from metathreads import MetaThreads, config

logger = logging.getLogger(__name__)


class BloksLoginFailed(Exception):
    """Ошибка входа через Bloks (metathreads). Вызывающий код может попробовать Danie1."""
    pass


LOGIN_TIMEOUT = 30   # секунды
MAX_RETRIES   = 3
RETRY_DELAY   = 5    # секунд между попытками


def login(username: str, password: str) -> dict:
    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info(f"[{username}] Авторизация, попытка {attempt}/{MAX_RETRIES}...")
        try:
            config.TIMEOUT = LOGIN_TIMEOUT   # увеличиваем до каждого запроса
            client     = MetaThreads()
            token_data = client.login(username, password)

            me         = client.logged_in_user or {}
            user_id    = str(me.get('pk') or me.get('id', ''))
            uname      = me.get('username', username)
            auth_token = client.session.headers.get('Authorization', '')
            mid_token  = client.session.headers.get('X-Mid', '')

            logger.info(f"[{username}] Авторизован. user_id={user_id}, username={uname}")
            return {
                'auth_token': auth_token,
                'mid_token':  mid_token,
                'user_id':    user_id,
                'username':   uname,
                'client':     client,
            }

        except Exception as e:
            last_err = e
            err = str(e).lower()

            # Ошибки при которых повтор не поможет (Bloks API) — вызывающий код может попробовать Danie1
            if "login failed" in err or "login_failed" in err:
                raise BloksLoginFailed(
                    "Неверный логин или пароль (Bloks).\n\n"
                    "Если пароль точно верный:\n"
                    "• Подожди 15-30 минут (Instagram временно блокирует)\n"
                    "• Или используй /manual_cookies"
                )
            if any(k in err for k in ("two_factor", "2fa")):
                raise TwoFactorRequired(username)
            if any(k in err for k in ("checkpoint", "challenge")):
                raise Exception(
                    "Instagram требует подтверждение входа.\n"
                    "Используй /manual_cookies."
                )
            if any(k in err for k in ("rate_limit", "spam", "block")):
                raise Exception(
                    "Instagram временно заблокировал вход.\n"
                    "Подожди 15-30 минут, затем попробуй снова."
                )

            # Таймаут / сеть — пауза и повтор
            is_timeout = any(k in err for k in (
                "timed out", "timeout", "time out",
                "read operation", "connection", "network",
                "remotedisconnected", "connectionerror",
            ))
            if is_timeout:
                logger.warning(f"[{username}] Таймаут (попытка {attempt}), жду {RETRY_DELAY}с...")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                    continue
            else:
                logger.error(f"[{username}] Ошибка (попытка {attempt}): {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)

    # Все попытки исчерпаны
    err_str = str(last_err).lower()
    is_timeout = any(k in err_str for k in ("timed out", "timeout", "read operation"))
    if is_timeout:
        raise Exception(
            "Instagram не отвечает (timeout) после 3 попыток.\n\n"
            "Что делать:\n"
            "1. Подожди 5-10 минут и попробуй /add_account снова\n"
            "2. Или сразу используй /manual_cookies — это всегда работает"
        )
    raise Exception(
        f"Ошибка авторизации после {MAX_RETRIES} попыток: {last_err}\n\n"
        "Попробуй /manual_cookies"
    )


class TwoFactorRequired(Exception):
    def __init__(self, login, two_factor_info=None):
        self.login           = login
        self.two_factor_info = two_factor_info or {}
        super().__init__(f"2FA required for {login}")
