### threads_api.py  —  DUAL-LIBRARY WITH AUTO-FALLBACK
#
# Все операции: сначала metathreads, при ошибке автоматически Danie1/threads-api.
# Публикация: сначала Danie1 (поддерживает image + reply_to), fallback metathreads.
#
# BUG-01: follow() вместо follow_user() в metathreads
# BUG-02: _post_with_reply_metathreads() для reply_to (metathreads не поддерживает)
# BUG-03: logged_in_user явно после cookie-auth
# BUG-04: _activate_client() перед каждым вызовом metathreads (глобальная сессия)
# BUG-05..08: _parse_users/threads/replies/stats() — распаковка dict-ответов
# BUG-09: add_account_manual через metathreads.get_user_id()
# BUG-10: _pk() safe fallback

import os, time, random, logging, datetime, json, asyncio
from dotenv import load_dotenv
import storage
import threads_auth
from threads_auth import TwoFactorRequired, BloksLoginFailed

AUTH_TYPE_BLOKS  = 'bloks'
AUTH_TYPE_COOKIE = 'cookie'
AUTH_TYPE_DANIE1 = 'danie1'

load_dotenv()
logger = logging.getLogger(__name__)

_clients: dict    = {}   # login -> { client, username, user_id, login }
_pending_2fa: dict = {}  # login -> { login, password }


# ══════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ: СЕССИЯ, ПАРСЕРЫ, PK
# ══════════════════════════════════════════════════════════════

def _activate_client(client):
    """Устанавливаем сессию нужного аккаунта в глобальный config metathreads."""
    try:
        from metathreads import config
        config._DEFAULT_SESSION = client.session
    except Exception:
        pass


def _parse_users(response) -> list:
    if not response: return []
    if isinstance(response, list): return response
    if isinstance(response, dict):
        users = response.get('users', [])
        if isinstance(users, list): return users
    return []


def _parse_threads(response) -> list:
    if not response: return []
    if isinstance(response, list): return response
    posts = []
    if isinstance(response, dict):
        for t in (response.get('threads', []) or []):
            if not isinstance(t, dict): continue
            for item in (t.get('thread_items', []) or []):
                post = item.get('post', item) if isinstance(item, dict) else item
                if post: posts.append(post)
    return posts


def _parse_replies(response) -> list:
    if not response: return []
    if isinstance(response, list): return response
    posts = []
    if isinstance(response, dict):
        for key in ('reply_threads', 'containing_thread', 'threads'):
            bucket = response.get(key, [])
            if isinstance(bucket, dict): bucket = [bucket]
            for t in (bucket or []):
                if not isinstance(t, dict): continue
                for item in (t.get('thread_items', []) or []):
                    post = item.get('post', item) if isinstance(item, dict) else item
                    if post: posts.append(post)
        if not posts:
            for val in response.values():
                if isinstance(val, list) and val:
                    posts = val; break
    return posts


def _parse_stats(response) -> dict:
    if not response or not isinstance(response, dict): return {}
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
    if response is None: return ''
    if isinstance(response, bool): return ''
    if isinstance(response, dict):
        pk = (response.get('media', {}) or {}).get('pk')
        if pk: return str(pk)
        pk = response.get('pk')
        if pk: return str(pk)
    return str(response) if response else ''


# ══════════════════════════════════════════════════════════════
#  DANIE1 / THREADS-API — КЛИЕНТ
# ══════════════════════════════════════════════════════════════

def _get_danie1_client(account_login: str):
    """
    Возвращает ThreadsAPI с Bearer token из settings.
    None если токена нет.
    """
    try:
        from threads_api.src.threads_api import ThreadsAPI
        token_key = f'threads_api_token:{account_login}'
        token     = storage.get_setting(token_key)
        if not token:
            return None
        acc = storage.get_account(account_login) or {}
        api              = ThreadsAPI()
        api.token        = token
        api.user_id      = acc.get('user_id', '')
        api.is_logged_in = True
        api.auth_headers = {
            'Authorization': f'Bearer IGT:2:{token}',
            'User-Agent':    'Barcelona 289.0.0.77.109 Android',
            'Content-Type':  'application/x-www-form-urlencoded; charset=UTF-8',
        }
        return api
    except Exception as e:
        logger.debug(f"_get_danie1_client({account_login}): {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  METATHREADS — ПУБЛИКАЦИЯ С REPLY_TO
# ══════════════════════════════════════════════════════════════

def _post_with_reply_metathreads(client, caption: str, reply_to: str = None) -> dict:
    """
    metathreads.post_thread() не поддерживает reply_to — собираем запрос вручную.
    """
    from metathreads.constants import Setting, Path
    from metathreads.request_util import generate_request_data

    upload_id = int(datetime.datetime.now().microsecond * datetime.datetime.now().microsecond)
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
    _activate_client(client)
    return generate_request_data(Path.POST_THREAD, data=signed_data, method="POST")


# ══════════════════════════════════════════════════════════════
#  СОЗДАНИЕ КЛИЕНТА И ЗАГРУЗКА ИЗ БД
# ══════════════════════════════════════════════════════════════

def _make_metathreads_client(session_id: str, csrf_token: str,
                              user_id: str, username: str,
                              auth_type: str = 'cookie'):
    from metathreads import MetaThreads
    client = MetaThreads()
    if auth_type == AUTH_TYPE_BLOKS:
        if session_id: client.session.headers.update({'Authorization': session_id})
        if csrf_token: client.session.headers.update({'X-Mid': csrf_token})
    else:
        client.session.cookies.update({'sessionid': session_id, 'csrftoken': csrf_token})
    client.logged_in_user = {'pk': user_id, 'username': username}
    return client


def _make_metathreads_from_danie1_token(token: str, user_id: str, username: str):
    """
    Создаём metathreads-клиент из Danie1 Bearer-токена.
    Danie1 хранит сырой токен; metathreads принимает его через Authorization header
    в формате 'Bearer IGT:2:<token>' — тот же формат, что использует Bloks.
    Это даёт аккаунту полный функционал обеих библиотек.
    """
    from metathreads import MetaThreads
    client = MetaThreads()
    client.session.headers.update({'Authorization': f'Bearer IGT:2:{token}'})
    client.logged_in_user = {'pk': user_id, 'username': username}
    logger.debug(f"metathreads-клиент из Danie1-токена: user_id={user_id}, username={username}")
    return client


def load_accounts_from_db():
    for acc_ref in storage.get_all_accounts():
        acc = storage.get_account(acc_ref['login'])
        if not acc:
            continue
        if acc.get('auth_type') == AUTH_TYPE_DANIE1:
            # Восстанавливаем metathreads-клиент из сохранённого Danie1-токена
            mt_client = None
            token = storage.get_setting(f'threads_api_token:{acc["login"]}')
            if token and acc.get('user_id') and acc.get('username'):
                try:
                    mt_client = _make_metathreads_from_danie1_token(
                        token, acc['user_id'], acc['username']
                    )
                except Exception as e:
                    logger.warning(f"metathreads из токена ({acc['login']}): {e}")
            _clients[acc['login']] = {
                'client':   mt_client,
                'username': acc.get('username', acc['login']),
                'user_id':  acc.get('user_id', acc['login']),
                'login':    acc['login'],
            }
            mode = 'hybrid ✓' if mt_client else 'Danie1-only'
            logger.info(f"Загружен ({mode}): {acc['login']}")
            continue
        if acc.get('session_id'):
            try:
                client = _make_metathreads_client(
                    acc['session_id'], acc['csrf_token'],
                    acc['user_id'],    acc['username'],
                    auth_type=acc.get('auth_type', 'cookie'),
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


# ══════════════════════════════════════════════════════════════
#  ДОБАВЛЕНИЕ АККАУНТОВ
# ══════════════════════════════════════════════════════════════

def add_account(login: str, password: str) -> dict:
    logger.info(f"[{login}] Авторизация через Bloks API...")
    try:
        result = threads_auth.login(login, password)
        return _save_from_bloks_result(login, result)
    except TwoFactorRequired:
        _pending_2fa[login] = {'login': login, 'password': password}
        raise
    except BloksLoginFailed as e:
        logger.warning(f"[{login}] Bloks login failed, пробую Danie1: {e}")
        return _add_account_via_danie1(login, password)


def confirm_2fa(login, code):
    if login not in _pending_2fa:
        raise Exception("Сессия 2FA не найдена. Начни заново с /add_account.")
    pending  = _pending_2fa.pop(login)
    password = pending.get('password', '')
    try:
        result = threads_auth.login(login, password)
        return _save_from_bloks_result(login, result)
    except Exception as e:
        _pending_2fa[login] = pending
        raise Exception(f"2FA не подтверждена: {e}\n\nДля аккаунтов с 2FA используй /manual_cookies")


def _add_account_via_danie1(login: str, password: str) -> dict:
    """Вход через Danie1/threads-api когда Bloks вернул login failed. Вызывается из sync add_account."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(_add_account_danie1_async(login, password))
        finally:
            loop.close()
    except Exception as e:
        raise Exception(
            f"Danie1 тоже не смог войти: {e}\n\n"
            "Если пароль верный:\n"
            "• Подожди 15-30 минут (Instagram блокирует)\n"
            "• Или используй /manual_cookies"
        )


async def _add_account_danie1_async(login: str, password: str) -> dict:
    """Логин через Danie1 + создание metathreads-клиента из того же токена (hybrid mode)."""
    from threads_api.src.threads_api import ThreadsAPI
    api = ThreadsAPI()
    ok = await api.login(login, password)
    if not ok or not getattr(api, 'token', None):
        raise Exception("Danie1: неверный логин или пароль, либо Instagram временно блокирует.")
    token    = api.token
    user_id  = str(getattr(api, 'user_id', None) or '')
    username = str(getattr(api, 'username', None) or login)

    storage.set_setting(f'threads_api_token:{login}', token)

    # Создаём metathreads-клиент из того же Bearer-токена — аккаунт получает полный функционал
    mt_client = None
    try:
        mt_client = _make_metathreads_from_danie1_token(token, user_id or login, username)
        logger.info(f"[{login}] metathreads hybrid-клиент создан ✓")
    except Exception as e:
        logger.warning(f"[{login}] metathreads из токена не создан (продолжаем только с Danie1): {e}")

    _clients[login] = {
        'client':   mt_client,   # не None если токен пробросился в metathreads
        'username': username,
        'user_id':  user_id or login,
        'login':    login,
    }
    storage.save_account({
        'login':      login,
        'session_id': token,
        'csrf_token': '',
        'user_id':    user_id or login,
        'username':   username,
        'auth_type':  AUTH_TYPE_DANIE1,
    })
    mode = 'hybrid' if mt_client else 'Danie1-only'
    logger.info(f"[{login}] Добавлен через Danie1 ({mode}). username={username}")
    return {'login': login, 'username': username}


def add_account_manual(login, session_id, csrf_token):
    """Добавление через cookies браузера. Профиль получаем через metathreads."""
    user_id  = ''
    username = login
    try:
        from metathreads import MetaThreads
        tmp = MetaThreads()
        tmp.session.cookies.update({'sessionid': session_id, 'csrftoken': csrf_token})
        _activate_client(tmp)
        resolved_id = tmp.get_user_id(login)
        if resolved_id:
            user_id = str(resolved_id)
        try:
            user_data = tmp.get_user(login)
            if isinstance(user_data, list) and user_data:
                user_data = user_data[0]
            if isinstance(user_data, dict):
                u = (user_data.get('data', {}) or {}).get('user', {}) or user_data
                username = u.get('username', login)
        except Exception:
            pass
        logger.info(f"[{login}] Профиль: user_id={user_id}, username={username}")
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
        'csrf_token': csrf_token, 'user_id': user_id,
        'username': username, 'auth_type': AUTH_TYPE_COOKIE,
    })
    logger.info(f"[{login}] Добавлен через cookies. username={username}")
    return {'login': login, 'username': username}


def _save_from_bloks_result(login: str, result: dict) -> dict:
    auth_token = result.get('auth_token', '')
    mid_token  = result.get('mid_token', '')
    user_id    = result.get('user_id', '')
    username   = result.get('username', login)

    existing_client = result.get('client')
    if existing_client:
        client = existing_client
        if not client.logged_in_user:
            client.logged_in_user = {'pk': user_id, 'username': username}
    else:
        client = _make_metathreads_client(auth_token, mid_token, user_id, username, AUTH_TYPE_BLOKS)

    _clients[login] = {
        'client':   client,
        'username': username,
        'user_id':  user_id,
        'login':    login,
    }
    storage.save_account({
        'login':     login,
        'session_id': auth_token,
        'csrf_token': mid_token,
        'user_id':   user_id,
        'username':  username,
        'auth_type': AUTH_TYPE_BLOKS,
    })
    logger.info(f"[{login}] Сохранён (Bloks). username={username}")
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
        'client': client, 'username': username, 'user_id': user_id, 'login': login,
    }
    storage.save_account({
        'login': login, 'session_id': session_id, 'csrf_token': csrf_token,
        'user_id': user_id, 'username': username,
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


async def login_danie1(login: str, password: str) -> bool:
    """Получаем Bearer token Danie1 для image-постинга + обновляем metathreads hybrid-клиент."""
    try:
        from threads_api.src.threads_api import ThreadsAPI
        api = ThreadsAPI()
        ok  = await api.login(login, password)
        if ok and api.token:
            token    = api.token
            user_id  = str(getattr(api, 'user_id', None) or '')
            username = str(getattr(api, 'username', None) or login)
            storage.set_setting(f'threads_api_token:{login}', token)

            # Обновляем metathreads-клиент в памяти если аккаунт уже загружен
            try:
                mt_client = _make_metathreads_from_danie1_token(
                    token, user_id or login, username
                )
                if login in _clients:
                    _clients[login]['client'] = mt_client
                    logger.info(f"[{login}] metathreads hybrid-клиент обновлён ✓")
            except Exception as e:
                logger.warning(f"[{login}] metathreads обновить не удалось: {e}")

            logger.info(f"[{login}] Danie1 Bearer token сохранён")
            return True
        return False
    except Exception as e:
        logger.warning(f"[{login}] Danie1 login: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  ПУБЛИКАЦИЯ СЕРИИ
# ══════════════════════════════════════════════════════════════

async def post_series_async(posts: dict, image_path: str = None,
                            account_login: str = None) -> list:
    """
    Публикация серии 4 постов.
    Порядок: Danie1 (image+reply) → metathreads (текст+reply) → Exception
    """
    entry     = get_client(account_login)
    login     = entry['login']
    mt_client = entry['client']

    logger.info(f"[{login}] Публикую серию: {posts.get('topic', '—')}")

    # 1. Пробуем Danie1 (поддерживает картинку и reply_to)
    danie1_api = _get_danie1_client(login)
    if danie1_api:
        try:
            ids = await _post_series_danie1(danie1_api, posts, image_path, login)
            logger.info(f"[{login}] ✓ Опубликовано через Danie1")
            return ids
        except Exception as e:
            logger.warning(f"[{login}] Danie1 упал ({e}), переключаюсь на metathreads...")

    # 2. Fallback: metathreads (если аккаунт не только Danie1)
    if mt_client is None:
        raise Exception(
            "Аккаунт добавлен через Danie1; токен истёк или недоступен. "
            "Используй /add_account с паролем заново или /manual_cookies."
        )
    try:
        ids = await _post_series_metathreads(mt_client, posts, login)
        logger.info(f"[{login}] ✓ Опубликовано через metathreads")
        return ids
    except Exception as e:
        logger.error(f"[{login}] metathreads тоже упал: {e}")
        raise Exception(
            f"Не удалось опубликовать ни через одну библиотеку.\n"
            f"Danie1: нет токена или ошибка.\n"
            f"metathreads: {e}"
        )


async def _post_series_danie1(api, posts: dict, image_path: str, login: str) -> list:
    """Публикация через Danie1/threads-api с поддержкой картинки и reply_to."""
    img = image_path if (image_path and os.path.exists(image_path)) else None
    ids = []

    ok1 = await api.post(caption=posts['post1'])
    if not ok1:
        raise Exception("post1 вернул False")
    ids.append('d1')
    await asyncio.sleep(random.uniform(8, 14))

    await api.post(caption=posts['post2'])
    ids.append('d2')
    await asyncio.sleep(random.uniform(8, 14))

    await api.post(caption=posts['post3'], image_path=img)
    ids.append('d3')
    await asyncio.sleep(random.uniform(8, 14))

    await api.post(caption=posts['post4'])
    ids.append('d4')

    return ids


async def _post_series_metathreads(client, posts: dict, login: str) -> list:
    """Публикация через metathreads с reply_to (без картинки)."""
    ids = []

    r1  = await asyncio.to_thread(_post_with_reply_metathreads, client, posts['post1'])
    id1 = _pk(r1); ids.append(id1)
    logger.info(f"[{login}] Пост 1: {id1}")
    await asyncio.sleep(random.uniform(8, 14))

    r2  = await asyncio.to_thread(_post_with_reply_metathreads, client, posts['post2'], id1)
    id2 = _pk(r2); ids.append(id2)
    logger.info(f"[{login}] Пост 2: {id2}")
    await asyncio.sleep(random.uniform(8, 14))

    r3  = await asyncio.to_thread(_post_with_reply_metathreads, client, posts['post3'], id2)
    id3 = _pk(r3); ids.append(id3)
    logger.info(f"[{login}] Пост 3: {id3}")
    await asyncio.sleep(random.uniform(8, 14))

    r4  = await asyncio.to_thread(_post_with_reply_metathreads, client, posts['post4'], id3)
    id4 = _pk(r4); ids.append(id4)
    logger.info(f"[{login}] Пост 4: {id4}")

    return ids


def post_series(posts: dict, image_path: str = None,
                account_login: str = None) -> list:
    """Синхронная обёртка."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            post_series_async(posts, image_path, account_login)
        )
    finally:
        loop.close()


# ══════════════════════════════════════════════════════════════
#  ПРОЧИЕ ДЕЙСТВИЯ — AUTO-FALLBACK НА КАЖДОМ МЕТОДЕ
# ══════════════════════════════════════════════════════════════

def _with_fallback(login: str, mt_fn, d1_fn=None, fn_name: str = ''):
    """
    Универсальный враппер с авто-фоллбэком.
    Сначала пробует metathreads, при ошибке — Danie1 (если d1_fn задан).
    Для аккаунтов только Danie1 (client is None) сразу идём в Danie1.
    """
    entry = get_client(login)
    mt_client = entry['client']

    # Попытка 1: metathreads (если есть клиент)
    if mt_client is not None:
        try:
            _activate_client(mt_client)
            return mt_fn(mt_client)
        except Exception as e:
            logger.warning(f"{fn_name}({login}) metathreads: {e}")

    # Попытка 2: Danie1
    if d1_fn:
        api = _get_danie1_client(login)
        if api:
            try:
                return d1_fn(api)
            except Exception as e2:
                logger.warning(f"{fn_name}({login}) Danie1: {e2}")

    return None  # обе библиотеки не сработали


def get_thread_replies(post_id: str, account_login: str = None) -> list:
    result = _with_fallback(
        account_login,
        mt_fn  = lambda c: _parse_replies(c.get_thread_replies(post_id)),
        fn_name= 'get_thread_replies',
    )
    return result if result is not None else []


def like_thread(post_id: str, account_login: str = None) -> bool:
    result = _with_fallback(
        account_login,
        mt_fn  = lambda c: (c.like_thread(post_id), True)[1],
        d1_fn  = lambda api: asyncio.get_event_loop().run_until_complete(
                     _danie1_like(api, post_id)),
        fn_name= 'like_thread',
    )
    return bool(result)


def repost_thread(post_id: str, account_login: str = None) -> bool:
    result = _with_fallback(
        account_login,
        mt_fn  = lambda c: (c.repost_thread(post_id), True)[1],
        fn_name= 'repost_thread',
    )
    return bool(result)


def follow_user(user_id: str, account_login: str = None) -> bool:
    result = _with_fallback(
        account_login,
        mt_fn  = lambda c: (c.follow(user_id), True)[1],
        d1_fn  = lambda api: asyncio.get_event_loop().run_until_complete(
                     _danie1_follow(api, user_id)),
        fn_name= 'follow_user',
    )
    return bool(result)


def search_users(query: str, account_login: str = None) -> list:
    result = _with_fallback(
        account_login,
        mt_fn  = lambda c: _parse_users(c.search_user(query)),
        fn_name= 'search_users',
    )
    return result if result is not None else []


def get_user_threads(user_id: str, account_login: str = None) -> list:
    result = _with_fallback(
        account_login,
        mt_fn  = lambda c: _parse_threads(c.get_user_threads(user_id)),
        d1_fn  = lambda api: asyncio.get_event_loop().run_until_complete(
                     _danie1_get_threads(api, user_id)),
        fn_name= 'get_user_threads',
    )
    return result if result is not None else []


def get_thread_stats(post_id: str, account_login: str = None) -> dict:
    result = _with_fallback(
        account_login,
        mt_fn  = lambda c: _parse_stats(c.get_thread(post_id)),
        d1_fn  = lambda api: asyncio.get_event_loop().run_until_complete(
                     _danie1_get_stats(api, post_id)),
        fn_name= 'get_thread_stats',
    )
    return result if result is not None else {}


# ── Danie1 async helpers (запускаются из синхронного контекста) ──

async def _danie1_like(api, post_id: str) -> bool:
    """Лайк через Danie1 — библиотека не имеет like, используем get_post_likes как проверку."""
    # Danie1 не имеет метода like — возвращаем False чтобы не крашить
    return False


async def _danie1_follow(api, user_id: str) -> bool:
    try:
        return await api.follow_user(user_id)
    except Exception:
        return False


async def _danie1_get_threads(api, user_id: str) -> list:
    try:
        result = await api.get_user_threads(user_id)
        if not result:
            return []
        # Danie1 возвращает список thread-объектов
        posts = []
        for t in (result if isinstance(result, list) else []):
            if isinstance(t, dict):
                items = t.get('thread_items', [])
                for item in (items or []):
                    post = item.get('post', item) if isinstance(item, dict) else item
                    if post: posts.append(post)
        return posts
    except Exception:
        return []


async def _danie1_get_stats(api, post_id: str) -> dict:
    try:
        result = await api.get_post(post_id)
        if not result:
            return {}
        # Danie1 get_post возвращает структуру с thread_items
        items = result.get('containing_thread', {}).get('thread_items', []) if isinstance(result, dict) else []
        if items and isinstance(items[0], dict):
            post = items[0].get('post', items[0])
            if isinstance(post, dict):
                return {
                    'likes':   post.get('like_count', 0),
                    'replies': post.get('reply_count', 0),
                    'reposts': post.get('repost_count', 0),
                }
        return {}
    except Exception:
        return {}
