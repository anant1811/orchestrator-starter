#!/usr/bin/env python3
"""Orchestrator helper: see, start, prompt and read your Claude Code agents.

  orch.py status                          open agents, with short IDs, state and idle time
  orch.py show <agent> [-n N]             the agent's last N replies (from its transcript)
  orch.py send <agent> <text> [--ship] [--force]
  orch.py wait <agent> [--timeout MIN]    wait until it stops working, then show its reply
  orch.py start <project> [<text>] [--ship]          new agent in that project's folder
  orch.py find <words>                    search past chats
  orch.py resume <session-id> [<text>] [--ship]      reopen a past chat in the background
  orch.py projects [<words>]              list or resolve project folders

<agent> is a short ID (session prefix), a pane ID, a word of the title, or a folder name.
Prompts get a guard line unless --ship is passed (only when the user said yes to shipping).
New agents open in the background, so nothing steals the user's focus.

Backends: tmux (default) or herdr (used when you run inside herdr).
Settings (environment variables, all optional):
  ORCH_BACKEND       tmux | herdr        (default: herdr inside herdr, else tmux)
  ORCH_TMUX_SESSION  tmux session for new agents   (default: agents)
  ORCH_HERDR_SPACE   herdr space for new agents    (default: Agents)
  ORCH_CLAUDE        command that starts Claude Code (default: claude)
  ORCH_CLAUDE_ARGS   extra arguments for every new agent, e.g. "--model sonnet"
Project folders and nicknames live in aliases.json next to this file.
"""
import json, os, re, shlex, shutil, subprocess, sys, tempfile, time, uuid
from pathlib import Path

HOME = Path.home()
HERE = Path(__file__).resolve().parent
PROJECTS_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude")) / "projects"
CLAUDE = os.environ.get("ORCH_CLAUDE", "claude")
CLAUDE_ARGS = shlex.split(os.environ.get("ORCH_CLAUDE_ARGS", ""))
GUARD = ("\n\n(Sent by the orchestrator. Do not commit, push, deploy, publish, or send anything "
         "outside this computer. When done, reply with a short summary of what you changed and what needs the user.)")
ORDER = {"working": 0, "blocked": 1, "done": 2, "idle": 3, "starting": 4, "exited": 5}


def die(msg):
    sys.exit(f"error: {msg}")


def run(cmd, stdin=None, check=True):
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=120)
    if check and p.returncode:
        die(f"{' '.join(cmd[:3])}: {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


# ---------- transcripts (shared by both backends) ----------

def transcript(session):
    if not session:
        return None
    hits = list(PROJECTS_DIR.glob(f"*/{session}.jsonl"))
    return hits[0] if hits else None


def newest_transcript(cwd):
    """Newest transcript for a folder: for agents that orch.py did not start."""
    d = PROJECTS_DIR / re.sub(r"[^A-Za-z0-9]", "-", cwd)
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if d.is_dir() else []
    return files[0].stem if files else None


def entries(path):
    for line in open(path, errors="replace"):
        try:
            yield json.loads(line)
        except ValueError:
            continue


def is_human(e):
    content = (e.get("message") or {}).get("content")
    return e.get("type") == "user" and not e.get("isMeta") and (
        isinstance(content, str) or any(isinstance(c, dict) and c.get("type") == "text" for c in content or []))


def turns(session):
    """Assistant text per human turn: [[text, ...], ...]."""
    path = transcript(session)
    if not path:
        return None
    out, cur = [], None
    for e in entries(path):
        content = (e.get("message") or {}).get("content")
        if is_human(e):
            if cur:
                out.append(cur)
            cur = []
        elif e.get("type") == "assistant" and isinstance(content, list) and cur is not None:
            cur += [c["text"] for c in content if c.get("type") == "text" and c.get("text", "").strip()]
    if cur is not None:
        out.append(cur)
    return out


def last_replies(session, n=1, limit=6000):
    t = turns(session)
    if t is None:
        return "(no transcript found)"
    text = "\n\n---\n\n".join("\n\n".join(x) for x in t[-n:] if x) or "(no reply yet)"
    return text if len(text) <= limit else "…" + text[-limit:]


def session_info(path):
    cwd, title, first = "", "", ""
    for e in entries(path):
        cwd = cwd or e.get("cwd", "")
        if e.get("type") == "custom-title":
            title = e.get("customTitle") or title
        elif e.get("type") == "ai-title" and not title:
            title = e.get("aiTitle", "")
        elif is_human(e) and not first:
            c = e["message"]["content"]
            first = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return {"cwd": cwd, "title": title or " ".join(first.split())[:70]}


# ---------- tmux backend ----------

SHELLS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "nu", "-bash", "-zsh", "login"}
TMUX_FIELDS = ["pane_id", "session_name", "window_name", "pane_current_command", "pane_current_path",
               "pane_title", "window_activity", "session_attached", "window_active", "pane_active",
               "@orch_session", "@orch_started"]


class Tmux:
    name = "tmux"

    def __init__(self):
        self.session = os.environ.get("ORCH_TMUX_SESSION", "agents")

    def tmux(self, *args, stdin=None, check=True):
        return run(["tmux", *args], stdin=stdin, check=check)

    def screen(self, pane, lines=60):
        return self.tmux("capture-pane", "-p", "-J", "-t", pane, "-S", f"-{lines}", check=False).rstrip()

    @staticmethod
    def state(screen, title=""):
        tail = "\n".join(screen.splitlines()[-25:]).lower()
        spinner = re.search(r"^\s*\S\s+\S.*…\s*\(\d", tail, re.M)  # "✽ Thinking… (4s · ↓ 80 tokens)"
        if spinner or "esc to interrupt" in tail or re.match(r"[\u2800-\u28ff]", title):
            return "working"
        if re.search(r"do you want to|do you trust|❯\s*1\.|enter to confirm|esc to cancel", tail):
            return "blocked"
        return "idle"

    def agents(self):
        fmt = "\t".join("#{%s}" % f for f in TMUX_FIELDS)
        out = run(["tmux", "list-panes", "-a", "-F", fmt], check=False) if shutil.which("tmux") else ""
        result = []
        for line in out.splitlines():
            r = dict(zip(TMUX_FIELDS, line.split("\t")))
            cmd = r["pane_current_command"]
            is_claude = "claude" in cmd.lower() or re.fullmatch(r"\d+\.\d+\.\d+", cmd)  # native installs show the version
            if not (r["@orch_session"] or is_claude):
                continue
            session = r["@orch_session"] or newest_transcript(r["pane_current_path"])
            if cmd in SHELLS:
                fresh = time.time() - float(r["@orch_started"] or 0) < 60
                status = "starting" if fresh else "exited"
            else:
                status = self.state(self.screen(r["pane_id"], 30), r["pane_title"])
                if status == "idle" and session and (turns(session) or [[]])[-1]:
                    status = "done"
            title = re.sub(r"^[^\w(]+", "", r["pane_title"]).strip()
            if not title or title.split(".")[0] == os.uname().nodename.split(".")[0]:
                title = r["window_name"]
            result.append({
                "key": r["pane_id"], "pane": r["pane_id"], "session": session or "", "status": status,
                "title": title, "cwd": r["pane_current_path"], "group": r["session_name"],
                "idle_min": max(0, int(time.time() - int(r["window_activity"] or time.time())) // 60),
                "focused": r["session_attached"] not in ("", "0") and r["window_active"] == "1" and r["pane_active"] == "1",
            })
        return result

    def read(self, a):
        return self.screen(a["pane"])

    def prompt(self, a, text):
        self.tmux("load-buffer", "-b", "orch", "-", stdin=text)
        self.tmux("paste-buffer", "-p", "-d", "-b", "orch", "-t", a["pane"])  # bracketed paste keeps newlines
        time.sleep(0.6)
        self.tmux("send-keys", "-t", a["pane"], "Enter")

    def start(self, folder, claude_args, text):
        name = re.sub(r"[^a-z0-9]+", "-", folder.name.lower()).strip("-")[:20] or "agent"
        if subprocess.run(["tmux", "has-session", "-t", f"={self.session}"], capture_output=True).returncode == 0:
            pane = self.tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", f"={self.session}:",
                             "-c", str(folder), "-n", name).strip()
        else:
            pane = self.tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", self.session,
                             "-c", str(folder), "-n", name).strip()
        resume = claude_args[1] if claude_args[:1] == ["--resume"] else None
        session = resume or str(uuid.uuid4())
        self.tmux("set-option", "-p", "-t", pane, "@orch_session", session)
        self.tmux("set-option", "-p", "-t", pane, "@orch_started", str(int(time.time())))
        args = [*CLAUDE_ARGS, *claude_args] if resume else ["--session-id", session, *CLAUDE_ARGS, *claude_args]
        cmd = " ".join(shlex.quote(x) for x in [CLAUDE, *args])
        if text:  # the first prompt goes in as an argument: no need to wait for the UI
            fd, path = tempfile.mkstemp(prefix="orch-", suffix=".txt")
            with os.fdopen(fd, "w") as f:
                f.write(text)
            cmd += f' "$(cat {shlex.quote(path)}; rm -f {shlex.quote(path)})"'
        time.sleep(0.8)  # let the shell reach its prompt
        self.tmux("send-keys", "-t", pane, "-l", cmd)
        self.tmux("send-keys", "-t", pane, "Enter")
        return {"key": pane, "pane": pane, "session": session, "group": self.session}

    def where(self):
        return f"tmux session '{self.session}' (attach: tmux attach -t {self.session})"


# ---------- herdr backend ----------

class Herdr:
    name = "herdr"

    def __init__(self):
        self.space = os.environ.get("ORCH_HERDR_SPACE", "Agents")

    def herdr(self, *args, check=True):
        p = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=120)
        out = p.stdout.strip()
        d = json.loads(out) if out.startswith("{") else {}
        if check and ("error" in d or not d):
            die(f"herdr {' '.join(args[:2])}: {d.get('error') or p.stderr.strip() or out}")
        return d.get("result", {})

    def spaces(self):
        return {w["workspace_id"]: w["label"] for w in self.herdr("workspace", "list").get("workspaces", [])}

    def agents(self):
        names = self.spaces()
        result = []
        for a in self.herdr("agent", "list").get("agents", []):
            if a.get("agent") != "claude":
                continue
            session = (a.get("agent_session") or {}).get("value") or ""  # new agents get it after a few seconds
            key = (a.get("tokens") or {}).get("sort_key")
            result.append({
                "key": session or a["pane_id"], "pane": a["pane_id"], "name": a.get("name", ""),
                "session": session, "status": a.get("agent_status", "?"),
                "title": a.get("terminal_title_stripped", ""), "cwd": a.get("cwd", ""),
                "group": names.get(a["workspace_id"], a["workspace_id"]),
                "idle_min": int(time.time() // 60) - int(key) if str(key).isdigit() else None,
                "focused": bool(a.get("focused")),
            })
        return result

    def read(self, a):
        return self.herdr("agent", "read", a["pane"], check=False).get("text", "")

    def prompt(self, a, text):
        self.herdr("agent", "prompt", a["pane"], text)

    def start(self, folder, claude_args, text):
        ws = next((w for w, label in self.spaces().items() if label == self.space), None) or \
            self.herdr("workspace", "create", "--label", self.space, "--no-focus", "--cwd", str(folder))["workspace"]["workspace_id"]
        tab = self.herdr("tab", "create", "--workspace", ws, "--cwd", str(folder), "--no-focus")
        pane = tab["root_pane"]["pane_id"]
        name = (re.sub(r"[^a-z0-9]+", "-", folder.name.lower()).strip("-")[:20] or "agent") + "-" + str(int(time.time()) % 10000)
        if not name[0].isalpha():
            name = "p" + name
        time.sleep(1.5)  # let the login shell reach its prompt
        a = self.herdr("agent", "start", name, "--kind", "claude", "--pane", pane, "--timeout", "90000",
                       "--", *CLAUDE_ARGS, *claude_args)["agent"]
        agent = {"key": a["pane_id"], "pane": a["pane_id"], "session": (a.get("agent_session") or {}).get("value") or "",
                 "group": self.space, "name": name, "status": "idle", "focused": False}
        if text:
            self.prompt(agent, text)
        return agent

    def where(self):
        return f"herdr space '{self.space}'"


def backend():
    choice = os.environ.get("ORCH_BACKEND", "auto").lower()
    if choice == "auto":
        if os.environ.get("HERDR_ENV") == "1" and shutil.which("herdr"):
            choice = "herdr"
        elif shutil.which("tmux"):
            choice = "tmux"
        elif shutil.which("herdr"):
            choice = "herdr"
        else:
            die("install tmux (brew install tmux / apt install tmux) or herdr")
    if choice not in ("tmux", "herdr") or not shutil.which(choice):
        die(f"backend {choice!r} is not available")
    return Tmux() if choice == "tmux" else Herdr()


B = None  # the backend, set in main()


# ---------- commands ----------

def short(a):
    return (a["session"] or a["key"])[:8]


def resolve(target, pool=None):
    pool = B.agents() if pool is None else pool
    t = target.lower()
    for test in (lambda a: a["session"].startswith(t) and len(t) >= 4, lambda a: a.get("name") == target,
                 lambda a: a["pane"] == target or a["key"] == target,
                 lambda a: a["title"].lower() == t,
                 lambda a: re.search(rf"\b{re.escape(t)}\b", a["title"].lower()),
                 lambda a: t in a["title"].lower(),
                 lambda a: t in Path(a["cwd"]).name.lower()):
        hits = [a for a in pool if test(a)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            die(f"{target!r} matches {len(hits)} agents: " + ", ".join(f"{short(a)} {a['title']!r}" for a in hits))
    die(f"no open agent matches {target!r} (try: orch.py status, or orch.py find)")


def ago(m):
    if m is None:
        return "?"
    return f"{m}m" if m < 60 else f"{m // 60}h" if m < 48 * 60 else f"{m // 1440}d"


def tilde(path):
    return str(path).replace(str(HOME), "~", 1)


def cmd_status(_):
    pool = B.agents()
    if not pool:
        print(f"no open agents ({B.name}). Start one: orch.py start <project> \"<task>\"")
        return
    groups = {}
    for a in pool:
        groups.setdefault(a["group"], []).append(a)
    me = os.environ.get("TMUX_PANE") or os.environ.get("HERDR_PANE_ID")
    for group, items in sorted(groups.items()):
        print(f"## {group} ({len(items)})")
        for a in sorted(items, key=lambda a: (ORDER.get(a["status"], 9), a["idle_min"] or 0)):
            you = " (you)" if me and a["pane"] == me else ""
            print(f"  {short(a)}  {a['status']:<8} {ago(a['idle_min']):>4}  {Path(a['cwd']).name:<24.24}  {a['title']}{you}")


def cmd_show(args):
    n = int(args[args.index("-n") + 1]) if "-n" in args else 1
    a = resolve(args[0])
    print(f"[{short(a)} {a['status']}] {a['title']}\n")
    print(last_replies(a["session"], n))


def prompt(a, text, ship, force):
    if a["status"] in ("working", "blocked") and not force:
        die(f"{short(a)} is {a['status']}; wait for it, or pass --force")
    if a.get("focused") and not force:
        die(f"the user is looking at {short(a)} right now; ask before sending, then pass --force")
    B.prompt(a, text if ship else text + GUARD)
    print(f"sent to {short(a)} {a.get('title', '')!r}")


def cmd_send(args):
    rest = [x for x in args if x not in ("--ship", "--force")]
    if len(rest) < 2:
        die("usage: orch.py send <agent> <text>")
    prompt(resolve(rest[0]), " ".join(rest[1:]), "--ship" in args, "--force" in args)


def cmd_wait(args):
    timeout = float(args[args.index("--timeout") + 1]) if "--timeout" in args else 30
    key = resolve(args[0])["key"]
    start = time.time()
    end, seen_work = start + timeout * 60, False
    while time.time() < end:
        a = next((x for x in B.agents() if x["key"] == key or (x["session"] and x["session"] == key)), None)
        if a is None:
            die(f"{key[:8]} is no longer open")
        st = a["status"]
        seen_work |= st == "working"
        settled = seen_work or time.time() - start > 20  # a fresh agent needs a few seconds to begin
        if st == "exited" or (st in ("idle", "done", "blocked") and settled):
            print(f"[{short(a)} {st}] {a['title']}\n")
            if st in ("blocked", "exited"):
                print("It waits for an answer or approval. Screen:\n" if st == "blocked" else "Claude Code is not running. Screen:\n")
                print(B.read(a)[-3000:])
            else:
                print(last_replies(a["session"]))
            return
        time.sleep(4)
    die(f"still working after {timeout:g} min")


def load_config():
    f = HERE / "aliases.json"
    data = json.load(open(f)) if f.exists() else {}
    if "aliases" in data or "roots" in data:
        aliases, roots = data.get("aliases", {}), data.get("roots", [])
    else:  # a plain {"nickname": "path"} map
        aliases, roots = data, []
    aliases = {k: Path(os.path.expanduser(v)) for k, v in aliases.items() if not k.startswith("_")}
    roots = [Path(os.path.expanduser(r)) for r in roots]
    return aliases, roots


def project_folders(roots):
    out = []
    for r in roots:
        if r.is_dir():
            out += [p for p in r.iterdir() if p.is_dir() and not p.name.startswith(".") and p not in roots]
    return out


def resolve_project(q):
    aliases, roots = load_config()
    ql = q.lower()
    for name, path in aliases.items():
        if ql == name.lower():
            return path
    folders = project_folders(roots) + list(aliases.values())
    exact = [p for p in folders if p.name.lower() == ql]
    hits = exact or [p for p in folders if ql in p.name.lower()] + [v for k, v in aliases.items() if ql in k.lower()]
    hits = list(dict.fromkeys(hits))
    if len(hits) == 1:
        return hits[0]
    if not hits and Path(os.path.expanduser(q)).is_dir():
        return Path(os.path.expanduser(q)).resolve()
    die(f"project {q!r} matches {len(hits)} folders: " + "; ".join(tilde(h) for h in hits[:10])
        if hits else f"no project folder matches {q!r} (add it to aliases.json, or pass a path)")


def cmd_projects(args):
    aliases, roots = load_config()
    q = " ".join(args).lower()
    for name, path in sorted(aliases.items()):
        if q in name.lower() or q in str(path).lower():
            print(f"  {name:<24} → {tilde(path)}{'' if path.is_dir() else '  (missing)'}")
    for p in sorted(project_folders(roots)):
        if q in str(p).lower() and p not in aliases.values():
            print(f"  {tilde(p)}")


def start_agent(folder, claude_args, rest, ship):
    text = " ".join(rest)
    return B.start(folder, claude_args, (text if ship else text + GUARD) if text else "")


def cmd_start(args):
    ship = "--ship" in args
    rest = [x for x in args if x != "--ship"]
    if not rest:
        die("usage: orch.py start <project> [<text>]")
    folder = resolve_project(rest[0])
    if not folder.is_dir():
        die(f"folder is missing: {tilde(folder)}")
    a = start_agent(folder, [], rest[1:], ship)
    print(f"started {short(a)} in {tilde(folder)} ({B.where()})")


def cmd_find(args):
    words = [w.lower() for w in args]
    if not words:
        die("usage: orch.py find <words>")
    open_ids = {a["session"] for a in B.agents()}
    files = sorted(PROJECTS_DIR.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    found = 0
    for path in files[:2000]:
        try:
            text = path.read_text(errors="replace").lower()
        except OSError:
            continue
        if not all(w in text for w in words):
            continue
        info = session_info(path)
        state = "OPEN" if path.stem in open_ids else time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime))
        print(f"  {path.stem}  {state:<10}  {Path(info['cwd']).name:<22.22}  {info['title'][:70]}")
        found += 1
        if found >= 10:
            break
    if not found:
        print(f"no past chat mentions: {' '.join(args)}")


def cmd_resume(args):
    ship = "--ship" in args
    rest = [x for x in args if x != "--ship"]
    s = rest[0]
    if any(a["session"] == s for a in B.agents()):
        die(f"{s[:8]} is already open; use send")
    path = transcript(s)
    if not path:
        die(f"no transcript for {s} in {tilde(PROJECTS_DIR)}")
    info = session_info(path)
    folder = Path(info["cwd"])
    if not folder.is_dir():
        die(f"folder of {s[:8]} is gone: {folder}")
    start_agent(folder, ["--resume", s], rest[1:], ship)
    print(f"resumed {s[:8]} {info['title']!r} in {tilde(folder)} ({B.where()})")


def main():
    global B
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd = {"status": cmd_status, "show": cmd_show, "send": cmd_send, "wait": cmd_wait, "start": cmd_start,
           "find": cmd_find, "resume": cmd_resume, "projects": cmd_projects}.get(sys.argv[1])
    if not cmd:
        die(f"unknown command {sys.argv[1]!r} (see orch.py --help)")
    if sys.argv[1] in ("show", "send", "wait", "resume") and len(sys.argv) < 3:
        die("usage: see orch.py --help")
    if sys.argv[1] != "projects":
        B = backend()
    cmd(sys.argv[2:])


if __name__ == "__main__":
    main()
