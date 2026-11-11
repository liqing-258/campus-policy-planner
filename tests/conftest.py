"""测试夹具。

要点：
- 每个测试用例用**独立的临时 SQLite 文件**，互不污染，也不碰开发库 data/app.db
- 上传目录同样隔离，避免测试残留文件混进演示数据
- 账号由夹具直接写入（不走 tools/seed_demo.py），保证测试不依赖脚本执行顺序
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.contract import new_ulid  # noqa: E402
from app.models import User, db  # noqa: E402
from shared.enums import ROLE_ADMIN, ROLE_STUDENT  # noqa: E402

TEST_SECRET = "test-only-secret-0123456789abcdef"


@pytest.fixture()
def app(tmp_path):
    instance = create_app({
        "database_url": f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        "upload_dir": str(tmp_path / "uploads"),
        "data_dir": str(tmp_path),
        "jwt_secret": TEST_SECRET,
        "app_env": "test",
        # 关闭登录限流：几十个用例共享同一个"IP"，否则每条都登录会撞上限流
        "login_rate_limit_per_minute": 0,
        "llm_api_key": "",
        "llm_model": "",
        "embedding_api_key": "",
        "embedding_model": "",
    })
    with instance.app_context():
        db.create_all()
        db.session.add_all([
            User(id=new_ulid(), username="student", display_name="演示学生",
                 password_hash=hash_password("student123"), role=ROLE_STUDENT),
            User(id=new_ulid(), username="admin", display_name="演示管理员",
                 password_hash=hash_password("admin123"), role=ROLE_ADMIN),
        ])
        db.session.commit()
    yield instance


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def student_token(client):
    resp = client.post("/api/v1/auth/login",
                       json={"username": "student", "password": "student123"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]["token"]


@pytest.fixture()
def admin_token(client):
    resp = client.post("/api/v1/auth/login",
                       json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["data"]["token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def student_headers(student_token):
    return auth(student_token)


@pytest.fixture()
def admin_headers(admin_token):
    return auth(admin_token)


# ---------------------------------------------------------------- M5 夹具
@pytest.fixture()
def policy(app):
    """一份政策文件 + 3 个片段 + 7 条已复核规则。

    为什么直接写库而不走上传：规则抽取要调模型，抽取结果本身由 test_rules.py 验；
    匹配/计划验的是"给定规则后算得对不对"，用手工规则把被测对象与模型解耦。
    规则覆盖 met / not_met / unknown(missing_info) / unknown(ambiguous_rule) 四类，
    外加 material / deadline 两类用来生成计划任务。
    """
    from app.models import Document, DocumentChunk, PolicyRule

    chunks = [
        ("第四条 申请国家励志奖学金，应当同时具备下列条件：学年平均学分绩点不低于 3.0，"
         "且专业排名位于前 30%；经认定为家庭经济困难。", 2, "第二章 申请条件"),
        ("第九条 申请国家励志奖学金应当提交下列材料：申请表、上一学年成绩单、"
         "家庭经济困难学生认定结果告知书、个人申请书。", 4, "第四章 申请材料与附则"),
        ("第七条 申请材料提交截止时间为当年 9 月 10 日 17:00。第十一条 评审结果公示无异议后，"
         "奖学金于当年 11 月 30 日前发放至本人银行账户。", 3, "第三章 评审程序"),
    ]
    rules = [
        # rule_code, rule_type, field_key, operator, expected, description
        ("R-GPA", "eligibility", "gpa", "gte", "3.0", "学年平均学分绩点不低于 3.0"),
        ("R-RANK", "eligibility", "rank_percent", "lte", "30", "专业排名位于前 30%"),
        ("R-GRADE", "eligibility", "grade", "in", "[2, 3]", "限二年级及以上学生申请"),
        ("R-POVERTY", "eligibility", "poverty_certified", "equals", "yes", "须经家庭经济困难认定"),
        ("R-SUBJECTIVE", "restriction", None, "manual_review", None, "综合表现突出者优先考虑"),
        ("R-MATERIAL", "material", None, None, None, "提交申请表、成绩单与认定告知书"),
        ("R-DEADLINE", "deadline", None, None, "2026-09-10",
         "申请材料提交截止时间为当年 9 月 10 日"),
        ("R-PAYOUT", "deadline", None, None, None, "评审结果公示无异议后发放奖学金"),
    ]

    with app.app_context():
        uploader = db.session.execute(
            db.select(User.id).where(User.username == "admin")).scalar()
        document = Document(
            id=new_ulid(), title="（测试）本科生奖学金评审办法", file_name="policy.pdf",
            stored_name="policy.pdf", file_type="pdf", file_size=2048,
            parse_status="indexed", chunk_count=len(chunks), uploader_id=uploader,
        )
        db.session.add(document)
        chunk_rows = []
        for index, (content, page, section) in enumerate(chunks):
            row = DocumentChunk(
                id=new_ulid(), document_id=document.id, chunk_index=index,
                content=content, char_start=index * 200, char_end=(index + 1) * 200,
                page_number=page, section_title=section,
            )
            chunk_rows.append(row)
            db.session.add(row)

        rule_rows = []
        # 先 flush：规则行引用 chunks.id，本文件没定义 relationship，
        # 依赖 SQLAlchemy 的自动排序在这里不够稳，显式 flush 一次比排查 FK 报错便宜
        db.session.flush()
        for code, rule_type, field, operator, expected, description in rules:
            row = PolicyRule(
                id=new_ulid(), document_id=document.id, chunk_id=chunk_rows[0].id,
                rule_code=code, rule_type=rule_type, description=description,
                field_key=field, operator=operator, expected_value=expected,
                original_quote=description, quote_verified=1,
                extraction_status="suggested", review_status="approved",
                confidence="high", page_or_section="第二章 申请条件",
            )
            rule_rows.append(row)
            db.session.add(row)
        db.session.commit()

        ids = {
            "document_id": document.id,
            "chunk_ids": [c.id for c in chunk_rows],
            "chunk_contents": [c.content for c in chunk_rows],
            "rule_ids": [r.id for r in rule_rows],
            "rule_codes": {r.rule_code: r.id for r in rule_rows},
        }
    return ids


# ---------------------------------------------------------------- M6 夹具
@pytest.fixture()
def llm_on(app):
    """把配置切成"已接入模型"，并把覆盖率闸门压到 0。

    闸门压 0 是刻意的：M6 的用例验的是**节点轨迹、注入标记、版本冲突、反馈**
    这些"链路之上"的行为，不该因为某条片段覆盖率恰好低于阈值而集体变红。
    需要验闸门的用例自己把阈值调回去。
    """
    def _enable(key: str = "test-key", model: str = "test-model"):
        settings = app.extensions["settings"]
        settings.llm_api_key = key
        settings.llm_model = model
        settings.rag_min_coverage = 0.0
    return _enable


@pytest.fixture()
def mock_llm(monkeypatch):
    """替掉模型 HTTP 层。返回一个"把模型输出设成什么"的函数。

    不调真实模型：这些用例验的是**代码对模型输出的处理**，
    调真模型会让失败原因变成"今天模型措辞变了"，那不是回归。
    """
    def _mock(content: str, *, latency: int = 12, prompt: int = 120,
              completion: int = 60, reasoning: int | None = None):
        payload = {
            "model": "fake-model",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "completion_tokens_details": {"reasoning_tokens": reasoning},
            },
        }
        calls = {"n": 0, "prompt": ""}

        def _post(body):
            calls["n"] += 1
            # 记下最后一次实际发出去的 Prompt：多轮上下文的用例要断言它
            messages = body.get("messages") or []
            calls["prompt"] = "\n".join(m.get("content", "") for m in messages)
            return payload, latency

        monkeypatch.setattr("app.llm._post", _post)
        _mock.calls = calls
    _mock.calls = {"n": 0, "prompt": ""}
    return _mock


def answer_payload(answer: str, cited: list, *, sufficient: bool = True,
                   confidence: str = "high") -> str:
    """模型应当返回的 JSON 字符串。"""
    import json as _json

    return _json.dumps(
        {"answer": answer, "cited": cited, "sufficient": sufficient,
         "confidence": confidence},
        ensure_ascii=False,
    )


@pytest.fixture()
def seeded_policy(app):
    """三个片段 + 建好索引。内容与演示文件里的奖学金办法一致，便于对应。"""
    from app.models import Document, DocumentChunk

    chunks = [
        ("第四条 申请国家励志奖学金，应当同时具备下列条件：学年平均学分绩点不低于 3.0，"
         "且专业排名位于前 30%；经学校家庭经济困难学生认定程序认定为家庭经济困难。",
         2, "第二章 申请条件"),
        ("第九条 申请国家励志奖学金应当提交下列材料：申请表、上一学年成绩单、"
         "家庭经济困难学生认定结果告知书、个人申请书。", 4, "第四章 申请材料与附则"),
        ("第七条 申请材料提交截止时间为当年 9 月 10 日 17:00。", 3, "第三章 评审程序"),
    ]
    with app.app_context():
        uploader = db.session.execute(
            db.select(User.id).where(User.username == "admin")).scalar()
        document = Document(
            id=new_ulid(), title="本科生奖学金评审办法", file_name="policy.pdf",
            stored_name=f"{new_ulid()}.pdf", file_type="pdf", file_size=1024,
            parse_status="indexed", chunk_count=len(chunks), uploader_id=uploader,
            academic_year="2026-2027", effective_status="active",
        )
        db.session.add(document)
        for index, (content, page, section) in enumerate(chunks):
            db.session.add(DocumentChunk(
                id=new_ulid(), document_id=document.id, chunk_index=index,
                content=content, char_start=index * 100, char_end=(index + 1) * 100,
                page_number=page, section_title=section,
            ))
        db.session.commit()

        from app.indexing import rebuild_index

        rebuild_index()
        return document.id
