"""六节点工作流的记录与回放。

对应里程碑 M6（FR-13 工作流节点可视化）。一条路由：
    GET /workflows/{run_id}     某次运行的节点轨迹

**为什么要有这一层，而不是在 qa.py 里散着记时间戳**

三个理由：

1. **"六节点"是跨链路的总视图，不是"每次提问都跑六个"。**
   一次问答真正执行 intent → retrieval → analysis → validation 四个；
   matching 与 planning 属于另外两条链路。如果为了界面好看把没跑的节点
   补成"已完成"，那张进度条就是假的——评审点开一个只有 0.3 秒的请求，
   却看到"计划生成 已完成"，这比不给看更糟。所以**没执行的节点必须显式
   记为 `skipped` 并写明原因**，前端渲染成灰色。

2. **节点失败不能吞掉异常。** 记录器用上下文管理器包住节点，异常时先把
   `status=failed` 与错误摘要写进轨迹，再把异常原样抛出去——调用方该怎么
   处理还怎么处理。吞掉异常的"可观测性"是负的：界面显示"完成"，接口却在报错。

3. **运行记录要在业务异常之前落库。** 拒答（4001）、引用被拒（4003）、
   降级（4006）都会抛业务态异常，而 Flask 的错误处理器会回滚会话。
   如果轨迹跟着业务数据一起回滚，"为什么这次拒答"就永远查不到了。
   所以 `finish()` 之后立刻 commit，再抛异常。

**status 与 outcome 的分工**（两者都要，缺一个说不清）

- `status`：这次跑完了没有（`succeeded` / `degraded` / `failed`）
- `outcome`：跑完了是什么结果（`answered` / `refused` / `citation_rejected` / …）

一次**正确的拒答**是 `status=succeeded` + `outcome=refused`。
如果只有一个字段，它要么被记成"失败"（冤枉了系统），
要么被记成"成功"（看不出发生了什么）。
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from flask import Blueprint, request

from app.auth import current_user, login_required
from app.contract import new_ulid, ok
from app.errors import MessageNotFound, Forbidden
from app.models import WorkflowRun, db
from shared.enums import (
    NODE_FAILED,
    NODE_LABELS,
    NODE_OK,
    NODE_ORDER,
    NODE_SKIPPED,
    OUTCOME_DEGRADED,
    OUTCOME_FAILED,
    RUN_DEGRADED,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
)

bp = Blueprint("workflows", __name__)

# 轨迹里每个节点存多少 detail：detail 只是给人看的摘要，不是数据源。
# 超过这个体积就截断，否则一条轨迹可能比答案本身大几十倍，列表接口会被拖死。
DETAIL_MAX_CHARS = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class NodeTrace:
    """一个节点的轨迹。用普通类而不是 dict，是为了让"写字段"这件事有地方校验。"""

    def __init__(self, node: str, *, status: str = RUN_RUNNING):
        self.node = node
        self.status = status
        self.ms = 0
        self.summary = ""
        self.model_calls = 0
        self.detail: dict = {}
        self.error: str | None = None

    def to_public(self) -> dict:
        payload = {
            "node": self.node,
            "label": NODE_LABELS.get(self.node, self.node),
            "status": self.status,
            "ms": self.ms,
            "summary": self.summary,
            "model_calls": self.model_calls,
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.error:
            payload["error"] = self.error
        return payload


class WorkflowRecorder:
    """收集节点轨迹，收尾时落一条 `workflow_runs`。"""

    def __init__(self, user_id: str, run_type: str = "qa"):
        self.user_id = user_id
        self.run_type = run_type
        self.started = time.perf_counter()
        self.nodes: dict[str, NodeTrace] = {}
        self.outcome: str | None = None
        # 已经落过库的运行记录。降级路径会先 finish() 拿到 run_id 写进消息、
        # commit 之后才抛异常，而外层 except 又会 persist 一次——
        # 没有这个字段就会为同一次问答留下两条运行记录。
        self.run: WorkflowRun | None = None

    # ---------------------------------------------------------- 节点
    def skip(self, node: str, reason: str) -> None:
        """显式跳过。**必须给原因**——"为什么这一步没跑"是本系统要回答的问题之一。"""
        trace = NodeTrace(node, status=NODE_SKIPPED)
        trace.summary = reason
        self.nodes[node] = trace

    @contextmanager
    def node(self, node: str, *, summary: str = "", detail: dict | None = None):
        """包住一个真实执行的节点：自动计时，异常时记失败再抛出。"""
        trace = NodeTrace(node)
        if detail:
            trace.detail = detail
        self.nodes[node] = trace
        mark = time.perf_counter()
        try:
            yield trace
        except BaseException as exc:  # 业务态异常也算"这次节点没走完"
            trace.status = NODE_FAILED
            trace.error = f"{type(exc).__name__}: {str(exc)[:200]}"
            if not trace.summary:
                trace.summary = "节点内抛出异常，轨迹已记录"
            raise
        else:
            if trace.status == RUN_RUNNING:  # 节点内没显式改状态，视为成功
                trace.status = NODE_OK
            if summary and not trace.summary:
                trace.summary = summary
        finally:
            trace.ms = int((time.perf_counter() - mark) * 1000)

    def note(self, node: str, **detail) -> None:
        """给已存在的节点补细节（如检索命中数、被降权的注入片段）。"""
        trace = self.nodes.get(node)
        if trace is not None:
            trace.detail.update(detail)

    # ---------------------------------------------------------- 收尾
    def _ordered(self) -> list[dict]:
        """按设计规定的六节点顺序输出，缺的补 skipped。

        补的节点用统一原因，避免"轨迹里只有 4 个节点"被误读成"另外 2 个失败了"。
        """
        traces: list[dict] = []
        for node in NODE_ORDER:
            trace = self.nodes.get(node)
            if trace is None:
                filler = NodeTrace(node, status=NODE_SKIPPED)
                filler.summary = "本次请求不涉及该节点"
                traces.append(filler.to_public())
            else:
                traces.append(trace.to_public())
        return traces

    def finish(self, outcome: str | None = None, *, status: str | None = None,
               error: str | None = None) -> WorkflowRun:
        """落一条运行记录（**不含 commit**，由调用方决定何时提交）。

        单独拆出 commit 的理由：成功路径要把 `run.id` 写进 assistant 消息的
        `workflow_run_id`，与消息同一次事务提交；而失败/拒答路径需要在抛异常
        之前就提交，否则错误处理器回滚会话会把轨迹一起抹掉。

        可重复调用：第二次起直接返回第一次建的那一行。
        """
        if self.run is not None:
            return self.run
        self.outcome = outcome or self.outcome or OUTCOME_FAILED
        if status is None:
            if self.outcome == OUTCOME_FAILED:
                status = RUN_FAILED
            elif self.outcome == OUTCOME_DEGRADED:
                status = RUN_DEGRADED
            else:
                status = RUN_SUCCEEDED
        run = WorkflowRun(
            id=new_ulid(),
            user_id=self.user_id,
            run_type=self.run_type,
            status=status,
            node_traces_json=json.dumps(
                {"outcome": self.outcome, "nodes": self._ordered()},
                ensure_ascii=False,
            )[:DETAIL_MAX_CHARS * 8],
            total_ms=int((time.perf_counter() - self.started) * 1000),
            model_calls=sum(t.model_calls for t in self.nodes.values()),
            error=(error or "")[:2000] or None,
            created_at=_now(),
        )
        db.session.add(run)
        self.run = run
        return run

    def persist(self, outcome: str | None = None, **kwargs) -> WorkflowRun:
        """立刻提交的运行记录。用于"马上要抛异常"的路径。"""
        run = self.finish(outcome, **kwargs)
        db.session.commit()
        return run


# ============================================================ 序列化
def serialize_run(run: WorkflowRun, *, include_result: bool = False) -> dict:
    envelope: dict = {}
    nodes: list[dict] = []
    if run.node_traces_json:
        try:
            envelope = json.loads(run.node_traces_json)
        except json.JSONDecodeError:
            # 轨迹坏了要能看出来，而不是静默当空
            envelope = {"outcome": None, "corrupted": True}
    if isinstance(envelope, dict):
        nodes = envelope.get("nodes") or []
    executed = [n for n in nodes if n.get("status") != NODE_SKIPPED]
    payload = {
        "run_id": run.id,
        "run_type": run.run_type,
        "status": run.status,
        "outcome": envelope.get("outcome") if isinstance(envelope, dict) else None,
        "total_ms": run.total_ms,
        "model_calls": run.model_calls,
        "created_at": _iso(run.created_at),
        "nodes": nodes,
        "executed_count": len(executed),
        "node_count": len(nodes),
    }
    if include_result and run.error:
        payload["error"] = run.error
    return payload


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================ 路由
@bp.get("/workflows/<run_id>")
@login_required
def get_run(run_id: str):
    """回看某次运行的节点轨迹。

    问答响应里已经内联了当次的 workflow，这个接口是给"隔天回看历史消息"用的：
    消息列表只带 `workflow_run_id`，点开才取轨迹，避免列表接口把轨迹全带上。
    """
    user = current_user()
    run = db.session.get(WorkflowRun, run_id)
    if run is None:
        raise MessageNotFound(f"工作流记录 {run_id} 不存在")
    if run.user_id != user.id:
        raise Forbidden("无法查看他人的工作流记录")
    return ok(serialize_run(run, include_result=True))
