"""R6：`.env` 的发现路径。

现场：用户双击 `dist/` 里的 exe 提问，界面常驻"降级模式·未配置模型密钥"，
而项目根的 `.env` 里 `LLM_API_KEY` 是有的。原因是打包后 `writable_root()`
是 **exe 所在目录**（`dist/`），配置只在那里找，自然找不到项目根那份。

这一组用例锁住两件事：
1. 打包形态下会**再往上一级看**，但只在上一级确实属于本项目时才看；
2. `/health` 必须能说清"缺什么、找过哪里"，且**不回显密钥值**。
"""

from __future__ import annotations

import os
from pathlib import Path

import app.config as config_module
from app.config import Settings, env_candidates


def test_source_mode_looks_at_project_root_first():
    """源码运行时第一候选就是项目根——这条不能因为"支持打包"而改变。"""
    first = env_candidates()[0]
    assert first == config_module.PROJECT_ROOT / ".env"
    assert first.is_file(), "本机应有 .env，否则这组用例的前提不成立"


def test_frozen_mode_also_looks_one_level_up(monkeypatch, tmp_path):
    """模拟 exe 落在 dist/：应当同时看 dist/.env 与项目根 .env。

    不真的打包，只把 PROJECT_ROOT 指向"项目根下的 dist"——这与
    `writable_root()` 在 frozen 时返回 exe 目录是同一种结构。
    """
    project_root = config_module.PROJECT_ROOT
    fake_dist = project_root / "dist"
    fake_dist.mkdir(exist_ok=True)
    monkeypatch.setattr(config_module, "PROJECT_ROOT", fake_dist)

    candidates = [str(p) for p in env_candidates()]
    assert str(fake_dist / ".env") in candidates
    assert str(project_root / ".env") in candidates, "上一级是项目根时才追加，这里应追加"
    # exe 目录优先于项目根：用户把 .env 放在 exe 旁边换密钥，应当生效
    assert candidates.index(str(fake_dist / ".env")) < candidates.index(
        str(project_root / ".env")
    )


def test_parent_without_server_py_is_not_trusted(monkeypatch, tmp_path):
    """上一级若是无关目录，不读它的 .env。

    否则把 exe 拷到桌面后，会去读 `C:\\Users\\xxx\\.env`——那是别人的配置，
    读进来比读不到更危险（错把别的项目的密钥当自己的用）。
    """
    lonely = tmp_path / "app_dir"
    lonely.mkdir()
    (lonely / "x.env").write_text("LLM_API_KEY=should-not-be-read\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "PROJECT_ROOT", lonely)

    candidates = [p.parent for p in env_candidates()]
    assert tmp_path not in candidates


def test_env_sources_report_found_without_values(monkeypatch, tmp_path):
    """`env_sources` 只报路径与是否存在——它是给用户看的，不是配置导出。"""
    secret = "sk-should-never-appear-in-health-0123456789"
    root = tmp_path / "proj"
    root.mkdir()
    (root / "server.py").write_text("# marker\n", encoding="utf-8")
    (root / ".env").write_text(f"LLM_API_KEY={secret}\n", encoding="utf-8")

    monkeypatch.setattr(config_module, "PROJECT_ROOT", root)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    sources = config_module.load_env_sources()

    assert sources[0]["path"] == str(root / ".env")
    assert sources[0]["found"] is True
    assert secret not in str(sources), "候选清单里不能带值，只能带路径"


def test_llm_config_names_the_missing_variables():
    """降级时要知道**缺哪一个**：两个都缺与只缺模型，排查方向完全不同。"""
    both = Settings({"llm_api_key": "", "llm_model": ""})
    assert both.llm_missing == ["LLM_API_KEY", "LLM_MODEL"]
    assert both.llm_config["configured"] is False

    only_model = Settings({"llm_api_key": "k", "llm_model": ""})
    assert only_model.llm_missing == ["LLM_MODEL"]

    ready = Settings({"llm_api_key": "k", "llm_model": "m"})
    assert ready.llm_missing == []
    assert ready.llm_config == {
        "configured": True,
        "api_key": "set",
        "model": "m",
        "base_url": "set" if ready.llm_base_url else "missing",
        "missing": [],
        "env_files": ready.env_sources,
    }


def test_health_carries_config_diagnostics_without_the_key(client):
    """`/health` 是公开接口：可以给诊断，不能给密钥。"""
    data = client.get("/api/v1/health").get_json()["data"]
    cfg = data["llm_config"]
    assert cfg["configured"] is False
    assert cfg["missing"] == ["LLM_API_KEY", "LLM_MODEL"]
    assert cfg["env_files"], "至少要报出找过哪些位置，否则用户无从下手"
    assert all("path" in item and "found" in item for item in cfg["env_files"])
    raw = client.get("/api/v1/health").get_data(as_text=True).lower()
    # 变量名可以出现（诊断必须指名），但真实密钥值一个字符都不能有
    real_key = os.getenv("LLM_API_KEY", "")
    if real_key:
        assert real_key.lower() not in raw
