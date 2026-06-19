# claude-tokens

Analyse token usage and cost from your local [Claude Code](https://claude.com/claude-code) transcripts.

`claude-tokens` reads the `.jsonl` session files under `~/.claude/projects/` and aggregates token usage and estimated USD cost across several time windows — all-time, recent, today, and your current session — plus breakdowns by model, project, session, or individual prompt.

No dependencies. It's a single Python script that uses only the standard library.

<img width="846" height="286" alt="Screenshot 2026-06-18 at 9 52 43 PM" src="https://github.com/user-attachments/assets/06ea5d8c-8f2e-423d-93d7-2c5bdeb81d2e" />


## Requirements

- **Python 3.8 or newer** (standard library only — nothing to `pip install`)
- Claude Code installed, with transcripts in `~/.claude/projects/`

## Install

### Option 1 — clone and symlink (recommended)

```bash
git clone https://github.com/leonelRos/Claude-token-tool.git
cd Claude-token-tool
chmod +x claude-tokens.py

# Put it on your PATH as the command `claude-tokens`
mkdir -p ~/.local/bin
ln -s "$(pwd)/claude-tokens.py" ~/.local/bin/claude-tokens
```

Using a symlink means `git pull` updates the command in place.

### Option 2 — download just the script

```bash
mkdir -p ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/leonelRos/Claude-token-tool/main/claude-tokens.py -o ~/.local/bin/claude-tokens
chmod +x ~/.local/bin/claude-tokens
```

### Make sure `~/.local/bin` is on your PATH

If `claude-tokens` isn't found after installing, add this line to your shell config (`~/.zshrc` on macOS, `~/.bashrc` on most Linux), then restart your terminal:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

> Prefer a system-wide install? Symlink into `/usr/local/bin` instead (may require `sudo`):
> `sudo ln -s "$(pwd)/claude-tokens.py" /usr/local/bin/claude-tokens`

### Verify

```bash
claude-tokens --help
```

## Usage

```bash
claude-tokens                          # dashboard: all-time + recent + today + current session
claude-tokens --window today           # zoom into one window
claude-tokens --window recent --recent-days 14
claude-tokens --by prompt --top 10     # priciest prompts
claude-tokens --by session
claude-tokens --by project
claude-tokens --since 2026-06-01 --until 2026-06-18
claude-tokens --project my-app --model opus
claude-tokens --json                   # machine-readable output
claude-tokens --no-cost                # hide cost columns
```

## Options

| Flag | Description |
| --- | --- |
| `--window {dashboard,all,today,recent,current}` | Time window (default `dashboard`, which shows all four). |
| `--recent-days N` | Size of the `recent` window in days (default `7`). |
| `--by {model,session,project,prompt}` | Alternate grouping; overrides the dashboard layout. |
| `--since DATE`, `--until DATE` | ISO date/datetime bounds, interpreted as UTC. |
| `--project SUBSTR` | Filter by project directory substring. |
| `--session SUBSTR` | Filter by session UUID substring. |
| `--model SUBSTR` | Filter by model name substring (e.g. `opus`, `sonnet`, `haiku`). |
| `--top N` | Limit the number of rows in tables. |
| `--no-cost` | Hide the USD cost columns. |
| `--json` | Emit JSON instead of formatted tables. |

## A note on cost estimates

Costs are **estimates**. Prices are hard-coded in the `PRICING` table near the top of `claude-tokens.py` (per-million-token rates for input, output, cache writes, and cache reads). They reflect public list prices at the time of writing — adjust the table if rates change or if your plan differs. "Today" and other windows use **UTC** day boundaries.

## License

MIT — see [LICENSE](LICENSE).
