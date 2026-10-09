from passlib.context import CryptContext
from itsdangerous import URLSafeSerializer
from app.config import SECRET_KEY

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

serializer = URLSafeSerializer(SECRET_KEY)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_session_token(user_id: int) -> str:
    return serializer.dumps({"user_id": user_id})


def get_user_id_from_token(token: str) -> int | None:
    try:
        data = serializer.loads(token)
        return data.get("user_id")
    except Exception:
        return None
