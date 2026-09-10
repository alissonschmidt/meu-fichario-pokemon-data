#!/usr/bin/env python3
import gzip
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from bs4 import BeautifulSoup

CATALOG = Path("card_prices.json")
PROGRESS = Path("catalog_progress.json")
INDEX = Path("myp_product_index.json")
MAX_POKEDEX = 1025
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "8"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.2"))
MAX_RETRIES = 4
MYP_BASE = "https://mypcards.com"
POKEAPI_SPECIES = "https://pokeapi.co/api/v2/pokemon-species/{number}"
PRICE_RE = re.compile(r"R\$\s*([0-9.]+(?:,[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)")
CARD_RE = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$")


def request_bytes(url: str) -> bytes:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "User-Agent": "PokeBinder-CatalogUpdater/5.0 (+incremental; respectful-rate-limit)",
    }
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                body = response.read()
            time.sleep(REQUEST_DELAY)
            return body
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else min(90, 10 * attempt)
                print(f"HTTP 429 em {url}; aguardando {wait}s")
                time.sleep(wait)
                continue
            if exc.code in {500, 502, 503, 504} and attempt < MAX_RETRIES:
                time.sleep(5 * attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if attempt == MAX_RETRIES:
                raise
            time.sleep(5 * attempt)
    if last:
        raise last
    raise RuntimeError("Falha sem erro detalhado")


def request_text(url: str) -> str:
    return request_bytes(url).decode("utf-8", errors="replace")


def request_json(url: str):
    return json.loads(request_text(url))


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    ascii_value = ascii_value.lower().replace("♀", "-f").replace("♂", "-m")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")


def pokemon_id_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def form_kind(name: str, is_default: bool) -> str:
    if is_default:
        return "normal"
    lowered = name.lower()
    if "-mega" in lowered:
        return "mega"
    for region in ("alola", "galar", "hisui", "paldea"):
        if f"-{region}" in lowered:
            return region
    return "variant"


def pretty_name(raw: str) -> str:
    parts = raw.replace("-mega-x", " mega x").replace("-mega-y", " mega y").replace("-mega", " mega").split("-")
    if " mega " in f" {raw.replace('-', ' ')} ":
        tokens = raw.split("-")
        base = tokens[0].capitalize()
        suffix = " ".join(token.upper() if token in {"x", "y"} else token.capitalize() for token in tokens[1:])
        return f"{suffix} {base}" if suffix.lower().startswith("mega") else f"{base} {suffix}"
    return " ".join(part.capitalize() for part in parts if part)


def build_targets(species_number: int):
    species = request_json(POKEAPI_SPECIES.format(number=species_number))
    species_name = species.get("name", f"pokemon-{species_number}")
    targets = []
    varieties = species.get("varieties", [])
    for variety in varieties:
        poke = variety.get("pokemon", {})
        raw_name = poke.get("name", "")
        poke_url = poke.get("url", "")
        if not raw_name or not poke_url:
            continue
        is_default = bool(variety.get("is_default"))
        poke_id = pokemon_id_from_url(poke_url)
        api_identifier = None if is_default else poke_id
        kind = form_kind(raw_name, is_default)
        targets.append({
            "speciesNumber": species_number,
            "name": pretty_name(raw_name) if not is_default else species_name.capitalize(),
            "rawName": raw_name,
            "formKind": kind,
            "apiIdentifier": api_identifier,
            "searchSlug": slugify(raw_name),
        })
        targets.append({
            "speciesNumber": species_number,
            "name": f"Shiny {pretty_name(raw_name) if not is_default else species_name.capitalize()}",
            "rawName": raw_name,
            "formKind": "shiny",
            "apiIdentifier": f"shiny:{poke_id}",
            "searchSlug": slugify(raw_name),
        })
    if not varieties:
        targets.append({
            "speciesNumber": species_number,
            "name": species_name.capitalize(),
            "rawName": species_name,
            "formKind": "normal",
            "apiIdentifier": None,
            "searchSlug": slugify(species_name),
        })
        targets.append({
            "speciesNumber": species_number,
            "name": f"Shiny {species_name.capitalize()}",
            "rawName": species_name,
            "formKind": "shiny",
            "apiIdentifier": f"shiny:{species_number}",
            "searchSlug": slugify(species_name),
        })
    return targets


def parse_sitemap(url: str):
    raw = request_bytes(url)
    if url.endswith(".gz"):
        raw = gzip.decompress(raw)
    root = ET.fromstring(raw)
    locs = [node.text.strip() for node in root.iter() if node.tag.endswith("loc") and node.text]
    return root.tag.endswith("sitemapindex"), locs


def discover_sitemap_roots():
    roots = []
    try:
        robots = request_text(f"{MYP_BASE}/robots.txt")
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                roots.append(line.split(":", 1)[1].strip())
    except Exception as exc:
        print(f"robots.txt indisponível: {exc}")
    for fallback in (f"{MYP_BASE}/sitemap.xml", f"{MYP_BASE}/sitemap_index.xml"):
        if fallback not in roots:
            roots.append(fallback)
    return roots


def rebuild_product_index():
    print("Reconstruindo índice de produtos do MYP Cards...")
    pending = discover_sitemap_roots()
    visited = set()
    by_slug = {}
    while pending and len(visited) < 160:
        url = pending.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            is_index, locs = parse_sitemap(url)
        except Exception as exc:
            print(f"Sitemap ignorado {url}: {exc}")
            continue
        if is_index:
            pending.extend(loc for loc in locs if loc not in visited)
            continue
        for loc in locs:
            if "/pokemon/produto/" not in loc:
                continue
            slug = urllib.parse.urlparse(loc).path.rstrip("/").split("/")[-1].lower()
            bucket = by_slug.setdefault(slug, [])
            if len(bucket) < 12 and loc not in bucket:
                bucket.append(loc)
    payload = {
        "updatedAt": time.strftime("%Y-%m-%d"),
        "slugs": by_slug,
    }
    INDEX.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Índice MYP: {len(by_slug)} slugs")
    return by_slug


def load_product_index():
    if INDEX.exists():
        try:
            payload = json.loads(INDEX.read_text(encoding="utf-8"))
            slugs = payload.get("slugs", {})
            if isinstance(slugs, dict) and slugs:
                return slugs
        except Exception:
            pass
    return rebuild_product_index()


def brl_to_float(text: str):
    match = PRICE_RE.search(text)
    if not match:
        return None
    raw = match.group(1)
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        value = float(raw)
        return value if value > 0 else None
    except ValueError:
        return None


def format_brl(value: float) -> str:
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def nearby_text(anchor):
    node = anchor
    for _ in range(7):
        if node is None:
            break
        text = " ".join(node.stripped_strings)
        if "R$" in text and len(text) < 1600:
            return text
        node = node.parent
    return ""


def infer_collection(text: str, code: str):
    cleaned = " ".join(text.split())
    after = cleaned.split(f"({code})", 1)[-1] if f"({code})" in cleaned else cleaned
    tokens = after.split()
    ignored = {"alta", "procura", "outros", "idiomas", "un", "ver", "ofertas", "adicionar", "pasta"}
    for token in tokens[:12]:
        token_clean = re.sub(r"[^A-Za-z0-9-]", "", token)
        if not token_clean or token_clean.lower() in ignored or token_clean.startswith("R"):
            continue
        if 2 <= len(token_clean) <= 16 and any(ch.isalpha() for ch in token_clean):
            return token_clean
    return None


def parse_seed_page(url: str, target_slug: str, shiny_only: bool):
    soup = BeautifulSoup(request_text(url), "html.parser")
    candidates = []

    def add(name, code, price, href, collection=None):
        if not name or not code or not price or price <= 0:
            return
        if shiny_only and "shiny" not in name.lower() and "brilhante" not in name.lower():
            return
        candidates.append({
            "name": name.strip(),
            "code": code.strip(),
            "collection": collection,
            "value": format_brl(price),
            "source": "MYP Cards",
            "url": urllib.parse.urljoin(MYP_BASE, href),
            "_price": price,
        })

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if "/pokemon/produto/" not in href:
            continue
        href_slug = urllib.parse.urlparse(href).path.rstrip("/").split("/")[-1].lower()
        if href_slug != target_slug:
            continue
        label = " ".join(anchor.stripped_strings).strip()
        match = CARD_RE.match(label)
        if not match:
            continue
        text = nearby_text(anchor)
        price = brl_to_float(text)
        add(match.group(1), match.group(2), price, href, infer_collection(text, match.group(2)))

    page_title = soup.find(["h1", "h2"])
    if page_title:
        label = " ".join(page_title.stripped_strings).strip()
        match = CARD_RE.match(label)
        if match:
            body = " ".join(soup.stripped_strings)
            price = brl_to_float(body[:1400])
            add(match.group(1), match.group(2), price, url, None)

    unique = {}
    for item in candidates:
        key = (item["name"].lower(), item["code"].lower())
        prev = unique.get(key)
        if prev is None or item["_price"] > prev["_price"]:
            unique[key] = item
    ordered = sorted(unique.values(), key=lambda item: item["_price"], reverse=True)[:3]
    return [{k: v for k, v in item.items() if k != "_price" and v is not None} for item in ordered]


def find_cards(target, product_index):
    slug = target["searchSlug"]
    urls = product_index.get(slug, [])
    if not urls:
        return []
    shiny_only = target["formKind"] == "shiny"
    # Uma única página-semente normalmente lista as demais edições do mesmo Pokémon.
    for url in urls[:2]:
        try:
            cards = parse_seed_page(url, slug, shiny_only)
            if cards:
                return cards
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                print("Rate limit persistente; encerrando lote com segurança")
                raise
            print(f"Falha em {url}: {exc}")
        except Exception as exc:
            print(f"Falha em {url}: {exc}")
    return []


def key_of(entry):
    return (int(entry.get("speciesNumber", -1)), entry.get("apiIdentifier") or None)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def main():
    catalog = load_json(CATALOG, {"version": 5, "pokemon": []})
    progress = load_json(PROGRESS, {"nextSpeciesNumber": 1, "cycle": 1, "totalRuns": 0})
    entries = catalog.setdefault("pokemon", [])
    by_key = {key_of(entry): entry for entry in entries}
    product_index = load_product_index()

    start = int(progress.get("nextSpeciesNumber", 1))
    if start < 1 or start > MAX_POKEDEX:
        start = 1
    numbers = []
    current = start
    while len(numbers) < BATCH_SIZE:
        numbers.append(current)
        current += 1
        if current > MAX_POKEDEX:
            current = 1

    new_entries_with_cards = 0
    cards_added = 0
    stop_early = False

    for species_number in numbers:
        print(f"Processando espécie #{species_number:04d}")
        try:
            targets = build_targets(species_number)
        except Exception as exc:
            print(f"PokeAPI falhou para #{species_number}: {exc}")
            continue

        for target in targets:
            entry_key = (species_number, target["apiIdentifier"])
            existing = by_key.get(entry_key)
            if existing and existing.get("cards"):
                # Registros já preenchidos são preservados integralmente.
                continue

            try:
                cards = find_cards(target, product_index)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    stop_early = True
                    break
                cards = []

            clean_target = {
                "speciesNumber": species_number,
                "name": target["name"],
                "formKind": target["formKind"],
                "cards": cards,
            }
            if target["apiIdentifier"] is not None:
                clean_target["apiIdentifier"] = target["apiIdentifier"]

            if existing:
                # Nunca substitui cards válidos por vazio.
                if cards:
                    previous_count = len(existing.get("cards", []))
                    existing.update(clean_target)
                    if previous_count == 0:
                        new_entries_with_cards += 1
                        cards_added += len(cards)
                else:
                    existing.setdefault("cards", [])
                    existing["name"] = existing.get("name") or clean_target["name"]
                    existing["formKind"] = existing.get("formKind") or clean_target["formKind"]
            else:
                entries.append(clean_target)
                by_key[entry_key] = clean_target
                if cards:
                    new_entries_with_cards += 1
                    cards_added += len(cards)

        if stop_early:
            break

    # Ordenação estável e estatísticas.
    entries.sort(key=lambda e: (int(e.get("speciesNumber", 9999)), e.get("apiIdentifier") or ""))
    catalog["version"] = 5
    catalog["updatedAt"] = time.strftime("%Y-%m-%d")
    catalog["priceSource"] = "Mercado brasileiro"
    catalog["sources"] = ["MYP Cards", "LigaPokemon"]
    catalog["catalogScope"] = "Catálogo incremental por espécie e forma"
    catalog["note"] = "Até 3 cartas por entrada. Registros preenchidos são preservados; falhas e rate limits nunca apagam dados válidos."
    with_cards = sum(1 for entry in entries if entry.get("cards"))
    total_cards = sum(len(entry.get("cards", [])) for entry in entries)
    catalog["stats"] = {
        "entries": len(entries),
        "entriesWithCards": with_cards,
        "cards": total_cards,
    }

    last_species = numbers[-1]
    if stop_early:
        next_species = species_number
    else:
        next_species = last_species + 1
        if next_species > MAX_POKEDEX:
            next_species = 1
            progress["cycle"] = int(progress.get("cycle", 1)) + 1

    progress.update({
        "version": 1,
        "nextSpeciesNumber": next_species,
        "lastRunAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lastBatchStart": start,
        "lastBatchEnd": species_number if stop_early else last_species,
        "lastBatchSpecies": (numbers.index(species_number) + 1) if stop_early else len(numbers),
        "lastBatchEntriesWithNewCards": new_entries_with_cards,
        "lastBatchCardsAdded": cards_added,
        "totalRuns": int(progress.get("totalRuns", 0)) + 1,
        "stoppedByRateLimit": stop_early,
    })

    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    PROGRESS.write_text(json.dumps(progress, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Lote concluído: {new_entries_with_cards} novas entradas com cartas, {cards_added} cartas adicionadas")
    print(f"Próxima espécie: #{next_species:04d}; catálogo com {with_cards} entradas preenchidas")


if __name__ == "__main__":
    main()
