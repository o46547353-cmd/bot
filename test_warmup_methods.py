"""
test_warmup_methods.py — проверка всех методов прогрева по одному.

Запуск:
    python test_warmup_methods.py

Что делает:
    1. Загружает аккаунт из БД
    2. Тестирует search_users → get_user_threads → like → follow → repost
    3. Каждый шаг с паузой и логами — видно что работает, что нет
    4. НЕ делает массовых действий — 1 лайк, 1 фоллоу максимум

После теста посмотри вывод:
    ✅ = метод работает
    ❌ = ошибка (покажет детали)
    ⚠️ = метод вернул пустой результат (может быть ок если нет данных)
"""

import os, sys, time, logging
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    format='%(asctime)s %(levelname)s %(message)s',
    level=logging.DEBUG  # DEBUG чтобы видеть HTTP-запросы
)
logger = logging.getLogger('test')

import storage
import threads_api

# ── Настройки теста ──────────────────────────────────────────────────────────

TEST_KEYWORD = 'vpn'           # ключевое слово для поиска
DO_LIKE      = True            # лайкнуть 1 пост?
DO_FOLLOW    = False           # подписаться на 1 юзера? (False = безопасно)
DO_REPOST    = False           # репостнуть 1 пост? (False = безопасно)
PAUSE_SEC    = 3               # пауза между действиями


def main():
    print("\n" + "="*60)
    print("  ТЕСТ МЕТОДОВ ПРОГРЕВА — slash_threads_client")
    print("="*60 + "\n")

    # Шаг 0: загрузка аккаунтов
    print("📦 Загрузка аккаунтов из БД...")
    threads_api.load_accounts_from_db()
    accounts = threads_api.list_accounts()

    if not accounts:
        print("❌ Нет аккаунтов в БД. Сначала добавь через бот.")
        sys.exit(1)

    login = accounts[0]
    entry = threads_api._clients[login]
    client = entry['client']
    print(f"✅ Аккаунт: {login}")
    print(f"   Клиент: {client}")
    print()

    results = {}

    # ── Тест 1: search_users ────────────────────────────────────────────────
    print(f"🔍 [1/6] search_users('{TEST_KEYWORD}')...")
    try:
        users = threads_api.search_users(TEST_KEYWORD, login)
        if users:
            print(f"   ✅ Найдено {len(users)} пользователей")
            for u in users[:3]:
                uname = u.get('username', '?')
                uid   = u.get('pk') or u.get('id', '?')
                print(f"      @{uname} (pk={uid})")
            results['search_users'] = '✅'
        else:
            print(f"   ⚠️ Пустой результат (0 пользователей)")
            results['search_users'] = '⚠️'
    except Exception as e:
        print(f"   ❌ Ошибка: {e}")
        results['search_users'] = f'❌ {e}'

    time.sleep(PAUSE_SEC)

    # ── Тест 2: get_user_threads ────────────────────────────────────────────
    test_user_id = None
    test_post_id = None

    if users:
        target = users[0]
        test_user_id = str(target.get('pk') or target.get('id', ''))
        target_name  = target.get('username', '?')

        print(f"\n📋 [2/6] get_user_threads('{test_user_id}') [@{target_name}]...")
        try:
            posts = threads_api.get_user_threads(test_user_id, login)
            if posts:
                print(f"   ✅ Найдено {len(posts)} постов")
                for p in posts[:3]:
                    pid  = p.get('pk') or p.get('id', '?')
                    text = ''
                    cap  = p.get('caption')
                    if isinstance(cap, dict):
                        text = cap.get('text', '')[:60]
                    elif isinstance(cap, str):
                        text = cap[:60]
                    print(f"      pk={pid}: {text}...")
                test_post_id = str(posts[0].get('pk') or posts[0].get('id', ''))
                results['get_user_threads'] = '✅'
            else:
                print(f"   ⚠️ Пустой результат (0 постов)")
                results['get_user_threads'] = '⚠️'
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
            results['get_user_threads'] = f'❌ {e}'
    else:
        print("\n📋 [2/6] get_user_threads — ПРОПУСК (нет юзеров из поиска)")
        results['get_user_threads'] = '⏭ пропуск'

    time.sleep(PAUSE_SEC)

    # ── Тест 3: like ────────────────────────────────────────────────────────
    if test_post_id and DO_LIKE:
        print(f"\n❤️ [3/6] like_thread('{test_post_id}')...")
        try:
            ok = threads_api.like_thread(test_post_id, login)
            if ok:
                print(f"   ✅ Лайк поставлен")
                results['like'] = '✅'
            else:
                print(f"   ⚠️ Вернул False")
                results['like'] = '⚠️'
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
            results['like'] = f'❌ {e}'
    else:
        reason = 'нет поста' if not test_post_id else 'DO_LIKE=False'
        print(f"\n❤️ [3/6] like — ПРОПУСК ({reason})")
        results['like'] = '⏭ пропуск'

    time.sleep(PAUSE_SEC)

    # ── Тест 4: follow ──────────────────────────────────────────────────────
    if test_user_id and DO_FOLLOW:
        print(f"\n👤 [4/6] follow_user('{test_user_id}')...")
        try:
            ok = threads_api.follow_user(test_user_id, login)
            if ok:
                print(f"   ✅ Подписка оформлена")
                results['follow'] = '✅'
            else:
                print(f"   ⚠️ Вернул False")
                results['follow'] = '⚠️'
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
            results['follow'] = f'❌ {e}'
    else:
        reason = 'нет юзера' if not test_user_id else 'DO_FOLLOW=False'
        print(f"\n👤 [4/6] follow — ПРОПУСК ({reason})")
        results['follow'] = '⏭ пропуск'

    time.sleep(PAUSE_SEC)

    # ── Тест 5: repost ─────────────────────────────────────────────────────
    if test_post_id and DO_REPOST:
        print(f"\n🔁 [5/6] repost_thread('{test_post_id}')...")
        try:
            ok = threads_api.repost_thread(test_post_id, login)
            if ok:
                print(f"   ✅ Репост сделан")
                results['repost'] = '✅'
            else:
                print(f"   ⚠️ Вернул False")
                results['repost'] = '⚠️'
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
            results['repost'] = f'❌ {e}'
    else:
        reason = 'нет поста' if not test_post_id else 'DO_REPOST=False'
        print(f"\n🔁 [5/6] repost — ПРОПУСК ({reason})")
        results['repost'] = '⏭ пропуск'

    time.sleep(PAUSE_SEC)

    # ── Тест 6: get_thread_replies + stats ──────────────────────────────────
    if test_post_id:
        print(f"\n💬 [6/6] get_thread_replies + get_thread_stats('{test_post_id}')...")
        try:
            replies = threads_api.get_thread_replies(test_post_id, login)
            print(f"   Ответов: {len(replies)}")
            results['get_replies'] = '✅' if isinstance(replies, list) else '⚠️'
        except Exception as e:
            print(f"   ❌ replies: {e}")
            results['get_replies'] = f'❌ {e}'

        try:
            stats = threads_api.get_thread_stats(test_post_id, login)
            print(f"   Статистика: {stats}")
            results['get_stats'] = '✅' if stats else '⚠️'
        except Exception as e:
            print(f"   ❌ stats: {e}")
            results['get_stats'] = f'❌ {e}'
    else:
        print(f"\n💬 [6/6] replies/stats — ПРОПУСК (нет поста)")
        results['get_replies'] = '⏭ пропуск'
        results['get_stats']   = '⏭ пропуск'

    # ── Итог ────────────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("  РЕЗУЛЬТАТЫ")
    print("="*60)
    for method, status in results.items():
        print(f"  {status}  {method}")
    print()

    ok_count = sum(1 for v in results.values() if v == '✅')
    total    = len(results)
    if ok_count == total:
        print("🎉 Всё работает! Прогрев можно включать.")
    elif ok_count > 0:
        print(f"⚡ {ok_count}/{total} работает. Проверь ошибки выше.")
    else:
        print("💀 Ничего не работает. Проверь авторизацию.")


if __name__ == '__main__':
    main()
