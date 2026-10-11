"""Password hashing and generation."""
import secrets
from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD_LENGTH = 8
# No look-alike characters (0/O, 1/l/I) so temporary passwords can be read out over the phone.
_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PBKDF2_ITERATIONS = 600_000   # slow on purpose; tests lower it
_DUMMY_HASH = generate_password_hash("not-a-real-password", method="pbkdf2:sha256")


def hash_password(password):
    return generate_password_hash(password, method=f"pbkdf2:sha256:{PBKDF2_ITERATIONS}")


def verify_password(password_hash, password):
    """Constant-ish time even when the user does not exist (password_hash=None)."""
    return check_password_hash(password_hash or _DUMMY_HASH, password) and bool(password_hash)


def generate_password(length=10):
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def password_problem(password):
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > 128:
        return "Password is too long."
    return None
