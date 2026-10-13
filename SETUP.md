# Setup

Open a terminal in this folder, start Claude Code (`claude`) or Codex (`codex`), and paste the prompt below. The agent finds your project folders, fills `aliases.json`, and tests one agent from start to finish.

````text
Set up this orchestrator for me. Follow these steps in order and stop to ask me when a step says so.

1. Check the requirements: python3 (3.8 or newer), Claude Code (`claude --version`) or Codex (`codex --version`), and tmux or herdr.
   If only Codex is installed, orch.py uses Codex agents by default. If both are installed, ask me which one new agents should use, and tell me to set `export ORCH_AGENT=codex` in my shell profile if I pick Codex.
   If neither tmux nor herdr is installed, tell me the install command for my OS (for example `brew install tmux` or `sudo apt install tmux`) and wait.
2. Find my project folders. Look for git repositories up to 3 levels deep in these places if they exist:
   ~/code, ~/Code, ~/dev, ~/src, ~/projects, ~/Projects, ~/GitHub, ~/repos, ~/Developer, ~/Documents, ~/Desktop.
   Use `find <dir> -maxdepth 3 -name .git -prune` and skip node_modules, vendor, caches and anything inside ~/Library.
3. Show me the list grouped by parent folder. Suggest:
   - "roots": the parent folders that hold most of my projects (every child folder of a root counts as a project);
   - "aliases": a short nickname for each project I am likely to name in chat.
   Wait for my OK or my edits.
4. Write `aliases.json` in this format (replace the example rows):
   {"roots": ["~/code"], "aliases": {"blog": "~/code/my-blog"}}
   Use ~ for my home folder. Then run `./orch.py projects` and show me the output.
5. Clear the example rows in `ledger.md`. Keep its headings and the format line.
6. Test one agent. Pick the smallest project from the list, then:
   - run `./orch.py start <nickname> "Reply with the word READY and nothing else. Do not change any files."`
   - run `./orch.py wait <short ID from the start output> --timeout 5`
   - run `./orch.py status`
   If the agent is blocked (for example a "trust this folder" question, or Codex's sandbox approval), show me its screen and tell me how to open it:
   `tmux attach -t agents` for tmux, or the herdr space "Agents" for herdr.
7. Tell me in 3 short lines: which backend is used, how many projects are set up, and whether the test agent replied READY.
   Then tell me how to close the test agent (in tmux: attach, then type /exit (Claude Code) or /quit (Codex) and close the window).

From then on, start Claude Code or Codex in this folder whenever you want to talk to the orchestrator. Claude Code follows CLAUDE.md; Codex follows AGENTS.md.
````

## Settings

All settings are optional environment variables. Set them in your shell profile.

| Variable | Default | What it does |
| --- | --- | --- |
| `ORCH_AGENT` | `claude` | `claude` or `codex`: the kind of new agents. `start --codex` or `--claude` picks one per agent. |
| `ORCH_BACKEND` | `auto` | `tmux` or `herdr`. `auto` uses herdr when you run inside herdr, else tmux. |
| `ORCH_TMUX_SESSION` | `agents` | The tmux session that holds new agents. |
| `ORCH_HERDR_SPACE` | `Agents` | The herdr space that holds new agents. |
| `ORCH_CLAUDE` | `claude` | The command that starts Claude Code. |
| `ORCH_CLAUDE_ARGS` | (empty) | Extra arguments for each new Claude Code agent, for example `--model sonnet`. |
| `ORCH_CODEX` | `codex` | The command that starts Codex. |
| `ORCH_CODEX_ARGS` | (empty) | Extra arguments for each new Codex agent, for example `--full-auto`. |
| `CODEX_HOME` | `~/.codex` | Where Codex keeps its sessions (Codex's own setting). |
