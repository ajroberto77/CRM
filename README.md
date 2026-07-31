# CRM

A self-hosted, multi-user, modular CRM on PostgreSQL.

- **Contacts and organizations**, derived automatically from an interaction log
  rather than typed in by hand.
- **Scheduling** — calendar sync, LLM extraction of scheduling intent from mail,
  and an approval queue before anything is written.
- **Multiple LLM providers** — ollama, OpenAI, Anthropic, Gemini, Claude Code —
  behind one router with a fallback chain.
- **Signal and Telegram** control, so records can be approved and updated from a
  phone without opening the app.
- **Microsoft 365 and Google** contact sync, with per-field provenance.

## Status

Design complete; implementation not started. This branch carries the
architecture, the project rules, the design system, and the enforcement
subagents. No server code exists yet.

## Documentation

| Document | What it is |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | The operating guide — the five rules, conventions, safety rules. Read first. |
| [`docs/DESIGN.md`](docs/DESIGN.md) | The architecture of record and the reasoning behind it. |
| [`docs/COMPETITIVE-ANALYSIS.md`](docs/COMPETITIVE-ANALYSIS.md) | Evidence base: Twenty, Attio, Salesforce, HubSpot, EspoCRM, SuiteCRM, Odoo, Affinity, Clay. |
| [`docs/SOURCE-PATTERNS.md`](docs/SOURCE-PATTERNS.md) | What is ported from the sibling projects, at which ref, and the production defects that became rules. |

## The founding constraint

**One code base. Similar things are never developed twice.**

Five rules in `CLAUDE.md` make that concrete, and the `no-dupes` subagent
enforces them on every diff:

1. One implementation per capability.
2. Generic core, name-prefixed adapters.
3. Five provider axes, five dispatchers, nothing else branching on provider.
4. One CRUD path — a new entity is a registry entry, not a stack of files.
5. One of each singleton, including exactly one file containing colors.

## Sibling projects

`Cal`, `JA` and `CATO` are **read-only reference**. Nothing is imported or
shared; patterns are ported and then owned here. Read them at the refs listed in
`docs/SOURCE-PATTERNS.md` — `main` is stale or empty in two of the three.

## Planned stack

Python 3.11+ / FastAPI, PostgreSQL (one database, logical schemas), React +
TypeScript + Vite. Deliberately few dependencies: FastAPI/uvicorn, psycopg2,
pgvector.
