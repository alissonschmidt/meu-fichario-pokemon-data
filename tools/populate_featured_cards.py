#!/usr/bin/env python3
import argparse
import json
import re
import time
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CATALOG = Path("featured_cards.json")
TCG_API = "https://api.pokemontcg.io/v2/cards"
POKE_SPECIES = "https://pokeapi.co/api/v2/pokemon-species?limit=1025"
POKE_ALL = "https://pokeapi.co/api/v2/pokemon?limit=5000"
USER_AGENT = "PokeBinder-FeaturedCatalog/2.0"
PAGE_SIZE = 200

CLASSIC_SETS = {
    "base set", "jungle", "fossil", "team rocket", "gym heroes", "gym challenge",
    "neo genesis", "neo discovery", "neo revelation", "neo destiny", "legendary collection",
    "expedition base set", "aquapolis", "skyridge",
}
PREMIUM = (
    "special illustration", "illustration rare", "ultra rare", "secret", "hyper rare",
    "rainbow", "shiny", "shining", "gold star", "gold",
)
MID = ("rare", "holo", "radiant", "amazing")
MECHANICS = (" ex", " gx", " vmax", " vstar", " lv.x", "break", "prime", "legend", "mega", " m ")
REGIONAL = {"alola": "Alolan", "galar": "Galarian", "hisui": "Hisuian", "paldea": "Paldean"}


def fetch_json(url, retries=10):
    last = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(req, timeout=35) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Falha ao consultar {url}: {last}")


def norm(value):
    value = unicodedata.normalize("NFD", value or "")
    value = "".join(c for c in value if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def display_name(slug):
    special = {
        "mr-mime": "Mr. Mime", "mime-jr": "Mime Jr.", "mr-rime": "Mr. Rime",
        "type-null": "Type: Null", "nidoran-f": "Nidoran♀", "nidoran-m": "Nidoran♂",
        "ho-oh": "Ho-Oh", "porygon-z": "Porygon-Z",
    }
    return special.get(slug, " ".join(p.capitalize() for p in slug.split("-")))


def card_year(card):
    release = ((card.get("set") or {}).get("releaseDate") or "")
    m = re.match(r"(\d{4})", release)
    return int(m.group(1)) if m else 9999


def era(year):
    if year <= 2003: return "wotc"
    if year <= 2007: return "ex"
    if year <= 2010: return "dp"
    if year <= 2013: return "bw"
    if year <= 2016: return "xy"
    if year <= 2019: return "sm"
    if year <= 2022: return "swsh"
    return "sv"


def rarity_strength(card):
    rarity = norm(card.get("rarity") or "")
    if any(norm(x) in rarity for x in PREMIUM): return 1.0
    if any(norm(x) in rarity for x in MID): return 0.5
    return 0.0


def premium_art(card):
    rarity = norm(card.get("rarity") or "")
    name = norm(card.get("name") or "")
    if any(norm(x) in rarity or norm(x) in name for x in PREMIUM): return 1.0
    if any(x in rarity for x in ("holo", "radiant", "amazing")): return 0.5
    return 0.0


def landmark(card):
    combined = " " + norm(card.get("name") or "") + " " + " ".join(norm(x) for x in card.get("subtypes") or []) + " "
    return 1.0 if any(norm(x) in combined for x in MECHANICS) else 0.0


def is_secret(card):
    try:
        number = int(re.match(r"\d+", str(card.get("number") or "")).group())
        total = int((card.get("set") or {}).get("printedTotal") or 0)
        return total > 0 and number > total
    except Exception:
        return False


def market_url(card):
    query = " ".join(filter(None, [card.get("name"), (card.get("set") or {}).get("name"), str(card.get("number") or "")]))
    params = {
        "productLineName": "pokemon",
        "q": query,
        "view": "grid",
        "ProductTypeName": "Cards",
        "Condition": "Near Mint",
        "sort": "price-asc",
    }
    return "https://www.tcgplayer.com/search/pokemon/product?" + urlencode(params)


def fame(c):
    return 15*c["recognition"] + 10*c["associatedArt"] + 10*c["collectorPresence"] + 5*c["representativeCard"]


def history(c):
    return 15*c["firstAppearance"] + 10*c["historicSet"] + 5*c["landmarkMechanic"] + 5*c["culturalCompetitiveImpact"]


def collector(c):
    return 10*c["rarityScarcity"] + 8*c["premiumArt"] + 5*c["promoChaseCommemorative"] + 2*c["recurringDemand"]


def score_candidate(card, identity, earliest):
    year = card_year(card)
    rarity = rarity_strength(card)
    premium = premium_art(card)
    mechanic = landmark(card)
    set_name = norm((card.get("set") or {}).get("name") or "")
    first = 1.0 if year == earliest else (0.67 if year <= earliest + 2 else 0.0)
    historic = 1.0 if year <= 2003 or set_name in CLASSIC_SETS else (0.5 if year <= 2010 else 0.0)
    card_name = norm(card.get("name") or "")
    identity_norm = norm(identity)
    exact = card_name == identity_norm or card_name.startswith(identity_norm + " ")
    promo = 1.0 if "promo" in set_name or is_secret(card) or premium == 1.0 else 0.0
    impact = 1.0 if mechanic and (rarity >= 0.5 or year <= 2016) else 0.0
    recognition = 1.0 if (first and rarity >= 0.5) or premium == 1.0 or (mechanic and rarity >= 0.5) else 0.0
    associated = 1.0 if premium == 1.0 or (first == 1.0 and rarity >= 0.5) else 0.0
    presence = 1.0 if premium == 1.0 or (mechanic and rarity >= 0.5) else (0.5 if rarity >= 0.5 else 0.0)
    recurring = 1.0 if (year <= 2003 and rarity >= 0.5) or premium == 1.0 or (mechanic and rarity == 1.0) else 0.0
    c = {
        "id": card.get("id") or f"{set_name}-{card.get('number')}", "card": card, "year": year, "era": era(year),
        "recognition": recognition, "associatedArt": associated, "collectorPresence": presence,
        "representativeCard": 1.0 if exact else 0.0, "firstAppearance": first,
        "historicSet": historic, "landmarkMechanic": mechanic, "culturalCompetitiveImpact": impact,
        "rarityScarcity": rarity, "premiumArt": premium, "promoChaseCommemorative": promo,
        "recurringDemand": recurring,
        "reprintGroup": norm((card.get("name") or "") + "|" + (card.get("artist") or "")),
    }
    c["baseScore"] = round(max(0.0, min(100.0, fame(c) + history(c) + collector(c))), 2)
    return c


def select(candidates):
    selected, remaining = [], list(candidates)
    while remaining and len(selected) < 3:
        evaluated = []
        for c in remaining:
            redundancy = 15 if any(c["reprintGroup"] and c["reprintGroup"] == s["reprintGroup"] for s in selected) else 0
            diversity = 0
            if len(selected) >= 2 and len({s["era"] for s in selected}) == 1 and c["era"] == selected[0]["era"]:
                if any(x["era"] != c["era"] and c["baseScore"] - x["baseScore"] <= 10 for x in remaining if x["id"] != c["id"]):
                    diversity = 10
            x = dict(c)
            x["adjusted"] = max(0, c["baseScore"] - redundancy - diversity)
            evaluated.append(x)
        evaluated.sort(key=lambda c: (-c["adjusted"], -history(c), -fame(c), -collector(c), c["year"], c["id"]))
        winner = evaluated[0]
        selected.append(winner)
        remaining = [x for x in remaining if x["id"] != winner["id"]]
    return selected


def categories(selected):
    if not selected: return []
    result = {}
    historical = max(selected, key=lambda c: (history(c), -c["year"], c["baseScore"]))
    result[historical["id"]] = "historical"
    rest = [c for c in selected if c["id"] != historical["id"]]
    if rest:
        iconic = max(rest, key=lambda c: (fame(c), c["baseScore"], -c["year"]))
        result[iconic["id"]] = "iconic"
        rest = [c for c in rest if c["id"] != iconic["id"]]
    for c in rest: result[c["id"]] = "collector"
    return [(c, result[c["id"]]) for c in selected]


def output_card(candidate, category):
    card = candidate["card"]
    set_data = card.get("set") or {}
    number = str(card.get("number") or "")
    total = set_data.get("printedTotal") or set_data.get("total")
    if total and "/" not in number: number = f"{number}/{total}"
    images = card.get("images") or {}
    return {
        "name": card.get("name") or "", "set": set_data.get("name") or "",
        "year": candidate["year"] if candidate["year"] < 9999 else date.today().year,
        "number": number, "rarity": card.get("rarity") or None,
        "category": category, "baseScore": candidate["baseScore"],
        "marketplace": "TCGplayer", "marketUrl": market_url(card),
        "imageUrl": images.get("large") or images.get("small"),
    }


def make_entry(dex, identity, cards, api_identifier=None, form_type=None):
    featured = []
    if cards:
        earliest = min(card_year(c) for c in cards)
        featured = [output_card(c, cat) for c, cat in categories(select([score_candidate(c, identity, earliest) for c in cards]))]
    entry = {"speciesNumber": dex, "name": identity, "featuredCards": featured}
    if api_identifier is not None: entry["apiIdentifier"] = str(api_identifier)
    if form_type: entry["formType"] = form_type
    return entry


def all_cards():
    cards, page = [], 1
    fields = "id,name,subtypes,set,number,artist,rarity,nationalPokedexNumbers,images"
    while True:
        payload = fetch_json(TCG_API + "?" + urlencode({"page": page, "pageSize": PAGE_SIZE, "select": fields}))
        batch = payload.get("data") or []
        if not batch: break
        cards.extend(batch)
        total = int(payload.get("totalCount") or len(cards))
        print(f"Cartas TCG carregadas: {len(cards)}/{total}")
        if len(cards) >= total: break
        page += 1
        time.sleep(2.1)
    return cards


def species_names():
    payload = fetch_json(POKE_SPECIES)
    result = {}
    for item in payload.get("results") or []:
        m = re.search(r"/(\d+)/?$", item.get("url") or "")
        if m: result[int(m.group(1))] = item.get("name") or f"pokemon-{m.group(1)}"
    return result


def group_by_dex(cards):
    grouped = {i: [] for i in range(1, 1026)}
    for card in cards:
        for dex in card.get("nationalPokedexNumbers") or []:
            if isinstance(dex, int) and dex in grouped: grouped[dex].append(card)
    return grouped


def write(entries, phase, cards_scanned):
    base = sum(1 for e in entries if "apiIdentifier" not in e)
    payload = {
        "version": 2, "updatedAt": date.today().isoformat(),
        "selectionPolicy": "featured-score-v1", "selectionMethod": "metadata-heuristic-v1",
        "description": "Cartas em destaque selecionadas por fama, relevância histórica e interesse de colecionador. Preços não são armazenados no PokéBinder.",
        "stats": {
            "phase": phase, "sourceCardsScanned": cards_scanned, "basePokemon": base,
            "formEntries": len(entries) - base,
            "entriesWithFeaturedCards": sum(1 for e in entries if e.get("featuredCards")),
        },
        "pokemon": entries,
    }
    CATALOG.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def base_phase():
    cards = all_cards()
    names = species_names()
    grouped = group_by_dex(cards)
    entries = [make_entry(dex, display_name(names.get(dex, f"pokemon-{dex}")), grouped[dex]) for dex in range(1, 1026)]
    write(entries, "base-complete", len(cards))
    print(f"Fase base concluída: 1025/1025; {sum(bool(e['featuredCards']) for e in entries)} com cartas.")


def form_targets(base_slug, form_slug):
    suffix = form_slug[len(base_slug):].strip("-") if form_slug.startswith(base_slug) else form_slug
    parts = [p for p in suffix.split("-") if p]
    base = display_name(base_slug)
    names = []
    if not parts: return []
    if parts[0] in REGIONAL: names.append(f"{REGIONAL[parts[0]]} {base}")
    if "mega" in parts: names += [f"M {base}", f"Mega {base}"]
    if "gmax" in parts: names += [f"{base} VMAX", f"{base} Gigantamax"]
    label = " ".join(p.capitalize() for p in parts)
    names += [f"{base} {label}", f"{label} {base}"]
    return [norm(x) for x in names]


def cards_for_form(cards, base_slug, form_slug):
    targets = form_targets(base_slug, form_slug)
    return [c for c in cards if any(t and (norm(c.get("name") or "") == t or t in norm(c.get("name") or "")) for t in targets)]


def shiny_cards(cards):
    result = []
    for c in cards:
        rarity, name = norm(c.get("rarity") or ""), norm(c.get("name") or "")
        set_name = norm((c.get("set") or {}).get("name") or "")
        number = str(c.get("number") or "").lower()
        if any(x in rarity or x in name for x in ("shiny", "shining", "radiant")):
            result.append(c)
        elif set_name in ("hidden fates", "shining fates", "paldean fates") and (number.startswith("sv") or "shiny" in rarity):
            result.append(c)
    return result


def form_type(slug):
    if "-mega" in slug: return "mega"
    if any(f"-{x}" in slug for x in REGIONAL): return "regional"
    if "-gmax" in slug: return "gigantamax"
    return "other"


def forms_phase():
    if not CATALOG.exists(): raise SystemExit("Execute a fase base primeiro.")
    current = json.loads(CATALOG.read_text(encoding="utf-8"))
    base_entries = [e for e in current.get("pokemon", []) if "apiIdentifier" not in e]
    if len(base_entries) != 1025: raise SystemExit(f"Fase base incompleta: {len(base_entries)}/1025")

    cards = all_cards()
    grouped = group_by_dex(cards)
    names = species_names()
    by_slug = {slug: dex for dex, slug in names.items()}
    sorted_slugs = sorted(by_slug, key=len, reverse=True)
    all_pokemon = fetch_json(POKE_ALL).get("results") or []
    forms = []

    for item in all_pokemon:
        slug = item.get("name") or ""
        m = re.search(r"/(\d+)/?$", item.get("url") or "")
        if not m: continue
        api_id = int(m.group(1))
        if api_id <= 1025 and slug in by_slug: continue
        base_slug = next((b for b in sorted_slugs if slug.startswith(b + "-")), None)
        if not base_slug: continue
        dex = by_slug[base_slug]
        matched = cards_for_form(grouped[dex], base_slug, slug)
        forms.append(make_entry(dex, display_name(slug), matched, api_id, form_type(slug)))

    # Shiny é uma variação visual e não possui ID próprio na PokeAPI; usamos um identificador estável interno.
    for dex in range(1, 1026):
        matched = shiny_cards(grouped[dex])
        if matched:
            forms.append(make_entry(dex, f"Shiny {display_name(names[dex])}", matched, f"shiny-{dex}", "shiny"))

    entries = base_entries + forms
    write(entries, "forms-complete", len(cards))
    print(f"Fase de formas concluída: {len(forms)} entradas adicionais.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["base", "forms", "all"], default="all")
    args = parser.parse_args()
    if args.phase in ("base", "all"): base_phase()
    if args.phase in ("forms", "all"): forms_phase()


if __name__ == "__main__":
    main()
