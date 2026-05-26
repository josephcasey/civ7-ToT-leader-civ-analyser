#!/usr/bin/env python3
"""
civ7_ability_extractor.py
=========================

Extract Civilization VII leader & civilization abilities -- including the
Age-scoped variants and modifier chains introduced by the 1.4.0 "Test of Time"
balance pass -- from the game's SQLite database(s) into clean JSON + CSV.

WHY THIS DESIGN
---------------
The game compiles its abilities into SQLite tables, so the data is exact (an
LLM would only guess at the numbers). Because 1.4.0 split data by Age and made
a civ's Unique Ability carry/enhance across Ages, this script merges multiple
per-Age database files and records *which Age* each ability variant came from.

The Civ VII schema is not officially documented and shifts between patches, so
this script DISCOVERS the schema at runtime (it inspects sqlite_master and
PRAGMA table_info) rather than hard-coding table/column names. Run with
`--inspect` first to see exactly what your 1.4.0 dump contains.

RUNNING OFFLINE
---------------
This script does not need the game running. It only reads .sqlite files. The
*creation* of DebugGameplay.sqlite requires one game launch after you set
`CopyDatabasesToDisk 1` in AppOptions.txt -- after that single dump, every run
of this script is fully offline.

USAGE
-----
    # See what tables/columns your dump actually has (do this first):
    python3 civ7_ability_extractor.py --inspect path/to/DebugGameplay.sqlite

    # Extract from one or more Age dumps (point at files or a folder):
    python3 civ7_ability_extractor.py \
        ~/.../Debug/Antiquity.sqlite \
        ~/.../Debug/Exploration.sqlite \
        ~/.../Debug/Modern.sqlite \
        --out civ7_abilities

    # Or just hand it a directory of .sqlite files:
    python3 civ7_ability_extractor.py ~/.../Debug --out civ7_abilities
"""

import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# Tables we look for, with fallbacks. Names are matched case-insensitively and
# the script skips any that are absent, so a schema change won't crash it.
CIV_TABLES = ["Civilizations", "Civilization"]
LEADER_TABLES = ["Leaders", "Leader"]
TRAIT_TABLES = ["Traits", "Trait"]
CIV_TRAIT_LINKS = ["CivilizationTraits", "CivilizationTrait"]
LEADER_TRAIT_LINKS = ["LeaderTraits", "LeaderTrait"]
TRAIT_MOD_LINKS = ["TraitModifiers", "TraitModifier"]
MODIFIER_TABLES = ["Modifiers", "Modifier"]
MOD_ARG_TABLES = ["ModifierArguments", "ModifierArgument"]
LOC_TABLES = ["LocalizedText", "BaseGameText", "Localization"]


# ----------------------------------------------------------------------------
# schema introspection
# ----------------------------------------------------------------------------

def open_db(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def list_tables(con):
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def resolve_table(con, candidates, available=None):
    """Return the first candidate table that exists (case-insensitive)."""
    if available is None:
        available = list_tables(con)
    lower = {t.lower(): t for t in available}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    return None


def columns(con, table):
    try:
        rows = con.execute(f'PRAGMA table_info("{table}")').fetchall()
        return [r["name"] for r in rows]
    except sqlite3.Error:
        return []


def pick_column(cols, *candidates):
    """Find the best-matching column name from a list of preferences."""
    lower = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    # loose contains-match as a last resort
    for cand in candidates:
        for c in cols:
            if cand.lower() in c.lower():
                return c
    return None


# ----------------------------------------------------------------------------
# localization
# ----------------------------------------------------------------------------

def build_loc_map(con, language="en_US"):
    """Map LocalizedText Tag -> Text for the chosen language."""
    table = resolve_table(con, LOC_TABLES)
    if not table:
        return {}
    cols = columns(con, table)
    tag_col = pick_column(cols, "Tag")
    text_col = pick_column(cols, "Text")
    lang_col = pick_column(cols, "Language", "Locale")
    if not (tag_col and text_col):
        return {}

    loc = {}
    if lang_col:
        q = f'SELECT "{tag_col}" AS tag, "{text_col}" AS txt FROM "{table}" WHERE "{lang_col}"=?'
        rows = con.execute(q, (language,)).fetchall()
        if not rows:  # fall back to any language if the chosen one is empty
            rows = con.execute(
                f'SELECT "{tag_col}" AS tag, "{text_col}" AS txt FROM "{table}"'
            ).fetchall()
    else:
        rows = con.execute(
            f'SELECT "{tag_col}" AS tag, "{text_col}" AS txt FROM "{table}"'
        ).fetchall()
    for r in rows:
        if r["tag"] is not None and r["tag"] not in loc:
            loc[r["tag"]] = r["txt"]
    return loc


def localize(loc, tag):
    if tag is None:
        return None
    return loc.get(tag, tag)  # fall back to the raw tag if not localized


# ----------------------------------------------------------------------------
# core extraction
# ----------------------------------------------------------------------------

def fetch_entities(con, table_candidates, type_aliases, loc):
    """Pull entity rows (civs or leaders): {type: {name, description, ...}}."""
    table = resolve_table(con, table_candidates)
    if not table:
        return {}, None
    cols = columns(con, table)
    type_col = pick_column(cols, *type_aliases)
    name_col = pick_column(cols, "Name")
    desc_col = pick_column(cols, "Description", "Capital")
    adj_col = pick_column(cols, "Adjective")
    if not type_col:
        return {}, None

    out = {}
    for r in con.execute(f'SELECT * FROM "{table}"').fetchall():
        etype = r[type_col]
        if etype is None:
            continue
        out[etype] = {
            "type": etype,
            "name": localize(loc, r[name_col]) if name_col else None,
            "description": localize(loc, r[desc_col]) if desc_col else None,
            "adjective": localize(loc, r[adj_col]) if adj_col else None,
        }
    return out, table


def fetch_links(con, table_candidates, left_aliases, right_aliases):
    """Pull a link table as list of (left, right) tuples."""
    table = resolve_table(con, table_candidates)
    if not table:
        return [], None
    cols = columns(con, table)
    left = pick_column(cols, *left_aliases)
    right = pick_column(cols, *right_aliases)
    if not (left and right):
        return [], table
    pairs = []
    for r in con.execute(f'SELECT "{left}" AS l, "{right}" AS r FROM "{table}"').fetchall():
        if r["l"] is not None and r["r"] is not None:
            pairs.append((r["l"], r["r"]))
    return pairs, table


def fetch_traits(con, loc):
    """{trait_type: {name, description}}."""
    table = resolve_table(con, TRAIT_TABLES)
    if not table:
        return {}
    cols = columns(con, table)
    tcol = pick_column(cols, "TraitType", "Type")
    ncol = pick_column(cols, "Name")
    dcol = pick_column(cols, "Description")
    if not tcol:
        return {}
    out = {}
    for r in con.execute(f'SELECT * FROM "{table}"').fetchall():
        t = r[tcol]
        if t is None:
            continue
        out[t] = {
            "trait": t,
            "name": localize(loc, r[ncol]) if ncol else None,
            "description": localize(loc, r[dcol]) if dcol else None,
        }
    return out


def fetch_modifier_args(con):
    """{modifier_id: [{name, value}, ...]} -- the actual numbers behind effects."""
    table = resolve_table(con, MOD_ARG_TABLES)
    if not table:
        return {}
    cols = columns(con, table)
    idcol = pick_column(cols, "ModifierId", "ModifierID")
    ncol = pick_column(cols, "Name")
    vcol = pick_column(cols, "Value")
    if not (idcol and ncol and vcol):
        return {}
    args = defaultdict(list)
    for r in con.execute(
        f'SELECT "{idcol}" AS mid, "{ncol}" AS n, "{vcol}" AS v FROM "{table}"'
    ).fetchall():
        args[r["mid"]].append({"name": r["n"], "value": r["v"]})
    return dict(args)


def fetch_modifier_types(con):
    """{modifier_id: modifier_type} so effects are human-readable."""
    table = resolve_table(con, MODIFIER_TABLES)
    if not table:
        return {}
    cols = columns(con, table)
    idcol = pick_column(cols, "ModifierId", "ModifierID")
    tcol = pick_column(cols, "ModifierType", "Type")
    if not (idcol and tcol):
        return {}
    out = {}
    for r in con.execute(f'SELECT "{idcol}" AS mid, "{tcol}" AS mt FROM "{table}"').fetchall():
        out[r["mid"]] = r["mt"]
    return out


def infer_age(db_path):
    """Guess the Age from the filename; fall back to the file stem."""
    stem = Path(db_path).stem
    for age in ("Antiquity", "Exploration", "Modern", "Atomic", "Future"):
        if age.lower() in stem.lower():
            return age
    return stem


def extract_one(db_path, loc):
    """Extract all entities + abilities from a single database file.

    `loc` is a prebuilt {tag: text} map, merged across every input DB. This
    matters for 1.4.0, where the localized text lives in its own
    `localization-copy.sqlite`, separate from `gameplay-copy.sqlite`.
    """
    age = infer_age(db_path)
    con = open_db(db_path)
    try:
        traits = fetch_traits(con, loc)
        mod_args = fetch_modifier_args(con)
        mod_types = fetch_modifier_types(con)
        trait_mods, _ = fetch_links(con, TRAIT_MOD_LINKS, ["TraitType", "Trait"],
                                    ["ModifierId", "ModifierID"])

        civs, _ = fetch_entities(con, CIV_TABLES, ["CivilizationType", "Type"], loc)
        leaders, _ = fetch_entities(con, LEADER_TABLES, ["LeaderType", "Type"], loc)

        civ_links, _ = fetch_links(con, CIV_TRAIT_LINKS, ["CivilizationType"], ["TraitType"])
        leader_links, _ = fetch_links(con, LEADER_TRAIT_LINKS, ["LeaderType"], ["TraitType"])

        # trait -> [modifier detail]
        mods_by_trait = defaultdict(list)
        for trait, mid in trait_mods:
            mods_by_trait[trait].append({
                "modifier_id": mid,
                "modifier_type": mod_types.get(mid),
                "arguments": mod_args.get(mid, []),
            })

        def assemble(entity_map, links):
            ent_traits = defaultdict(list)
            for ent, trait in links:
                ent_traits[ent].append(trait)
            results = []
            for ent_type, info in entity_map.items():
                abilities = []
                for trait in ent_traits.get(ent_type, []):
                    tinfo = traits.get(trait, {"trait": trait, "name": None, "description": None})
                    abilities.append({
                        "trait": trait,
                        "name": tinfo.get("name"),
                        "description": tinfo.get("description"),
                        "modifiers": mods_by_trait.get(trait, []),
                    })
                results.append({**info, "age": age, "abilities": abilities})
            return results

        return {
            "age": age,
            "source_file": str(db_path),
            "civilizations": assemble(civs, civ_links),
            "leaders": assemble(leaders, leader_links),
        }
    finally:
        con.close()


# ----------------------------------------------------------------------------
# merge across Ages
# ----------------------------------------------------------------------------

def merge_ages(per_age):
    """Merge the per-Age extractions into one dataset, keyed by entity type,
    preserving the Age-scoped ability variants (the 1.4.0 carry/enhance case)."""
    civs = defaultdict(lambda: {"type": None, "name": None, "description": None,
                                "adjective": None, "ages": {}})
    leaders = defaultdict(lambda: {"type": None, "name": None, "ages": {}})

    def fold(bucket, rows):
        for row in rows:
            t = row["type"]
            entry = bucket[t]
            entry["type"] = t
            # keep the first non-null display fields we encounter
            for k in ("name", "description", "adjective"):
                if entry.get(k) is None and row.get(k) is not None:
                    entry[k] = row.get(k)
            entry["ages"][row["age"]] = row["abilities"]

    for age_data in per_age:
        fold(civs, age_data["civilizations"])
        fold(leaders, age_data["leaders"])

    return {
        "civilizations": list(civs.values()),
        "leaders": list(leaders.values()),
    }


# ----------------------------------------------------------------------------
# markup cleaning
# ----------------------------------------------------------------------------

ICON_LABELS = {
    # Yields
    "YIELD_CULTURE":     "Culture",
    "YIELD_GOLD":        "Gold",
    "YIELD_PRODUCTION":  "Production",
    "YIELD_SCIENCE":     "Science",
    "YIELD_FOOD":        "Food",
    "YIELD_HAPPINESS":   "Happiness",
    "YIELD_DIPLOMACY":   "Influence",
    "YIELD_POPULATION":  "Population",
    "YIELD_WAREHOUSE":   "Warehouse",
    "YIELD_ANGRY":       "Unhappiness",
    "YIELD_CITIES":      "Cities",
    "YIELD_TOWNS":       "Towns",
    # Resources / slots
    "RADIAL_RESOURCES":  "Resource Capacity",
    "RADIAL_TECH":       "Technology",
    "RADIAL_CIVICS":     "Civics",
    "SPECIALIST":        "Specialist",
    "SETTLEMENT_LIMIT":  "Settlement Limit",
    "SOCIAL_POLICY":     "Social Policy",
    "TRADITION":         "Tradition",
    "GOVERNMENT":        "Government",
    # City / district
    "CITY_URBAN":         "Urban District",
    "CITY_RURAL":         "Rural District",
    "CITY_BUILDING_LIST": "Building",
    "CITY_FORTIFIED":     "Fortified City",
    "CITY_UNIMPROVED":    "Unimproved Plot",
    "CITY_UNIQUE_QUARTER":"Unique Quarter",
    # Units
    "UNIT_SETTLER":        "Settler",
    "UNIT_COLONIST":       "Colonist",
    "UNIT_MERCHANT":       "Merchant",
    "UNIT_MISSIONARY":     "Missionary",
    "UNIT_ARMY_COMMANDER": "Army Commander",
    "UNIT_TREASURE_FLEET": "Treasure Fleet",
    "UNIT_CAUTION":        "Caution",
    # Military / combat
    "MILITARY":          "Military",
    "WAR":               "War",
    "WAR_SUPPORT":       "War Support",
    "COMMANDER_RADIUS":  "Command Radius",
    # Diplomacy / relations
    "DIPLOMACY":                    "Diplomacy",
    "DIPLOMATIC_ACTION":            "Diplomatic Action",
    "RELATIONSHIP":                 "Relationship",
    "PLAYER_RELATIONSHIP_ALLIANCE": "Alliance",
    "PLAYER_RELATIONSHIP_FRIENDLY": "Friendly",
    "PLAYER_RELATIONSHIP_HELPFUL":  "Helpful",
    "PLAYER_RELATIONSHIP_HOSTILE":  "Hostile",
    "PLAYER_RELATIONSHIP_UNFRIENDLY":"Unfriendly",
    "SANCTIONS":                    "Sanctions",
    "CITYSTATE":                    "City-State",
    "INDEPENDENT_POWER":            "Independent Power",
    # Other gameplay
    "TRADE_ROUTE":        "Trade Route",
    "GROWTH_RATE":        "Growth Rate",
    "GREATWORK":          "Great Work",
    "WONDER":             "Wonder",
    "PROJECT":            "Project",
    "CELEBRATION":        "Celebration",
    "ENDEAVOR":           "Endeavor",
    "ESPIONAGE":          "Espionage",
    "ATTRIBUTE_WILDCARD": "Attribute",
    # Victory points
    "CULTURE_VP":    "Culture Victory Point",
    "MILITARY_VP":   "Military Victory Point",
    "ECONOMIC_VP":   "Economic Victory Point",
    # Narrative rewards (drop silently — they're UI chrome)
    "NAR_REW_COMBAT":        "",
    "NAR_REW_DEFAULT":       "",
    "NAR_REW_GREATWORK":     "",
    "NAR_REW_PROMOTION":     "",
    "NAR_REW_RELIGION":      "",
    "NAR_REW_TRADITION_SLOT":"",
    # Notifications (drop)
    "NOTIFICATION_DISCOVER_NATURAL_WONDER": "",
    "NOTIFICATION_SELECT_CAPITAL":          "",
    # Actions (drop — these are button icons)
    "Action_Heal":    "",
    "Action_Move":    "",
    "Action_Pillage": "",
    "Action_Showall": "",
}

# TIP tooltips: strip the tag wrapper, keep the visible label text
_TIP_RE = re.compile(r'\[TIP:[^\]]+\](.*?)\[/TIP\]', re.DOTALL)
_ICON_RE = re.compile(r'\[icon:([^\]]+)\]')
_TAG_RE  = re.compile(r'\[[^\]]+\]')


def clean_description(text):
    """Return a plain-English version of a game markup description string."""
    if not text:
        return text
    # [TIP:...]label[/TIP] -> keep the visible label
    text = _TIP_RE.sub(r'\1', text)
    # Icons always precede their text label in the UI, so the surrounding text
    # is already self-describing — drop all icons to avoid "(Culture) Culture".
    text = _ICON_RE.sub("", text)
    # [BLIST]/[LIST] -> nothing; [LI] -> bullet
    text = text.replace("[BLIST]", "").replace("[/BLIST]", "")
    text = text.replace("[LIST]", "").replace("[/LIST]", "")
    text = text.replace("[LI]", "\n• ")
    # Bold tags
    text = text.replace("[B]", "").replace("[/B]", "")
    # Any remaining bracketed tags
    text = _TAG_RE.sub("", text)
    # Collapse runs of spaces left behind by removed tags
    text = re.sub(r' {2,}', ' ', text)
    # Normalise whitespace, tidy bullet lines
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# output
# ----------------------------------------------------------------------------

def flatten_for_csv(merged):
    """One row per (entity, age, ability)."""
    rows = []
    for kind, key in (("civilization", "civilizations"), ("leader", "leaders")):
        for ent in merged[key]:
            for age, abilities in ent["ages"].items():
                if not abilities:
                    rows.append({
                        "kind": kind, "type": ent["type"], "name": ent.get("name"),
                        "age": age, "ability_name": None, "ability_trait": None,
                        "ability_description": None, "modifier_type": None,
                        "modifier_args": None,
                    })
                for ab in abilities:
                    mod_types = "; ".join(
                        m.get("modifier_type") or "" for m in ab.get("modifiers", [])
                    ).strip("; ")
                    mod_args = "; ".join(
                        f"{a['name']}={a['value']}"
                        for m in ab.get("modifiers", [])
                        for a in m.get("arguments", [])
                    )
                    rows.append({
                        "kind": kind, "type": ent["type"], "name": ent.get("name"),
                        "age": age, "ability_name": ab.get("name"),
                        "ability_trait": ab.get("trait"),
                        "ability_description": ab.get("description"),
                        "ability_description_clean": clean_description(ab.get("description")),
                        "modifier_type": mod_types or None,
                        "modifier_args": mod_args or None,
                    })
    return rows


def enrich_with_clean(merged):
    """Add clean_description fields to every ability in the merged dataset."""
    for key in ("civilizations", "leaders"):
        for ent in merged[key]:
            for abilities in ent["ages"].values():
                for ab in abilities:
                    ab["description_clean"] = clean_description(ab.get("description"))


def write_outputs(merged, out_base):
    enrich_with_clean(merged)
    json_path = f"{out_base}.json"
    csv_path = f"{out_base}.csv"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    rows = flatten_for_csv(merged)
    fields = ["kind", "type", "name", "age", "ability_name", "ability_trait",
              "ability_description", "ability_description_clean",
              "modifier_type", "modifier_args"]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return json_path, csv_path, len(rows)


# ----------------------------------------------------------------------------
# inspect mode
# ----------------------------------------------------------------------------

def inspect(db_path):
    con = open_db(db_path)
    try:
        tables = list_tables(con)
        print(f"\n=== {db_path} ===")
        print(f"{len(tables)} tables\n")
        # Highlight the tables this script cares about and whether they resolved.
        groups = {
            "Civilizations": CIV_TABLES, "Leaders": LEADER_TABLES,
            "Traits": TRAIT_TABLES, "CivTraitLinks": CIV_TRAIT_LINKS,
            "LeaderTraitLinks": LEADER_TRAIT_LINKS, "TraitModifiers": TRAIT_MOD_LINKS,
            "Modifiers": MODIFIER_TABLES, "ModifierArguments": MOD_ARG_TABLES,
            "LocalizedText": LOC_TABLES,
        }
        print("Resolved key tables:")
        for label, cands in groups.items():
            t = resolve_table(con, cands, tables)
            mark = "OK " if t else "-- "
            cols = ", ".join(columns(con, t)) if t else "(not found)"
            print(f"  [{mark}] {label:<18} -> {t or 'MISSING'}")
            if t:
                print(f"        cols: {cols}")
        print("\nAll tables:")
        for t in tables:
            print(f"  {t}")
    finally:
        con.close()


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def collect_db_files(inputs):
    files = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            files.extend(sorted(p.glob("*.sqlite")))
        elif p.exists():
            files.append(p)
        else:
            print(f"warning: path not found: {p}", file=sys.stderr)
    return files


def main():
    ap = argparse.ArgumentParser(description="Extract Civ VII leader/civ abilities to JSON+CSV.")
    ap.add_argument("inputs", nargs="+", help="SQLite file(s) or a folder of them.")
    ap.add_argument("--out", default="civ7_abilities", help="Output basename (no extension).")
    ap.add_argument("--language", default="en_US", help="Localization language tag.")
    ap.add_argument("--inspect", action="store_true",
                    help="Print schema for each DB and exit (no extraction).")
    args = ap.parse_args()

    db_files = collect_db_files(args.inputs)
    if not db_files:
        print("No database files found.", file=sys.stderr)
        sys.exit(1)

    if args.inspect:
        for db in db_files:
            inspect(db)
        return

    # 1.4.0 splits localized text into its own DB (localization-copy.sqlite),
    # so build ONE localization map by merging LocalizedText from every input
    # file. Entities are then extracted from whichever DB holds the trait data
    # (gameplay-copy.sqlite); files with no civ/leader/trait tables (colors,
    # images, frontend) contribute nothing and are skipped automatically.
    loc = {}
    for db in db_files:
        con = open_db(db)
        try:
            part = build_loc_map(con, args.language)
        finally:
            con.close()
        for k, v in part.items():
            loc.setdefault(k, v)
    if loc:
        print(f"Localization: {len(loc)} strings loaded across {len(db_files)} file(s).")
    else:
        print("Warning: no LocalizedText found; names will appear as raw LOC_ tags.")

    per_age = []
    for db in db_files:
        con = open_db(db)
        try:
            is_entity_db = (resolve_table(con, CIV_TABLES) is not None
                            or resolve_table(con, LEADER_TABLES) is not None
                            or resolve_table(con, TRAIT_TABLES) is not None)
        finally:
            con.close()
        if not is_entity_db:
            continue
        print(f"Extracting: {db}")
        data = extract_one(db, loc)
        if not data["civilizations"] and not data["leaders"]:
            continue
        print(f"  Age '{data['age']}': "
              f"{len(data['civilizations'])} civs, {len(data['leaders'])} leaders")
        per_age.append(data)

    if not per_age:
        print("No civ/leader tables found in any input DB. "
              "Run with --inspect to see the actual schema.", file=sys.stderr)
        sys.exit(1)

    merged = merge_ages(per_age)
    json_path, csv_path, n_rows = write_outputs(merged, args.out)
    print(f"\nDone.")
    print(f"  civilizations: {len(merged['civilizations'])}")
    print(f"  leaders:       {len(merged['leaders'])}")
    print(f"  JSON -> {json_path}")
    print(f"  CSV  -> {csv_path} ({n_rows} ability rows)")


if __name__ == "__main__":
    main()
