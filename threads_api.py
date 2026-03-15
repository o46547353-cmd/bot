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
from slash_threads_client import SlashThreadsClient

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

        # Восстанавливаем instagrapi из session-файла
        session_file = threads_auth._session_path(login)
        if os.path.exists(session_file):
            try:
                from instagrapi import Client
                ig = Client()
                ig.delay_range = [1, 3]
                ig.set_settings(ig.load_settings(session_file))
                ig_client = ig
                logger.info(f"[{login}] instagrapi session восстановлена ✓")
            except Exception as e:
                logger.warning(f"[{login}] instagrapi session: {e}")

        # Строим SlashThreadsClient
        try:
            if auth == AUTH_TYPE_INSTAGRAPI and ig_client:
                client = _make_client_from_ig(ig_client, user_id, username)
                logger.info(f"[{login}] SlashThreadsClient via instagrapi ✓")
            else:
                sid  = acc.get('session_id', '')
                csrf = acc.get('csrf_token', '')
                if sid:
                    client = _make_client_from_session(sid, csrf, user_id, username)
                    logger.info(f"[{login}] SlashThreadsClient via cookie ✓")
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
        mode = 'full' if (has_cl and has_ig) else ('cl-only' if has_cl else ('ig-only' if has_ig else 'empty'))
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
    logger.info(f"[{login}] Сохранён. username={username}")
    return {'login': login, 'username': username}


# ══════════════════════════════════════════════════════════════
#  ОБНОВЛЕНИЕ ТОКЕНА
# ══════════════════════════════════════════════════════════════

async def refresh_token(login: str, password: str) -> bool:
    """Перелогин через instagrapi, обновляем клиенты в памяти."""
    try:
        result = await asyncio.to_thread(threads_auth.login, login, password)
        _save_from_instagrapi_result(login, password, result)
        logger.info(f"[{login}] Токен обновлён ✓")
        return True
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
    Всё через SlashThreadsClient — один метод, без fallback-дублирования.
    """
    entry  = get_client(account_login)
    login  = entry['login']
    client = entry['client']  # SlashThreadsClient

    if not client:
        raise Exception(
            f"Аккаунт {login} не инициализирован.\n"
            "Используй /add_account или /manual_cookies."
        )

    logger.info(f"[{login}] Публикую серию: {posts.get('topic', '—')}")
    img = image_path if (image_path and os.path.exists(image_path)) else None

    async def _post_text(caption: str, reply_to: str = None) -> str:
        pk = await asyncio.to_thread(client.post_thread, caption, reply_to)
        if not pk:
            raise Exception(f"post_thread вернул пустой pk")
        return pk

    async def _post_image(caption: str, img_path: str, reply_to: str = None) -> str:
        pk = await asyncio.to_thread(client.post_image_thread, caption, img_path, reply_to)
        return pk

    ids = []

    # Пост 1 — хук
    id1 = await _post_text(posts['post1'])
    ids.append(id1)
    logger.info(f"[{login}] Пост 1: {id1}")
    await asyncio.sleep(random.uniform(8, 14))

    # Пост 2 — боль
    id2 = await _post_text(posts['post2'], id1)
    ids.append(id2)
    logger.info(f"[{login}] Пост 2: {id2}")
    await asyncio.sleep(random.uniform(8, 14))

    # Пост 3 — решение (с картинкой если есть)
    id3 = ''
    if img:
        try:
            id3 = await _post_image(posts['post3'], img, id2)
            logger.info(f"[{login}] Пост 3 (image): {id3}")
        except Exception as e:
            logger.warning(f"[{login}] Image posting: {e}, публикую текст...")
    if not id3:
        id3 = await _post_text(posts['post3'], id2)
        logger.info(f"[{login}] Пост 3 (текст): {id3}")
    ids.append(id3)
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
#  ПРОЧИЕ ДЕЙСТВИЯ — через SlashThreadsClient
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
        return _cl(account_login).get_thread_replies(post_id)
    except Exception as e:
        logger.warning(f"get_thread_replies({post_id}): {e}")
        return []


def like_thread(post_id: str, account_login: str = None) -> bool:
    try:
        return _cl(account_login).like(post_id)
    except Exception as e:
        logger.warning(f"like_thread({post_id}): {e}")
        return False


def repost_thread(post_id: str, account_login: str = None) -> bool:
    try:
        return _cl(account_login).repost(post_id)
    except Exception as e:
        logger.warning(f"repost_thread({post_id}): {e}")
        return False


def follow_user(user_id: str, account_login: str = None) -> bool:
    try:
        return _cl(account_login).follow(user_id)
    except Exception as e:
        logger.warning(f"follow_user({user_id}): {e}")
        return False


def search_users(query: str, account_login: str = None) -> list:
    try:
        return _cl(account_login).search_users(query)
    except Exception as e:
        logger.warning(f"search_users({query}): {e}")
        return []


def get_user_threads(user_id: str, account_login: str = None) -> list:
    try:
        return _cl(account_login).get_user_threads(user_id)
    except Exception as e:
        logger.warning(f"get_user_threads({user_id}): {e}")
        return []


def get_thread_stats(post_id: str, account_login: str = None) -> dict:
    try:
        return _cl(account_login).get_thread_stats(post_id)
    except Exception as e:
        logger.warning(f"get_thread_stats({post_id}): {e}")
        return {}
