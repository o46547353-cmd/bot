### bot.py  —  v2: SlashThreadsClient (metathreads removed)
# Auth: instagrapi | Operations: slash_threads_client.py

import os, asyncio, logging, random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler,
                           ContextTypes, filters, ConversationHandler)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
import ai_gen, storage, threads_api, warmup, monitor
from threads_auth import TwoFactorRequired

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
load_dotenv()

BOT_TOKEN     = os.environ['BOT_TOKEN']
ADMIN_IDS_RAW = os.environ.get('ADMIN_IDS', '')
ADMIN_IDS     = [int(x.strip()) for x in ADMIN_IDS_RAW.split(',') if x.strip()]

scheduler          = AsyncIOScheduler()
_scheduler_started = False   # BUG-16 FIX

# ─── Состояния ───────────────────────────────────────────────────────────────
WAIT_2FA = 1
WAIT_MANUAL_LOGIN, WAIT_MANUAL_SESSION, WAIT_MANUAL_CSRF          = 10, 11, 12
WAIT_IMAGE_LOGIN, WAIT_PHOTO                                       = 20, 21
WAIT_SETUP_LOGIN, WAIT_SETUP_KEYWORDS, WAIT_SETUP_PRESET, WAIT_SETUP_PROMPTS = 30, 31, 32, 33
# Новые состояния: редактирование промптов и картинки прямо из меню аккаунта
WAIT_EDIT_ACCOUNT_PROMPT = 50
WAIT_EDIT_TOPIC_PROMPT   = 51
WAIT_PHOTO_DIRECT        = 52


def is_admin(upd):
    if not ADMIN_IDS:
        return True
    return upd.effective_user.id in ADMIN_IDS


def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Аккаунты",   callback_data="menu:accounts"),
         InlineKeyboardButton("📋 Очередь",    callback_data="menu:queue")],
        [InlineKeyboardButton("🚀 Автопилот",  callback_data="menu:autopilot"),
         InlineKeyboardButton("📊 Статистика", callback_data="menu:stats")],
        [InlineKeyboardButton("⚙️ Настройки",  callback_data="menu:settings"),
         InlineKeyboardButton("🔍 Статус",     callback_data="menu:status")],
    ])


HELP_TEXT = (
    "🔒 *SLASH VPN Bot — все команды*\n\n"
    "━━━━━━━━━ 👤 *Аккаунты* ━━━━━━━━━\n"
    "/add\\_account `login password`\n"
    "  Добавить аккаунт через логин и пароль\n\n"
    "/manual\\_cookies\n"
    "  Добавить через cookies из браузера\n"
    "  _(F12 → Application → Cookies → threads.net → sessionid и csrftoken)_\n\n"
    "━━━━━━━━━ 🖼 *Картинка* ━━━━━━━━━\n"
    "/kartinka\n"
    "  Загрузить картинку для аккаунта\n"
    "  Бот спросит логин аккаунта, потом отправь фото как *фото* (не файл)\n"
    "  Картинка прикрепляется к 3-му посту серии\n\n"
    "━━━━━━━━━ ⚙️ *Настройка аккаунта* ━━━━━━━━━\n"
    "/setup\n"
    "  Интерактивная настройка: ключевые слова, пресет прогрева\n\n"
    "/prompt\\_account\n"
    "  Изменить системный промпт *(как AI пишет посты)*\n"
    "  Инструкция для AI — стиль, продукт, CTA, тарифы\n\n"
    "/prompt\\_topic\n"
    "  Изменить промпт для генерации тем постов\n\n"
    "/show\\_prompts\n"
    "  Показать текущие промпты аккаунта\n\n"
    "━━━━━━━━━ 📝 *Посты* ━━━━━━━━━\n"
    "/seriya `login тема`\n"
    "  Сгенерировать серию по теме\n"
    "  Пример: `/seriya mylogin Блокировки YouTube 2025`\n\n"
    "/interval `часы` — интервал автопостинга\n\n"
    "━━━━━━━━━ 🎛 *Управление* ━━━━━━━━━\n"
    "/start  — главное меню\n"
    "/help   — эта справка\n"
    "/cancel — отменить текущий диалог\n\n"
    "━━━━━━━━━ 💡 *Подсказки* ━━━━━━━━━\n"
    "• Прогрев и автопостинг включаются в меню *Автопилот*\n"
    "• Картинка прикрепляется к посту 3 (с тарифами и CTA)\n"
    "• 🔑✅ API подключён — постинг, прогрев, картинки работают\n"
    "• 🔑❌ Нет сессии — добавь: `/add\\_account логин ПАРОЛЬ`"
)


async def cmd_start(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await upd.message.reply_text(
        "🔒 *SLASH VPN Bot*\n\nВыбери раздел или /help для справки:",
        parse_mode='Markdown', reply_markup=kb_main()
    )


async def cmd_help(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await upd.message.reply_text(
        HELP_TEXT,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")
        ]])
    )


# ─── Меню ────────────────────────────────────────────────────────────────────

async def cb_menu(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = upd.callback_query
    await q.answer()
    dest = q.data.split(':')[1]
    if dest == 'accounts':  await _show_accounts(q)
    elif dest == 'queue':   await _show_queue(q)
    elif dest == 'autopilot': await _show_autopilot(q)
    elif dest == 'stats':   await _show_stats(q)
    elif dest == 'settings': await _show_settings(q)
    elif dest == 'status':  await _show_status(q)
    elif dest == 'main':
        await q.edit_message_text("🔒 *SLASH VPN Bot*\n\nВыбери раздел:",
                                  parse_mode='Markdown', reply_markup=kb_main())


# ─── Аккаунты ────────────────────────────────────────────────────────────────

async def _show_accounts(q):
    accs = threads_api.list_accounts()
    if not accs:
        await q.edit_message_text(
            "Нет аккаунтов.\nДобавь через /add\\_account или /manual\\_cookies",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Добавить аккаунт", callback_data="acc:add_info")],
                [InlineKeyboardButton("◀️ Назад", callback_data="menu:main")],
            ])
        )
        return
    lines = []
    for a in accs:
        acc = storage.get_account(a)
        wp  = "🟢" if acc and acc.get('warmup_active')  else "⚫"
        ap  = "🟢" if acc and acc.get('autopost_active') else "⚫"
        q_c = storage.count(a)
        lines.append(f"{wp} прогрев  {ap} постинг  📋{q_c}\n*@{acc.get('username', a)}*")
    rows = [[InlineKeyboardButton(f"⚙️ {a}", callback_data=f"acc:manage:{a}")] for a in accs]
    rows.append([InlineKeyboardButton("➕ Добавить", callback_data="acc:add_info"),
                 InlineKeyboardButton("◀️ Назад",   callback_data="menu:main")])
    await q.edit_message_text("\n\n".join(lines), parse_mode='Markdown',
                              reply_markup=InlineKeyboardMarkup(rows))


async def cb_acc(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = upd.callback_query
    await q.answer()
    parts  = q.data.split(':')
    action = parts[1]

    if action == 'add_info':
        await q.edit_message_text(
            "Как добавить аккаунт:\n\n"
            "*/add\\_account login password* — через логин и пароль\n"
            "*/manual\\_cookies* — через cookies из браузера",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu:accounts")]])
        )

    elif action == 'manage':
        login = parts[2]
        acc   = storage.get_account(login)
        if not acc:
            await q.edit_message_text("Аккаунт не найден."); return
        wp    = "🟢 Вкл" if acc.get('warmup_active')  else "⚫ Выкл"
        ap    = "🟢 Вкл" if acc.get('autopost_active') else "⚫ Выкл"
        img   = "🖼✅" if storage.get_image(login) else "🖼❌"
        try:
            cl_active = threads_api.get_client(login)['client'] is not None
        except Exception:
            cl_active = False
        cl_status = "🔑✅" if cl_active else "🔑❌"
        has_ap = bool((acc.get('account_prompt') or '').strip())
        has_tp = bool((acc.get('topic_prompt') or '').strip())
        text = (
            f"*@{acc.get('username', login)}*\n\n"
            f"Прогрев: {wp}  |  Автопостинг: {ap}\n"
            f"{img} Картинка  {cl_status} API\n"
            f"📝 Системный промпт: {'✅' if has_ap else '⚫ дефолтный'}\n"
            f"💡 Промпт тем: {'✅' if has_tp else '⚫ дефолтный'}\n\n"
            f"В очереди: {storage.count(login)}  |  Пресет: {acc.get('warmup_preset', 'A')}\n"
            f"Ключевые слова: `{acc.get('warmup_keywords', '—')}`"
        )
        toggle_w = "⏹ Стоп прогрев"  if acc.get('warmup_active')  else "▶️ Старт прогрев"
        toggle_a = "⏹ Стоп постинг"  if acc.get('autopost_active') else "▶️ Старт постинг"
        # Кнопка обновления токена
        show_refresh = cl_active or (acc.get('auth_type') == 'instagrapi')
        rows = [
            [InlineKeyboardButton(toggle_w, callback_data=f"acc:toggle_w:{login}"),
             InlineKeyboardButton(toggle_a, callback_data=f"acc:toggle_a:{login}")],
            [InlineKeyboardButton("🎲 Авто-серия",  callback_data=f"acc:autoseriya:{login}"),
             InlineKeyboardButton("📋 Очередь",     callback_data=f"acc:queue:{login}")],
            [InlineKeyboardButton("▶️ Пост сейчас", callback_data=f"acc:postnow:{login}")],
            [InlineKeyboardButton("🖼 Загрузить картинку",     callback_data=f"acc:upload_img:{login}")],
        ]
        rows.append([InlineKeyboardButton("🧪 Тест прогрева",   callback_data=f"acc:test_warmup:{login}"),
                     InlineKeyboardButton("🔬 Диагностика",     callback_data=f"acc:debug_api:{login}")])
        if show_refresh:
            rows.append([InlineKeyboardButton("🔄 Обновить токен",  callback_data=f"acc:refresh_token:{login}")])
        rows += [
            [InlineKeyboardButton("✏️ Системный промпт",       callback_data=f"acc:edit_aprompt:{login}"),
             InlineKeyboardButton("💡 Промпт тем",             callback_data=f"acc:edit_tprompt:{login}")],
            [InlineKeyboardButton("📋 Показать промпты",       callback_data=f"acc:show_prompts:{login}")],
            [InlineKeyboardButton("◀️ Назад",                  callback_data="menu:accounts")],
        ]
        await q.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(rows))

    elif action == 'toggle_w':
        login = parts[2]; acc = storage.get_account(login)
        storage.set_warmup_active(login, not bool(acc.get('warmup_active') if acc else False))
        await cb_acc(upd, ctx)

    elif action == 'toggle_a':
        login = parts[2]; acc = storage.get_account(login)
        storage.set_autopost_active(login, not bool(acc.get('autopost_active') if acc else False))
        await cb_acc(upd, ctx)

    elif action == 'autoseriya':
        login = parts[2]
        await q.edit_message_text(f"⏳ Генерирую для *{login}*...", parse_mode='Markdown')
        try:
            topic  = await asyncio.to_thread(ai_gen.generate_topic, login)
            series = await asyncio.to_thread(ai_gen.generate_series, topic, login)
            storage.add_series(series, login)
            await q.edit_message_text(
                f"✅ Тема: *{topic}*\nОчередь: {storage.count(login)}",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("▶️ Опубликовать сейчас", callback_data=f"acc:postnow:{login}"),
                    InlineKeyboardButton("◀️ Назад", callback_data=f"acc:manage:{login}"),
                ]])
            )
        except Exception as e:
            await q.edit_message_text(f"❌ {e}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"acc:manage:{login}")]]))

    elif action == 'postnow':
        login = parts[2]
        await q.edit_message_text(f"⏳ Публикую для *{login}*...", parse_mode='Markdown')
        try:
            await _do_post(login)
            await q.edit_message_text("✅ Серия опубликована!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К аккаунту", callback_data=f"acc:manage:{login}")]]))
        except Exception as e:
            await q.edit_message_text(f"❌ {e}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"acc:manage:{login}")]]))

    elif action == 'queue':
        login = parts[2]
        items = storage.get_queue(login)
        if not items:
            await q.edit_message_text(f"Очередь для *{login}* пуста.", parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎲 Добавить серию", callback_data=f"acc:autoseriya:{login}"),
                    InlineKeyboardButton("◀️ Назад", callback_data=f"acc:manage:{login}"),
                ]]))
            return
        lines = [f"📋 *Очередь @{login}* ({len(items)})\n"]
        for i, it in enumerate(items[:8]):
            lines.append(f"{i+1}. {it['topic']} ({it['added_at'][:10]})")
        await q.edit_message_text("\n".join(lines), parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("▶️ Опубликовать первую", callback_data=f"acc:postnow:{login}"),
                InlineKeyboardButton("◀️ Назад", callback_data=f"acc:manage:{login}"),
            ]]))

    elif action == 'upload_img':
        # Просим отправить фото прямо в этот чат — сохраняем login и ждём фото
        login = parts[2]
        ctx.user_data['img_login'] = login
        await q.edit_message_text(
            f"📸 *Загрузка картинки для @{login}*\n\n"
            f"Отправь фото прямо сюда как *фото* (не файл/документ).\n\n"
            f"❗ Именно через скрепку → *Фото*, иначе Telegram сожмёт качество неправильно.\n\n"
            f"/cancel — отмена",
            parse_mode='Markdown'
        )
        ctx.user_data['_waiting_photo_for'] = login

    elif action == 'edit_aprompt':
        login = parts[2]
        acc   = storage.get_account(login)
        cur   = (acc.get('account_prompt') or '').strip() if acc else ''
        ctx.user_data['edit_prompt_login'] = login
        preview = (cur[:400] + '...') if len(cur) > 400 else cur
        await q.edit_message_text(
            f"✏️ *Системный промпт для @{login}*\n\n"
            f"Это инструкция для AI — как писать посты, стиль, CTA, тарифы.\n\n"
            f"{'*Текущий:*\n`' + preview + '`' if cur else '*Сейчас:* используется дефолтный SLASH VPN'}\n\n"
            f"Напиши новый промпт и отправь сообщением.\n"
            f"Или отправь `-` чтобы сбросить на дефолтный.\n\n"
            f"/cancel — отмена",
            parse_mode='Markdown'
        )
        ctx.user_data['_conv_state'] = 'edit_account_prompt'

    elif action == 'edit_tprompt':
        login = parts[2]
        acc   = storage.get_account(login)
        cur   = (acc.get('topic_prompt') or '').strip() if acc else ''
        ctx.user_data['edit_prompt_login'] = login
        await q.edit_message_text(
            f"💡 *Промпт тем для @{login}*\n\n"
            f"Это инструкция для AI — о чём придумывать темы постов.\n\n"
            f"{'*Текущий:*\n`' + cur + '`' if cur else '*Сейчас:* дефолтный (VPN, блокировки, слежка, скорость)'}\n\n"
            f"Напиши новый промпт и отправь сообщением.\n"
            f"Или отправь `-` чтобы сбросить на дефолтный.\n\n"
            f"/cancel — отмена",
            parse_mode='Markdown'
        )
        ctx.user_data['_conv_state'] = 'edit_topic_prompt'

    elif action == 'refresh_token':
        login = parts[2]
        acc   = storage.get_account(login)
        if not acc:
            await q.edit_message_text("Аккаунт не найден."); return
        ctx.user_data['_conv_state']         = 'refresh_token'
        ctx.user_data['refresh_token_login'] = login
        await q.edit_message_text(
            f"🔄 *Обновление сессии для @{acc.get('username', login)}*\n\n"
            f"Отправь пароль от Instagram-аккаунта `{login}`.\n\n"
            f"Бот перелогинится через instagrapi — "
            f"все функции (лайки, прогрев, репосты, картинки) снова заработают.\n\n"
            f"⚠️ Пароль нигде не сохраняется — используется только для получения сессии.\n\n"
            f"/cancel — отмена",
            parse_mode='Markdown'
        )

    elif action == 'show_prompts':
        login = parts[2]
        acc   = storage.get_account(login)
        if not acc:
            await q.edit_message_text("Аккаунт не найден."); return
        ap = (acc.get('account_prompt') or '').strip()
        tp = (acc.get('topic_prompt') or '').strip()
        text = f"📋 *Промпты @{acc.get('username', login)}*\n\n"
        text += "*📝 Системный промпт (как писать посты):*\n"
        text += f"`{ap[:600]}`{'...' if len(ap) > 600 else ''}\n" if ap else "_используется дефолтный SLASH VPN_\n"
        text += "\n*💡 Промпт тем (о чём писать):*\n"
        text += f"`{tp[:300]}`{'...' if len(tp) > 300 else ''}" if tp else "_используется дефолтный (VPN, блокировки, слежка)_"
        await q.edit_message_text(text, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Изменить системный промпт", callback_data=f"acc:edit_aprompt:{login}")],
                [InlineKeyboardButton("💡 Изменить промпт тем",       callback_data=f"acc:edit_tprompt:{login}")],
                [InlineKeyboardButton("◀️ К аккаунту",                callback_data=f"acc:manage:{login}")],
            ]))

    elif action == 'test_warmup':
        login = parts[2]
        await q.edit_message_text(
            f"🧪 *Тест прогрева @{login}*\n\n⏳ Запускаю проверку...\n"
            f"search → threads → like → replies → stats",
            parse_mode='Markdown'
        )
        asyncio.ensure_future(_run_warmup_test(q, login))

    elif action == 'debug_api':
        login = parts[2]
        await q.edit_message_text(
            f"🔬 *Диагностика API @{login}*\n\n⏳ Тестирую эндпоинты...",
            parse_mode='Markdown'
        )
        asyncio.ensure_future(_run_api_debug(q, login))


async def _run_warmup_test(q, login: str):
    """Прогоняет все методы прогрева по одному и шлёт отчёт."""
    import time as _time

    results = {}
    details = []
    test_kw = 'vpn'

    acc = storage.get_account(login)
    if acc:
        raw_kw = acc.get('warmup_keywords', '')
        if raw_kw:
            first_kw = [k.strip() for k in raw_kw.split(',') if k.strip()]
            if first_kw:
                test_kw = first_kw[0]

    # 1. search_users
    users = []
    try:
        users = await asyncio.to_thread(threads_api.search_users, test_kw, login)
        if users:
            results['🔍 search'] = f'✅ {len(users)} юзеров'
            names = ', '.join(f"@{u.get('username','?')}" for u in users[:3])
            details.append(f"Поиск «{test_kw}»: {names}")
        else:
            results['🔍 search'] = '⚠️ пусто'
    except Exception as e:
        results['🔍 search'] = f'❌ {str(e)[:60]}'

    await asyncio.sleep(3)

    # 2. get_user_threads
    test_user_id = None
    test_post_id = None
    posts = []

    if users:
        target = users[0]
        test_user_id = str(target.get('pk') or target.get('id', ''))
        target_name  = target.get('username', '?')
        try:
            posts = await asyncio.to_thread(threads_api.get_user_threads, test_user_id, login)
            if posts:
                results['📋 threads'] = f'✅ {len(posts)} постов'
                test_post_id = str(posts[0].get('pk') or posts[0].get('id', ''))
                details.append(f"@{target_name}: {len(posts)} постов")
            else:
                results['📋 threads'] = '⚠️ пусто'
        except Exception as e:
            results['📋 threads'] = f'❌ {str(e)[:60]}'
    else:
        results['📋 threads'] = '⏭ пропуск'

    await asyncio.sleep(3)

    # 3. like
    if test_post_id:
        try:
            ok = await asyncio.to_thread(threads_api.like_thread, test_post_id, login)
            results['❤️ like'] = '✅' if ok else '⚠️ False'
            if ok:
                details.append(f"Лайк: pk={test_post_id}")
        except Exception as e:
            results['❤️ like'] = f'❌ {str(e)[:60]}'
    else:
        results['❤️ like'] = '⏭ нет поста'

    await asyncio.sleep(3)

    # 4. get_thread_replies
    if test_post_id:
        try:
            replies = await asyncio.to_thread(threads_api.get_thread_replies, test_post_id, login)
            results['💬 replies'] = f'✅ {len(replies)} ответов'
        except Exception as e:
            results['💬 replies'] = f'❌ {str(e)[:60]}'
    else:
        results['💬 replies'] = '⏭ нет поста'

    await asyncio.sleep(2)

    # 5. get_thread_stats
    if test_post_id:
        try:
            stats = await asyncio.to_thread(threads_api.get_thread_stats, test_post_id, login)
            if stats:
                results['📊 stats'] = f"✅ ❤️{stats.get('likes',0)} 💬{stats.get('replies',0)} 🔁{stats.get('reposts',0)}"
            else:
                results['📊 stats'] = '⚠️ пусто'
        except Exception as e:
            results['📊 stats'] = f'❌ {str(e)[:60]}'
    else:
        results['📊 stats'] = '⏭ нет поста'

    # Собираем отчёт
    ok_count  = sum(1 for v in results.values() if '✅' in v)
    total     = len(results)
    all_good  = ok_count == total

    lines = [f"🧪 *Тест прогрева @{login}*\n"]
    for method, status in results.items():
        lines.append(f"{method}: {status}")

    lines.append('')
    if all_good:
        lines.append("🎉 *Всё работает!* Прогрев можно включать.")
    elif ok_count > 0:
        lines.append(f"⚡ *{ok_count}/{total}* методов работают.")
    else:
        lines.append("💀 Ничего не работает. Проверь авторизацию.")

    if details:
        lines.append(f"\n_Детали: {'; '.join(details)}_")

    kb = []
    if all_good and acc and not acc.get('warmup_active'):
        kb.append([InlineKeyboardButton("▶️ Включить прогрев", callback_data=f"acc:toggle_w:{login}")])
    kb.append([InlineKeyboardButton("◀️ К аккаунту", callback_data=f"acc:manage:{login}")])

    try:
        await q.edit_message_text('\n'.join(lines), parse_mode='Markdown',
                                   reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        pass


async def _run_api_debug(q, login: str):
    """Диагностика: сырые HTTP-ответы от разных эндпоинтов."""
    try:
        entry  = threads_api.get_client(login)
        client = entry['client']
    except Exception as e:
        await q.edit_message_text(f"❌ Нет клиента: {e}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"acc:manage:{login}")]]))
        return

    if not client:
        await q.edit_message_text("❌ SlashThreadsClient = None",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"acc:manage:{login}")]]))
        return

    # Запускаем debug_search
    search_results = await asyncio.to_thread(client.debug_search, 'vpn')

    # Запускаем debug_feed (свой профиль)
    feed_results = await asyncio.to_thread(client.debug_feed)

    # Собираем отчёт
    lines = [f"🔬 *Диагностика API @{login}*\n"]

    # Auth info
    auth = search_results.pop('_auth', {})
    lines.append(f"*Auth:*")
    lines.append(f"  Bearer: {'✅' if auth.get('has_bearer') else '❌'}")
    lines.append(f"  Cookie: {'✅' if auth.get('has_session') else '❌'}")
    lines.append(f"  CSRF: {'✅' if auth.get('has_csrf') else '❌'}")
    lines.append(f"  user\\_id: `{auth.get('user_id', '?')}`")
    lines.append('')

    # Search endpoints
    for name, data in search_results.items():
        status = data.get('status', '?')
        body   = data.get('body', '')[:150]
        # Escape markdown
        body = body.replace('`', "'").replace('*', '').replace('_', '')
        emoji = '✅' if status == 200 else '❌'
        lines.append(f"{emoji} *{name}*")
        lines.append(f"  HTTP {status}")
        lines.append(f"  `{body}`")
        lines.append('')

    # Feed
    for name, data in feed_results.items():
        status = data.get('status', '?')
        body   = data.get('body', '')[:150]
        body = body.replace('`', "'").replace('*', '').replace('_', '')
        emoji = '✅' if status == 200 else '❌'
        lines.append(f"{emoji} *{name}*")
        lines.append(f"  HTTP {status}")
        lines.append(f"  `{body}`")
        lines.append('')

    text = '\n'.join(lines)
    # Telegram limit 4096
    if len(text) > 4000:
        text = text[:4000] + '\n...'

    try:
        await q.edit_message_text(text, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Повторить", callback_data=f"acc:debug_api:{login}")],
                [InlineKeyboardButton("◀️ К аккаунту", callback_data=f"acc:manage:{login}")],
            ]))
    except Exception as e:
        # Если Markdown сломался — шлём без форматирования
        try:
            await q.edit_message_text(text[:4000], parse_mode=None,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ К аккаунту", callback_data=f"acc:manage:{login}")]
                ]))
        except Exception:
            pass


# ─── Автопилот ───────────────────────────────────────────────────────────────

async def _show_autopilot(q):
    accs = threads_api.list_accounts()
    if not accs:
        await q.edit_message_text("Нет аккаунтов.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu:main")]])); return
    lines = ["🚀 *Автопилот*\n"]
    rows  = []
    for a in accs:
        acc = storage.get_account(a)
        wp  = "🟢" if acc and acc.get('warmup_active')  else "⚫"
        ap  = "🟢" if acc and acc.get('autopost_active') else "⚫"
        lines.append(f"{wp} прогрев {ap} постинг — *@{acc.get('username', a)}*")
        rows.append([
            InlineKeyboardButton(f"{'⏹' if acc and acc.get('warmup_active') else '▶️'} прогрев {a}", callback_data=f"ap:w:{a}"),
            InlineKeyboardButton(f"{'⏹' if acc and acc.get('autopost_active') else '▶️'} постинг {a}", callback_data=f"ap:a:{a}"),
        ])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="menu:main")])
    await q.edit_message_text("\n".join(lines), parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(rows))


async def cb_autopilot(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    parts = q.data.split(':'); login = parts[2]; acc = storage.get_account(login)
    if parts[1] == 'w': storage.set_warmup_active(login, not bool(acc.get('warmup_active') if acc else False))
    elif parts[1] == 'a': storage.set_autopost_active(login, not bool(acc.get('autopost_active') if acc else False))
    await _show_autopilot(q)


# ─── Статистика ──────────────────────────────────────────────────────────────

async def _show_stats(q):
    accs = threads_api.list_accounts()
    if not accs:
        await q.edit_message_text("Нет аккаунтов.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu:main")]])); return
    rows = [[InlineKeyboardButton(f"📊 @{a}", callback_data=f"stats:show:{a}")] for a in accs]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="menu:main")])
    await q.edit_message_text("📊 *Статистика*\nВыбери аккаунт:", parse_mode='Markdown',
                              reply_markup=InlineKeyboardMarkup(rows))


async def cb_stats(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    login = q.data.split(':')[2]
    stats = storage.get_post_stats(login)
    if not stats:
        await q.edit_message_text(f"Нет данных для *@{login}*", parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu:stats")]])); return
    lines = [f"📊 *@{login}*\n"]
    for s in stats[:6]:
        lines.append(f"_{s['topic'][:35]}_\n❤️{s['likes']} 💬{s['replies']} 🔁{s['reposts']} ·{s['hours_after']}ч\n")
    await q.edit_message_text("\n".join(lines), parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Обновить", callback_data=f"stats:show:{login}"),
            InlineKeyboardButton("◀️ Назад",    callback_data="menu:stats"),
        ]]))


# ─── Настройки ───────────────────────────────────────────────────────────────

async def _show_settings(q):
    interval = storage.get_setting('interval_hours', '4')
    rows = [[InlineKeyboardButton(f"⚙️ Настроить @{a}", callback_data=f"settings:setup:{a}")] for a in threads_api.list_accounts()]
    rows.append([InlineKeyboardButton(f"⏱ Интервал: {interval}ч", callback_data="settings:interval")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="menu:main")])
    await q.edit_message_text("⚙️ *Настройки*\n\nВыбери аккаунт для настройки:", parse_mode='Markdown',
                              reply_markup=InlineKeyboardMarkup(rows))


async def cb_settings(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    parts = q.data.split(':')
    if parts[1] == 'interval':
        await q.edit_message_text("Укажи интервал через команду:\n`/interval 4`", parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu:settings")]]))
    elif parts[1] == 'setup':
        login = parts[2]
        await q.edit_message_text(f"Настройка *@{login}*:\n\n*/setup* — тематика, промпты\n*/kartinka* — картинка",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu:settings")]]))


# ─── Статус ──────────────────────────────────────────────────────────────────

async def _show_status(q):
    accs = threads_api.list_accounts()
    interval = storage.get_setting('interval_hours', '4')
    lines = ["🤖 *Статус бота*\n", f"Аккаунты: {len(accs)}", f"Очередь: {storage.count()}", f"Интервал: {interval}ч\n"]
    for a in accs:
        acc = storage.get_account(a)
        if not acc: continue
        wp  = "🟢" if acc.get('warmup_active')  else "⚫"
        ap  = "🟢" if acc.get('autopost_active') else "⚫"
        img = "🖼✅" if storage.get_image(a) else "🖼❌"
        try:
            cl_ok = threads_api.get_client(a)['client'] is not None
        except Exception:
            cl_ok = False
        tok = "🔑✅" if cl_ok else "🔑❌"
        lines.append(f"{wp} прогрев {ap} постинг {img} {tok}\n*@{acc.get('username', a)}* | очередь: {storage.count(a)}")
    await q.edit_message_text("\n".join(lines), parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Обновить", callback_data="menu:status"),
            InlineKeyboardButton("◀️ Назад",    callback_data="menu:main"),
        ]]))


# ─── Очередь ─────────────────────────────────────────────────────────────────

async def _show_queue(q):
    items = storage.get_queue()
    if not items:
        await q.edit_message_text("📋 Очередь пуста.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu:main")]])); return
    lines = [f"📋 *Очередь* ({len(items)})\n"]
    for i, it in enumerate(items[:10]):
        lines.append(f"{i+1}. [{it['account_login']}] {it['topic']} ({it['added_at'][:10]})")
    await q.edit_message_text("\n".join(lines), parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Опубликовать первую", callback_data="queue:postnow"),
             InlineKeyboardButton("🔄 Обновить",            callback_data="menu:queue")],
            [InlineKeyboardButton("◀️ Назад", callback_data="menu:main")],
        ]))


async def cb_queue(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    if q.data == 'queue:postnow':
        await q.edit_message_text("⏳ Публикую...")
        try:
            await _do_post()
            await q.edit_message_text("✅ Опубликовано!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К очереди", callback_data="menu:queue")]]))
        except Exception as e:
            await q.edit_message_text(f"❌ {e}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu:queue")]]))


# ─── Команды ─────────────────────────────────────────────────────────────────

async def cmd_add_account(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(upd):
        return ConversationHandler.END
    if len(ctx.args) < 2:
        await upd.message.reply_text("Используй: /add_account login password")
        return ConversationHandler.END

    login, password = ctx.args[0], ctx.args[1]
    ctx.user_data['pending_login']    = login
    ctx.user_data['pending_password'] = password
    msg = await upd.message.reply_text(
        f"⏳ Авторизация...\n`{login}`",
        parse_mode='Markdown'
    )

    # Таймер который обновляет сообщение пока идёт авторизация
    async def _status_updater():
        steps = [
            (22, "⏳ Instagram думает...\n`{login}`"),
            (45, "⏳ Почти готово...\n`{login}` _(может занять до 40с)_"),
            (80, "⏳ Instagram медленно отвечает...\n`{login}`"),
        ]
        for delay, text in steps:
            await asyncio.sleep(delay)
            try:
                await msg.edit_text(text.format(login=login), parse_mode='Markdown')
            except Exception:
                pass

    updater_task = asyncio.ensure_future(_status_updater())

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(threads_api.add_account, login, password),
            timeout=120  # максимум 2 минуты на всю цепочку
        )
        updater_task.cancel()
        await msg.edit_text(
            f"✅ *@{result.get('username', login)}* добавлен!",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👤 К аккаунтам", callback_data="menu:accounts")]])
        )
        return ConversationHandler.END

    except asyncio.TimeoutError:
        updater_task.cancel()
        await msg.edit_text(
            f"❌ *Instagram не отвечает уже 2 минуты*\n\n"
            f"Аккаунт `{login}` заблокирован для входа по логину/паролю.\n\n"
            f"*Единственное решение — /manual\\_cookies:*\n"
            f"F12 → Application → Cookies → threads.net\n"
            f"Скопируй `sessionid` и `csrftoken`",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🍪 Инструкция cookies", callback_data="acc:add_info")
            ]])
        )
        return ConversationHandler.END

    except TwoFactorRequired:
        updater_task.cancel()
        ctx.user_data['2fa_login'] = login
        await msg.edit_text(
            f"🔐 *Двухфакторная аутентификация*\n\n"
            f"Аккаунт: `{login}`\n\n"
            f"Введи 6-значный код из Google Authenticator / Authy:",
            parse_mode='Markdown'
        )
        return WAIT_2FA

    except Exception as e:
        updater_task.cancel()
        await msg.edit_text(
            f"❌ *Не удалось добавить аккаунт*\n\n{e}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🍪 Добавить через cookies", callback_data="acc:add_info")
            ]])
        )
        return ConversationHandler.END


async def handle_2fa(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    code  = upd.message.text.strip()
    login = ctx.user_data.get('2fa_login')
    if not login:
        await upd.message.reply_text("❌ Сессия не найдена. Начни заново с /add_account.")
        return ConversationHandler.END

    msg = await upd.message.reply_text(f"⏳ Проверяю код 2FA для *{login}*...", parse_mode='Markdown')
    try:
        result = await asyncio.to_thread(threads_api.confirm_2fa, login, code)
        await msg.edit_text(
            f"✅ *@{result.get('username', login)}* добавлен — 2FA подтверждена!",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👤 К аккаунтам", callback_data="menu:accounts")]])
        )
    except Exception as e:
        await msg.edit_text(
            f"❌ *{e}*",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Ввести код снова", callback_data=f"acc:add_info")],
                [InlineKeyboardButton("🍪 Добавить через cookies", callback_data="acc:add_info")],
            ])
        )
    return ConversationHandler.END


async def cmd_manual_cookies(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(upd): return ConversationHandler.END
    await upd.message.reply_text(
        "Введи логин аккаунта:\n\n"
        "📌 F12 → Application → Cookies → threads.net\n"
        "Нужны: `sessionid` и `csrftoken`",
        parse_mode='Markdown'
    )
    return WAIT_MANUAL_LOGIN

async def manual_login_h(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['m_login'] = upd.message.text.strip()
    await upd.message.reply_text("Введи *sessionid*:", parse_mode='Markdown')
    return WAIT_MANUAL_SESSION

async def manual_session_h(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['m_session'] = upd.message.text.strip()
    await upd.message.reply_text("Введи *csrftoken*:", parse_mode='Markdown')
    return WAIT_MANUAL_CSRF

async def manual_csrf_h(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    login = ctx.user_data['m_login']
    try:
        result = await asyncio.to_thread(
            threads_api.add_account_manual, login,
            ctx.user_data['m_session'], upd.message.text.strip()
        )
        await upd.message.reply_text(
            f"✅ *{login}* добавлен (@{result.get('username', login)})\n\n"
            f"💡 Для image-постинга добавь пароль: `/add_account {login} ПАРОЛЬ`",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👤 К аккаунтам", callback_data="menu:accounts")]])
        )
    except Exception as e:
        await upd.message.reply_text(f"❌ {e}")
    return ConversationHandler.END


async def cmd_setup(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(upd): return ConversationHandler.END
    accs = threads_api.list_accounts()
    if not accs:
        await upd.message.reply_text("Нет аккаунтов."); return ConversationHandler.END
    await upd.message.reply_text(f"Аккаунты: {', '.join(accs)}\n\nВведи логин для настройки:")
    return WAIT_SETUP_LOGIN

async def setup_login_h(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    login = upd.message.text.strip()
    if login not in threads_api.list_accounts():
        await upd.message.reply_text("Не найден. Введи ещё раз:"); return WAIT_SETUP_LOGIN
    ctx.user_data['setup_login'] = login
    acc = storage.get_account(login)
    cur = acc.get('warmup_keywords', 'vpn,безопасность') if acc else 'vpn,безопасность'
    await upd.message.reply_text(f"Ключевые слова (через запятую):\nТекущие: `{cur}`", parse_mode='Markdown')
    return WAIT_SETUP_KEYWORDS

async def setup_keywords_h(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['setup_keywords'] = upd.message.text.strip()
    await upd.message.reply_text("Пресет прогрева:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("A — Осторожный (8-25 лайков)", callback_data="preset:A")],
        [InlineKeyboardButton("B — Активный (15-30 лайков)",  callback_data="preset:B")],
    ]))
    return WAIT_SETUP_PRESET

async def cb_preset(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    preset = q.data.split(':')[1]
    ctx.user_data['setup_preset'] = preset
    await q.edit_message_text(
        f"Пресет: *{preset}*\n\nВведи промпт для постов\n(или `-` для дефолтного):",
        parse_mode='Markdown'
    )
    return WAIT_SETUP_PROMPTS

async def setup_prompts_h(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    login    = ctx.user_data['setup_login']
    keywords = ctx.user_data['setup_keywords']
    preset   = ctx.user_data.get('setup_preset', 'A')
    text     = upd.message.text.strip()
    storage.update_warmup_settings(login, keywords, preset, 'Europe/Moscow')
    if text != '-':
        storage.update_account_prompts(login, text, '')
    await upd.message.reply_text(f"✅ Настройки сохранены для *{login}*", parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⚙️ К настройкам", callback_data="menu:settings"),
            InlineKeyboardButton("🚀 Автопилот",     callback_data="menu:autopilot"),
        ]]))
    return ConversationHandler.END


async def cmd_kartinka(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(upd): return ConversationHandler.END
    accs = threads_api.list_accounts()
    if not accs:
        await upd.message.reply_text("Нет аккаунтов."); return ConversationHandler.END
    if len(accs) == 1:
        # Только один аккаунт — пропускаем вопрос про логин
        ctx.user_data['img_login'] = accs[0]
        await upd.message.reply_text(
            f"📸 Отправь фото для *{accs[0]}* как *фото* (не файл):\n\n"
            f"❗️ Именно фото, не документ — иначе качество потеряется",
            parse_mode='Markdown'
        )
        return WAIT_PHOTO
    buttons = [[InlineKeyboardButton(f"@{a}", callback_data=f"img_acc:{a}")] for a in accs]
    await upd.message.reply_text(
        "Для какого аккаунта загрузить картинку?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return WAIT_IMAGE_LOGIN

async def kartinka_login_h(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    login = upd.message.text.strip()
    if login not in threads_api.list_accounts():
        await upd.message.reply_text("Не найден. Введи ещё раз:"); return WAIT_IMAGE_LOGIN
    ctx.user_data['img_login'] = login
    await upd.message.reply_text(f"Отправь фото для *{login}* как фото (не файл):", parse_mode='Markdown')
    return WAIT_PHOTO

async def handle_photo(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    login = ctx.user_data.get('img_login')
    if not login: return ConversationHandler.END
    file = await ctx.bot.get_file(upd.message.photo[-1].file_id)
    os.makedirs('images', exist_ok=True)
    path = f"images/{login}.jpg"
    await file.download_to_drive(path)
    storage.set_image(login, path)
    await upd.message.reply_text(f"✅ Картинка сохранена для *{login}*", parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👤 К аккаунту", callback_data=f"acc:manage:{login}")]]))
    return ConversationHandler.END



# ─── Промпты и картинка из кнопок аккаунта ───────────────────────────────────

async def cmd_prompt_account(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Команда /prompt_account — изменить системный промпт для генерации постов."""
    if not is_admin(upd): return ConversationHandler.END
    accs = threads_api.list_accounts()
    if not accs:
        await upd.message.reply_text("Нет аккаунтов."); return ConversationHandler.END

    if len(accs) == 1:
        ctx.user_data['edit_prompt_login'] = accs[0]
    elif ctx.args:
        login = ctx.args[0]
        if login not in accs:
            await upd.message.reply_text(f"❌ Аккаунт {login} не найден."); return ConversationHandler.END
        ctx.user_data['edit_prompt_login'] = login
    else:
        buttons = [[InlineKeyboardButton(f"@{a}", callback_data=f"selaccount:prompt_account:{a}")] for a in accs]
        await upd.message.reply_text("Выбери аккаунт:", reply_markup=InlineKeyboardMarkup(buttons))
        return ConversationHandler.END

    login = ctx.user_data['edit_prompt_login']
    acc   = storage.get_account(login)
    cur   = (acc.get('account_prompt') or '').strip() if acc else ''
    await upd.message.reply_text(
        f"✏️ *Системный промпт для @{login}*\n\n"
        f"Это инструкция для AI — как писать посты, стиль, продукт, тарифы, CTA.\n\n"
        f"{'*Текущий:*\n' + cur[:300] + '...' if len(cur) > 300 else ('*Текущий:*\n' + cur) if cur else '*Сейчас:* используется дефолтный промпт SLASH VPN'}\n\n"
        f"Напиши новый промпт или отправь `-` чтобы сбросить на дефолтный:",
        parse_mode='Markdown'
    )
    return WAIT_EDIT_ACCOUNT_PROMPT


async def edit_account_prompt_h(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    login = ctx.user_data.get('edit_prompt_login')
    text  = upd.message.text.strip()
    if text == '-':
        storage.update_account_prompts(login, '', storage.get_account(login).get('topic_prompt', '') if storage.get_account(login) else '')
        await upd.message.reply_text(f"✅ Промпт *@{login}* сброшен на дефолтный", parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👤 К аккаунту", callback_data=f"acc:manage:{login}")]]))
    else:
        acc = storage.get_account(login)
        storage.update_account_prompts(login, text, acc.get('topic_prompt', '') if acc else '')
        await upd.message.reply_text(
            f"✅ Системный промпт сохранён для *@{login}*\n\nТеперь все посты будут генерироваться по этой инструкции.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👤 К аккаунту", callback_data=f"acc:manage:{login}")]]))
    return ConversationHandler.END


async def cmd_prompt_topic(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Команда /prompt_topic — изменить промпт для генерации тем постов."""
    if not is_admin(upd): return ConversationHandler.END
    accs = threads_api.list_accounts()
    if not accs:
        await upd.message.reply_text("Нет аккаунтов."); return ConversationHandler.END

    if len(accs) == 1:
        ctx.user_data['edit_prompt_login'] = accs[0]
    elif ctx.args:
        login = ctx.args[0]
        if login not in accs:
            await upd.message.reply_text(f"❌ Аккаунт {login} не найден."); return ConversationHandler.END
        ctx.user_data['edit_prompt_login'] = login
    else:
        buttons = [[InlineKeyboardButton(f"@{a}", callback_data=f"selaccount:prompt_topic:{a}")] for a in accs]
        await upd.message.reply_text("Выбери аккаунт:", reply_markup=InlineKeyboardMarkup(buttons))
        return ConversationHandler.END

    login = ctx.user_data['edit_prompt_login']
    acc   = storage.get_account(login)
    cur   = (acc.get('topic_prompt') or '').strip() if acc else ''
    await upd.message.reply_text(
        f"✏️ *Промпт для тем постов @{login}*\n\n"
        f"Это инструкция для AI — какие темы придумывать для постов.\n\n"
        f"{'*Текущий:*\n' + cur if cur else '*Сейчас:* используется дефолтный (темы про VPN, блокировки, слежку)'}\n\n"
        f"Напиши новый промпт или `-` чтобы сбросить на дефолтный:",
        parse_mode='Markdown'
    )
    return WAIT_EDIT_TOPIC_PROMPT


async def edit_topic_prompt_h(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    login = ctx.user_data.get('edit_prompt_login')
    text  = upd.message.text.strip()
    acc   = storage.get_account(login)
    cur_account_prompt = acc.get('account_prompt', '') if acc else ''
    if text == '-':
        storage.update_account_prompts(login, cur_account_prompt, '')
        await upd.message.reply_text(f"✅ Промпт тем *@{login}* сброшен на дефолтный", parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👤 К аккаунту", callback_data=f"acc:manage:{login}")]]))
    else:
        storage.update_account_prompts(login, cur_account_prompt, text)
        await upd.message.reply_text(
            f"✅ Промпт тем сохранён для *@{login}*\n\nAI будет придумывать темы по этой инструкции.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👤 К аккаунту", callback_data=f"acc:manage:{login}")]]))
    return ConversationHandler.END


async def cmd_show_prompts(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Команда /show_prompts — показать текущие промпты аккаунта."""
    if not is_admin(upd): return
    accs = threads_api.list_accounts()
    if not accs:
        await upd.message.reply_text("Нет аккаунтов."); return

    login = ctx.args[0] if ctx.args else accs[0]
    if login not in accs:
        await upd.message.reply_text(f"❌ Аккаунт {login} не найден."); return
    acc = storage.get_account(login)
    if not acc:
        await upd.message.reply_text("Аккаунт не найден."); return

    ap = (acc.get('account_prompt') or '').strip()
    tp = (acc.get('topic_prompt') or '').strip()

    text = f"📋 *Промпты @{login}*\n\n"
    text += f"*1. Системный промпт (генерация постов):*\n"
    if ap:
        text += f"`{ap[:500]}`{'...' if len(ap) > 500 else ''}\n\n"
    else:
        text += "_используется дефолтный SLASH VPN_\n\n"
    text += f"*2. Промпт тем:*\n"
    if tp:
        text += f"`{tp[:300]}`{'...' if len(tp) > 300 else ''}"
    else:
        text += "_используется дефолтный (VPN, блокировки, слежка)_"

    await upd.message.reply_text(text, parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Изменить системный промпт", callback_data=f"acc:edit_aprompt:{login}")],
            [InlineKeyboardButton("✏️ Изменить промпт тем",       callback_data=f"acc:edit_tprompt:{login}")],
            [InlineKeyboardButton("👤 К аккаунту",                 callback_data=f"acc:manage:{login}")],
        ]))


async def cb_selaccount(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработчик выбора аккаунта для команд prompt_account и prompt_topic."""
    q = upd.callback_query; await q.answer()
    _, cmd, login = q.data.split(':', 2)
    ctx.user_data['edit_prompt_login'] = login
    acc = storage.get_account(login)
    if cmd == 'prompt_account':
        cur = (acc.get('account_prompt') or '').strip() if acc else ''
        await q.edit_message_text(
            f"✏️ *Системный промпт для @{login}*\n\n"
            f"{'*Текущий:*\n' + cur[:300] + ('...' if len(cur) > 300 else '') if cur else '*Сейчас:* дефолтный SLASH VPN'}\n\n"
            f"Напиши новый промпт или `-` чтобы сбросить на дефолтный:",
            parse_mode='Markdown'
        )
        ctx.user_data['_conv_state'] = 'edit_account_prompt'
    elif cmd == 'prompt_topic':
        cur = (acc.get('topic_prompt') or '').strip() if acc else ''
        await q.edit_message_text(
            f"✏️ *Промпт тем для @{login}*\n\n"
            f"{'*Текущий:*\n' + cur if cur else '*Сейчас:* дефолтный (VPN, блокировки)'}\n\n"
            f"Напиши новый промпт или `-` чтобы сбросить:",
            parse_mode='Markdown'
        )
        ctx.user_data['_conv_state'] = 'edit_topic_prompt'


async def cmd_seriya(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or len(ctx.args) < 2:
        await upd.message.reply_text("Используй: /seriya login тема серии"); return
    login = ctx.args[0]; topic = ' '.join(ctx.args[1:])
    if login not in threads_api.list_accounts():
        await upd.message.reply_text(f"❌ Аккаунт {login} не найден."); return
    msg = await upd.message.reply_text(f"⏳ Генерирую: _{topic}_...", parse_mode='Markdown')
    try:
        series = await asyncio.to_thread(ai_gen.generate_series, topic, login)
        storage.add_series(series, login)
        preview = series['post1'][:120] + '...'
        await msg.edit_text(
            f"✅ Серия добавлена ({storage.count(login)} в очереди)\n\n*Хук:* {preview}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("▶️ Пост сейчас", callback_data=f"acc:postnow:{login}"),
                InlineKeyboardButton("📋 Очередь",      callback_data=f"acc:queue:{login}"),
            ]])
        )
    except Exception as e:
        await msg.edit_text(f"❌ {e}")


async def cmd_interval(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(upd): return
    if not ctx.args:
        cur = storage.get_setting('interval_hours', '4')
        await upd.message.reply_text(
            f"Текущий интервал: *{cur} ч.*\n\nИспользуй: `/interval 4`",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("2ч", callback_data="interval:2"),
                InlineKeyboardButton("4ч", callback_data="interval:4"),
                InlineKeyboardButton("6ч", callback_data="interval:6"),
                InlineKeyboardButton("12ч",callback_data="interval:12"),
            ]])
        ); return
    try:
        h = int(ctx.args[0])
        storage.set_setting('interval_hours', h)
        _reschedule_post(h)
        await upd.message.reply_text(f"✅ Интервал: *{h} ч.*", parse_mode='Markdown')
    except ValueError:
        await upd.message.reply_text("Укажи целое число часов")


async def cb_interval(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query; await q.answer()
    h = int(q.data.split(':')[1])
    storage.set_setting('interval_hours', h)
    _reschedule_post(h)
    await q.edit_message_text(f"✅ Интервал: *{h} ч.*", parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu:settings")]]))


async def conv_cancel(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Сбрасываем все pending-состояния
    ctx.user_data.pop('_conv_state', None)
    ctx.user_data.pop('edit_prompt_login', None)
    ctx.user_data.pop('_waiting_photo_for', None)
    ctx.user_data.pop('img_login', None)
    ctx.user_data.pop('refresh_token_login', None)
    await upd.message.reply_text("Отменено.", reply_markup=kb_main())
    return ConversationHandler.END


async def universal_message_handler(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Универсальный обработчик текстовых сообщений для inline-редактирования промптов.
    Срабатывает когда пользователь нажал кнопку ✏️ из меню аккаунта и вводит промпт.
    """
    state = ctx.user_data.get('_conv_state')
    if not state:
        return

    login = ctx.user_data.get('edit_prompt_login')
    text  = upd.message.text.strip()

    if state == 'edit_account_prompt':
        acc = storage.get_account(login)
        cur_tp = acc.get('topic_prompt', '') if acc else ''
        if text == '-':
            storage.update_account_prompts(login, '', cur_tp)
            reply = f"✅ Системный промпт *@{login}* сброшен на дефолтный SLASH VPN"
        else:
            storage.update_account_prompts(login, text, cur_tp)
            reply = f"✅ Системный промпт сохранён для *@{login}*\n\nТеперь все посты будут генерироваться по твоей инструкции."
        ctx.user_data.pop('_conv_state', None)
        await upd.message.reply_text(reply, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Посмотреть промпты",  callback_data=f"acc:show_prompts:{login}")],
                [InlineKeyboardButton("👤 К аккаунту",          callback_data=f"acc:manage:{login}")],
            ]))

    elif state == 'refresh_token':
        login    = ctx.user_data.pop('refresh_token_login', None)
        password = text
        ctx.user_data.pop('_conv_state', None)
        if not login:
            await upd.message.reply_text("❌ Сессия не найдена, попробуй снова.", reply_markup=kb_main())
            return
        acc = storage.get_account(login)
        uname = acc.get('username', login) if acc else login
        await upd.message.reply_text(f"⏳ Обновляю токен для *@{uname}*...", parse_mode='Markdown')
        try:
            ok = await threads_api.refresh_token(login, password)
            if ok:
                # Проверяем что клиент создан
                try:
                    cl_active = threads_api.get_client(login)['client'] is not None
                except Exception:
                    cl_active = False
                mode = "✅ полный доступ" if cl_active else "⚠️ частично"
                await upd.message.reply_text(
                    f"✅ Токен обновлён для *@{uname}*\n\nРежим: {mode}\n\n"
                    f"Лайки, прогрев и репосты работают.",
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("👤 К аккаунту", callback_data=f"acc:manage:{login}")
                    ]])
                )
            else:
                await upd.message.reply_text(
                    f"❌ Не удалось обновить токен для *@{uname}*\n\n"
                    f"Проверь пароль или подожди 15-30 минут (Instagram временно блокирует).",
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"acc:refresh_token:{login}"),
                        InlineKeyboardButton("◀️ К аккаунту",        callback_data=f"acc:manage:{login}"),
                    ]])
                )
        except Exception as e:
            await upd.message.reply_text(
                f"❌ Ошибка: {e}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ К аккаунту", callback_data=f"acc:manage:{login}")
                ]])
            )

    elif state == 'edit_topic_prompt':
        acc = storage.get_account(login)
        cur_ap = acc.get('account_prompt', '') if acc else ''
        if text == '-':
            storage.update_account_prompts(login, cur_ap, '')
            reply = f"✅ Промпт тем *@{login}* сброшен на дефолтный"
        else:
            storage.update_account_prompts(login, cur_ap, text)
            reply = f"✅ Промпт тем сохранён для *@{login}*\n\nAI будет придумывать темы по твоей инструкции."
        ctx.user_data.pop('_conv_state', None)
        await upd.message.reply_text(reply, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Посмотреть промпты", callback_data=f"acc:show_prompts:{login}")],
                [InlineKeyboardButton("👤 К аккаунту",         callback_data=f"acc:manage:{login}")],
            ]))


async def universal_photo_handler(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Универсальный обработчик фото — ловит фото отправленное после нажатия кнопки
    🖼 Загрузить картинку в меню аккаунта.
    """
    login = ctx.user_data.get('_waiting_photo_for') or ctx.user_data.get('img_login')
    if not login:
        return
    file = await ctx.bot.get_file(upd.message.photo[-1].file_id)
    os.makedirs('images', exist_ok=True)
    path = f"images/{login}.jpg"
    await file.download_to_drive(path)
    storage.set_image(login, path)
    ctx.user_data.pop('_waiting_photo_for', None)
    ctx.user_data.pop('img_login', None)
    await upd.message.reply_text(
        f"✅ Картинка сохранена для *@{login}*\n\nОна будет добавлена к посту 3 (с тарифами) при следующей публикации.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("👤 К аккаунту", callback_data=f"acc:manage:{login}")
        ]])
    )


# ─── Планировщик ─────────────────────────────────────────────────────────────

async def _do_post(account_login=None):
    item = storage.pop(account_login)
    if not item:
        raise Exception("Очередь пуста")
    login = item['account_login']
    image = storage.get_image(login)
    acc   = storage.get_account(login)
    if acc and acc.get('warmup_active'):
        await warmup.pre_post_warmup(login)
    ids = await threads_api.post_series_async(item['posts'], image, login)
    storage.archive_item(item['posts'], login, ids)
    return ids


async def _scheduler_post():
    from humanize import is_active_hour
    for login in threads_api.list_accounts():
        acc = storage.get_account(login)
        if not acc or not acc.get('autopost_active'): continue
        if not is_active_hour(acc.get('timezone', 'Europe/Moscow')): continue
        if storage.count(login) > 0:
            try:
                await _do_post(login)
                await asyncio.sleep(random.uniform(300, 900))
            except Exception as e:
                logger.error(f"Ошибка публикации {login}: {e}")


async def _scheduler_warmup():
    for login in threads_api.list_accounts():
        acc = storage.get_account(login)
        if not acc or not acc.get('warmup_active'): continue
        await warmup.run_warmup_session(login)
        await asyncio.sleep(random.uniform(60, 300))


async def _scheduler_monitor():
    await monitor.check_all_comments()
    await monitor.check_post_stats()


def _reschedule_post(hours: int):
    """BUG-16 FIX: не вызываем add_job до scheduler.start()."""
    global _scheduler_started
    if not _scheduler_started:
        logger.warning("_reschedule_post: scheduler ещё не запущен — пропуск")
        return
    if scheduler.get_job('post'):
        scheduler.remove_job('post')
    scheduler.add_job(_scheduler_post, 'interval', hours=hours, jitter=1800, id='post')
    logger.info(f"Интервал постинга обновлён: {hours}ч")


async def on_startup(app):
    global _scheduler_started
    threads_api.load_accounts_from_db()
    monitor.set_telegram(app, ADMIN_IDS)
    interval = int(storage.get_setting('interval_hours', '4'))
    scheduler.add_job(_scheduler_post,    'interval', hours=interval, jitter=1800, id='post')
    scheduler.add_job(_scheduler_warmup,  'interval', hours=8,        jitter=600,  id='warmup')
    scheduler.add_job(_scheduler_monitor, 'interval', minutes=30,     jitter=120,  id='monitor')
    scheduler.start()
    _scheduler_started = True   # BUG-16 FIX: флаг ПОСЛЕ start()
    logger.info(f"Бот запущен. Аккаунтов: {len(threads_api.list_accounts())}. Интервал: {interval}ч")


# ─── Сборка ──────────────────────────────────────────────────────────────────

def build_app():
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    # ── Простые команды ──────────────────────────────────────────────────────
    app.add_handler(CommandHandler('start',         cmd_start))
    app.add_handler(CommandHandler('help',          cmd_help))
    app.add_handler(CommandHandler('seriya',        cmd_seriya))
    app.add_handler(CommandHandler('interval',      cmd_interval))
    app.add_handler(CommandHandler('show_prompts',  cmd_show_prompts))

    # ── Callback кнопки ──────────────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(cb_menu,       pattern=r'^menu:'))
    app.add_handler(CallbackQueryHandler(cb_acc,        pattern=r'^acc:'))
    app.add_handler(CallbackQueryHandler(cb_autopilot,  pattern=r'^ap:'))
    app.add_handler(CallbackQueryHandler(cb_stats,      pattern=r'^stats:'))
    app.add_handler(CallbackQueryHandler(cb_settings,   pattern=r'^settings:'))
    app.add_handler(CallbackQueryHandler(cb_queue,      pattern=r'^queue:'))
    app.add_handler(CallbackQueryHandler(cb_interval,   pattern=r'^interval:'))
    app.add_handler(CallbackQueryHandler(cb_selaccount, pattern=r'^selaccount:'))

    # ── /add_account + 2FA ───────────────────────────────────────────────────
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('add_account', cmd_add_account)],
        states={WAIT_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_2fa)]},
        fallbacks=[CommandHandler('cancel', conv_cancel)],
    ))
    # ── /manual_cookies ──────────────────────────────────────────────────────
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('manual_cookies', cmd_manual_cookies)],
        states={
            WAIT_MANUAL_LOGIN:   [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_login_h)],
            WAIT_MANUAL_SESSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_session_h)],
            WAIT_MANUAL_CSRF:    [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_csrf_h)],
        },
        fallbacks=[CommandHandler('cancel', conv_cancel)],
    ))
    # ── /setup ───────────────────────────────────────────────────────────────
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('setup', cmd_setup)],
        states={
            WAIT_SETUP_LOGIN:    [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_login_h)],
            WAIT_SETUP_KEYWORDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_keywords_h)],
            WAIT_SETUP_PRESET:   [CallbackQueryHandler(cb_preset, pattern=r'^preset:')],
            WAIT_SETUP_PROMPTS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_prompts_h)],
        },
        fallbacks=[CommandHandler('cancel', conv_cancel)],
    ))
    # ── /kartinka ────────────────────────────────────────────────────────────
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('kartinka', cmd_kartinka)],
        states={
            WAIT_IMAGE_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, kartinka_login_h)],
            WAIT_PHOTO:       [MessageHandler(filters.PHOTO, handle_photo)],
        },
        fallbacks=[CommandHandler('cancel', conv_cancel)],
    ))
    # ── /prompt_account — изменить системный промпт ──────────────────────────
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('prompt_account', cmd_prompt_account)],
        states={
            WAIT_EDIT_ACCOUNT_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_account_prompt_h)],
        },
        fallbacks=[CommandHandler('cancel', conv_cancel)],
    ))
    # ── /prompt_topic — изменить промпт тем ─────────────────────────────────
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler('prompt_topic', cmd_prompt_topic)],
        states={
            WAIT_EDIT_TOPIC_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_topic_prompt_h)],
        },
        fallbacks=[CommandHandler('cancel', conv_cancel)],
    ))

    # ── Универсальные обработчики для inline-редактирования из кнопок ────────
    # (когда пользователь нажимает ✏️ прямо в меню аккаунта)
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, universal_photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,  universal_message_handler))

    return app


if __name__ == '__main__':
    build_app().run_polling()
