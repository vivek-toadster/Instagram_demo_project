from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, UniqueConstraint, JSON
from sqlalchemy.orm import relationship
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(100), nullable=True)
    bio = Column(Text, nullable=True)
    profile_pic = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    posts = relationship("Post", back_populates="author", cascade="all, delete-orphan")
    likes = relationship("Like", back_populates="user", cascade="all, delete-orphan")
    comments = relationship("Comment", back_populates="author", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(id={self.id}, username='{self.username}')>"


class Post(Base):
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, index=True)
    caption = Column(Text, nullable=True)
    image_path = Column(String(500), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    author = relationship("User", back_populates="posts")
    likes = relationship("Like", back_populates="post", cascade="all, delete-orphan")
    comments = relationship("Comment", back_populates="post", cascade="all, delete-orphan", order_by="Comment.created_at")

    def __repr__(self):
        return f"<Post(id={self.id}, user_id={self.user_id})>"


class Like(Base):
    __tablename__ = "likes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


    __table_args__ = (UniqueConstraint("user_id", "post_id", name="uq_user_post_like"),)

    user = relationship("User", back_populates="likes")
    post = relationship("Post", back_populates="likes")


class Comment(Base):
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    body = Column(Text, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    author = relationship("User", back_populates="comments")
    post = relationship("Post", back_populates="comments")


class Follow(Base):
    __tablename__ = "follows"

    id = Column(Integer, primary_key=True, index=True)
    follower_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    followed_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    __table_args__ = (UniqueConstraint("follower_id", "followed_id", name="uq_follower_followed"),)


class GraphOutboxEvent(Base):
    __tablename__ = "graph_outbox_events"

    event_id = Column(String(36), primary_key=True)
    event_type = Column(String(50), nullable=False, index=True)
    payload = Column(JSON, nullable=False)
    status = Column(String(20), nullable=False, default="pending", index=True)
    retry_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    next_attempt_at = Column(DateTime, nullable=False, index=True)
    processed_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
