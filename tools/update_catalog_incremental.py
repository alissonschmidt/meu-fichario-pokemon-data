#!/usr/bin/env python3
import gzip, json, os, re, time, unicodedata, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from bs4 import BeautifulSoup

CATALOG=Path('card_prices.json'); PROGRESS=Path('catalog_progress.json'); INDEX=Path('myp_product_index.json')
MAX_POKEDEX=1025; BATCH_SIZE=int(os.getenv('BATCH_SIZE','8')); REQUEST_DELAY=float(os.getenv('REQUEST_DELAY','1.2')); MAX_RETRIES=4
MYP_BASE='https://mypcards.com'; POKEAPI_SPECIES='https://pokeapi.co/api/v2/pokemon-species/{number}'
PRICE_RE=re.compile(r'R\$\s*([0-9.]+(?:,[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)'); CARD_RE=re.compile(r'^(.*?)\s*\(([^()]+)\)\s*$'); PRODUCT_RE=re.compile(r'/pokemon/produto/\d+/([^/?#]+)',re.I)
FORM_ALIASES={'venusaur-mega':['mega-venusaur','venusaur-ex','venusaur'],'charizard-mega-x':['mega-charizard-x','charizard-ex','charizard'],'charizard-mega-y':['mega-charizard-y','charizard-ex','charizard'],'blastoise-mega':['mega-blastoise','blastoise-ex','blastoise'],'beedrill-mega':['mega-beedrill','beedrill-ex','beedrill'],'pidgeot-mega':['mega-pidgeot','pidgeot-ex','pidgeot']}

def request_bytes(url):
    headers={'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8','User-Agent':'PokeBinder-CatalogUpdater/5.2 (+incremental; missing-only-after-first-pass)'}
    last=None
    for attempt in range(1,MAX_RETRIES+1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=30) as r: body=r.read()
            time.sleep(REQUEST_DELAY); return body
        except urllib.error.HTTPError as e:
            last=e
            if e.code==429:
                wait=int(e.headers.get('Retry-After')) if (e.headers.get('Retry-After') or '').isdigit() else min(90,10*attempt)
                print(f'HTTP 429 em {url}; aguardando {wait}s'); time.sleep(wait); continue
            if e.code in {500,502,503,504} and attempt<MAX_RETRIES: time.sleep(5*attempt); continue
            raise
        except (urllib.error.URLError,TimeoutError) as e:
            last=e
            if attempt==MAX_RETRIES: raise
            time.sleep(5*attempt)
    raise last or RuntimeError('Falha sem erro detalhado')

def request_text(url): return request_bytes(url).decode('utf-8',errors='replace')
def request_json(url): return json.loads(request_text(url))
def slugify(v):
    n=unicodedata.normalize('NFKD',v); a=''.join(c for c in n if not unicodedata.combining(c)).lower().replace('♀','-f').replace('♂','-m'); return re.sub(r'[^a-z0-9]+','-',a).strip('-')
def pokemon_id_from_url(url): return url.rstrip('/').split('/')[-1]
def form_kind(name,is_default):
    if is_default:return 'normal'
    l=name.lower()
    if '-mega' in l:return 'mega'
    for r in ('alola','galar','hisui','paldea'):
        if f'-{r}' in l:return r
    return 'variant'
def pretty_name(raw):
    if '-mega' in raw:
        t=raw.split('-'); base=t[0].capitalize(); suffix=' '.join(x.upper() if x in {'x','y'} else x.capitalize() for x in t[1:]); return f'{suffix} {base}' if suffix.lower().startswith('mega') else f'{base} {suffix}'
    return ' '.join(x.capitalize() for x in raw.split('-') if x)

def build_targets(n):
    s=request_json(POKEAPI_SPECIES.format(number=n)); species=s.get('name',f'pokemon-{n}'); out=[]
    for v in s.get('varieties',[]):
        p=v.get('pokemon',{}); raw=p.get('name',''); url=p.get('url','')
        if not raw or not url: continue
        default=bool(v.get('is_default')); pid=pokemon_id_from_url(url); aid=None if default else pid; kind=form_kind(raw,default)
        base={'speciesNumber':n,'rawName':raw,'searchSlug':slugify(raw),'speciesSlug':slugify(species)}
        out.append({**base,'name':species.capitalize() if default else pretty_name(raw),'formKind':kind,'apiIdentifier':aid})
        out.append({**base,'name':f"Shiny {species.capitalize() if default else pretty_name(raw)}",'formKind':'shiny','apiIdentifier':f'shiny:{pid}'})
    return out

def parse_sitemap(url):
    raw=request_bytes(url); raw=gzip.decompress(raw) if url.endswith('.gz') else raw; root=ET.fromstring(raw); locs=[n.text.strip() for n in root.iter() if n.tag.endswith('loc') and n.text]; return root.tag.endswith('sitemapindex'),locs

def discover_sitemap_roots():
    roots=[]
    try:
        for line in request_text(f'{MYP_BASE}/robots.txt').splitlines():
            if line.lower().startswith('sitemap:'): roots.append(line.split(':',1)[1].strip())
    except Exception as e: print(f'robots.txt indisponível: {e}')
    for f in (f'{MYP_BASE}/sitemap.xml',f'{MYP_BASE}/sitemap_index.xml'):
        if f not in roots: roots.append(f)
    return roots

def rebuild_product_index():
    print('Reconstruindo índice de produtos do MYP Cards...'); pending=discover_sitemap_roots(); visited=set(); by_slug={}
    while pending and len(visited)<160:
        url=pending.pop(0)
        if url in visited: continue
        visited.add(url)
        try: is_index,locs=parse_sitemap(url)
        except Exception as e: print(f'Sitemap ignorado {url}: {e}'); continue
        if is_index: pending.extend(x for x in locs if x not in visited); continue
        for loc in locs:
            m=PRODUCT_RE.search(loc)
            if not m: continue
            b=by_slug.setdefault(m.group(1).lower(),[])
            if len(b)<40 and loc not in b: b.append(loc)
    INDEX.write_text(json.dumps({'updatedAt':time.strftime('%Y-%m-%d'),'slugs':by_slug},ensure_ascii=False,separators=(',',':'))+'\n',encoding='utf-8'); print(f'Índice MYP: {len(by_slug)} slugs'); return by_slug

def load_product_index():
    try:
        d=json.loads(INDEX.read_text(encoding='utf-8')); s=d.get('slugs',{})
        if isinstance(s,dict) and s:return s
    except Exception: pass
    return rebuild_product_index()

def brl_to_float(text):
    m=PRICE_RE.search(text or '')
    if not m:return None
    r=m.group(1); r=r.replace('.','').replace(',','.') if ',' in r else (r.replace('.','') if r.count('.')>1 else r)
    try:
        v=float(r); return v if v>0 else None
    except ValueError:return None
def format_brl(v): return f'R$ {v:,.2f}'.replace(',','X').replace('.',',').replace('X','.')
def infer_collection(text,code):
    cleaned=' '.join((text or '').split()); after=cleaned.split(f'({code})',1)[-1] if f'({code})' in cleaned else cleaned; ignored={'alta','procura','outros','idiomas','un','ver','ofertas','adicionar','pasta','a','partir','de'}
    for token in after.split()[:16]:
        t=re.sub(r'[^A-Za-z0-9-]','',token)
        if t and t.lower() not in ignored and not t.startswith('R') and 2<=len(t)<=20 and any(c.isalpha() for c in t): return t
    return None
def find_container_with_price(node):
    cur=node; best=None
    for _ in range(8):
        cur=cur.parent if cur is not None else None
        if cur is None:break
        text=' '.join(cur.stripped_strings)
        if 'R$' in text:
            best=cur
            if len(text)<=1800:return cur
    return best
def extract_product_link(container,preferred):
    fallback=None
    if container is None:return None
    for a in container.find_all('a',href=True):
        href=a.get('href',''); m=PRODUCT_RE.search(href)
        if not m:continue
        fallback=fallback or href
        if m.group(1).lower() in preferred:return href
    return fallback
def candidate_slugs(t):
    p=t['searchSlug']; s=t.get('speciesSlug',p); out=[p]
    for a in FORM_ALIASES.get(p,[]):
        if a not in out:out.append(a)
    if s not in out:out.append(s)
    return out
def name_matches_target(card_name,t,aliases):
    cs=slugify(card_name); ss=t.get('speciesSlug',t['searchSlug']); k=t['formKind']
    if k=='normal': return cs==ss or cs.startswith(ss+'-')
    if k=='mega': return 'mega' in cs and (ss in cs or any(a in cs for a in aliases if 'mega' in a))
    if k in {'alola','galar','hisui','paldea'}: return k in cs and ss in cs
    if k=='shiny': return ('shiny' in card_name.lower() or 'brilhante' in card_name.lower()) and ss in cs
    return ss in cs

def parse_seed_page(url,t,aliases):
    soup=BeautifulSoup(request_text(url),'html.parser'); candidates=[]; preferred=set(aliases)
    def add(name,code,price,href,collection=None):
        if not name or not code or not price or price<=0 or not href or not name_matches_target(name,t,aliases):return
        candidates.append({'name':name.strip(),'code':code.strip(),'collection':collection,'value':format_brl(price),'source':'MYP Cards','url':urllib.parse.urljoin(MYP_BASE,href),'_price':price})
    for h in soup.find_all(['h1','h2','h3','h4','h5','h6']):
        m=CARD_RE.match(' '.join(h.stripped_strings).strip())
        if not m:continue
        c=find_container_with_price(h)
        if c is None:continue
        text=' '.join(c.stripped_strings); add(m.group(1),m.group(2),brl_to_float(text),extract_product_link(c,preferred) or url,infer_collection(text,m.group(2)))
    for a in soup.find_all('a',href=True):
        href=a.get('href',''); murl=PRODUCT_RE.search(href); m=CARD_RE.match(' '.join(a.stripped_strings).strip())
        if not murl or not m:continue
        c=find_container_with_price(a)
        if c is None:continue
        text=' '.join(c.stripped_strings); add(m.group(1),m.group(2),brl_to_float(text),href,infer_collection(text,m.group(2)))
    unique={}
    for item in candidates:
        k=(item['name'].lower(),item['code'].lower()); prev=unique.get(k)
        if prev is None or item['_price']>prev['_price']:unique[k]=item
    return [{k:v for k,v in x.items() if k!='_price' and v is not None} for x in sorted(unique.values(),key=lambda x:x['_price'],reverse=True)[:3]]

def find_cards(t,index):
    aliases=candidate_slugs(t); urls=[]
    for a in aliases:
        for u in index.get(a,[]):
            if u not in urls:urls.append(u)
    if not urls: print(f"  Sem seed no índice para {t['name']} ({', '.join(aliases)})"); return []
    for url in urls[:3]:
        try:
            cards=parse_seed_page(url,t,aliases)
            if cards: print(f"  {t['name']}: {len(cards)} carta(s) encontrada(s)"); return cards
        except urllib.error.HTTPError as e:
            if e.code==429: print('Rate limit persistente; encerrando lote com segurança'); raise
            print(f'Falha em {url}: {e}')
        except Exception as e: print(f'Falha em {url}: {e}')
    print(f"  Página analisada, mas sem correspondência confirmada para {t['name']}"); return []

def key_of(e): return (int(e.get('speciesNumber',-1)),e.get('apiIdentifier') or None)
def load_json(path,default):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except Exception:return default

def first_pass_numbers(start):
    return list(range(start,min(MAX_POKEDEX,start+BATCH_SIZE-1)+1))
def missing_species(entries,after_species=0):
    missing=sorted({int(e.get('speciesNumber',0)) for e in entries if 1<=int(e.get('speciesNumber',0))<=MAX_POKEDEX and not e.get('cards')})
    if not missing:return []
    ordered=[n for n in missing if n>after_species]+[n for n in missing if n<=after_species]
    return ordered[:BATCH_SIZE]

def main():
    catalog=load_json(CATALOG,{'version':5,'pokemon':[]}); progress=load_json(PROGRESS,{'nextSpeciesNumber':1,'cycle':1,'totalRuns':0,'mode':'first_pass'}); entries=catalog.setdefault('pokemon',[]); by_key={key_of(e):e for e in entries}; index=load_product_index()
    mode=progress.get('mode','first_pass'); start=int(progress.get('nextSpeciesNumber',1)); start=start if 1<=start<=MAX_POKEDEX else 1
    if mode=='missing_only':
        cursor=int(progress.get('missingCursorSpecies',0)); numbers=missing_species(entries,cursor)
        if not numbers:
            print('Nenhuma entrada sem cartas pendente no catálogo.'); progress['lastRunAt']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()); progress['totalRuns']=int(progress.get('totalRuns',0))+1; PROGRESS.write_text(json.dumps(progress,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); return
        print(f'Modo missing_only: priorizando espécies com entradas vazias: {numbers}')
    else:
        numbers=first_pass_numbers(start); print(f'Modo first_pass: espécies {numbers[0]} a {numbers[-1]}')
    new_entries_with_cards=0; cards_added=0; stop_early=False
    for species_number in numbers:
        print(f'Processando espécie #{species_number:04d}')
        try: targets=build_targets(species_number)
        except Exception as e: print(f'PokeAPI falhou para #{species_number}: {e}'); continue
        for t in targets:
            k=(species_number,t['apiIdentifier']); existing=by_key.get(k)
            if existing and existing.get('cards'):continue
            try: cards=find_cards(t,index)
            except urllib.error.HTTPError as e:
                if e.code==429: stop_early=True; break
                cards=[]
            clean={'speciesNumber':species_number,'name':t['name'],'formKind':t['formKind'],'cards':cards}
            if t['apiIdentifier'] is not None:clean['apiIdentifier']=t['apiIdentifier']
            if existing:
                if cards:
                    previous=len(existing.get('cards',[])); existing.update(clean)
                    if previous==0:new_entries_with_cards+=1; cards_added+=len(cards)
                else:
                    existing.setdefault('cards',[]); existing['name']=existing.get('name') or clean['name']; existing['formKind']=existing.get('formKind') or clean['formKind']
            else:
                entries.append(clean); by_key[k]=clean
                if cards:new_entries_with_cards+=1; cards_added+=len(cards)
        if stop_early:break
    entries.sort(key=lambda e:(int(e.get('speciesNumber',9999)),e.get('apiIdentifier') or ''))
    with_cards=sum(1 for e in entries if e.get('cards')); total_cards=sum(len(e.get('cards',[])) for e in entries); missing_count=sum(1 for e in entries if not e.get('cards'))
    catalog.update({'version':5,'updatedAt':time.strftime('%Y-%m-%d'),'priceSource':'Mercado brasileiro','sources':['MYP Cards','LigaPokemon'],'catalogScope':'Catálogo incremental por espécie e forma','note':'Até 3 cartas por entrada. Primeira passagem cobre toda a Pokédex; depois apenas entradas sem cartas são priorizadas. Registros preenchidos são preservados.','stats':{'entries':len(entries),'entriesWithCards':with_cards,'cards':total_cards,'entriesWithoutCards':missing_count}})
    if mode=='first_pass':
        if stop_early: next_species=species_number
        elif numbers[-1]>=MAX_POKEDEX:
            mode='missing_only'; next_species=1; progress['firstPassCompletedAt']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()); progress['missingCursorSpecies']=0; print('Primeira passagem concluída. Próximas execuções usarão apenas entradas sem cartas.')
        else: next_species=numbers[-1]+1
    else:
        next_species=1; progress['missingCursorSpecies']=species_number if stop_early else numbers[-1]; progress['missingEntriesRemaining']=missing_count; progress['missingPasses']=int(progress.get('missingPasses',0))+(1 if numbers and numbers[-1]<=int(progress.get('missingCursorSpecies',0)) else 0)
    progress.update({'version':2,'mode':mode,'nextSpeciesNumber':next_species,'lastRunAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'lastBatchStart':numbers[0],'lastBatchEnd':species_number if stop_early else numbers[-1],'lastBatchSpecies':numbers.index(species_number)+1 if stop_early else len(numbers),'lastBatchEntriesWithNewCards':new_entries_with_cards,'lastBatchCardsAdded':cards_added,'totalRuns':int(progress.get('totalRuns',0))+1,'stoppedByRateLimit':stop_early,'parserVersion':'5.2-missing-only'})
    CATALOG.write_text(json.dumps(catalog,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); PROGRESS.write_text(json.dumps(progress,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'Lote concluído: {new_entries_with_cards} novas entradas com cartas, {cards_added} cartas adicionadas')
    print(f'Catálogo: {with_cards} preenchidas, {missing_count} sem cartas; modo={mode}; próxima espécie=#{next_species:04d}')
if __name__=='__main__': main()
