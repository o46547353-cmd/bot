"""
slash_threads_client.py — собственный клиент Threads API v3

ВСЕ запросы идут через www.threads.net/api/v1 (как metathreads).
Единственное исключение: rupload_igphoto (загрузка фото) → i.instagram.com.

(c) SLASH VPN Bot, 2026
"""

import json, time, logging, uuid
import requests

logger = logging.getLogger(__name__)

# ── Константы ────────────────────────────────────────────────────────────────

BASE_URL = 'https://www.threads.net/api/v1'

THREADS_APP_ID = '238260118697367'

DEFAULT_UA = (
    'Barcelona 344.0.0.0.0 Android '
    '(33/13; 420dpi; 1080x2400; samsung; SM-A536B; a53x; exynos1280; en_US; 604247854)'
)

BASE_HEADERS = {
    'User-Agent':          DEFAULT_UA,
    'X-IG-App-ID':         THREADS_APP_ID,
    'X-IG-App-Locale':     'en_US',
    'X-IG-Device-Locale':  'en_US',
    'X-Bloks-Version-Id':  '5f56efad68e1edec7801f630b5c122704ec5378adbee6609a448f105f34571c5',
    'X-IG-WWW-Claim':     '0',
    'X-Requested-With':    'com.instagram.barcelona',
    'Content-Type':        'application/x-www-form-urlencoded; charset=UTF-8',
    'Accept-Language':     'en-US',
    'Accept-Encoding':     'gzip, deflate',
}


class AuthExpired(Exception):
    """Bearer token протух — нужен перелогин."""
    pass


class SlashThreadsClient:

    def __init__(self, user_id: str = '', username: str = ''):
        self.session  = requests.Session()
        self.session.headers.update(BASE_HEADERS)
        self.user_id  = str(user_id)
        self.username = username
        self._device_id   = f'android-{uuid.uuid4().hex[:16]}'
        self._device_uuid = str(uuid.uuid4())
        # Отдельная сессия для Instagram private API (upload, feed)
        self.ig_session = None  # requests.Session from instagrapi

    # ── Фабрики ──────────────────────────────────────────────────────────────

    @classmethod
    def from_session(cls, sessionid: str, csrftoken: str = '',
                     user_id: str = '', username: str = ''):
        c = cls(user_id, username)
        c.session.cookies.set('sessionid', sessionid, domain='.threads.net')
        if csrftoken:
            c.session.cookies.set('csrftoken', csrftoken, domain='.threads.net')
            c.session.headers['X-CSRFToken'] = csrftoken
        return c

    @classmethod
    def from_bearer(cls, bearer_token: str,
                    user_id: str = '', username: str = ''):
        c = cls(user_id, username)
        if bearer_token:
            token = bearer_token if bearer_token.startswith('Bearer ') else f'Bearer {bearer_token}'
            c.session.headers['Authorization'] = token
        return c

    @classmethod
    def from_instagrapi(cls, ig_client, user_id: str = '', username: str = ''):
        """Создать из instagrapi.Client — берём Bearer + cookies + device IDs + ig session."""
        c = cls(user_id, username)

        # Bearer token
        try:
            auth = ig_client.private.headers.get('Authorization', '')
            if auth:
                c.session.headers['Authorization'] = auth
        except Exception:
            pass

        # Cookies — ставим на оба домена
        try:
            cookies = ig_client.cookie_dict if hasattr(ig_client, 'cookie_dict') else {}
            sid  = getattr(ig_client, 'sessionid', None) or cookies.get('sessionid', '')
            csrf = cookies.get('csrftoken', '')
            if sid:
                c.session.cookies.set('sessionid', sid, domain='.threads.net')
                c.session.cookies.set('sessionid', sid, domain='.instagram.com')
            if csrf:
                c.session.cookies.set('csrftoken', csrf, domain='.threads.net')
                c.session.cookies.set('csrftoken', csrf, domain='.instagram.com')
                c.session.headers['X-CSRFToken'] = csrf
        except Exception:
            pass

        # Device IDs
        try:
            c._device_id   = ig_client.android_id or c._device_id
            c._device_uuid = ig_client.uuid or c._device_uuid
        except Exception:
            pass

        # Сохраняем instagrapi private session для upload/feed
        try:
            c.ig_session = ig_client.private
        except Exception:
            pass

        return c

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def _signed_body(self, data: dict) -> dict:
        return {'signed_body': f'SIGNATURE.{json.dumps(data)}'}

    def _post(self, path: str, data: dict = None, signed: bool = False,
              base_url: str = None) -> dict:
        url = f'{base_url or BASE_URL}{path}'
        body = self._signed_body(data) if signed and data else (data or {})
        timeout = 30 if 'configure' in path else 15
        r = self.session.post(url, data=body, timeout=timeout, allow_redirects=False)
        if r.status_code in (400, 403):
            raise AuthExpired(f"{r.status_code} POST {path}")
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            logger.warning(f"POST {path}: не JSON ({r.status_code})")
            return {}

    def _get(self, path: str, params: dict = None,
             base_url: str = None) -> dict:
        url = f'{base_url or BASE_URL}{path}'
        r = self.session.get(url, params=params, timeout=15, allow_redirects=False)
        if r.status_code in (400, 403):
            raise AuthExpired(f"{r.status_code} GET {path}")
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            logger.warning(f"GET {path}: не JSON ({r.status_code})")
            return {}

    def _graphql(self, doc_id: str, variables: dict) -> dict:
        """
        GraphQL запрос к threads.net/api/graphql.
        Не требует Bearer — только x-ig-app-id + web user-agent.
        ВАЖНО: используем чистый requests.post() без self.session
        (чтобы не отправлять Android-заголовки и Bearer).
        """
        url = 'https://www.threads.net/api/graphql'
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'X-IG-App-ID': THREADS_APP_ID,
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Origin': 'https://www.threads.net',
            'Referer': 'https://www.threads.net/',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
        }
        try:
            # Чистый requests.post — не self.session!
            r = requests.post(url, data={
                'variables': json.dumps(variables),
                'doc_id': doc_id,
            }, headers=headers, timeout=15)
            # Проверяем что ответ — JSON, а не HTML
            ct = r.headers.get('content-type', '')
            if 'html' in ct or r.text.strip().startswith('<!'):
                logger.warning(f"graphql doc_id={doc_id}: HTML response ({r.status_code})")
                return {}
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and ('data' in data or 'errors' in data):
                return data
            return data
        except requests.exceptions.JSONDecodeError:
            body = r.text[:100] if r else ''
            logger.warning(f"graphql doc_id={doc_id}: не JSON ({r.status_code}, body={body})")
            return {}
        except Exception as e:
            logger.warning(f"graphql doc_id={doc_id}: {e}")
            return {}

    # ══════════════════════════════════════════════════════════════════════════
    #  ДИАГНОСТИКА — сырой HTTP ответ для отладки
    # ══════════════════════════════════════════════════════════════════════════

    def debug_search(self, query: str = 'vpn') -> dict:
        """Тестирует поиск и возвращает сырую диагностику."""
        results = {}

        # Тест 1: threads.net /users/search/
        url1 = f'{BASE_URL}/users/search/'
        try:
            r = self.session.get(url1, params={'q': query, 'count': 10},
                                 timeout=8, allow_redirects=False)
            results['threads.net /users/search/'] = {
                'status': r.status_code,
                'body':   r.text[:300],
            }
        except Exception as e:
            results['threads.net /users/search/'] = {'status': 0, 'body': str(e)[:300]}

        # Тест 2: i.instagram.com /users/search/
        url2 = f'https://i.instagram.com/api/v1/users/search/'
        try:
            r = self.session.get(url2, params={'q': query, 'count': 10},
                                 timeout=8, allow_redirects=False)
            results['i.instagram.com /users/search/'] = {
                'status': r.status_code,
                'body':   r.text[:300],
            }
        except Exception as e:
            results['i.instagram.com /users/search/'] = {'status': 0, 'body': str(e)[:300]}

        # Тест 3: threads.net /text_feed/recommended_users/
        url3 = f'{BASE_URL}/text_feed/recommended_users/'
        try:
            r = self.session.get(url3, params={'search_query': query},
                                 timeout=8, allow_redirects=False)
            results['threads.net /recommended_users/'] = {
                'status': r.status_code,
                'body':   r.text[:300],
            }
        except Exception as e:
            results['threads.net /recommended_users/'] = {'status': 0, 'body': str(e)[:300]}

        # Auth info
        auth_info = {
            'has_bearer':  'Authorization' in self.session.headers,
            'has_session':  bool(self.session.cookies.get('sessionid')),
            'has_csrf':     bool(self.session.cookies.get('csrftoken')),
            'user_id':      self.user_id,
            'device_id':    self._device_id[:20],
        }
        results['_auth'] = auth_info

        return results

    def debug_feed(self, user_id: str = None) -> dict:
        """Тестирует получение ленты."""
        uid = user_id or self.user_id
        results = {}
        url = f'{BASE_URL}/text_feed/{uid}/profile/'
        try:
            r = self.session.get(url, timeout=8, allow_redirects=False)
            results[f'/text_feed/{uid}/profile/'] = {
                'status': r.status_code,
                'body':   r.text[:400],
            }
        except Exception as e:
            results[f'/text_feed/{uid}/profile/'] = {'status': 0, 'body': str(e)[:300]}
        return results

    # ══════════════════════════════════════════════════════════════════════════
    #  ПУБЛИКАЦИЯ
    # ══════════════════════════════════════════════════════════════════════════

    def post_thread(self, caption: str, reply_to: str = None) -> str:
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
        Весь процесс через ig_session (instagrapi private) — и upload и configure.
        Это решает 412/500 — сессия единая.
        """
        upload_id = str(int(time.time() * 1000))

        with open(image_path, 'rb') as f:
            photo_data = f.read()

        rupload_params = json.dumps({
            'upload_id': upload_id,
            'media_type': 1,
            'retry_context': json.dumps({
                'num_reupload': 0, 'num_step_auto_retry': 0, 'num_step_manual_retry': 0,
            }),
        })

        # Нужен ig_session для upload+configure
        ig = self.ig_session
        if not ig:
            raise Exception("ig_session не доступна — нужен instagrapi логин для image posting")

        # Step 1: Upload через Instagram
        upload_url = f'https://i.instagram.com/rupload_igphoto/{upload_id}'
        r = ig.post(upload_url, data=photo_data, headers={
            'X-Entity-Type': 'image/jpeg', 'Offset': '0',
            'X-Instagram-Rupload-Params': rupload_params,
            'X-Entity-Name': f'fb_uploader_{upload_id}',
            'X-Entity-Length': str(len(photo_data)),
            'Content-Type': 'application/octet-stream',
        }, timeout=60)
        if r.status_code in (403, 412):
            raise AuthExpired(f"{r.status_code} rupload_igphoto")
        r.raise_for_status()
        logger.debug(f"Photo uploaded: {r.json()}")

        # Step 2: Configure через Instagram (не threads.net!)
        text_post_info = {'reply_control': 0}
        if reply_to:
            text_post_info['reply_id'] = str(reply_to)

        configure_data = {
            'caption': caption,
            'upload_id': upload_id,
            'publish_mode': 'media_post',
            'text_post_app_info': json.dumps(text_post_info),
            'timezone_offset': '0',
            'source_type': '4',
            'audience': 'default',
            '_uid': self.user_id,
            '_uuid': self._device_uuid,
            'device_id': self._device_id,
            'device': json.dumps({
                'manufacturer': 'OnePlus',
                'model': 'ONEPLUS A6013',
                'android_version': 28,
                'android_release': '9.0',
            }),
        }
        signed = {'signed_body': f'SIGNATURE.{json.dumps(configure_data)}'}

        configure_url = 'https://www.threads.net/api/v1/media/configure_text_post_app_feed/'
        r2 = ig.post(configure_url, data=signed, timeout=30)
        if r2.status_code in (403, 412):
            raise AuthExpired(f"{r2.status_code} configure_image")
        r2.raise_for_status()
        result = r2.json()
        return self._extract_pk(result)

    # ══════════════════════════════════════════════════════════════════════════
    #  ДЕЙСТВИЯ
    # ══════════════════════════════════════════════════════════════════════════

    def like(self, media_id: str) -> bool:
        try:
            self._post(f'/media/{media_id}/like/', {
                'media_id': media_id, '_uid': self.user_id, '_uuid': self._device_uuid,
            }, signed=True)
            return True
        except AuthExpired:
            raise
        except Exception as e:
            logger.warning(f"like({media_id}): {e}")
            return False

    def unlike(self, media_id: str) -> bool:
        try:
            self._post(f'/media/{media_id}/unlike/', {
                'media_id': media_id, '_uid': self.user_id, '_uuid': self._device_uuid,
            }, signed=True)
            return True
        except AuthExpired:
            raise
        except Exception:
            return False

    def repost(self, media_id: str) -> bool:
        try:
            self._post('/repost/create_repost/', {
                'media_id': media_id, '_uid': self.user_id, '_uuid': self._device_uuid,
            }, signed=True)
            return True
        except AuthExpired:
            raise
        except Exception as e:
            logger.warning(f"repost({media_id}): {e}")
            return False

    def unrepost(self, media_id: str) -> bool:
        try:
            self._post('/repost/delete_text_app_repost/', {
                'media_id': media_id, '_uid': self.user_id, '_uuid': self._device_uuid,
            }, signed=True)
            return True
        except AuthExpired:
            raise
        except Exception:
            return False

    def follow(self, user_id: str) -> bool:
        try:
            self._post(f'/friendships/create/{user_id}/', {
                'user_id': user_id, '_uid': self.user_id, '_uuid': self._device_uuid,
            }, signed=True)
            return True
        except AuthExpired:
            raise
        except Exception as e:
            logger.warning(f"follow({user_id}): {e}")
            return False

    def unfollow(self, user_id: str) -> bool:
        try:
            self._post(f'/friendships/destroy/{user_id}/', {
                'user_id': user_id, '_uid': self.user_id, '_uuid': self._device_uuid,
            }, signed=True)
            return True
        except AuthExpired:
            raise
        except Exception:
            return False

    # ══════════════════════════════════════════════════════════════════════════
    #  ПОЛУЧЕНИЕ ДАННЫХ
    # ══════════════════════════════════════════════════════════════════════════

    def search_users(self, query: str, count: int = 30) -> list:
        try:
            resp = self._get('/users/search/', params={'q': query, 'count': count})
            return resp.get('users', [])
        except AuthExpired:
            raise
        except Exception as e:
            logger.warning(f"search_users({query}): {e}")
            return []

    def get_recommended_users(self, search_query: str = '') -> list:
        """Получить рекомендованных юзеров (работает даже когда search пуст)."""
        try:
            params = {}
            if search_query:
                params['search_query'] = search_query
            resp = self._get('/text_feed/recommended_users/', params=params or None)
            return resp.get('users', [])
        except AuthExpired:
            raise
        except Exception as e:
            logger.warning(f"get_recommended_users: {e}")
            return []

    def get_user_id(self, username: str) -> str:
        try:
            resp = self._get('/users/web_profile_info/', params={'username': username})
            user = resp.get('data', {}).get('user', {})
            return str(user.get('pk', '') or user.get('id', ''))
        except AuthExpired:
            raise
        except Exception:
            users = self.search_users(username, count=5)
            for u in users:
                if u.get('username', '').lower() == username.lower():
                    return str(u.get('pk', '') or u.get('id', ''))
            return ''

    def get_user_info(self, username: str) -> dict:
        try:
            resp = self._get('/users/web_profile_info/', params={'username': username})
            return resp.get('data', {}).get('user', {})
        except AuthExpired:
            raise
        except Exception as e:
            logger.warning(f"get_user_info({username}): {e}")
            return {}

    # ── GraphQL doc_id (threads-re reverse engineering) ─────────────────────
    GQL_USER_POSTS   = '6232751443445612'  # userID → посты
    GQL_USER_PROFILE = '23996318473300828'  # userID → профиль
    GQL_USER_REPLIES = '6307072669391286'  # userID → ответы
    GQL_POST         = '5587632691339264'  # postID → пост
    GQL_POST_LIKERS  = '9360915773983802'  # mediaID → кто лайкнул

    def get_user_threads(self, user_id: str) -> list:
        """Получить посты пользователя через GraphQL (основной) → REST fallback."""
        # Способ 1: GraphQL (не требует Bearer, самый надёжный)
        try:
            resp = self._graphql(self.GQL_USER_POSTS, {'userID': user_id})
            posts = self._parse_graphql_threads(resp)
            if posts:
                logger.debug(f"get_user_threads({user_id}): graphql ✓ ({len(posts)})")
                return posts
        except Exception as e:
            logger.debug(f"get_user_threads graphql: {e}")

        # Способ 2: REST text_feed (может быть 403)
        try:
            resp = self._get(f'/text_feed/{user_id}/profile/')
            posts = self._parse_threads(resp)
            if posts:
                return posts
        except AuthExpired:
            raise
        except Exception:
            pass

        # Способ 3: Instagram private API через ig_session
        if self.ig_session:
            try:
                url = f'https://i.instagram.com/api/v1/text_feed/{user_id}/profile/'
                r = self.ig_session.get(url, timeout=15)
                if r.status_code == 200:
                    resp = r.json()
                    posts = self._parse_threads(resp)
                    if posts:
                        return posts
            except Exception:
                pass

        return []

    def get_timeline(self) -> list:
        """Получить домашнюю ленту — посты для лайков."""
        # Способ 1: Threads API
        try:
            resp = self._post('/feed/timeline/', {
                'reason': 'cold_start_fetch',
                '_uid': self.user_id,
                '_uuid': self._device_uuid,
            }, signed=True)
            items = resp.get('feed_items', []) or resp.get('items', [])
            posts = []
            for item in items:
                if isinstance(item, dict):
                    post = item.get('media_or_ad', item)
                    pk = post.get('pk') or post.get('id')
                    if pk:
                        posts.append(post)
            if posts:
                return posts
        except AuthExpired:
            pass  # Не raise — пробуем ig_session
        except Exception:
            pass

        # Способ 2: Instagram private API
        if self.ig_session:
            try:
                url = 'https://i.instagram.com/api/v1/feed/timeline/'
                r = self.ig_session.post(url, data={
                    'reason': 'cold_start_fetch',
                    '_uid': self.user_id,
                    '_uuid': self._device_uuid,
                }, timeout=15)
                if r.status_code == 200:
                    resp = r.json()
                    items = resp.get('feed_items', []) or resp.get('items', [])
                    posts = []
                    for item in items:
                        if isinstance(item, dict):
                            post = item.get('media_or_ad', item)
                            pk = post.get('pk') or post.get('id')
                            if pk:
                                posts.append(post)
                    if posts:
                        logger.info(f"get_timeline: ig_session ✓ ({len(posts)} posts)")
                        return posts
            except Exception:
                pass

        return []

    def get_text_app_explore(self) -> list:
        """Explore-лента Threads — посты для лайков."""
        try:
            resp = self._get('/text_feed/text_app_explore/')
            items = resp.get('items', []) or resp.get('threads', [])
            posts = []
            for item in items:
                if isinstance(item, dict):
                    for ti in item.get('thread_items', [item]):
                        post = ti.get('post', ti) if isinstance(ti, dict) else ti
                        if isinstance(post, dict) and (post.get('pk') or post.get('id')):
                            posts.append(post)
            return posts
        except AuthExpired:
            raise
        except Exception as e:
            logger.warning(f"get_text_app_explore: {e}")
            return []

    def get_thread(self, post_id: str) -> dict:
        """Получить пост по ID — GraphQL → REST fallback."""
        # GraphQL
        try:
            resp = self._graphql(self.GQL_POST, {'postID': post_id})
            if resp and resp.get('data'):
                return resp
        except Exception:
            pass
        # REST
        try:
            return self._get(f'/text_feed/{post_id}/replies/')
        except AuthExpired:
            raise
        except Exception as e:
            logger.warning(f"get_thread({post_id}): {e}")
            return {}

    def get_thread_replies(self, post_id: str) -> list:
        try:
            resp = self._get(f'/text_feed/{post_id}/replies/')
            return self._parse_replies(resp)
        except AuthExpired:
            raise
        except Exception as e:
            logger.warning(f"get_thread_replies({post_id}): {e}")
            return []

    def get_thread_stats(self, post_id: str) -> dict:
        try:
            resp = self.get_thread(post_id)
            return self._parse_stats(resp)
        except AuthExpired:
            raise
        except Exception:
            return {}

    # ══════════════════════════════════════════════════════════════════════════
    #  ПАРСИНГ
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _extract_pk(resp) -> str:
        if resp is None or isinstance(resp, bool):
            return ''
        if isinstance(resp, dict):
            pk = (resp.get('media') or {}).get('pk')
            if pk: return str(pk)
            pk = resp.get('pk')
            if pk: return str(pk)
        return str(resp) if resp else ''

    @staticmethod
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

    @staticmethod
    def _parse_graphql_threads(resp) -> list:
        """Парсит ответ GraphQL doc_id=6232751443445612 (user posts)."""
        if not resp or not isinstance(resp, dict):
            return []
        posts = []
        try:
            # GraphQL: data.mediaData.threads[].thread_items[].post
            data = resp.get('data', {})
            media_data = data.get('mediaData', data)
            threads = media_data.get('threads', [])
            if not threads:
                # Альтернативная структура
                user = data.get('userData', data).get('user', data)
                threads = user.get('threads', {}).get('edges', [])

            for t in threads:
                if isinstance(t, dict):
                    # Может быть {node: {thread_items: [...]}} или прямо {thread_items: [...]}
                    node = t.get('node', t)
                    for item in (node.get('thread_items', []) or []):
                        post = item.get('post', item) if isinstance(item, dict) else item
                        if isinstance(post, dict) and (post.get('pk') or post.get('id')):
                            posts.append(post)
        except Exception as e:
            logger.debug(f"_parse_graphql_threads: {e}")
        return posts

    @staticmethod
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

    @staticmethod
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

    def __repr__(self):
        auth = 'Bearer' if 'Authorization' in self.session.headers else 'Cookie'
        return f'<SlashThreadsClient @{self.username} [{auth}]>'