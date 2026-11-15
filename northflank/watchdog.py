#!/usr/bin/env python3
"""Nezha watchdog: check both Nezha dashboards, auto-resume on Northflank if down.

Services (from Northflank dashboard / CLI forward commands in screenshots):
  - 哪吒:    domain nf.djbadboy.nyc.mn, project=test,    service=nezha
  - Docker:  domain nf.djsb.nyc.mn,     project=bbqtest, service=docker
If a resume call returns 404, the project/service slug is wrong: check the
Northflank dashboard URL (.../project/<projectId>/services/<serviceId>/...)
or `northflank forward service` output, and fix SERVICES below.

Env:
  NF_API_TOKEN   Northflank API token for the first service (GitHub Secret).
                 Role needs: Project > Services > General > Read + Update.
  NF_API_TOKEN_2 Northflank API token for the second service, only needed
                 if it is in a different team/account. If unset, the second
                 service falls back to NF_API_TOKEN.
  TG_BOT_TOKEN   optional, Telegram bot token for alerts
  TG_CHAT_ID     optional, Telegram chat id for alerts (e.g. your own TG id)
  DRY_RUN=1      check domains only, never call Northflank API (local test)

Logic per service:
  1. GET domain, timeout 15s. 200 = up. Anything else = down, retry once
     after 20s to avoid a transient blip triggering a resume.
  2. If down: POST resume {"instances": 1}
     - 2xx = resumed. 409 (not paused) -> POST scale {"instances": 1}
     - scale fails -> POST restart (only helps if instances > 0)
     - every API response body is logged (token is never logged)
  3. Wait 120s, re-check domain. Up = recovered, down = still down.
  4. Telegram alert only when an action was taken, or recovery failed.
     A healthy check sends nothing, so no spam every 5 minutes.
Exit code: 0 if every service is up or recovered, 1 if any is still down.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

SERVICES = [
    # token_env: which env var holds this service's Northflank API token.
    # If the two services are in different Northflank teams/accounts, use
    # two different tokens (NF_API_TOKEN and NF_API_TOKEN_2). If they share
    # one team/token, leave NF_API_TOKEN_2 unset and both fall back to
    # NF_API_TOKEN.
    {"name": "nezha-哪吒", "domain": "https://nf.djbadboy.nyc.mn/",
     "project": "test", "service": "nezha", "token_env": "NF_API_TOKEN"},
    {"name": "docker-哪吒", "domain": "https://nf.djsb.nyc.mn/",
     "project": "bbqtest", "service": "docker", "token_env": "NF_API_TOKEN_2"},
]

NF_BASE = "https://api.northflank.com/v1"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
NF_TOKEN = os.environ.get("NF_API_TOKEN", "")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")

BJ = timezone(timedelta(hours=8))


def now_bj():
    return datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")


def check_domain(url):
    """Return (up: bool, status: str)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200, str(resp.status)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # timeout, DNS, connection reset, ...
        return False, f"{type(e).__name__}: {e}"


def service_token(svc):
    """Token for this service: its own env var, else fall back to NF_API_TOKEN."""
    return os.environ.get(svc.get("token_env", ""), "") or NF_TOKEN


def nf_post(path, body, token):
    """POST to Northflank API. Return (status, body_text). Token never logged."""
    url = NF_BASE + path
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        print("  [tg] no TG_BOT_TOKEN/TG_CHAT_ID, skipping alert")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = json.dumps({
        "chat_id": TG_CHAT, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"  [tg] sent, HTTP {resp.status}")
    except Exception as e:
        print(f"  [tg] FAILED: {e}")


def recover(svc):
    """Try resume -> scale -> restart. Return True if any call succeeded."""
    project, service = svc["project"], svc["service"]
    token = service_token(svc)
    if DRY_RUN or not token:
        print("  [dry-run] no API token for this service or DRY_RUN=1, not calling API")
        return False
    status, body = nf_post(
        f"/projects/{project}/services/{service}/resume", {"instances": 1}, token)
    print(f"  [nf] resume -> HTTP {status}: {body[:500]}")
    if 200 <= status < 300:
        return True
    if status == 404:
        print("  [nf] 404: project/service slug is wrong, fix SERVICES in watchdog.py")
        return False
    # 409 = not paused (already running / scaled to 0 by another path).
    # Fall through to scale, which works for the scaled-to-0 case.
    status, body = nf_post(
        f"/projects/{project}/services/{service}/scale", {"instances": 1}, token)
    print(f"  [nf] scale -> HTTP {status}: {body[:500]}")
    if 200 <= status < 300:
        return True
    status, body = nf_post(
        f"/projects/{project}/services/{service}/restart", {}, token)
    print(f"  [nf] restart -> HTTP {status}: {body[:500]}")
    return 200 <= status < 300


def main():
    failed = []
    for svc in SERVICES:
        name, domain = svc["name"], svc["domain"]
        up, status = check_domain(domain)
        if not up:
            print(f"[{now_bj()}] {name} {domain} DOWN ({status}), retry in 20s...")
            time.sleep(20)
            up, status = check_domain(domain)
        if up:
            print(f"[{now_bj()}] {name} {domain} OK (HTTP {status})")
            continue
        print(f"[{now_bj()}] {name} {domain} DOWN ({status}), recovering...")
        ok = recover(svc)
        if not ok:
            msg = (f"⚠️ 哪吒巡检 {now_bj()}\n{name} {domain} 掉线，"
                   f"自动恢复调用失败，请手动进 Northflank 看一眼。")
            tg_send(msg)
            failed.append(name)
            continue
        print("  waiting 120s for the container to come back...")
        time.sleep(120)
        up, status = check_domain(domain)
        if up:
            print(f"[{now_bj()}] {name} RECOVERED (HTTP {status})")
            tg_send(f"✅ 哪吒巡检 {now_bj()}\n{name} {domain} 掉线，已自动恢复上线。")
        else:
            print(f"[{now_bj()}] {name} STILL DOWN ({status}) after resume")
            tg_send(f"🔴 哪吒巡检 {now_bj()}\n{name} {domain} 掉线，"
                    f"已调用恢复但 120 秒后仍打不开，请手动处理。")
            failed.append(name)
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print("All services up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
