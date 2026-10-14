import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import DATABASE_URL


SQLALCHEMY_ECHO = os.getenv("SQLALCHEMY_ECHO", "false").lower() == "true"
engine = create_engine(DATABASE_URL, echo=SQLALCHEMY_ECHO)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():

    db = SessionLocal()
    try:
        yield db  
    finally:
        db.close()  
