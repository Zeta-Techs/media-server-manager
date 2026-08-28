from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest
from playwright.sync_api import sync_playwright

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("CLP_E2E") != "1", reason="set CLP_E2E=1 to run"),
]

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_url(url: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise AssertionError(f"service did not start: {url}")


@contextmanager
def process(command: list[str], env: dict[str, str]) -> Iterator[subprocess.Popen[bytes]]:
    child = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        yield child
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


class FakePlexHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "":
            payload = {"MediaContainer": {"friendlyName": "Mock Plex"}}
        elif self.path.startswith("/library/recentlyAdded"):
            payload = {
                "MediaContainer": {
                    "Metadata": [
                        {
                            "ratingKey": "42",
                            "title": "示例电影",
                            "year": 2026,
                            "type": "movie",
                            "thumb": "/library/metadata/42/thumb/987",
                            "addedAt": 200,
                        }
                    ]
                }
            }
        elif self.path.startswith("/library/metadata/42/thumb/987"):
            body = b"fake-png"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif self.path.startswith("/library/sections"):
            payload = {"MediaContainer": {"Directory": [], "Metadata": []}}
        else:
            payload = {"MediaContainer": {"Metadata": []}}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def fake_plex() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", free_port()), FakePlexHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def wait_for_job(db_file: Path, job_id: int, status: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with sqlite3.connect(db_file) as db:
            row = db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row and row[0] == status:
            return
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not reach {status}")


def test_webui_worker_sse_restart_and_mobile(tmp_path):
    web_port = free_port()
    base_url = f"http://127.0.0.1:{web_port}"
    env = os.environ.copy()
    env.update(
        {
            "CLP_CONFIG_DIR": str(tmp_path),
            "CLP_WEB_HOST": "127.0.0.1",
            "CLP_WEB_PORT": str(web_port),
            "CLP_WORKER_CONCURRENCY": "2",
            "CLP_ITEM_WORKERS": "2",
        }
    )
    db_file = tmp_path / "clp.db"

    with fake_plex() as plex_url, process([sys.executable, "-m", "clp_web"], env):
        wait_for_url(f"{base_url}/healthz")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(base_url)

            page.locator('#auth-form input[name="username"]').fill("admin")
            page.locator('#auth-form input[name="password"]').fill("secure-password")
            page.locator('#auth-form button[type="submit"]').click()
            page.locator("#app").wait_for(state="visible")
            assert "离线" in page.locator("#overview-cards").inner_text()

            page.locator('.nav[data-view="servers"]').click()
            server_form = page.locator("#server-form")
            server_form.locator('input[name="name"]').fill("Mock Plex")
            server_form.locator('input[name="address"]').fill(plex_url)
            server_form.locator('input[name="token"]').fill("super-secret-token")
            server_form.locator('button[type="submit"]').click()
            page.locator("#server-message").filter(has_text="已保存").wait_for()
            page.locator("#server-list button", has_text="编辑").click()
            token_input = server_form.locator('input[name="token"]')
            assert token_input.input_value() == ""
            assert token_input.get_attribute("placeholder") == "已配置，留空保持不变"

            with process([sys.executable, "-m", "clp_worker"], env) as worker:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    page.locator('.nav[data-view="overview"]').click()
                    page.locator("#refresh-overview").click()
                    if "在线" in page.locator("#overview-cards").inner_text():
                        break
                    time.sleep(0.2)
                assert "在线" in page.locator("#overview-cards").inner_text()
                page.locator("#overview-media .media-card").wait_for()
                assert page.locator("#overview-media .media-card").count() == 1

                page.locator('.nav[data-view="localization"]').click()
                page.locator('#localize-form button[type="submit"]').click()
                page.locator("#job-title").filter(has_text="任务 #").wait_for()
                page.locator("#job-status").filter(has_text="succeeded").wait_for(timeout=15000)
                job_id = int(page.locator("#job-title").inner_text().split("#", 1)[1])
                assert "任务执行完成" in page.locator("#job-logs").inner_text()

                worker.terminate()
                worker.wait(timeout=10)

            now = "2026-01-01T00:00:00Z"
            with sqlite3.connect(db_file) as db:
                server_id = int(db.execute("SELECT id FROM servers LIMIT 1").fetchone()[0])
                running_id = int(
                    db.execute(
                        "INSERT INTO jobs (type, server_id, status, payload, created_at) VALUES ('all', ?, 'running', '{}', ?)",
                        (server_id, now),
                    ).lastrowid
                )
                queued_id = int(
                    db.execute(
                        "INSERT INTO jobs (type, server_id, status, payload, created_at) VALUES ('all', ?, 'queued', '{}', ?)",
                        (server_id, now),
                    ).lastrowid
                )
                db.commit()

            with process([sys.executable, "-m", "clp_worker"], env):
                wait_for_job(db_file, running_id, "interrupted")
                wait_for_job(db_file, queued_id, "succeeded")

                page.locator('.nav[data-view="automation"]').click()
                page.locator("#schedule-mode").select_option("weekly")
                weekday_values = page.locator("#schedule-weekday option").evaluate_all(
                    "options => options.map(option => option.value)"
                )
                assert weekday_values == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

                page.set_viewport_size({"width": 390, "height": 844})
                page.locator('.nav[data-view="jobs"]').click()
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                )
                assert page.locator("#job-list").is_visible()
                assert job_id > 0

            browser.close()
