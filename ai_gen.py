### ai_gen.py
import os, json, re, logging
from dotenv import load_dotenv
import storage

load_dotenv()
logger = logging.getLogger(__name__)

# БАГ ИСПРАВЛЕН: клиент создаётся лениво, не при импорте
_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        api_key = os.environ.get('AITUNNEL_API_KEY')
        if not api_key:
            raise Exception("AITUNNEL_API_KEY не задан в .env")
        _client = OpenAI(api_key=api_key, base_url='https://api.aitunnel.ru/v1/')
    return _client


DEFAULT_ACCOUNT_PROMPT = '''
Ты пишешь конверсионные посты для Threads о SLASH VPN.
Продукт: SLASH VPN — Telegram-бот для защиты трафика.
Тарифы: 1 день 10р, 3 дня 30р, 7 дней 70р, 14 дней 150р, 30 дней 199р.
CTA: "напиши + в комментах — скину ссылку лично".
Тон: от первого лица, живой, без канцелярита. Тарифы только в посте 3.

Отвечай СТРОГО JSON без markdown:
{
  "topic": "<тема>",
  "post1": "<хук>",
  "post2": "<боль>",
  "post3": "<решение с тарифами и CTA>",
  "post4": "<дожим с CTA>"
}
'''

DEFAULT_TOPIC_PROMPT = '''
Придумай одну свежую тему для поста о SLASH VPN в Threads.
Аудитория: Россия, 18-35 лет. Тема: боль/страх (слежка, блокировки, утечки, скорость).
Отвечай одной строкой, 3-8 слов, без кавычек.
'''


def _prompts(account_login=None):
    if account_login:
        acc = storage.get_account(account_login)
        if acc:
            ap = (acc.get('account_prompt') or '').strip()
            tp = (acc.get('topic_prompt') or '').strip()
            return (ap or DEFAULT_ACCOUNT_PROMPT), (tp or DEFAULT_TOPIC_PROMPT)
    return DEFAULT_ACCOUNT_PROMPT, DEFAULT_TOPIC_PROMPT


def generate_topic(account_login=None):
    _, topic_prompt = _prompts(account_login)
    r = _get_client().chat.completions.create(
        model='gpt-4.1-nano',
        messages=[{'role': 'system', 'content': topic_prompt},
                  {'role': 'user',   'content': 'Придумай тему'}],
        max_tokens=60, temperature=1.0
    )
    return r.choices[0].message.content.strip().strip('"').strip("'")


def generate_series(topic, account_login=None):
    account_prompt, _ = _prompts(account_login)
    r = _get_client().chat.completions.create(
        model='gpt-4.1-nano',
        messages=[{'role': 'system', 'content': account_prompt},
                  {'role': 'user',   'content': f'Тема: {topic}'}],
        max_tokens=1400, temperature=0.85
    )
    text = r.choices[0].message.content.strip()
    text = re.sub(r'```json\s*|```\s*', '', text).strip()

    def fix_nl(m):
        return m.group(0).replace('\n', '\\n').replace('\r', '')
    text = re.sub(r'"(?:[^"\\]|\\.)*"', fix_nl, text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise Exception(f"AI вернул невалидный JSON: {e}")

    missing = [k for k in ['post1','post2','post3','post4'] if k not in data]
    if missing:
        raise Exception(f"AI не вернул поля: {missing}")

    data.setdefault('topic', topic)
    return data
