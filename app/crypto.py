import os
from cryptography.fernet import Fernet

KEYFILE = os.environ.get("SECRET_KEY_FILE", "/data/secret.key")


def _get_or_create_key() -> bytes:
    """Reads existing Fernet key from file or generates a new one and writes it."""
    if os.path.exists(KEYFILE):
        with open(KEYFILE, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(KEYFILE), exist_ok=True)
    with open(KEYFILE, "wb") as f:
        f.write(key)
    return key


def get_fernet() -> Fernet:
    """Returns a Fernet instance using the stored or generated key."""
    return Fernet(_get_or_create_key())


def get_session_secret() -> str:
    """Returns the secret key as a string for use with SessionMiddleware."""
    return _get_or_create_key().decode()


def encrypt(value: str) -> str:
    """Encrypts a plaintext string and returns the encrypted token as a string."""
    f = get_fernet()
    return f.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    """Decrypts a Fernet-encrypted token string and returns the plaintext."""
    f = get_fernet()
    return f.decrypt(value.encode()).decode()
