"""OPENSHORTS_ACCESS_TOKEN: the self-host gate for an instance on a public URL.

Without it a self-hosted /mcp (and every /api route behind it) runs on the
server's own Gemini / Upload-Post keys for anyone who finds the host. These
tests drive the real app over ASGI, like tests/test_mcp_endpoint.py, with the
token set by env patch: access_gate reads it per request.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid

import httpx
import pytest

import access_gate
import app as app_module
import media_auth
from app import app

TOKEN = "test-gate-token"


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setenv("OPENSHORTS_ACCESS_TOKEN", TOKEN)
    monkeypatch.delenv("PUBLIC_API_URL", raising=False)


def _request(method, path, headers=None, **kw):
    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.request(method, path, headers=headers or {}, **kw)
    return asyncio.run(_run())


def _rpc(payload, headers=None):
    return _request("POST", "/mcp", headers=headers, json=payload)


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18"}}
BEARER = {"Authorization": f"Bearer {TOKEN}"}


class TestOffByDefault:
    def test_unset_token_leaves_self_host_open(self, monkeypatch):
        monkeypatch.delenv("OPENSHORTS_ACCESS_TOKEN", raising=False)
        assert _rpc(INIT).status_code == 200

    def test_cloud_mode_ignores_the_token(self, monkeypatch):
        # Cloud has real per-user auth; a stray variable must not replace it.
        monkeypatch.setenv("OPENSHORTS_ACCESS_TOKEN", TOKEN)
        monkeypatch.setenv("BILLING_ENABLED", "1")
        assert access_gate.access_token() == ""
        assert access_gate.media_secret() == ""


class TestMcpBehindTheGate:
    def test_no_header_is_401_with_a_bearer_challenge(self, gate):
        resp = _rpc(INIT)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"].startswith("Bearer")
        assert "OPENSHORTS_ACCESS_TOKEN" in resp.json()["error"]

    def test_wrong_token_is_401(self, gate):
        assert _rpc(INIT, {"Authorization": "Bearer nope"}).status_code == 401

    def test_bearer_passes(self, gate):
        resp = _rpc(INIT, BEARER)
        assert resp.status_code == 200
        assert resp.json()["result"]["serverInfo"]["name"] == "openshorts"

    def test_x_api_key_passes(self, gate):
        assert _rpc(INIT, {"X-API-Key": TOKEN}).status_code == 200

    def test_tool_calls_pass_the_gate_in_process(self, gate):
        # The tool is an in-process HTTP call back into the app; it only works
        # if mcp_server forwards the caller's Authorization through the gate.
        app_module.jobs["gate-job"] = {"status": "processing", "logs": ["x"]}
        try:
            resp = _rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "get_job_status",
                                    "arguments": {"job_id": "gate-job"}}}, BEARER)
            result = resp.json()["result"]
            assert result["isError"] is False
            assert result["structuredContent"]["status"] == "processing"
        finally:
            app_module.jobs.pop("gate-job", None)

    def test_rest_api_is_gated_too(self, gate):
        assert _request("GET", "/api/config").status_code == 401
        assert _request("GET", "/api/config", BEARER).status_code == 200


class TestWhatStaysOpen:
    @pytest.mark.parametrize("path", ["/health", "/health/ready"])
    def test_health_checks(self, gate, path):
        assert _request("GET", path).status_code == 200

    def test_upload_put_is_its_own_capability(self, gate):
        # create_upload tells the agent to `curl -T file <upload_url>` with no
        # header: the uuid4 slot id is the capability. Creating one is gated.
        assert _request("POST", "/api/uploads", json={}).status_code == 401
        created = _request("POST", "/api/uploads", BEARER, json={"filename": "v.mp4"})
        assert created.status_code == 200
        upload_id = created.json()["upload_id"]
        try:
            # Reaches the handler (400: an empty body), not the gate's 401.
            assert _request("PUT", f"/api/uploads/{upload_id}").status_code == 400
            assert _request("PUT", f"/api/uploads/{uuid.uuid4()}").status_code == 404
            assert _request("PUT", "/api/uploads/not-a-uuid").status_code == 401
        finally:
            app_module.pending_uploads.pop(upload_id, None)


@pytest.fixture
def clip_file():
    job_id = f"gate-{uuid.uuid4().hex[:8]}"
    job_dir = os.path.join(app_module.OUTPUT_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    name = "My Clip é 1.mp4"
    with open(os.path.join(job_dir, name), "wb") as f:
        f.write(b"\x00" * 64)
    yield job_id, f"/videos/{job_id}/{name}"
    shutil.rmtree(job_dir, ignore_errors=True)


class TestSignedMedia:
    def test_bare_clip_url_is_gated(self, gate, clip_file):
        _, path = clip_file
        assert _request("GET", path).status_code == 401
        assert _request("GET", path, BEARER).status_code == 200

    def test_signed_clip_url_opens_without_a_header(self, gate, clip_file):
        _, path = clip_file
        signed = access_gate.sign(path)
        assert "?exp=" in signed and " " not in signed  # percent-encoded for curl
        resp = _request("GET", signed)
        assert resp.status_code == 200
        assert resp.content == b"\x00" * 64

    def test_tampered_or_expired_signature_is_refused(self, gate, clip_file):
        job_id, path = clip_file
        signed = access_gate.sign(path)
        assert _request("GET", signed[:-1] + ("0" if signed[-1] != "0" else "1")).status_code == 401
        other = access_gate.sign(f"/videos/{job_id}/other.mp4").split("?")[1]
        assert _request("GET", f"{signed.split('?')[0]}?{other}").status_code == 401
        rel = media_auth.strip_media_prefix(path)
        past = int(time.time()) - 5
        sig = media_auth.path_signature(rel, past, access_gate.media_secret())
        assert _request("GET", f"{signed.split('?')[0]}?exp={past}&sig={sig}").status_code == 401

    def test_signature_is_not_keyed_on_the_token_itself(self, gate, clip_file):
        _, path = clip_file
        rel = media_auth.strip_media_prefix(path)
        exp = int(time.time()) + 60
        forged = media_auth.path_signature(rel, exp, TOKEN)
        assert _request("GET", f"{access_gate.sign(path).split('?')[0]}?exp={exp}&sig={forged}").status_code == 401

    def test_sign_is_a_no_op_when_the_gate_is_off(self, monkeypatch):
        monkeypatch.delenv("OPENSHORTS_ACCESS_TOKEN", raising=False)
        assert access_gate.sign("/videos/j/a b.mp4") == "/videos/j/a b.mp4"

    def test_mcp_clip_urls_are_signed(self, gate, clip_file):
        job_id, path = clip_file
        app_module.jobs[job_id] = {
            "status": "completed", "logs": [],
            "result": {"clips": [{"title": "t", "video_url": path, "start": 0.0, "end": 5.0}]},
        }
        try:
            resp = _rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "list_clips", "arguments": {"job_id": job_id}}},
                        BEARER)
            url = resp.json()["result"]["structuredContent"]["clips"][0]["video_url"]
            assert "sig=" in url
            assert _request("GET", url).status_code == 200
        finally:
            app_module.jobs.pop(job_id, None)

    def test_webhook_clip_urls_are_signed(self, gate, monkeypatch):
        monkeypatch.setenv("PUBLIC_API_URL", "https://example.test")
        job = {"result": {"clips": [{"title": "t", "video_url": "/videos/j/c.mp4"}]}}
        entries = asyncio.run(app_module._webhook_clip_entries("j", job))
        assert entries[0]["video_url"].startswith("https://example.test/videos/j/c.mp4?exp=")


class TestStdioPresentsTheToken:
    def test_a_gated_instance_still_serves_its_own_stdio_bridge(self):
        # A real subprocess, as in tests/test_mcp_stdio.py: importing
        # mcp_stdio here would swap sys.stdout for the rest of the session.
        # A 404 tool error proves the call reached the endpoint; without the
        # token fallback the gate would answer 401 first.
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, BILLING_ENABLED="0", PYTHONPATH=repo,
                   OPENSHORTS_ACCESS_TOKEN=TOKEN)
        env.pop("OPENSHORTS_API_KEY", None)
        env.pop("PROXY_URL", None)
        messages = [
            INIT,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "get_job_status", "arguments": {"job_id": "no-such-job"}}},
        ]
        proc = subprocess.run(
            [sys.executable, "-u", os.path.join(repo, "mcp_stdio.py")],
            input="".join(json.dumps(m) + "\n" for m in messages),
            capture_output=True, text=True, cwd=repo, env=env, timeout=300,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        status = replies[-1]["result"]["structuredContent"]["http_status"]
        assert status == 404, replies[-1]
