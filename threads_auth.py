### threads_auth.py
"""
Авторизация в Threads/Instagram через мобильный API.
Без Selenium — чистые requests + шифрование пароля.
"""
import os, time, uuid, json, base64, struct, hmac, hashlib, logging, requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.padding import OAEP, MGF1
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import load_der_public_key, load_pem_public_key

logger = logging.getLogger(__name__)

IG_APP_ID      = "567067343352427"
IG_APP_VERSION = "289.0.0.77.109"
DEVICE_ID      = str(uuid.uuid4())
PHONE_ID       = str(uuid.uuid4())
UUID_          = str(uuid.uuid4())

BASE_HEADERS = {
    "User-Agent": f"Instagram {IG_APP_VERSION} Android (29/10; 420dpi; 1080x1920; Xiaomi; Mi 9; cepheus; qcom; ru_RU; {IG_APP_ID})",
    "X-IG-App-ID": IG_APP_ID,
    "X-IG-Android-ID": f"android-{DEVICE_ID[:16]}",
    "X-IG-Device-ID": DEVICE_ID,
    "X-IG-Phone-ID": PHONE_ID,
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "*/*",
    "Connection": "keep-alive",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}


def fetch_headers(session):
    r = session.get(
        "https://i.instagram.com/api/v1/si/fetch_headers/",
        headers={**BASE_HEADERS, "X-DEVICE-ID": UUID_},
        params={"challenge_type": "signup", "guid": UUID_},
        timeout=15
    )
    csrf = r.cookies.get("csrftoken", "")
    mid  = r.headers.get("X-MID", "")
    if not csrf:
        for part in r.headers.get("Set-Cookie", "").split(";"):
            if "csrftoken=" in part:
                csrf = part.split("csrftoken=")[1].strip()
    return csrf, mid


def get_enc_key(session, csrf, mid):
    r = session.get(
        "https://i.instagram.com/api/v1/qe/sync/",
        headers={**BASE_HEADERS, "X-CSRFToken": csrf, "X-MID": mid},
        timeout=15
    )
    enc_header  = r.headers.get("ig-set-password-encryption-key-id", "")
    enc_version = r.headers.get("ig-set-password-encryption-pub-key", "")
    if not enc_header or not enc_version:
        r2 = session.post(
            "https://i.instagram.com/api/v1/accounts/get_password_encryption_keyset/",
            headers={**BASE_HEADERS, "X-CSRFToken": csrf, "X-MID": mid},
            timeout=15
        )
        d = r2.json()
        return int(d.get("public_key_id", 0)), int(d.get("key_id", 0)), d.get("public_key", "")
    return int(r.headers.get("ig-set-password-encryption-key-version","0")), int(enc_header), enc_version.strip()


def _load_instagram_pubkey(pub_key_b64: str):
    """
    FIX: Instagram иногда возвращает base64(PEM), а не base64(DER).
    Первый байт PEM — '-' (0x2D), что даёт ASN.1 tag=13, constructed=True, class=Universal
    и вызывает ошибку 'Could not deserialize key data'.
    Определяем формат по первым байтам и используем нужный загрузчик.
    """
    key_bytes = base64.b64decode(pub_key_b64)
    if key_bytes.startswith(b'-----'):
        # Instagram вернул base64(PEM) — декодируем как PEM напрямую
        return load_pem_public_key(key_bytes)
    # Стандартный случай: base64(DER)
    return load_der_public_key(key_bytes)


def encrypt_password(password, key_id, key_version, pub_key_b64):
    aes_key = os.urandom(32)
    iv      = os.urandom(12)
    pub_key = _load_instagram_pubkey(pub_key_b64)  # FIX: was load_der_public_key(base64.b64decode(...))
    encrypted_aes_key = pub_key.encrypt(aes_key, OAEP(mgf=MGF1(algorithm=SHA256()), algorithm=SHA256(), label=None))
    timestamp = str(int(time.time()))
    aesgcm = AESGCM(aes_key)
    encrypted_with_tag = aesgcm.encrypt(iv, password.encode(), timestamp.encode())
    encrypted_password = encrypted_with_tag[:-16]
    auth_tag           = encrypted_with_tag[-16:]
    payload = (b"\x01" + struct.pack("<B", key_version) + struct.pack("<H", key_id)
               + iv + struct.pack("<H", len(encrypted_aes_key))
               + encrypted_aes_key + auth_tag + encrypted_password)
    return f"#PWD_INSTAGRAM:4:{timestamp}:{base64.b64encode(payload).decode()}"


def login(username, password):
    session = requests.Session()
    session.headers.update(BASE_HEADERS)
    csrf, mid = fetch_headers(session)
    key_version, key_id, pub_key_b64 = get_enc_key(session, csrf, mid)
    enc_password = encrypt_password(password, key_id, key_version, pub_key_b64)

    r = session.post(
        "https://i.instagram.com/api/v1/accounts/login/",
        data={"username": username, "enc_password": enc_password,
              "device_id": DEVICE_ID, "guid": UUID_, "phone_id": PHONE_ID,
              "login_attempt_count": "0"},
        headers={**BASE_HEADERS, "X-CSRFToken": csrf, "X-MID": mid},
        timeout=20
    )

    try:
        data = r.json()
    except Exception:
        raise Exception(f"Сервер вернул не JSON: {r.status_code} {r.text[:200]}")

    logger.debug(f"[{username}] Login response {r.status_code}: {str(data)[:500]}")

    msg   = data.get("message", "") if isinstance(data, dict) else str(data)
    error = data.get("error_type", "") if isinstance(data, dict) else ""

    if r.status_code == 400 or (isinstance(data, dict) and data.get("status") == "fail"):
        if data.get("two_factor_required") or "two_factor" in error:
            raise TwoFactorRequired(username, data.get("two_factor_info", {}))

        if any(k in error for k in ("checkpoint", "challenge")) or \
           any(k in str(data) for k in ("challenge_required", "checkpoint_required")):
            raise Exception(
                "Instagram требует подтверждение входа (checkpoint). "
                "Используй /manual_cookies"
            )

        if any(k in error for k in ("rate_limit", "sentry_block", "spam")) or \
           any(k in msg for k in ("wait a few minutes", "try again later", "Please wait")):
            raise Exception(
                "Instagram заблокировал вход временно (rate limit). "
                "Подожди 15-30 минут и попробуй снова, "
                "или используй /manual_cookies"
            )

        if any(k in error for k in ("bad_password", "invalid_user")) or \
           any(k in msg for k in ("Invalid", "Incorrect password", "password was incorrect")):
            raise Exception(
                "Неверный логин или пароль. "
                "Если пароль точно верный — Instagram мог заблокировать вход после "
                "предыдущих попыток. Подожди 15-30 минут или используй /manual_cookies"
            )

        if "inactive user" in msg or "deactivated" in msg:
            raise Exception("Аккаунт деактивирован или не существует.")

        raise Exception(f"Instagram: {msg or error or str(data)[:200]}. Попробуй /manual_cookies")

    if r.status_code == 429:
        raise Exception("Слишком много запросов. Подожди 15-30 минут и попробуй снова.")

    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code}. Попробуй /manual_cookies")

    if "logged_in_user" not in data:
        if "challenge" in str(data) or "checkpoint" in str(data):
            raise Exception("Instagram требует подтверждение входа. Используй /manual_cookies.")
        raise Exception(f"Неожиданный ответ от Instagram: {str(data)[:300]}")


    user = data["logged_in_user"]
    return {
        "session_id": session.cookies.get("sessionid", ""),
        "csrf_token": session.cookies.get("csrftoken", csrf),
        "user_id":    str(user.get("pk") or user.get("id", "")),
        "username":   user.get("username", username),
    }


class TwoFactorRequired(Exception):
    def __init__(self, login, two_factor_info=None):
        self.login = login
        self.two_factor_info = two_factor_info or {}
        super().__init__(f"2FA required for {login}")
