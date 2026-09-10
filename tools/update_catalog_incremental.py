#!/usr/bin/env python3
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

CATALOG = Path("card_prices.json")
PROGRESS = Path("catalog_progress.json")
MAX_POKEDEX = 1025
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "8"))
POKEAPI_SPECIES = "https://pokeapi.co/api/v2/pokemon-species/{number}"
LIGA_SEARCH = "https://www.ligapokemon.com.br/?view=cards/search&card={query}"
REQUIRED_SOURCE = "LigaPokemon"


def request_json(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "PokeBinder-CatalogUpdater/6.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def pokemon_id_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def display_name(raw: str) -> str:
    if "-mega" in raw:
        base = raw.split("-")[0].capitalize()
        tail = raw.split("-")[1:]
        suffix = " ".join(t.upper() if t in {"x", "y"} else t.capitalize() for t in tail)
        return f"{suffix} {base}" if suffix.lower().startswith("mega") else f"{base} {suffix}"
    regions = {"alola": "Alola", "galar": "Galar", "hisui": "Hisui", "paldea": "Paldea"}
    for key, label in regions.items():
        if raw.endswith(f"-{key}"):
            return f"{' '.join(p.capitalize() for p in raw[:-len(key)-1].split('-'))} de {label}"
    return " ".join(p.capitalize() for p in raw.split("-") if p)


def form_kind(raw: str, is_default: bool) -> str:
    if is_default:
        return "normal"
    if "-mega" in raw:
        return "mega"
    for region in ("alola", "galar", "hisui", "paldea"):
        if f"-{region}" in raw:
            return region
    return "variant"


def manual_url(name: str) -> str:
    return LIGA_SEARCH.format(query=urllib.parse.quote(name))


def build_targets(species_number: int):
    species = request_json(POKEAPI_SPECIES.format(number=species_number))
    species_name = species.get("name", f"pokemon-{species_number}")
    targets = []
    for variety in species.get("varieties", []):
        poke = variety.get("pokemon", {})
        raw = poke.get("name", "")
        url = poke.get("url", "")
        if not raw or not url:
            continue
        is_default = bool(variety.get("is_default"))
        poke_id = pokemon_id_from_url(url)
        name = species_name.capitalize() if is_default else display_name(raw)
        targets.append({
            "speciesNumber": species_number,
            "name": name,
            "formKind": form_kind(raw, is_default),
            "apiIdentifier": None if is_default else poke_id,
            "manualSearchUrl": manual_url(name),
        })
        shiny_name = f"Shiny {name}"
        targets.append({
            "speciesNumber": species_number,
            "name": shiny_name,
            "formKind": "shiny",
            "apiIdentifier": f"shiny:{poke_id}",
            "manualSearchUrl": manual_url(shiny_name),
        })
    return targets


def key_of(entry):
    return (int(entry.get("speciesNumber", -1)), entry.get("apiIdentifier") or None)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def keep_only_liga_cards(entry):
    cards = [c for c in entry.get("cards", []) if str(c.get("source", "")).lower() == REQUIRED_SOURCE.lower()]
    entry["cards"] = cards[:3]
    if not entry["cards"]:
        entry["lookupStatus"] = "manual_search_required"
    else:
        entry["lookupStatus"] = "found"
    entry["manualSearchUrl"] = entry.get("manualSearchUrl") or manual_url(entry.get("name", "Pokemon"))


def select_species(entries, progress):
    mode = progress.get("mode", "first_pass")
    start = int(progress.get("nextSpeciesNumber", 1) or 1)
    if mode == "first_pass":
        numbers = []
        current = max(1, min(MAX_POKEDEX, start))
        while len(numbers) < BATCH_SIZE:
            numbers.append(current)
            current += 1
            if current > MAX_POKEDEX:
                break
        return mode, numbers

    missing_species = sorted({int(e.get("speciesNumber", -1)) for e in entries if not e.get("cards") and 1 <= int(e.get("speciesNumber", -1)) <= MAX_POKEDEX})
    if not missing_species:
        return "complete", []
    cursor = int(progress.get("missingCursorSpecies", 1) or 1)
    ordered = [n for n in missing_species if n >= cursor] + [n for n in missing_species if n < cursor]
    return "missing_only", ordered[:BATCH_SIZE]


def main():
    catalog = load_json(CATALOG, {"version": 6, "pokemon": []})
    progress = load_json(PROGRESS, {"version": 2, "nextSpeciesNumber": 1, "mode": "first_pass", "totalRuns": 0})
    entries = catalog.setdefault("pokemon", [])

    # Migração definitiva: remove qualquer referência que não seja LigaPokemon.
    for entry in entries:
        keep_only_liga_cards(entry)

    by_key = {key_of(e): e for e in entries}
    mode, numbers = select_species(entries, progress)

    processed = 0
    for species_number in numbers:
        print(f"Processando espécie #{species_number:04d}")
        try:
            targets = build_targets(species_number)
        except Exception as exc:
            print(f"PokeAPI falhou para #{species_number}: {exc}")
            continue

        for target in targets:
            key = (species_number, target["apiIdentifier"])
            existing = by_key.get(key)
            if existing:
                existing["name"] = target["name"]
                existing["formKind"] = target["formKind"]
                existing["manualSearchUrl"] = target["manualSearchUrl"]
                keep_only_liga_cards(existing)
            else:
                new_entry = {
                    "speciesNumber": species_number,
                    "name": target["name"],
                    "formKind": target["formKind"],
                    "cards": [],
                    "lookupStatus": "manual_search_required",
                    "manualSearchUrl": target["manualSearchUrl"],
                }
                if target["apiIdentifier"] is not None:
                    new_entry["apiIdentifier"] = target["apiIdentifier"]
                entries.append(new_entry)
                by_key[key] = new_entry
        processed += 1

    entries.sort(key=lambda e: (int(e.get("speciesNumber", 9999)), e.get("apiIdentifier") or ""))
    filled = sum(1 for e in entries if e.get("cards"))
    total_cards = sum(len(e.get("cards", [])) for e in entries)
    empty = sum(1 for e in entries if not e.get("cards"))

    catalog.update({
        "version": 6,
        "updatedAt": time.strftime("%Y-%m-%d"),
        "priceSource": "LigaPokemon",
        "sources": ["LigaPokemon"],
        "catalogScope": "Catálogo por espécie e forma com referências exclusivamente da LigaPokemon",
        "note": "Até 3 cartas por entrada. Quando não houver referência confirmada, o app oferece busca manual na LigaPokemon.",
        "stats": {"entries": len(entries), "entriesWithCards": filled, "entriesWithoutCards": empty, "cards": total_cards},
    })

    if mode == "first_pass" and numbers:
        last = numbers[-1]
        if last >= MAX_POKEDEX:
            progress["mode"] = "missing_only"
            progress["firstPassCompletedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            progress["missingCursorSpecies"] = 1
            progress["nextSpeciesNumber"] = 1
        else:
            progress["mode"] = "first_pass"
            progress["nextSpeciesNumber"] = last + 1
    elif mode == "missing_only" and numbers:
        progress["mode"] = "missing_only"
        progress["missingCursorSpecies"] = numbers[-1] + 1 if numbers[-1] < MAX_POKEDEX else 1
        progress["nextSpeciesNumber"] = progress["missingCursorSpecies"]
        progress["missingPasses"] = int(progress.get("missingPasses", 0)) + 1
    elif mode == "complete":
        progress["mode"] = "complete"

    progress.update({
        "version": 2,
        "lastRunAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lastBatchSpecies": processed,
        "lastBatchCardsAdded": 0,
        "missingEntriesRemaining": empty,
        "totalRuns": int(progress.get("totalRuns", 0)) + 1,
        "sourcePolicy": "LigaPokemon-only",
    })

    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    PROGRESS.write_text(json.dumps(progress, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Modo: {progress.get('mode')}; espécies processadas: {processed}")
    print(f"LigaPokemon: {filled} entradas preenchidas / {total_cards} cartas; {empty} entradas aguardando busca manual ou referência confirmada")


if __name__ == "__main__":
    main()
