"""
scan_endpoints.py — Сканер эндпоинтов Threads / Instagram API

Что делает:
  1. Парсит исходники metathreads (если установлен) — вытаскивает все URL/пути
  2. Добавляет известные эндпоинты Instagram private API
  3. Пробует каждый с текущей сессией (GET/POST)
  4. Показывает HTTP-код + первые символы ответа
  5. Сохраняет результаты в endpoints_report.txt

Запуск:
  python scan_endpoints.py

Перед запуском убедись что бот уже добавил аккаунт (есть БД + session файл).
"""

import os, sys, re, json, time, importlib, inspect, glob
import requests

# ─── Загрузка сессии из бота ────────────────────────────────────────────────

def load_client_from_bot():
    """Загружает SlashThreadsClient из БД бота."""
    try:
        import storage, threads_api
        from dotenv import load_dotenv
        load_dotenv()
        threads_api.load_accounts_from_db()
        accounts = threads_api.list_accounts()
        if not accounts:
            print("❌ Нет аккаунтов в БД")
            sys.exit(1)
        login = accounts[0]
        entry = threads_api._clients[login]
        client = entry['client']
        print(f"✅ Аккаунт: {login} ({client})")
        return client, login
    except Exception as e:
        print(f"❌ Не удалось загрузить клиент: {e}")
        print("   Убедись что scan_endpoints.py лежит рядом с bot.py")
        sys.exit(1)


# ─── Извлечение эндпоинтов из metathreads ──────────────────────────────────

def extract_from_metathreads() -> list:
    """Парсит исходники metathreads и вытаскивает все API-пути."""
    paths = set()

    # Способ 1: из constants.py (Path enum)
    try:
        from metathreads import constants
        for name in dir(constants):
            obj = getattr(constants, name)
            if isinstance(obj, type):  # enum class
                for member in obj:
                    val = str(member.value) if hasattr(member, 'value') else str(member)
                    if '/api/' in val or val.startswith('/'):
                        paths.add(val)
            elif isinstance(obj, str) and ('/api/' in obj or obj.startswith('/')):
                paths.add(obj)
        print(f"  ✅ metathreads.constants: {len(paths)} путей")
    except Exception as e:
        print(f"  ⚠️ metathreads.constants: {e}")

    # Способ 2: grep по файлам пакета
    try:
        import metathreads
        pkg_dir = os.path.dirname(inspect.getfile(metathreads))
        py_files = glob.glob(os.path.join(pkg_dir, '**/*.py'), recursive=True)
        url_pattern = re.compile(r'''['"](/(?:api/v1|v1)/[a-zA-Z0-9_/{}\-\.]+/?)['"]''')
        for f in py_files:
            try:
                code = open(f, 'r', encoding='utf-8', errors='ignore').read()
                for m in url_pattern.finditer(code):
                    paths.add(m.group(1))
            except Exception:
                pass
        print(f"  ✅ metathreads source grep: всего {len(paths)} путей")
    except ImportError:
        print("  ⚠️ metathreads не установлен — пропуск")
    except Exception as e:
        print(f"  ⚠️ metathreads grep: {e}")

    return list(paths)


def extract_from_instagrapi() -> list:
    """Парсит исходники instagrapi и вытаскивает Threads-related пути."""
    paths = set()
    try:
        import instagrapi
        pkg_dir = os.path.dirname(inspect.getfile(instagrapi))
        py_files = glob.glob(os.path.join(pkg_dir, '**/*.py'), recursive=True)
        url_pattern = re.compile(r'''['"]((?:/api/v1|https?://[^'"]*threads[^'"]*)/[a-zA-Z0-9_/{}\-\.]+/?)['"]''')
        for f in py_files:
            try:
                code = open(f, 'r', encoding='utf-8', errors='ignore').read()
                for m in url_pattern.finditer(code):
                    p = m.group(1)
                    # Нормализуем
                    if p.startswith('http'):
                        from urllib.parse import urlparse
                        parsed = urlparse(p)
                        p = parsed.path
                    paths.add(p)
            except Exception:
                pass
        print(f"  ✅ instagrapi source grep: {len(paths)} путей")
    except ImportError:
        print("  ⚠️ instagrapi не установлен — пропуск")
    except Exception as e:
        print(f"  ⚠️ instagrapi grep: {e}")

    return list(paths)


# ─── Известные эндпоинты ───────────────────────────────────────────────────

KNOWN_ENDPOINTS = {
    # ── Поиск ──
    'GET  /users/search/?q=vpn&count=5': 'Поиск юзеров',
    'GET  /text_feed/recommended_users/?search_query=vpn': 'Рекомендации по запросу',
    'GET  /users/web_profile_info/?username=threads': 'Профиль по username',

    # ── Лента / посты ──
    'GET  /text_feed/{uid}/profile/': 'Посты юзера',
    'GET  /text_feed/{uid}/replies/': 'Ответы на пост',
    'GET  /text_feed/timeline/': 'Домашняя лента',
    'GET  /text_feed/text_app_notifications/': 'Уведомления',

    # ── Действия (POST, signed_body) ──
    'POST /media/{pid}/like/': 'Лайк',
    'POST /media/{pid}/unlike/': 'Анлайк',
    'POST /repost/create_repost/': 'Репост',
    'POST /repost/delete_text_app_repost/': 'Удалить репост',
    'POST /friendships/create/{uid}/': 'Подписаться',
    'POST /friendships/destroy/{uid}/': 'Отписаться',
    'POST /friendships/show/{uid}/': 'Статус подписки',

    # ── Постинг ──
    'POST /media/configure_text_post_app_feed/': 'Создать пост',

    # ── Прочее ──
    'GET  /accounts/current_user/?edit=true': 'Текущий юзер',
    'GET  /friendships/{uid}/followers/': 'Подписчики',
    'GET  /friendships/{uid}/following/': 'Подписки',
    'GET  /text_feed/link_preview/?url=https://example.com': 'Превью ссылки',
    'GET  /qp/batch_fetch/': 'Quick Promotions batch',
    'GET  /text_feed/text_app_settings/': 'Настройки приложения',
}

# ── Дополнительно: те же пути но на i.instagram.com ──
IG_MIRROR_ENDPOINTS = {
    'GET  /users/search/?q=vpn&count=5': 'Поиск (Instagram)',
    'GET  /text_feed/{uid}/profile/': 'Посты (Instagram)',
    'GET  /accounts/current_user/?edit=true': 'Текущий юзер (Instagram)',
}


# ─── Сканер ─────────────────────────────────────────────────────────────────

def scan_endpoint(session: requests.Session, base_url: str,
                  method: str, path: str, uid: str = '', pid: str = '',
                  device_uuid: str = '', timeout: int = 8) -> dict:
    """Тестирует один эндпоинт. Возвращает {status, body, size, time_ms}."""
    # Подставляем переменные
    path = path.replace('{uid}', uid).replace('{pid}', pid)

    # Разделяем path и query params
    if '?' in path:
        path_only, query_str = path.split('?', 1)
        params = dict(p.split('=', 1) for p in query_str.split('&') if '=' in p)
    else:
        path_only = path
        params = None

    url = f'{base_url}{path_only}'

    t0 = time.time()
    try:
        if method == 'GET':
            r = session.get(url, params=params, timeout=timeout, allow_redirects=False)
        else:
            # POST с signed_body
            data = {'signed_body': f'SIGNATURE.{json.dumps({"_uid": uid, "_uuid": device_uuid})}'}
            r = session.post(url, data=data, timeout=timeout, allow_redirects=False)

        elapsed = int((time.time() - t0) * 1000)
        return {
            'status':  r.status_code,
            'body':    r.text[:200],
            'size':    len(r.text),
            'time_ms': elapsed,
        }
    except requests.exceptions.Timeout:
        return {'status': 0, 'body': 'TIMEOUT', 'size': 0, 'time_ms': timeout * 1000}
    except Exception as e:
        return {'status': 0, 'body': str(e)[:200], 'size': 0, 'time_ms': 0}


def main():
    print("=" * 70)
    print("  СКАНЕР ЭНДПОИНТОВ Threads / Instagram API")
    print("=" * 70)
    print()

    # 1. Загружаем клиент
    client, login = load_client_from_bot()
    session = client.session
    uid = client.user_id
    device_uuid = client._device_uuid

    # Находим любой post_id из архива для тестов
    try:
        import storage
        archive = storage.get_archive(5)
        pid = ''
        for item in archive:
            pids = item.get('post_ids', [])
            if pids:
                pid = str(pids[0])
                break
        if pid:
            print(f"   Тестовый post_id: {pid}")
        else:
            print("   ⚠️ Нет постов в архиве — POST-тесты будут ограничены")
    except Exception:
        pid = ''

    print()

    # 2. Собираем эндпоинты
    print("📦 Собираю эндпоинты...")
    mt_paths  = extract_from_metathreads()
    ig_paths  = extract_from_instagrapi()
    print()

    # 3. Сканируем
    all_results = []

    # 3a. Известные эндпоинты на threads.net
    print("🔍 Сканирую threads.net...")
    print("-" * 70)

    for endpoint, desc in KNOWN_ENDPOINTS.items():
        method, path = endpoint.split(None, 1)
        result = scan_endpoint(session, 'https://www.threads.net/api/v1',
                               method, path, uid, pid, device_uuid)
        status = result['status']
        ms     = result['time_ms']

        if status == 200:
            emoji = '✅'
        elif status in (301, 302, 303, 307, 308):
            emoji = '↗️'
        elif status == 0:
            emoji = '⏱'
        else:
            emoji = '❌'

        line = f"  {emoji} {status:>3}  {ms:>4}ms  {method} {path[:50]}  — {desc}"
        print(line)
        all_results.append({
            'base': 'threads.net',
            'method': method,
            'path': path,
            'desc': desc,
            **result,
        })

    print()

    # 3b. Зеркало на i.instagram.com
    print("🔍 Сканирую i.instagram.com...")
    print("-" * 70)

    for endpoint, desc in IG_MIRROR_ENDPOINTS.items():
        method, path = endpoint.split(None, 1)
        result = scan_endpoint(session, 'https://i.instagram.com/api/v1',
                               method, path, uid, pid, device_uuid)
        status = result['status']
        ms     = result['time_ms']

        if status == 200:
            emoji = '✅'
        elif status in (301, 302, 303, 307, 308):
            emoji = '↗️'
        elif status == 0:
            emoji = '⏱'
        else:
            emoji = '❌'

        line = f"  {emoji} {status:>3}  {ms:>4}ms  {method} {path[:50]}  — {desc}"
        print(line)
        all_results.append({
            'base': 'i.instagram.com',
            'method': method,
            'path': path,
            'desc': desc,
            **result,
        })

    print()

    # 3c. Эндпоинты из metathreads (если нашлись новые)
    extra_paths = set()
    for p in mt_paths + ig_paths:
        # Нормализуем
        p = p.strip()
        if not p.startswith('/'):
            continue
        # Убираем /api/v1 prefix если есть
        if p.startswith('/api/v1'):
            p = p[7:]
        if not p.startswith('/'):
            p = '/' + p
        # Проверяем что нет в KNOWN
        is_known = any(p.rstrip('/') in ep for ep in KNOWN_ENDPOINTS)
        if not is_known:
            extra_paths.add(p)

    if extra_paths:
        print(f"🔍 Дополнительные пути из библиотек ({len(extra_paths)})...")
        print("-" * 70)

        for path in sorted(extra_paths):
            # Пробуем только GET
            result = scan_endpoint(session, 'https://www.threads.net/api/v1',
                                   'GET', path, uid, pid, device_uuid)
            status = result['status']
            ms     = result['time_ms']

            if status == 200:
                emoji = '✅'
            elif status in (301, 302, 303, 307, 308):
                emoji = '↗️'
            elif status == 0:
                emoji = '⏱'
            else:
                emoji = '❌'

            print(f"  {emoji} {status:>3}  {ms:>4}ms  GET {path[:60]}")
            all_results.append({
                'base': 'threads.net',
                'method': 'GET',
                'path': path,
                'desc': 'auto-discovered',
                **result,
            })

        print()

    # 4. Итог
    ok_count = sum(1 for r in all_results if r['status'] == 200)
    redirect = sum(1 for r in all_results if r['status'] in (301, 302, 303, 307, 308))
    err      = sum(1 for r in all_results if r['status'] >= 400)
    timeout  = sum(1 for r in all_results if r['status'] == 0)

    print("=" * 70)
    print(f"  ИТОГО: {len(all_results)} эндпоинтов")
    print(f"  ✅ 200 OK:     {ok_count}")
    print(f"  ↗️  Redirect:   {redirect}")
    print(f"  ❌ Error:      {err}")
    print(f"  ⏱  Timeout:    {timeout}")
    print("=" * 70)

    # 5. Сохраняем отчёт
    report_file = 'endpoints_report.txt'
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write("ENDPOINTS SCAN REPORT\n")
        f.write(f"Account: {login}  |  user_id: {uid}\n")
        f.write(f"Total: {len(all_results)}  |  OK: {ok_count}\n\n")

        for r in sorted(all_results, key=lambda x: (-int(x['status'] == 200), x['path'])):
            f.write(f"[{r['status']:>3}] {r['time_ms']:>4}ms  {r['base']}  {r['method']} {r['path']}\n")
            if r['status'] == 200:
                f.write(f"       {r['body'][:150]}\n")
            elif r.get('body') and r['body'] != 'TIMEOUT':
                f.write(f"       {r['body'][:100]}\n")
            f.write('\n')

    print(f"\n📄 Отчёт сохранён: {report_file}")

    # 6. Показываем работающие подробно
    working = [r for r in all_results if r['status'] == 200]
    if working:
        print(f"\n🎯 РАБОЧИЕ ЭНДПОИНТЫ ({len(working)}):\n")
        for r in working:
            print(f"  {r['base']}  {r['method']} {r['path']}")
            body = r['body'][:120].replace('\n', ' ')
            print(f"    → {body}")
            print()


if __name__ == '__main__':
    main()
