# Repository Guidelines

## Project Structure & Module Organization
- `server.js`: Express API and Mongoose models (single-file backend).
- `public/`: Static frontend (Vue + Chart.js) served by Express.
- `.env`: Environment configuration (not committed).
- `package.json`: Scripts and dependencies.
- `README.md`: Quick start.

## Build, Test, and Development Commands
- `npm install`: Install dependencies.
- `npm start`: Run the server on `http://localhost:3000`.
- `npm test`: Placeholder (no tests yet). Update when tests are added.

Examples:
- Health check: `curl http://localhost:3000/api/health`
- Start with custom port: `PORT=4000 npm start`

## Coding Style & Naming Conventions
- Language: Node.js (CommonJS). Indent with 2 spaces.
- Prefer `const`/`let`; use camelCase for variables/functions and UPPER_SNAKE_CASE for env vars.
- Keep route handlers small; extract helpers when logic grows.
- Sorting: group built-in, third-party, local requires.
- Lint/format: none configured; if adding, use ESLint + Prettier with default rules.

## Testing Guidelines
- Frameworks: not set up. Recommended: Jest + Supertest for API and a minimal E2E smoke (health, CRUD).
- Location: create `tests/` (e.g., `tests/expenses.spec.js`).
- Naming: `*.spec.js` for unit/integration.
- Run: update `package.json` to `"test": "jest"` once configured, then `npm test`.

## Commit & Pull Request Guidelines
- Commits: concise, imperative subject (≤72 chars). Examples: `Add category summary endpoint`, `Fix pagination off-by-one`.
- Prefer logical, small commits tied to a single change.
- PRs must include: purpose/summary, before/after behavior, steps to run locally, screenshots (UI), and sample `curl` for new/changed endpoints.
- Link related issues. Request review when CI (if added) is green.

## Security & Configuration Tips
- Required env: `MONGODB_URI`.
- Optional env: `PORT`, `DB_NAME`, `COLLECTION` (default `expenses`), `API_KEY` (enables header auth via `x-api-key`), `FORCE_CHAT_ID`.
- Example `.env`:
  - `MONGODB_URI=mongodb+srv://...`
  - `API_KEY=replace-me`
- When `API_KEY` is set, include `-H 'x-api-key: <key>'` in requests.
- Do not commit `.env` or secrets. Validate inputs on create/update routes and avoid broad `$where` queries.

