# AGENTS.md Signal

Find the teams that are hand-maintaining shared context for their AI agents.

`AGENTS.md` and `CLAUDE.md` have become the standard place where engineering teams tell their coding agents how the codebase works: conventions, commands, rules, who owns what. A large file on its own doesn't mean much. Tools generate them in seconds.

What *does* mean something is when **several different people keep editing the same file**. That's a team negotiating shared context by hand, every week, in a markdown file. It's exactly the problem that team-wide context layers like [SageOx](https://sageox.ai) exist to solve, which makes it one of the clearest public buying signals for that category.

This tool finds those teams.

## How it works

1. **Discover.** Searches GitHub code for `AGENTS.md` and `CLAUDE.md`, sliced into size ranges so it can see past GitHub's 1,000-results-per-query cap.
2. **Filter.** Keeps repos owned by organizations, skips forks, archived repos and personal projects, and picks the root-most context file in each repo.
3. **Measure co-editing.** Reads each file's commit history and counts distinct human authors, overall and in the last 90 days. Bot commits (Dependabot, Renovate, GitHub Actions) and agent-authored commits are excluded.
4. **Score.** 0 to 100, weighted heavily toward multiple people editing recently, then maintenance activity, file depth and recency. Collapses to one row per organization.
5. **Qualify with Claude (optional).** For the strongest targets, reads the file and org metadata and returns a segment (product company, devtools, open source, agency…), what the org builds, what in the file shows team coordination, and a specific opening line for outreach.

| Tier | Meaning |
|---|---|
| **A** | 3+ people edited the file in the last 90 days |
| **B** | 2 people recently, or 3+ over its history |
| **C** | Multi-author, but quieter |

### A note on prompt injection

`AGENTS.md` files are, by definition, instructions written for AI agents. Feeding them straight into a model is asking for trouble, so the excerpt is passed as quoted data inside tags, and the model is told never to follow anything inside it.

## Run it

Python 3.10+, standard library only. No installs.

```bash
gh auth login                          # code search requires a GitHub token
export ANTHROPIC_API_KEY=sk-ant-...    # optional, enables the Claude step

python3 -m agentsignal                 # defaults: AGENTS.md + CLAUDE.md, files ≥ 4 KB, 90-day window
```

Useful options:

```bash
python3 -m agentsignal --max-repos 200 --enrich-top 25   # quicker first pass
python3 -m agentsignal --filenames AGENTS.md --min-size 8000
python3 -m agentsignal --since-days 30 --min-authors 3
```

Outputs land in `out/`:

- `report.md`: funnel, tiers and the top 25 organizations
- `targets.csv` / `targets.json`: the full ranked list

A full default run takes roughly 30 to 60 minutes, mostly waiting on GitHub's code-search limit (about 10 searches a minute). Every response is cached in `.cache/`, so re-runs and tweaks to scoring are fast.

## Privacy

The tool reports at the **organization** level only. It counts distinct editors but never writes individual names, usernames or emails to its outputs. Everything it reads is public GitHub data, and `out/` is git-ignored so target lists stay private.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite runs the whole pipeline offline against a fake GitHub and a fake Claude, including bot filtering, rate-limit retries, caching and the scoring rule that a small file edited by a team outranks a huge file edited by one person.

## Why I built this

I've spent the last year building GTM systems on top of Claude Code and MCP, and the moment that keeps repeating is opening a fresh session and re-explaining decisions the team already made. Watching how teams cope with that in public (by growing a shared markdown file and taking turns editing it) felt like the most honest signal of who's ready for a better answer. This is v1 of turning that observation into an outbound list.

## License

MIT
