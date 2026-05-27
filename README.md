# Civ VII — Leader & Civ Ability Browser

A mobile-friendly single-page app for browsing and comparing leader and civilization abilities in **Civilization VII: Test of Time (v1.4.0)**.

Live at → **https://civ7-abilities.surge.sh**

---

## What it does

Three-tab interface:

| Tab | What you see |
|---|---|
| **Leaders** | Portrait grid of all leaders. Tap to select; the selected leader's ability appears in a strip at the top of the tab so you can compare as you browse. |
| **Civilizations** | Icon grid of all 43 civs with age badges and syncretism markers. Same compare-while-browsing strip at the top. |
| **Abilities** | Full combined view — leader ability, civ unique ability, cross-age traditions, and syncretism options for the chosen pair. |

Selections persist when you switch tabs, so you can freely compare before committing.

---

## Repo contents

| File | Purpose |
|---|---|
| `index.html` | The entire app — HTML, CSS, and JS in one self-contained file. Fetches the two CSV files at runtime. |
| `civ7_unified.csv` | All leader and civ abilities merged into one table (output of the extractor). |
| `civ7_leader_syncretism.csv` | Maps each leader to the civs they can syncretise with. |
| `civ7_ability_extractor.py` | Data pipeline — extracts abilities from the game's SQLite databases (see below). |
| `civ7_abilities.csv` / `.json` | Raw extractor output before post-processing. |
| `civ7_civ_syncretism.csv` | Per-civ syncretism options (raw). |
| `civ7_self_syncretism_traditions.csv` | Cross-age self-syncretism traditions per civ (raw). |

---

## Data pipeline

The app data comes from Civ VII's internal SQLite game databases, not scraping or manual entry.

### 1 — Dump the game databases (one-time)

In `AppOptions.txt` (usually at `%LOCALAPPDATA%\Firaxis Games\Sid Meier's Civilization VII\`) add:

```
CopyDatabasesToDisk 1
```

Launch the game once. It writes per-Age `.sqlite` files to a `Debug/` folder alongside `AppOptions.txt`.

### 2 — Run the extractor

Requires Python 3.9+ and no third-party dependencies.

```bash
# Inspect the schema of your dump first (optional but useful)
python3 civ7_ability_extractor.py --inspect path/to/DebugGameplay.sqlite

# Extract from per-Age dumps
python3 civ7_ability_extractor.py \
    Debug/Antiquity.sqlite \
    Debug/Exploration.sqlite \
    Debug/Modern.sqlite \
    --out civ7_abilities

# Or point at the whole Debug folder
python3 civ7_ability_extractor.py Debug/ --out civ7_abilities
```

Outputs: `civ7_abilities.csv`, `civ7_abilities.json`

### 3 — Post-process into unified CSVs

The extractor outputs are then manually curated and merged into:
- `civ7_unified.csv` — single table consumed by the app (`kind`, `name`, `row_type`, `ability_or_tradition_name`, `age`, `description_clean`, `syncretism_options`)
- `civ7_leader_syncretism.csv` — `leader_name`, `syncretism_civ_name`

> **Note:** The extractor discovers the game schema at runtime, so it tolerates schema changes between patches without hard-coded column names.

---

## Running locally

The app fetches `civ7_unified.csv` and `civ7_leader_syncretism.csv` via `fetch()`, so it needs a local HTTP server (not `file://`):

```bash
cd civ7-ToT-leader-civ-analyser

# Python
python3 -m http.server 8080

# Node
npx serve .
```

Then open `http://localhost:8080`.

---

## Deploying to Surge

The app is hosted on [Surge](https://surge.sh) — free static hosting, no GitHub link.

### First deploy

```bash
npm install -g surge

surge --project /path/to/civ7-ToT-leader-civ-analyser --domain civ7-abilities.surge.sh
# prompts for email + password (creates a free account on first run)
```

Credentials are saved to `~/.netrc` after the first login.

### Redeploy after changes

```bash
surge --project /path/to/civ7-ToT-leader-civ-analyser --domain civ7-abilities.surge.sh
```

### Tear down

```bash
surge teardown civ7-abilities.surge.sh
```

### Account details

| Field | Value |
|---|---|
| Email | josephjcasey@gmail.com |
| Password | Civ7-561b860e64c3 |
| Domain | civ7-abilities.surge.sh |

---

## Image sources

- **Leader portraits** — Civilization Wiki (`static.wikia.nocookie.net`), neutral portrait variants
- **Civ icons** — Civilization Wiki, `[Adjective]_(Civ7).png` emblem files (e.g. `Greek_(Civ7).png`)

Images are hotlinked; fallback initials render if any fail to load.

---

## Notes

- Data is current for **Civ VII v1.4.0 (Test of Time DLC)**. Re-run the extractor after patches.
- The CSV parser handles multi-line quoted fields (RFC 4180), so bullet-separated ability descriptions render fully.
- No build step, no dependencies, no framework — one HTML file and two CSVs.
