import os
import hmac
import secrets
import shutil
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import COOKIE_SECURE, SESSION_MAX_AGE_SECONDS
from app.models import User, Post, Like, Comment, Follow
from app.auth import (
    hash_password,
    verify_password,
    create_session_token,
    get_user_id_from_token,
)
from app.graph_outbox import enqueue_graph_event
from app.feed import get_feed_posts
from app.hashtags import extract_hashtags, hashtags_for_image

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
    following_count = db.query(Follow).filter(Follow.follower_id == user.id).count()
    return action_response(
        request,
        {
            "ok": True,
            "is_following": is_following,
            "followers_count": followers_count,
            "following_count": following_count,
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
        ctx(request, db, posts=posts, liked_post_ids=liked_post_ids),
    )


# ── Create Post ──────────────────────────────────────────────
@router.get("/post/create", response_class=HTMLResponse)
def create_post_page(request: Request, db: Session = Depends(get_db)):
    """Render the image upload form for a new post."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("create_post.html", ctx(request, db))


@router.post("/post/create", response_class=HTMLResponse)
async def create_post(
    request: Request,
    caption: str = Form(""),
    image: UploadFile = File(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Validate an uploaded image, create its post, and enqueue graph metadata."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    # Validate image
    allowed = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if image.content_type not in allowed:
        return templates.TemplateResponse(
            "create_post.html",
            ctx(request, db, error="Only JPG, PNG, GIF, WEBP allowed!"),
        )

    # Save file with unique name
    ext = image.filename.rsplit(".", 1)[-1]
    import time
    filename = f"{user.id}_{int(time.time())}.{ext}"
    file_path = os.path.join(UPLOAD_DIR, filename)
    with open(file_path, "wb") as f:
        shutil.copyfileobj(image.file, f)

    caption_text = caption.strip()
    existing_tags = set(extract_hashtags(caption_text))
    image_tags = [tag for tag in hashtags_for_image(image.filename or "photo") if tag not in existing_tags]
    if image_tags:
        caption_text = f"{caption_text} {' '.join(f'#{tag}' for tag in image_tags)}".strip()
    post = Post(caption=caption_text, image_path=filename, user_id=user.id)
    db.add(post)
    db.flush()
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
        img_path = os.path.join(UPLOAD_DIR, post.image_path)
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
