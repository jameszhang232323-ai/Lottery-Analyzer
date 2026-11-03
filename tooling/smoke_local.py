"""本机端到端烟测：真实进程 + 真实 HTTP，跑通核心闭环。

流程：健康检查 → 登录 → 触发采集（录制源）→ 覆盖清单 → 创建快照 →
统计作业 → 候选作业 → 读取结果。每一步失败即以非零码退出。

前置：API 与 worker 已启动，worker 需设置 ``DAJIDALI_SOURCE=recorded``。

用法：
    python tooling/smoke_local.py --base-url http://127.0.0.1:8055 --username admin --password ...
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CSRF_COOKIE = "dajidali_csrf"
CSRF_HEADER = "X-CSRF-Token"
TERMINAL = {"succeeded", "failed", "cancelled"}
SEED_HEX = "00" * 32


class SmokeError(RuntimeError):
    pass


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def cookie(self, name: str) -> str | None:
        for item in self.jar:
            if item.name == name:
                return item.value
        return None

    def request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None):
        url = f"{self.base_url}{path}"
        body = None
        merged = {"Accept": "application/json"}
        if headers:
            merged.update(headers)
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            merged["Content-Type"] = "application/json"
        if method not in {"GET", "HEAD"}:
            token = self.cookie(CSRF_COOKIE)
            if token:
                merged[CSRF_HEADER] = token
        request = urllib.request.Request(url, data=body, method=method, headers=merged)
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            raise SmokeError(f"{method} {path} -> {error.code}: {raw}") from error

    def get(self, path: str, params: dict | None = None):
        if params:
            path = f"{path}?{urllib.parse.urlencode(params)}"
        return self.request("GET", path)

    def post(self, path: str, payload: dict, headers: dict | None = None):
        return self.request("POST", path, payload, headers)


def step(label: str) -> None:
    print(f"[..] {label}", flush=True)


def ok(label: str, detail: str = "") -> None:
    print(f"[OK] {label}{(' — ' + detail) if detail else ''}", flush=True)


def wait_job(client: Client, job_id: int, *, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, job = client.get(f"/api/v1/jobs/{job_id}")
        assert job is not None
        if job["status"] in TERMINAL:
            if job["status"] != "succeeded":
                raise SmokeError(
                    f"任务 {job_id} 失败：{job.get('error_code')} {job.get('error_message')}"
                )
            return job
        time.sleep(1.0)
    raise SmokeError(f"任务 {job_id} 超时未结束")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机端到端烟测")
    parser.add_argument("--base-url", default="http://127.0.0.1:8055")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--start-issue", default="2024138")
    parser.add_argument("--end-issue", default="2025034")
    args = parser.parse_args(argv)

    client = Client(args.base_url)

    step("健康检查")
    _, health = client.get("/api/v1/health/ready")
    if health.get("status") not in {"ready", "ok"}:
        raise SmokeError(f"服务未就绪：{health}")
    ok("health/ready")

    step("登录")
    client.post(
        "/api/v1/auth/login",
        {"username": args.username, "password": args.password},
    )
    _, me = client.get("/api/v1/auth/me")
    ok("auth/me", str(me.get("user", {}).get("username") if isinstance(me, dict) else me))

    step("触发采集（录制源）")
    _, created = client.post(
        "/api/v1/sync-jobs",
        {
            "game": "ssq",
            "mode": "initialize",
            "start_issue": args.start_issue,
            "end_issue": args.end_issue,
        },
        headers={"Idempotency-Key": f"smoke-sync-{int(time.time())}"},
    )
    job = wait_job(client, created["job_id"], timeout=180.0)
    ok("sync.run", f"job={job['id']} result={json.dumps(job.get('result'), ensure_ascii=False)[:120]}")

    step("覆盖清单")
    _, coverage = client.get(
        "/api/v1/coverage",
        {"game": "ssq", "start_issue": args.start_issue, "end_issue": args.end_issue},
    )
    ok("coverage", f"acquired={coverage.get('acquired_count')} status={coverage.get('status')}")

    step("开奖数据分页")
    _, draws = client.get("/api/v1/draws", {"game": "ssq", "limit": 3})
    ok("draws", f"items={len(draws.get('items', []))}")

    step("创建快照")
    _, snapshot = client.post(
        "/api/v1/snapshots",
        {"game": "ssq", "start_issue": args.start_issue, "end_issue": args.end_issue},
    )
    ok("snapshots", f"id={snapshot['snapshot_id']} members={snapshot['member_count']}")

    step("统计作业")
    _, analysis_job = client.post(
        "/api/v1/analysis-jobs",
        {"snapshot_id": snapshot["snapshot_id"]},
        headers={"Idempotency-Key": f"smoke-analysis-{int(time.time())}"},
    )
    analysis_done = wait_job(client, analysis_job["job_id"])
    _, analysis_body = client.get(
        f"/api/v1/analyses/{analysis_done['result']['analysis_run_id']}"
    )
    analysis = analysis_body["analysis"]
    ok(
        "analysis.run",
        f"hash={analysis['analysis_hash'][:12]} facts={analysis['fact_count']}",
    )

    step("候选作业")
    _, candidate_job = client.post(
        "/api/v1/candidate-jobs",
        {
            "snapshot_id": snapshot["snapshot_id"],
            "strategy": {"strategy_type": "S0"},
            "count": 5,
            "seed_hex": SEED_HEX,
        },
        headers={"Idempotency-Key": f"smoke-candidate-{int(time.time())}"},
    )
    candidate_done = wait_job(client, candidate_job["job_id"])
    batch_id = candidate_done["result"]["batch_id"]
    _, batch_body = client.get(f"/api/v1/candidates/{batch_id}")
    batch = batch_body["batch"]
    ok(
        "candidate.generate",
        f"batch={batch_id} lines={len(batch.get('lines') or [])} status={batch.get('status')}",
    )

    print("\n烟测通过：采集 → 快照 → 统计 → 候选 闭环可用。", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeError as error:
        print(f"[FAIL] {error}", file=sys.stderr)
        sys.exit(1)
