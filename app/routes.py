import os
import hmac
import secrets
import shutil
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, Query, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload, selectinload

from app.database import get_db
from app.config import COOKIE_SECURE, SESSION_MAX_AGE_SECONDS
from app.models import (
    User,
    Post,
    Like,
    Comment,
    Follow,
    Conversation,
    ConversationParticipant,
    Message,
    PostMedia,
    DraftPost,
)
from app.auth import (
    hash_password,
    verify_password,
    create_session_token,
    get_user_id_from_token,
)
from app.graph_outbox import enqueue_graph_event
from app.feed import get_feed_posts
from app.hashtags import extract_hashtags, hashtags_for_image
from app.chatbot import answer_question
from app.database import SessionLocal
from app.realtime import chat_manager

router = APIRouter()
templates = Jinja2Templates(directory="templates")

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ── Helper ───────────────────────────────────────────────────
def get_current_user(request: Request, db: Session) -> User | None:
    """Resolve the signed-in user from the session cookie, if valid."""
    token = request.cookies.get("session_token")
    if not token:
        return None
    user_id = get_user_id_from_token(token)
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


def get_csrf_token(request: Request) -> str:
    """Return or create the session's CSRF token for state-changing forms."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def validate_csrf(request: Request, submitted_token: str) -> None:
    """Reject a request whose submitted CSRF token does not match the session."""
    expected_token = request.session.get("csrf_token")
    if not expected_token or not hmac.compare_digest(expected_token, submitted_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def wants_json(request: Request) -> bool:
    """Return true for fetch requests made by the interactive UI."""
    return request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"


def action_response(request: Request, payload: dict, fallback: str = "/feed"):
    """Return JSON to AJAX clients and a redirect to traditional form clients."""
    if wants_json(request):
        return JSONResponse(payload)
    return _back(request, fallback)


def ctx(request, db, **kwargs):
    """Build a base template context with current_user."""
    return {
        "request": request,
        "current_user": get_current_user(request, db),
        "csrf_token": get_csrf_token(request),
        **kwargs,
    }


def _sidebar_users(db: Session, user: User, limit: int = 5) -> list[tuple[User, bool]]:
    """Build right-rail suggestions with the current follow state."""
    following_ids = {
        row.followed_id
        for row in db.query(Follow).filter(Follow.follower_id == user.id).all()
    }
    candidates = (
        db.query(User)
        .filter(User.id != user.id)
        .order_by(User.created_at.desc(), User.username.asc())
        .limit(limit)
        .all()
    )
    return [(candidate, candidate.id in following_ids) for candidate in candidates]


# ── Home ─────────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    """Redirect authenticated users to the feed and guests to login."""
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/feed", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


# ── Register ─────────────────────────────────────────────────
@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    """Render the account registration page for unauthenticated visitors."""
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/feed", status_code=302)
    return templates.TemplateResponse("register.html", ctx(request, db))


@router.post("/register", response_class=HTMLResponse)
def register_user(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    full_name: str = Form(""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Validate registration fields, create the user, and enqueue its graph event."""
    validate_csrf(request, csrf_token)
    errors = []
    if password != confirm_password:
        errors.append("Passwords didn't match!")
    if len(password) < 6:
        errors.append("Password must be at least 6 characters!")
    if len(username) < 3:
        errors.append("Username must be at least 3 characters!")
    if db.query(User).filter(User.username == username.lower().strip()).first():
        errors.append(f"Username '{username}' already taken!")
    if db.query(User).filter(User.email == email.lower().strip()).first():
        errors.append("Email already registered!")

    if errors:
        return templates.TemplateResponse(
            "register.html",
            ctx(request, db, errors=errors, username=username, email=email, full_name=full_name),
        )

    new_user = User(
        username=username.lower().strip(),
        email=email.lower().strip(),
        password_hash=hash_password(password),
        full_name=full_name.strip() if full_name else None,
    )
    db.add(new_user)
    db.flush()
    enqueue_graph_event(
        db,
        "user.created",
        {"user_id": new_user.id, "username": new_user.username},
    )
    db.commit()
    return RedirectResponse(url="/login?registered=true", status_code=302)

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    """Render the login page and optional post-registration success message."""
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/feed", status_code=302)
    registered = request.query_params.get("registered")
    return templates.TemplateResponse("login.html", ctx(request, db, registered=registered))


@router.post("/login", response_class=HTMLResponse)
def login_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Authenticate a user and issue a secure session cookie."""
    validate_csrf(request, csrf_token)
    user = db.query(User).filter(
        (User.username == username.lower().strip()) |
        (User.email == username.lower().strip())
    ).first()

    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            ctx(request, db, errors=["Wrong username or password!"], username=username),
        )

    token = create_session_token(user.id)
    response = RedirectResponse(url="/feed", status_code=302)
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=SESSION_MAX_AGE_SECONDS,
    )
    return response


@router.get("/logout")
def logout():
    """Clear the session cookie and return the visitor to login."""
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session_token")
    return response


@router.get("/profile", response_class=HTMLResponse)
def my_profile(request: Request, db: Session = Depends(get_db)):
    """Render the current user's profile, posts, and relationship statistics."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    posts = db.query(Post).filter(Post.user_id == user.id).order_by(Post.created_at.desc()).all()
    followers_count = db.query(Follow).filter(Follow.followed_id == user.id).count()
    following_count = db.query(Follow).filter(Follow.follower_id == user.id).count()
    return templates.TemplateResponse("profile.html", ctx(request, db, profile_user=user, is_own_profile=True, posts=posts, followers_count=followers_count, following_count=following_count, is_following=False))


@router.get("/profile/{username}", response_class=HTMLResponse)
def user_profile(username: str, request: Request, db: Session = Depends(get_db)):
    """Render a public profile and determine the viewer's follow state."""
    profile_user = db.query(User).filter(User.username == username.lower()).first()
    if not profile_user:
        return templates.TemplateResponse("error.html", ctx(request, db, detail="User not found!"), status_code=404)
    current_user = get_current_user(request, db)
    is_own = current_user and current_user.id == profile_user.id
    posts = db.query(Post).filter(Post.user_id == profile_user.id).order_by(Post.created_at.desc()).all()
    followers_count = db.query(Follow).filter(Follow.followed_id == profile_user.id).count()
    following_count = db.query(Follow).filter(Follow.follower_id == profile_user.id).count()
    is_following = bool(current_user and db.query(Follow).filter(Follow.follower_id == current_user.id, Follow.followed_id == profile_user.id).first())
    return templates.TemplateResponse("profile.html", ctx(request, db, profile_user=profile_user, is_own_profile=is_own, posts=posts, followers_count=followers_count, following_count=following_count, is_following=is_following))


@router.post("/profile/{username}/follow")
def toggle_follow(
    username: str,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Follow or unfollow a profile and return updated relationship counts."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    profile_user = db.query(User).filter(User.username == username.lower()).first()
    if not profile_user or profile_user.id == user.id:
        return RedirectResponse(url=f"/profile/{username}", status_code=302)
    follow = db.query(Follow).filter(Follow.follower_id == user.id, Follow.followed_id == profile_user.id).first()
    if follow:
        db.delete(follow)
        event_type = "follow.deleted"
        is_following = False
    else:
        db.add(Follow(follower_id=user.id, followed_id=profile_user.id))
        event_type = "follow.created"
        is_following = True
    enqueue_graph_event(
        db,
        event_type,
        {"follower_id": user.id, "followed_id": profile_user.id},
    )
    db.commit()
    followers_count = db.query(Follow).filter(Follow.followed_id == profile_user.id).count()
    profile_following_count = db.query(Follow).filter(Follow.follower_id == profile_user.id).count()
    viewer_following_count = db.query(Follow).filter(Follow.follower_id == user.id).count()
    return action_response(
        request,
        {
            "ok": True,
            "is_following": is_following,
            "followers_count": followers_count,
            "following_count": profile_following_count,
            "viewer_following_count": viewer_following_count,
        },
        f"/profile/{profile_user.username}",
    )


@router.get("/profile-edit", response_class=HTMLResponse)
def edit_profile_page(request: Request, db: Session = Depends(get_db)):
    """Render the signed-in user's profile editing form."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("edit_profile.html", ctx(request, db, user=user))


@router.post("/profile-edit", response_class=HTMLResponse)
def edit_profile(
    request: Request,
    full_name: str = Form(""),
    bio: str = Form(""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Validate and persist the signed-in user's editable profile fields."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    user.full_name = full_name.strip() if full_name else None
    user.bio = bio.strip() if bio else None
    db.commit()
    return RedirectResponse(url="/profile", status_code=302)


# ── Private chat ────────────────────────────────────────────
def _find_conversation(db: Session, first_user_id: int, second_user_id: int) -> Conversation | None:
    """Find a one-to-one conversation shared by two users."""
    conversations = (
        db.query(Conversation)
        .join(ConversationParticipant)
        .filter(ConversationParticipant.user_id == first_user_id)
        .all()
    )
    for conversation in conversations:
        if any(participant.user_id == second_user_id for participant in conversation.participants):
            return conversation
    return None


@router.get("/messages", response_class=HTMLResponse)
def messages_inbox(request: Request, db: Session = Depends(get_db)):
    """Show recent private conversations and available users to message."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    conversations = (
        db.query(Conversation)
        .join(ConversationParticipant)
        .filter(ConversationParticipant.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
        .all()
    )
    conversation_rows = []
    for conversation in conversations:
        other = next((p.user for p in conversation.participants if p.user_id != user.id), None)
        if other:
            conversation_rows.append((conversation, other, conversation.messages[-1] if conversation.messages else None))
    contacts = db.query(User).filter(User.id != user.id).order_by(User.username.asc()).all()
    return templates.TemplateResponse(
        "messages.html",
        ctx(request, db, conversations=conversation_rows, contacts=contacts),
    )


@router.get("/messages/{username}", response_class=HTMLResponse)
def message_thread(username: str, request: Request, db: Session = Depends(get_db)):
    """Render a private thread with another user."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    other = db.query(User).filter(User.username == username.lower()).first()
    if not other or other.id == user.id:
        return templates.TemplateResponse("error.html", ctx(request, db, detail="User not found."), status_code=404)
    conversation = _find_conversation(db, user.id, other.id)
    messages = conversation.messages if conversation else []
    if conversation:
        changed = False
        for message in messages:
            if message.sender_id != user.id and message.read_at is None:
                message.read_at = datetime.now(timezone.utc)
                changed = True
        if changed:
            db.commit()
    return templates.TemplateResponse(
        "chat_thread.html",
        ctx(request, db, other_user=other, messages=messages),
    )


@router.post("/messages/{username}")
def send_message(
    username: str,
    request: Request,
    body: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Send a private message and create the conversation on first contact."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    other = db.query(User).filter(User.username == username.lower()).first()
    body = body.strip()
    if not other or other.id == user.id:
        raise HTTPException(status_code=404, detail="User not found.")
    if not body or len(body) > 2000:
        raise HTTPException(status_code=400, detail="Message must contain 1 to 2000 characters.")

    conversation = _find_conversation(db, user.id, other.id)
    if conversation is None:
        conversation = Conversation()
        db.add(conversation)
        db.flush()
        db.add_all([
            ConversationParticipant(conversation_id=conversation.id, user_id=user.id),
            ConversationParticipant(conversation_id=conversation.id, user_id=other.id),
        ])
    message = Message(conversation_id=conversation.id, sender_id=user.id, body=body)
    conversation.updated_at = datetime.now(timezone.utc)
    db.add(message)
    db.commit()
    room = tuple(sorted((user.id, other.id)))
    chat_manager.publish_from_sync(
        room,
        {"type": "message", "message": _message_payload(message)},
    )
    return action_response(request, {"ok": True, "message": body}, f"/messages/{other.username}")


def _message_payload(message: Message) -> dict:
    """Return the JSON representation shared with connected chat clients."""
    created_at = message.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return {
        "id": message.id,
        "sender_id": message.sender_id,
        "sender_username": message.sender.username,
        "body": message.body,
        "created_at": created_at.isoformat(),
    }


@router.websocket("/ws/messages/{username}")
async def message_socket(websocket: WebSocket, username: str):
    """Authenticate a private-chat socket and relay persisted messages live."""
    token = websocket.cookies.get("session_token")
    user_id = get_user_id_from_token(token) if token else None
    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first() if user_id else None
    other = db.query(User).filter(User.username == username.lower()).first()

    if not user or not other or other.id == user.id:
        await websocket.close(code=1008)
        db.close()
        return

    room = tuple(sorted((user.id, other.id)))
    await chat_manager.connect(room, websocket)
    try:
        while True:
            try:
                data = await websocket.receive_json()
            except ValueError:
                await websocket.send_json({"type": "error", "error": "Send a valid JSON message."})
                continue

            if data.get("type") != "message":
                await websocket.send_json({"type": "error", "error": "Unsupported message type."})
                continue

            body = str(data.get("body", "")).strip()
            if not body or len(body) > 2000:
                await websocket.send_json({"type": "error", "error": "Message must contain 1 to 2000 characters."})
                continue

            conversation = _find_conversation(db, user.id, other.id)
            if conversation is None:
                conversation = Conversation()
                db.add(conversation)
                db.flush()
                db.add_all([
                    ConversationParticipant(conversation_id=conversation.id, user_id=user.id),
                    ConversationParticipant(conversation_id=conversation.id, user_id=other.id),
                ])
            message = Message(conversation_id=conversation.id, sender_id=user.id, body=body)
            conversation.updated_at = datetime.now(timezone.utc)
            db.add(message)
            db.flush()
            payload = {"type": "message", "message": _message_payload(message)}
            db.commit()

            await chat_manager.publish(room, payload)
    except WebSocketDisconnect:
        pass
    finally:
        chat_manager.disconnect(room, websocket)
        db.close()


# ── Luma Assistant ──────────────────────────────────────────
@router.post("/chatbot")
def chatbot(
    request: Request,
    question: str = Form(...),
    post_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Answer an app or post question with the local Luma Assistant."""
    user = get_current_user(request, db)
    if not user:
        return JSONResponse({"ok": False, "error": "Please log in first."}, status_code=401)
    selected_post_id = None
    if post_id:
        try:
            selected_post_id = int(post_id)
        except ValueError:
            selected_post_id = None
    post = db.query(Post).filter(Post.id == selected_post_id).first() if selected_post_id else None
    return {"ok": True, "answer": answer_question(question, post, db=db, user=user)}


# ── Feed ─────────────────────────────────────────────────────
@router.get("/feed", response_class=HTMLResponse)
def feed(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Render a ranked page of feed posts for the authenticated user."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    posts = get_feed_posts(db, user.id, page=page, page_size=limit)
    liked_post_ids = {
        like.post_id
        for like in db.query(Like).filter(Like.user_id == user.id).all()
    }
    return templates.TemplateResponse(
        "feed.html",
        ctx(
            request,
            db,
            posts=posts,
            liked_post_ids=liked_post_ids,
            sidebar_users=_sidebar_users(db, user),
            explore=False,
        ),
    )


@router.get("/explore", response_class=HTMLResponse)
def explore(request: Request, db: Session = Depends(get_db)):
    """Show the complete public post collection, independent of personalization."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    posts = (
        db.query(Post)
        .options(
            joinedload(Post.author),
            selectinload(Post.likes),
            selectinload(Post.comments).selectinload(Comment.author),
        )
        .order_by(Post.created_at.desc(), Post.id.desc())
        .all()
    )
    liked_post_ids = {
        like.post_id
        for like in db.query(Like).filter(Like.user_id == user.id).all()
    }
    return templates.TemplateResponse(
        "feed.html",
        ctx(
            request,
            db,
            posts=posts,
            liked_post_ids=liked_post_ids,
            sidebar_users=_sidebar_users(db, user),
            explore=True,
        ),
    )


@router.get("/notifications", response_class=HTMLResponse)
def notifications(request: Request, db: Session = Depends(get_db)):
    """Show the intentionally empty notifications state until notifications are implemented."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("notifications.html", ctx(request, db))


# ── Create Post ──────────────────────────────────────────────
@router.get("/post/create", response_class=HTMLResponse)
def create_post_page(request: Request, db: Session = Depends(get_db)):
    """Render the image upload form for a new post."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    drafts = [
        draft for draft in db.query(DraftPost).filter(DraftPost.user_id == user.id).order_by(DraftPost.updated_at.desc()).all()
        if (draft.caption or "").strip() or draft.image_paths
    ]
    active_draft = None
    draft_query = request.query_params.get("draft_id")
    if draft_query:
        try:
            active_draft = db.query(DraftPost).filter(
                DraftPost.id == int(draft_query), DraftPost.user_id == user.id
            ).first()
        except ValueError:
            active_draft = None
    return templates.TemplateResponse(
        "create_post.html",
        ctx(request, db, drafts=drafts, active_draft=active_draft, saved=request.query_params.get("saved")),
    )


@router.post("/post/create", response_class=HTMLResponse)
async def create_post(
    request: Request,
    caption: str = Form(""),
    image: UploadFile | None = File(None),
    images: list[UploadFile] = File(default=[]),
    publish: str = Form("1"),
    # Browsers submit an empty string for the hidden field when creating a new
    # draft. Keep this as text at the request boundary, then parse it safely.
    draft_id: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Validate an uploaded image, create its post, and enqueue graph metadata."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    # Multiple files become a carousel; the original single-file parameter is
    # retained so existing clients and tests continue to work.
    image_list = images if isinstance(images, list) else []
    publish_value = publish if isinstance(publish, str) else "1"
    try:
        draft_value = int(draft_id) if draft_id and str(draft_id).strip() else None
    except (TypeError, ValueError):
        draft_value = None
    uploads = ([image] if image and image.filename else []) + [item for item in image_list if item and item.filename]
    allowed = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if any(item.content_type not in allowed for item in uploads):
        return templates.TemplateResponse(
            "create_post.html",
            ctx(request, db, error="Only JPG, PNG, GIF, WEBP allowed!"),
        )

    import time
    saved_paths = []
    for index, upload in enumerate(uploads):
        ext = upload.filename.rsplit(".", 1)[-1]
        filename = f"{user.id}_{int(time.time() * 1000)}_{index}.{ext}"
        with open(os.path.join(UPLOAD_DIR, filename), "wb") as f:
            shutil.copyfileobj(upload.file, f)
        saved_paths.append(filename)

    caption_text = caption.strip()
    existing_draft = db.query(DraftPost).filter(DraftPost.id == draft_value, DraftPost.user_id == user.id).first() if draft_value else None
    if publish_value != "1":
        if not existing_draft and not caption_text and not saved_paths:
            return templates.TemplateResponse(
                "create_post.html",
                ctx(request, db, drafts=[
                    draft for draft in db.query(DraftPost).filter(DraftPost.user_id == user.id).order_by(DraftPost.updated_at.desc()).all()
                    if (draft.caption or "").strip() or draft.image_paths
                ], error="Add a caption or choose at least one image before saving a draft."),
            )
        draft = existing_draft
        if draft is None:
            draft = DraftPost(user_id=user.id)
            db.add(draft)
        draft.caption = caption_text
        draft.image_paths = list(draft.image_paths or []) + saved_paths
        db.commit()
        return RedirectResponse(url="/post/create?saved=1", status_code=303)
    if existing_draft:
        saved_paths = list(existing_draft.image_paths or []) + saved_paths
    if not saved_paths:
        return templates.TemplateResponse("create_post.html", ctx(request, db, error="Choose at least one image to publish."))

    # Image-derived tags are generated only for the first image, as before.
    existing_tags = set(extract_hashtags(caption_text))
    image_tags = [tag for tag in hashtags_for_image(saved_paths[0]) if tag not in existing_tags]
    if image_tags:
        caption_text = f"{caption_text} {' '.join(f'#{tag}' for tag in image_tags)}".strip()
    post = Post(caption=caption_text, image_path=saved_paths[0], user_id=user.id)
    db.add(post)
    db.flush()
    db.add_all([PostMedia(post_id=post.id, image_path=path, position=index) for index, path in enumerate(saved_paths[1:], 1)])
    if existing_draft:
        db.delete(existing_draft)
    enqueue_graph_event(
        db,
        "post.created",
        {
            "post_id": post.id,
            "user_id": post.user_id,
            "caption": post.caption,
            "image_path": post.image_path,
            "created_at": post.created_at.isoformat() if post.created_at else None,
            "hashtags": extract_hashtags(post.caption),
        },
    )
    db.commit()
    return RedirectResponse(url="/feed", status_code=302)


@router.get("/post/{post_id}", response_class=HTMLResponse)
def post_detail(post_id: int, request: Request, db: Session = Depends(get_db)):
    """Show one profile post with the same like and comment controls as the feed."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    post = (
        db.query(Post)
        .options(
            joinedload(Post.author),
            selectinload(Post.likes),
            selectinload(Post.comments).selectinload(Comment.author),
        )
        .filter(Post.id == post_id)
        .first()
    )
    if not post:
        return templates.TemplateResponse("error.html", ctx(request, db, detail="Post not found."), status_code=404)
    return templates.TemplateResponse(
        "post_detail.html",
        ctx(request, db, post=post, is_liked=any(like.user_id == user.id for like in post.likes)),
    )


# ── Delete Post ──────────────────────────────────────────────
@router.post("/post/{post_id}/delete")
def delete_post(
    post_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Delete an owned post, its image file, and its graph projection event."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    post = db.query(Post).filter(Post.id == post_id, Post.user_id == user.id).first()
    if post:
        deleted_post_id = post.id
        # Delete image file
        for image_path in [post.image_path] + [media.image_path for media in post.media]:
            img_path = os.path.join(UPLOAD_DIR, image_path)
            if os.path.exists(img_path):
                os.remove(img_path)
        db.delete(post)
        enqueue_graph_event(db, "post.deleted", {"post_id": deleted_post_id})
        db.commit()

    return action_response(request, {"ok": True, "deleted_post_id": post_id})


# ── Like / Unlike ────────────────────────────────────────────
def _back(request: Request, fallback: str = "/feed") -> RedirectResponse:
    """Redirect back to the page the user came from."""
    return RedirectResponse(url=request.headers.get("referer") or fallback, status_code=303)


@router.post("/post/{post_id}/like")
def toggle_like(
    post_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Toggle a user's like and return the new count to the browser."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    post = db.query(Post).filter(Post.id == post_id).first()
    if not post:
        return _back(request)

    existing = db.query(Like).filter(Like.user_id == user.id, Like.post_id == post_id).first()
    if existing:
        db.delete(existing)  # already liked -> unlike
        event_type = "like.deleted"
        liked = False
    else:
        db.add(Like(user_id=user.id, post_id=post_id))
        event_type = "like.created"
        liked = True
    enqueue_graph_event(db, event_type, {"user_id": user.id, "post_id": post_id})
    db.commit()
    likes_count = db.query(Like).filter(Like.post_id == post_id).count()
    return action_response(
        request,
        {"ok": True, "liked": liked, "likes_count": likes_count},
    )


# ── Comments ─────────────────────────────────────────────────
@router.post("/post/{post_id}/comment")
def add_comment(
    post_id: int,
    request: Request,
    body: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Create a comment and return the rendered comment data to the browser."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    post = db.query(Post).filter(Post.id == post_id).first()
    body = body.strip()
    if post and body:
        comment = Comment(body=body, user_id=user.id, post_id=post_id)
        db.add(comment)
        db.flush()
        enqueue_graph_event(
            db,
            "comment.created",
            {"comment_id": comment.id, "user_id": user.id, "post_id": post_id},
        )
        db.commit()
        return action_response(
            request,
            {
                "ok": True,
                "comment": {
                    "id": comment.id,
                    "username": user.username,
                    "body": comment.body,
                    "created_at": comment.created_at.strftime("%d %b"),
                },
            },
        )
    return action_response(request, {"ok": False, "error": "Comment cannot be empty."})


@router.post("/comment/{comment_id}/delete")
def delete_comment(
    comment_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Delete a comment owned by the user or by the post owner."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    comment = db.query(Comment).filter(Comment.id == comment_id).first()
    # Comment author or the post owner may delete it
    if comment and (comment.user_id == user.id or comment.post.user_id == user.id):
        comment_user_id = comment.user_id
        comment_post_id = comment.post_id
        db.delete(comment)
        has_remaining_comments = db.query(Comment).filter(
            Comment.user_id == comment_user_id,
            Comment.post_id == comment_post_id,
            Comment.id != comment_id,
        ).first() is not None
        enqueue_graph_event(
            db,
            "comment.deleted",
            {
                "comment_id": comment_id,
                "user_id": comment_user_id,
                "post_id": comment_post_id,
                "has_remaining_comments": has_remaining_comments,
            },
        )
        db.commit()
    return action_response(request, {"ok": True, "comment_id": comment_id})
