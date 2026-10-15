from passlib.context import CryptContext
from itsdangerous import URLSafeTimedSerializer
from app.config import SECRET_KEY, SESSION_MAX_AGE_SECONDS

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

serializer = URLSafeTimedSerializer(SECRET_KEY)


def hash_password(password: str) -> str:
    """Hash a plain-text password before it is stored in PostgreSQL."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Check a login password against its stored bcrypt hash."""
    return pwd_context.verify(plain_password, hashed_password)


def create_session_token(user_id: int) -> str:
    """Create a signed token containing the authenticated user's ID."""
    return serializer.dumps({"user_id": user_id})


def get_user_id_from_token(token: str) -> int | None:
    """Return the user ID from a valid, unexpired token or ``None``."""
    try:
        data = serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
        return data.get("user_id")
    except Exception:
        return None
"""Password hashing and signed session-token helpers."""
