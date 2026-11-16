"""真实完整平台短测；合成语料只测机制/开销，不代表企业质量或容量上限。"""

import argparse
import os
import platform
import statistics
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from inkrag_platform.models import Build, Job, KnowledgeBase
from inkrag_platform.operations import DocumentView
from pydantic import JsonValue, TypeAdapter


def object_response(response: httpx.Response) -> dict[str, JsonValue]:
    """响应必须真实成功和符合JSON对象，不以失败内容当正常样本。"""
    response.raise_for_status()
    return TypeAdapter(dict[str, JsonValue]).validate_json(response.content)


def wait_jobs(
    client: httpx.Client,
    kb_id: str,
    expected: set[str],
    budget: int = 900,
    reauthorize: Callable[[], None] | None = None,
) -> list[Job]:
    """轮询持久状态并包含全部失败任务，不过滤失败分母。"""
    deadline = time.monotonic() + budget
    renew_at = time.monotonic() + 120
    completed: list[Job] = []
    while time.monotonic() < deadline:
        if reauthorize is not None and time.monotonic() >= renew_at:
            reauthorize()
            renew_at = time.monotonic() + 120
        response = client.get("/api/v2/jobs", params={"kb_id": kb_id})
        response.raise_for_status()
        rows = TypeAdapter(dict[str, list[Job]]).validate_json(response.content)["items"]
        for job in rows:
            if job.job_id in expected:
                if job.status in {"failed", "cancelled"}:
                    raise RuntimeError("语料准备失败：" + (job.error_code or job.status))
                if job.status == "succeeded":
                    expected.remove(job.job_id)
                    completed.append(job)
        if not expected:
            return completed
        time.sleep(1)
    raise RuntimeError("任务准备超过截止时间")


def prepare(
    client: httpx.Client, count: int, reauthorize: Callable[[], None] | None = None
) -> tuple[str, float, float]:
    """生成可识别的合成资料并走上传、隔离解析、队列、真实嵌入和显式发布。"""
    response = client.post(
        "/api/v2/knowledge-bases", json={"name": "性能基线·合成资料·" + uuid4().hex[:6]}
    )
    response.raise_for_status()
    kb = KnowledgeBase.model_validate_json(response.content)
    started = time.monotonic()
    jobs: set[str] = set()
    for number in range(count):
        paragraphs = [
            f"# 合成企业制度 {number:03d}\n这份资料仅用于性能测量。",
            f"费用报销需部门负责人签字。制度编号为{number:03d}，预算上限{1000 + number}元。",
        ]
        for section in range(6):
            paragraphs.append(
                f"## 流程{section}\n员工应在审批系统登记用途、部门、金额和单据。"
                "财务复核通过后按工作日付款；保存原始发票、合同、审批记录，"
                "异常申请需补充材料，禁止重复报销。负责人可查看处理状态和审计记录。"
            )
        response = client.post(
            f"/api/v2/knowledge-bases/{kb.kb_id}/documents",
            params={"filename": f"policy-{number:03d}.md"},
            headers={"Idempotency-Key": uuid4().hex},
            content="\n\n".join(paragraphs).encode(),
        )
        response.raise_for_status()
        jobs.add(Job.model_validate_json(response.content).job_id)
    for completed in wait_jobs(client, kb.kb_id, jobs, reauthorize=reauthorize):
        if completed.document_id is None:
            raise RuntimeError("解析完成的任务缺少资料身份")
        path = f"/api/v2/documents/{completed.document_id}"
        detail = client.get(path)
        detail.raise_for_status()
        view = DocumentView.model_validate_json(detail.content)
        if view.result is None:
            raise RuntimeError("解析完成的资料缺少解析版本")
        client.put(
            path + "/selection",
            json={
                "result_id": view.result.result_id,
                "expected_document_revision": view.document.revision,
            },
        ).raise_for_status()
    parse_seconds = time.monotonic() - started
    started = time.monotonic()
    response = client.post(
        f"/api/v2/knowledge-bases/{kb.kb_id}/builds", headers={"Idempotency-Key": uuid4().hex}
    )
    response.raise_for_status()
    job = Job.model_validate_json(response.content)
    wait_jobs(client, kb.kb_id, {job.job_id}, reauthorize=reauthorize)
    builds = object_response(client.get(f"/api/v2/knowledge-bases/{kb.kb_id}/builds"))["items"]
    build = TypeAdapter(list[Build]).validate_python(builds, strict=False)[0]
    if build.document_count != count:
        raise RuntimeError("候选索引未包含全部合成资料")
    object_response(
        client.post(f"/api/v2/builds/{build.build_id}/publish", json={"expected_release_id": None})
    )
    return kb.kb_id, parse_seconds, time.monotonic() - started


def percentile(values: list[float], fraction: float) -> float:
    """使用最近秩定义报告分位数，小样本不插值美化尾延迟。"""
    import math

    return round(sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)], 2)


def measure(client: httpx.Client, kb_id: str, concurrency: int, count: int) -> dict[str, JsonValue]:
    """闭环并发请求，成功、繁忙、错误和总时间全部入账。"""

    def request(number: int) -> tuple[int, float]:
        """每次真实执行认证、PG发布读取、TEI向量、搜索融合及权限复核。"""
        started = time.perf_counter()
        try:
            response = client.post(
                "/api/v2/retrieve",
                json={
                    "kb_id": kb_id,
                    "query": f"制度{number % 100:03d}的费用报销如何审批？",
                    "top_k": 5,
                    "mode": "hybrid",
                },
            )
            status = response.status_code
            if status == 200:
                data = object_response(response)
                if not isinstance(data.get("hits"), list) or not data["hits"]:
                    status = 598
        except httpx.HTTPError:
            status = 599
        return status, (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        rows = list(executor.map(request, range(count)))
    seconds = time.perf_counter() - started
    latencies = [elapsed for _, elapsed in rows]
    successes = [elapsed for status, elapsed in rows if status == 200]
    statuses: dict[str, JsonValue] = {
        str(status): sum(s == status for s, _ in rows) for status, _ in rows
    }
    return {
        "concurrency": concurrency,
        "requests": count,
        "successful": len(successes),
        "statuses": statuses,
        "wall_seconds": round(seconds, 3),
        "successful_rps": round(len(successes) / seconds, 2),
        "p50_ms_all": percentile(latencies, 0.5),
        "p95_ms_all": percentile(latencies, 0.95),
        "p99_ms_all": percentile(latencies, 0.99),
        "mean_ms_all": round(statistics.mean(latencies), 2),
        "p95_ms_success": percentile(successes, 0.95) if successes else None,
    }


def local_token(client: httpx.Client, state: Path) -> str:
    """只在自建开发realm使用测试密码授权，页面及生产仍使用PKCE/OIDC。"""
    access = TypeAdapter(dict[str, str]).validate_json((state / "access.json").read_bytes())
    values = dict(
        line.split("=", 1) for line in (state / "compose.env").read_text().splitlines() if line
    )
    response = client.post(
        "http://127.0.0.1:" + values["OIDC_PORT"] + "/realms/inkrag/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "inkrag-web",
            "username": access["username"],
            "password": access["password"],
        },
    )
    value = object_response(response).get("access_token")
    if not isinstance(value, str):
        raise RuntimeError("开发身份获取失败")
    return value


def main() -> None:
    """本地可读私有账号文件；外部基线使用环境凭证，报告永不保存令牌。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--kb-id")
    parser.add_argument("--requests", type=int, default=60)
    parser.add_argument(
        "--output", type=Path, default=Path("docs/evidence/platform-performance.json")
    )
    arguments = parser.parse_args()
    if arguments.seed and not 1 <= arguments.seed <= 100:
        raise ValueError("本次初始切片语料数须在1..100，阶梯容量测量另定义环境")
    if not arguments.seed and not arguments.kb_id:
        raise ValueError("需指定--seed或已发布--kb-id")
    if arguments.requests < 1:
        raise ValueError("请求数必须大于零")
    state = Path(os.environ.get("INKRAG_STATE_DIR", ".inkrag"))
    base = os.environ.get("INKRAG_BENCH_URL")
    if base is None:
        base = TypeAdapter(dict[str, str]).validate_json((state / "access.json").read_bytes())[
            "url"
        ]
    with httpx.Client(base_url=base, timeout=60, trust_env=False) as client:
        token = os.environ.get("INKRAG_BENCH_TOKEN")
        if token is None:
            token = local_token(client, state)
        client.headers["Authorization"] = "Bearer " + token

        def renew_development_token() -> None:
            """长语料准备期间更新开发测试身份，避免过期被误统计为服务容量失败。"""
            client.headers["Authorization"] = "Bearer " + local_token(client, state)

        kb_id, parse_time, build_time = (
            prepare(
                client,
                arguments.seed,
                renew_development_token if not os.environ.get("INKRAG_BENCH_TOKEN") else None,
            )
            if arguments.seed
            else (arguments.kb_id, None, None)
        )
        if not isinstance(kb_id, str):
            raise RuntimeError("缺少知识库")
        if os.environ.get("INKRAG_BENCH_TOKEN") is None:
            client.headers["Authorization"] = "Bearer " + local_token(client, state)
        status = object_response(client.get("/api/v2/status"))
        report: dict[str, JsonValue] = {
            "measured_at": datetime.now(UTC).isoformat(),
            "host": platform.platform(),
            "corpus": "合成企业制度；用于机制和开销，不证明业务召回",
            "seed_documents": arguments.seed,
            "kb_id": kb_id,
            "parse_end_to_end_seconds": parse_time,
            "build_and_publish_seconds": build_time,
            "model": status.get("model_identity"),
            "results": [measure(client, kb_id, c, arguments.requests) for c in (1, 4, 16)],
            "limits": "单机短测；不是峰值容量、HA、灾备或长期SLO验收。延迟分母包含全部请求。",
        }
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            TypeAdapter(dict[str, JsonValue]).dump_json(report, indent=2).decode()
        )
        print(str(arguments.output))


if __name__ == "__main__":
    main()
