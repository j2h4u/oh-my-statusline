# OMCC Statusline

Slot-based statusline for Claude Code — limits, git, PR dots, pace indicator.

## Preview

```
my-project/ ⋮ ⑂feat/auth*+ ⋮ 5h ▁ 7d ▃ ctx ▂ ⋮ chill 1%
```
```
my-project/ ⋮ ⑂feat/auth*+ · CI · ⁕⁕⁕⁕ 💬3 ⋮ 5h ▂ 7d ▁ ctx ▂ ⋮ based 28%
```

## Vibe Pacing

> Only relevant if you regularly burn through your weekly API limits. If you do, this saves you from constantly running `/usage` and mentally doing the math of "I used X% in Y days, am I on track?"

The `vibes` block tells you how your API spending is going relative to your 5-day budget window — without making you do math.

During the week you get a **pace label** and a **delta** (how far ahead or behind the expected burn rate you are):

| Label | What it means |
|-------|---------------|
| `depresso` | Way over pace — burning fast |
| `salty` | Slightly over pace |
| `chill` | Right on track, comfortable margin |
| `hyped` | Running a bit under pace |
| `based` | Barely spending anything |

Next to the label you get a signed percentage like `+18%` or `-5%` — positive means you're ahead of pace (good), negative means you're burning faster than expected (watch out). After the first half-day, a **surplus** indicator also appears: `+2.3d` means "at this rate, your budget stretches 2.3 extra days beyond the 5-day window." Negative? You're on borrowed time.

Once the 5 work days have elapsed and you're in weekend territory, pace metrics stop making sense — so the statusline switches to **weekend mode**: the text `no pace police` appears in a slowly cycling rainbow gradient. No judgment, just vibes.

## Installation

```bash
python3 ~/.claude/plugins/marketplaces/oh-my-claude-plugins/meta/utils/statusline/omcc-statusline.py --install
```

Test: `python3 omcc-statusline.py --demo`

## What You See

- **Directory** — current path
- **Git** — branch, dirty/staged/untracked indicators, CI status, PR dots, notifications
- **Limits** — 5h / 7d / context usage bars with color ramps
- **Vibe Pace** — are you burning your 7-day budget too fast or staying on track?

Pace labels: **based** (way under) → **hyped** → **chill** (on track) → **salty** → **depresso** (way over). Hidden at the start of a new window.

## Configuration

Copy `config.example.json` to `~/.config/omcc-statusline/config.json` and edit. Without config — defaults work out of the box.

External commands that aren't installed show a dim placeholder (e.g. `[ccusage: not found]`).

### Slot fields

| Field | Type | Description |
|-------|------|-------------|
| `provider` | string | Built-in provider: `path`, `git`, `limits`, `vibes` |
| `command` | string | Shell command — receives Claude's JSON via stdin, stdout shown in statusline |
| `enabled` | bool \| list | `false` — skip slot entirely. List of section names — show only those sections (providers only). Default: `true` (show all) |
| `ttl` | number | Cache TTL in seconds for external commands. Default: 60 |
| `cwd_sensitive` | bool | Cache external command output per working directory instead of globally. Use for commands that read project-local state. Default: `false` |

Provider sections for `enabled` filter:

| Provider | Sections |
|----------|---------|
| `git` | `branch`, `dirty`, `staged`, `untracked`, `ahead`, `behind`, `ci`, `prs`, `notif` |
| `limits` | `5h`, `7d`, `ctx` |
| `vibes` | `pace` |

## Theme Editor

```bash
python3 omcc-statusline.py --theme
```

Navigate elements, tweak colors, adjust separators and ramp styles — all with live preview.

## Troubleshooting

- **Not showing** — run `--install`, restart Claude Code
- **Limits empty** — fetched in background, appears on next render
- **PR dots missing** — install and auth `gh` CLI

## How Anthropic Weekly Limits Actually Work

> This isn't documented by Anthropic, but confirmed through observation and community reports.

The 7-day usage limit is a **rolling window anchored to your first usage after the previous window expires** — not a fixed calendar week.

**How it works:**

1. You activate your subscription (or your previous window expires)
2. You send your first message — this starts the 7-day clock
3. `resets_at` is set to exactly 7 days from that first message
4. When the window expires, usage drops to 0 — but the new window **doesn't start until you send another message**

**The drift problem:** If your window expires at 4:00 AM but you don't use Claude until 1:00 PM, the new window starts at 1:00 PM. Next week, your reset moves to 1:00 PM. Every week it can only **drift forward**, never backward. There is no known way to shift it back short of not using Claude for an entire week.

**Practical advice:**

- **Activate your subscription on Monday morning.** If you work at a pace that burns through limits, they'll naturally run out around Friday evening — giving you weekends to rest without "wasting" paid time
- If your window just expired and you want to keep the early reset time — **send a message as soon as possible** after reset, even a small one, to anchor the new window early
- Avoid the trap of activating mid-week: you'll end up working weekends (because limits are still available) and sitting idle early in the week (waiting for reset)
- The vibe pacing feature in this statusline assumes a **5-day work budget** within the 7-day window, specifically to help you pace through Mon–Fri and coast into the weekend

**Experiment: auto-anchoring with `/loop`**

> Status: untested, planned for next window reset

The idea: if you know your window resets at, say, 4:00 AM Monday — start a Claude Code session on Sunday evening and run `/loop 1m /usage`. This fires a lightweight prompt every minute. When the old window expires and the new one starts, the first prompt that hits the API after reset should anchor the new window to ~4:00 AM again — preventing forward drift even if you personally wake up at 9 AM.

**Open question:** Does `/loop` execute deterministically without consuming model tokens, or does each iteration count as API usage? If it burns tokens, this won't work when limits are exhausted. Needs testing.

**Fallback idea:** Create a Claude Code custom agent (`.md` file with `model: claude-haiku-4-5-20251001` in frontmatter) as a heartbeat probe. Haiku consumes far less of the subscription quota than Opus or Sonnet — a single-token ping should be negligible. Run it via `/loop` to register usage events and anchor the window at the right time.
