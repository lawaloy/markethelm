"""Registration and login for hosted multi-user mode."""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel, Field

import dashboard.backend.auth as auth
from dashboard.backend.account_email import send_account_email
from src.storage.account_tokens import (
    RESET_PASSWORD,
    VERIFY_EMAIL,
    consume_token,
    issue_token,
    revoke_tokens,
)
from src.storage.database import database_enabled, init_database
from src.storage.session import AuthError, create_access_token, ensure_auth_secret
from src.storage.users import (
    MAX_EMAIL_LENGTH,
    MAX_PASSWORD_LENGTH,
    UserError,
    authenticate_user,
    change_password,
    create_user,
    delete_user_account,
    get_user_by_email,
    get_user_by_id,
    mark_email_verified,
    revoke_user_sessions,
    update_password,
)

router = APIRouter()


class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=MAX_EMAIL_LENGTH)
    password: str = Field(..., min_length=8, max_length=MAX_PASSWORD_LENGTH)


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=MAX_EMAIL_LENGTH)
    password: str = Field(..., max_length=MAX_PASSWORD_LENGTH)


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class UserResponse(BaseModel):
    id: str
    email: str
    created_at: str
    email_verified: bool = False


class EmailRequest(BaseModel):
    email: str = Field(..., max_length=MAX_EMAIL_LENGTH)


class TokenRequest(BaseModel):
    token: str = Field(..., min_length=20, max_length=256)


class PasswordResetConfirm(TokenRequest):
    password: str = Field(..., min_length=8, max_length=MAX_PASSWORD_LENGTH)


class MessageResponse(BaseModel):
    message: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(..., min_length=8, max_length=MAX_PASSWORD_LENGTH)


class DeleteAccountRequest(BaseModel):
    current_password: str = Field(..., max_length=MAX_PASSWORD_LENGTH)
    confirmation: str = Field(..., max_length=16)


def _verification_required() -> bool:
    return (os.environ.get("MARKETHELM_REQUIRE_EMAIL_VERIFICATION") or "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _send_token(user: dict, purpose: str) -> bool:
    token = issue_token(user["id"], purpose)
    if send_account_email(recipient=user["email"], purpose=purpose, token=token):
        return True
    revoke_tokens(user["id"], purpose)
    return False


def _require_multi_user() -> None:
    if not database_enabled():
        raise HTTPException(
            status_code=501,
            detail="Multi-user mode is disabled. Set MARKETHELM_DATABASE_URL to enable.",
        )


@router.post("/register", response_model=AuthResponse)
async def register(body: RegisterRequest, background_tasks: BackgroundTasks) -> AuthResponse:
    _require_multi_user()
    init_database()
    # Fail before create_user so a missing/short AUTH_SECRET cannot orphan an account
    # that then blocks retry with "email already exists".
    try:
        ensure_auth_secret()
    except AuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    try:
        user = create_user(body.email, body.password)
    except UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    user["email_verified"] = False
    background_tasks.add_task(_send_token, user, VERIFY_EMAIL)
    try:
        token = create_access_token(user["id"], session_version=user["session_version"])
    except AuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return AuthResponse(access_token=token, user=user)


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest) -> AuthResponse:
    _require_multi_user()
    init_database()
    user = authenticate_user(body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    full_user = get_user_by_id(user["id"]) or user
    if _verification_required() and not full_user.get("email_verified"):
        raise HTTPException(status_code=403, detail="Verify your email before signing in.")
    try:
        token = create_access_token(user["id"], session_version=full_user["session_version"])
    except AuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return AuthResponse(access_token=token, user=full_user)


@router.post("/verify-email/request", response_model=MessageResponse)
async def request_email_verification(
    body: EmailRequest, background_tasks: BackgroundTasks
) -> MessageResponse:
    _require_multi_user()
    init_database()
    user = get_user_by_email(body.email)
    if user and not user.get("email_verified"):
        background_tasks.add_task(_send_token, user, VERIFY_EMAIL)
    return MessageResponse(message="If the account exists, a verification email has been sent.")


@router.post("/verify-email/confirm", response_model=MessageResponse)
async def confirm_email_verification(body: TokenRequest) -> MessageResponse:
    _require_multi_user()
    init_database()
    user_id = consume_token(body.token, VERIFY_EMAIL)
    if not user_id:
        raise HTTPException(status_code=400, detail="This verification link is invalid or expired.")
    mark_email_verified(user_id)
    return MessageResponse(message="Email verified. You can now sign in.")


@router.post("/password-reset/request", response_model=MessageResponse)
async def request_password_reset(
    body: EmailRequest, background_tasks: BackgroundTasks
) -> MessageResponse:
    _require_multi_user()
    init_database()
    user = get_user_by_email(body.email)
    if user:
        background_tasks.add_task(_send_token, user, RESET_PASSWORD)
    return MessageResponse(message="If the account exists, a password reset email has been sent.")


@router.post("/password-reset/confirm", response_model=MessageResponse)
async def confirm_password_reset(body: PasswordResetConfirm) -> MessageResponse:
    _require_multi_user()
    init_database()
    user_id = consume_token(body.token, RESET_PASSWORD)
    if not user_id:
        raise HTTPException(status_code=400, detail="This reset link is invalid or expired.")
    try:
        update_password(user_id, body.password)
    except UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    revoke_tokens(user_id, RESET_PASSWORD)
    return MessageResponse(message="Password updated. You can now sign in.")


@router.get("/me", response_model=UserResponse)
async def me(authorization: Optional[str] = Header(default=None)) -> UserResponse:
    _require_multi_user()
    user_id = await auth.require_user_id(authorization)
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found.")
    return UserResponse(**user)


@router.post("/logout", response_model=MessageResponse)
async def logout(authorization: Optional[str] = Header(default=None)) -> MessageResponse:
    _require_multi_user()
    user_id = await auth.require_user_id(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required.")
    revoke_user_sessions(user_id)
    return MessageResponse(message="Signed out from all sessions.")


@router.post("/password/change", response_model=MessageResponse)
async def change_account_password(
    body: ChangePasswordRequest,
    authorization: Optional[str] = Header(default=None),
) -> MessageResponse:
    _require_multi_user()
    user_id = await auth.require_user_id(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        change_password(user_id, body.current_password, body.new_password)
    except UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MessageResponse(message="Password changed. Sign in again on every device.")


@router.delete("/account", response_model=MessageResponse)
async def delete_account(
    body: DeleteAccountRequest,
    authorization: Optional[str] = Header(default=None),
) -> MessageResponse:
    _require_multi_user()
    user_id = await auth.require_user_id(authorization)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if body.confirmation != "DELETE":
        raise HTTPException(status_code=400, detail="Type DELETE to confirm account deletion.")
    try:
        delete_user_account(user_id, body.current_password)
    except UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MessageResponse(message="Account permanently deleted.")
