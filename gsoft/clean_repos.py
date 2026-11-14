#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub 批量删库工具 — 扫描自己 / 所有组织仓库，可选性删除
================================================================
特点：
  1. 自动扫描：个人仓库 + 你加入的所有组织仓库（默认 --scope all）
     也可只扫一个账号/组织：--owner dj56959566 或 --org xxx
  2. 先列清单、再交互勾选，不会一上来就删
  3. 两次确认：1. 选完输 y 确认清单 2. 再输 y 最后确认，逐个删、逐个回显结果
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

  # 8) v1.2：双击 exe / 不带参数直接跑，进中文主菜单
  #    启动时先问你要 token（谁用问谁要，隐藏输入，仅本次有效，不保存到文件）
  #    菜单：1 看清单 2 勾选删除 3 移动合并 4 归档 0 退出
  #    带参数跑（如 --list-only / --move）还是走命令行模式，不进菜单

作者：djkyc / 生成时间：2026-11-14
版本：v1.9（翻页提示改清楚 + 工作流自动读版本号发 Release）
  v1.9 改动：翻页提示原来挤在一行里，看完输 q 不明显，改成单独成段、
  空行隔开，q 放最前面：看完了吗？输 q 结束浏览去勾选；工作流不再写死
  v1.5，改为从 clean_repos.py 里自动读 VERSION，每次改代码升版本，
  重跑打包就发对应版本号的新 Release，覆盖旧文件。
  v1.3 改动：隐藏输入（getpass）在 Windows 黑窗口右键粘贴没反应，改成普通 input，
  粘贴用 Ctrl+V，校验通过后自动清屏；提示区单独一行给 https://github.com/settings/tokens，
  输 o 可直接打开浏览器去建 Token。
"""
import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
import webbrowser
from datetime import datetime

API = "https://api.github.com"
VERSION = "v1.9"


# ---------- 工具函数 ----------
def _read_token_file():
    """兜底：exe/脚本旁边有个 token.txt 就直接读它（第一行就是 Token）。

    粘贴实在不灵时用这个最稳：用记事本新建 token.txt，把 Token 粘贴进去保存，
    再运行 exe，不用在黑窗口里粘贴。注意：token.txt 只放你自己电脑上，
    千万别传到 GitHub 仓库里，用完可以删掉。
    """
    for base in (os.path.dirname(os.path.abspath(__file__)),
                 os.getcwd(),
                 os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else ""):
        if not base:
            continue
        p = os.path.join(base, "token.txt")
        try:
            if os.path.isfile(p):
                with open(p, encoding="utf-8", errors="ignore") as f:
                    content = f.read().strip()
                if content:
                    tok = content.split()[0]
                    print(f"[提示] 已从 {p} 读取 Token。")
                    return tok
        except Exception:
            continue
    return ""


def get_token(args):
    """token 来源：--token > GITHUB_TOKEN 环境变量 > gh auth token > token.txt"""
    if args.token:
        return args.token.strip()
    env = os.environ.get("GITHUB_TOKEN", "").strip()
    if env:
        return env
    try:
        r = subprocess.run(["gh", "auth", "token"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            print("[提示]  未设 GITHUB_TOKEN，已自动使用 `gh auth token`")
            return r.stdout.strip()
    except Exception:
        pass
    return _read_token_file()


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
            print(f"[失败] 拉取失败 {path}：HTTP {status} — {msg}")
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
        print(f"[提示]  当前 token 权限范围：{scopes}")
        if "delete_repo" not in scopes:
            print("[警告]  警告：token 没有 delete_repo 权限，删除时会报 403。"
                  "去 https://github.com/settings/tokens 重新勾选 delete_repo 再生成。")


def _read_clipboard():
    """从剪贴板读一段文字（只读，不写剪贴板）。Windows 用 ctypes 读，失败返回 ""。

    用途：黑窗口里右键粘贴、Ctrl+V 都不灵时，用户只要在浏览器里复制好 Token，
    回到这里直接按回车，就能自动从剪贴板取到，不用再粘贴。
    """
    if os.name == "nt":
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            CF_UNICODETEXT = 13
            if not user32.OpenClipboard(None):
                return ""
            try:
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return ""
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    return ""
                try:
                    text = ctypes.wstring_at(ptr)
                    return (text or "").strip()
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        except Exception:
            return ""
    # 非 Windows：先试 tkinter，再试 pbpaste / xclip
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        try:
            return root.clipboard_get().strip()
        finally:
            root.destroy()
    except Exception:
        pass
    for cmd in (["pbpaste"], ["xclip", "-selection", "clipboard", "-o"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            continue
    return ""


def _looks_like_token(text):
    """剪贴板里的东西像不像 Token：ghp_ / github_pat_ 开头，或一长串无空格字符。"""
    t = (text or "").strip()
    if t.startswith("ghp_") or t.startswith("github_pat_"):
        return True
    return len(t) >= 20 and " " not in t and "\n" not in t


def _print_token_link():
    """把建 Token 的网址单独一行打出来，只打纯文字，不加任何转义码。

    教训：v1.4 用 OSC 8 做可点链接，老黑窗口（GBK）不支持，会显示成乱码方框，
    所以 v1.5 改回纯文字网址。Windows Terminal 里按住 Ctrl 再左键点一下
    纯文字网址也能打开；普通 cmd 黑窗口点不开，就输 1 回车打开
    （下面 prompt 里有这个选项，字母 o 和数字 0 容易看混，改用数字 1）。
    """
    print("  去这里建 Token：")
    print("  https://github.com/settings/tokens")
    print("  （Classic 类型，Generate new token (classic)）")
    print("  点不开就输 1 回车，帮你打开这个网页。")


def _read_token_pasteable(prompt):
    """读 token：用普通 input，不用隐藏输入。

    原因：隐藏输入（getpass）在 Windows 黑窗口里右键粘贴经常没反应，
    改成普通 input 后粘贴用 Ctrl+V 就能贴进去（右键粘贴不灵就用 Ctrl+V）。
    代价是粘贴时屏幕上会显示出 Token，校验通过后立刻清屏，不让它留在屏幕上。
    注意：如果按 Ctrl+V 出来的还是你刚打的字母（比如只打出一个 T），
    说明剪贴板里当时就只有那个字母，Token 没复制上——回浏览器重新复制
    完整 Token（以 ghp_ 开头的那一长串）再回来按回车自动读取。
    """
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _clear_screen():
    """校验通过后清屏，把刚才显示出来的 Token 从屏幕上清掉。"""
    try:
        os.system("cls" if os.name == "nt" else "clear")
    except Exception:
        pass


def prompt_token():
    """启动时一步一步问你要 token：谁用问谁要。

    规则：仅本次运行有效、绝不写进文件；
    每输一次就去 GitHub 校验一次，不对就中文告诉你、让你重输，最多 3 次；
    输 q 或直接回车空着就退出。
    返回：校验通过的 token；3 次都不对或用户退出时返回 ""。
    """
    print("=" * 56)
    print("  GitHub 批量删库工具 " + VERSION)
    print("=" * 56)
    print("  第一步：先要你的 GitHub Token（谁用问谁要）。")
    print("  Token 需要勾选这些权限：delete_repo（删库）+ read:org（扫组织）+ repo")
    print()
    # 不再启动就自动弹浏览器：网址放在下面单独一行，左键点一下就能打开
    # （Windows Terminal 里按住 Ctrl 再左键点；普通黑窗口点不开就输 o 回车打开）。
    _print_token_link()
    print()
    print("  用法（不用粘贴也行）：")
    print("  1. 在浏览器里复制好 Token，回到这里直接按回车，自动从剪贴板读取；")
    print("  2. 或者在这里按 Ctrl+V 粘贴后按回车（右键粘贴不灵就用 Ctrl+V）。")
    print("  3. 粘贴不显示也不怕：用记事本在 exe 旁边新建 token.txt，")
    print("     把 Token 粘贴进去保存，再运行 exe，会自动读取，不用在黑窗口里粘贴。")
    print("  黑窗口里左键是选中、右键才是粘贴，左键点一下粘贴不了，这是黑窗口")
    print("  本身的规矩，不是脚本的问题；想用 Ctrl+V 就点窗口左上角图标")
    print("  -> 属性 -> 选项 -> 勾上“启用 Ctrl 键快捷方式”，以后 Ctrl+V 就能直接粘贴。")
    print("  还没建 Token 就输 1 回车，打开上面那个网页去建。")
    print("  粘贴后按回车会显示收到几位、开头是什么，校验通过后会自动清屏。")
    print("  这个 Token 只在本次运行里用，不会保存到任何文件里。")
    print("-" * 56)
    # 输 1/o/0 打开网页、剪贴板是空的，都不算一次校验机会；只有真拿 Token 去校验了才算
    # 注：字母 o 和数字 0 长得像，都当成打开网页，免得输错。
    attempt = 0
    while attempt < 3:
        raw = _read_token_pasteable(
            "  请粘贴 Token（已复制好直接按回车读取剪贴板，输 1 打开网页，输 q 退出）：")
        if raw.lower() == "q":
            return ""
        if raw.lower() in ("1", "o", "0", "打开"):
            try:
                webbrowser.open("https://github.com/settings/tokens")
                print("  已打开浏览器，去建好 Token、复制好，回来按回车。")
            except Exception:
                print("  打不开浏览器，手动复制上面那个网址到浏览器打开。")
            continue
        if not raw:
            # 直接按回车：自动从剪贴板取，不用粘贴
            tok = _read_clipboard()
            if not tok:
                print("  剪贴板是空的，先去浏览器复制好 Token 再回来按回车。")
                print("  粘贴实在不灵就用 token.txt：记事本新建 token.txt，粘贴保存再运行。")
                continue
            if not _looks_like_token(tok):
                print(f"  剪贴板里的不是 Token，长度 {len(tok)} 位，开头是 {tok[:10]!r}，")
                print("  请回浏览器复制完整 Token（以 ghp_ 开头的那一长串）再回来按回车。")
                continue
            print(f"  已从剪贴板读取到 Token，共 {len(tok)} 位，开头 {tok[:4]}。")
        else:
            tok = raw
            # 粘贴上去不显示也不怕：这里直接告诉你收到几位，证明粘贴进来了
            print(f"  已收到 Token，共 {len(tok)} 位，开头 {tok[:4]}。")
        attempt += 1
        print(f"  正在校验 Token（第 {attempt}/3 次）…")
        status, data, headers = api_request(tok, "GET", "/user")
        if status == 200:
            _clear_screen()
            print(f"  [成功] Token 有效，当前账号：{data.get('login')}")
            check_scopes(headers)
            return tok
        msg = data.get("message", "") if isinstance(data, dict) else data
        print(f"  [失败] Token 不对：HTTP {status} — {msg}")
        if status == 401:
            print("     一般是 Token 抄错了、过期了，或者不是这个账号的 Token。")
        if attempt < 3:
            print("  请重新粘贴一次（Ctrl+V），或直接按回车从剪贴板重读。")
    print("  [失败] 3 次都不对，退出。")
    return ""


def _pause(msg="  按回车返回菜单…"):
    """防闪退：每做完一步都等你按回车，双击 exe 也不会一闪而过。"""
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        pass


def _run_self(extra_args):
    """菜单里调自己：exe 就调自己，.py 就用 python 调自己。

    Token 已放进子进程的环境变量里，子进程不会再问你要 Token。
    """
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        cmd = [sys.executable] + extra_args
    else:
        cmd = [sys.executable, os.path.abspath(__file__)] + extra_args
    try:
        subprocess.run(cmd, env=env)
    except KeyboardInterrupt:
        print("\n  已中断，回到菜单。")


def _choose_scope(token):
    """菜单里先选范围：每个范围单独一行带序号，中间空一行，看得清楚。

    每个组织单独一个序号（1 个人、2 组织A、3 组织B……），不用再输 2-1 这种
    两层序号；每个范围后面还显示有几个仓库，一眼就能分清。
    返回：传给子进程的范围参数，如 ["--owner", "djkyc"] 或 ["--org", "xxx"]，
    选 9 是全部（个人+所有组织），选 0 或输错返回 None（回菜单）。
    """
    status, me, _ = api_request(token, "GET", "/user")
    if status != 200:
        print("  [失败] 拿不到当前账号，先检查 Token。")
        return None
    login = me.get("login", "")
    orgs, _ = api_get_all(token, "/user/orgs")
    org_names = [o.get("login", "") for o in orgs if o.get("login")]

    # 每个范围有几个仓库，先数一下，显示出来才分得清
    def _count(kind, owner):
        try:
            if kind == "org":
                items, _ = api_get_all(token, f"/orgs/{owner}/repos?type=all")
            else:
                items, _ = api_get_all(token, "/user/repos?affiliation=owner&sort=updated")
            return len(items)
        except Exception:
            return None

    options = []  # (序号, 类型, 名字, 个数)
    options.append((1, "user", login, _count("user", login)))
    for i, name in enumerate(org_names, 2):
        options.append((i, "org", name, _count("org", name)))

    print()
    print("=" * 56)
    print("  先选看哪个范围")
    print("=" * 56)
    print()
    for num, kind, name, cnt in options:
        label = "个人仓库" if kind == "user" else "组织仓库"
        cnt_text = f"（{cnt} 个仓库）" if cnt is not None else ""
        print(f"  {num}. {label}：{name} {cnt_text}")
        print()
    print("  9. 全部：个人 + 所有组织")
    print()
    print("  0. 返回菜单")
    print("-" * 56)
    sel = input("  请输入序号：").strip()
    if sel in ("0", ""):
        return None
    if sel == "9":
        return ["--scope", "all"]
    # 兼容老输入 2-1
    if sel.startswith("2-"):
        try:
            n = int(sel[2:])
        except ValueError:
            print("  [失败] 输入不对，回到菜单。")
            return None
        if 1 <= n <= len(org_names):
            return ["--org", org_names[n - 1]]
        print("  [失败] 没有这个组织，回到菜单。")
        return None
    try:
        n = int(sel)
    except ValueError:
        print("  [失败] 输入不对，回到菜单。")
        return None
    for num, kind, name, _cnt in options:
        if n == num:
            if kind == "user":
                return ["--owner", name]
            return ["--org", name]
    print("  [失败] 没有这个选项，回到菜单。")
    return None


def show_menu(token):
    """中文主菜单：无参数启动时进这里，循环到你选 0 退出为止。"""
    os.environ["GITHUB_TOKEN"] = token  # 只放进内存里的环境变量，不写文件
    while True:
        print()
        print("=" * 56)
        print("  GitHub 批量删库工具 — 中文菜单")
        print("=" * 56)
        print("  1. 查看仓库清单（只看不删，最安全）")
        print("  2. 勾选删除仓库（列清单 → 输序号 → 输 y 两次确认）")
        print("  3. 移动合并仓库（A 仓库搬到 B 仓库的子目录下）")
        print("  4. 归档仓库（代替删除，可恢复）")
        print("  0. 退出")
        print("-" * 56)
        try:
            choice = input("  请输入序号（0-4）：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  已退出。")
            return
        if choice == "0":
            print("  已退出，什么都没动。")
            return
        if choice == "1":
            scope = _choose_scope(token)
            if scope is None:
                continue
            _run_self(scope + ["--list-only"])
            _pause()
        elif choice == "2":
            scope = _choose_scope(token)
            if scope is None:
                continue
            print("  接下来会列出这个范围的仓库，你输序号勾选，删前还要输 y 两次确认。")
            _run_self(scope)
            _pause()
        elif choice == "3":
            src = input("  来源仓库（完整名，如 dj56959566/old-repo）：").strip()
            dst = input("  目标仓库（完整名，如 dj56959566/main-repo）：").strip()
            if not src or not dst or "/" not in src or "/" not in dst:
                print("  [失败] 两个都要写完整名（账号/仓库名），回到菜单。")
                _pause()
                continue
            sub = input("  子目录名（直接回车就用来源仓库名）：").strip()
            extra = ["--move", src, "--to", dst]
            if sub:
                extra += ["--subdir", sub]
            ans = input("  搬完要不要删除源库？输 y 才删，其他直接回车保留：").strip().lower()
            if ans == "y":
                extra += ["--delete-source"]
            print("  先演练一遍，确认没问题再真搬。")
            _run_self(extra + ["--dry-run"])
            ok = input("  演练看完了，确认要真搬？输 y 真搬，其他任意键取消：").strip().lower()
            if ok != "y":
                print("  已取消，没搬。")
                _pause()
                continue
            _run_self(extra)
            _pause()
        elif choice == "4":
            scope = _choose_scope(token)
            if scope is None:
                continue
            print("  归档是只读、可恢复的，比删除安全。")
            _run_self(scope + ["--archive"])
            _pause()
        else:
            print("  [失败] 没有这个选项，请输入 0-4。")


PAGE_SIZE = 10


def _print_repo_page(repos, page, total_pages):
    """打一页仓库表（一页 10 个），序号是全局序号，翻页后勾选还用这个号。"""
    start = (page - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, len(repos))
    print(f"\n共 {len(repos)} 个仓库，第 {page}/{total_pages} 页（一页 {PAGE_SIZE} 个）：")
    print(f"{'序号':>4}  {'仓库':<45} {'可见性':<8} {'*':>4}  {'更新':<10}  描述")
    for i in range(start, end):
        r = repos[i]
        desc = (r.get("description") or "")[:40].replace("\n", " ")
        vis = "private" if r.get("private") else "public"
        if r.get("archived"):
            vis += "/归档"
        print(f"{i + 1:>4}  {r['full_name']:<45} {vis:<8} "
              f"{r.get('stargazers_count', 0):>4}  "
              f"{(r.get('updated_at') or '')[:10]:<10}  {desc}")


def _show_repos_paged(repos):
    """分页显示仓库清单：一页 10 个，可以翻页。

    交互式才翻页（不是终端、或只有一页就一次打完，不卡住）；
    翻页：n 下一页 / p 上一页 / 数字跳到第几页 / q 结束浏览去勾选。
    序号是全局序号，翻页后勾选（1,3,5-8 / all）还用这个号。
    """
    total_pages = (len(repos) + PAGE_SIZE - 1) // PAGE_SIZE
    if total_pages <= 1 or not sys.stdin.isatty():
        _print_repo_page(repos, 1, total_pages)
        return
    page = 1
    while True:
        _print_repo_page(repos, page, total_pages)
        if total_pages == 1:
            return
        # 翻页提示单独成段、空行隔开，q 放最前面：看完了就输 q，去勾选要删的仓库
        print()
        print("-" * 56)
        print("  看完了吗？")
        print()
        print("  输 q  结束浏览，去勾选要删除的仓库")
        print()
        print("  输 n  下一页")
        print("  输 p  上一页")
        print("  输数字 跳到第几页（比如输 3 跳到第 3 页）")
        print("-" * 56)
        try:
            nav = input("  请输入 q / n / p / 数字：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if nav in ("q", "quit", "exit", ""):
            return
        if nav == "n":
            if page < total_pages:
                page += 1
            else:
                print("  已经是最后一页了，输 q 去勾选要删除的仓库。")
            continue
        if nav == "p":
            if page > 1:
                page -= 1
            else:
                print("  已经是第一页了。")
            continue
        try:
            n = int(nav)
        except ValueError:
            print("  输入不对：看完输 q 去勾选，还想翻页就输 n / p / 数字。")
            continue
        if 1 <= n <= total_pages:
            page = n
        else:
            print(f"  没有第 {n} 页，只有 1-{total_pages} 页。")


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
        print(f"  [警告]  同名工作流 {filename} 已存在，自动改名为 {new}")
        return new
    # 交互改名：回车=自动改名，输入“跳过”=不合并这个文件
    auto = _auto_rename(dst_dir, filename, source_repo)
    print(f"  [警告]  目标仓库已有同名工作流：{filename}")
    print(f"      来源仓库是 {source_repo}，直接覆盖会丢掉目标仓库原来的工作流。")
    ans = input(f"      请输入新名字（直接回车自动改成 {auto}，输入 跳过 则不合并这个文件）：").strip()
    if ans in ("跳过", "跳过这个", "skip"):
        print(f"      已跳过：{filename}")
        return None
    if not ans:
        return auto
    if "/" in ans or "\\" in ans:
        print("      [失败] 名字里不能带 /，已跳过这个文件。")
        return None
    if os.path.exists(os.path.join(dst_dir, ans)):
        print(f"      [失败] {ans} 也已存在，已跳过这个文件。")
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
        print(f"  [警告]  目标仓库已有目录 {subdir}/，自动改用 {cand}/")
        return cand
    ans = input(f"  [警告]  目标仓库已有目录 {subdir}/，请输入新的子目录名"
                f"（直接回车自动改成 {subdir}-1）：").strip()
    if not ans:
        n, cand = 1, f"{subdir}-1"
        while os.path.exists(os.path.join(dst_root, cand)):
            n += 1
            cand = f"{subdir}-{n}"
        return cand
    if "/" in ans or "\\" in ans or ans in (".", ".."):
        print("      [失败] 子目录名不合法，退出。")
        sys.exit(1)
    if os.path.exists(os.path.join(dst_root, ans)):
        print(f"      [失败] {ans}/ 也已存在，退出。")
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
                print(f"  [提示]  工作流 {f} 与目标完全相同，已跳过。")
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
        print(f"[失败] 命令失败：{' '.join(cmd[:3])}…\n   {err}")
        sys.exit(1)
    return r


def _clone(token, full_name, dest):
    url = f"https://oauth2:{token}@github.com/{full_name}.git"
    print(f"[提示]  正在克隆 {full_name} …")
    r = subprocess.run(["git", "clone", "--depth", "1", url, dest],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        err = (r.stderr or "").replace(token, "***").strip()
        print(f"[失败] 克隆失败 {full_name}：{err}")
        print("   常见原因：仓库不存在 / token 看不到它（私有库要 repo 权限）/ 网络问题。")
        sys.exit(1)


def do_move(token, args):
    """--move 的 GitHub 全流程：校验 → 克隆 → 计划 → 确认 → 合并提交推送 → 可选删源库"""
    source, target = args.move.strip(), args.to.strip()
    if "/" not in source or "/" not in target:
        print("[失败] --move 和 --to 都要写完整名，如 --move dj56959566/old-repo --to dj56959566/main-repo")
        sys.exit(1)
    if source.lower() == target.lower():
        print("[失败] 来源和目标是同一个仓库，退出。")
        sys.exit(1)
    source_repo = source.split("/")[-1]
    subdir = (args.subdir or source_repo).strip().strip("/")
    if not subdir:
        print("[失败] 子目录名不能为空。")
        sys.exit(1)

    # 校验两个仓库都存在、能读到，并拿到登录人（用于 git 提交身份）
    for name, role in ((source, "来源"), (target, "目标")):
        status, data, _ = api_request(token, "GET", f"/repos/{name}")
        if status != 200:
            msg = data.get("message", "") if isinstance(data, dict) else data
            print(f"[失败] {role}仓库 {name} 读不到：HTTP {status} — {msg}")
            sys.exit(1)
        print(f"[提示]  {role}仓库：{name}（默认分支 {data.get('default_branch')}）")
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
        print(f"\n[移动] 移动计划：{source} → {target}/{final_subdir}/")
        print(f"  源码文件 {len(to_copy)} 个（除 .git、.github/workflows 外，全部进子目录）")
        for f in to_copy[:20]:
            print(f"    + {final_subdir}/{f}")
        if len(to_copy) > 20:
            print(f"    … 还有 {len(to_copy) - 20} 个")
        print(f"  工作流 {len(to_workflow)} 个（合并到目标 .github/workflows/，不进子目录）")
        for f in to_workflow:
            print(f"    + .github/workflows/{os.path.basename(f)}")
        if not to_copy and not to_workflow:
            print("[提示]  来源仓库是空的，没什么可搬，退出。")
            return
        if args.dry_run:
            print("\n[成功] --dry-run 演练结束：上面就是会搬的内容，没改目标仓库、没推送、没删源库。")
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
            print("[提示]  合并后目标仓库没有任何变化（可能内容完全相同），不用提交。")
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
            print(f"[失败] 推送失败：{err}")
            print("   常见原因：你对目标仓库没有写权限（组织仓库要 Write 以上）。")
            sys.exit(1)
        print(f"\n[成功] 已搬完并推送到 {target}"
              f"（分支 {target_branch or '默认分支'}）："
              f"源码 {copied} 个文件进 {final_subdir}/，"
              f"工作流合并 {len(merged)} 个，跳过 {len(skipped)} 个。")
        for old, new in merged:
            print(f"  工作流：{old}" + (f" → {new}" if new != old else "（同名保留）"))
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        with open(f"moved_{ts}.log", "w", encoding="utf-8") as f:
            f.write(f"# 移动记录 {ts} {source} -> {target}/{final_subdir}/\n")
            f.write(f"源码 {copied} 个，工作流 {merged}，跳过 {skipped}\n")
        print(f"[提示]  记录已写入 moved_{ts}.log")

        # 可选：搬完删源库（输 y 确认）
        if args.delete_source:
            typed = input(f"\n源库 {source} 已搬完，是否删除源库？删除不可逆，"
                          f"输入 y 删除，其他任意键保留源库：").strip().lower()
            if typed != "y":
                print("已保留源库。")
                return
            status, data, _ = api_request(token, "DELETE", f"/repos/{source}")
            if status == 204:
                print(f"[成功] 源库 {source} 已删除。")
            else:
                msg = data.get("message", "") if isinstance(data, dict) else data
                print(f"[失败] 源库删除失败：HTTP {status} — {msg}")


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
    p.add_argument("--yes", action="store_true", help="跳过第一次 y 确认（仍需第二次 y 确认）")
    p.add_argument("--force", action="store_true",
                   help="配合 --file --yes 做完全非交互（危险，仍会写日志）")
    # 移动合并
    p.add_argument("--move", help="来源仓库完整名，如 --move dj56959566/old-repo")
    p.add_argument("--to", dest="to", help="目标仓库完整名，如 --to dj56959566/main-repo")
    p.add_argument("--subdir", help="搬到目标仓库下的子目录名，默认=来源仓库名")
    p.add_argument("--delete-source", action="store_true",
                   help="搬完后删除源库（仍需输入 y 确认）")
    p.add_argument("--auto-rename", action="store_true",
                   help="同名工作流/子目录自动改名，不逐个问你")
    args = p.parse_args()

    # 无参数启动（双击 exe / 直接 python3 clean_repos.py）进中文菜单，
    # 带参数（如 --list-only / --move / --file）走命令行模式，不进菜单。
    menu_mode = len(sys.argv) == 1

    token = get_token(args)
    if not token:
        if sys.stdin.isatty():
            # 谁用问谁要：一步一步交互问 Token，校验通过才继续
            token = prompt_token()
            if not token:
                _pause("  按回车退出…")
                sys.exit(1)
        else:
            print("[失败] 没找到 token。请先做一步（二选一）：\n"
                  "  export GITHUB_TOKEN=ghp_xxxx   # classic PAT，勾选 delete_repo + read:org\n"
                  "  或 gh auth login                # 用 gh 登录，token 要有 delete_repo 权限\n"
                  "  建 token：https://github.com/settings/tokens")
            sys.exit(1)

    if menu_mode:
        show_menu(token)
        return

    # ---- 移动合并模式：--move 优先，不走删库流程 ----
    if args.move:
        if not args.to:
            print("[失败] --move 必须配合 --to，如 --move A/old --to A/main")
            sys.exit(1)
        do_move(token, args)
        return

    # ---- 1. 确定要删的仓库列表 ----
    if args.file:
        if not os.path.exists(args.file):
            print(f"[失败] 找不到文件：{args.file}")
            sys.exit(1)
        targets = []
        for line in open(args.file, encoding="utf-8"):
            name = line.strip()
            if name and not name.startswith("#") and "/" in name:
                targets.append(name)
        if not targets:
            print(f"[失败] {args.file} 里没有有效的 owner/repo 行")
            sys.exit(1)
        print(f"[提示]  从文件读到 {len(targets)} 个待删仓库")
        repos = [{"full_name": n} for n in targets]
    else:
        # 先拿当前登录人，顺便校验 token 是否有效
        status, me, headers = api_request(token, "GET", "/user")
        if status != 200:
            msg = me.get("message", "") if isinstance(me, dict) else me
            print(f"[失败] token 校验失败：HTTP {status} — {msg}")
            sys.exit(1)
        login = me["login"]
        check_scopes(headers)
        print(f"[提示]  当前账号：{login}")

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
                print("[提示]  你没有加入任何组织")
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
            print(f"[提示]  {owner}：{len(items)} 个仓库")
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
            print("[提示]  没有符合条件的仓库，退出。")
            return

        # 备份完整清单（删前必做）
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        with open("repos_list.txt", "w", encoding="utf-8") as f:
            f.write(f"# GitHub 仓库清单 {ts} 账号 {login} 共 {len(repos)} 个\n")
            for r in repos:
                f.write(f"{r['full_name']}\t"
                        f"{'private' if r.get('private') else 'public'}\t"
                        f"*{r.get('stargazers_count', 0)}\t"
                        f"{(r.get('updated_at') or '')[:10]}\t"
                        f"{(r.get('description') or '').replace(chr(10), ' ')}\n")
        print(f"[提示]  完整清单已备份到 repos_list.txt（{len(repos)} 个）")

        # ---- 2. 展示 + 交互勾选 ----
        # 一页 10 个，可以翻页；序号是全局序号，勾选还用这个号
        _show_repos_paged(repos)

        if args.list_only:
            print("\n[成功] --list-only 模式：只列清单，不删除。清单在 repos_list.txt")
            return

        raw = input("\n要删除哪些？输入序号，如 1,3,5-8 / all=全部 / 0=退出：").strip()
        picked = parse_selection(raw, len(repos))
        if picked is None:
            print("[失败] 输入格式不对，退出。示例：1,3,5-8")
            sys.exit(1)
        if not picked:
            print("已退出，什么都没删。")
            return
        targets = [repos[i]["full_name"] for i in sorted(picked)]
        repos = [{"full_name": n} for n in targets]

    # ---- 3. 三重确认 ----
    action = "归档" if args.archive else "删除"
    print(f"\n[警告]  即将{action}以下 {len(targets)} 个仓库（{action}后"
          f"{'可恢复' if args.archive else '不可恢复，代码/Issues/PR 全没了'}）：")
    for n in targets:
        print(f"  - {n}")

    if args.dry_run:
        print("\n[成功] --dry-run 演练结束：上面这些就是会删的，没真删。")
        return

    if args.file and args.yes and args.force:
        pass  # 文件模式 + --yes --force：明确声明过才跳过交互确认
    else:
        if not args.yes:
            ok = input("\n确认清单无误？输入 y 继续，其他任意键退出：").strip().lower()
            if ok != "y":
                print("已取消，什么都没删。")
                return
        # 最后确认直接输 y 就行，不用再一字不差输 DELETE
        ok2 = input(f"最后确认：真的要{action}这 {len(targets)} 个仓库？输入 y 确认，其他任意键取消：").strip().lower()
        if ok2 != "y":
            print("已取消，什么都没删。")
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
            print(f"  [成功] {action}成功：{name}")
            ok_list.append(name)
        else:
            msg = data.get("message", "") if isinstance(data, dict) else data
            print(f"  [失败] {action}失败：{name} — HTTP {status} — {msg}")
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
