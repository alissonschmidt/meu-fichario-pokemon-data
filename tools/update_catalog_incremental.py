#!/usr/bin/env python3
import json
import os
import statistics
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CATALOG = Path("card_prices.json")
PROGRESS = Path("catalog_progress.json")
INDEX = Path("cardtrader_blueprint_index.json")
MAX_POKEDEX = 1025
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "4"))
MAX_MARKETPLACE_CALLS = int(os.getenv("MAX_MARKETPLACE_CALLS", "40"))
MARKETPLACE_DELAY = float(os.getenv("MARKETPLACE_DELAY", "1.05"))
INDEX_REQUEST_DELAY = float(os.getenv("INDEX_REQUEST_DELAY", "0.06"))
INDEX_MAX_AGE_DAYS = int(os.getenv("INDEX_MAX_AGE_DAYS", "7"))
TOKEN = os.getenv("CARDTRADER_TOKEN", "").strip()
API = "https://api.cardtrader.com/api/v2"
POKEAPI_SPECIES = "https://pokeapi.co/api/v2/pokemon-species/{number}"
SOURCE = "CardTrader"
MANUAL_BASE = "https://www.cardtrader.com/en/pokemon"


def api_json(path: str, params=None):
    if not TOKEN:
        raise RuntimeError("CARDTRADER_TOKEN não configurado")
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {TOKEN}",
            "User-Agent": "PokeBinder-CatalogUpdater/8.1",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def public_json(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "PokeBinder/8.1"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize(value) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch)).lower()
    for ch in "'’.:_-()/":
        value = value.replace(ch, " ")
    return " ".join(value.split())


def api_records(payload, label="response"):
    """Normaliza respostas CardTrader em lista de objetos.

    A documentação descreve arrays para games/categories/expansions, mas a API pode
    responder também com wrappers ou mapas indexados por id. Este parser aceita:
    - [ {..}, {..} ]
    - {"data"|"result"|"items"|"resources": [...]}
    - {"1": {..}, "2": {..}}
    - {"1": "Pokemon", "2": "Magic"}
    """
    if isinstance(payload, list):
        result = []
        for item in payload:
            if isinstance(item, dict):
                result.append(item)
            elif isinstance(item, str):
                result.append({"name": item, "display_name": item})
        return result

    if isinstance(payload, dict):
        for key in ("data", "result", "items", "resources", "games", "categories", "expansions", "blueprints"):
            nested = payload.get(key)
            if isinstance(nested, (list, dict)):
                return api_records(nested, f"{label}.{key}")

        result = []
        for key, value in payload.items():
            if isinstance(value, dict):
                item = dict(value)
                if "id" not in item:
                    try:
                        item["id"] = int(key)
                    except (TypeError, ValueError):
                        pass
                result.append(item)
            elif isinstance(value, str):
                item = {"name": value, "display_name": value}
                try:
                    item["id"] = int(key)
                except (TypeError, ValueError):
                    item["key"] = key
                result.append(item)
        if result:
            return result

    raise RuntimeError(f"Formato inesperado da CardTrader em {label}: {type(payload).__name__}")


def display_name(raw: str) -> str:
    if "-mega" in raw:
        parts = raw.split("-")
        base = parts[0].capitalize()
        tail = " ".join(p.upper() if p in {"x", "y"} else p.capitalize() for p in parts[1:])
        return f"{tail} {base}" if tail.lower().startswith("mega") else f"{base} {tail}"
    regions = {"alola": "Alola", "galar": "Galar", "hisui": "Hisui", "paldea": "Paldea"}
    for key, label in regions.items():
        if raw.endswith(f"-{key}"):
            base = raw[: -len(key) - 1]
            return f"{' '.join(p.capitalize() for p in base.split('-'))} de {label}"
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


def pokemon_id_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def build_targets(species_number: int):
    species = public_json(POKEAPI_SPECIES.format(number=species_number))
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
            "rawName": raw,
            "speciesName": species_name,
            "formKind": form_kind(raw, is_default),
            "apiIdentifier": None if is_default else poke_id,
            "manualSearchUrl": MANUAL_BASE,
        })
        targets.append({
            "speciesNumber": species_number,
            "name": f"Shiny {name}",
            "rawName": raw,
            "speciesName": species_name,
            "formKind": "shiny",
            "apiIdentifier": f"shiny:{poke_id}",
            "manualSearchUrl": MANUAL_BASE,
        })
    return targets


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def key_of(entry):
    return (int(entry.get("speciesNumber", -1)), entry.get("apiIdentifier") or None)


def format_money(cents: int, currency: str) -> str:
    amount = cents / 100.0
    symbols = {"USD": "US$", "EUR": "€", "BRL": "R$", "GBP": "£"}
    symbol = symbols.get(currency.upper(), currency.upper())
    return f"{symbol} {amount:,.2f}"


def cache_fresh(payload):
    ts = payload.get("generatedAtEpoch")
    return isinstance(ts, (int, float)) and time.time() - ts < INDEX_MAX_AGE_DAYS * 86400


def find_pokemon_game(games):
    for game in games:
        names = {normalize(game.get("name")), normalize(game.get("display_name"))}
        if "pokemon" in names or "pokémon" in names:
            return game
    return None


def build_blueprint_index():
    print("Construindo índice compacto da CardTrader...")
    games_payload = api_json("/games")
    games = api_records(games_payload, "/games")
    print(f"  /games: {type(games_payload).__name__} -> {len(games)} registro(s)")
    pokemon = find_pokemon_game(games)
    if not pokemon or pokemon.get("id") is None:
        available = [g.get("display_name") or g.get("name") for g in games[:20]]
        raise RuntimeError(f"Jogo Pokémon não localizado na API CardTrader. Jogos recebidos: {available}")
    game_id = int(pokemon["id"])
    print(f"  Pokémon localizado: game_id={game_id}")

    categories_payload = api_json("/categories", {"game_id": game_id})
    categories = api_records(categories_payload, "/categories")
    single_ids = {
        int(c["id"]) for c in categories
        if c.get("id") is not None and "single" in normalize(c.get("name") or c.get("display_name"))
    }
    if not single_ids:
        names = [c.get("name") or c.get("display_name") for c in categories]
        raise RuntimeError(f"Categoria Singles de Pokémon não localizada. Categorias: {names[:30]}")
    print(f"  Categorias Singles: {sorted(single_ids)}")

    expansions_payload = api_json("/expansions")
    expansions_all = api_records(expansions_payload, "/expansions")
    expansions = [e for e in expansions_all if int(e.get("game_id", -1) or -1) == game_id]
    if not expansions:
        raise RuntimeError(f"Nenhuma expansão Pokémon localizada para game_id={game_id}")
    expansion_map = {int(e["id"]): e for e in expansions if e.get("id") is not None}

    by_name = {}
    total = len(expansions)
    for idx, expansion in enumerate(expansions, 1):
        try:
            payload = api_json("/blueprints/export", {"expansion_id": expansion["id"]})
            blueprints = api_records(payload, "/blueprints/export")
        except urllib.error.HTTPError as exc:
            print(f"  Expansão {expansion.get('name')} ignorada: HTTP {exc.code}")
            continue
        except RuntimeError as exc:
            print(f"  Expansão {expansion.get('name')} ignorada: {exc}")
            continue
        for bp in blueprints:
            try:
                bp_game = int(bp.get("game_id", game_id) or game_id)
                bp_category = int(bp.get("category_id", -1) or -1)
            except (TypeError, ValueError):
                continue
            if bp_game != game_id or bp_category not in single_ids:
                continue
            name = str(bp.get("name") or bp.get("name_en") or "").strip()
            if not name or bp.get("id") is None:
                continue
            expansion_id = int(bp.get("expansion_id") or expansion["id"])
            expansion_info = expansion_map.get(expansion_id, expansion)
            item = {
                "id": int(bp["id"]),
                "name": name,
                "expansionId": expansion_id,
                "expansion": expansion_info.get("name"),
                "expansionCode": expansion_info.get("code"),
            }
            by_name.setdefault(normalize(name), []).append(item)
        if idx % 50 == 0:
            print(f"  {idx}/{total} expansões indexadas")
        time.sleep(INDEX_REQUEST_DELAY)

    payload = {
        "version": 2,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generatedAtEpoch": int(time.time()),
        "gameId": game_id,
        "singleCategoryIds": sorted(single_ids),
        "names": by_name,
    }
    INDEX.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Índice pronto: {sum(len(v) for v in by_name.values())} blueprints em {len(by_name)} nomes")
    return payload


def load_blueprint_index():
    payload = load_json(INDEX, {})
    if payload and int(payload.get("version", 0) or 0) >= 2 and cache_fresh(payload) and payload.get("names"):
        print("Usando índice CardTrader em cache")
        return payload
    return build_blueprint_index()


def candidate_names(target):
    species = normalize(target.get("speciesName", target["name"]))
    kind = target["formKind"]
    if kind == "shiny":
        return []
    if kind == "mega":
        base = species
        return [normalize(target["name"]), f"mega {base} ex", f"m {base} ex", f"mega {base}", f"m {base}"]
    if kind in {"alola", "galar", "hisui", "paldea"}:
        return [normalize(target["name"]), f"{kind} {species}", f"{species} {kind}"]
    return [species]


def blueprint_candidates(target, index):
    names = index.get("names", {})
    found = {}
    aliases = candidate_names(target)
    if not aliases:
        return []
    for alias in aliases:
        for key, items in names.items():
            if key == alias or key.startswith(alias + " "):
                rest = key[len(alias):].strip()
                allowed = ("ex", "gx", "v", "vmax", "vstar", "lv x", "star", "break")
                if rest and not any(rest == a or rest.startswith(a + " ") for a in allowed):
                    continue
                for item in items:
                    found[item["id"]] = item
    ordered = sorted(found.values(), key=lambda x: (x.get("expansionId", 10**9), x["id"]))
    if len(ordered) > 6:
        mixed = []
        left, right = 0, len(ordered) - 1
        while left <= right:
            mixed.append(ordered[left]); left += 1
            if left <= right:
                mixed.append(ordered[right]); right -= 1
        ordered = mixed
    return ordered


def product_condition(product):
    props = product.get("properties_hash") or product.get("properties") or {}
    return str(props.get("condition", ""))


def product_price(product):
    cents = product.get("price_cents")
    currency = product.get("price_currency")
    if cents is None:
        price = product.get("price") or {}
        cents = price.get("cents")
        currency = currency or price.get("currency") or price.get("currency_iso")
    try:
        cents = int(cents)
    except (TypeError, ValueError):
        return None
    currency = str(currency or "").upper()
    if cents <= 0 or not currency:
        return None
    return cents, currency


def marketplace_products(payload, blueprint_id):
    if isinstance(payload, dict):
        direct = payload.get(str(blueprint_id))
        if isinstance(direct, list):
            return [p for p in direct if isinstance(p, dict)]
        if isinstance(direct, dict):
            return [direct]
        for key in ("data", "result", "items", "products"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [p for p in nested if isinstance(p, dict)]
            if isinstance(nested, dict):
                return marketplace_products(nested, blueprint_id)
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    return []


def blueprint_reference(bp):
    data = api_json("/marketplace/products", {"blueprint_id": bp["id"]})
    products = marketplace_products(data, bp["id"])
    valid = []
    for product in products:
        if bool(product.get("graded")):
            continue
        quantity = product.get("quantity", product.get("bundled_quantity", 0))
        try:
            if int(quantity or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        if product_condition(product).lower() != "near mint":
            continue
        parsed = product_price(product)
        if parsed:
            valid.append(parsed)
    if not valid:
        return None

    currencies = {}
    for cents, currency in valid:
        currencies.setdefault(currency, []).append(cents)
    currency, prices = max(currencies.items(), key=lambda kv: len(kv[1]))
    prices = sorted(prices)[:15]
    reference = int(round(statistics.median(prices)))
    return {
        "name": bp["name"],
        "code": bp.get("expansionCode") or str(bp["id"]),
        "collection": bp.get("expansion"),
        "value": format_money(reference, currency),
        "source": SOURCE,
        "url": MANUAL_BASE,
        "blueprintId": bp["id"],
        "referenceCents": reference,
        "currency": currency,
        "priceMethod": "Mediana de até 15 ofertas Near Mint mais baratas",
    }


def rank_cards(cards):
    by_id = {}
    for card in cards:
        if str(card.get("source", "")).lower() != SOURCE.lower():
            continue
        try:
            key = int(card.get("blueprintId", 0) or 0)
        except (TypeError, ValueError):
            key = 0
        if key:
            by_id[key] = card
    grouped = {}
    for card in by_id.values():
        grouped.setdefault(card.get("currency", ""), []).append(card)
    if not grouped:
        return []
    _, items = max(grouped.items(), key=lambda kv: len(kv[1]))
    items.sort(key=lambda c: int(c.get("referenceCents", 0) or 0), reverse=True)
    return items[:3]


def select_species(entries, progress):
    mode = progress.get("mode", "first_pass")
    start = int(progress.get("nextSpeciesNumber", 1) or 1)
    if mode == "first_pass":
        nums = []
        current = max(1, min(MAX_POKEDEX, start))
        while len(nums) < BATCH_SIZE and current <= MAX_POKEDEX:
            nums.append(current)
            current += 1
        return mode, nums

    pending_species = sorted({
        int(e.get("speciesNumber", -1)) for e in entries
        if 1 <= int(e.get("speciesNumber", -1)) <= MAX_POKEDEX
        and (not e.get("scanComplete", False) or not e.get("cards"))
    })
    if not pending_species:
        return "complete", []
    cursor = int(progress.get("missingCursorSpecies", 1) or 1)
    ordered = [n for n in pending_species if n >= cursor] + [n for n in pending_species if n < cursor]
    return "missing_only", ordered[:BATCH_SIZE]


def main():
    if not TOKEN:
        raise SystemExit("ERRO: configure o secret CARDTRADER_TOKEN no repositório antes de executar o workflow.")

    info = api_json("/info")
    print(f"Autenticação CardTrader OK; /info retornou {type(info).__name__}")
    index = load_blueprint_index()

    catalog = load_json(CATALOG, {"version": 8, "pokemon": []})
    progress = load_json(PROGRESS, {"version": 4, "nextSpeciesNumber": 1, "mode": "first_pass", "totalRuns": 0})
    entries = catalog.setdefault("pokemon", [])

    # Migração: remove referências de fontes antigas sem apagar a estrutura de espécies/formas.
    for entry in entries:
        entry["cards"] = [c for c in entry.get("cards", []) if str(c.get("source", "")).lower() == SOURCE.lower()]
        if not entry["cards"]:
            entry["lookupStatus"] = "pending"
            entry["scanCursor"] = 0
            entry["scanComplete"] = False

    by_key = {key_of(e): e for e in entries}
    mode, numbers = select_species(entries, progress)
    marketplace_calls = 0
    cards_added = 0
    processed_species = 0

    for species_number in numbers:
        print(f"Processando espécie #{species_number:04d}")
        try:
            targets = build_targets(species_number)
        except Exception as exc:
            print(f"  PokeAPI falhou: {exc}")
            continue

        for target in targets:
            key = (species_number, target["apiIdentifier"])
            entry = by_key.get(key)
            if entry is None:
                entry = {
                    "speciesNumber": species_number,
                    "name": target["name"],
                    "formKind": target["formKind"],
                    "cards": [],
                    "lookupStatus": "pending",
                    "manualSearchUrl": target["manualSearchUrl"],
                    "scanCursor": 0,
                    "scanComplete": False,
                }
                if target["apiIdentifier"] is not None:
                    entry["apiIdentifier"] = target["apiIdentifier"]
                entries.append(entry)
                by_key[key] = entry
            else:
                entry["name"] = target["name"]
                entry["formKind"] = target["formKind"]
                entry["manualSearchUrl"] = target["manualSearchUrl"]

            candidates = blueprint_candidates(target, index)
            if not candidates:
                entry["lookupStatus"] = "not_found"
                entry["scanComplete"] = True
                entry["scanCursor"] = 0
                continue

            cursor = int(entry.get("scanCursor", 0) or 0)
            collected = list(entry.get("cards", []))
            while cursor < len(candidates) and marketplace_calls < MAX_MARKETPLACE_CALLS:
                bp = candidates[cursor]
                cursor += 1
                marketplace_calls += 1
                try:
                    card = blueprint_reference(bp)
                except urllib.error.HTTPError as exc:
                    if exc.code == 429:
                        print("  CardTrader rate limit; encerrando lote")
                        cursor -= 1
                        break
                    print(f"  Falha blueprint {bp['id']}: HTTP {exc.code}")
                    continue
                except Exception as exc:
                    print(f"  Falha blueprint {bp['id']}: {exc}")
                    continue
                if card:
                    collected.append(card)
                    cards_added += 1
                time.sleep(MARKETPLACE_DELAY)

            entry["scanCursor"] = cursor
            entry["scanComplete"] = cursor >= len(candidates)
            entry["lastLookupAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            entry["cards"] = rank_cards(collected)
            entry["lookupStatus"] = "found" if entry["cards"] else ("not_found" if entry["scanComplete"] else "pending")
            entry["candidateBlueprints"] = len(candidates)

            if marketplace_calls >= MAX_MARKETPLACE_CALLS:
                break
        processed_species += 1
        if marketplace_calls >= MAX_MARKETPLACE_CALLS:
            break

    entries.sort(key=lambda e: (int(e.get("speciesNumber", 9999)), e.get("apiIdentifier") or ""))
    filled = sum(1 for e in entries if e.get("cards"))
    total_cards = sum(len(e.get("cards", [])) for e in entries)
    incomplete = sum(1 for e in entries if not e.get("scanComplete", False))

    catalog.update({
        "version": 8,
        "updatedAt": time.strftime("%Y-%m-%d"),
        "priceSource": SOURCE,
        "sources": [SOURCE],
        "currencyPolicy": "Moeda retornada pela CardTrader",
        "priceMethod": "Mediana de até 15 ofertas Near Mint mais baratas por blueprint",
        "catalogScope": "Catálogo incremental por espécie e forma usando a API oficial CardTrader",
        "note": "Até 3 cartas por entrada. Formas Shiny não recebem cartas normais automaticamente.",
        "stats": {"entries": len(entries), "entriesWithCards": filled, "cards": total_cards, "entriesIncomplete": incomplete},
    })

    if mode == "first_pass" and numbers:
        last = numbers[min(processed_species, len(numbers)) - 1] if processed_species else numbers[0]
        if last >= MAX_POKEDEX:
            progress["mode"] = "missing_only"
            progress["firstPassCompletedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            progress["missingCursorSpecies"] = 1
            progress["nextSpeciesNumber"] = 1
        else:
            progress["mode"] = "first_pass"
            progress["nextSpeciesNumber"] = last + 1
    elif mode == "missing_only" and numbers:
        last = numbers[min(processed_species, len(numbers)) - 1] if processed_species else numbers[0]
        progress["mode"] = "missing_only"
        progress["missingCursorSpecies"] = last + 1 if last < MAX_POKEDEX else 1
        progress["nextSpeciesNumber"] = progress["missingCursorSpecies"]
    elif mode == "complete":
        progress["mode"] = "complete"

    progress.update({
        "version": 4,
        "lastRunAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lastBatchSpecies": processed_species,
        "lastBatchMarketplaceCalls": marketplace_calls,
        "lastBatchCardsObserved": cards_added,
        "entriesIncomplete": incomplete,
        "totalRuns": int(progress.get("totalRuns", 0)) + 1,
        "sourcePolicy": "CardTrader-only",
    })

    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    PROGRESS.write_text(json.dumps(progress, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"CardTrader: {filled} entradas preenchidas / {total_cards} cartas; {incomplete} entradas ainda incompletas")
    print(f"Chamadas marketplace nesta execução: {marketplace_calls}")


if __name__ == "__main__":
    main()
