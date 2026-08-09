# Skills, Commands, Plugins & Agent Config Audit

**Generated:** 2026-08-09 14:01 
**Host:** macOS — `/Users/ruttanshbhatelia`
**Mode:** Read-only inventory (no files modified or deleted)

## Executive summary

- **480** total artifacts catalogued across Cursor, Claude Code, Codex, shared agents, and side_projects repos.
- **353** skill instances (**248** unique skill IDs by directory name).
- **57** plugin cache entries (**10** named Cursor plugins + **10** numeric cache aliases; **37** Claude plugin cache entries).
- **31** commands, **24** agent defs, **13** rules/instruction files, **2** configs.
- **68** exact-content duplicate groups covering **150** skill/command copies.
- **13** same-name skills with divergent content (excluding Vercel nested `upstream` stubs).
- Estimated recoverable skill-token inventory from exact duplicate copies: **~209,006 tokens** across **82** redundant copies (does not mean all are loaded into every prompt).

### Missing expected paths

| Path | Status |
|------|--------|
| `~/.cursor/skills/` | Does not exist — Cursor skills live in ~/.cursor/skills-cursor/ |
| `~/.cursor/rules/` | Does not exist — no user-level Cursor rules dir |
| `~/.claude/skills/` | Does not exist — Claude skills come from plugins cache |
| `~/.agents/commands/` | Does not exist |
| `~/Documents/*/skills/` | No matches |

### Important layout notes

- Cursor user skills live in `~/.cursor/skills-cursor/` (not `~/.cursor/skills/`).
- Cursor plugins are cached under `~/.cursor/plugins/cache/cursor-public/` with **both numeric IDs and human names** pointing at the same content hashes (double-counted).
- Claude skills primarily come from `~/.claude/plugins/cache/` (no top-level `~/.claude/skills/`).
- Shared agent skills in `~/.agents/skills/` are thin wrappers around Claude slash commands (`source-command-*`).
- `~/Library/Application Support/Cursor/` holds app runtime data; no user skill library there (only `User/settings.json` noted).

## Summary stats by harness

| Harness / source | Skills | Plugins | Commands | Agents | Rules | Configs |
|-----------------|-------:|--------:|---------:|-------:|------:|--------:|
| agents | 4 | 0 | 0 | 0 | 0 | 0 |
| claude | 0 | 0 | 4 | 0 | 1 | 0 |
| claude-plugin | 161 | 37 | 27 | 23 | 0 | 0 |
| codex | 10 | 0 | 0 | 0 | 1 | 1 |
| cursor | 20 | 0 | 0 | 0 | 0 | 0 |
| cursor-app | 0 | 0 | 0 | 0 | 0 | 1 |
| cursor-plugin | 126 | 20 | 0 | 0 | 0 | 0 |
| global | 0 | 0 | 0 | 0 | 1 | 0 |
| repo | 1 | 0 | 0 | 1 | 10 | 0 |
| repo-agents | 14 | 0 | 0 | 0 | 0 | 0 |
| repo-claude | 5 | 0 | 0 | 0 | 0 | 0 |
| repo-skills | 12 | 0 | 0 | 0 | 0 | 0 |
| **TOTAL** | **353** | **57** | **31** | **24** | **13** | **2** |

### Size / token footprint (skills)

- Combined skill directory size: **12.3 MB**
- Combined primary `SKILL.md` estimated tokens: **~846,667** (chars/4)
- Skills ≥ 4k tokens: **70**

## Claude installed plugins (from installed_plugins.json)

**20** plugins registered as installed (scope=user).

| Plugin | Version | Installed | Updated | Install path |
|--------|---------|-----------|---------|--------------|
| `agent-sdk-dev@claude-plugins-official` | unknown | 2026-03-13 | 2026-07-23 | `~/.claude/plugins/cache/claude-plugins-official/agent-sdk-dev/unknown` |
| `chrome-devtools-mcp@claude-plugins-official` | 1.6.0 | 2026-03-13 | 2026-07-23 | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0` |
| `claude-code-setup@claude-plugins-official` | 1.0.0 | 2026-03-13 | 2026-03-13 | `~/.claude/plugins/cache/claude-plugins-official/claude-code-setup/1.0.0` |
| `claude-md-management@claude-plugins-official` | 1.0.0 | 2026-03-13 | 2026-03-13 | `~/.claude/plugins/cache/claude-plugins-official/claude-md-management/1.0.0` |
| `code-review@claude-plugins-official` | unknown | 2026-03-13 | 2026-07-23 | `~/.claude/plugins/cache/claude-plugins-official/code-review/unknown` |
| `code-simplifier@claude-plugins-official` | 1.0.0 | 2026-03-13 | 2026-03-13 | `~/.claude/plugins/cache/claude-plugins-official/code-simplifier/1.0.0` |
| `commit-commands@claude-plugins-official` | unknown | 2026-03-13 | 2026-07-23 | `~/.claude/plugins/cache/claude-plugins-official/commit-commands/unknown` |
| `discord@claude-plugins-official` | 0.0.4 | 2026-04-12 | 2026-04-12 | `~/.claude/plugins/cache/claude-plugins-official/discord/0.0.4` |
| `explanatory-output-style@claude-plugins-official` | 1.0.0 | 2026-03-13 | 2026-04-11 | `~/.claude/plugins/cache/claude-plugins-official/explanatory-output-style/1.0.0` |
| `feature-dev@claude-plugins-official` | unknown | 2026-03-13 | 2026-07-23 | `~/.claude/plugins/cache/claude-plugins-official/feature-dev/unknown` |
| `firecrawl@claude-plugins-official` | 1.0.9 | 2026-03-13 | 2026-06-08 | `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9` |
| `frontend-design@claude-plugins-official` | unknown | 2026-03-13 | 2026-07-23 | `~/.claude/plugins/cache/claude-plugins-official/frontend-design/unknown` |
| `hookify@claude-plugins-official` | unknown | 2026-03-13 | 2026-07-23 | `~/.claude/plugins/cache/claude-plugins-official/hookify/unknown` |
| `playground@claude-plugins-official` | unknown | 2026-03-13 | 2026-07-23 | `~/.claude/plugins/cache/claude-plugins-official/playground/unknown` |
| `playwright@claude-plugins-official` | unknown | 2026-03-13 | 2026-07-23 | `~/.claude/plugins/cache/claude-plugins-official/playwright/unknown` |
| `pr-review-toolkit@claude-plugins-official` | unknown | 2026-03-13 | 2026-07-23 | `~/.claude/plugins/cache/claude-plugins-official/pr-review-toolkit/unknown` |
| `security-guidance@claude-plugins-official` | 2.0.6 | 2026-03-13 | 2026-06-12 | `~/.claude/plugins/cache/claude-plugins-official/security-guidance/2.0.6` |
| `skill-creator@claude-plugins-official` | unknown | 2026-03-13 | 2026-07-23 | `~/.claude/plugins/cache/claude-plugins-official/skill-creator/unknown` |
| `superpowers@claude-plugins-official` | 6.1.1 | 2026-03-13 | 2026-07-23 | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1` |
| `vercel@claude-plugins-official` | 0.45.1 | 2026-03-13 | 2026-07-23 | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1` |

### Cache entries not clearly mapped to installed list (17)

_These appear under `~/.claude/plugins/cache` but may be unused, staging, or marketplace mirrors._

| Cache entry | Size | Modified | Note |
|-------------|------|----------|------|
| `claude-plugins-official/adspirer-ads-agent` | 42.3KB | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/amazon-location-service` | 121.6KB | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/aws-serverless` | 740.9KB | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/context7` | 937B | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/data` | 2.2MB | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/deploy-on-aws` | 505.8KB | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/figma` | 1.7MB | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/gopls-lsp` | 12.0KB | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/learning-output-style` | 19.8KB | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/mintlify` | 40.9KB | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/plugin-dev` | 537.6KB | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/pyright-lsp` | 12.2KB | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/ralph-loop` | 36.6KB | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/rust-analyzer-lsp` | 12.2KB | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/semgrep` | 79.3MB | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/swift-lsp` | 12.1KB | 2026-07-24 | present in cache, not in installed_plugins.json |
| `claude-plugins-official/typescript-lsp` | 14.6KB | 2026-07-24 | present in cache, not in installed_plugins.json |
## Cursor plugins (named + numeric aliases)

| Name | Skills | Size | Modified | Path |
|------|-------:|------|----------|------|
| `2050` | 3 | 194.9KB | 2026-07-23 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/2050/e351cde6137a98b943c8fb8d0c698c1b438dfc0f` |
| `25808295` | 1 | 103.4KB | 2026-07-22 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/25808295/a22550306ff6b704649a8f09faf393e007cbcc1e` |
| `26098676` | 7 | 325.1KB | 2026-07-20 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e` |
| `684` | 14 | 1.9MB | 2026-07-24 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99` |
| `6948` | 0 | 0B | 2026-07-26 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/6948/10f1717a3e2a3c16cfbd43877c1e44063d9d749a` |
| `7194` | 8 | 237.3KB | 2026-07-20 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760` |
| `735` | 19 | 1.9MB | 2026-07-20 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd` |
| `738` | 2 | 191.7KB | 2026-07-25 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/738/8e6c2d02accefc0dad3b7d3be3751f7fcc210885` |
| `789` | 1 | 62.3KB | 2026-07-22 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/789/80ce444eb020b5f41b34836c553f162d6113cd6f` |
| `9345` | 3 | 38.9KB | 2026-07-20 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/9345/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf` |
| `aikido-cursor-plugin` | 3 | 42.0KB | 2026-07-02 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/aikido-cursor-plugin/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf` |
| `aws-agents` | 7 | 325.1KB | 2026-07-02 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e` |
| `firecrawl` | 10 | 136.0KB | 2026-08-05 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c` |
| `gsap-skills` | 8 | 243.6KB | 2026-07-02 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760` |
| `huggingface-skills` | 19 | 1.9MB | 2026-07-02 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd` |
| `langfuse` | 2 | 195.0KB | 2026-07-02 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/langfuse/8e6c2d02accefc0dad3b7d3be3751f7fcc210885` |
| `mintlify-cursor-plugin` | 1 | 106.6KB | 2026-07-02 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/mintlify-cursor-plugin/a22550306ff6b704649a8f09faf393e007cbcc1e` |
| `postman` | 3 | 245.2KB | 2026-08-05 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/postman/f5ea7c56da1dc022753c66ac4fba398e881b07dd` |
| `shadcn` | 1 | 96.7MB | 2026-07-02 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/shadcn/10f1717a3e2a3c16cfbd43877c1e44063d9d749a` |
| `superpowers` | 14 | 1.9MB | 2026-07-16 | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99` |

## Claude plugin cache entries

| Marketplace/Plugin | Skills | Size | Modified | Path |
|--------------------|-------:|------|----------|------|
| `claude-plugins-official/adspirer-ads-agent` | 1 | 42.3KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/adspirer-ads-agent/1.1.0` |
| `claude-plugins-official/agent-sdk-dev` | 0 | 36.2KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/agent-sdk-dev/unknown` |
| `claude-plugins-official/amazon-location-service` | 1 | 121.6KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/amazon-location-service/1.0.0` |
| `claude-plugins-official/aws-serverless` | 7 | 740.9KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/aws-serverless/1.3.0` |
| `claude-plugins-official/chrome-devtools-mcp` | 24 | 329.2MB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0` |
| `claude-plugins-official/claude-code-setup` | 1 | 586.7KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/claude-code-setup/1.0.0` |
| `claude-plugins-official/claude-md-management` | 1 | 1.1MB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/claude-md-management/1.0.0` |
| `claude-plugins-official/code-review` | 0 | 26.2KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/code-review/unknown` |
| `claude-plugins-official/code-simplifier` | 0 | 15.0KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/code-simplifier/1.0.0` |
| `claude-plugins-official/commit-commands` | 0 | 20.8KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/commit-commands/unknown` |
| `claude-plugins-official/context7` | 0 | 937B | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/context7/unknown` |
| `claude-plugins-official/data` | 22 | 2.2MB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/data/0.1.0` |
| `claude-plugins-official/deploy-on-aws` | 3 | 505.8KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/deploy-on-aws/1.3.0` |
| `claude-plugins-official/discord` | 2 | 30.8MB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/discord/0.0.4` |
| `claude-plugins-official/explanatory-output-style` | 0 | 16.2KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/explanatory-output-style/1.0.0` |
| `claude-plugins-official/feature-dev` | 0 | 35.4KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/feature-dev/unknown` |
| `claude-plugins-official/figma` | 14 | 1.7MB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/figma/2.2.81` |
| `claude-plugins-official/firecrawl` | 10 | 74.2KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9` |
| `claude-plugins-official/frontend-design` | 1 | 16.9KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/frontend-design/unknown` |
| `claude-plugins-official/gopls-lsp` | 0 | 12.0KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/gopls-lsp/1.0.0` |
| `claude-plugins-official/hookify` | 1 | 102.9KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/hookify/unknown` |
| `claude-plugins-official/learning-output-style` | 0 | 19.8KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/learning-output-style/1.0.0` |
| `claude-plugins-official/mintlify` | 1 | 40.9KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/mintlify/acd6d2e0128c` |
| `claude-plugins-official/playground` | 1 | 42.6KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/playground/unknown` |
| `claude-plugins-official/playwright` | 0 | 944B | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/playwright/unknown` |
| `claude-plugins-official/plugin-dev` | 7 | 537.6KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/plugin-dev/unknown` |
| `claude-plugins-official/pr-review-toolkit` | 0 | 56.6KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/pr-review-toolkit/unknown` |
| `claude-plugins-official/pyright-lsp` | 0 | 12.2KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/pyright-lsp/1.0.0` |
| `claude-plugins-official/ralph-loop` | 0 | 36.6KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/ralph-loop/1.0.0` |
| `claude-plugins-official/rust-analyzer-lsp` | 0 | 12.2KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/rust-analyzer-lsp/1.0.0` |
| `claude-plugins-official/security-guidance` | 0 | 645.8KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/security-guidance/2.0.6` |
| `claude-plugins-official/semgrep` | 1 | 79.3MB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/semgrep/2.1.4` |
| `claude-plugins-official/skill-creator` | 1 | 231.0KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/skill-creator/unknown` |
| `claude-plugins-official/superpowers` | 14 | 1.3MB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1` |
| `claude-plugins-official/swift-lsp` | 0 | 12.1KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/swift-lsp/1.0.0` |
| `claude-plugins-official/typescript-lsp` | 0 | 14.6KB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/typescript-lsp/1.0.0` |
| `claude-plugins-official/vercel` | 48 | 157.6MB | 2026-07-24 | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1` |

## Full inventory: Skills

Hash column is first 16 hex chars of SHA-256 of primary `SKILL.md` (or skill dir content).

| Name (id) | Source | Hash | Est. tokens | Size | Modified | Description | Location |
|-----------|--------|------|------------:|------|----------|-------------|----------|
| `source-command-continual-learning` | agents | `df2bf0f5707d4585` | 1640 | 6.4KB | 2026-05-02 | Run the migrated source command `continual-learning`. | `~/.agents/skills/source-command-continual-learning` |
| `source-command-dream` | agents | `92327b4a48a85e09` | 1457 | 5.7KB | 2026-05-02 | Run the migrated source command `dream`. | `~/.agents/skills/source-command-dream` |
| `source-command-learn` | agents | `de862c65a47fd122` | 1231 | 4.8KB | 2026-05-02 | Run the migrated source command `learn`. | `~/.agents/skills/source-command-learn` |
| `source-command-recap` | agents | `f95b441d144fccfe` | 523 | 2.1KB | 2026-05-02 | Run the migrated source command `recap`. | `~/.agents/skills/source-command-recap` |
| `a11y-debugging` | claude-plugin | `f2ee15c98641fd10` | 1408 | 8.2KB | 2026-07-24 | Uses Chrome DevTools MCP for accessibility (a11y) debugging and auditing based o | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/skills/a11y-debugging` |
| `access` | claude-plugin | `4fc3da872e033c37` | 1085 | 4.3KB | 2026-04-12 | Manage Discord channel access — approve pairings, edit allowlists, set DM/group  | `~/.claude/plugins/cache/claude-plugins-official/discord/0.0.4/skills/access` |
| `ad-campaign-best-practices` | claude-plugin | `612e6391b1ae4457` | 704 | 2.8KB | 2026-03-14 | Best practices for creating and managing ad campaigns across Google Ads, Meta Ad | `~/.claude/plugins/cache/claude-plugins-official/adspirer-ads-agent/1.1.0/skills/ad-campaign-best-practices` |
| `agent-development` | claude-plugin | `6a2826571320828c` | 2777 | 67.7KB | 2026-07-24 | This skill should be used when the user asks to "create an agent", "add an agent | `~/.claude/plugins/cache/claude-plugins-official/plugin-dev/unknown/skills/agent-development` |
| `ai-gateway` | claude-plugin | `7bfdfaa64d0636a3` | 5903 | 23.2KB | 2026-07-24 | Vercel AI Gateway expert guidance. Use when configuring model routing, provider  | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/ai-gateway` |
| `ai-sdk` | claude-plugin | `255c0f4bd83a3a60` | 4937 | 77.9KB | 2026-07-24 | Vercel AI SDK expert guidance. Use when building AI-powered features — chat inte | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/ai-sdk` |
| `airflow` | claude-plugin | `20e49b81a5617fe5` | 2986 | 17.8KB | 2026-03-14 | Manages Apache Airflow operations including listing, testing, running, and debug | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/airflow` |
| `airflow-adapter` | claude-plugin | `ae70e850019de4f5` | 227 | 4.6KB | 2026-03-14 | Airflow adapter pattern for v2/v3 API compatibility. Use when working with adapt | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/astro-airflow-mcp/.claude/skills/airflow-adapter` |
| `airflow-hitl` | claude-plugin | `c6d69c800465f937` | 2080 | 8.1KB | 2026-03-14 | Use when the user needs human-in-the-loop workflows in Airflow (approval/reject, | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/airflow-hitl` |
| `amazon-location-service` | claude-plugin | `b988ad4344d49e87` | 3198 | 120.9KB | 2026-03-14 | Integrates Amazon Location Service APIs for AWS applications. Use this skill whe | `~/.claude/plugins/cache/claude-plugins-official/amazon-location-service/1.0.0/skills/amazon-location-service` |
| `analyzing-data` | claude-plugin | `0cfcbc4b43b10540` | 1014 | 223.7KB | 2026-03-14 | Queries data warehouse and answers business questions about data. Handles questi | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/analyzing-data` |
| `annotating-task-lineage` | claude-plugin | `ff51650a384b6e22` | 2843 | 11.1KB | 2026-03-14 | Annotate Airflow tasks with data lineage using inlets and outlets. Use when the  | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/annotating-task-lineage` |
| `api-gateway` | claude-plugin | `c0971ab0e5e6fee0` | 4941 | 234.2KB | 2026-07-24 | Build, manage, and operate APIs with Amazon API Gateway (REST, HTTP, and WebSock | `~/.claude/plugins/cache/claude-plugins-official/aws-serverless/1.3.0/skills/api-gateway` |
| `auth` | claude-plugin | `b6b9c4b0c9c214f7` | 2835 | 11.1KB | 2026-07-24 | Authentication integration guidance — Clerk (native Vercel Marketplace), Descope | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/auth` |
| `authoring-dags` | claude-plugin | `5c10e7f767f2d4bc` | 1737 | 17.4KB | 2026-03-14 | Workflow and best practices for writing Apache Airflow DAGs. Use when the user w | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/authoring-dags` |
| `aws-architecture-diagram` | claude-plugin | `14a7804261946537` | 3815 | 362.1KB | 2026-06-19 | Generate validated AWS architecture diagrams as draw.io XML using official AWS4  | `~/.claude/plugins/cache/claude-plugins-official/deploy-on-aws/1.3.0/skills/aws-architecture-diagram` |
| `aws-lambda` | claude-plugin | `26257c68ac930cce` | 3099 | 125.6KB | 2026-07-24 | Design, build, deploy, test, and debug serverless applications with AWS Lambda.  | `~/.claude/plugins/cache/claude-plugins-official/aws-serverless/1.3.0/skills/aws-lambda` |
| `aws-lambda-durable-functions` | claude-plugin | `30b5352bc7caad79` | 2297 | 119.2KB | 2026-07-24 | Build resilient, long-running, multi-step applications with AWS Lambda durable f | `~/.claude/plugins/cache/claude-plugins-official/aws-serverless/1.3.0/skills/aws-lambda-durable-functions` |
| `aws-lambda-managed-instances` | claude-plugin | `d512c801d15ade0e` | 3989 | 49.0KB | 2026-07-24 | Evaluate, configure, and migrate workloads to AWS Lambda Managed Instances (LMI) | `~/.claude/plugins/cache/claude-plugins-official/aws-serverless/1.3.0/skills/aws-lambda-managed-instances` |
| `aws-lambda-microvms` | claude-plugin | `8b90386e379e877f` | 4075 | 71.7KB | 2026-07-24 | Build, run, debug, and operate applications on AWS Lambda MicroVMs — Firecracker | `~/.claude/plugins/cache/claude-plugins-official/aws-serverless/1.3.0/skills/aws-lambda-microvms` |
| `aws-serverless-deployment` | claude-plugin | `652f390acd47fd30` | 1336 | 40.1KB | 2026-07-24 | AWS SAM and AWS CDK deployment for serverless applications. Triggers on phrases  | `~/.claude/plugins/cache/claude-plugins-official/aws-serverless/1.3.0/skills/aws-serverless-deployment` |
| `aws-step-functions` | claude-plugin | `179fc933ff2b04c7` | 1896 | 97.1KB | 2026-07-24 | Build workflows with AWS Step Functions state machines using the JSONata query l | `~/.claude/plugins/cache/claude-plugins-official/aws-serverless/1.3.0/skills/aws-step-functions` |
| `benchmark-agents` | claude-plugin | `95fd9d33be1b5704` | 3612 | 25.9KB | 2026-07-24 | Advanced AI agent benchmark scenarios that push Vercel's cutting-edge platform f | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/.claude/skills/benchmark-agents` |
| `benchmark-e2e` | claude-plugin | `1f18ec33990b765e` | 1339 | 5.3KB | 2026-07-24 | End-to-end benchmark suite for vercel-plugin. Runs realistic projects through sk | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/.claude/skills/benchmark-e2e` |
| `benchmark-sandbox` | claude-plugin | `2588bbda779e1843` | 5405 | 188.7KB | 2026-07-24 | Run vercel-plugin eval scenarios in Vercel Sandboxes instead of local WezTerm pa | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/.claude/skills/benchmark-sandbox` |
| `benchmark-testing` | claude-plugin | `9d5ead7eed4f56a5` | 946 | 3.7KB | 2026-07-24 | Create and launch benchmark test projects to exercise vercel-plugin skill inject | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/.claude/skills/benchmark-testing` |
| `bootstrap` | claude-plugin | `ed2f91dbec2cb2be` | 2002 | 7.8KB | 2026-07-24 | Project bootstrapping orchestrator for repos that depend on Vercel-linked resour | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/bootstrap` |
| `brainstorming` | claude-plugin | `e14914605f640e08` | 2598 | 73.1KB | 2026-07-24 | You MUST use this before any creative work - creating features, building compone | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/brainstorming` |
| `cdn-caching` | claude-plugin | `5b5f2fa4057993d9` | 4831 | 19.0KB | 2026-07-24 | Debug Vercel CDN caching — cache hit rate, stale content, revalidation behavior, | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/cdn-caching` |
| `chat-sdk` | claude-plugin | `82d66d41af16dcc1` | 3242 | 26.3KB | 2026-07-24 | Vercel Chat SDK expert guidance. Use when building multi-platform chat bots — Sl | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/chat-sdk` |
| `checking-freshness` | claude-plugin | `48ac4e767dc683c8` | 800 | 3.1KB | 2026-03-14 | Quick data freshness check. Use when the user asks if data is up to date, when a | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/checking-freshness` |
| `chrome-devtools` | claude-plugin | `9878a3260c5d73ca` | 915 | 3.6KB | 2026-07-24 | Uses Chrome DevTools via MCP for efficient debugging, troubleshooting and browse | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/skills/chrome-devtools` |
| `chrome-devtools-cli` | claude-plugin | `7382e3581eebfce8` | 2072 | 9.0KB | 2026-07-24 | Use this skill to write shell scripts or run shell commands to automate tasks in | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/skills/chrome-devtools-cli` |
| `claude-automation-recommender` | claude-plugin | `bd8789052f89e784` | 2709 | 41.3KB | 2026-03-14 | Analyze a codebase and recommend Claude Code automations (hooks, subagents, skil | `~/.claude/plugins/cache/claude-plugins-official/claude-code-setup/1.0.0/skills/claude-automation-recommender` |
| `claude-md-improver` | claude-plugin | `b06c7420be08ca1c` | 1507 | 15.2KB | 2026-03-14 | Audit and improve CLAUDE.md files in repositories. Use when user asks to check,  | `~/.claude/plugins/cache/claude-plugins-official/claude-md-management/1.0.0/skills/claude-md-improver` |
| `command-development` | claude-plugin | `c55ad02cfe4cbb2a` | 4765 | 150.5KB | 2026-07-24 | This skill should be used when the user asks to "create a slash command", "add a | `~/.claude/plugins/cache/claude-plugins-official/plugin-dev/unknown/skills/command-development` |
| `configure` | claude-plugin | `9364d7895d6f38b9` | 1068 | 4.2KB | 2026-04-12 | Set up the Discord channel — save the bot token and review access policy. Use wh | `~/.claude/plugins/cache/claude-plugins-official/discord/0.0.4/skills/configure` |
| `cosmos-dbt-core` | claude-plugin | `25034c443fa33980` | 3327 | 25.9KB | 2026-03-14 | Use when turning a dbt Core project into an Airflow DAG/TaskGroup using Astronom | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/cosmos-dbt-core` |
| `cosmos-dbt-fusion` | claude-plugin | `6e4f7ff71bbe0090` | 1780 | 10.8KB | 2026-03-14 | Use when running a dbt Fusion project with Astronomer Cosmos. Covers Cosmos 1.11 | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/cosmos-dbt-fusion` |
| `creating-a-model` | claude-plugin | `ff325bc46688f68c` | 937 | 3.7KB | 2026-07-24 | --- | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/creating-a-model` |
| `creating-openlineage-extractors` | claude-plugin | `e0dff637da044d41` | 3289 | 12.9KB | 2026-03-14 | Create custom OpenLineage extractors for Airflow operators. Use when the user ne | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/creating-openlineage-extractors` |
| `debug-optimize-lcp` | claude-plugin | `3617cbbc64378652` | 1618 | 14.6KB | 2026-07-24 | Guides debugging and optimizing Largest Contentful Paint (LCP) using Chrome DevT | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/skills/debug-optimize-lcp` |
| `debugging-dags` | claude-plugin | `4aff25f0e67d4643` | 1052 | 4.1KB | 2026-03-14 | Comprehensive DAG failure diagnosis and root cause analysis. Use for complex deb | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/debugging-dags` |
| `deploy` | claude-plugin | `7fec2fa8cd5e4a82` | 664 | 22.5KB | 2026-06-19 | Deploy applications to AWS. Triggers on phrases like: deploy to AWS, host on AWS | `~/.claude/plugins/cache/claude-plugins-official/deploy-on-aws/1.3.0/skills/deploy` |
| `deploying-airflow` | claude-plugin | `dde1ffd810f670e2` | 2754 | 10.8KB | 2026-03-14 | Deploy Airflow DAGs and projects. Use when the user wants to deploy code, push D | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/deploying-airflow` |
| `deployments-cicd` | claude-plugin | `a0d6c92265015d02` | 2934 | 11.5KB | 2026-07-24 | Vercel deployment and CI/CD expert guidance. Use when deploying, promoting, roll | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/deployments-cicd` |
| `devtools-imports` | claude-plugin | `4f2ead126a8c48d8` | 460 | 1.8KB | 2026-07-24 | Conventions for importing code in Devtools to avoid build errors. Covers cross-m | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/devtools-imports` |
| `devtools-source-maps` | claude-plugin | `9efd75a2af564f61` | 1162 | 4.5KB | 2026-07-24 | Guidelines for utilizing source maps and structured stack traces in DevTools. Co | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/devtools-source-maps` |
| `devtools-ux-writing-refactor` | claude-plugin | `7683c4a6615c68e8` | 2457 | 9.6KB | 2026-07-24 | Refactor user-facing UIStrings and localization comments in a DevTools module fo | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/devtools-ux-writing-refactor` |
| `dispatching-parallel-agents` | claude-plugin | `f0df13f584049059` | 1654 | 6.5KB | 2026-07-24 | Use when facing 2+ independent tasks that can be worked on without shared state  | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/dispatching-parallel-agents` |
| `elastic-beanstalk` | claude-plugin | `bfdbdcd99da3df92` | 2345 | 17.7KB | 2026-06-19 | Deploy to AWS Elastic Beanstalk. Triggers on: elastic beanstalk, EB, managed EC2 | `~/.claude/plugins/cache/claude-plugins-official/deploy-on-aws/1.3.0/skills/elastic-beanstalk` |
| `env-vars` | claude-plugin | `ba0bdb4db224e04d` | 2409 | 9.4KB | 2026-07-24 | Vercel environment variable expert guidance. Use when working with .env files, v | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/env-vars` |
| `evaluate-ai-css-completion` | claude-plugin | `25e1ef71512f6903` | 1136 | 7.2KB | 2026-07-24 | Expose a temporary evaluation hook in DevTools and run a Puppeteer script to val | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/evaluate-ai-css-completion` |
| `eve` | claude-plugin | `c7871bdfd5924130` | 1571 | 12.5KB | 2026-07-24 | Build durable AI agents and agent-powered applications with the eve framework. U | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/eve` |
| `executing-plans` | claude-plugin | `bbd8d28bb655a528` | 647 | 2.5KB | 2026-07-24 | Use when you have a written implementation plan to execute in a separate session | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/executing-plans` |
| `figma-code-connect` | claude-plugin | `d69cfc0e2080351f` | 6370 | 57.7KB | 2026-07-24 | Creates and maintains Figma Code Connect template files that map Figma component | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-code-connect` |
| `figma-create-new-file` | claude-plugin | `82e0a018692d3d00` | 978 | 3.9KB | 2026-07-24 | **MANDATORY prerequisite** — you MUST invoke this skill BEFORE every `create_new | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-create-new-file` |
| `figma-design-to-code` | claude-plugin | `936bbe68b4731d4a` | 1236 | 4.9KB | 2026-07-24 | **MANDATORY prerequisite** — you MUST invoke this skill BEFORE calling the `get_ | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-design-to-code` |
| `figma-generate-design` | claude-plugin | `7d07e4f4f8fec701` | 8276 | 38.7KB | 2026-07-24 | Use this skill alongside figma-use when the task involves translating an applica | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-generate-design` |
| `figma-generate-diagram` | claude-plugin | `7297638dbf2130d1` | 2552 | 108.2KB | 2026-07-24 | MANDATORY prerequisite — load this skill BEFORE every `generate_diagram` tool ca | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-generate-diagram` |
| `figma-generate-library` | claude-plugin | `38d9381d4fb08923` | 5669 | 231.7KB | 2026-07-24 | Build or update a professional-grade design system in Figma from a codebase. Use | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-generate-library` |
| `figma-implement-motion` | claude-plugin | `2e26e67284d47c6a` | 5792 | 66.7KB | 2026-07-24 | Translates Figma motion and animations into production-ready application code. U | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-implement-motion` |
| `figma-swiftui` | claude-plugin | `868c4defe2c0854f` | 1000 | 59.0KB | 2026-07-24 | SwiftUI ↔ Figma translation. Use whenever the user mentions Swift, SwiftUI, iOS, | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-swiftui` |
| `figma-use` | claude-plugin | `6c0715d4947137d0` | 8519 | 686.8KB | 2026-07-24 | **MANDATORY prerequisite** — you MUST invoke this skill BEFORE every `use_figma` | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-use` |
| `figma-use-figjam` | claude-plugin | `16b15da304f777e6` | 1755 | 145.8KB | 2026-07-24 | This skill helps agents use Figma's use_figma MCP tool in the FigJam context. Ca | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-use-figjam` |
| `figma-use-motion` | claude-plugin | `d74fa8c9435a5260` | 1714 | 30.7KB | 2026-07-24 | Motion / animation context for the `use_figma` MCP tool — animating Figma nodes  | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-use-motion` |
| `figma-use-slides` | claude-plugin | `0a69a65e686dcc22` | 5473 | 67.9KB | 2026-07-24 | This skill helps agents use Figma's use_figma MCP tool in the Slides context. Ca | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-use-slides` |
| `finishing-a-development-branch` | claude-plugin | `e6d4a812de900d33` | 1703 | 6.7KB | 2026-07-24 | Use when implementation is complete, all tests pass, and you need to decide how  | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/finishing-a-development-branch` |
| `firecrawl-agent` | claude-plugin | `9a8badff132ebea1` | 686 | 2.7KB | 2026-06-08 | AI-powered autonomous data extraction that navigates complex sites and returns s | `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-agent` |
| `firecrawl-cli` | claude-plugin | `e413e43067ad20e9` | 4082 | 19.2KB | 2026-06-08 | Search, scrape, and interact with the web via the Firecrawl CLI. Use this skill  | `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-cli` |
| `firecrawl-crawl` | claude-plugin | `a5ddaae261c6c2c4` | 672 | 2.6KB | 2026-06-08 | Bulk extract content from an entire website or site section. Use this skill when | `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-crawl` |
| `firecrawl-download` | claude-plugin | `8c734be50e335cf8` | 774 | 3.0KB | 2026-06-08 | Download an entire website as local files — markdown, screenshots, or multiple f | `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-download` |
| `firecrawl-interact` | claude-plugin | `f6ddfc4b857a7c54` | 978 | 3.8KB | 2026-06-08 | Control and interact with a live browser session on any scraped page — click but | `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-interact` |
| `firecrawl-map` | claude-plugin | `55e6ea4076bdda1a` | 533 | 2.1KB | 2026-06-08 | Discover and list all URLs on a website, with optional search filtering. Use thi | `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-map` |
| `firecrawl-monitor` | claude-plugin | `87268056a806cbff` | 3312 | 13.0KB | 2026-06-08 | Detect when content on a website changes and get notified by webhook or email —  | `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-monitor` |
| `firecrawl-parse` | claude-plugin | `c6f694cab0dbfddc` | 678 | 2.7KB | 2026-06-08 | Efficiently extract and convert the contents of any local file—such as PDF, DOCX | `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-parse` |
| `firecrawl-scrape` | claude-plugin | `4fd52e6478dc8964` | 930 | 3.7KB | 2026-06-08 | Extract clean markdown from any URL, including JavaScript-rendered SPAs. Use thi | `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-scrape` |
| `firecrawl-search` | claude-plugin | `e946e2b4062da5df` | 1683 | 6.6KB | 2026-06-08 | Web search with full page content extraction. Use this skill whenever the user a | `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-search` |
| `fixing-skipped-tests` | claude-plugin | `f3b5a6029af6c188` | 531 | 2.1KB | 2026-07-24 | Use this skill when unskipping a test that was previously skipped. | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/fixing-skipped-tests` |
| `foundation-test-migration` | claude-plugin | `71dda7b3daca4afd` | 1721 | 6.7KB | 2026-07-24 | Migrating unit tests to foundation unit tests using TestUniverse and devtools_fo | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/foundation-test-migration` |
| `frontend-design` | claude-plugin | `d39adf3a983de7da` | 1068 | 4.2KB | 2026-04-11 | Create distinctive, production-grade frontend interfaces with high design qualit | `~/.claude/plugins/cache/claude-plugins-official/frontend-design/unknown/skills/frontend-design` |
| `generate-project-plan` | claude-plugin | `0cc7b780099b957d` | 6833 | 101.7KB | 2026-07-24 | Generate a FigJam project plan board from a PRD plus codebase context. Interacti | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/workflow-skills/generate-project-plan` |
| `hook-development` | claude-plugin | `f47e2d42f6360294` | 4055 | 62.8KB | 2026-07-24 | This skill should be used when the user asks to "create a hook", "add a PreToolU | `~/.claude/plugins/cache/claude-plugins-official/plugin-dev/unknown/skills/hook-development` |
| `install-mfw` | claude-plugin | `e5479a19f4456e62` | 2921 | 17.4KB | 2026-07-24 | Install the mfw (Semgrep Malware Firewall) CLI via the curl\|sh installer and wa | `~/.claude/plugins/cache/claude-plugins-official/semgrep/2.1.4/skills/install-mfw` |
| `knowledge-update` | claude-plugin | `a7f6ede9545dcb9b` | 1782 | 7.0KB | 2026-07-24 | Corrects outdated LLM knowledge about the Vercel platform and introduces new pro | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/knowledge-update` |
| `managing-astro-deployments` | claude-plugin | `2ce3c36d89bd12c4` | 1618 | 6.3KB | 2026-03-14 | Manage Astronomer production deployments with Astro CLI. Use when the user wants | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/managing-astro-deployments` |
| `managing-astro-local-env` | claude-plugin | `0f45d43729282b78` | 633 | 2.5KB | 2026-03-14 | Manage local Airflow environment with Astro CLI. Use when the user wants to star | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/managing-astro-local-env` |
| `marketplace` | claude-plugin | `e36d17e4a8cfbfd4` | 1821 | 7.2KB | 2026-07-24 | Vercel Marketplace expert guidance — discovering, installing, and managing third | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/marketplace` |
| `mcp-integration` | claude-plugin | `2bcc3b5e93924d76` | 3118 | 45.5KB | 2026-07-24 | This skill should be used when the user asks to "add MCP server", "integrate MCP | `~/.claude/plugins/cache/claude-plugins-official/plugin-dev/unknown/skills/mcp-integration` |
| `memory-leak-debugging` | claude-plugin | `d0c5a08e3189ae28` | 982 | 9.7KB | 2026-07-24 | Diagnoses and resolves memory leaks in JavaScript/Node.js applications. Use when | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/skills/memory-leak-debugging` |
| `merging-devtools-module` | claude-plugin | `606c8af6cbe380a1` | 1322 | 5.2KB | 2026-07-24 | Workflow for merging a DevTools submodule into its parent module. Covers BUILD.g | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/merging-devtools-module` |
| `microfrontends` | claude-plugin | `2b846d0e87511791` | 1257 | 43.3KB | 2026-07-24 | Guide for building, configuring, and deploying microfrontends on Vercel. Use thi | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/microfrontends` |
| `migrate-chromium-test` | claude-plugin | `fc79941e00218264` | 1505 | 5.9KB | 2026-07-24 | Use when migrating Chromium layout tests to DevTools unit tests | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/migrate-chromium-test` |
| `migrating-airflow-2-to-3` | claude-plugin | `a4a43e8ec944427c` | 2290 | 26.5KB | 2026-03-14 | Guide for migrating Apache Airflow 2.x projects to Airflow 3.x. Use when the use | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/migrating-airflow-2-to-3` |
| `mintlify` | claude-plugin | `d0a01483e35bd596` | 2159 | 37.7KB | 2026-06-08 | Comprehensive reference for building Mintlify documentation sites. Use when crea | `~/.claude/plugins/cache/claude-plugins-official/mintlify/acd6d2e0128c/skills/mintlify` |
| `next-cache-components` | claude-plugin | `4c296aa985079c91` | 2927 | 23.0KB | 2026-07-24 | Next.js 16 Cache Components guidance — PPR, use cache directive, cacheLife, cach | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/next-cache-components` |
| `next-forge` | claude-plugin | `97fcefaa3f0a5174` | 2293 | 67.5KB | 2026-07-24 | next-forge expert guidance — production-grade Turborepo monorepo SaaS starter by | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/next-forge` |
| `next-upgrade` | claude-plugin | `96d79dd3cff54d5c` | 863 | 6.9KB | 2026-07-24 | Upgrade Next.js to the latest version following official migration guides and co | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/next-upgrade` |
| `nextjs` | claude-plugin | `a84d03ab8e780505` | 4478 | 190.0KB | 2026-07-24 | Next.js App Router expert guidance. Use when building, debugging, or architectin | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/nextjs` |
| `playground` | claude-plugin | `521a3d62211e5f47` | 949 | 29.4KB | 2026-04-11 | Creates interactive HTML playgrounds — self-contained single-file explorers that | `~/.claude/plugins/cache/claude-plugins-official/playground/unknown/skills/playground` |
| `plugin-audit` | claude-plugin | `3179280ffb97921c` | 738 | 11.1KB | 2026-07-24 | Audit vercel-plugin performance on real-world projects. Extracts tool calls from | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/.claude/skills/plugin-audit` |
| `plugin-settings` | claude-plugin | `028b955244b937eb` | 3018 | 43.4KB | 2026-07-24 | This skill should be used when the user asks about "plugin settings", "store plu | `~/.claude/plugins/cache/claude-plugins-official/plugin-dev/unknown/skills/plugin-settings` |
| `plugin-structure` | claude-plugin | `a2dbc1e5502aacb3` | 3361 | 73.2KB | 2026-07-24 | This skill should be used when the user asks to "create a plugin", "scaffold a p | `~/.claude/plugins/cache/claude-plugins-official/plugin-dev/unknown/skills/plugin-structure` |
| `profiling-tables` | claude-plugin | `4a0e5739cf2a898c` | 964 | 3.8KB | 2026-03-14 | Deep-dive data profiling for a specific table. Use when the user asks to profile | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/profiling-tables` |
| `react-best-practices` | claude-plugin | `1f5a862b4c60fbf1` | 2009 | 394.3KB | 2026-07-24 | React best-practices reviewer for TSX files. Triggers after editing multiple TSX | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/react-best-practices` |
| `receiving-code-review` | claude-plugin | `647036bbdab7bf23` | 1586 | 6.2KB | 2026-07-24 | Use when receiving code review feedback, before implementing suggestions, especi | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/receiving-code-review` |
| `release` | claude-plugin | `f0ba3b1f281455ac` | 528 | 2.1KB | 2026-07-24 | Release vercel-plugin — run gates, bump version, generate artifacts, commit, and | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/.claude/skills/release` |
| `repro-flaky-tests` | claude-plugin | `49ec4da84012fb98` | 783 | 3.1KB | 2026-07-24 | Reproduce and investigate flakiness in a test. | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/repro-flaky-tests` |
| `requesting-code-review` | claude-plugin | `1017ccdd5bc61fab` | 706 | 7.9KB | 2026-07-24 | Use when completing tasks, implementing major features, or before merging to ver | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/requesting-code-review` |
| `routing-middleware` | claude-plugin | `b582ee4551d744ad` | 2777 | 10.9KB | 2026-07-24 | Vercel Routing Middleware guidance — request interception before cache, rewrites | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/routing-middleware` |
| `runtime-cache` | claude-plugin | `22fb4be9b3b2e39e` | 2292 | 9.0KB | 2026-07-24 | Vercel Runtime Cache API guidance — ephemeral per-region key-value cache with ta | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/runtime-cache` |
| `setting-up-astro-project` | claude-plugin | `0e1755743d5d9533` | 705 | 2.8KB | 2026-03-14 | Initialize and configure Astro/Airflow projects. Use when the user wants to crea | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/setting-up-astro-project` |
| `shadcn` | claude-plugin | `4a24abf22a6d10a6` | 5007 | 19.6KB | 2026-07-24 | shadcn/ui expert guidance — CLI, component installation, composition patterns, c | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/shadcn` |
| `skill-creator` | claude-plugin | `ba8bebb2c0854441` | 8047 | 218.6KB | 2026-04-11 | Create new skills, modify and improve existing skills, and measure skill perform | `~/.claude/plugins/cache/claude-plugins-official/skill-creator/unknown/skills/skill-creator` |
| `skill-development` | claude-plugin | `d51b4e20043b13e4` | 5633 | 33.6KB | 2026-07-24 | This skill should be used when the user wants to "create a skill", "add a skill  | `~/.claude/plugins/cache/claude-plugins-official/plugin-dev/unknown/skills/skill-development` |
| `subagent-driven-development` | claude-plugin | `41ab239a6ad1c487` | 5385 | 37.5KB | 2026-07-24 | Use when executing implementation plans with independent tasks in the current se | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/subagent-driven-development` |
| `systematic-debugging` | claude-plugin | `3b20719eca4f0461` | 2465 | 39.8KB | 2026-07-24 | Use when encountering any bug, test failure, or unexpected behavior, before prop | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/systematic-debugging` |
| `test-driven-development` | claude-plugin | `b5b4717b8b761cce` | 2471 | 17.7KB | 2026-07-24 | Use when implementing any feature or bugfix, before writing implementation code | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/test-driven-development` |
| `testing-dags` | claude-plugin | `4086dca947e6cfb6` | 2493 | 10.4KB | 2026-03-14 | Complex DAG testing workflows with debugging and fixing cycles. Use for multi-st | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/testing-dags` |
| `tracing-downstream-lineage` | claude-plugin | `e700fd1a5d81b7b3` | 1244 | 4.9KB | 2026-03-14 | Trace downstream data lineage and impact analysis. Use when the user asks what d | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/tracing-downstream-lineage` |
| `tracing-upstream-lineage` | claude-plugin | `f7e540bb391c24c1` | 1125 | 4.4KB | 2026-03-14 | Trace upstream data lineage. Use when the user asks where data comes from, what  | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/tracing-upstream-lineage` |
| `troubleshooting` | claude-plugin | `f7deca2af0ea0f65` | 1730 | 6.8KB | 2026-07-24 | Uses Chrome DevTools MCP and documentation to troubleshoot connection and target | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/skills/troubleshooting` |
| `troubleshooting-astro-deployments` | claude-plugin | `0751f6a79258a332` | 2135 | 8.4KB | 2026-03-14 | Troubleshoot Astronomer production deployments with Astro CLI. Use when investig | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/troubleshooting-astro-deployments` |
| `turbopack` | claude-plugin | `8ef65187c42680aa` | 2664 | 10.4KB | 2026-07-24 | Turbopack expert guidance. Use when configuring the Next.js bundler, optimizing  | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/turbopack` |
| `ui-eng-vision-local-lit-renderer` | claude-plugin | `a5f06b009796431e` | 1511 | 5.9KB | 2026-07-24 | Migrates legacy imperative DOM construction to local declarative Lit-html templa | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/ui-eng-vision-local-lit-renderer` |
| `ui-eng-vision-logic-consolidator` | claude-plugin | `eb49f9a344b5647a` | 797 | 3.1KB | 2026-07-24 | Consolidates manual DOM creation, updates, and constructors into private helper  | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/ui-eng-vision-logic-consolidator` |
| `ui-eng-vision-orchestrator` | claude-plugin | `3dc11d933dc3483c` | 1899 | 7.4KB | 2026-07-24 | High-level orchestrator for managing multi-pass migration of Chrome DevTools leg | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/ui-eng-vision-orchestrator` |
| `ui-eng-vision-test-scaffolder` | claude-plugin | `3e4a1ff26bc8f6f7` | 1382 | 5.4KB | 2026-07-24 | Scaffolds unit tests and screenshot tests to establish visual and functional ren | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/ui-eng-vision-test-scaffolder` |
| `ui-eng-vision-widget-promoter` | claude-plugin | `f80ecfccfa58c703` | 733 | 2.9KB | 2026-07-24 | Promotes legacy views to modern UI.Widget classes, hooks up performUpdate() rend | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/ui-eng-vision-widget-promoter` |
| `ui-widgets` | claude-plugin | `3cc8ba81f0fb9b92` | 3108 | 12.1KB | 2026-07-24 | Guidelines for building UI widgets using the MVP architecture in DevTools. Cover | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/ui-widgets` |
| `upstream` | claude-plugin | `be76e0d67cfd5086` | 1323 | 29.8KB | 2026-07-24 | Expert assistance for next-forge — a production-grade Turborepo template for Nex | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/next-forge/upstream` |
| `upstream` | claude-plugin | `92fc6ac94c5cabdc` | 2340 | 9.1KB | 2026-07-24 | Next.js 16 Cache Components - PPR, use cache directive, cacheLife, cacheTag, upd | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/next-cache-components/upstream` |
| `upstream` | claude-plugin | `07e3ac5f42310401` | 1170 | 23.9KB | 2026-07-24 | Answer questions about the AI SDK and help build AI-powered features. Use when d | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/ai-sdk/upstream` |
| `upstream` | claude-plugin | `d6d1224c2d01aa14` | 2235 | 8.8KB | 2026-07-24 | Build multi-platform chat bots with Chat SDK (`chat` npm package). Use when deve | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/chat-sdk/upstream` |
| `upstream` | claude-plugin | `562f9834d7c55c37` | 501 | 2.0KB | 2026-07-24 | Upgrade Next.js to the latest version following official migration guides and co | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/next-upgrade/upstream` |
| `upstream` | claude-plugin | `15da32dca80c4c84` | 2417 | 9.4KB | 2026-07-24 | Run agent-browser + Chrome inside Vercel Sandbox microVMs for browser automation | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/vercel-sandbox/upstream` |
| `upstream` | claude-plugin | `c78f952d4e98bf36` | 1000 | 81.2KB | 2026-07-24 | Next.js best practices - file conventions, RSC boundaries, data patterns, async  | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/nextjs/upstream` |
| `upstream` | claude-plugin | `d7b0ec66ea66a17e` | 1672 | 195.6KB | 2026-07-24 | React and Next.js performance optimization guidelines from Vercel Engineering. T | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/react-best-practices/upstream` |
| `upstream` | claude-plugin | `ee1d6c402c507653` | 4674 | 20.8KB | 2026-07-24 | Creates durable, resumable workflows using Vercel's Workflow DevKit. Use when bu | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/workflow/upstream` |
| `upstream` | claude-plugin | `e467a328d14991a6` | 514 | 2.0KB | 2026-07-24 | Build durable backend AI agents with the eve framework. Use when creating, editi | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/eve/upstream` |
| `upstream` | claude-plugin | `14984c8339516b9c` | 908 | 35.5KB | 2026-07-24 | Deploy, manage, and develop projects on Vercel from the command line | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/vercel-cli/upstream` |
| `using-git-worktrees` | claude-plugin | `e2c3ec142e52868a` | 1866 | 7.3KB | 2026-07-24 | Use when starting feature work that needs isolation from current workspace or be | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/using-git-worktrees` |
| `using-superpowers` | claude-plugin | `55379fe7c1c473a0` | 762 | 7.2KB | 2026-07-24 | Use when starting any conversation - establishes how to find and use skills, req | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/using-superpowers` |
| `vercel-agent` | claude-plugin | `c7ddcf740e606904` | 727 | 2.9KB | 2026-07-24 | Vercel Agent guidance — AI-powered code review, incident investigation, and SDK  | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/vercel-agent` |
| `vercel-cli` | claude-plugin | `2e41d91935f64834` | 1470 | 75.4KB | 2026-07-24 | Vercel CLI expert guidance. Use when deploying, managing environment variables,  | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/vercel-cli` |
| `vercel-connect` | claude-plugin | `5e8e30c2452d66c0` | 4767 | 18.7KB | 2026-07-24 | Vercel Connect expert guidance — securely obtain scoped OAuth tokens for third-p | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/vercel-connect` |
| `vercel-firewall` | claude-plugin | `04869e0db25bf5bf` | 5125 | 20.1KB | 2026-07-24 | Vercel Firewall expert guidance — automatic DDoS mitigation, the Vercel WAF (cus | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/vercel-firewall` |
| `vercel-functions` | claude-plugin | `66234b69f9c9a2fa` | 5517 | 21.7KB | 2026-07-24 | Vercel Functions expert guidance — Serverless Functions, Edge Functions, Fluid C | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/vercel-functions` |
| `vercel-plugin-eval` | claude-plugin | `5c701868cc662025` | 1235 | 4.9KB | 2026-07-24 | Run live eval sessions against the vercel-plugin to verify hook behavior, skill  | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/.claude/skills/vercel-plugin-eval` |
| `vercel-sandbox` | claude-plugin | `8a80453de690fb07` | 2940 | 23.6KB | 2026-07-24 | Vercel Sandbox guidance — ephemeral Firecracker microVMs for running untrusted c | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/vercel-sandbox` |
| `vercel-storage` | claude-plugin | `9365181d819522cb` | 4947 | 19.4KB | 2026-07-24 | Vercel storage expert guidance — Blob, Edge Config, and Marketplace storage (Neo | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/vercel-storage` |
| `verification` | claude-plugin | `b167d95f2644801e` | 413 | 1.6KB | 2026-07-24 | MANDATORY: Activate this skill ANY TIME you need to build the project, run tests | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/verification` |
| `verification` | claude-plugin | `1c9cdb2b04c479a9` | 1960 | 7.7KB | 2026-07-24 | Full-story verification — infers what the user is building, then verifies the co | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/verification` |
| `verification-before-completion` | claude-plugin | `ea52d15aabaf72bc` | 1037 | 4.1KB | 2026-07-24 | Use when about to claim work is complete, fixed, or passing, before committing o | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/verification-before-completion` |
| `version-control` | claude-plugin | `d97e46356c37870a` | 933 | 3.6KB | 2026-07-24 | Use when starting a new task, creating a branch, switching branches, managing br | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/version-control` |
| `video-interaction-mapper` | claude-plugin | `aa32df853ec3c7a3` | 3235 | 72.0KB | 2026-07-24 | This skill should be used when the user asks to analyze a UI screen recording an | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/workflow-skills/video-interaction-mapper` |
| `warehouse-init` | claude-plugin | `0f8d982796189047` | 2637 | 10.3KB | 2026-03-14 | Initialize warehouse schema discovery. Generates .astro/warehouse.md with all ta | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/skills/warehouse-init` |
| `workflow` | claude-plugin | `5d3f2e4a353fd3df` | 7983 | 67.9KB | 2026-07-24 | Vercel Workflow DevKit (WDK) expert guidance. Use when building durable workflow | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/workflow` |
| `writing-plans` | claude-plugin | `272e1af349f5062c` | 1767 | 8.6KB | 2026-07-24 | Use when you have a spec or requirements for a multi-step task, before touching  | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/writing-plans` |
| `writing-rules` | claude-plugin | `2994b5d3152243b1` | 2103 | 8.2KB | 2026-04-11 | This skill should be used when the user asks to "create a hookify rule", "write  | `~/.claude/plugins/cache/claude-plugins-official/hookify/unknown/skills/writing-rules` |
| `writing-skills` | claude-plugin | `6b8d08fe863318be` | 6582 | 104.8KB | 2026-07-24 | Use when creating new skills, editing existing skills, or verifying skills work  | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/writing-skills` |
| `claude-dynamic-workflows` | codex | `74c0f0176646d08c` | 1281 | 5.3KB | 2026-06-09 | Coordinate Claude Code dynamic workflows from Codex through a visible tmux sessi | `~/.codex/skills/claude-dynamic-workflows` |
| `codex-claude-communication` | codex | `9e6f35143ae9d6ff` | 3502 | 14.0KB | 2026-05-26 | Coordinate with Claude Code through a visible shared tmux session. Use when Code | `~/.codex/skills/codex-claude-communication` |
| `imagegen` | codex | `59981d23519222bc` | 5990 | 131.8KB | 2026-08-07 | Generate or edit raster images when the task benefits from AI-created bitmap vis | `~/.codex/skills/.system/imagegen` |
| `migrate-to-codex` | codex | `145b7fe48360642c` | 1981 | 291.0KB | 2026-05-02 | Migrate supported instruction files, skills, agents, and MCP config into Codex p | `~/.codex/skills/migrate-to-codex` |
| `openai-docs` | codex | `7cb8fa1b2a0c635b` | 1360 | 99.2KB | 2026-08-07 | Use for Codex models/pricing, scheduled tasks, skills, settings, setup, troubles | `~/.codex/skills/.system/openai-docs` |
| `playwright` | codex | `0ffaabcc8e099062` | 942 | 22.3KB | 2026-05-02 | Use when the task requires automating a real browser from the terminal (navigati | `~/.codex/skills/playwright` |
| `plugin-creator` | codex | `8fd56316b2c49cbd` | 2759 | 64.3KB | 2026-08-07 | Create and scaffold plugin directories for Codex with a required `.codex-plugin/ | `~/.codex/skills/.system/plugin-creator` |
| `review-agent` | codex | `07079efd0dc76f05` | 664 | 2.8KB | 2026-08-07 | Perform a read-only, defect-first review of a specified code change and return e | `~/.codex/skills/.system/review-agent` |
| `skill-creator` | codex | `da44c88f6b3845a8` | 5474 | 61.9KB | 2026-08-07 | Guide for creating effective skills. This skill should be used when users want t | `~/.codex/skills/.system/skill-creator` |
| `skill-installer` | codex | `d68b77e5bbb34ded` | 841 | 30.0KB | 2026-08-07 | Install Codex skills into $CODEX_HOME/skills from a curated list or a GitHub rep | `~/.codex/skills/.system/skill-installer` |
| `automate` | cursor | `0a0aadc25417c7cd` | 8828 | 34.7KB | 2026-07-02 | Use this skill to create Cursor Automations. | `~/.cursor/skills-cursor/automate` |
| `autopilot` | cursor | `2f0fd7aebbcacfa8` | 975 | 3.8KB | 2026-08-01 | Keep a PR merge-ready by triaging comments, resolving clear conflicts, and fixin | `~/.cursor/skills-cursor/autopilot` |
| `canvas` | cursor | `f2167e32c6f15996` | 2265 | 81.4KB | 2026-08-09 | A Cursor Canvas is a live React app that the user can open beside the chat. You  | `~/.cursor/skills-cursor/canvas` |
| `create-hook` | cursor | `28bfa0c6cd13fa01` | 2298 | 9.0KB | 2026-07-02 | Create Cursor hooks. Use when you want to create a hook, write hooks.json, add h | `~/.cursor/skills-cursor/create-hook` |
| `create-rule` | cursor | `fe8499dfe93edbf8` | 908 | 3.6KB | 2026-07-02 | Create Cursor rules for persistent AI guidance. Use when you want to create a ru | `~/.cursor/skills-cursor/create-rule` |
| `create-skill` | cursor | `f3df382ea9cf2119` | 3580 | 14.1KB | 2026-07-02 | Create Cursor Agent Skills. Use when authoring a new skill or asking about SKILL | `~/.cursor/skills-cursor/create-skill` |
| `create-subagent` | cursor | `ab46a80f90cb3810` | 1612 | 6.3KB | 2026-07-02 | Create custom subagents for specialized AI tasks. Use when you want to create a  | `~/.cursor/skills-cursor/create-subagent` |
| `loop` | cursor | `03f141406a7e6f65` | 961 | 3.8KB | 2026-07-02 | Run a prompt or skill in this session on a recurring or variable interval (e.g.  | `~/.cursor/skills-cursor/loop` |
| `migrate-to-skills` | cursor | `78c0483cc96ea3dc` | 1607 | 6.3KB | 2026-07-02 | Convert 'Applied intelligently' Cursor rules (.cursor/rules/*.mdc) and slash com | `~/.cursor/skills-cursor/migrate-to-skills` |
| `onboard` | cursor | `ad68425f9ae8124b` | 3059 | 12.0KB | 2026-07-02 | Use /onboard for a focused Cursor onboarding flow that learns basic preferences, | `~/.cursor/skills-cursor/onboard` |
| `rename-chat` | cursor | `618b6d42cacec9b7` | 197 | 793B | 2026-08-06 | Rename the current chat to match its focus. Use only when the user invokes /rena | `~/.cursor/skills-cursor/rename-chat` |
| `review` | cursor | `bd00601823f73cce` | 146 | 587B | 2026-07-02 | Review code changes with the Bugbot or Security Review subagent. | `~/.cursor/skills-cursor/review` |
| `review-bugbot` | cursor | `24bb0b36844583c5` | 1225 | 4.8KB | 2026-07-16 | Review code changes with Bugbot subagent. | `~/.cursor/skills-cursor/review-bugbot` |
| `review-security` | cursor | `d007ec5a617e9cae` | 939 | 3.7KB | 2026-07-16 | Review code changes with Security Review subagent. | `~/.cursor/skills-cursor/review-security` |
| `sdk` | cursor | `0db41158a6b4fd16` | 4894 | 19.1KB | 2026-07-02 | Guide users building apps, scripts, CI pipelines, or automations on top of the C | `~/.cursor/skills-cursor/sdk` |
| `shell` | cursor | `b354ce28c70af1d5` | 216 | 867B | 2026-07-02 | Runs the rest of a /shell request as a literal shell command. Use only when the  | `~/.cursor/skills-cursor/shell` |
| `split-to-prs` | cursor | `61e3b94b54457e0f` | 565 | 2.2KB | 2026-07-02 | Split current work into small reviewable PRs. Use when the user asks to split a  | `~/.cursor/skills-cursor/split-to-prs` |
| `statusline` | cursor | `9ce07cefc16faf73` | 1793 | 7.0KB | 2026-07-02 | Configure a custom status line in the CLI. Use when the user mentions status lin | `~/.cursor/skills-cursor/statusline` |
| `update-cli-config` | cursor | `4c784e4e26cc3c8b` | 1156 | 4.6KB | 2026-08-08 | View and modify Cursor CLI configuration settings in ~/.cursor/cli-config.json.  | `~/.cursor/skills-cursor/update-cli-config` |
| `update-cursor-settings` | cursor | `8b5ee1a74dc964f5` | 1073 | 4.2KB | 2026-07-02 | Modify Cursor/VSCode user settings in settings.json. Use when you want to change | `~/.cursor/skills-cursor/update-cursor-settings` |
| `agent-ready-apis` | cursor-plugin | `84d33fc53f753a99` | 809 | 11.2KB | 2026-07-23 | Knowledge about AI agent API compatibility. Use when user asks about API readine | `~/.cursor/plugins/cache/cursor-public/2050/e351cde6137a98b943c8fb8d0c698c1b438dfc0f/skills/agent-ready-apis` |
| `agent-ready-apis` | cursor-plugin | `84d33fc53f753a99` | 809 | 11.2KB | 2026-08-05 | Knowledge about AI agent API compatibility. Use when user asks about API readine | `~/.cursor/plugins/cache/cursor-public/postman/f5ea7c56da1dc022753c66ac4fba398e881b07dd/skills/agent-ready-apis` |
| `agents-build` | cursor-plugin | `43083e10f5ef8343` | 2163 | 124.6KB | 2026-07-20 | Use when adding capabilities to an existing agent project — memory, app integrat | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-build` |
| `agents-build` | cursor-plugin | `43083e10f5ef8343` | 2163 | 124.6KB | 2026-07-02 | Use when adding capabilities to an existing agent project — memory, app integrat | `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-build` |
| `agents-connect` | cursor-plugin | `ad5a87cb3f64b3a1` | 7355 | 38.7KB | 2026-07-20 | Use when connecting your agent to external APIs, tools, or services via Gateway, | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-connect` |
| `agents-connect` | cursor-plugin | `ad5a87cb3f64b3a1` | 7355 | 38.7KB | 2026-07-02 | Use when connecting your agent to external APIs, tools, or services via Gateway, | `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-connect` |
| `agents-debug` | cursor-plugin | `dd20ab6870ef8552` | 7732 | 36.2KB | 2026-07-20 | Use when your agent or environment is broken — wrong answers, errors, timeouts,  | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-debug` |
| `agents-debug` | cursor-plugin | `dd20ab6870ef8552` | 7732 | 36.2KB | 2026-07-02 | Use when your agent or environment is broken — wrong answers, errors, timeouts,  | `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-debug` |
| `agents-deploy` | cursor-plugin | `31f0800e3c31a7e3` | 1976 | 11.8KB | 2026-07-20 | Use when deploying your agent to AWS, or when a deploy has failed. Handles pre-f | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-deploy` |
| `agents-deploy` | cursor-plugin | `31f0800e3c31a7e3` | 1976 | 11.8KB | 2026-07-02 | Use when deploying your agent to AWS, or when a deploy has failed. Handles pre-f | `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-deploy` |
| `agents-get-started` | cursor-plugin | `fab2f8dbccaea9cb` | 4299 | 23.6KB | 2026-07-20 | Use when a developer wants to create a new agent project or get started with Age | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-get-started` |
| `agents-get-started` | cursor-plugin | `fab2f8dbccaea9cb` | 4299 | 23.6KB | 2026-07-02 | Use when a developer wants to create a new agent project or get started with Age | `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-get-started` |
| `agents-harden` | cursor-plugin | `6207879b9d7637eb` | 8025 | 45.0KB | 2026-07-20 | Use when preparing your agent for production — IAM scoping, inbound auth (JWT, S | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-harden` |
| `agents-harden` | cursor-plugin | `6207879b9d7637eb` | 8025 | 45.0KB | 2026-07-02 | Use when preparing your agent for production — IAM scoping, inbound auth (JWT, S | `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-harden` |
| `agents-optimize` | cursor-plugin | `91c53dbdd334010a` | 913 | 37.0KB | 2026-07-20 | Use when measuring or improving agent quality and performance — set up evaluator | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-optimize` |
| `agents-optimize` | cursor-plugin | `91c53dbdd334010a` | 913 | 37.0KB | 2026-07-02 | Use when measuring or improving agent quality and performance — set up evaluator | `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-optimize` |
| `brainstorming` | cursor-plugin | `e14914605f640e08` | 2598 | 73.1KB | 2026-07-24 | You MUST use this before any creative work - creating features, building compone | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/brainstorming` |
| `brainstorming` | cursor-plugin | `e14914605f640e08` | 2598 | 73.1KB | 2026-07-16 | You MUST use this before any creative work - creating features, building compone | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/brainstorming` |
| `dispatching-parallel-agents` | cursor-plugin | `f0df13f584049059` | 1654 | 6.5KB | 2026-07-24 | Use when facing 2+ independent tasks that can be worked on without shared state  | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/dispatching-parallel-agents` |
| `dispatching-parallel-agents` | cursor-plugin | `f0df13f584049059` | 1654 | 6.5KB | 2026-07-16 | Use when facing 2+ independent tasks that can be worked on without shared state  | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/dispatching-parallel-agents` |
| `executing-plans` | cursor-plugin | `bbd8d28bb655a528` | 647 | 2.5KB | 2026-07-24 | Use when you have a written implementation plan to execute in a separate session | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/executing-plans` |
| `executing-plans` | cursor-plugin | `bbd8d28bb655a528` | 647 | 2.5KB | 2026-07-16 | Use when you have a written implementation plan to execute in a separate session | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/executing-plans` |
| `finishing-a-development-branch` | cursor-plugin | `e6d4a812de900d33` | 1703 | 6.7KB | 2026-07-24 | Use when implementation is complete, all tests pass, and you need to decide how  | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/finishing-a-development-branch` |
| `finishing-a-development-branch` | cursor-plugin | `e6d4a812de900d33` | 1703 | 6.7KB | 2026-07-16 | Use when implementation is complete, all tests pass, and you need to decide how  | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/finishing-a-development-branch` |
| `firecrawl` | cursor-plugin | `a699d1e65f711651` | 5510 | 21.6KB | 2026-07-22 | Firecrawl handles all web operations with superior accuracy, speed, and LLM-opti | `~/.cursor/plugins/cache/cursor-public/789/80ce444eb020b5f41b34836c553f162d6113cd6f/skills/firecrawl` |
| `firecrawl-agent` | cursor-plugin | `9a8badff132ebea1` | 686 | 2.7KB | 2026-08-05 | AI-powered autonomous data extraction that navigates complex sites and returns s | `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-agent` |
| `firecrawl-cli` | cursor-plugin | `b02b8467e6f4de9d` | 4396 | 20.7KB | 2026-08-05 | Search, scrape, and interact with the web via the Firecrawl CLI. Use this skill  | `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-cli` |
| `firecrawl-crawl` | cursor-plugin | `a5ddaae261c6c2c4` | 672 | 2.6KB | 2026-08-05 | Bulk extract content from an entire website or site section. Use this skill when | `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-crawl` |
| `firecrawl-download` | cursor-plugin | `8c734be50e335cf8` | 774 | 3.0KB | 2026-08-05 | Download an entire website as local files — markdown, screenshots, or multiple f | `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-download` |
| `firecrawl-interact` | cursor-plugin | `f6ddfc4b857a7c54` | 978 | 3.8KB | 2026-08-05 | Control and interact with a live browser session on any scraped page — click but | `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-interact` |
| `firecrawl-map` | cursor-plugin | `55e6ea4076bdda1a` | 533 | 2.1KB | 2026-08-05 | Discover and list all URLs on a website, with optional search filtering. Use thi | `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-map` |
| `firecrawl-monitor` | cursor-plugin | `ff3a9ed21dc1c4e3` | 4841 | 19.0KB | 2026-08-05 | Detect when content on a website changes and get notified by webhook or email —  | `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-monitor` |
| `firecrawl-parse` | cursor-plugin | `c6f694cab0dbfddc` | 678 | 2.7KB | 2026-08-05 | Efficiently extract and convert the contents of any local file—such as PDF, DOCX | `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-parse` |
| `firecrawl-scrape` | cursor-plugin | `4fd52e6478dc8964` | 930 | 3.7KB | 2026-08-05 | Extract clean markdown from any URL, including JavaScript-rendered SPAs. Use thi | `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-scrape` |
| `firecrawl-search` | cursor-plugin | `8d926a5840e1c9b5` | 2004 | 7.9KB | 2026-08-05 | Web search with full page content extraction. Use this skill whenever the user a | `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-search` |
| `gsap-core` | cursor-plugin | `3887b47e050ab5af` | 3669 | 14.4KB | 2026-07-20 | Official GSAP skill for the core API — gsap.to(), from(), fromTo(), easing, dura | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-core` |
| `gsap-core` | cursor-plugin | `3887b47e050ab5af` | 3669 | 14.4KB | 2026-07-02 | Official GSAP skill for the core API — gsap.to(), from(), fromTo(), easing, dura | `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-core` |
| `gsap-frameworks` | cursor-plugin | `842d9d3659ec3ddc` | 2640 | 10.4KB | 2026-07-20 | Official GSAP skill for Vue, Svelte, and other non-React frameworks — lifecycle, | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-frameworks` |
| `gsap-frameworks` | cursor-plugin | `842d9d3659ec3ddc` | 2640 | 10.4KB | 2026-07-02 | Official GSAP skill for Vue, Svelte, and other non-React frameworks — lifecycle, | `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-frameworks` |
| `gsap-performance` | cursor-plugin | `cb5408d6fba707aa` | 1026 | 4.0KB | 2026-07-20 | Official GSAP skill for performance — prefer transforms, avoid layout thrashing, | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-performance` |
| `gsap-performance` | cursor-plugin | `cb5408d6fba707aa` | 1026 | 4.0KB | 2026-07-02 | Official GSAP skill for performance — prefer transforms, avoid layout thrashing, | `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-performance` |
| `gsap-plugins` | cursor-plugin | `5838b856c74c07fb` | 5369 | 21.1KB | 2026-07-20 | Official GSAP skill for GSAP plugins — registration, ScrollToPlugin, ScrollSmoot | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-plugins` |
| `gsap-plugins` | cursor-plugin | `5838b856c74c07fb` | 5369 | 21.1KB | 2026-07-02 | Official GSAP skill for GSAP plugins — registration, ScrollToPlugin, ScrollSmoot | `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-plugins` |
| `gsap-react` | cursor-plugin | `88e2a5312b45e8cc` | 1632 | 6.4KB | 2026-07-20 | Official GSAP skill for React — useGSAP hook, refs, gsap.context(), cleanup. Use | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-react` |
| `gsap-react` | cursor-plugin | `88e2a5312b45e8cc` | 1632 | 6.4KB | 2026-07-02 | Official GSAP skill for React — useGSAP hook, refs, gsap.context(), cleanup. Use | `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-react` |
| `gsap-scrolltrigger` | cursor-plugin | `9351b6666a4749c0` | 4574 | 18.0KB | 2026-07-20 | Official GSAP skill for ScrollTrigger — scroll-linked animations, pinning, scrub | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-scrolltrigger` |
| `gsap-scrolltrigger` | cursor-plugin | `9351b6666a4749c0` | 4574 | 18.0KB | 2026-07-02 | Official GSAP skill for ScrollTrigger — scroll-linked animations, pinning, scrub | `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-scrolltrigger` |
| `gsap-timeline` | cursor-plugin | `1a8b0f39cc4be3ed` | 1084 | 4.3KB | 2026-07-20 | Official GSAP skill for timelines — gsap.timeline(), position parameter, nesting | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-timeline` |
| `gsap-timeline` | cursor-plugin | `1a8b0f39cc4be3ed` | 1084 | 4.3KB | 2026-07-02 | Official GSAP skill for timelines — gsap.timeline(), position parameter, nesting | `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-timeline` |
| `gsap-utils` | cursor-plugin | `1927bcc4ea95b382` | 3012 | 11.8KB | 2026-07-20 | Official GSAP skill for gsap.utils — clamp, mapRange, normalize, interpolate, ra | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-utils` |
| `gsap-utils` | cursor-plugin | `1927bcc4ea95b382` | 3012 | 11.8KB | 2026-07-02 | Official GSAP skill for gsap.utils — clamp, mapRange, normalize, interpolate, ra | `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-utils` |
| `hf-cli` | cursor-plugin | `6cca38f44ab1485a` | 6752 | 26.7KB | 2026-07-20 | Hugging Face Hub CLI (`hf`) for downloading, uploading, and managing models, dat | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/hf-cli` |
| `hf-cli` | cursor-plugin | `6cca38f44ab1485a` | 6752 | 26.7KB | 2026-07-02 | Hugging Face Hub CLI (`hf`) for downloading, uploading, and managing models, dat | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/hf-cli` |
| `hf-mcp` | cursor-plugin | `4cd99f2f6fabc5d8` | 1241 | 4.9KB | 2026-07-20 | Use Hugging Face Hub via MCP server tools. Search models, datasets, Spaces, pape | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/hf-mcp/skills/hf-mcp` |
| `hf-mcp` | cursor-plugin | `4cd99f2f6fabc5d8` | 1241 | 4.9KB | 2026-07-02 | Use Hugging Face Hub via MCP server tools. Search models, datasets, Spaces, pape | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/hf-mcp/skills/hf-mcp` |
| `huggingface-best` | cursor-plugin | `156a85e1e6e6c750` | 1452 | 5.7KB | 2026-07-20 | Use when the user asks about finding the best, top, or recommended model for a t | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-best` |
| `huggingface-best` | cursor-plugin | `156a85e1e6e6c750` | 1452 | 5.7KB | 2026-07-02 | Use when the user asks about finding the best, top, or recommended model for a t | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-best` |
| `huggingface-community-evals` | cursor-plugin | `a97f1c703f55b724` | 1638 | 29.4KB | 2026-07-20 | Run evaluations for Hugging Face Hub models using inspect-ai and lighteval on lo | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-community-evals` |
| `huggingface-community-evals` | cursor-plugin | `a97f1c703f55b724` | 1638 | 29.4KB | 2026-07-02 | Run evaluations for Hugging Face Hub models using inspect-ai and lighteval on lo | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-community-evals` |
| `huggingface-datasets` | cursor-plugin | `eeca50adf211ea64` | 1141 | 4.6KB | 2026-07-20 | Use this skill for Hugging Face Dataset Viewer API workflows that fetch subset/s | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-datasets` |
| `huggingface-datasets` | cursor-plugin | `eeca50adf211ea64` | 1141 | 4.6KB | 2026-07-02 | Use this skill for Hugging Face Dataset Viewer API workflows that fetch subset/s | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-datasets` |
| `huggingface-gradio` | cursor-plugin | `ce41c656e364a802` | 6182 | 38.3KB | 2026-07-20 | Build Gradio web UIs and demos in Python. Use when creating or editing Gradio ap | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-gradio` |
| `huggingface-gradio` | cursor-plugin | `ce41c656e364a802` | 6182 | 38.3KB | 2026-07-02 | Build Gradio web UIs and demos in Python. Use when creating or editing Gradio ap | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-gradio` |
| `huggingface-llm-trainer` | cursor-plugin | `fb5c5e25103e3822` | 7165 | 181.5KB | 2026-07-20 | Train or fine-tune language and vision models using TRL (Transformer Reinforceme | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-llm-trainer` |
| `huggingface-llm-trainer` | cursor-plugin | `fb5c5e25103e3822` | 7165 | 181.5KB | 2026-07-02 | Train or fine-tune language and vision models using TRL (Transformer Reinforceme | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-llm-trainer` |
| `huggingface-local-models` | cursor-plugin | `814640db1d5f2f27` | 945 | 15.5KB | 2026-07-20 | Use to select models to run locally with llama.cpp and GGUF on CPU, Mac Metal, C | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-local-models` |
| `huggingface-local-models` | cursor-plugin | `814640db1d5f2f27` | 945 | 15.5KB | 2026-07-02 | Use to select models to run locally with llama.cpp and GGUF on CPU, Mac Metal, C | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-local-models` |
| `huggingface-lora-space-builder` | cursor-plugin | `208fbefe87f3ce1a` | 8187 | 105.4KB | 2026-07-20 | Build and publish a Gradio demo on Hugging Face Spaces for a user-provided LoRA. | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-lora-space-builder` |
| `huggingface-lora-space-builder` | cursor-plugin | `208fbefe87f3ce1a` | 8187 | 105.4KB | 2026-07-02 | Build and publish a Gradio demo on Hugging Face Spaces for a user-provided LoRA. | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-lora-space-builder` |
| `huggingface-paper-publisher` | cursor-plugin | `04b59406cf88054a` | 4172 | 73.6KB | 2026-07-20 | Publish and manage research papers on Hugging Face Hub. Supports creating paper  | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-paper-publisher` |
| `huggingface-paper-publisher` | cursor-plugin | `04b59406cf88054a` | 4172 | 73.6KB | 2026-07-02 | Publish and manage research papers on Hugging Face Hub. Supports creating paper  | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-paper-publisher` |
| `huggingface-papers` | cursor-plugin | `985c2d5c7261aba2` | 2337 | 9.1KB | 2026-07-20 | Look up and read Hugging Face paper pages in markdown, and use the papers API fo | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-papers` |
| `huggingface-papers` | cursor-plugin | `985c2d5c7261aba2` | 2337 | 9.1KB | 2026-07-02 | Look up and read Hugging Face paper pages in markdown, and use the papers API fo | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-papers` |
| `huggingface-spaces` | cursor-plugin | `3cbaf778d674e292` | 3735 | 80.1KB | 2026-07-20 | Build, deploy, and maintain applications on Hugging Face Spaces — Gradio / Docke | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-spaces` |
| `huggingface-spaces` | cursor-plugin | `3cbaf778d674e292` | 3735 | 80.1KB | 2026-07-02 | Build, deploy, and maintain applications on Hugging Face Spaces — Gradio / Docke | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-spaces` |
| `huggingface-tool-builder` | cursor-plugin | `2846b591259b134a` | 1470 | 27.9KB | 2026-07-20 | Use this skill when the user wants to build tool/scripts or achieve a task where | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-tool-builder` |
| `huggingface-tool-builder` | cursor-plugin | `2846b591259b134a` | 1470 | 27.9KB | 2026-07-02 | Use this skill when the user wants to build tool/scripts or achieve a task where | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-tool-builder` |
| `huggingface-trackio` | cursor-plugin | `893ac9695f8677db` | 1211 | 23.8KB | 2026-07-20 | Track and visualize ML training experiments with Trackio. Use when logging metri | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-trackio` |
| `huggingface-trackio` | cursor-plugin | `893ac9695f8677db` | 1211 | 23.8KB | 2026-07-02 | Track and visualize ML training experiments with Trackio. Use when logging metri | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-trackio` |
| `huggingface-vision-trainer` | cursor-plugin | `c7aba4de75fa6595` | 7498 | 196.8KB | 2026-07-20 | Trains and fine-tunes vision models for object detection (D-FINE, RT-DETR v2, DE | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-vision-trainer` |
| `huggingface-vision-trainer` | cursor-plugin | `c7aba4de75fa6595` | 7498 | 196.8KB | 2026-07-02 | Trains and fine-tunes vision models for object detection (D-FINE, RT-DETR v2, DE | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-vision-trainer` |
| `huggingface-zerogpu` | cursor-plugin | `829659aec3422497` | 4551 | 33.5KB | 2026-07-20 | AI demos and GPU compute with Gradio Spaces and Hugging Face Spaces ZeroGPU. Use | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-zerogpu` |
| `huggingface-zerogpu` | cursor-plugin | `829659aec3422497` | 4551 | 33.5KB | 2026-07-02 | AI demos and GPU compute with Gradio Spaces and Hugging Face Spaces ZeroGPU. Use | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-zerogpu` |
| `issues` | cursor-plugin | `e27aa511a26c9258` | 380 | 1.5KB | 2026-07-20 | List, count, summarize, or triage security issues from the Aikido security feed. | `~/.cursor/plugins/cache/cursor-public/9345/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf/skills/issues` |
| `issues` | cursor-plugin | `e27aa511a26c9258` | 380 | 1.5KB | 2026-07-02 | List, count, summarize, or triage security issues from the Aikido security feed. | `~/.cursor/plugins/cache/cursor-public/aikido-cursor-plugin/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf/skills/issues` |
| `langfuse` | cursor-plugin | `e5312bca7d29ad25` | 1644 | 43.3KB | 2026-07-25 | Interact with Langfuse and access its documentation. Use when needing to (1) que | `~/.cursor/plugins/cache/cursor-public/738/8e6c2d02accefc0dad3b7d3be3751f7fcc210885/skills/langfuse` |
| `langfuse` | cursor-plugin | `e5312bca7d29ad25` | 1644 | 43.3KB | 2026-07-02 | Interact with Langfuse and access its documentation. Use when needing to (1) que | `~/.cursor/plugins/cache/cursor-public/langfuse/8e6c2d02accefc0dad3b7d3be3751f7fcc210885/skills/langfuse` |
| `mintlify` | cursor-plugin | `09cc4ae1e5e41a95` | 2256 | 46.7KB | 2026-07-22 | Comprehensive reference for building Mintlify sites. Use when creating pages, co | `~/.cursor/plugins/cache/cursor-public/25808295/a22550306ff6b704649a8f09faf393e007cbcc1e/skills/mintlify` |
| `mintlify` | cursor-plugin | `09cc4ae1e5e41a95` | 2256 | 46.7KB | 2026-07-02 | Comprehensive reference for building Mintlify sites. Use when creating pages, co | `~/.cursor/plugins/cache/cursor-public/mintlify-cursor-plugin/a22550306ff6b704649a8f09faf393e007cbcc1e/skills/mintlify` |
| `postman-knowledge` | cursor-plugin | `0c96b9bcbe40aec5` | 1146 | 7.8KB | 2026-07-23 | Postman concepts and MCP tool guidance. Loaded when working with Postman MCP too | `~/.cursor/plugins/cache/cursor-public/2050/e351cde6137a98b943c8fb8d0c698c1b438dfc0f/skills/postman-knowledge` |
| `postman-knowledge` | cursor-plugin | `fe498ee9ce4895f5` | 1182 | 7.9KB | 2026-08-05 | Postman concepts and MCP tool guidance. Loaded when working with Postman MCP too | `~/.cursor/plugins/cache/cursor-public/postman/f5ea7c56da1dc022753c66ac4fba398e881b07dd/skills/postman-knowledge` |
| `postman-routing` | cursor-plugin | `a71efd98e2c0edef` | 843 | 3.3KB | 2026-07-23 | Automatically routes Postman and API-related requests to the correct command. Us | `~/.cursor/plugins/cache/cursor-public/2050/e351cde6137a98b943c8fb8d0c698c1b438dfc0f/skills/postman-routing` |
| `postman-routing` | cursor-plugin | `e352c68b4c70d222` | 1132 | 4.4KB | 2026-08-05 | Automatically routes Postman and API-related requests to the correct command. Us | `~/.cursor/plugins/cache/cursor-public/postman/f5ea7c56da1dc022753c66ac4fba398e881b07dd/skills/postman-routing` |
| `receiving-code-review` | cursor-plugin | `647036bbdab7bf23` | 1586 | 6.2KB | 2026-07-24 | Use when receiving code review feedback, before implementing suggestions, especi | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/receiving-code-review` |
| `receiving-code-review` | cursor-plugin | `647036bbdab7bf23` | 1586 | 6.2KB | 2026-07-16 | Use when receiving code review feedback, before implementing suggestions, especi | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/receiving-code-review` |
| `requesting-code-review` | cursor-plugin | `1017ccdd5bc61fab` | 706 | 7.9KB | 2026-07-24 | Use when completing tasks, implementing major features, or before merging to ver | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/requesting-code-review` |
| `requesting-code-review` | cursor-plugin | `1017ccdd5bc61fab` | 706 | 7.9KB | 2026-07-16 | Use when completing tasks, implementing major features, or before merging to ver | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/requesting-code-review` |
| `scan` | cursor-plugin | `4aa2b6cdb6fb72d4` | 448 | 1.8KB | 2026-07-20 | Runs an Aikido security scan on generated, added, or modified code files to dete | `~/.cursor/plugins/cache/cursor-public/9345/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf/skills/scan` |
| `scan` | cursor-plugin | `4aa2b6cdb6fb72d4` | 448 | 1.8KB | 2026-07-02 | Runs an Aikido security scan on generated, added, or modified code files to dete | `~/.cursor/plugins/cache/cursor-public/aikido-cursor-plugin/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf/skills/scan` |
| `setup` | cursor-plugin | `911501f230d9aeeb` | 571 | 2.2KB | 2026-07-20 | Configures the Aikido plugin by signing the user in through the MCP login tool a | `~/.cursor/plugins/cache/cursor-public/9345/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf/skills/setup` |
| `setup` | cursor-plugin | `911501f230d9aeeb` | 571 | 2.2KB | 2026-07-02 | Configures the Aikido plugin by signing the user in through the MCP login tool a | `~/.cursor/plugins/cache/cursor-public/aikido-cursor-plugin/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf/skills/setup` |
| `shadcn` | cursor-plugin | `75e9d038e282bb90` | 4620 | 82.4KB | 2026-07-02 | Manages shadcn components and projects — adding, searching, fixing, debugging, s | `~/.cursor/plugins/cache/cursor-public/shadcn/10f1717a3e2a3c16cfbd43877c1e44063d9d749a/skills/shadcn` |
| `skill-creator` | cursor-plugin | `d57b6e3a44535b9c` | 4485 | 49.0KB | 2026-07-25 | Guide for creating effective skills. This skill should be used when users want t | `~/.cursor/plugins/cache/cursor-public/738/8e6c2d02accefc0dad3b7d3be3751f7fcc210885/.cursor/skills/skill-creator` |
| `skill-creator` | cursor-plugin | `d57b6e3a44535b9c` | 4485 | 49.0KB | 2026-07-02 | Guide for creating effective skills. This skill should be used when users want t | `~/.cursor/plugins/cache/cursor-public/langfuse/8e6c2d02accefc0dad3b7d3be3751f7fcc210885/.cursor/skills/skill-creator` |
| `subagent-driven-development` | cursor-plugin | `41ab239a6ad1c487` | 5385 | 37.5KB | 2026-07-24 | Use when executing implementation plans with independent tasks in the current se | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/subagent-driven-development` |
| `subagent-driven-development` | cursor-plugin | `41ab239a6ad1c487` | 5385 | 37.5KB | 2026-07-16 | Use when executing implementation plans with independent tasks in the current se | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/subagent-driven-development` |
| `systematic-debugging` | cursor-plugin | `3b20719eca4f0461` | 2465 | 39.8KB | 2026-07-24 | Use when encountering any bug, test failure, or unexpected behavior, before prop | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/systematic-debugging` |
| `systematic-debugging` | cursor-plugin | `3b20719eca4f0461` | 2465 | 39.8KB | 2026-07-16 | Use when encountering any bug, test failure, or unexpected behavior, before prop | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/systematic-debugging` |
| `test-driven-development` | cursor-plugin | `b5b4717b8b761cce` | 2471 | 17.7KB | 2026-07-24 | Use when implementing any feature or bugfix, before writing implementation code | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/test-driven-development` |
| `test-driven-development` | cursor-plugin | `b5b4717b8b761cce` | 2471 | 17.7KB | 2026-07-16 | Use when implementing any feature or bugfix, before writing implementation code | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/test-driven-development` |
| `train-sentence-transformers` | cursor-plugin | `8195c00c438d8f57` | 2229 | 243.2KB | 2026-07-20 | Train or fine-tune sentence-transformers models across `SentenceTransformer` (bi | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/train-sentence-transformers` |
| `train-sentence-transformers` | cursor-plugin | `8195c00c438d8f57` | 2229 | 243.2KB | 2026-07-02 | Train or fine-tune sentence-transformers models across `SentenceTransformer` (bi | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/train-sentence-transformers` |
| `transformers-js` | cursor-plugin | `241ef15a39acbdf1` | 6224 | 93.1KB | 2026-07-20 | Use Transformers.js to run state-of-the-art machine learning models directly in  | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/transformers-js` |
| `transformers-js` | cursor-plugin | `241ef15a39acbdf1` | 6224 | 93.1KB | 2026-07-02 | Use Transformers.js to run state-of-the-art machine learning models directly in  | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/transformers-js` |
| `trl-training` | cursor-plugin | `cfdec413fcf030f2` | 2217 | 8.7KB | 2026-07-20 | Train and fine-tune transformer language models using TRL (Transformers Reinforc | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/trl-training` |
| `trl-training` | cursor-plugin | `cfdec413fcf030f2` | 2217 | 8.7KB | 2026-07-02 | Train and fine-tune transformer language models using TRL (Transformers Reinforc | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/trl-training` |
| `using-git-worktrees` | cursor-plugin | `e2c3ec142e52868a` | 1866 | 7.3KB | 2026-07-24 | Use when starting feature work that needs isolation from current workspace or be | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/using-git-worktrees` |
| `using-git-worktrees` | cursor-plugin | `e2c3ec142e52868a` | 1866 | 7.3KB | 2026-07-16 | Use when starting feature work that needs isolation from current workspace or be | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/using-git-worktrees` |
| `using-superpowers` | cursor-plugin | `55379fe7c1c473a0` | 762 | 7.2KB | 2026-07-24 | Use when starting any conversation - establishes how to find and use skills, req | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/using-superpowers` |
| `using-superpowers` | cursor-plugin | `55379fe7c1c473a0` | 762 | 7.2KB | 2026-07-16 | Use when starting any conversation - establishes how to find and use skills, req | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/using-superpowers` |
| `verification-before-completion` | cursor-plugin | `ea52d15aabaf72bc` | 1037 | 4.1KB | 2026-07-24 | Use when about to claim work is complete, fixed, or passing, before committing o | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/verification-before-completion` |
| `verification-before-completion` | cursor-plugin | `ea52d15aabaf72bc` | 1037 | 4.1KB | 2026-07-16 | Use when about to claim work is complete, fixed, or passing, before committing o | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/verification-before-completion` |
| `writing-plans` | cursor-plugin | `272e1af349f5062c` | 1767 | 8.6KB | 2026-07-24 | Use when you have a spec or requirements for a multi-step task, before touching  | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/writing-plans` |
| `writing-plans` | cursor-plugin | `272e1af349f5062c` | 1767 | 8.6KB | 2026-07-16 | Use when you have a spec or requirements for a multi-step task, before touching  | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/writing-plans` |
| `writing-skills` | cursor-plugin | `6b8d08fe863318be` | 6582 | 104.8KB | 2026-07-24 | Use when creating new skills, editing existing skills, or verifying skills work  | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/writing-skills` |
| `writing-skills` | cursor-plugin | `6b8d08fe863318be` | 6582 | 104.8KB | 2026-07-16 | Use when creating new skills, editing existing skills, or verifying skills work  | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/writing-skills` |
| `traces` | repo | `def09caea5219948` | 1491 | 65.2KB | 2026-03-06 | End-to-end marketing research pipeline -- from data collection through analysis  | `~/side_projects/marketing-agent/docs/traces` |
| `add-source` | repo-agents | `3c8d8076d5e041a7` | 642 | 2.5KB | 2026-05-02 | Add a new normalized data source collector to the marketing data service. | `~/side_projects/marketing-agent/marketing-agent-data-source/.agents/skills/add-source` |
| `analyze` | repo-agents | `8cfeff5193ce3c3d` | 468 | 1.8KB | 2026-05-02 | Query and analyze posts and authors already collected in the marketing data data | `~/side_projects/marketing-agent/marketing-agent-data-source/.agents/skills/analyze` |
| `annotate` | repo-agents | `ad0efbf83b2188d0` | 624 | 2.4KB | 2026-05-02 | Write post and author analysis back to the marketing data database as annotation | `~/side_projects/marketing-agent/marketing-agent-data-source/.agents/skills/annotate` |
| `collect` | repo-agents | `361e92dd787a6073` | 406 | 1.6KB | 2026-05-02 | Run and inspect collection jobs through the marketing data service API. | `~/side_projects/marketing-agent/marketing-agent-data-source/.agents/skills/collect` |
| `fastapi` | repo-agents | `3c8b2bced3222051` | 2597 | 17.3KB | 2026-04-25 | FastAPI best practices and conventions. Use when working with FastAPI APIs and P | `~/side_projects/solprobe/backend/.venv/lib/python3.14/site-packages/fastapi/.agents/skills/fastapi` |
| `next-best-practices` | repo-agents | `b54f75cdf617c6c9` | 1020 | 78.8KB | 2026-05-02 | Next.js best practices - file conventions, RSC boundaries, data patterns, async  | `~/side_projects/solShare/.agents/skills/next-best-practices` |
| `next-best-practices` | repo-agents | `b54f75cdf617c6c9` | 1020 | 4.0KB | 2026-05-02 | Next.js best practices - file conventions, RSC boundaries, data patterns, async  | `~/side_projects/ai-challenge-loan-ref/.agents/skills/next-best-practices` |
| `read-arxiv-paper` | repo-agents | `c252413d274cfd03` | 494 | 1.9KB | 2026-05-02 | Use this skill when asked to read an arxiv paper given an arxiv URL | `~/side_projects/nanochat-solprobe/.agents/skills/read-arxiv-paper` |
| `read-arxiv-paper` | repo-agents | `c252413d274cfd03` | 494 | 1.9KB | 2026-05-02 | Use this skill when asked to read an arxiv paper given an arxiv URL | `~/side_projects/solprobe/.worktrees/nanochat-solprobe/.agents/skills/read-arxiv-paper` |
| `typer` | repo-agents | `dfef816d8d67c991` | 1677 | 6.6KB | 2026-08-09 | Typer best practices and conventions. Use when working with Typer CLIs. Keeps Ty | `~/side_projects/Plugin/.venv/lib/python3.14/site-packages/typer/.agents/skills/typer` |
| `vercel-react-best-practices` | repo-agents | `c19b6fbf4c930c9d` | 1558 | 164.9KB | 2026-05-02 | React and Next.js performance optimization guidelines from Vercel Engineering. T | `~/side_projects/solShare/.agents/skills/vercel-react-best-practices` |
| `vercel-react-best-practices` | repo-agents | `c19b6fbf4c930c9d` | 1558 | 6.1KB | 2026-05-02 | React and Next.js performance optimization guidelines from Vercel Engineering. T | `~/side_projects/ai-challenge-loan-ref/.agents/skills/vercel-react-best-practices` |
| `web-design-guidelines` | repo-agents | `0e0b21f2a066eb64` | 323 | 1.3KB | 2026-05-02 | Review UI code for Web Interface Guidelines compliance. Use when asked to \"revi | `~/side_projects/solShare/.agents/skills/web-design-guidelines` |
| `web-design-guidelines` | repo-agents | `0e0b21f2a066eb64` | 323 | 1.3KB | 2026-05-02 | Review UI code for Web Interface Guidelines compliance. Use when asked to \"revi | `~/side_projects/ai-challenge-loan-ref/.agents/skills/web-design-guidelines` |
| `next-best-practices` | repo-claude | `c78f952d4e98bf36` | 1000 | 78.7KB | 2026-02-07 | Next.js best practices - file conventions, RSC boundaries, data patterns, async  | `~/side_projects/ai-challenge-loan-ref/.claude/skills/next-best-practices` |
| `read-arxiv-paper` | repo-claude | `5b7a49e740abe8aa` | 493 | 1.9KB | 2026-04-26 | Use this skill when asked to read an arxiv paper given an arxiv URL | `~/side_projects/nanochat-solprobe/.claude/skills/read-arxiv-paper` |
| `read-arxiv-paper` | repo-claude | `5b7a49e740abe8aa` | 493 | 1.9KB | 2026-04-26 | Use this skill when asked to read an arxiv paper given an arxiv URL | `~/side_projects/solprobe/.worktrees/nanochat-solprobe/.claude/skills/read-arxiv-paper` |
| `vercel-react-best-practices` | repo-claude | `61860fd4249cf5e5` | 1541 | 164.8KB | 2026-02-07 | React and Next.js performance optimization guidelines from Vercel Engineering. T | `~/side_projects/ai-challenge-loan-ref/.claude/skills/vercel-react-best-practices` |
| `web-design-guidelines` | repo-claude | `f4647ca866a3accf` | 307 | 1.2KB | 2026-02-07 | Review UI code for Web Interface Guidelines compliance. Use when asked to "revie | `~/side_projects/ai-challenge-loan-ref/.claude/skills/web-design-guidelines` |
| `diagram` | repo-skills | `a20c51627954ead4` | 380 | 1.5KB | 2026-07-23 | Produce one-idea diagrams (Mermaid default) under diagrams/; link from the relev | `~/side_projects/research-papers/skills/diagram` |
| `distill-newsletter` | repo-skills | `3dcfa389067515db` | 965 | 3.8KB | 2026-07-28 | Tier-2 distill — read full bodies only for promote verdicts; write teaching brie | `~/side_projects/research-papers/skills/distill-newsletter` |
| `explain-layman` | repo-skills | `f91abe2409eddabd` | 373 | 1.5KB | 2026-07-23 | Plain-language explanation with analogy first, then precise version; persist und | `~/side_projects/research-papers/skills/explain-layman` |
| `explain-math` | repo-skills | `d23be231c4a1d674` | 384 | 1.5KB | 2026-07-23 | Deep-dive an equation or derivation; persist under math/ and link from the page  | `~/side_projects/research-papers/skills/explain-math` |
| `ingest` | repo-skills | `2e5bc61c95ae8b2d` | 538 | 2.1KB | 2026-07-23 | Acquire paper source into source/ and extract/ using the AGENTS.md preference or | `~/side_projects/research-papers/skills/ingest` |
| `newsletter-digest` | repo-skills | `54350515e93ee459` | 434 | 1.7KB | 2026-07-26 | Length-capped digest for a day or backlog window; surfaces paper bridges and can | `~/side_projects/research-papers/skills/newsletter-digest` |
| `process-newsletters` | repo-skills | `8b63dc30e1bfb9fb` | 720 | 2.8KB | 2026-07-27 | Front door — process newsletters from a plain chat phrase; pick lane A/B, resume | `~/side_projects/research-papers/skills/process-newsletters` |
| `promote-concept` | repo-skills | `993d5ed9c5dddcf9` | 568 | 2.2KB | 2026-07-26 | After user approval in concept-candidates.md, create concepts/<slug>.md with evi | `~/side_projects/research-papers/skills/promote-concept` |
| `resume-paper` | repo-skills | `0321370d4e18ebc6` | 437 | 1.7KB | 2026-07-23 | Resume study from folder state alone via the retrieval ladder — never chat histo | `~/side_projects/research-papers/skills/resume-paper` |
| `study-page` | repo-skills | `1f64c2bdae5644d8` | 461 | 1.8KB | 2026-07-23 | Study ONE page or chunk — write page note, update gist, update both SESSION file | `~/side_projects/research-papers/skills/study-page` |
| `study-paper` | repo-skills | `ae0fc60afe9ee4ee` | 585 | 2.3KB | 2026-07-23 | Start studying a NEW paper — ingest, create the paper tree, then enter the page  | `~/side_projects/research-papers/skills/study-paper` |
| `synthesize-papers` | repo-skills | `c1ddd4b1df05a605` | 414 | 1.6KB | 2026-07-23 | Cross-paper synthesis from topic + paper paths — path refs only, never copy arti | `~/side_projects/research-papers/skills/synthesize-papers` |

## Full inventory: Commands

| Name | Source | Hash | Est. tokens | Size | Modified | Description | Location |
|------|--------|------|------------:|------|----------|-------------|----------|
| `continual-learning` | claude | `ce92d024ea0512ac` | 1442 | 5.7KB | 2026-04-04 | /continual-learning — Mine Sessions & Update Memory | `~/.claude/commands/continual-learning.md` |
| `dream` | claude | `c81a7b6025f39c9a` | 1279 | 5.0KB | 2026-04-04 | /dream — Session Reflection & Memory Consolidation | `~/.claude/commands/dream.md` |
| `learn` | claude | `c1bf1cae4368c131` | 1084 | 4.3KB | 2026-04-04 | /learn — Extract Learnings from Session | `~/.claude/commands/learn.md` |
| `recap` | claude | `520efe3b833310eb` | 376 | 1.5KB | 2026-04-04 | /recap — Session Summary | `~/.claude/commands/recap.md` |
| `_conventions` | claude-plugin | `cb10f630e3d615ef` | 739 | 2.9KB | 2026-07-24 | Command Conventions | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/commands/_conventions.md` |
| `add-adapter-method` | claude-plugin | `a74c08d161111c12` | 223 | 895B | 2026-03-14 | Add a new method to both Airflow adapters | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/astro-airflow-mcp/.claude/commands/add-adapter-method.md` |
| `add-tool` | claude-plugin | `b92b02edc9a8066a` | 245 | 980B | 2026-03-14 | Add a new MCP tool to server.py | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/astro-airflow-mcp/.claude/commands/add-tool.md` |
| `bootstrap` | claude-plugin | `e26a1f7aa23a17b5` | 1451 | 5.7KB | 2026-07-24 | Bootstrap a repository with Vercel-linked resources by running preflight checks, | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/commands/bootstrap.md` |
| `campaign-performance` | claude-plugin | `87ec8805db04917e` | 218 | 875B | 2026-03-14 | Analyze ad campaign performance across Google Ads, Meta Ads, LinkedIn Ads, or Ti | `~/.claude/plugins/cache/claude-plugins-official/adspirer-ads-agent/1.1.0/commands/campaign-performance.md` |
| `cancel-ralph` | claude-plugin | `7f3fd4218c50f66d` | 184 | 738B | 2026-04-11 | Cancel active Ralph Loop | `~/.claude/plugins/cache/claude-plugins-official/ralph-loop/1.0.0/commands/cancel-ralph.md` |
| `check-airflow-compat` | claude-plugin | `26df0152ec353311` | 199 | 798B | 2026-03-14 | Verify code works with both Airflow 2.x and 3.x | `~/.claude/plugins/cache/claude-plugins-official/data/0.1.0/astro-airflow-mcp/.claude/commands/check-airflow-compat.md` |
| `clean_gone` | claude-plugin | `4f07fa2ccf4f81a6` | 466 | 1.8KB | 2026-04-11 | Cleans up all git branches marked as [gone] (branches that have been deleted on  | `~/.claude/plugins/cache/claude-plugins-official/commit-commands/unknown/commands/clean_gone.md` |
| `code-review` | claude-plugin | `7d5a0bc9a41babad` | 1852 | 7.2KB | 2026-04-11 | Code review a pull request | `~/.claude/plugins/cache/claude-plugins-official/code-review/unknown/commands/code-review.md` |
| `commit` | claude-plugin | `d1acbc2bf0c50164` | 156 | 624B | 2026-04-11 | Create a git commit | `~/.claude/plugins/cache/claude-plugins-official/commit-commands/unknown/commands/commit.md` |
| `commit-push-pr` | claude-plugin | `3bc3d171939149cb` | 199 | 796B | 2026-04-11 | Commit, push, and open a PR | `~/.claude/plugins/cache/claude-plugins-official/commit-commands/unknown/commands/commit-push-pr.md` |
| `configure` | claude-plugin | `1cb84c1055f6069f` | 714 | 2.8KB | 2026-04-11 | Enable or disable hookify rules interactively | `~/.claude/plugins/cache/claude-plugins-official/hookify/unknown/commands/configure.md` |
| `create-plugin` | claude-plugin | `f2c634dfc0b519db` | 3958 | 15.5KB | 2026-07-24 | Guided end-to-end plugin creation workflow with component design, implementation | `~/.claude/plugins/cache/claude-plugins-official/plugin-dev/unknown/commands/create-plugin.md` |
| `deploy` | claude-plugin | `236ad738d9c57b04` | 1933 | 7.6KB | 2026-07-24 | Deploy the current project to Vercel. Pass "prod" or "production" as argument to | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/commands/deploy.md` |
| `env` | claude-plugin | `cb1a0174593e1e15` | 2230 | 8.8KB | 2026-07-24 | Manage Vercel environment variables. Commands include list, pull, add, remove, a | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/commands/env.md` |
| `feature-dev` | claude-plugin | `652e5d6264fd253f` | 1274 | 5.0KB | 2026-04-11 | Guided feature development with codebase understanding and architecture focus | `~/.claude/plugins/cache/claude-plugins-official/feature-dev/unknown/commands/feature-dev.md` |
| `help` | claude-plugin | `39529a6e6473c45d` | 1155 | 4.5KB | 2026-04-11 | Get help with the hookify plugin | `~/.claude/plugins/cache/claude-plugins-official/hookify/unknown/commands/help.md` |
| `help` | claude-plugin | `51bbd57d6edf8f50` | 806 | 3.2KB | 2026-04-11 | Explain Ralph Loop plugin and available commands | `~/.claude/plugins/cache/claude-plugins-official/ralph-loop/1.0.0/commands/help.md` |
| `hookify` | claude-plugin | `7560e58edc6cbb89` | 1915 | 7.5KB | 2026-04-11 | Create hooks to prevent unwanted behaviors from conversation analysis or explici | `~/.claude/plugins/cache/claude-plugins-official/hookify/unknown/commands/hookify.md` |
| `keyword-research` | claude-plugin | `b6f1fba0d43c6229` | 203 | 813B | 2026-03-14 | Research Google Ads keywords with real CPC data, search volumes, and competition | `~/.claude/plugins/cache/claude-plugins-official/adspirer-ads-agent/1.1.0/commands/keyword-research.md` |
| `list` | claude-plugin | `6128e44611d981e9` | 502 | 2.0KB | 2026-04-11 | List all configured hookify rules | `~/.claude/plugins/cache/claude-plugins-official/hookify/unknown/commands/list.md` |
| `new-sdk-app` | claude-plugin | `d273539c037e63be` | 1961 | 7.7KB | 2026-04-11 | Create and setup a new Claude Agent SDK application | `~/.claude/plugins/cache/claude-plugins-official/agent-sdk-dev/unknown/commands/new-sdk-app.md` |
| `ralph-loop` | claude-plugin | `e15d5d0afe3097f6` | 229 | 916B | 2026-04-11 | Start Ralph Loop in current session | `~/.claude/plugins/cache/claude-plugins-official/ralph-loop/1.0.0/commands/ralph-loop.md` |
| `review-pr` | claude-plugin | `5e70c17293a044e1` | 1249 | 4.9KB | 2026-04-11 | Comprehensive PR review using specialized agents | `~/.claude/plugins/cache/claude-plugins-official/pr-review-toolkit/unknown/commands/review-pr.md` |
| `revise-claude-md` | claude-plugin | `d59ffd7ef2b793bd` | 339 | 1.3KB | 2026-03-14 | Update CLAUDE.md with learnings from this session | `~/.claude/plugins/cache/claude-plugins-official/claude-md-management/1.0.0/commands/revise-claude-md.md` |
| `skill-gen` | claude-plugin | `9f80eed0cfc820ae` | 2278 | 9.0KB | 2026-06-08 | Generate a complete Agent Skill from a documentation URL using Firecrawl | `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/commands/skill-gen.md` |
| `status` | claude-plugin | `975d6a9ea4b8c702` | 2804 | 11.0KB | 2026-07-24 | Show the status of the current Vercel project — recent deployments, linked proje | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/commands/status.md` |

## Full inventory: Agents

| Name | Source | Hash | Est. tokens | Size | Modified | Description | Location |
|------|--------|------|------------:|------|----------|-------------|----------|
| `agent-creator` | claude-plugin | `99ca808b7577e5c4` | 1872 | 7.3KB | 2026-07-24 | Use this agent when the user asks to "create an agent", "generate an agent", "bu | `~/.claude/plugins/cache/claude-plugins-official/plugin-dev/unknown/agents/agent-creator.md` |
| `agent-sdk-verifier-py` | claude-plugin | `a72c50412f4f9940` | 1301 | 5.1KB | 2026-04-11 | Use this agent to verify that a Python Agent SDK application is properly configu | `~/.claude/plugins/cache/claude-plugins-official/agent-sdk-dev/unknown/agents/agent-sdk-verifier-py.md` |
| `agent-sdk-verifier-ts` | claude-plugin | `68fe98341656fa24` | 1354 | 5.3KB | 2026-04-11 | Use this agent to verify that a TypeScript Agent SDK application is properly con | `~/.claude/plugins/cache/claude-plugins-official/agent-sdk-dev/unknown/agents/agent-sdk-verifier-ts.md` |
| `ai-architect` | claude-plugin | `926ec5ae67bbce6a` | 6028 | 24.3KB | 2026-07-24 | Specializes in architecting AI-powered applications on Vercel — choosing between | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/agents/ai-architect.md` |
| `analyzer` | claude-plugin | `bf68f4cac5a56c67` | 2593 | 10.1KB | 2026-04-11 | Post-hoc Analyzer Agent | `~/.claude/plugins/cache/claude-plugins-official/skill-creator/unknown/skills/skill-creator/agents/analyzer.md` |
| `code-architect` | claude-plugin | `c50fb08d59a4bbd1` | 564 | 2.2KB | 2026-04-11 | Designs feature architectures by analyzing existing codebase patterns and conven | `~/.claude/plugins/cache/claude-plugins-official/feature-dev/unknown/agents/code-architect.md` |
| `code-explorer` | claude-plugin | `3b277703de745898` | 527 | 2.1KB | 2026-04-11 | Deeply analyzes existing codebase features by tracing execution paths, mapping a | `~/.claude/plugins/cache/claude-plugins-official/feature-dev/unknown/agents/code-explorer.md` |
| `code-reviewer` | claude-plugin | `a7df173bf77a00da` | 748 | 2.9KB | 2026-04-11 | Reviews code for bugs, logic errors, security vulnerabilities, code quality issu | `~/.claude/plugins/cache/claude-plugins-official/feature-dev/unknown/agents/code-reviewer.md` |
| `code-reviewer` | claude-plugin | `533b9967ca21b7e9` | 995 | 3.9KB | 2026-04-11 | Use this agent when you need to review code for adherence to project guidelines, | `~/.claude/plugins/cache/claude-plugins-official/pr-review-toolkit/unknown/agents/code-reviewer.md` |
| `code-simplifier` | claude-plugin | `2a51e8d210580d9f` | 782 | 3.1KB | 2026-03-14 | Simplifies and refines code for clarity, consistency, and maintainability while  | `~/.claude/plugins/cache/claude-plugins-official/code-simplifier/1.0.0/agents/code-simplifier.md` |
| `code-simplifier` | claude-plugin | `976ddb22b84bc5a7` | 1323 | 5.2KB | 2026-04-11 | Use this agent when code has been written or modified and needs to be simplified | `~/.claude/plugins/cache/claude-plugins-official/pr-review-toolkit/unknown/agents/code-simplifier.md` |
| `comment-analyzer` | claude-plugin | `da0540c507e42a7f` | 1431 | 5.6KB | 2026-04-11 | Use this agent when you need to analyze code comments for accuracy, completeness | `~/.claude/plugins/cache/claude-plugins-official/pr-review-toolkit/unknown/agents/comment-analyzer.md` |
| `comparator` | claude-plugin | `fe1fc9787c495d86` | 1820 | 7.1KB | 2026-04-11 | Blind Comparator Agent | `~/.claude/plugins/cache/claude-plugins-official/skill-creator/unknown/skills/skill-creator/agents/comparator.md` |
| `conversation-analyzer` | claude-plugin | `535ec8acb55b51bf` | 1369 | 5.3KB | 2026-04-11 | Use this agent when analyzing conversation transcripts to find behaviors worth p | `~/.claude/plugins/cache/claude-plugins-official/hookify/unknown/agents/conversation-analyzer.md` |
| `deployment-expert` | claude-plugin | `e88a78d6850bd6c7` | 2934 | 12.0KB | 2026-07-24 | Specializes in Vercel deployment strategies, CI/CD pipelines, preview URLs, prod | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/agents/deployment-expert.md` |
| `grader` | claude-plugin | `57134da0c1a4eea3` | 2257 | 8.8KB | 2026-04-11 | Grader Agent | `~/.claude/plugins/cache/claude-plugins-official/skill-creator/unknown/skills/skill-creator/agents/grader.md` |
| `performance-optimizer` | claude-plugin | `7985b48f2906a760` | 6078 | 24.3KB | 2026-07-24 | Specializes in optimizing Vercel application performance — Core Web Vitals, rend | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/agents/performance-optimizer.md` |
| `plugin-validator` | claude-plugin | `cd1cf892e9d4693a` | 1669 | 6.5KB | 2026-07-24 | Use this agent when the user asks to "validate my plugin", "check plugin structu | `~/.claude/plugins/cache/claude-plugins-official/plugin-dev/unknown/agents/plugin-validator.md` |
| `pr-test-analyzer` | claude-plugin | `d369fd3946a814bb` | 1246 | 4.9KB | 2026-04-11 | Use this agent when you need to review a pull request for test coverage quality  | `~/.claude/plugins/cache/claude-plugins-official/pr-review-toolkit/unknown/agents/pr-test-analyzer.md` |
| `README` | claude-plugin | `bad00ece86a76f43` | 1789 | 7.0KB | 2026-07-24 | AI Agents | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/front_end/models/ai_assistance/agents/README.md` |
| `silent-failure-hunter` | claude-plugin | `fa9b0daec5a267e7` | 1951 | 7.6KB | 2026-04-11 | Use this agent when reviewing code changes in a pull request to identify silent  | `~/.claude/plugins/cache/claude-plugins-official/pr-review-toolkit/unknown/agents/silent-failure-hunter.md` |
| `skill-reviewer` | claude-plugin | `78ac55ddc31dd782` | 1533 | 6.0KB | 2026-07-24 | Use this agent when the user has created or modified a skill and needs quality r | `~/.claude/plugins/cache/claude-plugins-official/plugin-dev/unknown/agents/skill-reviewer.md` |
| `type-design-analyzer` | claude-plugin | `ad160052fd3b4539` | 1342 | 5.2KB | 2026-04-11 | Use this agent when you need expert analysis of type design in your codebase. Sp | `~/.claude/plugins/cache/claude-plugins-official/pr-review-toolkit/unknown/agents/type-design-analyzer.md` |
| `git-diff-test-writer` | repo | `5871ef8145840c4b` | 1987 | 7.8KB | 2026-01-18 | Use this agent when you need automated code review and test generation for chang | `~/side_projects/solShare/.claude/agents/git-diff-test-writer.md` |

## Full inventory: Rules & configs

| Name | Kind | Source | Hash | Est. tokens | Size | Modified | Location |
|------|------|--------|------|------------:|------|----------|----------|
| `config.toml` | config | codex | `80343495fcff1108` | 1564 | 6.1KB | 2026-08-07 | `~/.codex/config.toml` |
| `settings.json` | config | cursor-app | `332b0f1f8e1366ee` | 0 | 311B | 2026-08-08 | `~/Library/Application Support/Cursor/User/settings.json` |
| `CLAUDE.md` | rule | claude | `afa17a2f1a2ee71b` | 1276 | 5.0KB | 2026-04-04 | `~/.claude/CLAUDE.md` |
| `AGENTS.md` | rule | codex | `9cd766948f6ce7e4` | 1501 | 5.9KB | 2026-05-26 | `~/.codex/AGENTS.md` |
| `AGENTS.md` | rule | global | `afa17a2f1a2ee71b` | 1276 | 5.0KB | 2026-04-04 | `~/AGENTS.md` |
| `AGENTS.md` | rule | repo | `1f4e180eafdf782e` | 2494 | 10.0KB | 2026-07-29 | `~/side_projects/research-papers/AGENTS.md` |
| `AGENTS.md` | rule | repo | `766b72fb0df4bbb9` | 5134 | 20.4KB | 2026-05-02 | `~/side_projects/ai-challenge-loan-ref/AGENTS.md` |
| `AGENTS.md` | rule | repo | `e6baef37283039e1` | 1224 | 4.8KB | 2026-06-20 | `~/side_projects/solprobe/AGENTS.md` |
| `AGENTS.md` | rule | repo | `be5257266331a38b` | 20417 | 79.8KB | 2026-02-02 | `~/side_projects/solShare/.agents/skills/vercel-react-best-practices/AGENTS.md` |
| `AGENTS.md` | rule | repo | `be5257266331a38b` | 20417 | 79.8KB | 2026-02-07 | `~/side_projects/ai-challenge-loan-ref/.claude/skills/vercel-react-best-practices/AGENTS.md` |
| `AGENTS.md` | rule | repo | `eb2509c7bfa22a61` | 1102 | 4.3KB | 2026-02-18 | `~/side_projects/marketing-agent/marketing-agent-data-source/AGENTS.md` |
| `CLAUDE.md` | rule | repo | `6006af7c30b99a65` | 43 | 175B | 2026-07-23 | `~/side_projects/research-papers/CLAUDE.md` |
| `CLAUDE.md` | rule | repo | `b3b714be60df130d` | 5088 | 20.3KB | 2026-02-14 | `~/side_projects/ai-challenge-loan-ref/CLAUDE.md` |
| `CLAUDE.md` | rule | repo | `e6baef37283039e1` | 1224 | 4.8KB | 2026-06-20 | `~/side_projects/solprobe/CLAUDE.md` |
| `CLAUDE.md` | rule | repo | `eb2509c7bfa22a61` | 1102 | 4.3KB | 2026-02-18 | `~/side_projects/marketing-agent/marketing-agent-data-source/CLAUDE.md` |

## Exact duplicates (same content hash)

Found **68** groups.

### 1. `brainstorming` — 3 copies — hash `e14914605f640e08`

- Est. tokens (each): **2598**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/brainstorming`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/brainstorming`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/brainstorming`

### 2. `dispatching-parallel-agents` — 3 copies — hash `f0df13f584049059`

- Est. tokens (each): **1654**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/dispatching-parallel-agents`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/dispatching-parallel-agents`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/dispatching-parallel-agents`

### 3. `executing-plans` — 3 copies — hash `bbd8d28bb655a528`

- Est. tokens (each): **647**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/executing-plans`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/executing-plans`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/executing-plans`

### 4. `finishing-a-development-branch` — 3 copies — hash `e6d4a812de900d33`

- Est. tokens (each): **1703**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/finishing-a-development-branch`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/finishing-a-development-branch`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/finishing-a-development-branch`

### 5. `receiving-code-review` — 3 copies — hash `647036bbdab7bf23`

- Est. tokens (each): **1586**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/receiving-code-review`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/receiving-code-review`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/receiving-code-review`

### 6. `requesting-code-review` — 3 copies — hash `1017ccdd5bc61fab`

- Est. tokens (each): **706**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/requesting-code-review`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/requesting-code-review`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/requesting-code-review`

### 7. `subagent-driven-development` — 3 copies — hash `41ab239a6ad1c487`

- Est. tokens (each): **5385**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/subagent-driven-development`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/subagent-driven-development`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/subagent-driven-development`

### 8. `systematic-debugging` — 3 copies — hash `3b20719eca4f0461`

- Est. tokens (each): **2465**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/systematic-debugging`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/systematic-debugging`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/systematic-debugging`

### 9. `test-driven-development` — 3 copies — hash `b5b4717b8b761cce`

- Est. tokens (each): **2471**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/test-driven-development`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/test-driven-development`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/test-driven-development`

### 10. `using-git-worktrees` — 3 copies — hash `e2c3ec142e52868a`

- Est. tokens (each): **1866**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/using-git-worktrees`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/using-git-worktrees`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/using-git-worktrees`

### 11. `using-superpowers` — 3 copies — hash `55379fe7c1c473a0`

- Est. tokens (each): **762**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/using-superpowers`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/using-superpowers`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/using-superpowers`

### 12. `verification-before-completion` — 3 copies — hash `ea52d15aabaf72bc`

- Est. tokens (each): **1037**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/verification-before-completion`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/verification-before-completion`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/verification-before-completion`

### 13. `writing-plans` — 3 copies — hash `272e1af349f5062c`

- Est. tokens (each): **1767**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/writing-plans`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/writing-plans`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/writing-plans`

### 14. `writing-skills` — 3 copies — hash `6b8d08fe863318be`

- Est. tokens (each): **6582**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/writing-skills`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/writing-skills`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/writing-skills`

### 15. `agent-ready-apis` — 2 copies — hash `84d33fc53f753a99`

- Est. tokens (each): **809**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/2050/e351cde6137a98b943c8fb8d0c698c1b438dfc0f/skills/agent-ready-apis`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/postman/f5ea7c56da1dc022753c66ac4fba398e881b07dd/skills/agent-ready-apis`

### 16. `agents-build` — 2 copies — hash `43083e10f5ef8343`

- Est. tokens (each): **2163**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-build`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-build`

### 17. `agents-connect` — 2 copies — hash `ad5a87cb3f64b3a1`

- Est. tokens (each): **7355**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-connect`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-connect`

### 18. `agents-debug` — 2 copies — hash `dd20ab6870ef8552`

- Est. tokens (each): **7732**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-debug`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-debug`

### 19. `agents-deploy` — 2 copies — hash `31f0800e3c31a7e3`

- Est. tokens (each): **1976**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-deploy`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-deploy`

### 20. `agents-get-started` — 2 copies — hash `fab2f8dbccaea9cb`

- Est. tokens (each): **4299**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-get-started`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-get-started`

### 21. `agents-harden` — 2 copies — hash `6207879b9d7637eb`

- Est. tokens (each): **8025**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-harden`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-harden`

### 22. `agents-optimize` — 2 copies — hash `91c53dbdd334010a`

- Est. tokens (each): **913**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-optimize`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-optimize`

### 23. `firecrawl-agent` — 2 copies — hash `9a8badff132ebea1`

- Est. tokens (each): **686**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-agent`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-agent`

### 24. `firecrawl-crawl` — 2 copies — hash `a5ddaae261c6c2c4`

- Est. tokens (each): **672**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-crawl`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-crawl`

### 25. `firecrawl-download` — 2 copies — hash `8c734be50e335cf8`

- Est. tokens (each): **774**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-download`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-download`

### 26. `firecrawl-interact` — 2 copies — hash `f6ddfc4b857a7c54`

- Est. tokens (each): **978**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-interact`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-interact`

### 27. `firecrawl-map` — 2 copies — hash `55e6ea4076bdda1a`

- Est. tokens (each): **533**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-map`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-map`

### 28. `firecrawl-parse` — 2 copies — hash `c6f694cab0dbfddc`

- Est. tokens (each): **678**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-parse`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-parse`

### 29. `firecrawl-scrape` — 2 copies — hash `4fd52e6478dc8964`

- Est. tokens (each): **930**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-scrape`
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-scrape`

### 30. `gsap-core` — 2 copies — hash `3887b47e050ab5af`

- Est. tokens (each): **3669**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-core`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-core`

### 31. `gsap-frameworks` — 2 copies — hash `842d9d3659ec3ddc`

- Est. tokens (each): **2640**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-frameworks`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-frameworks`

### 32. `gsap-performance` — 2 copies — hash `cb5408d6fba707aa`

- Est. tokens (each): **1026**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-performance`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-performance`

### 33. `gsap-plugins` — 2 copies — hash `5838b856c74c07fb`

- Est. tokens (each): **5369**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-plugins`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-plugins`

### 34. `gsap-react` — 2 copies — hash `88e2a5312b45e8cc`

- Est. tokens (each): **1632**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-react`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-react`

### 35. `gsap-scrolltrigger` — 2 copies — hash `9351b6666a4749c0`

- Est. tokens (each): **4574**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-scrolltrigger`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-scrolltrigger`

### 36. `gsap-timeline` — 2 copies — hash `1a8b0f39cc4be3ed`

- Est. tokens (each): **1084**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-timeline`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-timeline`

### 37. `gsap-utils` — 2 copies — hash `1927bcc4ea95b382`

- Est. tokens (each): **3012**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-utils`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-utils`

### 38. `hf-cli` — 2 copies — hash `6cca38f44ab1485a`

- Est. tokens (each): **6752**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/hf-cli`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/hf-cli`

### 39. `hf-mcp` — 2 copies — hash `4cd99f2f6fabc5d8`

- Est. tokens (each): **1241**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/hf-mcp/skills/hf-mcp`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/hf-mcp/skills/hf-mcp`

### 40. `huggingface-best` — 2 copies — hash `156a85e1e6e6c750`

- Est. tokens (each): **1452**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-best`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-best`

### 41. `huggingface-community-evals` — 2 copies — hash `a97f1c703f55b724`

- Est. tokens (each): **1638**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-community-evals`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-community-evals`

### 42. `huggingface-datasets` — 2 copies — hash `eeca50adf211ea64`

- Est. tokens (each): **1141**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-datasets`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-datasets`

### 43. `huggingface-gradio` — 2 copies — hash `ce41c656e364a802`

- Est. tokens (each): **6182**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-gradio`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-gradio`

### 44. `huggingface-llm-trainer` — 2 copies — hash `fb5c5e25103e3822`

- Est. tokens (each): **7165**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-llm-trainer`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-llm-trainer`

### 45. `huggingface-local-models` — 2 copies — hash `814640db1d5f2f27`

- Est. tokens (each): **945**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-local-models`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-local-models`

### 46. `huggingface-lora-space-builder` — 2 copies — hash `208fbefe87f3ce1a`

- Est. tokens (each): **8187**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-lora-space-builder`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-lora-space-builder`

### 47. `huggingface-paper-publisher` — 2 copies — hash `04b59406cf88054a`

- Est. tokens (each): **4172**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-paper-publisher`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-paper-publisher`

### 48. `huggingface-papers` — 2 copies — hash `985c2d5c7261aba2`

- Est. tokens (each): **2337**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-papers`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-papers`

### 49. `huggingface-spaces` — 2 copies — hash `3cbaf778d674e292`

- Est. tokens (each): **3735**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-spaces`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-spaces`

### 50. `huggingface-tool-builder` — 2 copies — hash `2846b591259b134a`

- Est. tokens (each): **1470**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-tool-builder`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-tool-builder`

### 51. `huggingface-trackio` — 2 copies — hash `893ac9695f8677db`

- Est. tokens (each): **1211**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-trackio`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-trackio`

### 52. `huggingface-vision-trainer` — 2 copies — hash `c7aba4de75fa6595`

- Est. tokens (each): **7498**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-vision-trainer`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-vision-trainer`

### 53. `huggingface-zerogpu` — 2 copies — hash `829659aec3422497`

- Est. tokens (each): **4551**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-zerogpu`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-zerogpu`

### 54. `issues` — 2 copies — hash `e27aa511a26c9258`

- Est. tokens (each): **380**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/9345/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf/skills/issues`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/aikido-cursor-plugin/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf/skills/issues`

### 55. `langfuse` — 2 copies — hash `e5312bca7d29ad25`

- Est. tokens (each): **1644**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/738/8e6c2d02accefc0dad3b7d3be3751f7fcc210885/skills/langfuse`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/langfuse/8e6c2d02accefc0dad3b7d3be3751f7fcc210885/skills/langfuse`

### 56. `mintlify` — 2 copies — hash `09cc4ae1e5e41a95`

- Est. tokens (each): **2256**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/25808295/a22550306ff6b704649a8f09faf393e007cbcc1e/skills/mintlify`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/mintlify-cursor-plugin/a22550306ff6b704649a8f09faf393e007cbcc1e/skills/mintlify`

### 57. `next-best-practices` — 2 copies — hash `b54f75cdf617c6c9`

- Est. tokens (each): **1020**
- [repo-agents] `~/side_projects/solShare/.agents/skills/next-best-practices`
- [repo-agents] `~/side_projects/ai-challenge-loan-ref/.agents/skills/next-best-practices`

### 58. `read-arxiv-paper` — 2 copies — hash `5b7a49e740abe8aa`

- Est. tokens (each): **493**
- [repo-claude] `~/side_projects/nanochat-solprobe/.claude/skills/read-arxiv-paper`
- [repo-claude] `~/side_projects/solprobe/.worktrees/nanochat-solprobe/.claude/skills/read-arxiv-paper`

### 59. `read-arxiv-paper` — 2 copies — hash `c252413d274cfd03`

- Est. tokens (each): **494**
- [repo-agents] `~/side_projects/nanochat-solprobe/.agents/skills/read-arxiv-paper`
- [repo-agents] `~/side_projects/solprobe/.worktrees/nanochat-solprobe/.agents/skills/read-arxiv-paper`

### 60. `scan` — 2 copies — hash `4aa2b6cdb6fb72d4`

- Est. tokens (each): **448**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/9345/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf/skills/scan`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/aikido-cursor-plugin/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf/skills/scan`

### 61. `setup` — 2 copies — hash `911501f230d9aeeb`

- Est. tokens (each): **571**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/9345/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf/skills/setup`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/aikido-cursor-plugin/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf/skills/setup`

### 62. `skill-creator` — 2 copies — hash `d57b6e3a44535b9c`

- Est. tokens (each): **4485**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/738/8e6c2d02accefc0dad3b7d3be3751f7fcc210885/.cursor/skills/skill-creator`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/langfuse/8e6c2d02accefc0dad3b7d3be3751f7fcc210885/.cursor/skills/skill-creator`

### 63. `train-sentence-transformers` — 2 copies — hash `8195c00c438d8f57`

- Est. tokens (each): **2229**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/train-sentence-transformers`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/train-sentence-transformers`

### 64. `transformers-js` — 2 copies — hash `241ef15a39acbdf1`

- Est. tokens (each): **6224**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/transformers-js`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/transformers-js`

### 65. `trl-training` — 2 copies — hash `cfdec413fcf030f2`

- Est. tokens (each): **2217**
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/trl-training`
- [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/trl-training`

### 66. `upstream` — 2 copies — hash `c78f952d4e98bf36`

- Est. tokens (each): **1000**
- [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/nextjs/upstream`
- [repo-claude] `~/side_projects/ai-challenge-loan-ref/.claude/skills/next-best-practices`

### 67. `vercel-react-best-practices` — 2 copies — hash `c19b6fbf4c930c9d`

- Est. tokens (each): **1558**
- [repo-agents] `~/side_projects/solShare/.agents/skills/vercel-react-best-practices`
- [repo-agents] `~/side_projects/ai-challenge-loan-ref/.agents/skills/vercel-react-best-practices`

### 68. `web-design-guidelines` — 2 copies — hash `0e0b21f2a066eb64`

- Est. tokens (each): **323**
- [repo-agents] `~/side_projects/solShare/.agents/skills/web-design-guidelines`
- [repo-agents] `~/side_projects/ai-challenge-loan-ref/.agents/skills/web-design-guidelines`

## Same-name skills with divergent content (conflicts / drift)

These share a skill directory/name but have **different** `SKILL.md` hashes — agents may behave differently depending on which harness/plugin loads.

**13** conflict groups (excluding Vercel nested `upstream` stubs).

### `skill-creator` — 4 copies, 3 content variants

- Sources: claude-plugin, codex, cursor-plugin
- hash `d57b6e3a44535b9c` [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/738/8e6c2d02accefc0dad3b7d3be3751f7fcc210885/.cursor/skills/skill-creator`
  - Guide for creating effective skills. This skill should be used when users want to create a new skill (or update an exist
- hash `d57b6e3a44535b9c` [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/langfuse/8e6c2d02accefc0dad3b7d3be3751f7fcc210885/.cursor/skills/skill-creator`
  - Guide for creating effective skills. This skill should be used when users want to create a new skill (or update an exist
- hash `ba8bebb2c0854441` [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/skill-creator/unknown/skills/skill-creator`
  - Create new skills, modify and improve existing skills, and measure skill performance. Use when users want to create a sk
- hash `da44c88f6b3845a8` [codex] `~/.codex/skills/.system/skill-creator`
  - Guide for creating effective skills. This skill should be used when users want to create a new skill (or update an exist

### `postman-knowledge` — 2 copies, 2 content variants

- Sources: cursor-plugin
- hash `0c96b9bcbe40aec5` [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/2050/e351cde6137a98b943c8fb8d0c698c1b438dfc0f/skills/postman-knowledge`
  - Postman concepts and MCP tool guidance. Loaded when working with Postman MCP tools to make better decisions about tool s
- hash `fe498ee9ce4895f5` [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/postman/f5ea7c56da1dc022753c66ac4fba398e881b07dd/skills/postman-knowledge`
  - Postman concepts and MCP tool guidance. Loaded when working with Postman MCP tools to make better decisions about tool s

### `postman-routing` — 2 copies, 2 content variants

- Sources: cursor-plugin
- hash `a71efd98e2c0edef` [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/2050/e351cde6137a98b943c8fb8d0c698c1b438dfc0f/skills/postman-routing`
  - Automatically routes Postman and API-related requests to the correct command. Use when user mentions APIs, collections, 
- hash `e352c68b4c70d222` [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/postman/f5ea7c56da1dc022753c66ac4fba398e881b07dd/skills/postman-routing`
  - Automatically routes Postman and API-related requests to the correct command. Use when user mentions APIs, collections, 

### `mintlify` — 3 copies, 2 content variants

- Sources: claude-plugin, cursor-plugin
- hash `09cc4ae1e5e41a95` [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/25808295/a22550306ff6b704649a8f09faf393e007cbcc1e/skills/mintlify`
  - Comprehensive reference for building Mintlify sites. Use when creating pages, configuring docs.json, adding components, 
- hash `09cc4ae1e5e41a95` [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/mintlify-cursor-plugin/a22550306ff6b704649a8f09faf393e007cbcc1e/skills/mintlify`
  - Comprehensive reference for building Mintlify sites. Use when creating pages, configuring docs.json, adding components, 
- hash `d0a01483e35bd596` [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/mintlify/acd6d2e0128c/skills/mintlify`
  - Comprehensive reference for building Mintlify documentation sites. Use when creating pages, configuring docs.json, addin

### `firecrawl-monitor` — 2 copies, 2 content variants

- Sources: claude-plugin, cursor-plugin
- hash `ff3a9ed21dc1c4e3` [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-monitor`
  - Detect when content on a website changes and get notified by webhook or email — no cron jobs, scrapers, or diff scripts 
- hash `87268056a806cbff` [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-monitor`
  - Detect when content on a website changes and get notified by webhook or email — no cron jobs, scrapers, or diff scripts 

### `firecrawl-cli` — 2 copies, 2 content variants

- Sources: claude-plugin, cursor-plugin
- hash `b02b8467e6f4de9d` [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-cli`
  - Search, scrape, and interact with the web via the Firecrawl CLI. Use this skill whenever the user wants to search the we
- hash `e413e43067ad20e9` [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-cli`
  - Search, scrape, and interact with the web via the Firecrawl CLI. Use this skill whenever the user wants to search the we

### `firecrawl-search` — 2 copies, 2 content variants

- Sources: claude-plugin, cursor-plugin
- hash `8d926a5840e1c9b5` [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-search`
  - Web search with full page content extraction. Use this skill whenever the user asks to search the web, find articles, re
- hash `e946e2b4062da5df` [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-search`
  - Web search with full page content extraction. Use this skill whenever the user asks to search the web, find articles, re

### `shadcn` — 2 copies, 2 content variants

- Sources: claude-plugin, cursor-plugin
- hash `75e9d038e282bb90` [cursor-plugin] `~/.cursor/plugins/cache/cursor-public/shadcn/10f1717a3e2a3c16cfbd43877c1e44063d9d749a/skills/shadcn`
  - Manages shadcn components and projects — adding, searching, fixing, debugging, styling, and composing UI. Provides proje
- hash `4a24abf22a6d10a6` [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/shadcn`
  - shadcn/ui expert guidance — CLI, component installation, composition patterns, custom registries, theming, Tailwind CSS 

### `verification` — 2 copies, 2 content variants

- Sources: claude-plugin
- hash `b167d95f2644801e` [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/verification`
  - MANDATORY: Activate this skill ANY TIME you need to build the project, run tests, or verify code health in DevTools. You
- hash `1c9cdb2b04c479a9` [claude-plugin] `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/verification`
  - Full-story verification — infers what the user is building, then verifies the complete flow end-to-end: browser → API → 

### `web-design-guidelines` — 3 copies, 2 content variants

- Sources: repo-agents, repo-claude
- hash `0e0b21f2a066eb64` [repo-agents] `~/side_projects/solShare/.agents/skills/web-design-guidelines`
  - Review UI code for Web Interface Guidelines compliance. Use when asked to \"review my UI\", \"check accessibility\", \"a
- hash `f4647ca866a3accf` [repo-claude] `~/side_projects/ai-challenge-loan-ref/.claude/skills/web-design-guidelines`
  - Review UI code for Web Interface Guidelines compliance. Use when asked to "review my UI", "check accessibility", "audit 
- hash `0e0b21f2a066eb64` [repo-agents] `~/side_projects/ai-challenge-loan-ref/.agents/skills/web-design-guidelines`
  - Review UI code for Web Interface Guidelines compliance. Use when asked to \"review my UI\", \"check accessibility\", \"a

### `next-best-practices` — 3 copies, 2 content variants

- Sources: repo-agents, repo-claude
- hash `b54f75cdf617c6c9` [repo-agents] `~/side_projects/solShare/.agents/skills/next-best-practices`
  - Next.js best practices - file conventions, RSC boundaries, data patterns, async APIs, metadata, error handling, route ha
- hash `c78f952d4e98bf36` [repo-claude] `~/side_projects/ai-challenge-loan-ref/.claude/skills/next-best-practices`
  - Next.js best practices - file conventions, RSC boundaries, data patterns, async APIs, metadata, error handling, route ha
- hash `b54f75cdf617c6c9` [repo-agents] `~/side_projects/ai-challenge-loan-ref/.agents/skills/next-best-practices`
  - Next.js best practices - file conventions, RSC boundaries, data patterns, async APIs, metadata, error handling, route ha

### `vercel-react-best-practices` — 3 copies, 2 content variants

- Sources: repo-agents, repo-claude
- hash `c19b6fbf4c930c9d` [repo-agents] `~/side_projects/solShare/.agents/skills/vercel-react-best-practices`
  - React and Next.js performance optimization guidelines from Vercel Engineering. This skill should be used when writing, r
- hash `61860fd4249cf5e5` [repo-claude] `~/side_projects/ai-challenge-loan-ref/.claude/skills/vercel-react-best-practices`
  - React and Next.js performance optimization guidelines from Vercel Engineering. This skill should be used when writing, r
- hash `c19b6fbf4c930c9d` [repo-agents] `~/side_projects/ai-challenge-loan-ref/.agents/skills/vercel-react-best-practices`
  - React and Next.js performance optimization guidelines from Vercel Engineering. This skill should be used when writing, r

### `read-arxiv-paper` — 4 copies, 2 content variants

- Sources: repo-agents, repo-claude
- hash `5b7a49e740abe8aa` [repo-claude] `~/side_projects/nanochat-solprobe/.claude/skills/read-arxiv-paper`
  - Use this skill when asked to read an arxiv paper given an arxiv URL
- hash `c252413d274cfd03` [repo-agents] `~/side_projects/nanochat-solprobe/.agents/skills/read-arxiv-paper`
  - Use this skill when asked to read an arxiv paper given an arxiv URL
- hash `5b7a49e740abe8aa` [repo-claude] `~/side_projects/solprobe/.worktrees/nanochat-solprobe/.claude/skills/read-arxiv-paper`
  - Use this skill when asked to read an arxiv paper given an arxiv URL
- hash `c252413d274cfd03` [repo-agents] `~/side_projects/solprobe/.worktrees/nanochat-solprobe/.agents/skills/read-arxiv-paper`
  - Use this skill when asked to read an arxiv paper given an arxiv URL

## Near-duplicate / overlapping functionality

### Superpowers installed on both Cursor and Claude

The `superpowers` plugin appears in:
- Cursor: `~/.cursor/plugins/cache/cursor-public/superpowers/` **and** numeric alias `684/`
- Claude: `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/`

All 14 core superpowers skills are exact content matches across these three trees → triple redundancy in cache.

### Claude slash commands ↔ `~/.agents/skills/source-command-*`

| Claude command | Agents skill wrapper |
|---------------|----------------------|
| `/continual-learning` (`~/.claude/commands/continual-learning.md`) | `source-command-continual-learning` |
| `/dream` (`~/.claude/commands/dream.md`) | `source-command-dream` |
| `/learn` (`~/.claude/commands/learn.md`) | `source-command-continual-learning` |
| `/learn` (`~/.claude/commands/learn.md`) | `source-command-learn` |
| `/recap` (`~/.claude/commands/recap.md`) | `source-command-recap` |
These are intentional bridges for Codex/agents, but count as functional duplicates if both harnesses load them.

### Review skills overlap

| Theme | Locations |
|-------|-----------|
| Bugbot review | Cursor `skills-cursor/review-bugbot`; Cursor plugin ecosystems |
| Security review | Cursor `skills-cursor/review-security` |
| Generic review | Cursor `skills-cursor/review`; Codex `.system/review-agent` |
| Code review workflow | Superpowers `requesting-code-review` / `receiving-code-review` |

### Skill-creator overlap

Three distinct `skill-creator` implementations:
1. Cursor/Langfuse plugin cache
2. Claude `skill-creator` plugin
3. Codex `.system/skill-creator`

### Firecrawl / Playwright / Chrome DevTools / Discord

Present in both Cursor plugin cache and Claude plugin cache (and sometimes `-inline` Claude copies). Content often drifted between harnesses (see conflicts).

### Repo-local Vercel skill packs

`solShare` and `ai-challenge-loan-ref` both carry overlapping Next/React/web-design skills under `.agents/skills` and/or `.claude/skills`, with hash drift between copies.

## Orphaned / likely-unused candidates

_Heuristic only — "referenced nowhere" is hard without full prompt/session telemetry. These are high-likelihood cleanup candidates._

| Item | Reason | Location |
|------|--------|----------|
| `postman-knowledge` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/2050/e351cde6137a98b943c8fb8d0c698c1b438dfc0f/skills/postman-knowledge` |
| `agent-ready-apis` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/2050/e351cde6137a98b943c8fb8d0c698c1b438dfc0f/skills/agent-ready-apis` |
| `postman-routing` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/2050/e351cde6137a98b943c8fb8d0c698c1b438dfc0f/skills/postman-routing` |
| `mintlify` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/25808295/a22550306ff6b704649a8f09faf393e007cbcc1e/skills/mintlify` |
| `agents-debug` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-debug` |
| `agents-build` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-build` |
| `agents-harden` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-harden` |
| `agents-get-started` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-get-started` |
| `agents-deploy` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-deploy` |
| `agents-optimize` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-optimize` |
| `agents-connect` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-connect` |
| `using-git-worktrees` | Located in git worktree; likely duplicate of main repo skill | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/using-git-worktrees` |
| `test-driven-development` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/test-driven-development` |
| `systematic-debugging` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/systematic-debugging` |
| `using-superpowers` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/using-superpowers` |
| `dispatching-parallel-agents` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/dispatching-parallel-agents` |
| `executing-plans` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/executing-plans` |
| `finishing-a-development-branch` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/finishing-a-development-branch` |
| `brainstorming` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/brainstorming` |
| `writing-plans` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/writing-plans` |
| `requesting-code-review` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/requesting-code-review` |
| `receiving-code-review` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/receiving-code-review` |
| `writing-skills` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/writing-skills` |
| `verification-before-completion` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/verification-before-completion` |
| `subagent-driven-development` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/subagent-driven-development` |
| `gsap-plugins` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-plugins` |
| `gsap-timeline` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-timeline` |
| `gsap-scrolltrigger` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-scrolltrigger` |
| `gsap-performance` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-performance` |
| `gsap-core` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-core` |
| `gsap-utils` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-utils` |
| `gsap-frameworks` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-frameworks` |
| `gsap-react` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-react` |
| `huggingface-papers` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-papers` |
| `huggingface-local-models` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-local-models` |
| `huggingface-llm-trainer` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-llm-trainer` |
| `huggingface-lora-space-builder` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-lora-space-builder` |
| `transformers-js` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/transformers-js` |
| `huggingface-community-evals` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-community-evals` |
| `trl-training` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/trl-training` |
| `huggingface-zerogpu` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-zerogpu` |
| `huggingface-spaces` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-spaces` |
| `huggingface-paper-publisher` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-paper-publisher` |
| `hf-cli` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/hf-cli` |
| `train-sentence-transformers` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/train-sentence-transformers` |
| `huggingface-vision-trainer` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-vision-trainer` |
| `huggingface-tool-builder` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-tool-builder` |
| `huggingface-trackio` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-trackio` |
| `huggingface-best` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-best` |
| `huggingface-gradio` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-gradio` |
| `huggingface-datasets` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-datasets` |
| `hf-mcp` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/hf-mcp/skills/hf-mcp` |
| `langfuse` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/738/8e6c2d02accefc0dad3b7d3be3751f7fcc210885/skills/langfuse` |
| `skill-creator` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/738/8e6c2d02accefc0dad3b7d3be3751f7fcc210885/.cursor/skills/skill-creator` |
| `firecrawl` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/789/80ce444eb020b5f41b34836c553f162d6113cd6f/skills/firecrawl` |
| `setup` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/9345/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf/skills/setup` |
| `scan` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/9345/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf/skills/scan` |
| `issues` | Cursor plugin numeric cache alias; duplicate of named plugin path | `~/.cursor/plugins/cache/cursor-public/9345/e1bd1dfee0ee0d890d51a0024b0ee7dee99fd3bf/skills/issues` |
| `using-git-worktrees` | Located in git worktree; likely duplicate of main repo skill | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/using-git-worktrees` |
| `using-git-worktrees` | Located in git worktree; likely duplicate of main repo skill | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/using-git-worktrees` |
| `source-command-continual-learning` | Migrated wrapper for Claude slash command; may be unused if commands still live in ~/.claude/commands | `~/.agents/skills/source-command-continual-learning` |
| `source-command-dream` | Migrated wrapper for Claude slash command; may be unused if commands still live in ~/.claude/commands | `~/.agents/skills/source-command-dream` |
| `source-command-learn` | Migrated wrapper for Claude slash command; may be unused if commands still live in ~/.claude/commands | `~/.agents/skills/source-command-learn` |
| `source-command-recap` | Migrated wrapper for Claude slash command; may be unused if commands still live in ~/.claude/commands | `~/.agents/skills/source-command-recap` |
| `read-arxiv-paper` | Located in git worktree; likely duplicate of main repo skill | `~/side_projects/solprobe/.worktrees/nanochat-solprobe/.claude/skills/read-arxiv-paper` |
| `read-arxiv-paper` | Located in git worktree; likely duplicate of main repo skill | `~/side_projects/solprobe/.worktrees/nanochat-solprobe/.agents/skills/read-arxiv-paper` |
| `~/.cursor/agents/` | Directory exists but is empty | `~/.cursor/agents/` |
| `~/.cursor/plugins/local/` | Empty local plugins dir | `~/.cursor/plugins/local/` |
| Claude `*-inline` plugin caches | Often install/staging mirrors of official plugins | see Claude plugin table |

## Oversized skills (high token cost)

Primary `SKILL.md` estimated ≥ 4,000 tokens. Loading these eagerly is expensive.

| Est. tokens | Size | Source | Name | Location |
|------------:|------|--------|------|----------|
| 8828 | 34.7KB | cursor | `automate` | `~/.cursor/skills-cursor/automate` |
| 8519 | 686.8KB | claude-plugin | `figma-use` | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-use` |
| 8276 | 38.7KB | claude-plugin | `figma-generate-design` | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-generate-design` |
| 8187 | 105.4KB | cursor-plugin | `huggingface-lora-space-builder` | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-lora-space-builder` |
| 8187 | 105.4KB | cursor-plugin | `huggingface-lora-space-builder` | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-lora-space-builder` |
| 8047 | 218.6KB | claude-plugin | `skill-creator` | `~/.claude/plugins/cache/claude-plugins-official/skill-creator/unknown/skills/skill-creator` |
| 8025 | 45.0KB | cursor-plugin | `agents-harden` | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-harden` |
| 8025 | 45.0KB | cursor-plugin | `agents-harden` | `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-harden` |
| 7983 | 67.9KB | claude-plugin | `workflow` | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/workflow` |
| 7732 | 36.2KB | cursor-plugin | `agents-debug` | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-debug` |
| 7732 | 36.2KB | cursor-plugin | `agents-debug` | `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-debug` |
| 7498 | 196.8KB | cursor-plugin | `huggingface-vision-trainer` | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-vision-trainer` |
| 7498 | 196.8KB | cursor-plugin | `huggingface-vision-trainer` | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-vision-trainer` |
| 7355 | 38.7KB | cursor-plugin | `agents-connect` | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-connect` |
| 7355 | 38.7KB | cursor-plugin | `agents-connect` | `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-connect` |
| 7165 | 181.5KB | cursor-plugin | `huggingface-llm-trainer` | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-llm-trainer` |
| 7165 | 181.5KB | cursor-plugin | `huggingface-llm-trainer` | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-llm-trainer` |
| 6833 | 101.7KB | claude-plugin | `generate-project-plan` | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/workflow-skills/generate-project-plan` |
| 6752 | 26.7KB | cursor-plugin | `hf-cli` | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/hf-cli` |
| 6752 | 26.7KB | cursor-plugin | `hf-cli` | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/hf-cli` |
| 6582 | 104.8KB | cursor-plugin | `writing-skills` | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/writing-skills` |
| 6582 | 104.8KB | cursor-plugin | `writing-skills` | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/writing-skills` |
| 6582 | 104.8KB | claude-plugin | `writing-skills` | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/writing-skills` |
| 6370 | 57.7KB | claude-plugin | `figma-code-connect` | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-code-connect` |
| 6224 | 93.1KB | cursor-plugin | `transformers-js` | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/transformers-js` |
| 6224 | 93.1KB | cursor-plugin | `transformers-js` | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/transformers-js` |
| 6182 | 38.3KB | cursor-plugin | `huggingface-gradio` | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-gradio` |
| 6182 | 38.3KB | cursor-plugin | `huggingface-gradio` | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-gradio` |
| 5990 | 131.8KB | codex | `imagegen` | `~/.codex/skills/.system/imagegen` |
| 5903 | 23.2KB | claude-plugin | `ai-gateway` | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/ai-gateway` |
| 5792 | 66.7KB | claude-plugin | `figma-implement-motion` | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-implement-motion` |
| 5669 | 231.7KB | claude-plugin | `figma-generate-library` | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-generate-library` |
| 5633 | 33.6KB | claude-plugin | `skill-development` | `~/.claude/plugins/cache/claude-plugins-official/plugin-dev/unknown/skills/skill-development` |
| 5517 | 21.7KB | claude-plugin | `vercel-functions` | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/vercel-functions` |
| 5510 | 21.6KB | cursor-plugin | `firecrawl` | `~/.cursor/plugins/cache/cursor-public/789/80ce444eb020b5f41b34836c553f162d6113cd6f/skills/firecrawl` |
| 5474 | 61.9KB | codex | `skill-creator` | `~/.codex/skills/.system/skill-creator` |
| 5473 | 67.9KB | claude-plugin | `figma-use-slides` | `~/.claude/plugins/cache/claude-plugins-official/figma/2.2.81/skills/figma-use-slides` |
| 5405 | 188.7KB | claude-plugin | `benchmark-sandbox` | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/.claude/skills/benchmark-sandbox` |
| 5385 | 37.5KB | cursor-plugin | `subagent-driven-development` | `~/.cursor/plugins/cache/cursor-public/684/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/subagent-driven-development` |
| 5385 | 37.5KB | cursor-plugin | `subagent-driven-development` | `~/.cursor/plugins/cache/cursor-public/superpowers/d884ae04edebef577e82ff7c4e143debd0bbec99/skills/subagent-driven-development` |
| 5385 | 37.5KB | claude-plugin | `subagent-driven-development` | `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/skills/subagent-driven-development` |
| 5369 | 21.1KB | cursor-plugin | `gsap-plugins` | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-plugins` |
| 5369 | 21.1KB | cursor-plugin | `gsap-plugins` | `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-plugins` |
| 5125 | 20.1KB | claude-plugin | `vercel-firewall` | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/vercel-firewall` |
| 5007 | 19.6KB | claude-plugin | `shadcn` | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/shadcn` |
| 4947 | 19.4KB | claude-plugin | `vercel-storage` | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/vercel-storage` |
| 4941 | 234.2KB | claude-plugin | `api-gateway` | `~/.claude/plugins/cache/claude-plugins-official/aws-serverless/1.3.0/skills/api-gateway` |
| 4937 | 77.9KB | claude-plugin | `ai-sdk` | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/ai-sdk` |
| 4894 | 19.1KB | cursor | `sdk` | `~/.cursor/skills-cursor/sdk` |
| 4841 | 19.0KB | cursor-plugin | `firecrawl-monitor` | `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-monitor` |
| 4831 | 19.0KB | claude-plugin | `cdn-caching` | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/cdn-caching` |
| 4767 | 18.7KB | claude-plugin | `vercel-connect` | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/vercel-connect` |
| 4765 | 150.5KB | claude-plugin | `command-development` | `~/.claude/plugins/cache/claude-plugins-official/plugin-dev/unknown/skills/command-development` |
| 4674 | 20.8KB | claude-plugin | `upstream` | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/workflow/upstream` |
| 4620 | 82.4KB | cursor-plugin | `shadcn` | `~/.cursor/plugins/cache/cursor-public/shadcn/10f1717a3e2a3c16cfbd43877c1e44063d9d749a/skills/shadcn` |
| 4574 | 18.0KB | cursor-plugin | `gsap-scrolltrigger` | `~/.cursor/plugins/cache/cursor-public/7194/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-scrolltrigger` |
| 4574 | 18.0KB | cursor-plugin | `gsap-scrolltrigger` | `~/.cursor/plugins/cache/cursor-public/gsap-skills/aed9cfd3277740755f6bfc1155c7aa645403b760/skills/gsap-scrolltrigger` |
| 4551 | 33.5KB | cursor-plugin | `huggingface-zerogpu` | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-zerogpu` |
| 4551 | 33.5KB | cursor-plugin | `huggingface-zerogpu` | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-zerogpu` |
| 4485 | 49.0KB | cursor-plugin | `skill-creator` | `~/.cursor/plugins/cache/cursor-public/738/8e6c2d02accefc0dad3b7d3be3751f7fcc210885/.cursor/skills/skill-creator` |
| 4485 | 49.0KB | cursor-plugin | `skill-creator` | `~/.cursor/plugins/cache/cursor-public/langfuse/8e6c2d02accefc0dad3b7d3be3751f7fcc210885/.cursor/skills/skill-creator` |
| 4478 | 190.0KB | claude-plugin | `nextjs` | `~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/nextjs` |
| 4396 | 20.7KB | cursor-plugin | `firecrawl-cli` | `~/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c/skills/firecrawl-cli` |
| 4299 | 23.6KB | cursor-plugin | `agents-get-started` | `~/.cursor/plugins/cache/cursor-public/26098676/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-get-started` |
| 4299 | 23.6KB | cursor-plugin | `agents-get-started` | `~/.cursor/plugins/cache/cursor-public/aws-agents/9ad8fe7d729435ae3788a16fdbf308520ee2b78e/skills/agents-get-started` |
| 4172 | 73.6KB | cursor-plugin | `huggingface-paper-publisher` | `~/.cursor/plugins/cache/cursor-public/735/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-paper-publisher` |
| 4172 | 73.6KB | cursor-plugin | `huggingface-paper-publisher` | `~/.cursor/plugins/cache/cursor-public/huggingface-skills/d7223848c3895fbd447faf2aec73e0a6cdd7fdcd/skills/huggingface-paper-publisher` |
| 4082 | 19.2KB | claude-plugin | `firecrawl-cli` | `~/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9/skills/firecrawl-cli` |
| 4075 | 71.7KB | claude-plugin | `aws-lambda-microvms` | `~/.claude/plugins/cache/claude-plugins-official/aws-serverless/1.3.0/skills/aws-lambda-microvms` |
| 4055 | 62.8KB | claude-plugin | `hook-development` | `~/.claude/plugins/cache/claude-plugins-official/plugin-dev/unknown/skills/hook-development` |

## Broken / invalid format

| Name | Issues | Location |
|------|--------|----------|
| `creating-a-model` | missing name in frontmatter; missing description in frontmatter | `~/.claude/plugins/cache/claude-plugins-official/chrome-devtools-mcp/1.6.0/node_modules/chrome-devtools-frontend/.agents/skills/creating-a-model` |

### Additional format notes

- Many plugin skills use valid YAML frontmatter with `name` + `description` (good).
- Codex `.system` skills are platform-managed; treat as read-only.
- Vercel plugin nests many `upstream/SKILL.md` stubs with name `upstream` — not true user skills; inventory noise.

## Recommendations (DO NOT EXECUTE — report only)

### High impact / low risk
1. **Treat Cursor numeric plugin IDs as aliases** — do not count `684/` and `superpowers/` as separate installs; prune mental model / docs accordingly. Disk reclaim only if Cursor allows cache GC.
2. **Pick one harness for Superpowers** — keep Claude *or* Cursor copy as primary; disable the other to avoid double-injection of the same 14 skills.
3. **Collapse Claude `*-inline` caches** if they are unused mirrors of `claude-plugins-official` equivalents.
4. **Keep `~/.claude/commands/{dream,learn,recap,continual-learning}.md` as canonical**; treat `~/.agents/skills/source-command-*` as Codex bridges only — document that relationship.

### Medium impact
5. **Resolve Firecrawl / Mintlify / Playwright dual installs** — Cursor vs Claude versions have drifted; choose one source of truth per tool.
6. **Deduplicate repo skills** — `ai-challenge-loan-ref` has both `.claude/skills` and `.agents/skills` variants of the same Next/React guidelines; sync or symlink.
7. **Ignore or exclude worktree skill trees** from future audits (`solprobe/.worktrees/...`).
8. **Cap oversized skills** — split `automate`, `figma-*`, HuggingFace trainers, `agents-harden` into progressive disclosure files so the main `SKILL.md` stays <2–3k tokens.

### Structural hygiene
9. Create `~/.cursor/rules/` only if you want user-global Cursor rules; today guidance lives in `~/AGENTS.md` + per-repo `AGENTS.md`/`CLAUDE.md`.
10. Leave Codex `.system` skills alone.
11. Empty `~/.cursor/agents/` can stay or be removed; no agents defined there.
12. Before deleting any cache path, confirm via the product UI which plugins are enabled — cache ≠ enabled.

## Estimated token savings from cleanup

| Cleanup action | Redundant copies | Est. tokens removed from inventory | Notes |
|----------------|-----------------:|-----------------------------------:|-------|
| Dedupe exact-hash skill copies (keep 1 canonical) | 82 | ~209,006 | Mostly cache aliases + cross-harness Superpowers |
| Drop Cursor numeric-alias + worktree skill views | 62 | ~179,856 | No behavior change if named/main paths remain |
| Disable duplicate Superpowers harness install | (subset) | ~31,991 | Avoids loading same skills twice in dual-harness workflows |
| Trim/split top oversized SKILL.md files (target ≤3k tok) | 52 | ~145,651 | Only helps when those skills are actually loaded |

### Practical savings interpretation

- **Inventory tokens** ≠ **per-prompt tokens**. Cache duplicates on disk do not cost tokens until a harness surfaces them to the model.
- The real per-session cost is from: (a) skills listed in the system prompt, (b) skills the agent chooses to read, (c) always-on rules like `~/AGENTS.md`.
- `~/AGENTS.md` alone is ~1276 tokens of always-on guidance.
- Highest ROI for prompt cost: disable unused plugins in Cursor/Claude UI, then slim oversized skills you actually use.

## Cursor built-in skills (`~/.cursor/skills-cursor`)

| Skill | Tokens | Size | Modified | Description |
|-------|-------:|------|----------|-------------|
| `automate` | 8828 | 34.7KB | 2026-07-02 | Use this skill to create Cursor Automations. |
| `autopilot` | 975 | 3.8KB | 2026-08-01 | Keep a PR merge-ready by triaging comments, resolving clear conflicts, and fixing CI in a loop. |
| `canvas` | 2265 | 81.4KB | 2026-08-09 | A Cursor Canvas is a live React app that the user can open beside the chat. You MUST use a canvas wh |
| `create-hook` | 2298 | 9.0KB | 2026-07-02 | Create Cursor hooks. Use when you want to create a hook, write hooks.json, add hook scripts, or auto |
| `create-rule` | 908 | 3.6KB | 2026-07-02 | Create Cursor rules for persistent AI guidance. Use when you want to create a rule, add coding stand |
| `create-skill` | 3580 | 14.1KB | 2026-07-02 | Create Cursor Agent Skills. Use when authoring a new skill or asking about SKILL.md structure. |
| `create-subagent` | 1612 | 6.3KB | 2026-07-02 | Create custom subagents for specialized AI tasks. Use when you want to create a new type of subagent |
| `loop` | 961 | 3.8KB | 2026-07-02 | Run a prompt or skill in this session on a recurring or variable interval (e.g. /loop 5m /foo). |
| `migrate-to-skills` | 1607 | 6.3KB | 2026-07-02 | Convert 'Applied intelligently' Cursor rules (.cursor/rules/*.mdc) and slash commands (.cursor/comma |
| `onboard` | 3059 | 12.0KB | 2026-07-02 | Use /onboard for a focused Cursor onboarding flow that learns basic preferences, picks a first goal, |
| `rename-chat` | 197 | 793B | 2026-08-06 | Rename the current chat to match its focus. Use only when the user invokes /rename-chat. Optional te |
| `review` | 146 | 587B | 2026-07-02 | Review code changes with the Bugbot or Security Review subagent. |
| `review-bugbot` | 1225 | 4.8KB | 2026-07-16 | Review code changes with Bugbot subagent. |
| `review-security` | 939 | 3.7KB | 2026-07-16 | Review code changes with Security Review subagent. |
| `sdk` | 4894 | 19.1KB | 2026-07-02 | Guide users building apps, scripts, CI pipelines, or automations on top of the Cursor SDK - TypeScri |
| `shell` | 216 | 867B | 2026-07-02 | Runs the rest of a /shell request as a literal shell command. Use only when the user explicitly invo |
| `split-to-prs` | 565 | 2.2KB | 2026-07-02 | Split current work into small reviewable PRs. Use when the user asks to split a chat, set of changes |
| `statusline` | 1793 | 7.0KB | 2026-07-02 | Configure a custom status line in the CLI. Use when the user mentions status line, statusline, statu |
| `update-cli-config` | 1156 | 4.6KB | 2026-08-08 | View and modify Cursor CLI configuration settings in ~/.cursor/cli-config.json. Use when the user wa |
| `update-cursor-settings` | 1073 | 4.2KB | 2026-07-02 | Modify Cursor/VSCode user settings in settings.json. Use when you want to change editor settings, pr |

## Codex skills

| Skill | Tokens | Size | Modified | Description |
|-------|-------:|------|----------|-------------|
| `claude-dynamic-workflows` | 1281 | 5.3KB | 2026-06-09 | Coordinate Claude Code dynamic workflows from Codex through a visible tmux session. Use when the use |
| `codex-claude-communication` | 3502 | 14.0KB | 2026-05-26 | Coordinate with Claude Code through a visible shared tmux session. Use when Codex needs Claude as a  |
| `imagegen` | 5990 | 131.8KB | 2026-08-07 | Generate or edit raster images when the task benefits from AI-created bitmap visuals such as photos, |
| `migrate-to-codex` | 1981 | 291.0KB | 2026-05-02 | Migrate supported instruction files, skills, agents, and MCP config into Codex project and global fi |
| `openai-docs` | 1360 | 99.2KB | 2026-08-07 | Use for Codex models/pricing, scheduled tasks, skills, settings, setup, troubleshooting, customizati |
| `playwright` | 942 | 22.3KB | 2026-05-02 | Use when the task requires automating a real browser from the terminal (navigation, form filling, sn |
| `plugin-creator` | 2759 | 64.3KB | 2026-08-07 | Create and scaffold plugin directories for Codex with a required `.codex-plugin/plugin.json`, option |
| `review-agent` | 664 | 2.8KB | 2026-08-07 | Perform a read-only, defect-first review of a specified code change and return every actionable find |
| `skill-creator` | 5474 | 61.9KB | 2026-08-07 | Guide for creating effective skills. This skill should be used when users want to create a new skill |
| `skill-installer` | 841 | 30.0KB | 2026-08-07 | Install Codex skills into $CODEX_HOME/skills from a curated list or a GitHub repo path. Use when a u |

## Shared agents skills (`~/.agents/skills`)

| Skill | Tokens | Size | Modified | Description |
|-------|-------:|------|----------|-------------|
| `source-command-continual-learning` | 1640 | 6.4KB | 2026-05-02 | Run the migrated source command `continual-learning`. |
| `source-command-dream` | 1457 | 5.7KB | 2026-05-02 | Run the migrated source command `dream`. |
| `source-command-learn` | 1231 | 4.8KB | 2026-05-02 | Run the migrated source command `learn`. |
| `source-command-recap` | 523 | 2.1KB | 2026-05-02 | Run the migrated source command `recap`. |

## Repo-embedded skills (side_projects)

Total repo skill instances: **32**

### `Plugin` (1)

- `typer` [repo-agents] hash `dfef816d8d67c991` — 1677 tok — `~/side_projects/Plugin/.venv/lib/python3.14/site-packages/typer/.agents/skills/typer`

### `ai-challenge-loan-ref` (6)

- `next-best-practices` [repo-claude] hash `c78f952d4e98bf36` — 1000 tok — `~/side_projects/ai-challenge-loan-ref/.claude/skills/next-best-practices`
- `next-best-practices` [repo-agents] hash `b54f75cdf617c6c9` — 1020 tok — `~/side_projects/ai-challenge-loan-ref/.agents/skills/next-best-practices`
- `vercel-react-best-practices` [repo-claude] hash `61860fd4249cf5e5` — 1541 tok — `~/side_projects/ai-challenge-loan-ref/.claude/skills/vercel-react-best-practices`
- `vercel-react-best-practices` [repo-agents] hash `c19b6fbf4c930c9d` — 1558 tok — `~/side_projects/ai-challenge-loan-ref/.agents/skills/vercel-react-best-practices`
- `web-design-guidelines` [repo-claude] hash `f4647ca866a3accf` — 307 tok — `~/side_projects/ai-challenge-loan-ref/.claude/skills/web-design-guidelines`
- `web-design-guidelines` [repo-agents] hash `0e0b21f2a066eb64` — 323 tok — `~/side_projects/ai-challenge-loan-ref/.agents/skills/web-design-guidelines`

### `marketing-agent` (5)

- `add-source` [repo-agents] hash `3c8d8076d5e041a7` — 642 tok — `~/side_projects/marketing-agent/marketing-agent-data-source/.agents/skills/add-source`
- `analyze` [repo-agents] hash `8cfeff5193ce3c3d` — 468 tok — `~/side_projects/marketing-agent/marketing-agent-data-source/.agents/skills/analyze`
- `annotate` [repo-agents] hash `ad0efbf83b2188d0` — 624 tok — `~/side_projects/marketing-agent/marketing-agent-data-source/.agents/skills/annotate`
- `collect` [repo-agents] hash `361e92dd787a6073` — 406 tok — `~/side_projects/marketing-agent/marketing-agent-data-source/.agents/skills/collect`
- `traces` [repo] hash `def09caea5219948` — 1491 tok — `~/side_projects/marketing-agent/docs/traces`

### `nanochat-solprobe` (2)

- `read-arxiv-paper` [repo-claude] hash `5b7a49e740abe8aa` — 493 tok — `~/side_projects/nanochat-solprobe/.claude/skills/read-arxiv-paper`
- `read-arxiv-paper` [repo-agents] hash `c252413d274cfd03` — 494 tok — `~/side_projects/nanochat-solprobe/.agents/skills/read-arxiv-paper`

### `research-papers` (12)

- `diagram` [repo-skills] hash `a20c51627954ead4` — 380 tok — `~/side_projects/research-papers/skills/diagram`
- `distill-newsletter` [repo-skills] hash `3dcfa389067515db` — 965 tok — `~/side_projects/research-papers/skills/distill-newsletter`
- `explain-layman` [repo-skills] hash `f91abe2409eddabd` — 373 tok — `~/side_projects/research-papers/skills/explain-layman`
- `explain-math` [repo-skills] hash `d23be231c4a1d674` — 384 tok — `~/side_projects/research-papers/skills/explain-math`
- `ingest` [repo-skills] hash `2e5bc61c95ae8b2d` — 538 tok — `~/side_projects/research-papers/skills/ingest`
- `newsletter-digest` [repo-skills] hash `54350515e93ee459` — 434 tok — `~/side_projects/research-papers/skills/newsletter-digest`
- `process-newsletters` [repo-skills] hash `8b63dc30e1bfb9fb` — 720 tok — `~/side_projects/research-papers/skills/process-newsletters`
- `promote-concept` [repo-skills] hash `993d5ed9c5dddcf9` — 568 tok — `~/side_projects/research-papers/skills/promote-concept`
- `resume-paper` [repo-skills] hash `0321370d4e18ebc6` — 437 tok — `~/side_projects/research-papers/skills/resume-paper`
- `study-page` [repo-skills] hash `1f64c2bdae5644d8` — 461 tok — `~/side_projects/research-papers/skills/study-page`
- `study-paper` [repo-skills] hash `ae0fc60afe9ee4ee` — 585 tok — `~/side_projects/research-papers/skills/study-paper`
- `synthesize-papers` [repo-skills] hash `c1ddd4b1df05a605` — 414 tok — `~/side_projects/research-papers/skills/synthesize-papers`

### `solShare` (3)

- `next-best-practices` [repo-agents] hash `b54f75cdf617c6c9` — 1020 tok — `~/side_projects/solShare/.agents/skills/next-best-practices`
- `vercel-react-best-practices` [repo-agents] hash `c19b6fbf4c930c9d` — 1558 tok — `~/side_projects/solShare/.agents/skills/vercel-react-best-practices`
- `web-design-guidelines` [repo-agents] hash `0e0b21f2a066eb64` — 323 tok — `~/side_projects/solShare/.agents/skills/web-design-guidelines`

### `solprobe` (3)

- `fastapi` [repo-agents] hash `3c8b2bced3222051` — 2597 tok — `~/side_projects/solprobe/backend/.venv/lib/python3.14/site-packages/fastapi/.agents/skills/fastapi`
- `read-arxiv-paper` [repo-claude] hash `5b7a49e740abe8aa` — 493 tok — `~/side_projects/solprobe/.worktrees/nanochat-solprobe/.claude/skills/read-arxiv-paper`
- `read-arxiv-paper` [repo-agents] hash `c252413d274cfd03` — 494 tok — `~/side_projects/solprobe/.worktrees/nanochat-solprobe/.agents/skills/read-arxiv-paper`

## Methodology

- Scanned all paths listed in the audit request, plus discovered alternates (`~/.cursor/skills-cursor`, plugin caches, repo worktrees).
- Content hash = SHA-256 of primary `SKILL.md` (skills/commands) or aggregated text manifests (plugins).
- Token estimate = `len(text) // 4` on the primary markdown file (rough).
- Size = full skill/plugin directory bytes.
- Duplicate detection = exact hash match; conflicts = same skill id, different hash.
- Raw machine-readable dump: `docs/_skills-audit-raw.json`

---

_End of audit. No cleanup was performed._