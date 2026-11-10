"""把演示库的抽取结果整理成"可演示、且经得起追问"的状态。

为什么需要这个脚本：规则抽取是模型干的，抽出来的是**待复核**条目。
演示时如果直接全批准，会出现两个错误结论：

1. `expected_value` 带修饰语（如「是（经学校家庭经济困难学生认定）」），
   学生填 `yes` 永远不等于长句，合规的人会被判成「不符合」。
   → 复核时必须改成规范值。这是复核环节存在的理由，不是模型坏了。
2. 同一份文件里包含两类奖学金（国家励志 / 校级优秀学生）的条件。
   匹配接口**不按 target_group 过滤**（M5 已知缺口，见接口清单 §7），
   全都批准会把「gpa 3.2 申请励志奖学金」的人判成「不符合优秀学生奖学金」。
   → 只能在复核阶段把不适用本场景的条目拒绝掉。

用法（服务需已启动）：
    .venv\\Scripts\\python.exe tools\\review_demo_rules.py
    .venv\\Scripts\\python.exe tools\\review_demo_rules.py --dry-run
"""

from __future__ import annotations

import argparse
import sys

import httpx

BASE = "http://127.0.0.1:5000/api/v1"
SCHOLARSHIP = "01M6X41G23ZAS4FY1B5HR51WS8"   # 01-本科生奖学金评审办法
SERVICE_GUIDE = "01M6X41GPGMVZGNK6AVJW5YCXN"  # 02-学生事务办理指南

# rule_code -> 需要修正的字段（None 表示原样批准）
APPROVE: dict[str, dict | None] = {
    # 国家励志奖学金的可判定条件
    "R-3": None,                                    # grade >= 2
    "R-4": None,                                    # gpa >= 3.0
    "R-5": None,                                    # rank_percent <= 30
    "R-6": {"expected_value": "yes"},               # 原值「是（经学校…认定）」，equals 永远不成立
    "R-7": {"expected_value": "none"},              # 原值「无纪律处分记录」
    # 截止时间与材料：进计划任务
    "R-14": None,
    "R-16": None,
    "R-17": None,
    "R-18": None,
    "R-19": None,
}

REJECT = {
    "R-1": "适用范围（是否本校全日制本科生）由系统上下文决定，不该让学生自述；"
           "且期望值是长句，equals 无法匹配",
    "R-2": "同上，属适用范围而非个人条件",
    "R-8": "校级优秀学生奖学金的条件（gpa>=3.5），不适用于励志奖学金场景；"
           "匹配接口不支持按奖项过滤，只能在此剔除",
    "R-9": "同上（rank<=15）",
    "R-10": "同上（无纪律处分的重复表述）",
}


def login(client: httpx.Client) -> str:
    resp = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    resp.raise_for_status()
    return resp.json()["data"]["token"]


def fetch_rules(client: httpx.Client, headers: dict, document_id: str) -> dict[str, dict]:
    resp = client.get(f"/documents/{document_id}/rules?page=1&page_size=100", headers=headers)
    resp.raise_for_status()
    body = resp.json()
    if body["code"] != 0:
        raise SystemExit(f"读取规则失败：{body['code']} {body['message']}")
    return {r["rule_code"]: r for r in body["data"]["items"]}


def patch(client: httpx.Client, headers: dict, rule_id: str, payload: dict,
          dry_run: bool) -> tuple[int, str]:
    if dry_run:
        return 0, "dry-run"
    resp = client.patch(f"/rules/{rule_id}", json=payload, headers=headers)
    body = resp.json()
    return body["code"], body.get("message", "")


def main() -> int:
    parser = argparse.ArgumentParser(description="整理演示库的规则复核状态")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做什么")
    args = parser.parse_args()

    client = httpx.Client(base_url=BASE, trust_env=False, timeout=60)
    headers = {"Authorization": f"Bearer {login(client)}"}

    rules = fetch_rules(client, headers, SCHOLARSHIP)
    if not rules:
        raise SystemExit("该文档还没有规则，请先在界面上运行抽取，或调 extract 接口")

    print(f"奖学金办法共 {len(rules)} 条规则\n")

    approved = rejected = failed = 0

    print("—— 批准 ——")
    for code, override in APPROVE.items():
        rule = rules.get(code)
        if not rule:
            print(f"  {code}: 不存在，跳过")
            continue
        payload = {"review_status": "approved"}
        if override:
            payload.update(override)
        status_code, message = patch(client, headers, rule["id"], payload, args.dry_run)
        note = ""
        if override:
            note = f"（修正 {', '.join(f'{k}={v}' for k, v in override.items())}）"
        if status_code == 0:
            approved += 1
            print(f"  ✓ {code} {rule['description'][:40]}{note}")
        else:
            failed += 1
            print(f"  ✗ {code} 失败 {status_code}: {message}")

    print("\n—— 拒绝 ——")
    for code, reason in REJECT.items():
        rule = rules.get(code)
        if not rule:
            print(f"  {code}: 不存在，跳过")
            continue
        status_code, message = patch(client, headers, rule["id"],
                                     {"review_status": "rejected"}, args.dry_run)
        if status_code == 0:
            rejected += 1
            print(f"  ✓ {code} {rule['description'][:36]}")
            print(f"      理由：{reason}")
        else:
            failed += 1
            print(f"  ✗ {code} 失败 {status_code}: {message}")

    print(f"\n结果：批准 {approved} · 拒绝 {rejected} · 失败 {failed}")
    if args.dry_run:
        print("（dry-run，未写入数据库）")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
