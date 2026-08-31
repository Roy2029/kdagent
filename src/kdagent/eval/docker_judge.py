"""官方 SWE-bench harness Docker 判分（11 §5 224 落地，D91 链路已验证）。

本地 test_cmd / gold_similarity 双轨是无 Docker 降级；Docker 判分是标准路径：
跑批产 model_patch → 本模块组 predictions.jsonl + SWEbenchInstance dataset →
subprocess 调官方 run_harness.py → 读 report.json → 逐题判定 resolved/失败。

外部环境依赖（Windows 受限见 00 D91）：
- `harness_script`：run_harness.py（Windows 启动器——stub `resource` 模块后 runpy）；
- `python`：装了官方 swebench fork 的 venv python；
- Docker Desktop 运行中 + Clash 代理（拉取 starryzhang 预构建镜像）。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from kdagent.eval.model import EvalTask

# harness report 文件名 = f"{model_name_or_path 转义}.{run_id}.json"（swebench reporting.py:131）
_DOCKER_TIMEOUT = 7200.0  # 判分超时（单题容器跑 F2P+P2P，10+ 分钟常见）


class DockerJudgeError(Exception):
    """Docker 判分失败（harness 调用/报告缺失）。"""


@dataclass(slots=True)
class DockerJudgeConfig:
    """官方 harness 判分配置（`kdagent eval --docker` 启用，D91）。"""

    harness_script: Path  # run_harness.py（Windows 兼容启动器）
    python: Path  # 装了 swebench 包的 venv python
    namespace: str = "starryzhang"  # DockerHub 预构建镜像命名空间
    max_workers: int = 1  # harness 并发容器数
    # predictions/dataset/report 落点（默认 work_dir/.kdagent/eval/<run_id>/docker）
    out_dir: Path | None = None
    run_id_prefix: str = "judge"  # harness run_id = f"{prefix}-<eval_run_id>"

    def resolve_out(self, eval_work_dir: Path, eval_run_id: str) -> Path:
        return self.out_dir or (
            eval_work_dir / ".kdagent" / "eval" / eval_run_id / "docker"
        )


@dataclass(slots=True)
class JudgeOutcome:
    """单题 Docker 判分结果。"""

    instance_id: str
    resolved: bool
    error: bool = False  # harness 环境错误（容器启动失败等）
    empty_patch: bool = False  # 模型补丁为空（harness 记录 empty_patch_ids）
    reason: str = ""  # 失败/错误补充说明
    # D4 v052：harness report 逐题 F2P/P2P 明细（report.json 落盘 / trace 回填用）
    f2p_tests: list[str] = field(default_factory=list)  # 该题 FAIL_TO_PASS 测试
    p2p_tests: list[str] = field(default_factory=list)  # 该题 PASS_TO_PASS 测试
    p2p_failed: list[str] = field(default_factory=list)  # 被补丁碰坏的 P2P 测试


def _normalize_patch(patch: str) -> str:
    """D95：patch 行尾归一化 CRLF → LF。

    Windows 上 agent 编辑/封史副本产生的 patch 带 CRLF 行尾，而 SWE-bench 容器
    git checkout 默认 LF → 容器 `git apply`/`patch` 拒绝 CRLF（实测 B3 3437
    `malformed patch at line 44`、B4 2322 `patch unexpectedly ends in middle of
    line`，均 Patch Apply Failed，patch 内容本身可能没问题）。只改行尾不动内容。
    """
    return patch.replace("\r\n", "\n")


def build_predictions(tasks: list[EvalTask]) -> list[dict[str, str]]:
    """tasks → predictions.jsonl 行（官方格式：instance_id/model_patch/model_name_or_path）。"""
    return [
        {
            "instance_id": t.instance_id,
            "model_patch": _normalize_patch(t.model_patch),
            "model_name_or_path": "kdagent",
        }
        for t in tasks
    ]


def build_dataset(tasks: list[EvalTask]) -> list[dict[str, object]]:
    """tasks → SWEbenchInstance 数据集（官方 harness 本地 .json 加载格式，绕开 HF）。

    全大写 FAIL_TO_PASS/PASS_TO_PASS（harness 反序列化要求）；log_parser 缺省 pytest。
    """
    return [
        {
            "instance_id": t.instance_id,
            "repo": t.repo,
            "base_commit": t.base_commit,
            "patch": t.gold_patch,
            "test_patch": t.test_patch,
            "problem_statement": t.problem_statement,
            "FAIL_TO_PASS": t.fail_to_pass,
            "PASS_TO_PASS": t.pass_to_pass,
            "test_cmds": t.test_cmds,
            "log_parser": t.log_parser or "pytest",
        }
        for t in tasks
    ]


def _error_reason(
    out_dir: Path, harness_run_id: str, model_name: str, instance_id: str
) -> str:
    """D96 治理②：harness error 细分——读该题 run_instance.log 判定失败阶段。

    此前 error 统一归「容器启动/依赖安装失败」，但实测（B4 2322）容器能起来、
    挂在 patch 应用（Patch Apply Failed）——两者成因不同（patch 质量 vs 环境），
    细分后 reason 才能指导修哪边。读不到日志时回退通用描述。
    """
    log = (
        out_dir
        / "logs"
        / "run_evaluation"
        / harness_run_id
        / model_name
        / instance_id
        / "run_instance.log"
    )
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "harness 环境错误（容器启动/依赖安装失败）"
    low = text.lower()
    if "patch apply failed" in low or "malformed patch" in low or "failed to apply patch" in low:
        return "patch 应用失败（容器内 git apply/patch 拒绝，见 run_instance.log）"
    if ("container" in low and "errno" in low) or "error response from daemon" in low:
        return "容器启动失败（Docker daemon/镜像问题）"
    return "harness 环境错误（容器内执行失败，见 run_instance.log）"


def _report_path(out_dir: Path, model_name: str, harness_run_id: str) -> Path:
    """harness report 文件名前缀 = predictions 的 model_name_or_path（reporting.py:131）。"""
    return out_dir / f"{model_name.replace('/', '__')}.{harness_run_id}.json"


def _test_details(
    logs: object, instance_id: str
) -> tuple[list[str], list[str], list[str]]:
    """从 harness report 逐题 logs 解析 F2P/P2P 明细（D4 v052）。

    report.json 的 `logs[instance_id]` 带 FAIL_TO_PASS/PASS_TO_PASS 测试名列表与
    tests_status（PASSED/FAILED/ERROR）。返回 (f2p_tests, p2p_tests, p2p_failed)：
    p2p_failed = PASS_TO_PASS 里非 PASSED 的（被补丁碰坏）。结构缺失/脏数据回退空
    （判分结果已由 resolved/error 承载，明细是加分项，不因解析失败而阻断）。
    """
    if not isinstance(logs, dict):
        return [], [], []
    entry = logs.get(instance_id)
    if not isinstance(entry, dict):
        return [], [], []
    f2p = entry.get("FAIL_TO_PASS")
    p2p = entry.get("PASS_TO_PASS")
    f2p_list = [str(t) for t in f2p] if isinstance(f2p, list) else []
    p2p_list = [str(t) for t in p2p] if isinstance(p2p, list) else []
    p2p_failed: list[str] = []
    status = entry.get("tests_status")
    if isinstance(status, dict):
        for t in p2p_list:
            if str(status.get(t, "")).upper() != "PASSED":
                p2p_failed.append(t)
    return f2p_list, p2p_list, p2p_failed


def judge(
    config: DockerJudgeConfig,
    tasks: list[EvalTask],
    *,
    eval_work_dir: Path,
    eval_run_id: str,
) -> list[JudgeOutcome]:
    """调官方 harness 批量判分，返回逐题结果（按 tasks 顺序）。

    流程：写 predictions.jsonl + judge-dataset.json → subprocess 调 run_harness.py →
    读 `<predictions_stem>.<harness_run_id>.json` → 映射 resolved/error/empty_patch。
    判分失败（harness 退出非 0 / 报告缺失）抛 DockerJudgeError，由调用方兜底归类。
    """
    out = config.resolve_out(eval_work_dir, eval_run_id)
    out.mkdir(parents=True, exist_ok=True)
    pred_path = out / "predictions.jsonl"
    ds_path = out / "judge-dataset.json"
    preds = build_predictions(tasks)
    pred_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in preds),
        encoding="utf-8",
    )
    ds_path.write_text(
        json.dumps(build_dataset(tasks), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    harness_run_id = f"{config.run_id_prefix}-{eval_run_id}"
    cmd = [
        str(config.python),
        str(config.harness_script),
        "--predictions_path", str(pred_path),
        "--dataset_name", str(ds_path),
        "--split", "test",
        "--namespace", config.namespace,
        "--max_workers", str(config.max_workers),
        "--run_id", harness_run_id,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=out,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_DOCKER_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise DockerJudgeError(f"harness 判分超时（>{_DOCKER_TIMEOUT:.0f}s）") from exc
    model_name = preds[0]["model_name_or_path"] if preds else "kdagent"
    report_path = _report_path(out, model_name, harness_run_id)
    if proc.returncode != 0 or not report_path.is_file():
        tail = (proc.stdout or "")[-500:]
        raise DockerJudgeError(
            f"harness 判分失败（exit {proc.returncode}，report 缺失 {report_path.name}）：{tail}"
        )
    data = json.loads(report_path.read_text(encoding="utf-8"))
    resolved_ids = set(data.get("resolved_ids") or [])
    error_ids = set(data.get("error_ids") or [])
    empty_ids = set(data.get("empty_patch_ids") or [])
    logs = data.get("logs")  # 逐题 F2P/P2P 明细（D4 v052），判分结果外附账目
    outcomes: list[JudgeOutcome] = []
    for task in tasks:
        iid = task.instance_id
        f2p, p2p, p2p_failed = _test_details(logs, iid)
        if iid in resolved_ids:
            outcomes.append(
                JudgeOutcome(iid, True, f2p_tests=f2p, p2p_tests=p2p, p2p_failed=p2p_failed)
            )
        elif iid in error_ids:
            outcomes.append(
                JudgeOutcome(
                    iid,
                    False,
                    error=True,
                    reason=_error_reason(out, harness_run_id, model_name, iid),
                    f2p_tests=f2p,
                    p2p_tests=p2p,
                    p2p_failed=p2p_failed,
                )
            )
        elif iid in empty_ids:
            outcomes.append(
                JudgeOutcome(
                    iid,
                    False,
                    empty_patch=True,
                    reason="模型补丁为空",
                    f2p_tests=f2p,
                    p2p_tests=p2p,
                    p2p_failed=p2p_failed,
                )
            )
        else:
            outcomes.append(
                JudgeOutcome(iid, False, f2p_tests=f2p, p2p_tests=p2p, p2p_failed=p2p_failed)
            )
    return outcomes
