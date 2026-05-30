#!/usr/bin/env python3
"""Add a ``description_raw`` column to civ7_unified.csv.

The unified table only stored ``description_clean`` (game markup stripped to
plain words). To render real yield icons the way the Triumphs tab does, the app
needs the original ``[icon:...]`` markup. This backfills that column:

  * Unique Abilities  -> raw text from civ7_abilities.csv, joined on
    (name, ability_name).
  * Self-Syncretism Traditions -> raw Description from the per-Age gameplay
    databases, joined on the localized tradition name.

The new column is inserted right after ``description_clean``; every other
column is preserved untouched.
"""

import csv
import os
import sqlite3

AGES = ["antiquity", "exploration", "modern"]
SQL_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sql")


def loc_map(age, language="en_US"):
    con = sqlite3.connect(os.path.join(SQL_ROOT, age, "localization.sqlite"))
    try:
        return {t: x for t, x in con.execute(
            "SELECT Tag, Text FROM LocalizedText WHERE Language=?", (language,))}
    finally:
        con.close()


def build_ability_raw():
    """(name, ability_name) -> raw ability description, from civ7_abilities.csv."""
    out = {}
    if not os.path.exists("civ7_abilities.csv"):
        return out
    for r in csv.DictReader(open("civ7_abilities.csv")):
        raw = (r.get("ability_description") or "").strip()
        if not raw:
            continue
        out[((r.get("name") or "").strip(),
             (r.get("ability_name") or "").strip())] = raw
    return out


def build_tradition_raw():
    """Localized tradition name -> raw Description, from the gameplay DBs."""
    out = {}
    for age in AGES:
        gameplay = os.path.join(SQL_ROOT, age, "gameplay.sqlite")
        if not os.path.exists(gameplay):
            continue
        loc = loc_map(age)
        con = sqlite3.connect(gameplay)
        for name_tag, desc_tag in con.execute(
                "SELECT Name, Description FROM Traditions"):
            name = loc.get(name_tag, name_tag)
            raw = loc.get(desc_tag, "") if desc_tag else ""
            if name and raw:
                out.setdefault(name, raw)
        con.close()
    return out


def main():
    ability_raw = build_ability_raw()
    tradition_raw = build_tradition_raw()

    rows = list(csv.DictReader(open("civ7_unified.csv")))
    fields = list(rows[0].keys())
    if "description_raw" not in fields:
        idx = fields.index("description_clean") + 1
        fields.insert(idx, "description_raw")

    matched = 0
    for r in rows:
        name = (r.get("name") or "").strip()
        ability = (r.get("ability_or_tradition_name") or "").strip()
        raw = ""
        if r.get("row_type") == "Unique Ability":
            raw = ability_raw.get((name, ability), "")
        elif r.get("row_type") == "Self-Syncretism Tradition":
            raw = tradition_raw.get(ability, "")
        # Fall back to the clean text so the column is never empty.
        r["description_raw"] = raw or (r.get("description_clean") or "")
        if raw:
            matched += 1

    with open("civ7_unified.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"Enriched civ7_unified.csv: {matched}/{len(rows)} rows got raw markup")


if __name__ == "__main__":
    main()
