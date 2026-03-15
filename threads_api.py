### threads_api.py — instagrapi auth + metathreads operations
#
# Auth: instagrapi (sessionid, password+2FA, TOTP, session file)
# Threads operations: metathreads (like, follow, search, repost, replies, stats)
# Image posting: instagrapi private API → Threads configure endpoint
#
# AUTH_TYPE_INSTAGRAPI — новый тип (заменяет danie1)
# AUTH_TYPE_COOKIE     — ручной ввод cookies (manual_cookies)
# AUTH_TYPE_BLOKS      — legacy metathreads bloks (fallback)

import os, time, random, logging, datetime, json, asyncio
from dotenv import load_dotenv
import storage
import threads_auth
from threads_auth import TwoFactorRequired, LoginFailed

load_dotenv()
logger = logging.getLogger(__name__)

AUTH_TYPE_INSTAGRAPI = 'instagrapi'
AUTH_TYPE_COOKIE     = 'cookie'
AUTH_TYPE_BLOKS      = 'bloks'

# login → { client, ig_client, username, user_id, login }
_clients: dict     = {}
# login → { login, password, ig_client } — ожидание кода 2FA
_pending_2fa: dict = {}


# ══════════════════════════════════════════════════════════════
#  METATHREADS — ВСПОМОГАТЕЛЬНЫЕ
# ══════════════════════════════════════════════════════════════

def _activate_client(mt_client):
    try:
        from metathreads import config
        config._DEFAULT_SESSION = mt_client.session
    except Exception:
        pass


def _parse_users(resp) -> list:
    if not resp: return []
    if isinstance(resp, list): return resp
    if isinstance(resp, dict):
        u = resp.get('users', [])
        if isinstance(u, list): return u
    return []


def _parse_threads(resp) -> list:
    if not resp: return []
    if isinstance(resp, list): return resp
    posts = []
    if isinstance(resp, dict):
        for t in (resp.get('threads', []) or []):
            if not isinstance(t, dict): continue
            for item in (t.get('thread_items', []) or []):
                post = item.get('post', item) if isinstance(item, dict) else item
                if post: posts.append(post)
    return posts


def _parse_replies(resp) -> list:
    if not resp: return []
    if isinstance(resp, list): return resp
    posts = []
    if isinstance(resp, dict):
        for key in ('reply_threads', 'containing_thread', 'threads'):
            bucket = resp.get(key, [])
            if isinstance(bucket, dict): bucket = [bucket]
            for t in (bucket or []):
                if not isinstance(t, dict): continue
                for item in (t.get('thread_items', []) or []):
                    post = item.get('post', item) if isinstance(item, dict) else item
                    if post: posts.append(post)
        if not posts:
            for val in resp.values():
                if isinstance(val, list) and val:
                    posts = val; break
    return posts


def _parse_stats(resp) -> dict:
    if not resp or not isinstance(resp, dict): return {}
    ct = resp.get('containing_thread', {})
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
        'likes':   resp.get('like_count', 0),
        'replies': resp.get('reply_count', 0),
        'reposts': resp.get('repost_count', 0),
    }


def _pk(resp) -> str:
    if resp is None: return ''
    if isinstance(resp, bool): return ''
    if isinstance(resp, dict):
        pk = (resp.get('media', {}) or {}).get('pk')
        if pk: return str(pk)
        pk = resp.get('pk')
        if pk: return str(pk)
    return str(resp) if resp else ''


# ══════════════════════════════════════════════════════════════
#  METATHREADS — СОЗДАНИЕ КЛИЕНТА
# ══════════════════════════════════════════════════════════════

def _make_metathreads_client(session_id: str, csrf_token: str,
                              user_id: str, username: str,
                              auth_type: str = 'cookie'):
    from metathreads import MetaThreads
    cl = MetaThreads()
    if auth_type == AUTH_TYPE_BLOKS:
        if session_id: cl.session.headers.update({'Authorization': session_id})
        if csrf_token: cl.session.headers.update({'X-Mid': csrf_token})
    else:
        cl.session.cookies.update({'sessionid': session_id, 'csrftoken': csrf_token})
    cl.logged_in_user = {'pk': user_id, 'username': username}
    return cl


def _make_metathreads_from_token(bearer_token: str, user_id: str, username: str):
    """Создаём metathreads-клиент из Bearer-токена (instagrapi или Bloks)."""
    from metathreads import MetaThreads
    cl = MetaThreads()
    if bearer_token:
        cl.session.headers.update({'Authorization': bearer_token})
    cl.logged_in_user = {'pk': user_id, 'username': username}
    return cl


def _make_metathreads_from_sessionid(sessionid: str, csrftoken: str,
                                      user_id: str, username: str):
    """Создаём metathreads-клиент из sessionid cookie."""
    from metathreads import MetaThreads
    cl = MetaThreads()
    cl.session.cookies.update({'sessionid': sessionid, 'csrftoken': csrftoken})
    cl.logged_in_user = {'pk': user_id, 'username': username}
    return cl


# ══════════════════════════════════════════════════════════════
#  INSTAGRAPI — ПОЛУЧИТЬ КЛИЕНТ ИЗ ХРАНИЛИЩА
# ══════════════════════════════════════════════════════════════

def _get_ig_client(login: str):
    """
    Возвращает живой instagrapi Client для аккаунта.
    Сначала смотрим в памяти (_clients[login]['ig_client']),
    потом пробуем восстановить из session-файла.
    """
    entry = _clients.get(login, {})
    ig = entry.get('ig_client')
    if ig:
        return ig

    # Пробуем восстановить из session-файла
    session_file = threads_auth._session_path(login)
    if not os.path.exists(session_file):
        return None

    try:
        from instagrapi import Client
        cl = Client()
        cl.delay_range = [1, 3]
        cl.set_settings(cl.load_settings(session_file))
        # Обновляем в памяти
        if login in _clients:
            _clients[login]['ig_client'] = cl
        logger.info(f"[{login}] instagrapi восстановлен из session-файла ✓")
        return cl
    except Exception as e:
        logger.warning(f"[{login}] instagrapi не восстановлен: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  IMAGE POSTING — instagrapi private API → Threads endpoint
# ══════════════════════════════════════════════════════════════

def _post_image_to_threads(ig_cl, caption: str, image_path: str,
                            reply_to: str = None) -> str:
    """
    Публикует тред с изображением через instagrapi private session.
    Использует Instagram rupload endpoint + Threads configure endpoint.
    Возвращает media_pk (строка) или '' при ошибке.
    """
    upload_id = str(int(time.time() * 1000))

    # Step 1: загружаем фото
    with open(image_path, 'rb') as f:
        photo_data = f.read()

    rupload_params = json.dumps({
        'upload_id': upload_id,
        'media_type': 1,
        'retry_context': json.dumps({
            'num_reupload': 0, 'num_step_auto_retry': 0, 'num_step_manual_retry': 0
        }),
    })

    r = ig_cl.private.post(
        f'https://i.instagram.com/rupload_igphoto/{upload_id}',
        data=photo_data,
        headers={
            'X-Entity-Type': 'image/jpeg',
            'Offset': '0',
            'X-Instagram-Rupload-Params': rupload_params,
            'X-Entity-Name': f'fb_uploader_{upload_id}',
            'X-Entity-Length': str(len(photo_data)),
            'Content-Type': 'application/octet-stream',
        }
    )
    r.raise_for_status()
    logger.debug(f"Photo uploaded: {r.json()}")

    # Step 2: конфигурируем как Threads пост
    text_post_info: dict = {'reply_control': 0}
    if reply_to:
        text_post_info['reply_id'] = str(reply_to)

    data = {
        'caption':           caption,
        'upload_id':         upload_id,
        'publish_mode':      'media_post',
        'text_post_app_info': json.dumps(text_post_info),
        'timezone_offset':   '0',
        'audience':          'default',
        '_uid':              str(ig_cl.user_id),
        '_uuid':             ig_cl.uuid,
        'device_id':         ig_cl.android_id,
    }
    signed = {'signed_body': f'SIGNATURE.{json.dumps(data)}'}

    r2 = ig_cl.private.post(
        'https://www.threads.net/api/v1/media/configure_text_post_app_feed/',
        data=signed,
    )
    r2.raise_for_status()
    result = r2.json()
    return str((result.get('media') or {}).get('pk', ''))


def _ig_post_text(ig_cl, caption: str, reply_to: str = None) -> str:
    """Текстовый пост в Threads через instagrapi private API (fallback когда metathreads недоступен)."""
    text_post_info: dict = {'reply_control': 0}
    if reply_to:
        text_post_info['reply_id'] = str(reply_to)

    upload_id = str(int(time.time() * 1000))
    data = {
        'publish_mode':       'text_post',
        'upload_id':          upload_id,
        'text_post_app_info': json.dumps(text_post_info),
        'timezone_offset':    '0',
        'caption':            caption,
        'audience':           'default',
        '_uid':               str(ig_cl.user_id),
        '_uuid':              ig_cl.uuid,
        'device_id':          ig_cl.android_id,
    }
    signed = {'signed_body': f'SIGNATURE.{json.dumps(data)}'}
    r = ig_cl.private.post(
        'https://www.threads.net/api/v1/media/configure_text_post_app_feed/',
        data=signed,
    )
    r.raise_for_status()
    result = r.json()
    return str((result.get('media') or {}).get('pk', ''))



# ══════════════════════════════════════════════════════════════
#  METATHREADS — ПУБЛИКАЦИЯ С REPLY_TO (текст)
# ══════════════════════════════════════════════════════════════

def _post_with_reply_metathreads(client, caption: str, reply_to: str = None) -> dict:
    from metathreads.constants import Path
    from metathreads.request_util import generate_request_data

    # user_id берём из logged_in_user — у MetaThreads нет прямого атрибута user_id
    user = client.logged_in_user or {}
    uid  = str(user.get('pk') or user.get('id') or '')

    # Setting.ANDROID_ID/DEVICE_ID могут отсутствовать в старых версиях metathreads
    try:
        from metathreads.constants import Setting
        android_id = getattr(Setting, 'ANDROID_ID', 'android-' + uid[:8])
        device_uuid = getattr(Setting, 'DEVICE_ID', uid or 'device-uuid')
    except Exception:
        android_id  = 'android-' + uid[:8]
        device_uuid = uid or 'device-uuid'

    upload_id = int(datetime.datetime.now().timestamp() * 1000) % (10 ** 9)
    text_post_info: dict = {'reply_control': 0}
    if reply_to:
        text_post_info['reply_id'] = str(reply_to)

    data = {
        'publish_mode':      'text_post',
        'upload_id':         upload_id,
        'text_post_app_info': text_post_info,
        'timezone_offset':   0,
        '_uid':              uid,
        'device_id':         android_id,
        '_uuid':             device_uuid,
        'caption':           caption,
        'audience':          'default',
    }
    signed_data = {'signed_body': f'SIGNATURE.{json.dumps(data)}'}
    _activate_client(client)
    return generate_request_data(Path.POST_THREAD, data=signed_data, method='POST')


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

        mt_client = None
        ig_client  = None

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

        # Строим metathreads-клиент
        try:
            sid = acc.get('session_id', '')
            csrf = acc.get('csrf_token', '')

            if auth == AUTH_TYPE_INSTAGRAPI and ig_client:
                # Из Bearer-токена instagrapi
                bearer = threads_auth._extract_bearer(ig_client)
                mt_client = _make_metathreads_from_token(bearer, user_id, username)
            elif auth == AUTH_TYPE_BLOKS and sid:
                mt_client = _make_metathreads_client(sid, csrf, user_id, username, AUTH_TYPE_BLOKS)
            elif sid:
                mt_client = _make_metathreads_client(sid, csrf, user_id, username, AUTH_TYPE_COOKIE)
        except Exception as e:
            logger.warning(f"[{login}] metathreads клиент: {e}")

        _clients[login] = {
            'client':    mt_client,
            'ig_client': ig_client,
            'username':  username,
            'user_id':   user_id,
            'login':     login,
        }

        has_mt = mt_client is not None
        has_ig = ig_client is not None
        mode = 'full' if (has_mt and has_ig) else ('mt-only' if has_mt else ('ig-only' if has_ig else 'empty'))
        logger.info(f"Загружен [{mode}]: {login}")

    logger.info(f"Загружено аккаунтов: {len(_clients)}")


# ══════════════════════════════════════════════════════════════
#  ДОБАВЛЕНИЕ АККАУНТОВ
# ══════════════════════════════════════════════════════════════

def add_account(login: str, password: str) -> dict:
    """
    Логин через instagrapi.
    При 2FA — raise TwoFactorRequired (pending_2fa сохраняет password + ig_client).
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
    """Завершение 2FA. Восстанавливает pending из памяти или предлагает повтор."""
    if login not in _pending_2fa:
        saved = storage.get_pending_2fa(login)
        if saved:
            logger.info(f"[{login}] pending_2fa восстановлен из БД")
            _pending_2fa[login] = {
                'login':     saved['login'],
                'password':  saved['password'],
                'ig_client': None,  # потерян при рестарте
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
        # Рестарт бота — нужен новый логин чтобы получить свежую 2FA сессию
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

    # Сначала пробуем instagrapi login_by_sessionid
    ig_client = None
    try:
        result = threads_auth.login_by_sessionid(login, session_id, csrf_token)
        user_id   = result.get('user_id', '')
        username  = result.get('username', login)
        ig_client = result.get('client')
    except Exception as e:
        logger.warning(f"[{login}] instagrapi by sessionid: {e}")

    # Fallback: metathreads для получения user_id/username
    if not user_id:
        try:
            from metathreads import MetaThreads
            tmp = MetaThreads()
            tmp.session.cookies.update({'sessionid': session_id, 'csrftoken': csrf_token})
            _activate_client(tmp)
            resolved = tmp.get_user_id(login)
            if resolved:
                user_id = str(resolved)
            user_data = tmp.get_user(login)
            if isinstance(user_data, list) and user_data:
                user_data = user_data[0]
            if isinstance(user_data, dict):
                u = (user_data.get('data', {}) or {}).get('user', {}) or user_data
                username = u.get('username', login)
        except Exception as e:
            logger.warning(f"[{login}] metathreads profile: {e}")

    mt_client = _make_metathreads_client(session_id, csrf_token, user_id, username)
    _clients[login] = {
        'client':    mt_client,
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
    bearer    = result.get('auth_token', '')

    # metathreads: сначала Bearer, fallback на sessionid cookie
    mt_client = None
    if bearer:
        try:
            mt_client = _make_metathreads_from_token(bearer, user_id, username)
            logger.info(f"[{login}] metathreads via Bearer ✓")
        except Exception as e:
            logger.warning(f"[{login}] metathreads Bearer: {e}")
    if mt_client is None and sessionid:
        try:
            mt_client = _make_metathreads_from_sessionid(sessionid, csrftoken, user_id, username)
            logger.info(f"[{login}] metathreads via sessionid ✓")
        except Exception as e:
            logger.warning(f"[{login}] metathreads sessionid: {e}")

    _clients[login] = {
        'client':    mt_client,
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
#  ОБНОВЛЕНИЕ ТОКЕНА (кнопка "🔄 Обновить токен")
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
    Публикует 4 поста.
    Приоритет: metathreads (reply_to цепочка) → fallback: instagrapi private API.
    Картинка: instagrapi → fallback текст.
    """
    entry     = get_client(account_login)
    login     = entry['login']
    mt_client = entry['client']
    ig_client = entry.get('ig_client') or _get_ig_client(login)

    # Нужен хотя бы один из клиентов
    if not mt_client and not ig_client:
        raise Exception(
            f"Аккаунт {login} не инициализирован.\n"
            "Используй /add_account или /manual_cookies."
        )

    logger.info(f"[{login}] Публикую серию: {posts.get('topic', '—')}")
    img = image_path if (image_path and os.path.exists(image_path)) else None

    async def _post_text(caption: str, reply_to: str = None) -> str:
        """Публикует текстовый пост: metathreads → fallback instagrapi."""
        mt_err = ig_err = None
        if mt_client:
            try:
                r = await asyncio.to_thread(
                    _post_with_reply_metathreads, mt_client, caption, reply_to
                )
                pk = _pk(r)
                if pk:
                    return pk
                logger.warning(f"[{login}] metathreads вернул пустой pk, r={r}")
            except Exception as e:
                mt_err = str(e)
                logger.warning(f"[{login}] metathreads text post: {e}", exc_info=True)
        # fallback: instagrapi
        if ig_client:
            try:
                pk = await asyncio.to_thread(
                    _ig_post_text, ig_client, caption, reply_to
                )
                if pk:
                    return pk
                logger.warning(f"[{login}] instagrapi вернул пустой pk")
            except Exception as e:
                ig_err = str(e)
                logger.warning(f"[{login}] instagrapi text post: {e}", exc_info=True)
        raise Exception(
            f"Не удалось опубликовать пост (оба метода упали). "
            f"metathreads: {mt_err}. instagrapi: {ig_err}"
        )

    ids = []

    id1 = await _post_text(posts['post1'])
    ids.append(id1)
    logger.info(f"[{login}] Пост 1: {id1}")
    await asyncio.sleep(random.uniform(8, 14))

    id2 = await _post_text(posts['post2'], id1)
    ids.append(id2)
    logger.info(f"[{login}] Пост 2: {id2}")
    await asyncio.sleep(random.uniform(8, 14))

    # Пост 3 — с картинкой если есть
    id3 = ''
    if img and ig_client:
        try:
            id3 = await asyncio.to_thread(
                _post_image_to_threads, ig_client, posts['post3'], img, id2
            )
            logger.info(f"[{login}] Пост 3 (image): {id3}")
        except Exception as e:
            logger.warning(f"[{login}] Image posting: {e}, публикую текст...")
    if not id3:
        id3 = await _post_text(posts['post3'], id2)
        logger.info(f"[{login}] Пост 3 (текст): {id3}")
    ids.append(id3)
    await asyncio.sleep(random.uniform(8, 14))

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
#  ПРОЧИЕ ДЕЙСТВИЯ — METATHREADS + FALLBACK
# ══════════════════════════════════════════════════════════════

def _mt(login: str, fn, fn_name: str = ''):
    """Вызов metathreads с активацией сессии нужного аккаунта."""
    entry = get_client(login)
    mt    = entry['client']
    if not mt:
        logger.warning(f"{fn_name}({login}): нет metathreads-клиента")
        return None
    try:
        _activate_client(mt)
        return fn(mt)
    except Exception as e:
        logger.warning(f"{fn_name}({login}): {e}")
        return None


def get_thread_replies(post_id: str, account_login: str = None) -> list:
    r = _mt(account_login, lambda c: _parse_replies(c.get_thread_replies(post_id)),
            'get_thread_replies')
    return r or []


def like_thread(post_id: str, account_login: str = None) -> bool:
    r = _mt(account_login, lambda c: (c.like_thread(post_id), True)[1], 'like_thread')
    return bool(r)


def repost_thread(post_id: str, account_login: str = None) -> bool:
    r = _mt(account_login, lambda c: (c.repost_thread(post_id), True)[1], 'repost_thread')
    return bool(r)


def follow_user(user_id: str, account_login: str = None) -> bool:
    r = _mt(account_login, lambda c: (c.follow(user_id), True)[1], 'follow_user')
    return bool(r)


def search_users(query: str, account_login: str = None) -> list:
    r = _mt(account_login, lambda c: _parse_users(c.search_user(query)), 'search_users')
    return r or []


def get_user_threads(user_id: str, account_login: str = None) -> list:
    r = _mt(account_login, lambda c: _parse_threads(c.get_user_threads(user_id)),
            'get_user_threads')
    return r or []


def get_thread_stats(post_id: str, account_login: str = None) -> dict:
    r = _mt(account_login, lambda c: _parse_stats(c.get_thread(post_id)), 'get_thread_stats')
    return r or {}
