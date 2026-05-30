#!/usr/bin/env python3
"""Extract human-readable descriptions for the bonus effects and traditions/
policies referenced by the Techs and Civics trees.

The Techs/Civics CSVs list their unlocks as opaque IDs such as
``MODIFIER:MOD_AQ_CODEX_REDUX`` and ``KIND_TRADITION:TRADITION_CHARISMATIC_LEADER``.
The app could previously only show a "3 bonus effects" count and bare tradition
names. This script resolves those IDs to plain-English text from the per-Age
gameplay + localization databases under ``sql/`` and writes two lookup CSVs:

    civ7_modifiers.csv    modifier_id, description_clean
    civ7_traditions.csv   tradition_type, name, slot, description_clean

Both are consumed by index.html (Techs and Civics tabs).
"""

import csv
import os
import sqlite3
import sys

from civ7_ability_extractor import clean_description

AGES = ["antiquity", "exploration", "modern"]
SQL_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sql")


def loc_map(age, language="en_US"):
    """Tag -> localized text for one Age's localization DB."""
    path = os.path.join(SQL_ROOT, age, "localization.sqlite")
    con = sqlite3.connect(path)
    try:
        return {
            tag: text
            for tag, text in con.execute(
                "SELECT Tag, Text FROM LocalizedText WHERE Language=?", (language,)
            )
        }
    finally:
        con.close()


# Culture-slot type -> the label players actually see.
SLOT_LABELS = {
    "TRADITION_CULTURE_SLOT": "Tradition",
    "POLICY_CULTURE_SLOT": "Policy",
}


def extract():
    mods = {}      # modifier_id -> description_clean
    traditions = {}  # tradition_type -> {name, slot, description_clean}

    for age in AGES:
        gameplay = os.path.join(SQL_ROOT, age, "gameplay.sqlite")
        if not os.path.exists(gameplay):
            print(f"  (skip {age}: no gameplay.sqlite)", file=sys.stderr)
            continue
        loc = loc_map(age)
        con = sqlite3.connect(gameplay)

        # Modifier descriptions: ModifierStrings holds a 'Description' context
        # pointing at a localization tag for most gameplay modifiers.
        for mid, text_tag in con.execute(
            "SELECT ModifierId, Text FROM ModifierStrings WHERE Context='Description'"
        ):
            if mid in mods:
                continue
            raw = loc.get(text_tag)
            if raw:
                mods[mid] = clean_description(raw)

        # Traditions + Policies: the Traditions table carries a localized Name,
        # Description and the culture-slot type that distinguishes the two.
        for tt, slot, desc_tag, name_tag in con.execute(
            "SELECT TraditionType, CultureSlotType, Description, Name FROM Traditions"
        ):
            if tt in traditions:
                continue
            desc = loc.get(desc_tag, "") if desc_tag else ""
            traditions[tt] = {
                "name": loc.get(name_tag, name_tag),
                "slot": SLOT_LABELS.get(slot, "Tradition"),
                "description_clean": clean_description(desc),
            }
        con.close()

    return mods, traditions


def write_csvs(mods, traditions):
    with open("civ7_modifiers.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["modifier_id", "description_clean"])
        for mid in sorted(mods):
            w.writerow([mid, mods[mid]])

    with open("civ7_traditions.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tradition_type", "name", "slot", "description_clean"])
        for tt in sorted(traditions):
            t = traditions[tt]
            w.writerow([tt, t["name"], t["slot"], t["description_clean"]])


def main():
    mods, traditions = extract()
    write_csvs(mods, traditions)
    print(f"Wrote civ7_modifiers.csv  ({len(mods)} effects)")
    print(f"Wrote civ7_traditions.csv ({len(traditions)} traditions/policies)")


if __name__ == "__main__":
    main()
