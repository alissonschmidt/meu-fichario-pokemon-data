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
API = "https://api.cardtrader.com/api/v2"
POKEAPI = "https://pokeapi.co/api/v2/pokemon-species/{number}"
SOURCE = "CardTrader"
MANUAL_URL = "https://www.cardtrader.com/en/pokemon"
MAX_POKEDEX = 1025
TOKEN = os.getenv("CARDTRADER_TOKEN", "").strip()
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "4"))
MAX_MARKETPLACE_CALLS = int(os.getenv("MAX_MARKETPLACE_CALLS", "40"))
MARKETPLACE_DELAY = float(os.getenv("MARKETPLACE_DELAY", "1.05"))
INDEX_REQUEST_DELAY = float(os.getenv("INDEX_REQUEST_DELAY", "0.08"))
INDEX_MAX_AGE_DAYS = int(os.getenv("INDEX_MAX_AGE_DAYS", "7"))


def request_json(url, token=False):
    headers = {"Accept": "application/json", "User-Agent": "PokeBinder-CatalogUpdater/8.2"}
    if token:
        if not TOKEN:
            raise RuntimeError("CARDTRADER_TOKEN não configurado")
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def api(path, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return request_json(url, token=True)


def records(payload, label="response"):
    if isinstance(payload, list):
        out = []
        for item in payload:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, str):
                out.append({"name": item, "display_name": item})
        return out

    if isinstance(payload, dict):
        for wrapper in ("array", "data", "result", "items", "resources", "games", "categories", "expansions", "blueprints"):
            nested = payload.get(wrapper)
            if isinstance(nested, (list, dict)):
                return records(nested, f"{label}.{wrapper}")

        out = []
        for key, value in payload.items():
            if isinstance(value, dict):
                item = dict(value)
                if "id" not in item:
                    try:
                        item["id"] = int(key)
                    except (TypeError, ValueError):
                        pass
                out.append(item)
            elif isinstance(value, str):
                item = {"name": value, "display_name": value}
                try:
                    item["id"] = int(key)
                except (TypeError, ValueError):
                    item["key"] = key
                out.append(item)
        if out:
            return out

    raise RuntimeError(f"Formato inesperado em {label}: {type(payload).__name__}")


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch)).lower()
    for ch in "'’.:_-()/":
        value = value.replace(ch, " ")
    return " ".join(value.split())


def load(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cache_fresh(payload):
    generated = payload.get("generatedAtEpoch")
    return isinstance(generated, (int, float)) and time.time() - generated < INDEX_MAX_AGE_DAYS * 86400


def build_index():
    print("Construindo índice CardTrader...")
    games_payload = api("/games")
    games = records(games_payload, "/games")
    print(f"/games formato={type(games_payload).__name__}, registros={len(games)}")

    pokemon = None
    for game in games:
        names = {normalize(game.get("name")), normalize(game.get("display_name"))}
        if "pokemon" in names:
            pokemon = game
            break
    if not pokemon or pokemon.get("id") is None:
        raise RuntimeError(f"Pokémon não localizado em /games. Amostra: {[g.get('display_name') or g.get('name') for g in games[:20]]}")
    game_id = int(pokemon["id"])
    print(f"Pokémon game_id={game_id}")

    categories_payload = api("/categories", {"game_id": game_id})
    categories = records(categories_payload, "/categories")
    single_ids = {
        int(c["id"]) for c in categories
        if c.get("id") is not None and "single" in normalize(c.get("name") or c.get("display_name"))
    }
    if not single_ids:
        raise RuntimeError(f"Categoria Singles não localizada. Categorias: {[c.get('name') for c in categories[:30]]}")
    print(f"Categorias Singles={sorted(single_ids)}")

    expansions_payload = api("/expansions")
    expansions = [
        e for e in records(expansions_payload, "/expansions")
        if int(e.get("game_id", -1) or -1) == game_id
    ]
    if not expansions:
        raise RuntimeError("Nenhuma expansão Pokémon retornada")

    expansion_map = {int(e["id"]): e for e in expansions if e.get("id") is not None}
    names = {}
    for pos, expansion in enumerate(expansions, 1):
        try:
            payload = api("/blueprints/export", {"expansion_id": expansion["id"]})
            blueprints = records(payload, "/blueprints/export")
        except urllib.error.HTTPError as exc:
            print(f"Expansão {expansion.get('name')} ignorada: HTTP {exc.code}")
            continue
        for bp in blueprints:
            try:
                bp_game = int(bp.get("game_id", game_id) or game_id)
                bp_category = int(bp.get("category_id", -1) or -1)
            except (TypeError, ValueError):
                continue
            if bp_game != game_id or bp_category not in single_ids or bp.get("id") is None:
                continue
            name = str(bp.get("name") or bp.get("name_en") or "").strip()
            if not name:
                continue
            expansion_id = int(bp.get("expansion_id") or expansion["id"])
            ex = expansion_map.get(expansion_id, expansion)
            item = {
                "id": int(bp["id"]),
                "name": name,
                "expansionId": expansion_id,
                "expansion": ex.get("name"),
                "expansionCode": ex.get("code"),
            }
            names.setdefault(normalize(name), []).append(item)
        if pos % 50 == 0:
            print(f"{pos}/{len(expansions)} expansões indexadas")
        time.sleep(INDEX_REQUEST_DELAY)

    payload = {
        "version": 2,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generatedAtEpoch": int(time.time()),
        "gameId": game_id,
        "singleCategoryIds": sorted(single_ids),
        "names": names,
    }
    save(INDEX, payload)
    print(f"Índice: {sum(len(v) for v in names.values())} blueprints / {len(names)} nomes")
    return payload


def get_index():
    current = load(INDEX, {})
    if current.get("version") == 2 and current.get("names") and cache_fresh(current):
        print("Usando índice CardTrader em cache")
        return current
    return build_index()


def species_targets(number):
    species = request_json(POKEAPI.format(number=number))
    species_name = species.get("name", f"pokemon-{number}")
    out = []
    for variety in species.get("varieties", []):
        poke = variety.get("pokemon", {})
        raw = str(poke.get("name") or "")
        url = str(poke.get("url") or "")
        if not raw or not url:
            continue
        poke_id = url.rstrip("/").split("/")[-1]
        is_default = bool(variety.get("is_default"))
        kind = "normal" if is_default else "variant"
        display = species_name.capitalize()
        if not is_default:
            if "-mega" in raw:
                kind = "mega"
                parts = raw.split("-")
                suffix = " ".join(p.upper() if p in {"x", "y"} else p.capitalize() for p in parts[1:])
                display = f"{suffix} {parts[0].capitalize()}"
            else:
                for region in ("alola", "galar", "hisui", "paldea"):
                    if f"-{region}" in raw:
                        kind = region
                        display = f"{species_name.capitalize()} de {region.capitalize()}"
                        break
        out.append({"speciesNumber": number, "speciesName": species_name, "name": display, "formKind": kind, "apiIdentifier": None if is_default else poke_id})
        out.append({"speciesNumber": number, "speciesName": species_name, "name": f"Shiny {display}", "formKind": "shiny", "apiIdentifier": f"shiny:{poke_id}"})
    return out


def aliases(target):
    species = normalize(target["speciesName"])
    kind = target["formKind"]
    if kind == "shiny":
        return []
    if kind == "mega":
        return [normalize(target["name"]), f"mega {species} ex", f"m {species} ex", f"mega {species}", f"m {species}"]
    if kind in {"alola", "galar", "hisui", "paldea"}:
        return [normalize(target["name"]), f"{kind} {species}", f"{species} {kind}"]
    return [species]


def candidates(target, index):
    found = {}
    for alias in aliases(target):
        for key, items in index.get("names", {}).items():
            if key != alias and not key.startswith(alias + " "):
                continue
            rest = key[len(alias):].strip()
            allowed = ("ex", "gx", "v", "vmax", "vstar", "lv x", "star", "break")
            if rest and not any(rest == a or rest.startswith(a + " ") for a in allowed):
                continue
            for item in items:
                found[item["id"]] = item
    ordered = sorted(found.values(), key=lambda x: (x.get("expansionId", 10**9), x["id"]))
    if len(ordered) <= 6:
        return ordered
    mixed, left, right = [], 0, len(ordered) - 1
    while left <= right:
        mixed.append(ordered[left]); left += 1
        if left <= right:
            mixed.append(ordered[right]); right -= 1
    return mixed


def marketplace_products(payload, blueprint_id):
    if isinstance(payload, dict):
        direct = payload.get(str(blueprint_id))
        if isinstance(direct, list):
            return [p for p in direct if isinstance(p, dict)]
        for wrapper in ("array", "data", "result", "items", "products"):
            nested = payload.get(wrapper)
            if isinstance(nested, (list, dict)):
                return marketplace_products(nested, blueprint_id)
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    return []


def reference_for(bp):
    payload = api("/marketplace/products", {"blueprint_id": bp["id"]})
    offers = []
    for product in marketplace_products(payload, bp["id"]):
        if bool(product.get("graded")):
            continue
        props = product.get("properties_hash") or product.get("properties") or {}
        if str(props.get("condition", "")).lower() != "near mint":
            continue
        try:
            quantity = int(product.get("quantity", product.get("bundled_quantity", 0)) or 0)
        except (TypeError, ValueError):
            quantity = 0
        if quantity <= 0:
            continue
        cents = product.get("price_cents")
        currency = product.get("price_currency")
        if cents is None:
            price = product.get("price") or {}
            cents = price.get("cents")
            currency = currency or price.get("currency") or price.get("currency_iso")
        try:
            cents = int(cents)
        except (TypeError, ValueError):
            continue
        currency = str(currency or "").upper()
        if cents > 0 and currency:
            offers.append((cents, currency))
    if not offers:
        return None
    by_currency = {}
    for cents, currency in offers:
        by_currency.setdefault(currency, []).append(cents)
    currency, values = max(by_currency.items(), key=lambda item: len(item[1]))
    values = sorted(values)[:15]
    reference = int(round(statistics.median(values)))
    symbol = {"USD": "US$", "EUR": "€", "BRL": "R$", "GBP": "£"}.get(currency, currency)
    return {
        "name": bp["name"],
        "code": bp.get("expansionCode") or str(bp["id"]),
        "collection": bp.get("expansion"),
        "value": f"{symbol} {reference / 100:,.2f}",
        "source": SOURCE,
        "url": MANUAL_URL,
        "blueprintId": bp["id"],
        "referenceCents": reference,
        "currency": currency,
        "priceMethod": "Mediana de até 15 ofertas Near Mint mais baratas",
    }


def rank(cards):
    unique = {}
    for card in cards:
        if str(card.get("source", "")).lower() == "cardtrader" and card.get("blueprintId"):
            unique[int(card["blueprintId"])] = card
    grouped = {}
    for card in unique.values():
        grouped.setdefault(card.get("currency", ""), []).append(card)
    if not grouped:
        return []
    _, group = max(grouped.items(), key=lambda item: len(item[1]))
    return sorted(group, key=lambda card: int(card.get("referenceCents", 0)), reverse=True)[:3]


def key(entry):
    return (int(entry.get("speciesNumber", -1)), entry.get("apiIdentifier") or None)


def species_batch(entries, progress):
    mode = progress.get("mode", "first_pass")
    if mode == "first_pass":
        start = max(1, min(MAX_POKEDEX, int(progress.get("nextSpeciesNumber", 1) or 1)))
        return mode, list(range(start, min(MAX_POKEDEX + 1, start + BATCH_SIZE)))
    pending = sorted({int(e.get("speciesNumber", -1)) for e in entries if 1 <= int(e.get("speciesNumber", -1)) <= MAX_POKEDEX and (not e.get("scanComplete") or not e.get("cards"))})
    if not pending:
        return "complete", []
    cursor = int(progress.get("missingCursorSpecies", 1) or 1)
    ordered = [n for n in pending if n >= cursor] + [n for n in pending if n < cursor]
    return "missing_only", ordered[:BATCH_SIZE]


def main():
    if not TOKEN:
        raise SystemExit("CARDTRADER_TOKEN não configurado")
    info = api("/info")
    print(f"Autenticação OK (/info={type(info).__name__})")
    index = get_index()

    catalog = load(CATALOG, {"version": 8, "pokemon": []})
    progress = load(PROGRESS, {"version": 4, "mode": "first_pass", "nextSpeciesNumber": 1, "totalRuns": 0})
    entries = catalog.setdefault("pokemon", [])
    for entry in entries:
        entry["cards"] = [c for c in entry.get("cards", []) if str(c.get("source", "")).lower() == "cardtrader"]
        if not entry["cards"]:
            entry["scanCursor"] = 0
            entry["scanComplete"] = False
            entry["lookupStatus"] = "pending"

    by_key = {key(e): e for e in entries}
    mode, numbers = species_batch(entries, progress)
    calls = 0
    observed = 0
    processed = 0

    for number in numbers:
        print(f"Processando #{number:04d}")
        targets = species_targets(number)
        for target in targets:
            k = (number, target["apiIdentifier"])
            entry = by_key.get(k)
            if entry is None:
                entry = {"speciesNumber": number, "name": target["name"], "formKind": target["formKind"], "cards": [], "scanCursor": 0, "scanComplete": False, "lookupStatus": "pending", "manualSearchUrl": MANUAL_URL}
                if target["apiIdentifier"] is not None:
                    entry["apiIdentifier"] = target["apiIdentifier"]
                entries.append(entry)
                by_key[k] = entry
            else:
                entry["name"] = target["name"]
                entry["formKind"] = target["formKind"]
                entry["manualSearchUrl"] = MANUAL_URL

            pool = candidates(target, index)
            if not pool:
                entry["scanCursor"] = 0
                entry["scanComplete"] = True
                entry["lookupStatus"] = "not_found"
                continue

            cursor = int(entry.get("scanCursor", 0) or 0)
            collected = list(entry.get("cards", []))
            while cursor < len(pool) and calls < MAX_MARKETPLACE_CALLS:
                bp = pool[cursor]
                cursor += 1
                calls += 1
                try:
                    card = reference_for(bp)
                except urllib.error.HTTPError as exc:
                    if exc.code == 429:
                        cursor -= 1
                        print("Rate limit 429; preservando cursor")
                        break
                    print(f"Blueprint {bp['id']} HTTP {exc.code}")
                    continue
                if card:
                    collected.append(card)
                    observed += 1
                time.sleep(MARKETPLACE_DELAY)

            entry["scanCursor"] = cursor
            entry["scanComplete"] = cursor >= len(pool)
            entry["candidateBlueprints"] = len(pool)
            entry["lastLookupAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            entry["cards"] = rank(collected)
            entry["lookupStatus"] = "found" if entry["cards"] else ("not_found" if entry["scanComplete"] else "pending")
            if calls >= MAX_MARKETPLACE_CALLS:
                break
        processed += 1
        if calls >= MAX_MARKETPLACE_CALLS:
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

    if numbers:
        last = numbers[min(processed, len(numbers)) - 1] if processed else numbers[0]
        if mode == "first_pass":
            if last >= MAX_POKEDEX:
                progress["mode"] = "missing_only"
                progress["firstPassCompletedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                progress["missingCursorSpecies"] = 1
                progress["nextSpeciesNumber"] = 1
            else:
                progress["mode"] = "first_pass"
                progress["nextSpeciesNumber"] = last + 1
        elif mode == "missing_only":
            progress["mode"] = "missing_only"
            progress["missingCursorSpecies"] = last + 1 if last < MAX_POKEDEX else 1
            progress["nextSpeciesNumber"] = progress["missingCursorSpecies"]
    elif mode == "complete":
        progress["mode"] = "complete"

    progress.update({
        "version": 4,
        "lastRunAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lastBatchSpecies": processed,
        "lastBatchMarketplaceCalls": calls,
        "lastBatchCardsObserved": observed,
        "entriesIncomplete": incomplete,
        "totalRuns": int(progress.get("totalRuns", 0)) + 1,
        "sourcePolicy": "CardTrader-only",
        "parserVersion": "8.2-array-wrapper",
    })
    save(CATALOG, catalog)
    save(PROGRESS, progress)
    print(f"CardTrader: {filled} entradas / {total_cards} cartas; incompletas={incomplete}; marketplace_calls={calls}")


if __name__ == "__main__":
    main()
