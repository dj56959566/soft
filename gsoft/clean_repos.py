#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub 批量删库工具 — 扫描自己 / 所有组织仓库，可选性删除
================================================================
特点：
  1. 自动扫描：个人仓库 + 你加入的所有组织仓库（默认 --scope all）
     也可只扫一个账号/组织：--owner dj56959566 或 --org xxx
  2. 先列清单、再交互勾选，不会一上来就删
  3. 三重保险：① 再次确认清单 ② 输入 DELETE ③ 逐个删、逐个回显结果
  4. 删除不可逆！删前自动把完整清单备份到 repos_list.txt，
     删完写删除日志 deleted_YYYYMMDD_HHMMSS.log
  5. 只用 Python 标准库，不用装任何依赖；gh 已登录时可自动取 token

Token 准备（二选一，token 绝不要写进脚本里）：
  A. Classic PAT（最省事，推荐批量删库用）：
     GitHub → Settings → Developer settings → Personal access tokens
     → Tokens (classic) → Generate new token → 勾选 delete_repo + read:org + repo
     网址：https://github.com/settings/tokens
  B. 已装 gh 且已登录：直接跑，不用设 token，脚本会自动用 `gh auth token`
     但 gh 的 token 也必须有 delete_repo 权限，否则删时会报 403

用法：
  # 1) 只看清单，不删（先跑这个，最安全）
  python3 clean_repos.py --list-only

  # 2) 交互式勾选删除（默认模式）
  export GITHUB_TOKEN=ghp_xxxx
  python3 clean_repos.py

  # 3) 只扫某个组织 / 某个账号
  python3 clean_repos.py --org adjlevis-qq-com
  python3 clean_repos.py --owner dj56959566

  # 4) 只列含某关键词的仓库
  python3 clean_repos.py --keyword test --list-only
  python3 clean_repos.py --private --list-only        # 只看私有库
  python3 clean_repos.py --archived-only --list-only  # 只看已归档库

  # 5) 从文件批量删（每行一个完整名 owner/repo），非交互需同时加 --yes --force
  python3 clean_repos.py --file delete_list.txt --dry-run   # 先演练
  python3 clean_repos.py --file delete_list.txt --yes --force

  # 6) 不想真删、只想归档（可恢复）：加 --archive，代替删除
  python3 clean_repos.py --archive

  # 7) 移动合并：把 A 仓库整个搬到 B 仓库的子目录下
  #    源码进 B/<A仓库名>/，工作流单独合并到 B/.github/workflows/
  #    同名工作流会中文提示改名，不会直接覆盖
  export GITHUB_TOKEN=ghp_xxxx
  python3 clean_repos.py --move dj56959566/old-repo --to dj56959566/main-repo --dry-run
  python3 clean_repos.py --move dj56959566/old-repo --to dj56959566/main-repo
  python3 clean_repos.py --move dj56959566/old-repo --to dj56959566/main-repo --subdir old-code --delete-source
  python3 clean_repos.py --move dj56959566/old-repo --to dj56959566/main-repo --auto-rename

作者：djkyc / 生成时间：2026-11-14
版本：v1.1（新增 --move 移动合并）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
from datetime import datetime

API = "https://api.github.com"
VERSION = "v1.1"


# ---------- 工具函数 ----------
def get_token(args):
    """token 来源：--token > GITHUB_TOKEN 环境变量 > gh auth token"""
    if args.token:
        return args.token.strip()
    env = os.environ.get("GITHUB_TOKEN", "").strip()
    if env:
        return env
    try:
        r = subprocess.run(["gh", "auth", "token"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            print("ℹ️  未设 GITHUB_TOKEN，已自动使用 `gh auth token`")
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def api_request(token, method, path, data=None):
    """发一次 GitHub API 请求，返回 (状态码, json/None, 响应头)"""
    url = API + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", f"github-repo-cleaner/{VERSION}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None), resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="ignore")
        try:
            j = json.loads(raw) if raw else None
        except Exception:
            j = {"message": raw}
        # 调试优先：把服务端原样回来的 body 打出来，别只报状态码
        return e.code, j, e.headers
    except Exception as e:
        return 0, {"message": str(e)}, {}


def api_get_all(token, path):
    """分页拉全量列表，返回 (全部条目, 最后一次响应头)"""
    items, page, headers = [], 1, {}
    while True:
        sep = "&" if "?" in path else "?"
        status, data, headers = api_request(
            token, "GET", f"{path}{sep}per_page=100&page={page}")
        if status != 200:
            msg = data.get("message", "") if isinstance(data, dict) else data
            print(f"❌ 拉取失败 {path}：HTTP {status} — {msg}")
            if status == 401:
                print("   原因一般是 token 无效/过期，或没用 Bearer 方式传。"
                      "先去 https://github.com/settings/tokens 检查。")
            if status == 403:
                print("   原因一般是权限不够：删库要 delete_repo，扫组织要 read:org；"
                      "组织仓库还要你是该组织的 Owner/Admin 才能删。")
            if status == 403 and "rate limit" in str(msg).lower():
                print("   也可能是触发了限流，稍后再试。")
            sys.exit(1)
        if not data:
            break
        items.extend(data)
        if len(data) < 100:
            break
        page += 1
    return items, headers


def check_scopes(headers):
    """Classic token 会在响应头回显权限范围，缺 delete_repo 就提前警告"""
    scopes = headers.get("X-OAuth-Scopes", "") if headers else ""
    if scopes:
        print(f"ℹ️  当前 token 权限范围：{scopes}")
        if "delete_repo" not in scopes:
            print("⚠️  警告：token 没有 delete_repo 权限，删除时会报 403。"
                  "去 https://github.com/settings/tokens 重新勾选 delete_repo 再生成。")


def parse_selection(text, total):
    """解析 '1,3,5-8' / 'all' / '0'，返回下标集合（0-based），非法返回 None"""
    text = text.strip().lower()
    if text in ("0", "q", "quit", "exit", ""):
        return set()
    if text == "all":
        return set(range(total))
    picked = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                a, b = int(a), int(b)
            except ValueError:
                return None
            if a < 1 or b > total or a > b:
                return None
            picked.update(range(a - 1, b))
        else:
            try:
                n = int(part)
            except ValueError:
                return None
            if n < 1 or n > total:
                return None
            picked.add(n - 1)
    return picked


# ---------- 移动合并（--move） ----------
def _same_file(a, b):
    """两个文件内容是否完全相同"""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def _auto_rename(dst_dir, filename, source_repo):
    """同名时自动改名：check.yml -> check-<来源仓库名>.yml，再冲突加 -1、-2"""
    stem, ext = os.path.splitext(filename)
    cand = f"{stem}-{source_repo}{ext}"
    n = 1
    while os.path.exists(os.path.join(dst_dir, cand)):
        n += 1
        cand = f"{stem}-{source_repo}-{n}{ext}"
    return cand


def resolve_workflow_name(dst_dir, filename, source_repo, auto_rename):
    """决定一个工作流文件合并后的名字。

    返回：新文件名（str），或 None 表示跳过这个文件。
    全部中文提示，同名绝不静默覆盖。
    """
    dst = os.path.join(dst_dir, filename)
    if not os.path.exists(dst):
        return filename
    # 注意：内容完全相同的情况调用方已提前跳过，不会走到这里
    if auto_rename:
        new = _auto_rename(dst_dir, filename, source_repo)
        print(f"  ⚠️  同名工作流 {filename} 已存在，自动改名为 {new}")
        return new
    # 交互改名：回车=自动改名，输入“跳过”=不合并这个文件
    auto = _auto_rename(dst_dir, filename, source_repo)
    print(f"  ⚠️  目标仓库已有同名工作流：{filename}")
    print(f"      来源仓库是 {source_repo}，直接覆盖会丢掉目标仓库原来的工作流。")
    ans = input(f"      请输入新名字（直接回车自动改成 {auto}，输入 跳过 则不合并这个文件）：").strip()
    if ans in ("跳过", "跳过这个", "skip"):
        print(f"      已跳过：{filename}")
        return None
    if not ans:
        return auto
    if "/" in ans or "\\" in ans:
        print("      ❌ 名字里不能带 /，已跳过这个文件。")
        return None
    if os.path.exists(os.path.join(dst_dir, ans)):
        print(f"      ❌ {ans} 也已存在，已跳过这个文件。")
        return None
    return ans


def resolve_subdir(dst_root, subdir, auto_rename):
    """目标仓库里子目录已存在时，中文提示改名；返回最终子目录名"""
    if not os.path.exists(os.path.join(dst_root, subdir)):
        return subdir
    if auto_rename:
        n, cand = 1, f"{subdir}-1"
        while os.path.exists(os.path.join(dst_root, cand)):
            n += 1
            cand = f"{subdir}-{n}"
        print(f"  ⚠️  目标仓库已有目录 {subdir}/，自动改用 {cand}/")
        return cand
    ans = input(f"  ⚠️  目标仓库已有目录 {subdir}/，请输入新的子目录名"
                f"（直接回车自动改成 {subdir}-1）：").strip()
    if not ans:
        n, cand = 1, f"{subdir}-1"
        while os.path.exists(os.path.join(dst_root, cand)):
            n += 1
            cand = f"{subdir}-{n}"
        return cand
    if "/" in ans or "\\" in ans or ans in (".", ".."):
        print("      ❌ 子目录名不合法，退出。")
        sys.exit(1)
    if os.path.exists(os.path.join(dst_root, ans)):
        print(f"      ❌ {ans}/ 也已存在，退出。")
        sys.exit(1)
    return ans


def plan_move(src_dir, dst_dir, subdir):
    """只算计划、不写文件：返回 (待复制文件清单, 待合并工作流清单)。

    规则：源码（除 .git、.github/workflows 外）进 <目标>/<subdir>/；
    工作流进 <目标>/.github/workflows/，不进子目录。
    """
    to_copy, to_workflow = [], []
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        rel = os.path.relpath(root, src_dir)
        if rel.startswith(".github" + os.sep + "workflows") or rel == os.path.join(".github", "workflows"):
            for f in files:
                to_workflow.append(os.path.join(rel, f))
            continue
        if rel == ".github":
            continue  # .github 下除 workflows 外的文件也单独处理，下面补
        for f in files:
            to_copy.append(os.path.join(rel, f) if rel != "." else f)
    # .github 下非 workflows 的文件（如 ISSUE_TEMPLATE）也进子目录
    gh = os.path.join(src_dir, ".github")
    if os.path.isdir(gh):
        for root, dirs, files in os.walk(gh):
            if "workflows" in root.split(os.sep):
                continue
            rel = os.path.relpath(root, src_dir)
            for f in files:
                p = os.path.join(rel, f)
                if p not in to_copy:
                    to_copy.append(p)
    return sorted(to_copy), sorted(to_workflow)


def apply_move(src_dir, dst_dir, subdir, source_repo, auto_rename):
    """真正执行一次本地合并，返回 (已复制数, 已合并工作流[(原名, 新名)], 已跳过工作流[])。

    调用前已由 resolve_subdir 定好最终 subdir。
    """
    copied, merged, skipped = 0, [], []
    src_wf = os.path.join(src_dir, ".github", "workflows")
    dst_wf = os.path.join(dst_dir, ".github", "workflows")
    # 1) 源码进子目录
    dst_sub = os.path.join(dst_dir, subdir)
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        rel = os.path.relpath(root, src_dir)
        # 跳过工作流目录（第 2 步单独处理）
        if rel == os.path.join(".github", "workflows") or rel.startswith(".github" + os.sep + "workflows"):
            dirs[:] = []
            continue
        if rel == ".github":
            # .github 本身不进子目录根，只处理其非 workflows 子项时由下层循环处理
            pass
        for f in files:
            s = os.path.join(root, f)
            d = os.path.join(dst_sub, rel, f) if rel != "." else os.path.join(dst_sub, f)
            # .github/workflows 已跳过；.github 其他文件也进子目录，保持原相对路径
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
            copied += 1
    # 2) 工作流合并到目标根目录的 .github/workflows/
    if os.path.isdir(src_wf):
        os.makedirs(dst_wf, exist_ok=True)
        for f in sorted(os.listdir(src_wf)):
            s = os.path.join(src_wf, f)
            if not os.path.isfile(s):
                continue
            dst_file = os.path.join(dst_wf, f)
            if os.path.exists(dst_file) and _same_file(s, dst_file):
                print(f"  ℹ️  工作流 {f} 与目标完全相同，已跳过。")
                skipped.append(f)
                continue
            new_name = resolve_workflow_name(dst_wf, f, source_repo, auto_rename)
            if new_name is None:
                skipped.append(f)
                continue
            shutil.copy2(s, os.path.join(dst_wf, new_name))
            merged.append((f, new_name))
    return copied, merged, skipped


def _run(cmd, cwd=None, redact=""):
    """跑一条 git 命令；失败时打印 stderr（并把 token 抹掉，绝不外泄）"""
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        if redact:
            err = err.replace(redact, "***")
        print(f"❌ 命令失败：{' '.join(cmd[:3])}…\n   {err}")
        sys.exit(1)
    return r


def _clone(token, full_name, dest):
    url = f"https://oauth2:{token}@github.com/{full_name}.git"
    print(f"ℹ️  正在克隆 {full_name} …")
    r = subprocess.run(["git", "clone", "--depth", "1", url, dest],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        err = (r.stderr or "").replace(token, "***").strip()
        print(f"❌ 克隆失败 {full_name}：{err}")
        print("   常见原因：仓库不存在 / token 看不到它（私有库要 repo 权限）/ 网络问题。")
        sys.exit(1)


def do_move(token, args):
    """--move 的 GitHub 全流程：校验 → 克隆 → 计划 → 确认 → 合并提交推送 → 可选删源库"""
    source, target = args.move.strip(), args.to.strip()
    if "/" not in source or "/" not in target:
        print("❌ --move 和 --to 都要写完整名，如 --move dj56959566/old-repo --to dj56959566/main-repo")
        sys.exit(1)
    if source.lower() == target.lower():
        print("❌ 来源和目标是同一个仓库，退出。")
        sys.exit(1)
    source_repo = source.split("/")[-1]
    subdir = (args.subdir or source_repo).strip().strip("/")
    if not subdir:
        print("❌ 子目录名不能为空。")
        sys.exit(1)

    # 校验两个仓库都存在、能读到，并拿到登录人（用于 git 提交身份）
    for name, role in ((source, "来源"), (target, "目标")):
        status, data, _ = api_request(token, "GET", f"/repos/{name}")
        if status != 200:
            msg = data.get("message", "") if isinstance(data, dict) else data
            print(f"❌ {role}仓库 {name} 读不到：HTTP {status} — {msg}")
            sys.exit(1)
        print(f"ℹ️  {role}仓库：{name}（默认分支 {data.get('default_branch')}）")
    status, me, _ = api_request(token, "GET", "/user")
    login = me.get("login", "github-repo-cleaner") if status == 200 else "github-repo-cleaner"
    target_branch = None
    status, tdata, _ = api_request(token, "GET", f"/repos/{target}")
    if status == 200:
        target_branch = tdata.get("default_branch", "main")

    with tempfile.TemporaryDirectory(prefix="repo-move-") as tmp:
        src_dir = os.path.join(tmp, "src")
        dst_dir = os.path.join(tmp, "dst")
        _clone(token, source, src_dir)
        _clone(token, target, dst_dir)

        # 子目录冲突先定好名字
        final_subdir = resolve_subdir(dst_dir, subdir, args.auto_rename)
        to_copy, to_workflow = plan_move(src_dir, dst_dir, final_subdir)
        print(f"\n📦 移动计划：{source} → {target}/{final_subdir}/")
        print(f"  源码文件 {len(to_copy)} 个（除 .git、.github/workflows 外，全部进子目录）")
        for f in to_copy[:20]:
            print(f"    + {final_subdir}/{f}")
        if len(to_copy) > 20:
            print(f"    … 还有 {len(to_copy) - 20} 个")
        print(f"  工作流 {len(to_workflow)} 个（合并到目标 .github/workflows/，不进子目录）")
        for f in to_workflow:
            print(f"    + .github/workflows/{os.path.basename(f)}")
        if not to_copy and not to_workflow:
            print("ℹ️  来源仓库是空的，没什么可搬，退出。")
            return
        if args.dry_run:
            print("\n✅ --dry-run 演练结束：上面就是会搬的内容，没改目标仓库、没推送、没删源库。")
            return
        if not args.yes:
            ok = input("\n确认按上面搬？输入 y 继续，其他任意键退出：").strip().lower()
            if ok != "y":
                print("已取消，什么都没动。")
                return

        copied, merged, skipped = apply_move(src_dir, dst_dir, final_subdir,
                                             source_repo, args.auto_rename)
        # 提交推送
        r = subprocess.run(["git", "-C", dst_dir, "status", "--porcelain"],
                           capture_output=True, text=True, timeout=60)
        if not r.stdout.strip():
            print("ℹ️  合并后目标仓库没有任何变化（可能内容完全相同），不用提交。")
            return
        _run(["git", "-C", dst_dir, "config", "user.name", login])
        _run(["git", "-C", dst_dir, "config", "user.email",
              f"{login}@users.noreply.github.com"])
        _run(["git", "-C", dst_dir, "add", "-A"])
        msg = f"合并仓库 {source} 到 {final_subdir}/（工作流合并到 .github/workflows）"
        _run(["git", "-C", dst_dir, "commit", "-m", msg])
        # 推送：远端 URL 里已带 token，直接推当前分支
        pr = subprocess.run(["git", "-C", dst_dir, "push", "origin", "HEAD"],
                            capture_output=True, text=True, timeout=300)
        if pr.returncode != 0:
            err = (pr.stderr or "").replace(token, "***").strip()
            print(f"❌ 推送失败：{err}")
            print("   常见原因：你对目标仓库没有写权限（组织仓库要 Write 以上）。")
            sys.exit(1)
        print(f"\n✅ 已搬完并推送到 {target}"
              f"（分支 {target_branch or '默认分支'}）："
              f"源码 {copied} 个文件进 {final_subdir}/，"
              f"工作流合并 {len(merged)} 个，跳过 {len(skipped)} 个。")
        for old, new in merged:
            print(f"  工作流：{old}" + (f" → {new}" if new != old else "（同名保留）"))
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        with open(f"moved_{ts}.log", "w", encoding="utf-8") as f:
            f.write(f"# 移动记录 {ts} {source} -> {target}/{final_subdir}/\n")
            f.write(f"源码 {copied} 个，工作流 {merged}，跳过 {skipped}\n")
        print(f"ℹ️  记录已写入 moved_{ts}.log")

        # 可选：搬完删源库（同样要输 DELETE，三重确认里最后一道）
        if args.delete_source:
            typed = input(f"\n源库 {source} 已搬完，是否删除源库？删除不可逆，"
                          f"请一字不差输入 DELETE 删除，不输则保留源库：").strip()
            if typed != "DELETE":
                print("已保留源库。")
                return
            status, data, _ = api_request(token, "DELETE", f"/repos/{source}")
            if status == 204:
                print(f"✅ 源库 {source} 已删除。")
            else:
                msg = data.get("message", "") if isinstance(data, dict) else data
                print(f"❌ 源库删除失败：HTTP {status} — {msg}")


# ---------- 主流程 ----------
def main():
    p = argparse.ArgumentParser(description="GitHub 批量删库：先列清单，再勾选删除")
    p.add_argument("--scope", choices=["all", "user", "orgs"], default="all",
                   help="all=个人+所有组织（默认），user=只个人，orgs=只组织")
    p.add_argument("--owner", help="只扫这一个账号/组织，如 --owner dj56959566")
    p.add_argument("--org", help="只扫这一个组织，如 --org adjlevis-qq-com")
    p.add_argument("--keyword", help="只保留名字/描述含该关键词的仓库")
    p.add_argument("--private", action="store_true", help="只看私有库")
    p.add_argument("--public", action="store_true", help="只看公开库")
    p.add_argument("--archived-only", action="store_true", help="只看已归档库")
    p.add_argument("--file", help="从文件批量删，每行一个 owner/repo")
    p.add_argument("--list-only", action="store_true", help="只列清单并导出，不删除")
    p.add_argument("--dry-run", action="store_true", help="演练模式：走到删除前停下，不真删")
    p.add_argument("--archive", action="store_true",
                   help="归档代替删除（可恢复）：对选中仓库做 PATCH archived=true")
    p.add_argument("--token", help="GitHub token（不推荐命令行传，优先用环境变量）")
    p.add_argument("--yes", action="store_true", help="跳过第一次 y/n 确认（仍需 DELETE）")
    p.add_argument("--force", action="store_true",
                   help="配合 --file --yes 做完全非交互（危险，仍会写日志）")
    # 移动合并
    p.add_argument("--move", help="来源仓库完整名，如 --move dj56959566/old-repo")
    p.add_argument("--to", dest="to", help="目标仓库完整名，如 --to dj56959566/main-repo")
    p.add_argument("--subdir", help="搬到目标仓库下的子目录名，默认=来源仓库名")
    p.add_argument("--delete-source", action="store_true",
                   help="搬完后删除源库（仍需输入 DELETE 确认）")
    p.add_argument("--auto-rename", action="store_true",
                   help="同名工作流/子目录自动改名，不逐个问你")
    args = p.parse_args()

    token = get_token(args)
    if not token:
        print("❌ 没找到 token。请先做一步（二选一）：\n"
              "  export GITHUB_TOKEN=ghp_xxxx   # classic PAT，勾选 delete_repo + read:org\n"
              "  或 gh auth login                # 用 gh 登录，token 要有 delete_repo 权限\n"
              "  建 token：https://github.com/settings/tokens")
        sys.exit(1)

    # ---- 移动合并模式：--move 优先，不走删库流程 ----
    if args.move:
        if not args.to:
            print("❌ --move 必须配合 --to，如 --move A/old --to A/main")
            sys.exit(1)
        do_move(token, args)
        return

    # ---- 1. 确定要删的仓库列表 ----
    if args.file:
        if not os.path.exists(args.file):
            print(f"❌ 找不到文件：{args.file}")
            sys.exit(1)
        targets = []
        for line in open(args.file, encoding="utf-8"):
            name = line.strip()
            if name and not name.startswith("#") and "/" in name:
                targets.append(name)
        if not targets:
            print(f"❌ {args.file} 里没有有效的 owner/repo 行")
            sys.exit(1)
        print(f"ℹ️  从文件读到 {len(targets)} 个待删仓库")
        repos = [{"full_name": n} for n in targets]
    else:
        # 先拿当前登录人，顺便校验 token 是否有效
        status, me, headers = api_request(token, "GET", "/user")
        if status != 200:
            msg = me.get("message", "") if isinstance(me, dict) else me
            print(f"❌ token 校验失败：HTTP {status} — {msg}")
            sys.exit(1)
        login = me["login"]
        check_scopes(headers)
        print(f"ℹ️  当前账号：{login}")

        # 决定扫哪些 owner
        owners = []
        if args.org:
            owners = [("org", args.org)]
        elif args.owner:
            owners = [("user", args.owner)]
        elif args.scope == "user":
            owners = [("user", login)]
        elif args.scope == "orgs":
            orgs, _ = api_get_all(token, "/user/orgs")
            owners = [("org", o["login"]) for o in orgs]
            if not owners:
                print("ℹ️  你没有加入任何组织")
        else:  # all
            owners = [("user", login)]
            orgs, _ = api_get_all(token, "/user/orgs")
            owners += [("org", o["login"]) for o in orgs]

        repos = []
        for kind, owner in owners:
            if kind == "org":
                items, _ = api_get_all(token, f"/orgs/{owner}/repos?type=all")
            else:
                items, _ = api_get_all(
                    token, "/user/repos?affiliation=owner&sort=updated")
            print(f"ℹ️  {owner}：{len(items)} 个仓库")
            repos.extend(items)

        # 去重 + 过滤 + 排序
        seen = set()
        uniq = []
        for r in repos:
            if r["full_name"] not in seen:
                seen.add(r["full_name"])
                uniq.append(r)
        repos = uniq
        if args.keyword:
            kw = args.keyword.lower()
            repos = [r for r in repos
                     if kw in r["full_name"].lower()
                     or kw in (r.get("description") or "").lower()]
        if args.private:
            repos = [r for r in repos if r.get("private")]
        if args.public:
            repos = [r for r in repos if not r.get("private")]
        if args.archived_only:
            repos = [r for r in repos if r.get("archived")]
        repos.sort(key=lambda r: r["full_name"].lower())

        if not repos:
            print("ℹ️  没有符合条件的仓库，退出。")
            return

        # 备份完整清单（删前必做）
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        with open("repos_list.txt", "w", encoding="utf-8") as f:
            f.write(f"# GitHub 仓库清单 {ts} 账号 {login} 共 {len(repos)} 个\n")
            for r in repos:
                f.write(f"{r['full_name']}\t"
                        f"{'private' if r.get('private') else 'public'}\t"
                        f"★{r.get('stargazers_count', 0)}\t"
                        f"{(r.get('updated_at') or '')[:10]}\t"
                        f"{(r.get('description') or '').replace(chr(10), ' ')}\n")
        print(f"ℹ️  完整清单已备份到 repos_list.txt（{len(repos)} 个）")

        # ---- 2. 展示 + 交互勾选 ----
        print(f"\n共 {len(repos)} 个仓库：")
        print(f"{'序号':>4}  {'仓库':<45} {'可见性':<8} {'★':>4}  {'更新':<10}  描述")
        for i, r in enumerate(repos, 1):
            desc = (r.get("description") or "")[:40].replace("\n", " ")
            vis = "private" if r.get("private") else "public"
            if r.get("archived"):
                vis += "/归档"
            print(f"{i:>4}  {r['full_name']:<45} {vis:<8} "
                  f"{r.get('stargazers_count', 0):>4}  "
                  f"{(r.get('updated_at') or '')[:10]:<10}  {desc}")

        if args.list_only:
            print("\n✅ --list-only 模式：只列清单，不删除。清单在 repos_list.txt")
            return

        raw = input("\n要删除哪些？输入序号，如 1,3,5-8 / all=全部 / 0=退出：").strip()
        picked = parse_selection(raw, len(repos))
        if picked is None:
            print("❌ 输入格式不对，退出。示例：1,3,5-8")
            sys.exit(1)
        if not picked:
            print("已退出，什么都没删。")
            return
        targets = [repos[i]["full_name"] for i in sorted(picked)]
        repos = [{"full_name": n} for n in targets]

    # ---- 3. 三重确认 ----
    action = "归档" if args.archive else "删除"
    print(f"\n⚠️  即将{action}以下 {len(targets)} 个仓库（{action}后"
          f"{'可恢复' if args.archive else '不可恢复，代码/Issues/PR 全没了'}）：")
    for n in targets:
        print(f"  - {n}")

    if args.dry_run:
        print("\n✅ --dry-run 演练结束：上面这些就是会删的，没真删。")
        return

    if args.file and args.yes and args.force:
        pass  # 文件模式 + --yes --force：明确声明过才跳过交互确认
    else:
        if not args.yes:
            ok = input("\n确认清单无误？输入 y 继续，其他任意键退出：").strip().lower()
            if ok != "y":
                print("已取消，什么都没删。")
                return
        typed = input(f"最后确认：请一字不差输入 DELETE 以{action}这 {len(targets)} 个仓库：").strip()
        if typed != "DELETE":
            print("输入不是 DELETE，已取消，什么都没删。")
            return

    # ---- 4. 逐个执行 ----
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log = f"deleted_{ts}.log" if not args.archive else f"archived_{ts}.log"
    ok_list, fail_list = [], []
    for name in targets:
        if args.archive:
            status, data, _ = api_request(
                token, "PATCH", f"/repos/{name}", {"archived": True})
            good = status == 200
        else:
            status, data, _ = api_request(token, "DELETE", f"/repos/{name}")
            good = status == 204
        if good:
            print(f"  ✅ {action}成功：{name}")
            ok_list.append(name)
        else:
            msg = data.get("message", "") if isinstance(data, dict) else data
            print(f"  ❌ {action}失败：{name} — HTTP {status} — {msg}")
            if status == 403:
                print("     403 一般是没权限：token 缺 delete_repo，"
                      "或你不是该组织仓库的管理员。")
            if status == 404:
                print("     404 一般是仓库已删/不存在，或 token 看不到它。")
            fail_list.append(f"{name} HTTP {status} {msg}")

    with open(log, "w", encoding="utf-8") as f:
        f.write(f"# {action}记录 {ts} 成功 {len(ok_list)} 失败 {len(fail_list)}\n")
        for n in ok_list:
            f.write(f"OK {n}\n")
        for n in fail_list:
            f.write(f"FAIL {n}\n")
    print(f"\n完成：成功 {len(ok_list)} 个，失败 {len(fail_list)} 个。"
          f"记录已写入 {log}")
    if fail_list:
        print("失败的没删掉，可检查权限/token 后，把它们单独再跑一次。")


if __name__ == "__main__":
    main()
