#!/usr/bin/env python3
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from bs4 import BeautifulSoup

CATALOG = Path("card_prices.json")
PROGRESS = Path("catalog_progress.json")
MAX_POKEDEX = 1025
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "1"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "15"))
MAX_CARD_DETAIL_REQUESTS = int(os.getenv("MAX_CARD_DETAIL_REQUESTS", "3"))
POKEAPI_SPECIES = "https://pokeapi.co/api/v2/pokemon-species/{number}"
LIGA_BASE = "https://www.ligapokemon.com.br/"
LIGA_SEARCH = "https://www.ligapokemon.com.br/?view=cards/search&card={query}"
REQUIRED_SOURCE = "LigaPokemon"
CARD_LABEL_RE = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$")
PRICE_RE = re.compile(r"R\$\s*([0-9.]+(?:,[0-9]{1,2})?)")


class LigaBlocked(Exception):
    def __init__(self, status: int, url: str):
        super().__init__(f"LigaPokemon respondeu HTTP {status} em {url}")
        self.status = status
        self.url = url


def polite_sleep():
    if REQUEST_DELAY > 0:
        time.sleep(REQUEST_DELAY)


def request_text(url: str, accept: str = "text/html,application/xhtml+xml") -> str:
    headers = {
        "Accept": accept,
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.6",
        "User-Agent": "PokeBinder-CatalogUpdater/7.0 (+slow-sequential; contact-via-repository)",
        "Cache-Control": "no-cache",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            body = response.read().decode("utf-8", errors="replace")
        polite_sleep()
        return body
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 429}:
            raise LigaBlocked(exc.code, url) from exc
        if exc.code in {500, 502, 503, 504}:
            polite_sleep()
        raise


def request_json(url: str):
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "PokeBinder-CatalogUpdater/7.0"},
    )
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


def brl_to_float(text: str):
    match = PRICE_RE.search(text or "")
    if not match:
        return None
    raw = match.group(1).replace(".", "").replace(",", ".")
    try:
        value = float(raw)
        return value if value > 0 else None
    except ValueError:
        return None


def format_brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def normalize_card_url(href: str) -> str:
    return urllib.parse.urljoin(LIGA_BASE, href)


def is_card_href(href: str) -> bool:
    lowered = (href or "").lower()
    return "view=cards/card" in lowered


def nearest_text_with_price(node):
    current = node
    for _ in range(7):
        if current is None:
            break
        text = " ".join(current.stripped_strings)
        if "R$" in text and len(text) <= 2200:
            return text
        current = current.parent
    return ""


def parse_collection_from_url(url: str):
    try:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        values = params.get("ed") or params.get("edition")
        if values and values[0]:
            return values[0]
    except Exception:
        pass
    return None


def parse_card_label(text: str):
    clean = " ".join((text or "").split()).strip()
    match = CARD_LABEL_RE.match(clean)
    if not match:
        return None, None
    return match.group(1).strip(), match.group(2).strip()


def extract_search_candidates(html: str):
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if not is_card_href(href):
            continue
        url = normalize_card_url(href)
        if url in seen:
            continue
        seen.add(url)

        label = " ".join(anchor.stripped_strings).strip()
        name, code = parse_card_label(label)
        if not name or not code:
            # Em algumas páginas o texto fica no contêiner pai, não no link.
            container_text = nearest_text_with_price(anchor)
            possible = re.search(r"([^\n\r]+?)\s*\(([^()]+)\)", container_text)
            if possible:
                name = " ".join(possible.group(1).split())
                code = possible.group(2).strip()

        nearby = nearest_text_with_price(anchor)
        price = brl_to_float(nearby)
        candidates.append({
            "name": name,
            "code": code,
            "collection": parse_collection_from_url(url),
            "url": url,
            "_price": price,
        })
    return candidates


def extract_card_detail(url: str, fallback):
    html = request_text(url)
    soup = BeautifulSoup(html, "html.parser")
    title = soup.find(["h1", "h2", "h3"])
    title_text = " ".join(title.stripped_strings).strip() if title else ""
    name, code = parse_card_label(title_text)
    if not name:
        name = fallback.get("name")
    if not code:
        code = fallback.get("code")
    body = " ".join(soup.stripped_strings)
    price = brl_to_float(body)
    return {
        "name": name,
        "code": code,
        "collection": fallback.get("collection") or parse_collection_from_url(url),
        "url": url,
        "_price": price,
    }


def cards_from_liga(name: str):
    search_url = manual_url(name)
    html = request_text(search_url)
    raw = extract_search_candidates(html)
    if not raw:
        return [], "not_found"

    completed = []
    detail_requests = 0
    for candidate in raw:
        if candidate.get("_price") is not None and candidate.get("name") and candidate.get("code"):
            completed.append(candidate)
            continue
        if detail_requests >= MAX_CARD_DETAIL_REQUESTS:
            continue
        detail_requests += 1
        try:
            completed.append(extract_card_detail(candidate["url"], candidate))
        except LigaBlocked:
            raise
        except Exception as exc:
            print(f"  Falha ao abrir detalhe {candidate['url']}: {exc}")

    valid = []
    seen = set()
    for item in completed:
        if not item.get("name") or not item.get("code") or item.get("_price") is None:
            continue
        key = (item["name"].lower(), item["code"].lower())
        if key in seen:
            continue
        seen.add(key)
        valid.append({
            "name": item["name"],
            "code": item["code"],
            **({"collection": item["collection"]} if item.get("collection") else {}),
            "value": format_brl(item["_price"]),
            "source": REQUIRED_SOURCE,
            "url": item["url"],
            "_price": item["_price"],
        })

    valid.sort(key=lambda x: x["_price"], reverse=True)
    result = [{k: v for k, v in item.items() if k != "_price"} for item in valid[:3]]
    return result, ("found" if result else "not_found")


def keep_only_liga_cards(entry):
    cards = [
        c for c in entry.get("cards", [])
        if str(c.get("source", "")).lower() == REQUIRED_SOURCE.lower()
    ]
    entry["cards"] = cards[:3]
    entry["manualSearchUrl"] = entry.get("manualSearchUrl") or manual_url(entry.get("name", "Pokemon"))
    if entry["cards"]:
        entry["lookupStatus"] = "found"


def select_species(entries, progress):
    mode = progress.get("mode", "first_pass")
    start = int(progress.get("nextSpeciesNumber", 1) or 1)
    if mode == "first_pass":
        return mode, [max(1, min(MAX_POKEDEX, start))]

    missing_species = sorted({
        int(e.get("speciesNumber", -1))
        for e in entries
        if not e.get("cards") and 1 <= int(e.get("speciesNumber", -1)) <= MAX_POKEDEX
    })
    if not missing_species:
        return "complete", []
    cursor = int(progress.get("missingCursorSpecies", 1) or 1)
    ordered = [n for n in missing_species if n >= cursor] + [n for n in missing_species if n < cursor]
    return "missing_only", ordered[:BATCH_SIZE]


def main():
    catalog = load_json(CATALOG, {"version": 7, "pokemon": []})
    progress = load_json(PROGRESS, {"version": 3, "nextSpeciesNumber": 1, "mode": "first_pass", "totalRuns": 0})
    entries = catalog.setdefault("pokemon", [])

    for entry in entries:
        keep_only_liga_cards(entry)

    by_key = {key_of(e): e for e in entries}
    mode, numbers = select_species(entries, progress)
    processed = 0
    cards_added = 0
    blocked = False
    blocked_status = None
    blocked_name = None

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
            if existing is None:
                existing = {
                    "speciesNumber": species_number,
                    "name": target["name"],
                    "formKind": target["formKind"],
                    "cards": [],
                    "lookupStatus": "pending",
                    "manualSearchUrl": target["manualSearchUrl"],
                }
                if target["apiIdentifier"] is not None:
                    existing["apiIdentifier"] = target["apiIdentifier"]
                entries.append(existing)
                by_key[key] = existing
            else:
                existing["name"] = target["name"]
                existing["formKind"] = target["formKind"]
                existing["manualSearchUrl"] = target["manualSearchUrl"]
                keep_only_liga_cards(existing)

            if existing.get("cards"):
                print(f"  {target['name']}: já preenchido; preservado")
                continue

            print(f"  Consultando LigaPokemon: {target['name']}")
            try:
                cards, status = cards_from_liga(target["name"])
            except LigaBlocked as exc:
                blocked = True
                blocked_status = exc.status
                blocked_name = target["name"]
                existing["lookupStatus"] = "blocked"
                existing["lastLookupAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                existing["lastLookupHttpStatus"] = exc.status
                print(f"  LigaPokemon bloqueou a consulta com HTTP {exc.status}; encerrando execução sem novas tentativas")
                break
            except Exception as exc:
                existing["lookupStatus"] = "error"
                existing["lastLookupAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                existing["lastLookupError"] = str(exc)[:240]
                print(f"  Erro ao consultar LigaPokemon para {target['name']}: {exc}")
                continue

            existing["lastLookupAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            existing.pop("lastLookupHttpStatus", None)
            existing.pop("lastLookupError", None)
            if cards:
                existing["cards"] = cards
                existing["lookupStatus"] = "found"
                cards_added += len(cards)
                print(f"  {target['name']}: {len(cards)} carta(s) confirmada(s) na LigaPokemon")
            else:
                existing["cards"] = []
                existing["lookupStatus"] = status
                print(f"  {target['name']}: nenhuma carta confirmada na LigaPokemon")

        if blocked:
            break
        processed += 1

    entries.sort(key=lambda e: (int(e.get("speciesNumber", 9999)), e.get("apiIdentifier") or ""))
    filled = sum(1 for e in entries if e.get("cards"))
    total_cards = sum(len(e.get("cards", [])) for e in entries)
    empty = sum(1 for e in entries if not e.get("cards"))
    statuses = {}
    for e in entries:
        status = e.get("lookupStatus", "pending")
        statuses[status] = statuses.get(status, 0) + 1

    catalog.update({
        "version": 7,
        "updatedAt": time.strftime("%Y-%m-%d"),
        "priceSource": "LigaPokemon",
        "sources": ["LigaPokemon"],
        "catalogScope": "Catálogo por espécie e forma com coleta conservadora exclusivamente na LigaPokemon",
        "note": "Até 3 cartas por entrada. Consultas são sequenciais e lentas. HTTP 403/429 encerra imediatamente a execução; nenhuma tentativa de contorno é feita.",
        "stats": {
            "entries": len(entries),
            "entriesWithCards": filled,
            "entriesWithoutCards": empty,
            "cards": total_cards,
            "lookupStatuses": statuses,
        },
    })

    # Só avança o cursor quando a espécie terminou sem bloqueio. Assim 403/429 não vira falso 'não encontrado'.
    if mode == "first_pass" and numbers:
        current_species = numbers[0]
        if not blocked:
            if current_species >= MAX_POKEDEX:
                progress["mode"] = "missing_only"
                progress["firstPassCompletedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                progress["missingCursorSpecies"] = 1
                progress["nextSpeciesNumber"] = 1
            else:
                progress["mode"] = "first_pass"
                progress["nextSpeciesNumber"] = current_species + 1
    elif mode == "missing_only" and numbers and not blocked:
        progress["mode"] = "missing_only"
        last = numbers[-1]
        progress["missingCursorSpecies"] = last + 1 if last < MAX_POKEDEX else 1
        progress["nextSpeciesNumber"] = progress["missingCursorSpecies"]
        progress["missingPasses"] = int(progress.get("missingPasses", 0)) + 1
    elif mode == "complete":
        progress["mode"] = "complete"

    progress.update({
        "version": 3,
        "lastRunAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lastBatchSpecies": processed,
        "lastBatchCardsAdded": cards_added,
        "missingEntriesRemaining": empty,
        "totalRuns": int(progress.get("totalRuns", 0)) + 1,
        "sourcePolicy": "LigaPokemon-only",
        "collectorMode": "slow-sequential",
        "requestDelaySeconds": REQUEST_DELAY,
        "stoppedByLigaBlock": blocked,
        "blockedHttpStatus": blocked_status,
        "blockedEntryName": blocked_name,
    })

    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    PROGRESS.write_text(json.dumps(progress, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Modo: {progress.get('mode')}; espécies concluídas: {processed}; cartas adicionadas: {cards_added}")
    if blocked:
        print(f"Execução interrompida de forma conservadora: HTTP {blocked_status} em {blocked_name}; cursor preservado")
    print(f"LigaPokemon: {filled} entradas preenchidas / {total_cards} cartas; {empty} entradas ainda sem cartas")


if __name__ == "__main__":
    main()
