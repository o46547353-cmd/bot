### threads_auth.py — instagrapi auth (replaces metathreads Bloks + Danie1)
"""
Авторизация через instagrapi (subzeroid/instagrapi).
Поддерживает: пароль+2FA, login_by_sessionid, TOTP seed, сохранение сессии.
После логина извлекаем sessionid + Bearer token → передаём в metathreads.
"""

import os, logging

logger = logging.getLogger(__name__)
SESSIONS_DIR = os.environ.get('SESSIONS_DIR', 'data/sessions')


class TwoFactorRequired(Exception):
    """2FA нужна. client — живой instagrapi Client для confirm_2fa."""
    def __init__(self, login: str, client=None):
        self.login  = login
        self.client = client
        super().__init__(f"2FA required for {login}")


class LoginFailed(Exception):
    pass


def _session_path(login: str) -> str:
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    return f"{SESSIONS_DIR}/{login}.json"


def _make_client():
    from instagrapi import Client
    cl = Client()
    cl.delay_range = [1, 3]
    return cl


def _extract_bearer(cl) -> str:
    try:
        auth = cl.private.headers.get('Authorization', '')
        if auth:
            return auth
    except Exception:
        pass
    return f'Bearer IGT:2:{cl.sessionid}'


def _build_result(cl, username: str) -> dict:
    return {
        'sessionid':  cl.sessionid or '',
        'csrftoken':  cl.csrftoken or '',
        'auth_token': _extract_bearer(cl),
        'user_id':    str(cl.user_id or ''),
        'username':   cl.username or username,
        'client':     cl,
    }


def _totp_code(seed: str):
    try:
        import pyotp
        return pyotp.TOTP(seed.replace(' ', '').upper()).now()
    except Exception as e:
        logger.warning(f"TOTP ошибка: {e}")
        return None


def login(username: str, password: str,
          verification_code: str = None,
          totp_seed: str = None) -> dict:
    """
    Логин через instagrapi.
    1. Пробует восстановить сохранённую сессию (не трогает Instagram)
    2. Если устарела или нет — полный логин с паролем
    3. Если 2FA — raise TwoFactorRequired(client=cl)

    totp_seed: 16-символьный ключ Google Auth → код генерируется автоматически.
    verification_code: передай если уже знаешь код.
    """
    from instagrapi.exceptions import (
        TwoFactorRequired as IG_2FA, BadPassword,
        LoginRequired, ChallengeRequired, FeedbackRequired,
    )

    session_file = _session_path(username)

    # Шаг 1: восстановить сессию
    if os.path.exists(session_file) and not verification_code:
        cl = _make_client()
        try:
            cl.set_settings(cl.load_settings(session_file))
            cl.login(username, password)
            try:
                cl.get_timeline_feed()
                logger.info(f"[{username}] Сессия восстановлена ✓")
                return _build_result(cl, username)
            except LoginRequired:
                logger.info(f"[{username}] Сессия устарела, логинюсь заново...")
                old = cl.get_settings()
                cl.set_settings({})
                cl.set_uuids(old.get('uuids', {}))
        except IG_2FA:
            raise TwoFactorRequired(username, client=cl)
        except Exception as e:
            logger.warning(f"[{username}] Сессия не восстановлена: {e}")
        cl = _make_client()
    else:
        cl = _make_client()

    # Шаг 2: TOTP / код 2FA
    code = verification_code
    if not code and totp_seed:
        code = _totp_code(totp_seed)
        if code:
            logger.info(f"[{username}] TOTP код: {code}")

    # Шаг 3: полный логин
    kwargs = {'verification_code': code} if code else {}
    try:
        cl.login(username, password, **kwargs)
        cl.dump_settings(session_file)
        logger.info(f"[{username}] Залогинен через instagrapi ✓")
        return _build_result(cl, username)

    except IG_2FA:
        raise TwoFactorRequired(username, client=cl)

    except BadPassword:
        raise LoginFailed("Неверный логин или пароль.")

    except ChallengeRequired:
        raise LoginFailed(
            "Instagram требует подтверждение (checkpoint).\n"
            "Используй /manual_cookies"
        )

    except FeedbackRequired as e:
        msg = str(e).lower()
        if any(k in msg for k in ('spam', 'block', 'limit')):
            raise LoginFailed(
                "Instagram временно заблокировал вход с этого IP.\n"
                "Используй /manual_cookies — работает без IP-логина."
            )
        raise LoginFailed(f"Instagram отклонил вход: {e}")

    except Exception as e:
        err = str(e).lower()
        if any(k in err for k in ('timeout', 'timed out', 'connection')):
            raise LoginFailed(
                "Instagram не отвечает (timeout).\n"
                "Используй /manual_cookies — работает без IP-логина."
            )
        raise LoginFailed(f"Ошибка входа: {e}")


def confirm_2fa(client, username: str, password: str, code: str) -> dict:
    """
    Завершение 2FA.
    client — объект из TwoFactorRequired.client.
    instagrapi принимает код прямо в login() — никаких отдельных шагов.
    """
    from instagrapi.exceptions import BadPassword, TwoFactorRequired as IG_2FA
    session_file = _session_path(username)
    try:
        client.login(username, password, verification_code=code.strip())
        client.dump_settings(session_file)
        logger.info(f"[{username}] 2FA подтверждена ✓")
        return _build_result(client, username)
    except (BadPassword, IG_2FA):
        raise Exception(
            "Неверный код 2FA.\n\n"
            "• Код одноразовый — бери свежий из Google Authenticator\n"
            "• SMS-коды не поддерживаются, только TOTP"
        )
    except Exception as e:
        raise Exception(f"Ошибка 2FA: {e}")


def login_by_sessionid(username: str, sessionid: str, csrftoken: str = '') -> dict:
    """Вход по sessionid из браузера. Надёжно, без IP-блоков."""
    cl = _make_client()
    try:
        cl.login_by_sessionid(sessionid)
        cl.dump_settings(_session_path(username))
        logger.info(f"[{username}] Залогинен по sessionid ✓")
        return _build_result(cl, username)
    except Exception as e:
        raise LoginFailed(f"Ошибка входа по sessionid: {e}")
