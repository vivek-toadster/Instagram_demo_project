import os
from dotenv import load_dotenv
from sqlalchemy.engine import URL

load_dotenv()

DB_USER = os.getenv("DB_USER", "viveks")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "instagram_clone")

DATABASE_URL = URL.create(
    drivername="postgresql+psycopg2",
    username=DB_USER,
    password=DB_PASSWORD,
    host=DB_HOST,
    port=DB_PORT,
    database=DB_NAME,
)

SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "change-this-in-production",
)