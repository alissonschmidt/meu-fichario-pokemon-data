#!/usr/bin/env python3
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("POKEMON_TCG_DATA_DIR", "/tmp/pokemon-tcg-data"))
SETS_FILE = ROOT / "sets" / "en.json"
CARDS_DIR = ROOT / "cards" / "en"
GENERATOR_PATH = Path(__file__).with_name("populate_featured_cards.py")


def load_generator():
    spec = importlib.util.spec_from_file_location("featured_generator", GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Não foi possível carregar populate_featured_cards.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cards():
    if not SETS_FILE.exists() or not CARDS_DIR.exists():
        raise SystemExit(f"Dataset Pokémon TCG não encontrado em {ROOT}")

    sets = json.loads(SETS_FILE.read_text(encoding="utf-8"))
    set_by_id = {item.get("id"): item for item in sets if item.get("id")}
    cards = []

    for path in sorted(CARDS_DIR.glob("*.json")):
        set_id = path.stem
        set_data = set_by_id.get(set_id)
        if set_data is None:
            print(f"Aviso: coleção {set_id} sem metadados; ignorando {path.name}")
            continue

        batch = json.loads(path.read_text(encoding="utf-8"))
        compact_set = {
            "id": set_data.get("id"),
            "name": set_data.get("name") or set_id,
            "printedTotal": set_data.get("printedTotal"),
            "total": set_data.get("total"),
            "releaseDate": set_data.get("releaseDate"),
        }
        for card in batch:
            card["set"] = compact_set
            cards.append(card)

    print(f"Dataset Pokémon TCG carregado do GitHub: {len(cards)} cartas em {len(set_by_id)} coleções.")
    if not cards:
        raise SystemExit("Nenhuma carta foi carregada do dataset Pokémon TCG.")
    return cards


def main():
    generator = load_generator()
    cards = load_cards()
    generator.all_cards = lambda: cards
    generator.forms_phase()


if __name__ == "__main__":
    main()
