"""鉴权：JWT 签发/校验 + 角色守卫。

设计取舍（ADR-05/D-06）：
- 用 JWT 而非 session：前后端分离，免服务端会话存储
- 角色只有 student / admin 两种，够演示"数据隔离"与"越权拦截"
- 登录失败**不区分**"用户不存在"与"密码错误"，避免账号枚举
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import Blueprint, g, request
from werkzeug.security import check_password_hash, generate_password_hash

from app import captcha as captcha_mod
from app.contract import new_ulid, ok
from app.errors import Forbidden, InvalidParam, Unauthenticated, UserDuplicated
from app.models import User, db
from app.runtime import current_settings
from app.schemas import LoginRequest, RegisterRequest, validate
from shared.enums import ROLE_ADMIN, ROLE_STUDENT, ROLES

PASSWORD_METHOD = "pbkdf2:sha256:600000"  # 长度 ~102 字符，适配 VARCHAR(128)

bp = Blueprint("auth", __name__)

# ---------- 简易登录限流（进程内，单实例足够；多实例需换 Redis）----------
_LOGIN_WINDOW_SECONDS = 60
_login_attempts: dict[str, list[float]] = {}
# 注册单独一个桶：登录撞限流不该连带把"注册"也锁死，反之亦然。
# 阈值取登录限额的一半（下限 3）——注册是低频动作，没必要给同样宽的额度。
_register_attempts: dict[str, list[float]] = {}


def _rate_limit_login(ip: str) -> None:
    limit = current_settings().login_rate_limit_per_minute
    if limit <= 0:
        return  # 测试或压测环境显式关闭
    now = time.monotonic()
    bucket = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW_SECONDS]
    if len(bucket) >= limit:
        raise InvalidParam("登录尝试过于频繁，请稍后再试")
    bucket.append(now)
    _login_attempts[ip] = bucket


def reset_login_attempts() -> None:
    """测试与运维用：清空计数器（正常业务不需要调用）。"""
    _login_attempts.clear()
    _register_attempts.clear()


def _rate_limit_register(ip: str) -> None:
    limit = current_settings().login_rate_limit_per_minute
    if limit <= 0:
        return
    limit = max(3, limit // 2)
    now = time.monotonic()
    bucket = [t for t in _register_attempts.get(ip, []) if now - t < _LOGIN_WINDOW_SECONDS]
    if len(bucket) >= limit:
        raise InvalidParam("注册尝试过于频繁，请稍后再试")
    bucket.append(now)
    _register_attempts[ip] = bucket


def hash_password(plain: str) -> str:
    return generate_password_hash(plain, method=PASSWORD_METHOD)


def verify_password(plain: str, hashed: str) -> bool:
    return check_password_hash(hashed, plain)


# ---------- token ----------
def issue_token(user: User) -> tuple[str, int]:
    settings = current_settings()
    expires_in = settings.jwt_expire_minutes * 60
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_in


def _decode(token: str) -> dict:
    settings = current_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        raise Unauthenticated("登录已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise Unauthenticated("登录凭证无效")


def _bearer_token() -> str:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise Unauthenticated("缺少 Authorization: Bearer <token>")
    token = header[7:].strip()
    if not token:
        raise Unauthenticated("缺少 Authorization: Bearer <token>")
    return token


def current_user() -> User:
    """取当前登录用户；已在本请求内解析过则直接复用。"""
    cached = getattr(g, "current_user", None)
    if cached is not None:
        return cached
    payload = _decode(_bearer_token())
    user = db.session.get(User, payload.get("sub"))
    if user is None:
        # 令牌有效但用户已被删除：按未登录处理，不泄露原因
        raise Unauthenticated("登录已过期，请重新登录")
    g.current_user = user
    return user


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        # 先解析再进视图，保证视图内一定拿得到 g.current_user
        current_user()
        return view(*args, **kwargs)

    return wrapper


def role_required(*roles: str):
    """角色守卫。角色不符返回 403（T-22）；资源归属不符应返回 404，两者语义不同。"""

    unknown = set(roles) - set(ROLES)
    if unknown:
        raise RuntimeError(f"未定义的角色：{unknown}")

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user.role not in roles:
                raise Forbidden("当前角色无权执行该操作")
            return view(*args, **kwargs)

        return wrapper

    return decorator


def admin_required(view):
    return role_required(ROLE_ADMIN)(view)


# ---------- 路由 ----------
@bp.post("/auth/login")
def login():
    _rate_limit_login(request.headers.get("X-Forwarded-For", request.remote_addr or "local"))
    payload = validate(LoginRequest, request.get_json(silent=True) or {})
    user = db.session.execute(
        db.select(User).where(User.username == payload.username)
    ).scalar_one_or_none()
    # 用户不存在与密码错误返回同一文案与同一状态码，避免账号枚举
    if user is None or not verify_password(payload.password, user.password_hash):
        raise Unauthenticated("用户名或密码错误")
    token, expires_in = issue_token(user)
    return ok({
        "token": token,
        "expires_in": expires_in,
        "user": user.to_public(),
    })


@bp.get("/auth/me")
@login_required
def me():
    return ok(current_user().to_public())


# ---------- 数字验证码 / 自助注册 ----------
@bp.get("/auth/captcha")
def captcha_image():
    """签发一张数字验证码图。

    公开接口（注册前当然没有令牌）。**不返回明文**，明文只在服务端存储里，
    客户端拿到的是 `captcha_id` 与一张 `data:image/svg+xml;base64,...`。
    """
    return ok(captcha_mod.issue())


@bp.post("/auth/register")
def register():
    """学生自助注册：姓名 + 学号 + 数字验证码（密码可选）。

    **学号即用户名**：这样既满足"注册要填学号"，又不用给 `users` 表加列——
    加列要写迁移，而本项目用 `create_all()` 建表，老库不会被自动改结构。
    管理端账号不开放注册（需求里注册栏只要求填学生姓名与学号）。
    """
    _rate_limit_register(request.headers.get("X-Forwarded-For", request.remote_addr or "local"))
    payload = validate(RegisterRequest, request.get_json(silent=True) or {})

    # 先验验证码再查库：让"学号是否已注册"这个信息不能绕过验证码去探
    captcha_mod.verify(payload.captcha_id, payload.captcha_code)

    existing = db.session.execute(
        db.select(User).where(User.username == payload.student_no)
    ).scalar_one_or_none()
    if existing is not None:
        raise UserDuplicated("该学号已注册，请直接登录")

    custom = payload.password is not None
    plain = payload.password if custom else payload.student_no[-6:]
    user = User(
        id=new_ulid(),
        username=payload.student_no,
        display_name=payload.display_name,
        password_hash=hash_password(plain),
        role=ROLE_STUDENT,
    )
    db.session.add(user)
    db.session.commit()

    token, expires_in = issue_token(user)
    return ok({
        "token": token,
        "expires_in": expires_in,
        "user": user.to_public(),
        # 没填密码时告诉用户初始口令怎么来的。这不是泄露——它是学号的后缀，
        # 用户本来就有；藏着不说才会让人第二次登录不进去。
        "password_source": "custom" if custom else "student_no_suffix",
        "initial_password": None if custom else plain,
    })
