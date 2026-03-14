### threads_api.py
import os, time, random, logging
from dotenv import load_dotenv
import storage
import threads_auth
from threads_auth import TwoFactorRequired

load_dotenv()
logger = logging.getLogger(__name__)

_clients: dict = {}
_pending_2fa: dict = {}  # login -> {'password': ..., 'login': ...}


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


def add_account(login, password):
    logger.info(f"[{login}] Авторизация...")
    try:
        result = threads_auth.login(login, password)
        return _save_from_result(login, result)
    except TwoFactorRequired:
        # БАГ ИСПРАВЛЕН: сохраняем пароль ДО pop, не после
        _pending_2fa[login] = {'login': login, 'password': password}
        raise


def confirm_2fa(login, code):
    if login not in _pending_2fa:
        raise Exception("Сессия 2FA не найдена. Начни заново.")
    # БАГ ИСПРАВЛЕН: сначала читаем пароль, потом удаляем
    pending  = _pending_2fa.pop(login)
    password = pending.get('password', '')
    try:
        from metathreads import MetaThreads
        client = MetaThreads()
        client.login(login, password)
        return _save_from_client(login, client)
    except Exception as e:
        # Возвращаем в pending чтобы можно было попробовать снова
        _pending_2fa[login] = pending
        raise Exception(f"Не удалось подтвердить 2FA: {e}. Используй /manual_cookies")


def add_account_manual(login, session_id, csrf_token):
    from metathreads import MetaThreads
    client   = MetaThreads()
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

    _clients[login] = {
        'client':   client,
        'username': username,
        'user_id':  user_id,
        'login':    login,
    }
    storage.save_account({'login': login, 'session_id': session_id,
                          'csrf_token': csrf_token, 'user_id': user_id,
                          'username': username})
    logger.info(f"[{login}] Добавлен вручную. username={username}")
    return {'login': login, 'username': username}


def _save_from_result(login, result):
    from metathreads import MetaThreads
    client = MetaThreads()
    client.session.cookies.update({
        'sessionid': result['session_id'],
        'csrftoken':  result['csrf_token'],
    })
    _clients[login] = {
        'client':   client,
        'username': result['username'],
        'user_id':  result['user_id'],
        'login':    login,
    }
    storage.save_account({'login': login, **result})
    return {'login': login, 'username': result['username']}


def _save_from_client(login, client):
    cookies    = client.session.cookies.get_dict()
    session_id = cookies.get('sessionid', '')
    csrf_token = cookies.get('csrftoken', '')
    user_id    = ''
    username   = login
    try:
        me = client.me
        if me:
            user_id  = str(me.get('pk') or me.get('id', ''))
            username = me.get('username', login)
    except Exception:
        pass
    _clients[login] = {
        'client':   client,
        'username': username,
        'user_id':  user_id,
        'login':    login,
    }
    storage.save_account({'login': login, 'session_id': session_id,
                          'csrf_token': csrf_token, 'user_id': user_id,
                          'username': username})
    return {'login': login, 'username': username}


def get_client(login=None):
    """Возвращает dict {'client', 'username', 'user_id', 'login'}."""
    if login and login in _clients:
        return _clients[login]
    if not login and _clients:
        return next(iter(_clients.values()))
    raise Exception(f"Аккаунт {'«'+login+'»' if login else ''} не найден. Авторизуйтесь.")


def list_accounts():
    return list(_clients.keys())


def post_series(posts, image_path=None, account_login=None):
    entry  = get_client(account_login)
    client = entry['client']
    ids    = []
    logger.info(f"[{entry['login']}] Публикую: {posts.get('topic','—')}")

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
    try:
        return get_client(account_login)['client'].get_thread_replies(post_id) or []
    except Exception as e:
        logger.warning(f"get_thread_replies: {e}"); return []


def like_thread(post_id, account_login=None):
    try:
        get_client(account_login)['client'].like_thread(post_id); return True
    except Exception as e:
        logger.warning(f"like_thread: {e}"); return False


def repost_thread(post_id, account_login=None):
    try:
        get_client(account_login)['client'].repost_thread(post_id); return True
    except Exception as e:
        logger.warning(f"repost_thread: {e}"); return False


def follow_user(user_id, account_login=None):
    try:
        get_client(account_login)['client'].follow_user(user_id); return True
    except Exception as e:
        logger.warning(f"follow_user: {e}"); return False


def search_users(query, account_login=None):
    try:
        return get_client(account_login)['client'].search_user(query) or []
    except Exception as e:
        logger.warning(f"search_user: {e}"); return []


def get_user_threads(user_id, account_login=None):
    try:
        return get_client(account_login)['client'].get_user_threads(user_id) or []
    except Exception as e:
        logger.warning(f"get_user_threads: {e}"); return []


def get_thread_stats(post_id, account_login=None):
    try:
        thread = get_client(account_login)['client'].get_thread(post_id)
        if not thread: return {}
        d = thread if isinstance(thread, dict) else {}
        return {
            'likes':   d.get('like_count', 0),
            'replies': d.get('reply_count', 0) or d.get('text_post_app_info', {}).get('reply_count', 0),
            'reposts': d.get('repost_count', 0),
        }
    except Exception as e:
        logger.warning(f"get_thread_stats: {e}"); return {}


def _pk(response):
    if isinstance(response, dict):
        return str(response.get('media', {}).get('pk') or response.get('pk', ''))
    return str(response)
