import os
import shutil
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, Post, Like, Comment, Follow
from app.auth import (
    hash_password,
    verify_password,
    create_session_token,
    get_user_id_from_token,
)

router = APIRouter()
templates = Jinja2Templates(directory="templates")

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ── Helper ───────────────────────────────────────────────────
def get_current_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get("session_token")
    if not token:
        return None
    user_id = get_user_id_from_token(token)
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


def ctx(request, db, **kwargs):
    """Build a base template context with current_user."""
    return {"request": request, "current_user": get_current_user(request, db), **kwargs}


# ── Home ─────────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/feed", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


# ── Register ─────────────────────────────────────────────────
@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
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
    db: Session = Depends(get_db),
):
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
    db.commit()
    return RedirectResponse(url="/login?registered=true", status_code=302)

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
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
    db: Session = Depends(get_db),
):
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
    response.set_cookie(key="session_token", value=token, httponly=True, samesite="lax", max_age=7*24*3600)
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session_token")
    return response


@router.get("/profile", response_class=HTMLResponse)
def my_profile(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    posts = db.query(Post).filter(Post.user_id == user.id).order_by(Post.created_at.desc()).all()
    followers_count = db.query(Follow).filter(Follow.followed_id == user.id).count()
    following_count = db.query(Follow).filter(Follow.follower_id == user.id).count()
    return templates.TemplateResponse("profile.html", ctx(request, db, profile_user=user, is_own_profile=True, posts=posts, followers_count=followers_count, following_count=following_count, is_following=False))


@router.get("/profile/{username}", response_class=HTMLResponse)
def user_profile(username: str, request: Request, db: Session = Depends(get_db)):
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
def toggle_follow(username: str, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    profile_user = db.query(User).filter(User.username == username.lower()).first()
    if not profile_user or profile_user.id == user.id:
        return RedirectResponse(url=f"/profile/{username}", status_code=302)
    follow = db.query(Follow).filter(Follow.follower_id == user.id, Follow.followed_id == profile_user.id).first()
    if follow:
        db.delete(follow)
    else:
        db.add(Follow(follower_id=user.id, followed_id=profile_user.id))
    db.commit()
    return RedirectResponse(url=f"/profile/{profile_user.username}", status_code=302)


@router.get("/profile-edit", response_class=HTMLResponse)
def edit_profile_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("edit_profile.html", ctx(request, db, user=user))


@router.post("/profile-edit", response_class=HTMLResponse)
def edit_profile(
    request: Request,
    full_name: str = Form(""),
    bio: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    user.full_name = full_name.strip() if full_name else None
    user.bio = bio.strip() if bio else None
    db.commit()
    return RedirectResponse(url="/profile", status_code=302)


# ── Feed ─────────────────────────────────────────────────────
@router.get("/feed", response_class=HTMLResponse)
def feed(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    posts = db.query(Post).order_by(Post.created_at.desc()).all()
    return templates.TemplateResponse("feed.html", ctx(request, db, posts=posts))


# ── Create Post ──────────────────────────────────────────────
@router.get("/post/create", response_class=HTMLResponse)
def create_post_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("create_post.html", ctx(request, db))


@router.post("/post/create", response_class=HTMLResponse)
async def create_post(
    request: Request,
    caption: str = Form(""),
    image: UploadFile = File(...),
    db: Session = Depends(get_db),
):
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

    post = Post(caption=caption.strip(), image_path=filename, user_id=user.id)
    db.add(post)
    db.commit()
    return RedirectResponse(url="/feed", status_code=302)


# ── Delete Post ──────────────────────────────────────────────
@router.post("/post/{post_id}/delete")
def delete_post(post_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    post = db.query(Post).filter(Post.id == post_id, Post.user_id == user.id).first()
    if post:
        # Delete image file
        img_path = os.path.join(UPLOAD_DIR, post.image_path)
        if os.path.exists(img_path):
            os.remove(img_path)
        db.delete(post)
        db.commit()

    return RedirectResponse(url="/feed", status_code=302)


# ── Like / Unlike ────────────────────────────────────────────
def _back(request: Request, fallback: str = "/feed") -> RedirectResponse:
    """Redirect back to the page the user came from."""
    return RedirectResponse(url=request.headers.get("referer") or fallback, status_code=303)


@router.post("/post/{post_id}/like")
def toggle_like(post_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    post = db.query(Post).filter(Post.id == post_id).first()
    if not post:
        return _back(request)

    existing = db.query(Like).filter(Like.user_id == user.id, Like.post_id == post_id).first()
    if existing:
        db.delete(existing)  # already liked -> unlike
    else:
        db.add(Like(user_id=user.id, post_id=post_id))
    db.commit()
    return _back(request)


# ── Comments ─────────────────────────────────────────────────
@router.post("/post/{post_id}/comment")
def add_comment(
    post_id: int,
    request: Request,
    body: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    post = db.query(Post).filter(Post.id == post_id).first()
    body = body.strip()
    if post and body:
        db.add(Comment(body=body, user_id=user.id, post_id=post_id))
        db.commit()
    return _back(request)


@router.post("/comment/{comment_id}/delete")
def delete_comment(comment_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    comment = db.query(Comment).filter(Comment.id == comment_id).first()
    # Comment author or the post owner may delete it
    if comment and (comment.user_id == user.id or comment.post.user_id == user.id):
        db.delete(comment)
        db.commit()
    return _back(request)