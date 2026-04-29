import sqlite3
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from config import DATABASE_URL

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


@event.listens_for(engine, "connect")
def _set_pragmas(conn, _):
    if isinstance(conn, sqlite3.Connection):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA cache_size=-32000")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA mmap_size=268435456")
        cur.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
