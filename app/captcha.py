"""数字验证码：服务端生成、服务端校验，前端拿到的只是一张图。

## 三个取舍

**为什么自己画 SVG 而不引第三方控件**：本项目前端零依赖（无 UI 库、无图表库），
引一个验证码组件会把"离线可用"这条一起破掉；而 SVG 不需要 PIL、不需要字体文件，
`pymupdf` 那份依赖里已经有的东西一个都不用加。

**为什么存进程内字典**：部署形态是单进程 waitress，量级是"几十个学生同时注册"。
上 Redis 属于过度设计；反过来，如果哪天改多实例，**这里必须换掉**——
否则 A 实例发的码，B 实例校验必然失败（已登记在已知限制里）。

**为什么一码一用、错了也销毁**：验证码的全部价值在于"人没见过这个码"。
留着让用户重试，等于把 4 位数字的穷举成本从 1 次降到 N 次。代价是打错一次要换张图，
这在注册页是可接受的交互（点一下图片就换）。

## 安全边界

这道题拦的是脚本批量注册，不是"防破解"。图上那串数字在 SVG 里是**真文字**，
拿 XML 解析器读一遍就能拿到——所以：

- 服务端只认 `captcha_id`，不认客户端回传的任何"我看到了什么"；
- 一码一用 + 180 秒过期，让"抓一次图慢慢用"的收益趋近于零；
- 真要抗 OCR，得换成扭曲字形的位图，那是另一个量级的工作，本轮明确不做。
"""

from __future__ import annotations

import base64
import html
import random
import secrets
import time

from app.errors import CaptchaInvalid

# 进程内存储：captcha_id -> (明文, 过期时间戳)
_STORE: dict[str, tuple[str, float]] = {}

_TTL_SECONDS = 180
_MAX_ENTRIES = 2000
_LENGTH = 4

# 图上不出现 0/1/6/9/8 这类易混或可旋转的数字？这里保留全部数字——
# 需求写的是"数字验证码"，去掉几个会让用户以为键盘坏了。
_DIGITS = "0123456789"


def _sweep(now: float) -> None:
    """顺手清过期项。

    没有定时任务，也不想为这点内存起一个线程；每次签发时扫一遍足够——
    过期项最多活到下一次签发。超过上限再做一次硬截断，防止被刷爆。
    """
    expired = [key for key, (_, exp) in _STORE.items() if exp <= now]
    for key in expired:
        _STORE.pop(key, None)
    if len(_STORE) > _MAX_ENTRIES:
        # 按过期时间排序，丢最早到期的那些
        keep = sorted(_STORE.items(), key=lambda kv: kv[1][1])[-_MAX_ENTRIES:]
        _STORE.clear()
        _STORE.update(dict(keep))


def _svg(code: str) -> str:
    """把 4 位数字画成一张 108×40 的 SVG。

    每个数字独立旋转、上下抖动，再压几条干扰线与噪点。底色用浅色：
    深色底上画数字在暗色主题里会被压没，浅底这一块在两种主题下都读得出来。
    """
    width, height = 108, 40
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="数字验证码">',
        f'<rect width="{width}" height="{height}" rx="8" fill="#eef4f2"/>',
    ]
    # 干扰线先画，压在字下面
    for _ in range(4):
        x1, y1 = random.randint(0, width), random.randint(0, height)
        x2, y2 = random.randint(0, width), random.randint(0, height)
        parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="#a9c4bd" stroke-width="{random.choice((1, 1, 2))}" '
            f'stroke-opacity="0.55"/>'
        )
    # 噪点
    for _ in range(12):
        parts.append(
            f'<circle cx="{random.randint(0, width)}" cy="{random.randint(0, height)}" '
            f'r="{random.choice((1, 1, 1.5))}" fill="#8fada6" fill-opacity="0.5"/>'
        )
    # 数字
    for index, char in enumerate(code):
        x = 20 + index * 24
        y = 28 + random.randint(-3, 3)
        rotate = random.randint(-18, 18)
        parts.append(
            f'<text x="{x}" y="{y}" fill="#0d2a26" font-family="Georgia,serif" '
            f'font-size="24" font-weight="700" text-anchor="middle" '
            f'transform="rotate({rotate} {x} {y})">{html.escape(char)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def issue(length: int = _LENGTH, plain: str | None = None) -> dict:
    """签发一张新验证码。

    `plain` 只给测试用：让用例能在不知道图里画了什么的情况下走通正确分支。
    生产路径永远由服务端随机生成，不接受任何外部输入。
    """
    now = time.time()
    _sweep(now)

    if plain is None:
        code = "".join(secrets.choice(_DIGITS) for _ in range(length))
    else:
        code = plain
        length = len(plain)

    captcha_id = secrets.token_urlsafe(16)
    _STORE[captcha_id] = (code, now + _TTL_SECONDS)

    svg = _svg(code)
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return {
        "captcha_id": captcha_id,
        "image": f"data:image/svg+xml;base64,{encoded}",
        "expires_in": _TTL_SECONDS,
        "length": length,
    }


def verify(captcha_id: str, code: str) -> None:
    """校验并从存储里移除（一码一用，成功失败都移除）。"""
    entry = _STORE.pop(captcha_id, None)
    if entry is None:
        raise CaptchaInvalid("验证码已失效，请点击图片换一张")
    expected, expires_at = entry
    if expires_at <= time.time():
        raise CaptchaInvalid("验证码已过期，请点击图片换一张")
    # 比对用 secrets.compare_digest：虽然只有 4 位，但没有理由留一个时序侧信道
    if not secrets.compare_digest(expected, code.strip()):
        raise CaptchaInvalid("验证码不正确，请点击图片换一张")


def reset() -> None:
    """测试与运维用：清空存储。"""
    _STORE.clear()


def pending_count() -> int:
    """当前未消费的验证码数量（测试断言用）。"""
    return len(_STORE)
