"""Authentication: who is making this request?

Flow:
1. POST /api/auth/login with email + password. If correct, we create a session row and set an
   HttpOnly cookie holding a random token. JavaScript can't read HttpOnly cookies, so a
   malicious script on the page can't steal the session.
2. The browser sends the cookie automatically on every request. `current_user` looks up the
   session by the token's hash, checks expiry and that the user is still active.
3. Requests that change data (POST/PUT/PATCH/DELETE) must also send the CSRF token in an
   X-CSRF-Token header. Another website can make your browser send the cookie, but it can't read
   the CSRF token, so it can't forge that header.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Session as LoginSession
from app.models import User
from app.security import hash_token, new_token, tokens_equal, verify_password

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "X-CSRF-Token"

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    groups: list[str]

    @classmethod
    def of(cls, user: User) -> "UserOut":
        return cls(
            id=str(user.id),
            email=user.email,
            display_name=user.display_name,
            role=user.role.value,
            groups=sorted(g.slug for g in user.groups),
        )


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str


def _unauthorized() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in.")


def current_session(request: Request, db: Session = Depends(get_db)) -> LoginSession:
    """Resolve the login session from the cookie, and enforce CSRF on state-changing requests."""
    token = request.cookies.get(get_settings().session_cookie_name)
    if not token:
        raise _unauthorized()
    session = db.get(LoginSession, hash_token(token))
    if session is None or session.expires_at <= datetime.now(UTC) or not session.user.is_active:
        raise _unauthorized()
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not tokens_equal(sent, session.csrf_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing or invalid CSRF token.")
    return session


def current_user(session: LoginSession = Depends(current_session)) -> User:
    return session.user


@router.post("/login")
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)) -> SessionOut:
    settings = get_settings()
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    # Always verify (against a dummy hash if needed) so timing doesn't reveal valid emails, and
    # return one generic message for every failure.
    ok = verify_password(body.password, user.password_hash if user else None)
    if not ok or user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password.")

    token = new_token()
    session = LoginSession(
        token_hash=hash_token(token),
        user_id=user.id,
        csrf_token=new_token(),
        expires_at=datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours),
    )
    db.add(session)
    db.commit()
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,  # not readable by JavaScript
        secure=settings.cookie_secure,  # HTTPS-only outside local development
        samesite="lax",  # not sent on cross-site POSTs
        path="/",
    )
    return SessionOut(user=UserOut.of(user), csrf_token=session.csrf_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    session: LoginSession = Depends(current_session),
    db: Session = Depends(get_db),
) -> None:
    db.execute(delete(LoginSession).where(LoginSession.token_hash == session.token_hash))
    db.commit()
    response.delete_cookie(get_settings().session_cookie_name, path="/")


@router.get("/me")
def me(session: LoginSession = Depends(current_session)) -> SessionOut:
    """Who am I? The web app calls this on load to restore the session and get the CSRF token."""
    return SessionOut(user=UserOut.of(session.user), csrf_token=session.csrf_token)
