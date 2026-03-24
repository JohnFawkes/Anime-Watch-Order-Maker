from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, nullable=False, index=True)
    value = Column(String, nullable=False)  # Fernet-encrypted text
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ShowSkip(Base):
    """Shows marked to be skipped by the auto-playlist cron job."""

    __tablename__ = "show_skips"

    show_rating_key = Column(Integer, primary_key=True)
    show_title = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Playlist(Base):
    __tablename__ = "playlists"

    id = Column(Integer, primary_key=True, index=True)
    show_rating_key = Column(Integer, unique=True, nullable=False, index=True)
    show_title = Column(String, nullable=False)
    playlist_rating_key = Column(Integer, nullable=False)
    playlist_title = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
