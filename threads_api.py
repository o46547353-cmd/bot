### threads_api.py — v2: SlashThreadsClient (заменяет metathreads)
#
# Auth: instagrapi (sessionid, password+2FA, TOTP, session file)
# Threads operations: SlashThreadsClient (like, follow, search, repost, replies, stats, post)
# Image posting: SlashThreadsClient.post_image_thread (через rupload → configure)
#
# ИЗМЕНЕНИЯ v2:
#   - metathreads полностью удалён
#   - все операции через SlashThreadsClient (slash_threads_client.py)
#   - _post_with_reply_metathreads → client.post_thread()
#   - _post_image_to_threads → client.post_image_thread()
#   - _ig_post_text удалён (дублировал post_thread)
#   - парсинг ответов перенесён в SlashThreadsClient

import os, time, random, logging, asyncio, json
from dotenv import load_dotenv
import storage
import threads_auth
from threads_auth import TwoFactorRequired, LoginFailed
from slash_threads_client import SlashThreadsClient, AuthExpired

load_dotenv()
logger = logging.getLogger(__name__)

AUTH_TYPE_INSTAGRAPI = 'instagrapi'
AUTH_TYPE_COOKIE     = 'cookie'

# login → { client: SlashThreadsClient, ig_client, username, user_id, login }
_clients: dict     = {}
# login → { login, password, ig_client } — ожидание кода 2FA
_pending_2fa: dict = {}


# ══════════════════════════════════════════════════════════════
#  INSTAGRAPI — ПОЛУЧИТЬ КЛИЕНТ ИЗ ХРАНИЛИЩА
# ══════════════════════════════════════════════════════════════

def _get_ig_client(login: str):
    """
    Возвращает живой instagrapi Client для аккаунта.
    Нужен ТОЛЬКО для device IDs при создании SlashThreadsClient.
    """
    entry = _clients.get(login, {})
    ig = entry.get('ig_client')
    if ig:
        return ig

    session_file = threads_auth._session_path(login)
    if not os.path.exists(session_file):
        return None

    try:
        from instagrapi import Client
        cl = Client()
        cl.delay_range = [1, 3]
        cl.set_settings(cl.load_settings(session_file))
        if login in _clients:
            _clients[login]['ig_client'] = cl
        logger.info(f"[{login}] instagrapi восстановлен из session-файла ✓")
        return cl
    except Exception as e:
        logger.warning(f"[{login}] instagrapi не восстановлен: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  ИЗВЛЕЧЕНИЕ BEARER ИЗ SESSION-ФАЙЛА (без instagrapi)
# ══════════════════════════════════════════════════════════════

def _extract_bearer_from_session_file(login: str) -> dict:
    """
    Читает data/sessions/{login}.json напрямую как JSON.
    Возвращает: {bearer, sessionid, csrftoken, device_id, uuid} или {}.
    Работает БЕЗ установленного instagrapi.
    """
    import json as _json
    sessions_dir = os.environ.get('SESSIONS_DIR', 'data/sessions')
    path = f"{sessions_dir}/{login}.json"
    if not os.path.exists(path):
        return {}

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = _json.load(f)
    except Exception as e:
        logger.warning(f"[{login}] Не могу прочитать session file: {e}")
        return {}

    result = {}

    # Bearer token — в authorization_data или в headers
    auth_data = data.get('authorization_data', {})
    if isinstance(auth_data, dict):
        # instagrapi хранит: {"ds_user_id": "...", "sessionid": "...", "authorization": "Bearer IGT:2:..."}
        bearer = auth_data.get('authorization', '')
        if not bearer:
            # Альтернативный формат
            mid = auth_data.get('mid', '')
            token = auth_data.get('token', '')
            if token:
                bearer = f'Bearer IGT:2:{token}'
        if bearer:
            result['bearer'] = bearer

    # Cookies
    cookies = data.get('cookies', {})
    if isinstance(cookies, dict):
        result['sessionid']  = cookies.get('sessionid', '')
        result['csrftoken']  = cookies.get('csrftoken', '')
    elif isinstance(cookies, list):
        # Некоторые версии instagrapi хранят cookies как list of dicts
        for c in cookies:
            if isinstance(c, dict):
                if c.get('name') == 'sessionid':
                    result['sessionid'] = c.get('value', '')
                elif c.get('name') == 'csrftoken':
                    result['csrftoken'] = c.get('value', '')

    # Device IDs
    result['device_id'] = data.get('device_settings', {}).get('android_device_id', '')
    result['uuid']      = data.get('uuid', '') or data.get('device_settings', {}).get('uuid', '')

    # User agent
    result['user_agent'] = data.get('user_agent', '')

    if result.get('bearer') or result.get('sessionid'):
        logger.info(f"[{login}] Session file: bearer={'✓' if result.get('bearer') else '✗'}, "
                    f"sid={'✓' if result.get('sessionid') else '✗'}")
    return result


# ══════════════════════════════════════════════════════════════
#  СОЗДАНИЕ SlashThreadsClient
# ══════════════════════════════════════════════════════════════

def _make_client_from_ig(ig_client, user_id: str, username: str) -> SlashThreadsClient:
    """Создать SlashThreadsClient из instagrapi Client (Bearer + cookies + device IDs)."""
    return SlashThreadsClient.from_instagrapi(ig_client, user_id, username)


def _make_client_from_session(sessionid: str, csrftoken: str,
                               user_id: str, username: str) -> SlashThreadsClient:
    """Создать SlashThreadsClient из sessionid cookie."""
    return SlashThreadsClient.from_session(sessionid, csrftoken, user_id, username)


def _make_client_from_bearer(bearer: str, user_id: str, username: str) -> SlashThreadsClient:
    """Создать SlashThreadsClient из Bearer токена."""
    return SlashThreadsClient.from_bearer(bearer, user_id, username)


# ══════════════════════════════════════════════════════════════
#  ЗАГРУЗКА АККАУНТОВ ИЗ БД
# ══════════════════════════════════════════════════════════════

def load_accounts_from_db():
    for acc_ref in storage.get_all_accounts():
        acc = storage.get_account(acc_ref['login'])
        if not acc:
            continue
        login    = acc['login']
        username = acc.get('username', login)
        user_id  = acc.get('user_id', login)
        auth     = acc.get('auth_type', AUTH_TYPE_COOKIE)

        client    = None
        ig_client = None

        # Шаг 1: пробуем instagrapi (если установлен)
        session_file = threads_auth._session_path(login)
        if os.path.exists(session_file):
            try:
                from instagrapi import Client
                ig = Client()
                ig.delay_range = [1, 3]
                ig.set_settings(ig.load_settings(session_file))
                ig_client = ig
                logger.info(f"[{login}] instagrapi session восстановлена ✓")
            except ImportError:
                logger.info(f"[{login}] instagrapi не установлен — читаю session файл напрямую")
            except Exception as e:
                logger.warning(f"[{login}] instagrapi session: {e}")

        # Шаг 2: строим SlashThreadsClient
        try:
            if ig_client:
                # Лучший вариант — из живого instagrapi клиента (Bearer + cookies + device)
                client = _make_client_from_ig(ig_client, user_id, username)
                logger.info(f"[{login}] SlashThreadsClient via instagrapi ✓")
            else:
                # Без instagrapi — читаем session file напрямую
                sf = _extract_bearer_from_session_file(login)
                bearer = sf.get('bearer', '')
                sid    = sf.get('sessionid', '') or acc.get('session_id', '')
                csrf   = sf.get('csrftoken', '') or acc.get('csrf_token', '')

                if bearer:
                    # Есть Bearer — создаём клиент с Bearer + cookies
                    client = _make_client_from_bearer(bearer, user_id, username)
                    # Дополнительно ставим cookies
                    if sid:
                        client.session.cookies.set('sessionid', sid, domain='.threads.net')
                        client.session.cookies.set('sessionid', sid, domain='.instagram.com')
                    if csrf:
                        client.session.cookies.set('csrftoken', csrf, domain='.threads.net')
                        client.session.cookies.set('csrftoken', csrf, domain='.instagram.com')
                        client.session.headers['X-CSRFToken'] = csrf
                    # Device IDs из session file
                    if sf.get('device_id'):
                        client._device_id = sf['device_id']
                    if sf.get('uuid'):
                        client._device_uuid = sf['uuid']
                    logger.info(f"[{login}] SlashThreadsClient via Bearer (session file) ✓")
                elif sid:
                    # Только cookies — fallback
                    client = _make_client_from_session(sid, csrf, user_id, username)
                    logger.info(f"[{login}] SlashThreadsClient via cookie (⚠️ без Bearer)")
                else:
                    logger.warning(f"[{login}] Нет ни Bearer, ни sessionid")
        except Exception as e:
            logger.warning(f"[{login}] SlashThreadsClient: {e}")

        _clients[login] = {
            'client':    client,
            'ig_client': ig_client,
            'username':  username,
            'user_id':   user_id,
            'login':     login,
        }

        has_cl = client is not None
        has_ig = ig_client is not None
        auth_mode = 'Bearer' if (client and 'Authorization' in client.session.headers) else 'Cookie'
        mode = f"{'ig+' if has_ig else ''}{auth_mode}" if has_cl else 'empty'
        logger.info(f"Загружен [{mode}]: {login}")

    logger.info(f"Загружено аккаунтов: {len(_clients)}")


# ══════════════════════════════════════════════════════════════
#  ДОБАВЛЕНИЕ АККАУНТОВ
# ══════════════════════════════════════════════════════════════

def add_account(login: str, password: str) -> dict:
    """
    Логин через instagrapi.
    При 2FA — raise TwoFactorRequired.
    """
    logger.info(f"[{login}] Логин через instagrapi...")
    try:
        result = threads_auth.login(login, password)
        return _save_from_instagrapi_result(login, password, result)
    except TwoFactorRequired as e:
        _pending_2fa[login] = {
            'login':     login,
            'password':  password,
            'ig_client': e.client,
        }
        storage.save_pending_2fa(login, password, 'instagrapi')
        raise
    except LoginFailed as e:
        raise Exception(str(e))


def confirm_2fa(login: str, code: str) -> dict:
    """Завершение 2FA."""
    if login not in _pending_2fa:
        saved = storage.get_pending_2fa(login)
        if saved:
            logger.info(f"[{login}] pending_2fa восстановлен из БД")
            _pending_2fa[login] = {
                'login':     saved['login'],
                'password':  saved['password'],
                'ig_client': None,
            }
        else:
            raise Exception(
                "Сессия 2FA не найдена.\n\n"
                "Код истёк или бот был перезапущен.\n"
                "Начни заново: /add_account"
            )

    pending   = _pending_2fa[login]
    password  = pending.get('password', '')
    ig_client = pending.get('ig_client')

    if not ig_client:
        _pending_2fa.pop(login, None)
        storage.delete_pending_2fa(login)
        raise Exception(
            "Сессия 2FA потеряна (бот был перезапущен).\n\n"
            "Введи /add_account снова — придёт новый код 2FA."
        )

    try:
        result = threads_auth.confirm_2fa(ig_client, login, password, code)
        _pending_2fa.pop(login, None)
        storage.delete_pending_2fa(login)
        return _save_from_instagrapi_result(login, password, result)
    except Exception:
        raise


def add_account_manual(login: str, session_id: str, csrf_token: str) -> dict:
    """Добавление через sessionid из браузера."""
    user_id  = ''
    username = login
    ig_client = None

    # Пробуем instagrapi login_by_sessionid
    try:
        result = threads_auth.login_by_sessionid(login, session_id, csrf_token)
        user_id   = result.get('user_id', '')
        username  = result.get('username', login)
        ig_client = result.get('client')
    except Exception as e:
        logger.warning(f"[{login}] instagrapi by sessionid: {e}")

    # Создаём наш клиент из cookies
    client = _make_client_from_session(session_id, csrf_token, user_id, username)

    # Пробуем получить user_id/username через наш клиент
    if not user_id:
        try:
            resolved = client.get_user_id(login)
            if resolved:
                user_id = resolved
                client.user_id = user_id
        except Exception as e:
            logger.warning(f"[{login}] get_user_id: {e}")

    if username == login:
        try:
            info = client.get_user_info(login)
            if info.get('username'):
                username = info['username']
                client.username = username
        except Exception:
            pass

    _clients[login] = {
        'client':    client,
        'ig_client': ig_client,
        'username':  username,
        'user_id':   user_id,
        'login':     login,
    }
    storage.save_account({
        'login':      login,
        'session_id': session_id,
        'csrf_token': csrf_token,
        'user_id':    user_id,
        'username':   username,
        'auth_type':  AUTH_TYPE_COOKIE,
    })
    logger.info(f"[{login}] Добавлен через cookies. username={username}")
    return {'login': login, 'username': username}


def _save_from_instagrapi_result(login: str, password: str, result: dict) -> dict:
    """Сохраняет аккаунт после успешного instagrapi-логина."""
    ig_client = result.get('client')
    user_id   = result.get('user_id', login)
    username  = result.get('username', login)
    sessionid = result.get('sessionid', '')
    csrftoken = result.get('csrftoken', '')

    # Создаём SlashThreadsClient из instagrapi (самый надёжный способ)
    client = None
    if ig_client:
        try:
            client = _make_client_from_ig(ig_client, user_id, username)
            logger.info(f"[{login}] SlashThreadsClient via instagrapi ✓")
        except Exception as e:
            logger.warning(f"[{login}] SlashThreadsClient from ig: {e}")

    # Fallback: из sessionid
    if client is None and sessionid:
        try:
            client = _make_client_from_session(sessionid, csrftoken, user_id, username)
            logger.info(f"[{login}] SlashThreadsClient via sessionid ✓")
        except Exception as e:
            logger.warning(f"[{login}] SlashThreadsClient from session: {e}")

    _clients[login] = {
        'client':    client,
        'ig_client': ig_client,
        'username':  username,
        'user_id':   user_id,
        'login':     login,
    }
    storage.save_account({
        'login':      login,
        'session_id': sessionid,
        'csrf_token': csrftoken,
        'user_id':    user_id,
        'username':   username,
        'auth_type':  AUTH_TYPE_INSTAGRAPI,
    })
    # Сохраняем пароль для авто-перелогина (TOTP seed сохраняется отдельно)
    if password:
        storage.set_account_credentials(login, password=password)
    logger.info(f"[{login}] Сохранён. username={username}")
    return {'login': login, 'username': username}


# ══════════════════════════════════════════════════════════════
#  ОБНОВЛЕНИЕ ТОКЕНА
# ══════════════════════════════════════════════════════════════

async def refresh_token(login: str, password: str) -> bool:
    """Перелогин через instagrapi с TOTP, обновляем клиенты в памяти."""
    try:
        # Получаем TOTP seed из БД
        creds = storage.get_account_credentials(login)
        totp_seed = creds.get('totp_seed', '') if creds else ''
        totp_code = None
        if totp_seed:
            try:
                import pyotp
                totp_code = pyotp.TOTP(totp_seed.replace(' ', '').upper()).now()
                logger.info(f"[{login}] refresh_token: TOTP код сгенерирован")
            except Exception:
                pass

        result = await asyncio.to_thread(
            threads_auth.login, login, password,
            verification_code=totp_code, totp_seed=totp_seed
        )
        _save_from_instagrapi_result(login, password, result)
        logger.info(f"[{login}] Токен обновлён ✓")
        return True
    except TwoFactorRequired as e:
        # 2FA — пробуем подтвердить автоматически
        if totp_seed and e.client:
            try:
                import pyotp
                code = pyotp.TOTP(totp_seed.replace(' ', '').upper()).now()
                result = threads_auth.confirm_2fa(e.client, login, password, code)
                _save_from_instagrapi_result(login, password, result)
                logger.info(f"[{login}] Токен обновлён через 2FA ✓")
                return True
            except Exception as e2:
                logger.warning(f"[{login}] refresh 2FA: {e2}")
        logger.warning(f"[{login}] Обновление токена: {e}")
        return False
    except Exception as e:
        logger.warning(f"[{login}] Обновление токена: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ — GET CLIENT, LIST
# ══════════════════════════════════════════════════════════════

def get_client(login=None) -> dict:
    if login and login in _clients:
        return _clients[login]
    if not login and _clients:
        return next(iter(_clients.values()))
    raise Exception(
        f"Аккаунт {'«' + login + '»' if login else ''} не найден. "
        "Авторизуйтесь через /add_account или /manual_cookies."
    )


def list_accounts() -> list:
    return list(_clients.keys())


# ══════════════════════════════════════════════════════════════
#  ПУБЛИКАЦИЯ СЕРИИ
# ══════════════════════════════════════════════════════════════

async def post_series_async(posts: dict, image_path: str = None,
                            account_login: str = None) -> list:
    """
    Публикует 4 поста цепочкой.
    При 403 — авто-перелогин и повтор.
    """
    entry  = get_client(account_login)
    login  = entry['login']

    def _get_fresh_client():
        return get_client(login)['client']

    client = entry['client']

    if not client:
        raise Exception(
            f"Аккаунт {login} не инициализирован.\n"
            "Используй /add_account или /manual_cookies."
        )

    logger.info(f"[{login}] Публикую серию: {posts.get('topic', '—')}")
    img = image_path if (image_path and os.path.exists(image_path)) else None

    async def _post_text(caption: str, reply_to: str = None) -> str:
        nonlocal client
        try:
            pk = await asyncio.to_thread(client.post_thread, caption, reply_to)
        except AuthExpired:
            logger.info(f"[{login}] post_thread 403 → авто-перелогин...")
            ok = await asyncio.to_thread(_auto_relogin, login)
            if not ok:
                raise Exception("Постинг 403 и авто-перелогин не удался")
            client = _get_fresh_client()
            pk = await asyncio.to_thread(client.post_thread, caption, reply_to)
        if not pk:
            raise Exception(f"post_thread вернул пустой pk")
        return pk

    async def _post_image(caption: str, img_path: str, reply_to: str = None) -> str:
        nonlocal client
        try:
            pk = await asyncio.to_thread(client.post_image_thread, caption, img_path, reply_to)
        except AuthExpired:
            logger.info(f"[{login}] post_image 403 → авто-перелогин...")
            ok = await asyncio.to_thread(_auto_relogin, login)
            if not ok:
                raise Exception("Image posting 403 и авто-перелогин не удался")
            client = _get_fresh_client()
            pk = await asyncio.to_thread(client.post_image_thread, caption, img_path, reply_to)
        return pk

    ids = []

    # Пост 1 — хук (с картинкой если есть)
    id1 = ''
    if img:
        try:
            id1 = await _post_image(posts['post1'], img)
            logger.info(f"[{login}] Пост 1 (image): {id1}")
        except Exception as e:
            logger.warning(f"[{login}] Image posting: {e}, публикую текст...")
    if not id1:
        id1 = await _post_text(posts['post1'])
        logger.info(f"[{login}] Пост 1: {id1}")
    ids.append(id1)
    await asyncio.sleep(random.uniform(8, 14))

    # Пост 2 — боль
    id2 = await _post_text(posts['post2'], id1)
    ids.append(id2)
    logger.info(f"[{login}] Пост 2: {id2}")
    await asyncio.sleep(random.uniform(8, 14))

    # Пост 3 — решение
    id3 = await _post_text(posts['post3'], id2)
    ids.append(id3)
    logger.info(f"[{login}] Пост 3: {id3}")
    await asyncio.sleep(random.uniform(8, 14))

    # Пост 4 — дожим
    id4 = await _post_text(posts['post4'], id3)
    ids.append(id4)
    logger.info(f"[{login}] Пост 4: {id4}")

    logger.info(f"[{login}] ✓ Серия опубликована: {ids}")
    return ids


def post_series(posts: dict, image_path: str = None,
                account_login: str = None) -> list:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(post_series_async(posts, image_path, account_login))
    finally:
        loop.close()


# ══════════════════════════════════════════════════════════════
#  АВТО-ПЕРЕЛОГИН ПРИ 403
# ══════════════════════════════════════════════════════════════

import threading
_relogin_lock = threading.Lock()
_last_relogin = {}  # login → timestamp (не чаще раз в 5 минут)


def _auto_relogin(login: str) -> bool:
    """
    Перелогин через instagrapi + TOTP. Вызывается автоматически при 403.
    Возвращает True если успешно перелогинились.
    """
    import time as _time

    # Не чаще раз в 5 минут
    now = _time.time()
    if now - _last_relogin.get(login, 0) < 300:
        logger.info(f"[{login}] Перелогин пропущен (< 5 мин)")
        return False

    with _relogin_lock:
        # Ещё раз проверяем (другой поток мог уже перелогиниться)
        if now - _last_relogin.get(login, 0) < 300:
            return False

        creds = storage.get_account_credentials(login)
        if not creds:
            logger.warning(f"[{login}] Нет credentials для перелогина")
            return False

        password  = creds.get('password', '')
        totp_seed = creds.get('totp_seed', '')

        if not password:
            logger.warning(f"[{login}] Нет пароля для перелогина. "
                          f"Установи через /add_account или кнопку 🔑 TOTP")
            return False

        logger.info(f"[{login}] 🔄 Авто-перелогин (403 detected)...")

        try:
            # Генерируем TOTP код если есть seed
            totp_code = None
            if totp_seed:
                try:
                    import pyotp
                    totp_code = pyotp.TOTP(totp_seed.replace(' ', '').upper()).now()
                    logger.info(f"[{login}] TOTP код сгенерирован")
                except Exception as e:
                    logger.warning(f"[{login}] TOTP ошибка: {e}")

            # Логин через instagrapi
            result = threads_auth.login(login, password, verification_code=totp_code, totp_seed=totp_seed)

            # Обновляем клиент в памяти
            _save_from_instagrapi_result(login, password, result)
            _last_relogin[login] = _time.time()
            logger.info(f"[{login}] ✅ Авто-перелогин успешен!")
            return True

        except TwoFactorRequired as e:
            # 2FA нужна но TOTP seed не задан или невалидный
            if totp_seed and e.client:
                try:
                    import pyotp
                    code = pyotp.TOTP(totp_seed.replace(' ', '').upper()).now()
                    result = threads_auth.confirm_2fa(e.client, login, password, code)
                    _save_from_instagrapi_result(login, password, result)
                    _last_relogin[login] = _time.time()
                    logger.info(f"[{login}] ✅ Авто-перелогин через 2FA успешен!")
                    return True
                except Exception as e2:
                    logger.error(f"[{login}] 2FA авто-подтверждение: {e2}")
            else:
                logger.warning(f"[{login}] 2FA нужна, но TOTP seed не задан")
            return False

        except Exception as e:
            logger.error(f"[{login}] Авто-перелогин ошибка: {e}")
            _last_relogin[login] = _time.time()  # Не спамим повторами
            return False


def _with_relogin(login: str, fn, fn_name: str = ''):
    """
    Обёртка: вызывает fn(client), при AuthExpired — перелогин и повтор.
    """
    client = _cl(login)
    try:
        return fn(client)
    except AuthExpired:
        logger.info(f"[{login}] {fn_name}: 403 → пробую авто-перелогин...")
        if _auto_relogin(login):
            # Получаем свежий клиент после перелогина
            client = _cl(login)
            try:
                return fn(client)
            except Exception as e:
                logger.warning(f"[{login}] {fn_name} после перелогина: {e}")
                raise
        else:
            raise Exception(f"{fn_name}: 403 и авто-перелогин не удался")


# ══════════════════════════════════════════════════════════════
#  ПРОЧИЕ ДЕЙСТВИЯ — с авто-перелогином
# ══════════════════════════════════════════════════════════════

def _cl(login: str) -> SlashThreadsClient:
    """Получить SlashThreadsClient для аккаунта."""
    entry = get_client(login)
    client = entry['client']
    if not client:
        raise Exception(f"Нет клиента для {login}")
    return client


def get_thread_replies(post_id: str, account_login: str = None) -> list:
    try:
        return _with_relogin(account_login,
            lambda c: c.get_thread_replies(post_id), 'get_thread_replies')
    except Exception as e:
        logger.warning(f"get_thread_replies({post_id}): {e}")
        return []


def like_thread(post_id: str, account_login: str = None) -> bool:
    try:
        return _with_relogin(account_login,
            lambda c: c.like(post_id), 'like_thread')
    except Exception as e:
        logger.warning(f"like_thread({post_id}): {e}")
        return False


def repost_thread(post_id: str, account_login: str = None) -> bool:
    try:
        return _with_relogin(account_login,
            lambda c: c.repost(post_id), 'repost_thread')
    except Exception as e:
        logger.warning(f"repost_thread({post_id}): {e}")
        return False


def follow_user(user_id: str, account_login: str = None) -> bool:
    try:
        return _with_relogin(account_login,
            lambda c: c.follow(user_id), 'follow_user')
    except Exception as e:
        logger.warning(f"follow_user({user_id}): {e}")
        return False


def search_users(query: str, account_login: str = None) -> list:
    try:
        return _with_relogin(account_login,
            lambda c: c.search_users(query), 'search_users')
    except Exception as e:
        logger.warning(f"search_users({query}): {e}")
        return []


def get_recommended_users(search_query: str = '', account_login: str = None) -> list:
    try:
        return _with_relogin(account_login,
            lambda c: c.get_recommended_users(search_query), 'get_recommended_users')
    except Exception as e:
        logger.warning(f"get_recommended_users: {e}")
        return []


# Популярные аккаунты Threads — fallback если поиск и рекомендации пусты
SEED_USERS = [
    {'pk': '25025320',    'username': 'zuck'},
    {'pk': '10784921',    'username': 'mosseri'},
    {'pk': '25921237',    'username': 'instagram'},
    {'pk': '7719696689',  'username': 'threadsapp'},
    {'pk': '232192182',   'username': 'mkbhd'},
    {'pk': '460563723',   'username': 'garyvee'},
    {'pk': '4213518589',  'username': 'codewithandrea'},
    {'pk': '2440249055',  'username': 'tech'},
    {'pk': '13460080',    'username': 'nasa'},
    {'pk': '6860189',     'username': 'nike'},
    {'pk': '25955306',    'username': 'natgeo'},
]


def find_warmup_targets(keywords: list, account_login: str = None) -> list:
    """
    Ищет юзеров для прогрева. Три стратегии с fallback:
      1. search_users по ключевым словам
      2. recommended_users (с и без query)
      3. Захардкоженные популярные аккаунты (seed)

    Возвращает список юзеров [{pk, username, ...}]
    """
    users = []

    # Стратегия 1: поиск
    for kw in keywords[:5]:
        try:
            found = search_users(kw, account_login)
            if found:
                users.extend(found)
                logger.info(f"find_warmup_targets: search({kw}) → {len(found)} юзеров")
                if len(users) >= 10:
                    break
        except Exception:
            pass

    if users:
        return users

    # Стратегия 2: рекомендации
    try:
        rec = get_recommended_users('', account_login)
        if rec:
            users.extend(rec)
            logger.info(f"find_warmup_targets: recommended → {len(rec)} юзеров")
    except Exception:
        pass

    if not users:
        for kw in keywords[:3]:
            try:
                rec = get_recommended_users(kw, account_login)
                if rec:
                    users.extend(rec)
                    logger.info(f"find_warmup_targets: recommended({kw}) → {len(rec)} юзеров")
                    break
            except Exception:
                pass

    if users:
        return users

    # Стратегия 3: seed-аккаунты (гарантированно есть)
    logger.info(f"find_warmup_targets: search и recommended пусты — используем seed-аккаунты")
    import random as _rnd
    seed_copy = list(SEED_USERS)
    _rnd.shuffle(seed_copy)
    return seed_copy


def get_user_threads(user_id: str, account_login: str = None) -> list:
    try:
        return _with_relogin(account_login,
            lambda c: c.get_user_threads(user_id), 'get_user_threads')
    except Exception as e:
        logger.warning(f"get_user_threads({user_id}): {e}")
        return []


def get_timeline(account_login: str = None) -> list:
    """Домашняя лента — посты для лайков."""
    try:
        return _with_relogin(account_login,
            lambda c: c.get_timeline(), 'get_timeline')
    except Exception as e:
        logger.warning(f"get_timeline: {e}")
        return []


def get_explore(account_login: str = None) -> list:
    """Explore-лента Threads."""
    try:
        return _with_relogin(account_login,
            lambda c: c.get_text_app_explore(), 'get_explore')
    except Exception as e:
        logger.warning(f"get_explore: {e}")
        return []


def get_thread_stats(post_id: str, account_login: str = None) -> dict:
    try:
        return _with_relogin(account_login,
            lambda c: c.get_thread_stats(post_id), 'get_thread_stats')
    except Exception as e:
        logger.warning(f"get_thread_stats({post_id}): {e}")
        return {}