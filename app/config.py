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

APP_ENV = os.getenv("APP_ENV", "development").lower()
SECRET_KEY = os.getenv("SECRET_KEY") or "change-this-in-production"
if APP_ENV in {"production", "prod"} and (
    SECRET_KEY == "change-this-in-production" or len(SECRET_KEY) < 32
):
    raise RuntimeError("SECRET_KEY must be a strong value in production")
SESSION_MAX_AGE_SECONDS = int(os.getenv("SESSION_MAX_AGE_SECONDS", "604800"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"

# Neo4j is an optional graph store at this stage.  When NEO4J_DATABASE is not
# set, the driver uses the server's configured default database.
NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")
