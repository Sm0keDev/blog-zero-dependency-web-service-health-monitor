#!/usr/bin/env python3
import json
import logging
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("health_monitor")


@dataclass
class Target:
    name: str
    url: str
    expected_status: int = 200
    timeout: float = 5.0
    max_failures: int = 3
    consecutive_failures: int = field(default=0, init=False)
    is_down: bool = field(default=False, init=False)


def send_webhook(webhook_url: str, title: str, message: str, severity: str) -> None:
    if not webhook_url:
        logger.warning("No WEBHOOK_URL configured; skipping external alert.")
        return

    payload = {
        "text": f"[{severity}] {title}\n{message}",
        "severity": severity,
        "title": title,
        "message": message,
        "host": socket.gethostname(),
    }

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "StdlibHealthMonitor/1.0"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            if resp.status >= 300:
                logger.error("Webhook returned non-2xx status: %s", resp.status)
    except Exception as exc:
        logger.error("Failed to deliver webhook alert: %s", exc)


def probe_target(target: Target, ssl_ctx: ssl.SSLContext) -> tuple[bool, str]:
    req = urllib.request.Request(
        target.url,
        headers={"User-Agent": "StdlibHealthMonitor/1.0"},
        method="GET",
    )
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=target.timeout, context=ssl_ctx) as resp:
            latency_ms = (time.monotonic() - start) * 1000
            if resp.status == target.expected_status:
                return True, f"HTTP {resp.status} ({latency_ms:.1f}ms)"
            return False, f"Unexpected HTTP {resp.status} (expected {target.expected_status})"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP Error {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return False, f"Connection failed: {exc.reason}"
    except TimeoutError:
        return False, f"Request timed out after {target.timeout}s"
    except Exception as exc:
        return False, f"Probe exception: {exc}"


def run_monitor(targets: List[Target], webhook_url: str, interval: int = 15) -> None:
    ssl_ctx = ssl.create_default_context()
    logger.info("Starting health monitor for %d target(s) [interval=%ds]", len(targets), interval)

    while True:
        for target in targets:
            healthy, detail = probe_target(target, ssl_ctx)

            if healthy:
                if target.is_down:
                    logger.info("RECOVERY: %s is back UP (%s)", target.name, detail)
                    send_webhook(
                        webhook_url,
                        title=f"Service Recovered: {target.name}",
                        message=f"Endpoint {target.url} is healthy again. Detail: {detail}",
                        severity="RECOVERY",
                    )
                target.consecutive_failures = 0
                target.is_down = False
            else:
                target.consecutive_failures += 1
                logger.warning(
                    "FAIL (%d/%d): %s -> %s",
                    target.consecutive_failures,
                    target.max_failures,
                    target.name,
                    detail,
                )

                if target.consecutive_failures >= target.max_failures and not target.is_down:
                    target.is_down = True
                    logger.error("CRITICAL: %s is DOWN (%s)", target.name, detail)
                    send_webhook(
                        webhook_url,
                        title=f"Service Down: {target.name}",
                        message=(
                            f"Endpoint {target.url} failed {target.consecutive_failures} "
                            f"consecutive checks.\nLast error: {detail}"
                        ),
                        severity="CRITICAL",
                    )

        time.sleep(interval)


if __name__ == "__main__":
    WEBHOOK = os.environ.get("MONITOR_WEBHOOK_URL", "")
    CHECK_INTERVAL = int(os.environ.get("MONITOR_INTERVAL", "15"))

    WATCHLIST = [
        Target(name="Public Edge Gateway", url="https://myblog.com/health", expected_status=200),
        Target(name="Private WireGuard Backend", url="http://10.100.0.2:8080/", expected_status=200),
    ]

    run_monitor(WATCHLIST, webhook_url=WEBHOOK, interval=CHECK_INTERVAL)
