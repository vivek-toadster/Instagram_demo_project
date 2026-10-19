import asyncio
from io import BytesIO

import pytest
from fastapi import HTTPException
from fastapi.datastructures import UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import Headers
from starlette.requests import Request

from app import routes
from app.auth import create_session_token, hash_password
from app.database import Base
from app.models import Comment, Conversation, Follow, GraphOutboxEvent, Like, Message, Post, User


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def request_for(user_id: int | None = None, csrf: str | None = None, path: str = "/") -> Request:
    headers = []
    if user_id is not None:
        headers.append((b"cookie", f"session_token={create_session_token(user_id)}".encode()))
    scope = {"type": "http", "method": "POST", "path": path, "headers": headers, "session": {}}
    request = Request(scope)
    if csrf is not None:
        request.session["csrf_token"] = csrf
    return request


def add_user(db, user_id: int, username: str) -> User:
    user = User(
        id=user_id,
        username=username,
        email=f"{username}@example.com",
        password_hash=hash_password("password123"),
    )
    db.add(user)
    db.flush()
    return user


def test_registration_and_login_create_user_and_preserve_authentication(db):
    registration_request = request_for(csrf="register-token", path="/register")
    response = routes.register_user(
        registration_request,
        username="alice",
        email="alice@example.com",
        password="password123",
        confirm_password="password123",
        full_name="Alice",
        csrf_token="register-token",
        db=db,
    )

    user = db.query(User).filter_by(username="alice").one()
    event = db.query(GraphOutboxEvent).filter_by(event_type="user.created").one()
    assert response.status_code == 302
    assert event.payload["user_id"] == user.id

    login_request = request_for(csrf="login-token", path="/login")
    login_response = routes.login_user(
        login_request,
        username="alice",
        password="password123",
        csrf_token="login-token",
        db=db,
    )

    assert login_response.status_code == 302
    assert "session_token=" in login_response.headers["set-cookie"]


def test_invalid_csrf_is_rejected_without_database_change(db):
    request = request_for(csrf="expected", path="/register")

    with pytest.raises(HTTPException) as error:
        routes.register_user(
            request,
            username="alice",
            email="alice@example.com",
            password="password123",
            confirm_password="password123",
            full_name="",
            csrf_token="wrong",
            db=db,
        )

    assert error.value.status_code == 403
    assert db.query(User).count() == 0
    assert db.query(GraphOutboxEvent).count() == 0


def test_follow_unfollow_and_like_comment_events(db):
    add_user(db, 1, "alice")
    add_user(db, 2, "bob")
    post = Post(id=10, user_id=2, image_path="post.jpg")
    db.add(post)
    db.commit()
    request = request_for(1, "token", "/profile/bob/follow")

    routes.toggle_follow("bob", request, csrf_token="token", db=db)
    assert db.query(Follow).count() == 1
    routes.toggle_follow("bob", request, csrf_token="token", db=db)
    assert db.query(Follow).count() == 0

    routes.toggle_like(10, request_for(1, "token", "/post/10/like"), csrf_token="token", db=db)
    assert db.query(Like).count() == 1
    routes.toggle_like(10, request_for(1, "token", "/post/10/like"), csrf_token="token", db=db)
    assert db.query(Like).count() == 0

    routes.add_comment(
        10,
        request_for(1, "token", "/post/10/comment"),
        body="hello",
        csrf_token="token",
        db=db,
    )
    comment = db.query(Comment).one()
    routes.delete_comment(
        comment.id,
        request_for(1, "token", "/comment/1/delete"),
        csrf_token="token",
        db=db,
    )
    assert db.query(Comment).count() == 0
    assert [event.event_type for event in db.query(GraphOutboxEvent).order_by(GraphOutboxEvent.created_at)] == [
        "follow.created",
        "follow.deleted",
        "like.created",
        "like.deleted",
        "comment.created",
        "comment.deleted",
    ]


def test_post_create_and_delete_are_atomic_with_outbox(db, monkeypatch, tmp_path):
    add_user(db, 1, "alice")
    monkeypatch.setattr(routes, "UPLOAD_DIR", str(tmp_path))
    request = request_for(1, "token", "/post/create")
    upload = UploadFile(
        filename="photo.jpg",
        file=BytesIO(b"image-bytes"),
        headers=Headers({"content-type": "image/jpeg"}),
    )

    response = asyncio.run(
        routes.create_post(
            request,
            caption="hello",
            image=upload,
            csrf_token="token",
            db=db,
        )
    )

    post = db.query(Post).one()
    assert response.status_code == 302
    assert db.query(GraphOutboxEvent).filter_by(event_type="post.created").count() == 1
    assert (tmp_path / post.image_path).exists()

    routes.delete_post(
        post.id,
        request_for(1, "token", f"/post/{post.id}/delete"),
        csrf_token="token",
        db=db,
    )
    assert db.query(Post).count() == 0
    assert db.query(GraphOutboxEvent).filter_by(event_type="post.deleted").count() == 1


def test_private_messages_create_a_conversation_and_message(db):
    add_user(db, 1, "alice")
    add_user(db, 2, "bob")

    response = routes.send_message(
        "bob",
        request_for(1, "token", "/messages/bob"),
        body="Hey Bob!",
        csrf_token="token",
        db=db,
    )

    assert response.status_code == 303
    assert db.query(Conversation).count() == 1
    message = db.query(Message).one()
    assert message.sender_id == 1
    assert message.body == "Hey Bob!"


def test_chatbot_uses_post_context(db):
    add_user(db, 1, "alice")
    post = Post(id=10, user_id=1, caption="A Goa sunset #goa", image_path="goa.jpg")
    db.add(post)
    db.commit()

    response = routes.chatbot(
        request_for(1, "token", "/chatbot"),
        question="What is this post about?",
        post_id=10,
        db=db,
    )

    assert response["ok"] is True
    assert "A Goa sunset" in response["answer"]
