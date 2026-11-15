#!/usr/bin/env python3
"""Nezha watchdog: check both Nezha dashboards, auto-resume on Northflank if down.

Services (from Northflank dashboard / CLI forward commands in screenshots):
  - 哪吒:    domain nf.djbadboy.nyc.mn, project=test,    service=nezha
  - Docker:  domain nf.djsb.nyc.mn,     project=bbqtest, service=docker
If a resume call returns 404, the project/service slug is wrong: check the
Northflank dashboard URL (.../project/<projectId>/services/<serviceId>/...)
or `northflank forward service` output, and fix SERVICES below.

Env:
  NF_API_TOKEN, NF_API_TOKEN_2, NF_API_TOKEN_3, ... (up to _10)
                 Northflank API tokens (GitHub Secrets). Fill as many as
                 you have; empty/unset ones are simply ignored. Each
                 service tries the tokens in order until one works, so a
                 new token needs no code change — just add the Secret and
                 its line in the workflow env. A token only works for the
                 projects in its own Northflank team.
                 Role needs: Project > Services > General > Read + Update.
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
    # token_env is only a hint for which token to try first. If it is
    # wrong or missing, the service tries every filled token in turn,
    # so adding a new token needs no change here.
    {"name": "nezha-哪吒", "domain": "https://nf.djbadboy.nyc.mn/",
     "project": "test", "service": "nezha", "token_env": "NF_API_TOKEN"},
    {"name": "docker-哪吒", "domain": "https://nf.djsb.nyc.mn/",
     "project": "bbqtest", "service": "docker", "token_env": "NF_API_TOKEN_2"},
]

# Project-wide auto-resume: every service in these projects is checked via
# the Northflank API and resumed if it is paused / scaled to 0. This is
# in addition to the domain checks in SERVICES above. Remove a project
# from this list if you do NOT want all of its services brought back up
# (e.g. a test sandbox you keep paused on purpose).
# Env NF_AUTO_PROJECTS="test,bbqtest" overrides this list.
AUTO_PROJECTS = [p.strip() for p in
                 os.environ.get("NF_AUTO_PROJECTS", "").split(",")
                 if p.strip()] or ["test", "bbqtest"]

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


def get_tokens():
    """Collect every filled NF_API_TOKEN* from the environment, in order.
    NF_API_TOKEN, NF_API_TOKEN_1, NF_API_TOKEN_2, ... up to _10.
    Empty/unset ones are ignored, duplicates are dropped.
    Returns a list of (label, token); label is the env name, never a value."""
    names = ["NF_API_TOKEN"] + [f"NF_API_TOKEN_{i}" for i in range(1, 11)]
    tokens, seen = [], set()
    for name in names:
        value = os.environ.get(name, "")
        if value and value not in seen:
            seen.add(value)
            tokens.append((name, value))
    return tokens


def service_token(svc):
    """Backward-compatible helper: the service's hinted token, if filled."""
    return os.environ.get(svc.get("token_env", ""), "") or NF_TOKEN


def tokens_for_service(svc):
    """Tokens to try for this service: hinted token first, then the rest."""
    all_tokens = get_tokens()
    hint = service_token(svc)
    ordered = [(n, t) for n, t in all_tokens if t == hint]
    ordered += [(n, t) for n, t in all_tokens if t != hint]
    return ordered


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


def nf_get(path, token):
    """GET from Northflank API. Return (status, body_text). Token never logged."""
    url = NF_BASE + path
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": "Bearer " + token,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def list_projects_for_token(token):
    """On 404, show which projects this token can actually see, so a
    wrong-team token is obvious. Prints project id/slug/name only."""
    status, body = nf_get("/projects", token)
    print(f"  [nf] list projects -> HTTP {status}: {body[:1000]}")
    try:
        data = json.loads(body)
        items = data.get("data", {}).get("projects") or data.get("data") or []
        if isinstance(items, list):
            for p in items:
                if isinstance(p, dict):
                    print(f"  [nf] token can see project: id={p.get('id')} "
                          f"name={p.get('name')} slug={p.get('slug')}")
    except Exception:
        pass


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


def find_instances(obj, depth=0):
    """Recursively find an instances/replicas count anywhere in a
    service object. Returns an int or None."""
    if depth > 4 or not isinstance(obj, dict):
        return None
    for key in ("instances", "replicas", "desiredInstances",
                "runningInstances", "availableInstances"):
        if isinstance(obj.get(key), int):
            return obj[key]
    for val in obj.values():
        if isinstance(val, dict):
            found = find_instances(val, depth + 1)
            if found is not None:
                return found
        elif isinstance(val, list):
            for item in val:
                found = find_instances(item, depth + 1)
                if found is not None:
                    return found
    return None


def find_status_texts(obj, depth=0):
    """Recursively collect status strings from a service object."""
    texts = []
    if depth > 4 or not isinstance(obj, dict):
        return texts
    for key, val in obj.items():
        if "status" in key.lower() or "state" in key.lower():
            if isinstance(val, str):
                texts.append(val)
            elif isinstance(val, dict):
                texts += find_status_texts(val, depth + 1)
    for val in obj.values():
        if isinstance(val, dict):
            texts += find_status_texts(val, depth + 1)
    return texts


def service_is_down(svc):
    """Decide from the API service object whether it is paused / at 0.
    Returns (down: bool, reason: str). Unknown shape = not down, never
    resume a running service blindly."""
    if not isinstance(svc, dict):
        return False, "not an object"
    instances = find_instances(svc)
    if instances == 0:
        return True, "instances=0"
    joined = " ".join(find_status_texts(svc)).lower()
    for word in ("paused", "stopped", "scaled_to_zero", "scaled to zero"):
        if word in joined:
            return True, f"status={joined}"
    return False, f"instances={instances} status={joined or 'unknown'}"


def resume_service(project, service_id, tokens):
    """Resume one service, trying every token. Return True on success."""
    for label, token in tokens:
        status, body = nf_post(
            f"/projects/{project}/services/{service_id}/resume",
            {"instances": 1}, token)
        print(f"  [nf] resume {project}/{service_id} with {label} "
              f"-> HTTP {status}: {body[:300]}")
        if 200 <= status < 300:
            return True
        if status in (401, 404):
            continue
        status, body = nf_post(
            f"/projects/{project}/services/{service_id}/scale",
            {"instances": 1}, token)
        print(f"  [nf] scale {project}/{service_id} with {label} "
              f"-> HTTP {status}: {body[:300]}")
        if 200 <= status < 300:
            return True
    return False


def resume_all_in_project(project, tokens):
    """List every service in a project, resume the ones that are down.
    Returns a list of service ids that are still down afterwards."""
    if DRY_RUN or not tokens:
        print(f"  [project {project}] dry-run / no token, skipping")
        return []
    services = None
    for label, token in tokens:
        status, body = nf_get(f"/projects/{project}/services", token)
        print(f"  [project {project}] list services with {label} "
              f"-> HTTP {status}")
        if 200 <= status < 300:
            try:
                data = json.loads(body)
                services = (data.get("data", {}).get("services")
                            or data.get("data", {}).get("deployments")
                            or data.get("data") or [])
                if isinstance(services, dict):
                    services = [services]
                break
            except Exception as e:
                print(f"  [project {project}] could not parse list: {e}")
                return []
        if status in (401, 404):
            continue
        return []
    if not services:
        print(f"  [project {project}] no services found in list response")
        return []
    still_down = []
    for svc in services:
        if not isinstance(svc, dict):
            continue
        sid = svc.get("id") or svc.get("serviceId") or svc.get("slug")
        name = svc.get("name") or sid
        if not sid:
            continue
        # The list response has no instance count (it only carries the
        # last deployment status), so fetch the service detail, which
        # does carry the running instance count.
        detail = svc
        for label, token in tokens:
            status, body = nf_get(
                f"/projects/{project}/services/{sid}", token)
            if 200 <= status < 300:
                try:
                    data = json.loads(body)
                    detail = (data.get("data", {}).get("service")
                              or data.get("data") or svc)
                    break
                except Exception:
                    break
            if status in (401, 404):
                continue
            break
        down, reason = service_is_down(detail)
        if not down:
            print(f"  [project {project}] {name} ({sid}) up ({reason})")
            if "unknown" in reason:
                print(f"  [project {project}] detail keys for {sid}: "
                      f"{json.dumps(detail)[:600]}")
            continue
        print(f"  [project {project}] {name} ({sid}) DOWN ({reason}), resuming...")
        if resume_service(project, sid, tokens):
            print(f"  [project {project}] {name} resumed")
        else:
            print(f"  [project {project}] {name} FAILED to resume")
            still_down.append(str(name))
    return still_down


def recover(svc):
    """Try resume -> scale -> restart with every filled token, in order.
    A 401 (bad token) or 404 (token's team has no such project) just
    moves on to the next token. Return True if any call succeeded."""
    project, service = svc["project"], svc["service"]
    tokens = tokens_for_service(svc)
    if DRY_RUN or not tokens:
        print("  [dry-run] no NF_API_TOKEN* filled or DRY_RUN=1, not calling API")
        return False
    print(f"  [nf] {len(tokens)} token(s) filled, trying in order")
    for label, token in tokens:
        status, body = nf_post(
            f"/projects/{project}/services/{service}/resume", {"instances": 1}, token)
        print(f"  [nf] resume with {label} -> HTTP {status}: {body[:500]}")
        if 200 <= status < 300:
            return True
        if status in (401, 404):
            # Wrong token for this project/team, try the next one.
            continue
        # 409 = not paused (already running / scaled to 0 by another path).
        # Fall through to scale, which works for the scaled-to-0 case.
        status, body = nf_post(
            f"/projects/{project}/services/{service}/scale", {"instances": 1}, token)
        print(f"  [nf] scale with {label} -> HTTP {status}: {body[:500]}")
        if 200 <= status < 300:
            return True
        status, body = nf_post(
            f"/projects/{project}/services/{service}/restart", {}, token)
        print(f"  [nf] restart with {label} -> HTTP {status}: {body[:500]}")
        if 200 <= status < 300:
            return True
    # Every token failed: show what the first token can actually see.
    print("  [nf] all tokens failed for this project, checking token scope:")
    list_projects_for_token(tokens[0][1])
    return False


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
    # Project-wide: bring up every paused / 0-instance service in the
    # listed projects, not just the two domain-checked Nezha services.
    tokens = get_tokens()
    for project in AUTO_PROJECTS:
        print(f"[{now_bj()}] project-wide check: {project}")
        still = resume_all_in_project(project, tokens)
        if still:
            failed.extend(still)
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print("All services up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
