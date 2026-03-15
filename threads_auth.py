### threads_auth.py  —  REWRITTEN
"""
Авторизация через Bloks API (тот же метод что использует metathreads).
Старый метод (#PWD_INSTAGRAM:4, мобильный v1 API) заблокирован Instagram.
Новый метод: #PWD_INSTAGRAM_BROWSER:0 через Bloks endpoint — работает.
"""
import logging
from metathreads import MetaThreads

logger = logging.getLogger(__name__)


def login(username: str, password: str) -> dict:
    """
    Авторизация через metathreads Bloks API.
    Возвращает dict с ключами: auth_token, mid_token, user_id, username.
    Может вызвать TwoFactorRequired или Exception с понятным текстом.
    """
    logger.info(f"[{username}] Авторизация через Bloks API...")
    try:
        client = MetaThreads()
        token_data = client.login(username, password)
    except Exception as e:
        err = str(e).lower()
        msg = str(e)

        if "login failed" in err or "login_failed" in err:
            raise Exception(
                "Неверный логин или пароль.\n\n"
                "Если пароль точно верный:\n"
                "• Instagram мог временно заблокировать вход после нескольких попыток\n"
                "• Подожди 15–30 минут и попробуй снова\n"
                "• Или добавь через /manual_cookies (cookies из браузера)"
            )

        if any(k in err for k in ("two_factor", "2fa", "two factor")):
            raise TwoFactorRequired(username)

        if any(k in err for k in ("checkpoint", "challenge")):
            raise Exception(
                "Instagram требует подтверждение входа.\n"
                "Используй /manual_cookies — добавь через cookies браузера."
            )

        if any(k in err for k in ("rate_limit", "wait", "spam", "block")):
            raise Exception(
                "Instagram временно заблокировал вход (rate limit).\n"
                "Подожди 15–30 минут и попробуй снова.\n"
                "Или используй /manual_cookies."
            )

        raise Exception(f"Ошибка авторизации: {msg}\n\nПопробуй /manual_cookies")

    # Извлекаем данные после успешного входа
    me = client.logged_in_user or {}
    user_id  = str(me.get('pk') or me.get('id', ''))
    uname    = me.get('username', username)

    # Bearer token и Mid из заголовков сессии
    auth_token = client.session.headers.get('Authorization', '')
    mid_token  = client.session.headers.get('X-Mid', '')

    logger.info(f"[{username}] Авторизован. user_id={user_id}, username={uname}")

    return {
        'auth_token': auth_token,   # Bearer IGT:2:xxxxx
        'mid_token':  mid_token,
        'user_id':    user_id,
        'username':   uname,
        'client':     client,       # готовый клиент (не нужно пересоздавать)
    }


class TwoFactorRequired(Exception):
    def __init__(self, login, two_factor_info=None):
        self.login = login
        self.two_factor_info = two_factor_info or {}
        super().__init__(f"2FA required for {login}")
