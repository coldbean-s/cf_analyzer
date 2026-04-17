"""GitHub OAuth + JWT authentication for CF Analyzer Web."""

import secrets
from datetime import datetime, timezone, timedelta

import httpx
from jose import jwt, JWTError
from fastapi import APIRouter, Request, Response, Depends, HTTPException
from fastapi.responses import RedirectResponse

import config
import db

router = APIRouter()

# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 7


def create_jwt(user_id: int, github_login: str) -> str:
    payload = {
        "sub": str(user_id),
        "login": github_login,
        "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict | None:
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

async def get_optional_user(request: Request) -> db.User | None:
    """Extract user from JWT cookie or Authorization header. Returns None if absent/invalid."""
    token = request.cookies.get("token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        return None

    payload = decode_jwt(token)
    if not payload:
        return None

    user_id = int(payload["sub"])
    return await db.get_user_by_id(user_id)


async def require_user(user: db.User | None = Depends(get_optional_user)) -> db.User:
    """Require authenticated user. Raises 401 if not logged in."""
    if user is None:
        raise HTTPException(status_code=401, detail="未登录，请先通过 GitHub 登录")
    return user


async def require_admin(user: db.User = Depends(require_user)) -> db.User:
    """Require admin user. Raises 403 if not admin."""
    if user.github_id not in config.ADMIN_GITHUB_IDS:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ---------------------------------------------------------------------------
# OAuth routes
# ---------------------------------------------------------------------------

@router.get("/auth/github")
async def github_login(request: Request):
    """Redirect to GitHub OAuth authorization page."""
    if not config.GITHUB_CLIENT_ID:
        raise HTTPException(500, "GITHUB_CLIENT_ID not configured")

    state = secrets.token_urlsafe(32)
    # Store state in a short-lived cookie for CSRF verification
    redirect_url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={config.GITHUB_CLIENT_ID}"
        f"&scope=read:user"
        f"&state={state}"
    )
    response = RedirectResponse(redirect_url)
    response.set_cookie("oauth_state", state, max_age=600, httponly=True, samesite="lax")
    return response


@router.get("/auth/callback")
async def github_callback(request: Request, code: str, state: str):
    """GitHub OAuth callback: exchange code → token → user info → JWT."""
    # Verify CSRF state
    saved_state = request.cookies.get("oauth_state")
    if not saved_state or saved_state != state:
        raise HTTPException(400, "OAuth state mismatch")

    # Exchange code for access token
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": config.GITHUB_CLIENT_ID,
                "client_secret": config.GITHUB_CLIENT_SECRET,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        token_data = token_resp.json()

    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(400, f"GitHub OAuth failed: {token_data.get('error_description', 'unknown')}")

    # Fetch user info
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        user_data = user_resp.json()

    github_id = user_data["id"]
    github_login = user_data["login"]
    avatar_url = user_data.get("avatar_url", "")

    # Upsert user in database
    user = await db.upsert_user(github_id, github_login, avatar_url)

    # Create JWT and set as httpOnly cookie
    token = create_jwt(user.id, github_login)
    response = RedirectResponse("/")
    is_prod = request.url.scheme == "https"
    response.set_cookie(
        "token", token,
        max_age=JWT_EXPIRE_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=is_prod,
    )
    response.delete_cookie("oauth_state")
    return response


@router.post("/auth/logout")
async def logout():
    response = Response(status_code=200)
    response.delete_cookie("token")
    return response


@router.get("/api/me")
async def get_me(user: db.User = Depends(require_user)):
    return {
        "id": user.id,
        "github_login": user.github_login,
        "github_avatar_url": user.github_avatar_url,
        "is_admin": user.github_id in config.ADMIN_GITHUB_IDS,
    }
