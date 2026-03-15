"""
slash_threads_client.py — собственный клиент Threads API
Полная замена metathreads. Только requests + JSON.

Авторизация: sessionid cookie ИЛИ Bearer token (из instagrapi).
Все методы синхронные — бот оборачивает через asyncio.to_thread().

(c) SLASH VPN Bot, 2026
"""

import json, time, random, logging, uuid
import requests

logger = logging.getLogger(__name__)

# ── Константы ────────────────────────────────────────────────────────────────

THREADS_API  = 'https://www.threads.net/api/v1'
IG_API       = 'https://i.instagram.com/api/v1'

# App ID Threads (публичный, один для всех)
THREADS_APP_ID = '238260118697367'

# User-Agent — эмулируем Android-приложение Threads
DEFAULT_UA = (
    'Barcelona 344.0.0.0.0 Android '
    '(33/13; 420dpi; 1080x2400; samsung; SM-A536B; a53x; exynos1280; en_US; 604247854)'
)

# Заголовки по умолчанию (Instagram private API стиль)
BASE_HEADERS = {
    'User-Agent':        DEFAULT_UA,
    'X-IG-App-ID':       THREADS_APP_ID,
    'X-IG-App-Locale':   'en_US',
    'X-IG-Device-Locale': 'en_US',
    'X-Bloks-Version-Id': '5f56efad68e1edec7801f630b5c122704ec5378adbee6609a448f105f34571c5',
    'X-IG-WWW-Claim':   '0',
    'X-Requested-With':  'com.instagram.barcelona',
    'Content-Type':      'application/x-www-form-urlencoded; charset=UTF-8',
    'Accept-Language':   'en-US',
    'Accept-Encoding':   'gzip, deflate',
}


# ══════════════════════════════════════════════════════════════════════════════
#  КЛИЕНТ
# ══════════════════════════════════════════════════════════════════════════════

class SlashThreadsClient:
    """
    Минимальный клиент Threads/Instagram private API.

    Инициализация:
        # Через sessionid (из браузера / instagrapi)
        client = SlashThreadsClient.from_session(sessionid, csrftoken, user_id, username)

        # Через Bearer token (из instagrapi)
        client = SlashThreadsClient.from_bearer(token, user_id, username)
    """

    def __init__(self, user_id: str = '', username: str = ''):
        self.session  = requests.Session()
        self.session.headers.update(BASE_HEADERS)
        self.user_id  = str(user_id)
        self.username = username
        self._device_id  = f'android-{uuid.uuid4().hex[:16]}'
        self._device_uuid = str(uuid.uuid4())

    # ── Фабрики ──────────────────────────────────────────────────────────────

    @classmethod
    def from_session(cls, sessionid: str, csrftoken: str = '',
                     user_id: str = '', username: str = ''):
        """Создать клиент из sessionid + csrftoken (cookies)."""
        c = cls(user_id, username)
        c.session.cookies.update({
            'sessionid': sessionid,
            'csrftoken': csrftoken or '',
        })
        if csrftoken:
            c.session.headers['X-CSRFToken'] = csrftoken
        return c

    @classmethod
    def from_bearer(cls, bearer_token: str,
                    user_id: str = '', username: str = ''):
        """Создать клиент из Bearer токена (instagrapi auth)."""
        c = cls(user_id, username)
        if bearer_token:
            token = bearer_token if bearer_token.startswith('Bearer ') else f'Bearer {bearer_token}'
            c.session.headers['Authorization'] = token
        return c

    @classmethod
    def from_instagrapi(cls, ig_client, user_id: str = '', username: str = ''):
        """
        Создать из живого instagrapi.Client — извлекаем Bearer + cookies.
        Это самый надёжный способ: берём всё что есть.
        """
        c = cls(user_id, username)

        # Bearer
        try:
            auth = ig_client.private.headers.get('Authorization', '')
            if auth:
                c.session.headers['Authorization'] = auth
        except Exception:
            pass

        # Cookies
        try:
            cookies = ig_client.cookie_dict if hasattr(ig_client, 'cookie_dict') else {}
            sid  = getattr(ig_client, 'sessionid', None) or cookies.get('sessionid', '')
            csrf = cookies.get('csrftoken', '')
            if sid:
                c.session.cookies.update({'sessionid': sid})
            if csrf:
                c.session.cookies.update({'csrftoken': csrf})
                c.session.headers['X-CSRFToken'] = csrf
        except Exception:
            pass

        # Device IDs
        try:
            c._device_id  = ig_client.android_id or c._device_id
            c._device_uuid = ig_client.uuid or c._device_uuid
        except Exception:
            pass

        return c

    # ── Внутренние HTTP-методы ───────────────────────────────────────────────

    def _signed_body(self, data: dict) -> dict:
        """Instagram signed_body формат."""
        return {'signed_body': f'SIGNATURE.{json.dumps(data)}'}

    def _post(self, path: str, data: dict = None, signed: bool = False,
              base_url: str = THREADS_API) -> dict:
        """POST запрос к API."""
        url = f'{base_url}{path}'
        body = self._signed_body(data) if signed and data else (data or {})
        try:
            r = self.session.post(url, data=body, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.JSONDecodeError:
            logger.warning(f"POST {path}: не JSON ответ ({r.status_code})")
            return {}
        except requests.exceptions.HTTPError as e:
            logger.warning(f"POST {path}: HTTP {r.status_code} — {e}")
            raise
        except Exception as e:
            logger.warning(f"POST {path}: {e}")
            raise

    def _get(self, path: str, params: dict = None,
             base_url: str = THREADS_API) -> dict:
        """GET запрос к API."""
        url = f'{base_url}{path}'
        try:
            r = self.session.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.JSONDecodeError:
            logger.warning(f"GET {path}: не JSON ответ ({r.status_code})")
            return {}
        except requests.exceptions.HTTPError as e:
            logger.warning(f"GET {path}: HTTP {r.status_code} — {e}")
            raise
        except Exception as e:
            logger.warning(f"GET {path}: {e}")
            raise

    # ══════════════════════════════════════════════════════════════════════════
    #  ПУБЛИКАЦИЯ
    # ══════════════════════════════════════════════════════════════════════════

    def post_thread(self, caption: str, reply_to: str = None) -> str:
        """
        Опубликовать текстовый пост в Threads.
        reply_to: media_pk поста, на который отвечаем (для цепочки).
        Возвращает media_pk нового поста (строка) или '' при ошибке.
        """
        upload_id = str(int(time.time() * 1000))
        text_post_info = {'reply_control': 0}
        if reply_to:
            text_post_info['reply_id'] = str(reply_to)

        data = {
            'publish_mode':       'text_post',
            'upload_id':          upload_id,
            'text_post_app_info': json.dumps(text_post_info),
            'timezone_offset':    '0',
            'caption':            caption,
            'audience':           'default',
            '_uid':               self.user_id,
            '_uuid':              self._device_uuid,
            'device_id':          self._device_id,
        }
        resp = self._post('/media/configure_text_post_app_feed/', data, signed=True)
        return self._extract_pk(resp)

    def post_image_thread(self, caption: str, image_path: str,
                          reply_to: str = None) -> str:
        """
        Опубликовать пост с изображением.
        1. Загружаем фото через rupload
        2. Конфигурируем как Threads-пост
        Возвращает media_pk (строка) или '' при ошибке.
        """
        upload_id = str(int(time.time() * 1000))

        # Шаг 1: загрузка фото
        with open(image_path, 'rb') as f:
            photo_data = f.read()

        rupload_params = json.dumps({
            'upload_id': upload_id,
            'media_type': 1,
            'retry_context': json.dumps({
                'num_reupload': 0,
                'num_step_auto_retry': 0,
                'num_step_manual_retry': 0,
            }),
        })

        upload_url = f'https://i.instagram.com/rupload_igphoto/{upload_id}'
        upload_headers = {
            'X-Entity-Type': 'image/jpeg',
            'Offset': '0',
            'X-Instagram-Rupload-Params': rupload_params,
            'X-Entity-Name': f'fb_uploader_{upload_id}',
            'X-Entity-Length': str(len(photo_data)),
            'Content-Type': 'application/octet-stream',
        }
        r = self.session.post(upload_url, data=photo_data,
                              headers=upload_headers, timeout=60)
        r.raise_for_status()
        logger.debug(f"Photo uploaded: {r.json()}")

        # Шаг 2: конфигурируем как Threads-пост
        text_post_info = {'reply_control': 0}
        if reply_to:
            text_post_info['reply_id'] = str(reply_to)

        data = {
            'caption':            caption,
            'upload_id':          upload_id,
            'publish_mode':       'media_post',
            'text_post_app_info': json.dumps(text_post_info),
            'timezone_offset':    '0',
            'audience':           'default',
            '_uid':               self.user_id,
            '_uuid':              self._device_uuid,
            'device_id':          self._device_id,
        }
        resp = self._post('/media/configure_text_post_app_feed/', data, signed=True)
        return self._extract_pk(resp)

    # ══════════════════════════════════════════════════════════════════════════
    #  ДЕЙСТВИЯ (лайк, репост, фоллоу)
    # ══════════════════════════════════════════════════════════════════════════

    def like(self, media_id: str) -> bool:
        """Лайкнуть пост/комментарий."""
        try:
            self._post(f'/media/{media_id}/like/', self._signed_body({
                'media_id': media_id,
                '_uid':     self.user_id,
                '_uuid':    self._device_uuid,
            }))
            return True
        except Exception as e:
            logger.warning(f"like({media_id}): {e}")
            return False

    def unlike(self, media_id: str) -> bool:
        """Убрать лайк."""
        try:
            self._post(f'/media/{media_id}/unlike/', self._signed_body({
                'media_id': media_id,
                '_uid':     self.user_id,
                '_uuid':    self._device_uuid,
            }))
            return True
        except Exception:
            return False

    def repost(self, media_id: str) -> bool:
        """Репост (repost) в Threads."""
        try:
            self._post('/repost/create_repost/', self._signed_body({
                'media_id': media_id,
                '_uid':     self.user_id,
                '_uuid':    self._device_uuid,
            }))
            return True
        except Exception as e:
            logger.warning(f"repost({media_id}): {e}")
            return False

    def unrepost(self, media_id: str) -> bool:
        """Убрать репост."""
        try:
            self._post('/repost/delete_text_app_repost/', self._signed_body({
                'media_id': media_id,
                '_uid':     self.user_id,
                '_uuid':    self._device_uuid,
            }))
            return True
        except Exception:
            return False

    def follow(self, user_id: str) -> bool:
        """Подписаться на пользователя."""
        try:
            self._post(f'/friendships/create/{user_id}/', self._signed_body({
                'user_id':  user_id,
                '_uid':     self.user_id,
                '_uuid':    self._device_uuid,
            }))
            return True
        except Exception as e:
            logger.warning(f"follow({user_id}): {e}")
            return False

    def unfollow(self, user_id: str) -> bool:
        """Отписаться."""
        try:
            self._post(f'/friendships/destroy/{user_id}/', self._signed_body({
                'user_id':  user_id,
                '_uid':     self.user_id,
                '_uuid':    self._device_uuid,
            }))
            return True
        except Exception:
            return False

    # ══════════════════════════════════════════════════════════════════════════
    #  ПОЛУЧЕНИЕ ДАННЫХ
    # ══════════════════════════════════════════════════════════════════════════

    def search_users(self, query: str, count: int = 30) -> list:
        """Поиск пользователей."""
        try:
            resp = self._get('/users/search/', params={
                'q': query,
                'count': count,
            })
            return resp.get('users', [])
        except Exception as e:
            logger.warning(f"search_users({query}): {e}")
            return []

    def get_user_id(self, username: str) -> str:
        """Получить user_id по username."""
        try:
            resp = self._get('/users/web_profile_info/', params={
                'username': username,
            })
            user = resp.get('data', {}).get('user', {})
            return str(user.get('pk', '') or user.get('id', ''))
        except Exception:
            # Fallback: поиск
            users = self.search_users(username, count=5)
            for u in users:
                if u.get('username', '').lower() == username.lower():
                    return str(u.get('pk', '') or u.get('id', ''))
            return ''

    def get_user_info(self, username: str) -> dict:
        """Получить профиль пользователя."""
        try:
            resp = self._get('/users/web_profile_info/', params={
                'username': username,
            })
            return resp.get('data', {}).get('user', {})
        except Exception as e:
            logger.warning(f"get_user_info({username}): {e}")
            return {}

    def get_user_threads(self, user_id: str) -> list:
        """Получить посты пользователя."""
        try:
            resp = self._get(f'/text_feed/{user_id}/profile/')
            return self._parse_threads(resp)
        except Exception as e:
            logger.warning(f"get_user_threads({user_id}): {e}")
            return []

    def get_thread(self, post_id: str) -> dict:
        """Получить пост (thread) по ID — включая статистику."""
        try:
            return self._get(f'/text_feed/{post_id}/replies/')
        except Exception as e:
            logger.warning(f"get_thread({post_id}): {e}")
            return {}

    def get_thread_replies(self, post_id: str) -> list:
        """Получить ответы (replies) на пост."""
        try:
            resp = self._get(f'/text_feed/{post_id}/replies/')
            return self._parse_replies(resp)
        except Exception as e:
            logger.warning(f"get_thread_replies({post_id}): {e}")
            return []

    def get_thread_stats(self, post_id: str) -> dict:
        """Получить статистику поста: лайки, ответы, репосты."""
        resp = self.get_thread(post_id)
        return self._parse_stats(resp)

    # ══════════════════════════════════════════════════════════════════════════
    #  ПАРСИНГ ОТВЕТОВ (перенесено из threads_api.py)
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _extract_pk(resp) -> str:
        """Извлечь media pk из ответа API."""
        if resp is None or isinstance(resp, bool):
            return ''
        if isinstance(resp, dict):
            pk = (resp.get('media') or {}).get('pk')
            if pk:
                return str(pk)
            pk = resp.get('pk')
            if pk:
                return str(pk)
        return str(resp) if resp else ''

    @staticmethod
    def _parse_threads(resp) -> list:
        """Распарсить список постов пользователя."""
        if not resp:
            return []
        if isinstance(resp, list):
            return resp
        posts = []
        if isinstance(resp, dict):
            for t in (resp.get('threads', []) or []):
                if not isinstance(t, dict):
                    continue
                for item in (t.get('thread_items', []) or []):
                    post = item.get('post', item) if isinstance(item, dict) else item
                    if post:
                        posts.append(post)
        return posts

    @staticmethod
    def _parse_replies(resp) -> list:
        """Распарсить ответы на пост."""
        if not resp:
            return []
        if isinstance(resp, list):
            return resp
        posts = []
        if isinstance(resp, dict):
            for key in ('reply_threads', 'containing_thread', 'threads'):
                bucket = resp.get(key, [])
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
                for val in resp.values():
                    if isinstance(val, list) and val:
                        posts = val
                        break
        return posts

    @staticmethod
    def _parse_stats(resp) -> dict:
        """Извлечь статистику из ответа get_thread."""
        if not resp or not isinstance(resp, dict):
            return {}
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

    # ── Утилиты ──────────────────────────────────────────────────────────────

    def __repr__(self):
        auth = 'Bearer' if 'Authorization' in self.session.headers else 'Cookie'
        return f'<SlashThreadsClient @{self.username} [{auth}]>'
