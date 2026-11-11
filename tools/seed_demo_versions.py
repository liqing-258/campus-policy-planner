"""给演示库补上「同一政策的两个版本」，让 T-12 能被真实复现。

**为什么不用 SQL 直接改库**

版本冲突检测要的是**经过产品路径产生的数据**：上传 → 魔数校验 → 解析 → 切分 →
索引，再加上元数据。用 INSERT 造假数据当然也能让检测逻辑吐出提示，但那只能证明
"函数在假数据上返回了预期结构"，证明不了"管理员按界面操作后能看到它"。
所以这里用应用内的 HTTP 测试客户端打真实接口：
`POST /documents`（上传）与 `PATCH /documents/<id>`（补/改版本信息）。

**这一步在做什么**

1. 把已有的《本科生奖学金评审办法（演示样例）》标注为 2026-2027 学年、
   2026-09-01 发布、现行有效——它此前三项全空，等于"来源不明"；
2. 上传《01-本科生奖学金评审办法（2025年版）.pdf》，标注 2025-2026 学年、
   2025-09-01 发布、已被替代。

两份文件的规范化标题相同（`01本科生奖学金评审办法`），年份与发布日期不同，
于是问答召回两版时会并列展示。**内容差异是实质的**：绩点线 3.0/3.5 对 2.8/3.3、
截止日 9/10 对 9/12、公示 3 天对 5 天、申请书 800 字对 600 字。

幂等：重复运行不会重复上传（同文件哈希被拒，2001），只会把元数据改回这套演示值。

用法：
    .venv\\Scripts\\python.exe tools\\seed_demo_versions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.versions import normalize_title  # noqa: E402

DEMO = ROOT / "demo"
CURRENT_PDF = DEMO / "01-本科生奖学金评审办法（演示样例）.pdf"
OLD_PDF = DEMO / "01-本科生奖学金评审办法（2025年版）.pdf"

CURRENT_META = {
    "academic_year": "2026-2027",
    "published_at": "2026-09-01",
    "version_label": "2026版",
    "effective_status": "active",
}
OLD_META = {
    "academic_year": "2025-2026",
    "published_at": "2025-09-01",
    "version_label": "2025版",
    "effective_status": "superseded",
}

ADMIN = {"username": "admin", "password": "admin123"}
LIST = "/api/v1/documents?page=1&page_size=100"
# 重复文件（同哈希）不是失败：磁盘上已经有了，接着补元数据就行
DUPLICATED = 2001


def _login(client) -> dict:
    body = client.post("/api/v1/auth/login", json=ADMIN).get_json()
    if body.get("code") != 0:
        raise SystemExit(f"登录失败：{body}\n请先初始化演示账号（见 docs/60-交付/运行说明.md）")
    return {"Authorization": f"Bearer {body['data']['token']}"}


def _topic_rows(client, headers: dict, topic: str) -> list[dict]:
    """按规范化主题筛，而不是按完整标题：括号内容会被规范化掉。"""
    items = client.get(LIST, headers=headers).get_json()["data"]["items"]
    return [item for item in items if normalize_title(item["title"]) == topic]


def _upload(client, headers: dict, path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"缺少 {path.name}，请先运行 tools/make_demo_docs.py")
    with open(path, "rb") as handle:
        resp = client.post("/api/v1/documents", headers=headers,
                           data={"file": (handle, path.name)},
                           content_type="multipart/form-data")
    code = (resp.get_json() or {}).get("code")
    if code not in (0, DUPLICATED):
        raise SystemExit(f"上传 {path.name} 失败：{resp.get_json()}")


def _patch(client, headers: dict, doc_id: str, meta: dict) -> dict:
    body = client.patch(f"/api/v1/documents/{doc_id}", headers=headers, json=meta).get_json()
    if body.get("code") != 0:
        raise SystemExit(f"更新 {doc_id} 元数据失败：{body}")
    return body["data"]


def main() -> int:
    topic = normalize_title(CURRENT_PDF.stem)
    if len(topic) < 6:
        raise SystemExit(f"规范化主题过短（{topic!r}），版本检测会跳过它")

    app = create_app()
    with app.test_client() as client:
        headers = _login(client)
        rows = _topic_rows(client, headers, topic)

        # 现行版：先认已经标成 2026 的，其次认标题里不带年份的那份
        current = next((r for r in rows if (r["academic_year"] or "").startswith("2026")), None)
        if current is None:
            current = next((r for r in rows if "2025" not in r["title"]), None)
        if current is None:
            _upload(client, headers, CURRENT_PDF)
            rows = _topic_rows(client, headers, topic)
            current = rows[0] if rows else None
        if current is None:
            raise SystemExit("现行版既没找到也没上传成功，请检查 data/ 目录写权限")
        current = _patch(client, headers, current["id"], CURRENT_META)
        print(f"[ok] 现行版 {current['title']} → "
              f"{current['academic_year']} / {current['effective_status']}")

        old = [r for r in rows if r["id"] != current["id"]]
        if not old:
            _upload(client, headers, OLD_PDF)
            old = [r for r in _topic_rows(client, headers, topic) if r["id"] != current["id"]]
        if not old:
            raise SystemExit("旧版本既没找到也没上传成功")
        old_meta = _patch(client, headers, old[0]["id"], OLD_META)
        print(f"[ok] 旧版本 {old_meta['title']} → "
              f"{old_meta['academic_year']} / {old_meta['effective_status']}")

        # 自检：同主题下必须正好两份、学年必须区分开——否则检测器不会触发
        final = _topic_rows(client, headers, topic)
        years = sorted({r["academic_year"] for r in final if r["academic_year"]})
        print(f"[check] 同主题文件 {len(final)} 份 · 学年 {years}")
        if len(final) < 2 or len(years) < 2:
            raise SystemExit("自检失败：两份文件的年份没有区分开，版本冲突检测不会触发")
        print("[done] 到「政策问答」问“奖学金申请截止时间”，即可看到两版并列与日期差异提示")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
