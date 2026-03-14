### threads_api.py  —  FULLY FIXED
#
# BUG-01: follow_user() -> follow() в metathreads
# BUG-02: post_thread() не имеет reply_to/image -> _post_with_reply_metathreads()
# BUG-03: client.me=None после cookie-auth -> явно ставим logged_in_user
# BUG-04: КРИТИЧНЫЙ - config._DEFAULT_SESSION глобальный -> _activate_client() перед каждым вызовом
# BUG-05: search_user() -> dict, не list -> _parse_users()
# BUG-06: get_user_threads() -> dict, не list -> _parse_threads()
# BUG-07: get_thread_replies() -> dict, не list -> _parse_replies()
# BUG-08: get_thread_stats() неправильная вложенность -> _parse_stats()
# BUG-09: add_account_manual вызывал client.me когда logged_in_user=None
# BUG-10: _pk() падал если response не dict

import os, time, random, logging, datetime, json, asyncio
from dotenv import load_dotenv
import storage
import threads_auth
from threads_auth import TwoFactorRequired

load_dotenv()
logger = logging.getLogger(__name__)

_clients: dict = {}       # login -> { client, username, user_id, login }
_pending_2fa: dict = {}   # login -> { login, password }


# ─── BUG-04 FIX: изоляция сессий между аккаунтами ──────────────────────────

def _activate_client(client):
    """
    metathreads использует ГЛОБАЛЬНЫЙ config._DEFAULT_SESSION.
    При нескольких аккаунтах все запросы идут через последний созданный клиент.
    Вызываем перед КАЖДЫМ API-вызовом чтобы установить правильную сессию.
    """
    try:
        from metathreads import config
        config._DEFAULT_SESSION = client.session
    except Exception:
        pass


# ─── Парсеры ответов metathreads ────────────────────────────────────────────

def _parse_users(response) -> list:
    """BUG-05 FIX: search_user() возвращает {'users':[...]}, не list."""
    if not response:
        return []
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        users = response.get('users', [])
        if isinstance(users, list):
            return users
    return []


def _parse_threads(response) -> list:
    """BUG-06 FIX: get_user_threads() возвращает dict с вложенными thread_items."""
    if not response:
        return []
    if isinstance(response, list):
        return response
    posts = []
    if isinstance(response, dict):
        for t in (response.get('threads', []) or []):
            if not isinstance(t, dict):
                continue
            for item in (t.get('thread_items', []) or []):
                post = item.get('post', item) if isinstance(item, dict) else item
                if post:
                    posts.append(post)
    return posts


def _parse_replies(response) -> list:
    """BUG-07 FIX: get_thread_replies() возвращает dict с reply_threads."""
    if not response:
        return []
    if isinstance(response, list):
        return response
    posts = []
    if isinstance(response, dict):
        for key in ('reply_threads', 'containing_thread', 'threads'):
            bucket = response.get(key, [])
            if isinstance(bucket, dict):
                bucket = [bucket]
            for t in (bucket or []):
                if not isinstance(t, dict):
                    continue
                for item in (t.get('thread_items', []) or []):
                    post = item.get('post', item) if isinstance(item, dict) else item
                    if post:
                        posts.append(post)
        if not posts:
            for val in response.values():
                if isinstance(val, list) and val:
                    posts = val
                    break
    return posts


def _parse_stats(response) -> dict:
    """BUG-08 FIX: статистика в containing_thread.thread_items[0].post."""
    if not response or not isinstance(response, dict):
        return {}
    ct = response.get('containing_thread', {})
    if isinstance(ct, dict):
        items = ct.get('thread_items', [])
        if items and isinstance(items[0], dict):
            post = items[0].get('post', items[0])
            if isinstance(post, dict):
                return {
                    'likes':   post.get('like_count', 0),
                    'replies': (post.get('reply_count', 0)
                                or post.get('text_post_app_info', {}).get('reply_count', 0)),
                    'reposts': post.get('repost_count', 0),
                }
    return {
        'likes':   response.get('like_count', 0),
        'replies': response.get('reply_count', 0),
        'reposts': response.get('repost_count', 0),
    }


def _pk(response) -> str:
    """BUG-10 FIX: безопасное извлечение post PK из любого формата ответа."""
    if response is None:
        return ''
    if isinstance(response, bool):
        return ''   # threads-api (Danie1) возвращает True при успехе
    if isinstance(response, dict):
        pk = (response.get('media', {}) or {}).get('pk')
        if pk:
            return str(pk)
        pk = response.get('pk')
        if pk:
            return str(pk)
    return str(response) if response else ''


# ─── BUG-02 FIX: публикация с reply_to через прямой запрос metathreads ──────

def _post_with_reply_metathreads(client, caption: str, reply_to: str = None) -> dict:
    """
    metathreads.post_thread() НЕ поддерживает reply_to и image.
    Собираем запрос вручную через внутренние модули библиотеки.
    """
    from metathreads.constants import Setting, Path
    from metathreads.request_util import generate_request_data

    upload_id = int(
        datetime.datetime.now().microsecond * datetime.datetime.now().microsecond
    )
    text_post_info: dict = {"reply_control": 0}
    if reply_to:
        text_post_info["reply_id"] = str(reply_to)

    data = {
        "publish_mode": "text_post",
        "upload_id": upload_id,
        "text_post_app_info": text_post_info,
        "timezone_offset": 0,
        "_uid": client.user_id,
        "device_id": Setting.ANDROID_ID,
        "_uuid": Setting.DEVICE_ID,
        "caption": caption,
        "audience": "default",
    }
    signed_data = {"signed_body": f"SIGNATURE.{json.dumps(data)}"}
    _activate_client(client)   # BUG-04 FIX
    return generate_request_data(Path.POST_THREAD, data=signed_data, method="POST")


# ─── Создание клиента ────────────────────────────────────────────────────────

def _make_metathreads_client(session_id: str, csrf_token: str,
                              user_id: str, username: str):
    """BUG-03 FIX: явно выставляем logged_in_user после cookie-авторизации."""
    from metathreads import MetaThreads
    client = MetaThreads()
    client.session.cookies.update({'sessionid': session_id, 'csrftoken': csrf_token})
    client.logged_in_user = {'pk': user_id, 'username': username}
    return client


# ─── Загрузка из БД ─────────────────────────────────────────────────────────

def load_accounts_from_db():
    for acc_ref in storage.get_all_accounts():
        acc = storage.get_account(acc_ref['login'])
        if acc and acc.get('session_id'):
            try:
                client = _make_metathreads_client(
                    acc['session_id'], acc['csrf_token'],
                    acc['user_id'],    acc['username'],
                )
                _clients[acc['login']] = {
                    'client':   client,
                    'username': acc['username'],
                    'user_id':  acc['user_id'],
                    'login':    acc['login'],
                }
                logger.info(f"Загружен: {acc['login']}")
            except Exception as e:
                logger.warning(f"Не удалось загрузить {acc['login']}: {e}")
    logger.info(f"Загружено аккаунтов: {len(_clients)}")


# ─── Добавление аккаунтов ────────────────────────────────────────────────────

def add_account(login, password):
    logger.info(f"[{login}] Авторизация через threads_auth...")
    try:
        result = threads_auth.login(login, password)
        return _save_from_result(login, result)
    except TwoFactorRequired:
        _pending_2fa[login] = {'login': login, 'password': password}
        raise


def confirm_2fa(login, code):
    if login not in _pending_2fa:
        raise Exception("Сессия 2FA не найдена. Начни заново с /add_account.")
    pending  = _pending_2fa.pop(login)
    password = pending.get('password', '')
    try:
        from metathreads import MetaThreads
        client = MetaThreads()
        client.login(login, password)
        return _save_from_client_obj(login, client)
    except Exception as e:
        _pending_2fa[login] = pending
        raise Exception(f"Не удалось подтвердить 2FA: {e}. Попробуй /manual_cookies")


def add_account_manual(login, session_id, csrf_token):
    """
    BUG-09 FIX: не вызываем client.me (logged_in_user=None после cookie-auth).
    Получаем профиль через отдельный GET-запрос к web API Instagram.
    """
    user_id  = ''
    username = login
    try:
        import requests as _req
        resp = _req.get(
            'https://www.instagram.com/api/v1/users/web_profile_info/',
            params={'username': login},
            headers={
                'x-ig-app-id': '936619743392459',
                'Cookie': f'sessionid={session_id}; csrftoken={csrf_token}',
                'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                               'AppleWebKit/537.36 Chrome/114.0.0.0 Safari/537.36'),
            },
            timeout=15,
        )
        if resp.status_code == 200:
            d = resp.json()
            u = ((d.get('data') or {}).get('user') or {})
            user_id  = str(u.get('id') or u.get('pk', ''))
            username = u.get('username', login)
            logger.info(f"[{login}] Профиль получен: username={username}")
    except Exception as e:
        logger.warning(f"[{login}] Профиль не получен (продолжаем): {e}")

    client = _make_metathreads_client(session_id, csrf_token, user_id, username)
    _clients[login] = {
        'client':   client,
        'username': username,
        'user_id':  user_id,
        'login':    login,
    }
    storage.save_account({
        'login': login, 'session_id': session_id,
        'csrf_token': csrf_token, 'user_id': user_id, 'username': username,
    })
    logger.info(f"[{login}] Добавлен вручную. username={username}")
    return {'login': login, 'username': username}


def _save_from_result(login, result):
    client = _make_metathreads_client(
        result['session_id'], result['csrf_token'],
        result['user_id'],    result['username'],
    )
    _clients[login] = {
        'client':   client,
        'username': result['username'],
        'user_id':  result['user_id'],
        'login':    login,
    }
    storage.save_account({'login': login, **result})
    return {'login': login, 'username': result['username']}


def _save_from_client_obj(login, client):
    cookies    = client.session.cookies.get_dict()
    session_id = cookies.get('sessionid', '')
    csrf_token = cookies.get('csrftoken', '')
    me         = client.me or {}
    user_id    = str(me.get('pk') or me.get('id', ''))
    username   = me.get('username', login)
    if not client.logged_in_user:
        client.logged_in_user = {'pk': user_id, 'username': username}
    _clients[login] = {
        'client':   client,
        'username': username,
        'user_id':  user_id,
        'login':    login,
    }
    storage.save_account({
        'login': login, 'session_id': session_id,
        'csrf_token': csrf_token, 'user_id': user_id, 'username': username,
    })
    return {'login': login, 'username': username}


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


# ─── Публикация серии ────────────────────────────────────────────────────────

async def post_series_async(posts: dict, image_path: str = None,
                            account_login: str = None) -> list:
    """
    Асинхронная публикация серии из 4 постов-ответов.
    Приоритет: Danie1/threads-api (поддерживает image+reply) -> metathreads fallback.
    BUG-02 FIX: metathreads.post_thread() не имеет reply_to -> _post_with_reply_metathreads()
    BUG-04 FIX: _activate_client() перед каждым вызовом metathreads
    """
    entry     = get_client(account_login)
    login     = entry['login']
    mt_client = entry['client']
    acc       = storage.get_account(login)

    logger.info(f"[{login}] Публикую серию: {posts.get('topic', '—')}")

    danie1_ids = await _try_danie1_post_series(acc, posts, image_path, login)
    if danie1_ids is not None:
        return danie1_ids

    # Fallback: metathreads (без картинки)
    logger.info(f"[{login}] Публикую через metathreads (без картинки)")
    ids = []

    r1  = await asyncio.to_thread(_post_with_reply_metathreads, mt_client, posts['post1'])
    id1 = _pk(r1); ids.append(id1)
    logger.info(f"[{login}] Пост 1: {id1}")
    await asyncio.sleep(random.uniform(8, 14))

    r2  = await asyncio.to_thread(_post_with_reply_metathreads, mt_client, posts['post2'], id1)
    id2 = _pk(r2); ids.append(id2)
    logger.info(f"[{login}] Пост 2: {id2}")
    await asyncio.sleep(random.uniform(8, 14))

    r3  = await asyncio.to_thread(_post_with_reply_metathreads, mt_client, posts['post3'], id2)
    id3 = _pk(r3); ids.append(id3)
    logger.info(f"[{login}] Пост 3: {id3}")
    await asyncio.sleep(random.uniform(8, 14))

    r4  = await asyncio.to_thread(_post_with_reply_metathreads, mt_client, posts['post4'], id3)
    id4 = _pk(r4); ids.append(id4)
    logger.info(f"[{login}] Пост 4: {id4}")

    return ids


def post_series(posts: dict, image_path: str = None,
                account_login: str = None) -> list:
    """Синхронная обёртка для совместимости."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            post_series_async(posts, image_path, account_login)
        )
    finally:
        loop.close()


async def _try_danie1_post_series(acc: dict, posts: dict,
                                   image_path: str, login: str):
    """
    Публикация через Danie1/threads-api.
    Возвращает список ids если успех, None если нет токена/ошибка.
    """
    try:
        from threads_api.src.threads_api import ThreadsAPI

        token_key    = f'threads_api_token:{login}'
        cached_token = storage.get_setting(token_key)
        if not cached_token:
            return None

        api              = ThreadsAPI()
        api.token        = cached_token
        api.user_id      = acc.get('user_id', '') if acc else ''
        api.is_logged_in = True
        api.auth_headers = {
            'Authorization': f'Bearer IGT:2:{cached_token}',
            'User-Agent':    'Barcelona 289.0.0.77.109 Android',
            'Content-Type':  'application/x-www-form-urlencoded; charset=UTF-8',
        }

        img = image_path if (image_path and os.path.exists(image_path)) else None
        ids = []

        ok1 = await api.post(caption=posts['post1'])
        if not ok1:
            logger.warning(f"[{login}] Danie1 post1 вернул False")
            return None
        ids.append('danie1_1')
        await asyncio.sleep(random.uniform(8, 14))

        await api.post(caption=posts['post2'])
        ids.append('danie1_2')
        await asyncio.sleep(random.uniform(8, 14))

        await api.post(caption=posts['post3'], image_path=img)
        ids.append('danie1_3')
        await asyncio.sleep(random.uniform(8, 14))

        await api.post(caption=posts['post4'])
        ids.append('danie1_4')

        logger.info(f"[{login}] Серия опубликована через Danie1/threads-api ✓")
        return ids

    except Exception as e:
        logger.warning(f"[{login}] Danie1/threads-api: {e}")
        return None


async def login_danie1(login: str, password: str) -> bool:
    """
    Авторизация через Danie1/threads-api для получения Bearer token.
    Вызывается при добавлении аккаунта — сохраняет токен в settings.
    """
    try:
        from threads_api.src.threads_api import ThreadsAPI
        api = ThreadsAPI()
        ok  = await api.login(login, password)
        if ok and api.token:
            storage.set_setting(f'threads_api_token:{login}', api.token)
            logger.info(f"[{login}] Danie1 Bearer token сохранён")
            return True
        return False
    except Exception as e:
        logger.warning(f"[{login}] Danie1 login: {e}")
        return False


# ─── Прочие API-обёртки ──────────────────────────────────────────────────────

def get_thread_replies(post_id: str, account_login: str = None) -> list:
    try:
        entry = get_client(account_login)
        _activate_client(entry['client'])        # BUG-04 FIX
        raw = entry['client'].get_thread_replies(post_id)
        return _parse_replies(raw)               # BUG-07 FIX
    except Exception as e:
        logger.warning(f"get_thread_replies({post_id}): {e}")
        return []


def like_thread(post_id: str, account_login: str = None) -> bool:
    try:
        entry = get_client(account_login)
        _activate_client(entry['client'])        # BUG-04 FIX
        entry['client'].like_thread(post_id)
        return True
    except Exception as e:
        logger.warning(f"like_thread({post_id}): {e}")
        return False


def repost_thread(post_id: str, account_login: str = None) -> bool:
    try:
        entry = get_client(account_login)
        _activate_client(entry['client'])        # BUG-04 FIX
        entry['client'].repost_thread(post_id)
        return True
    except Exception as e:
        logger.warning(f"repost_thread({post_id}): {e}")
        return False


def follow_user(user_id: str, account_login: str = None) -> bool:
    try:
        entry = get_client(account_login)
        _activate_client(entry['client'])        # BUG-04 FIX
        entry['client'].follow(user_id)          # BUG-01 FIX: follow(), не follow_user()
        return True
    except Exception as e:
        logger.warning(f"follow_user({user_id}): {e}")
        return False


def search_users(query: str, account_login: str = None) -> list:
    try:
        entry = get_client(account_login)
        _activate_client(entry['client'])        # BUG-04 FIX
        raw = entry['client'].search_user(query)
        return _parse_users(raw)                 # BUG-05 FIX
    except Exception as e:
        logger.warning(f"search_users({query!r}): {e}")
        return []


def get_user_threads(user_id: str, account_login: str = None) -> list:
    try:
        entry = get_client(account_login)
        _activate_client(entry['client'])        # BUG-04 FIX
        raw = entry['client'].get_user_threads(user_id)
        return _parse_threads(raw)               # BUG-06 FIX
    except Exception as e:
        logger.warning(f"get_user_threads({user_id}): {e}")
        return []


def get_thread_stats(post_id: str, account_login: str = None) -> dict:
    try:
        entry = get_client(account_login)
        _activate_client(entry['client'])        # BUG-04 FIX
        raw = entry['client'].get_thread(post_id)
        return _parse_stats(raw)                 # BUG-08 FIX
    except Exception as e:
        logger.warning(f"get_thread_stats({post_id}): {e}")
        return {}
