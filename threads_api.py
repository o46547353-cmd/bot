### threads_api.py
import os, time, random, logging
from dotenv import load_dotenv
import storage
import threads_auth
from threads_auth import TwoFactorRequired

load_dotenv()
logger = logging.getLogger(__name__)

_clients: dict = {}
_pending_2fa: dict = {}


def load_accounts_from_db():
    for acc_ref in storage.get_all_accounts():
        acc = storage.get_account(acc_ref['login'])
        if acc and acc.get('session_id'):
            try:
                from metathreads import MetaThreads
                client = MetaThreads()
                client.session.cookies.update({
                    'sessionid': acc['session_id'],
                    'csrftoken':  acc['csrf_token'],
                })
                client.username = acc['username']
                client.user_id  = acc['user_id']
                _clients[acc['login']] = client
                logger.info(f"Загружен: {acc['login']}")
            except Exception as e:
                logger.warning(f"Не удалось загрузить {acc['login']}: {e}")
    logger.info(f"Загружено аккаунтов: {len(_clients)}")


def add_account(login, password):
    """Авторизация через мобильный API. При 2FA бросает TwoFactorRequired."""
    logger.info(f"[{login}] Авторизация...")
    try:
        result = threads_auth.login(login, password)
        return _save_from_result(login, result)
    except TwoFactorRequired:
        # Сохраняем для confirm_2fa
        _pending_2fa[login] = {'login': login, 'password': password}
        raise


def confirm_2fa(login, code):
    """Подтверждение 2FA кода."""
    if login not in _pending_2fa:
        raise Exception("Сессия 2FA не найдена. Начни заново через /add_account")
    _pending_2fa.pop(login)
    # Пробуем через metathreads
    try:
        from metathreads import MetaThreads
        client = MetaThreads()
        client.login(login, _pending_2fa.get('password', ''))
        return _save_from_client(login, client)
    except Exception:
        raise Exception("Не удалось подтвердить 2FA. Используй /manual_cookies")


def add_account_manual(login, session_id, csrf_token):
    """Добавление через cookies из браузера."""
    from metathreads import MetaThreads
    client = MetaThreads()
    client.session.cookies.update({'sessionid': session_id, 'csrftoken': csrf_token})
    user_id  = ''
    username = login
    try:
        me = client.me
        if me:
            user_id  = str(me.get('pk') or me.get('id', ''))
            username = me.get('username', login)
    except Exception as e:
        logger.warning(f"[{login}] Профиль не получен: {e}")
    client.username = username
    client.user_id  = user_id
    _clients[login] = client
    storage.save_account({'login': login, 'session_id': session_id,
                          'csrf_token': csrf_token, 'user_id': user_id, 'username': username})
    logger.info(f"[{login}] Добавлен вручную")
    return {'login': login, 'username': username}


def _save_from_result(login, result):
    from metathreads import MetaThreads
    client = MetaThreads()
    client.session.cookies.update({'sessionid': result['session_id'], 'csrftoken': result['csrf_token']})
    client.username = result['username']
    client.user_id  = result['user_id']
    _clients[login] = client
    storage.save_account({'login': login, **result})
    return {'login': login, 'username': result['username']}


def _save_from_client(login, client):
    cookies    = client.session.cookies.get_dict()
    session_id = cookies.get('sessionid', '')
    csrf_token = cookies.get('csrftoken', '')
    user_id    = str(getattr(client, 'user_id', '') or '')
    username   = str(getattr(client, 'username', login) or login)
    _clients[login] = client
    storage.save_account({'login': login, 'session_id': session_id,
                          'csrf_token': csrf_token, 'user_id': user_id, 'username': username})
    return {'login': login, 'username': username}


def get_client(login=None):
    if login and login in _clients:
        return _clients[login]
    if not login and _clients:
        return next(iter(_clients.values()))
    raise Exception(f"Аккаунт {'«'+login+'»' if login else ''} не найден. Авторизуйтесь.")


def list_accounts():
    return list(_clients.keys())


def post_series(posts, image_path=None, account_login=None):
    client = get_client(account_login)
    ids = []
    logger.info(f"[{account_login}] Публикую серию: {posts.get('topic','—')}")

    r1  = client.post_thread(thread_caption=posts['post1'])
    id1 = _pk(r1); ids.append(id1)
    logger.info(f"Пост 1: {id1}")
    time.sleep(random.uniform(8, 14))

    r2  = client.post_thread(thread_caption=posts['post2'], reply_to=id1)
    id2 = _pk(r2); ids.append(id2)
    logger.info(f"Пост 2: {id2}")
    time.sleep(random.uniform(8, 14))

    if image_path and os.path.exists(image_path):
        r3 = client.post_thread(thread_caption=posts['post3'], image=image_path, reply_to=id2)
    else:
        r3 = client.post_thread(thread_caption=posts['post3'], reply_to=id2)
    id3 = _pk(r3); ids.append(id3)
    logger.info(f"Пост 3: {id3}")
    time.sleep(random.uniform(8, 14))

    r4  = client.post_thread(thread_caption=posts['post4'], reply_to=id3)
    id4 = _pk(r4); ids.append(id4)
    logger.info(f"Пост 4: {id4}")

    return ids


def get_thread_replies(post_id, account_login=None):
    """Получить комментарии к посту."""
    client = get_client(account_login)
    try:
        return client.get_thread_replies(post_id) or []
    except Exception as e:
        logger.warning(f"get_thread_replies ошибка: {e}")
        return []


def like_thread(post_id, account_login=None):
    client = get_client(account_login)
    try:
        client.like_thread(post_id)
        return True
    except Exception as e:
        logger.warning(f"like_thread ошибка: {e}")
        return False


def repost_thread(post_id, account_login=None):
    client = get_client(account_login)
    try:
        client.repost_thread(post_id)
        return True
    except Exception as e:
        logger.warning(f"repost_thread ошибка: {e}")
        return False


def follow_user(user_id, account_login=None):
    client = get_client(account_login)
    try:
        client.follow_user(user_id)
        return True
    except Exception as e:
        logger.warning(f"follow_user ошибка: {e}")
        return False


def search_users(query, account_login=None):
    client = get_client(account_login)
    try:
        return client.search_user(query) or []
    except Exception as e:
        logger.warning(f"search_user ошибка: {e}")
        return []


def get_user_threads(user_id, account_login=None):
    client = get_client(account_login)
    try:
        return client.get_user_threads(user_id) or []
    except Exception as e:
        logger.warning(f"get_user_threads ошибка: {e}")
        return []


def get_thread_stats(post_id, account_login=None):
    """Получить статистику поста (лайки, ответы)."""
    client = get_client(account_login)
    try:
        thread = client.get_thread(post_id)
        if not thread:
            return {}
        data = thread if isinstance(thread, dict) else {}
        return {
            'likes':   data.get('like_count', 0),
            'replies': data.get('reply_count', 0) or data.get('text_post_app_info', {}).get('reply_count', 0),
            'reposts': data.get('repost_count', 0),
        }
    except Exception as e:
        logger.warning(f"get_thread_stats ошибка: {e}")
        return {}


def _pk(response):
    if isinstance(response, dict):
        return str(response.get('media', {}).get('pk') or response.get('pk', ''))
    return str(response)
