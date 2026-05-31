# WeChat EXP — Project Instructions

## Project Overview

WeChat EXP (wechat-exp) is a portable Windows tool for extracting, decrypting, and analyzing WeChat 4.x chat records. It features:
- SQLCipher 4 key extraction from Weixin.exe memory
- Batch database decryption (AES-256-CBC)
- Web-based chat viewer (Flask SPA) with avatars, group info, message search
- Annual "Wrapped" report (8 cards, Spotify-Wrapped-style)
- Word cloud, employee export, comprehensive HTML reports
- Backup/export wizards with SSE progress

**Tech Stack**: Python 3.8+, Flask 3.x, SQLite (SQLCipher 4), Vanilla JS (SPA), ECharts

## Key Paths

- Source: `src/`
- Engine services: `src/engine/services/` (chat.py, message.py, media.py)
- Web app: `src/web/app.py` → `src/web/routes/`
- Wrapped cards: `src/web/reports/wrapped/`
- Output: `output/` (decrypted DBs, exports, caches)
- Docs: `docs/` (specs, plans, analysis)

## Continuous Analysis Mode

**When the environment variable `WECHAT_EXP_ANALYSIS=1` is set:**

1. Immediately read `docs/analysis/CONTINUOUS_ANALYSIS.md` to understand current analysis progress
2. Read `docs/analysis/ANALYSIS_PROMPT.md` for the full autonomous analysis protocol
3. Begin executing the analysis loop:
   - Find the first incomplete (unchecked) item in CONTINUOUS_ANALYSIS.md
   - Execute the analysis per ANALYSIS_PROMPT.md
   - Record results immediately
   - Use ScheduleWakeup to schedule the next round
   - Continue until all phases complete
4. Do NOT wait for user input between rounds — self-pace with ScheduleWakeup
5. Report each round's findings concisely

**When the environment variable is NOT set (normal development mode):**
- Respond to user requests normally
- Follow standard coding practices
- Do NOT start autonomous analysis

## Normal Development Mode Guidelines

- Prefer editing existing files over creating new ones
- Follow existing code patterns in the project
- `src/engine/services/` is shared between Web and CLI — keep it that way
- Python imports use flat `from engine.services.xxx import yyy` style
- Frontend JS is vanilla (no framework), split into api.js / app.js / components.js / filters.js
- All API routes return JSON; use Flask Blueprints
- Chinese text in UI, English in code identifiers

## Memory

Project memory is maintained at `C:\Users\dean\.claude\projects\d--perl-wrk\memory\`.
Key memories relevant to this project:
- WeChat 4.x uses SQLCipher 4 with AES-256-CBC
- Message DBs are sharded as message_0.db, message_1.db, etc.
- Group chats stored as xxxxx@chatroom, resolved via 5-level name resolution
- Avatar system uses 3-tier fallback (CDN → cache → SVG default)
