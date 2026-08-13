"""
build_data.py
Generates data.js for battle_tracker.html by combining:
  - pokedex.json (existing: KO/EN names, forms, base stats)
  - pokemondb.net/pokedex/all  -> types per (name, form) row
  - PokeAPI CSVs               -> full move list + official KO/EN/JA names
                                  (species, moves, abilities, items, natures)
  - hardcoded Gen 6+ type chart, type/nature names
  - my_team.json               -> initial team (moves matched to move DB by KO name)

Run:  python build_data.py
"""
import json
import re
import sys
import csv
import io
import unicodedata
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

UA = {"User-Agent": "Mozilla/5.0 (personal pokemon team tool)"}
POKEAPI_CSV = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/"

def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def fetch_csv(name: str):
    raw = fetch(POKEAPI_CSV + name).decode("utf-8")
    return list(csv.DictReader(io.StringIO(raw)))

# PokeAPI local_language_id: 1 = ja-Hrkt (kana), 3 = ko, 9 = en, 11 = ja (kanji)
LANG_JA_KANA, LANG_JA_KANJI, LANG_KO, LANG_EN = "1", "11", "3", "9"

def names_by_lang(rows, id_col: str):
    """{lang: {id: name}} from a PokeAPI *_names.csv row list."""
    out = {LANG_JA_KANA: {}, LANG_JA_KANJI: {}, LANG_KO: {}, LANG_EN: {}}
    for r in rows:
        lang = r["local_language_id"]
        if lang in out:
            out[lang][r[id_col]] = r["name"]
    return out

def ja_of(names, key):
    """Japanese name, preferring the in-game kana spelling."""
    return names[LANG_JA_KANA].get(key) or names[LANG_JA_KANJI].get(key)

# ---------------------------------------------------------------- types (pokemondb)
def scrape_types() -> dict:
    """Return {(name_en, form): [Type, ...]} from pokemondb's /pokedex/all table."""
    html = fetch("https://pokemondb.net/pokedex/all").decode("utf-8")
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", {"id": "pokedex"})
        result = {}
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) < 10:
                continue
            name = cols[1].find("a", class_="ent-name").text.strip()
            small = cols[1].find("small", class_="text-muted")
            form = small.text.strip() if small else "default"
            types = [a.text.strip() for a in cols[2].find_all("a")]
            result[(name, form)] = types
        return result
    except ImportError:
        # regex fallback: each row has ent-name link, optional small, then type-icon links
        result = {}
        rows = re.findall(r"<tr>(.*?)</tr>", html, re.S)
        for row in rows:
            m = re.search(r'class="ent-name"[^>]*>([^<]+)</a>', row)
            if not m:
                continue
            name = m.group(1).strip()
            fm = re.search(r'<small class="text-muted">([^<]+)</small>', row)
            form = fm.group(1).strip() if fm else "default"
            types = re.findall(r'class="type-icon[^"]*"[^>]*>([^<]+)</a>', row)
            if types:
                result[(name, form)] = types
        return result

# ---------------------------------------------------------------- moves (PokeAPI)
TYPE_BY_ID = {
    1: "Normal", 2: "Fighting", 3: "Flying", 4: "Poison", 5: "Ground",
    6: "Rock", 7: "Bug", 8: "Ghost", 9: "Steel", 10: "Fire",
    11: "Water", 12: "Grass", 13: "Electric", 14: "Psychic", 15: "Ice",
    16: "Dragon", 17: "Dark", 18: "Fairy",
}
CAT_BY_ID = {"1": "X", "2": "P", "3": "S"}  # status / physical / special
SPREAD_TARGETS = {"9", "11"}  # all-other-pokemon (Earthquake), all-opponents (Heat Wave)

def build_moves():
    moves_rows = fetch_csv("moves.csv")
    names = names_by_lang(fetch_csv("move_names.csv"), "move_id")
    ko, en = names[LANG_KO], names[LANG_EN]
    moves = []
    for r in moves_rows:
        mid = r["id"]
        tid = int(r["type_id"]) if r["type_id"] else 0
        if tid not in TYPE_BY_ID:      # shadow moves etc.
            continue
        if mid not in en:
            continue
        moves.append({
            "id": int(mid),
            "ko": ko.get(mid, en[mid]),
            "en": en[mid],
            "ja": ja_of(names, mid) or en[mid],
            "type": TYPE_BY_ID[tid],
            "cat": CAT_BY_ID.get(r["damage_class_id"], "X"),
            "power": int(r["power"]) if r["power"] else 0,
            "acc": int(r["accuracy"]) if r["accuracy"] else 0,
            "pp": int(r["pp"]) if r["pp"] else 0,
            "pri": int(r["priority"]) if r["priority"] else 0,
            "spread": r["target_id"] in SPREAD_TARGETS,
        })
    return moves

# ---------------------------------------------------------------- species names (PokeAPI)
def build_species_ja(pokedex: list) -> dict:
    """{name_en: name_ja} for every species in pokedex.json (official kana names)."""
    names = names_by_lang(fetch_csv("pokemon_species_names.csv"), "pokemon_species_id")
    ja_by_en = {}
    ja_by_norm = {}
    for sid, en in names[LANG_EN].items():
        ja = ja_of(names, sid)
        if not ja:
            continue
        ja_by_en[en] = ja
        ja_by_norm[toid(en)] = ja
    out, misses = {}, []
    for p in pokedex:
        en = p["name_en"]
        ja = ja_by_en.get(en) or ja_by_norm.get(toid(en))
        if ja:
            out[en] = ja
        elif en not in out:
            misses.append(en)
    if misses:
        print(f"  WARNING: no Japanese species name for {sorted(set(misses))}")
    return out

# ---------------------------------------------------------------- sprites (Pokemon Showdown)
def toid(s: str) -> str:
    s = s.replace("♀", "f").replace("♂", "m")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())

FILLER_TOKENS = {"form", "forme", "style", "mode", "size", "cloak", "breed", "plumage", "family", "of", "the"}
TOKEN_ALIAS = {"alolan": "alola", "galarian": "galar", "hisuian": "hisui", "paldean": "paldea",
               "female": "f", "male": "m"}

def form_tokens(form: str, species: str) -> set:
    sp = {toid(w) for w in re.split(r"[^A-Za-z0-9%]+", species) if w}
    toks = set()
    for w in re.split(r"[^A-Za-z0-9%]+", form):
        t = toid(w)
        if not t or t in FILLER_TOKENS or t in sp:
            continue
        toks.add(TOKEN_ALIAS.get(t, t))
    return toks

def build_sprite_ids(pokedex: list):
    """Match each (name_en, form) to a Showdown pokedex entry; derive sprite id + abilities."""
    sd = json.loads(fetch("https://play.pokemonshowdown.com/data/pokedex.json"))
    groups = {}
    for e in sd.values():
        base = e.get("baseSpecies", e["name"])
        groups.setdefault(toid(base), []).append(e)
    sids, abils, wts, misses = [], [], [], []
    for p in pokedex:
        b = toid(p["name_en"])
        cands = groups.get(b)
        if not cands:
            sids.append(b)
            abils.append([])
            wts.append(0)
            misses.append((p["name_en"], p["form"]))
            continue
        base_e = next((e for e in cands if not e.get("forme")), cands[0])
        if p["form"] == "default":
            chosen = base_e
        else:
            # accept a Showdown forme only if ALL its tokens appear in our form name;
            # partial overlap ("Single Strike" vs "Rapid-Strike" sharing "strike") must
            # fall back to the base entry, not pick the wrong forme. Exception: pokemondb
            # sometimes omits the region word ("Combat Breed" for Paldean Tauros), so a
            # forme whose only extra tokens are region names still matches, second-choice.
            REGION_TOKENS = {"alola", "galar", "hisui", "paldea"}
            ours = form_tokens(p["form"], p["name_en"])
            chosen, best = base_e, (0, 0)
            for e in cands:
                f = e.get("forme")
                if not f:
                    continue
                ft = form_tokens(f, "")
                if not ft:
                    continue
                if ft <= ours:
                    rank = (2, len(ft))
                elif (ft - REGION_TOKENS) and (ft - REGION_TOKENS) <= ours and not (REGION_TOKENS & ours):
                    rank = (1, len(ft))
                else:
                    continue
                if rank > best:
                    chosen, best = e, rank
        base = chosen.get("baseSpecies", chosen["name"])
        f = chosen.get("forme", "")
        sids.append(toid(base) + ("-" + toid(f) if f else ""))
        ab = chosen.get("abilities", {})
        seen = []
        for slot in ("0", "1", "H", "S"):
            v = ab.get(slot)
            if v and v not in seen:
                seen.append(v)
        abils.append(seen)
        wts.append(chosen.get("weightkg", 0))
    return sids, abils, wts, misses

def build_learnsets(dex_out: list, moves: list):
    """Attach learnable move ids to each pokedex entry from Showdown learnsets.
    Form entry (if any) is merged with its base species' learnset."""
    ls = json.loads(fetch("https://play.pokemonshowdown.com/data/learnsets.json"))
    mid_by_sid = {toid(m["en"]): m["id"] for m in moves}
    unknown_moves, empty = set(), 0
    for p in dex_out:
        keys = []
        flat = p["sid"].replace("-", "")          # showdown species key, e.g. charizardmegax
        base = p["sid"].split("-")[0]             # base species key,     e.g. charizard
        for k in (flat, base):
            if k in ls and "learnset" in ls[k] and k not in keys:
                keys.append(k)
        ids = set()
        for k in keys:
            for mv in ls[k]["learnset"]:
                mid = mid_by_sid.get(mv)
                if mid:
                    ids.add(mid)
                else:
                    unknown_moves.add(mv)
        p["learn"] = sorted(ids)
        if not ids:
            empty += 1
    print(f"  learnsets: {empty} entries without data (app falls back to full move list)")
    if unknown_moves:
        print(f"  {len(unknown_moves)} showdown move keys not in move DB (G-Max etc.), e.g. {sorted(unknown_moves)[:6]}")

# ---------------------------------------------------------------- items (championship list)
# Korean names exactly as the user's championship list; the English identifier is resolved
# from PokeAPI's official Korean item names so effects/sprites can't drift from a bad guess.
ITEM_KO_LIST = [
    "하양허브", "구애스카프", "기합의띠", "기합의머리띠", "먹다남은음식", "반짝가루",
    "선제공격손톱", "왕의징표석", "리샘열매", "자뭉열매", "생명의구슬", "힘의머리띠",
    "박식안경", "달인의띠", "메트로놈", "큰뿌리", "빛의점토", "뜨거운바위",
    "차가운바위", "보송보송바위", "축축한바위", "광각렌즈", "포커스렌즈", "검은철구",
    "아름다운허물", "실크스카프", "기적의씨", "목탄", "신비의물방울", "자석",
    "은빛가루", "예리한부리", "딱딱한돌", "독바늘", "부드러운모래", "녹지않는얼음",
    "검은띠", "휘어진스푼", "저주의부적", "용의이빨", "검은안경", "금속코트",
    "요정의깃털", "멘탈허브", "조개껍질방울", "카리열매", "린드열매", "오카열매",
    "꼬시개열매", "초나열매", "리체열매", "바코열매", "루미열매", "으름열매",
    "슈캐열매", "플카열매", "로플열매", "야파열매", "수불열매", "하반열매",
    "마코열매", "바리비열매", "로셀열매", "초점렌즈", "전기구슬",
]

def build_items():
    """Resolve KO name -> official EN identifier (PokeAPI) -> EN/JA names + working icon URL.
    Returns [{ko, en, ja, id, url}]."""
    items_rows = fetch_csv("items.csv")
    names = names_by_lang(fetch_csv("item_names.csv"), "item_id")
    ident_by_id = {r["id"]: r["identifier"] for r in items_rows}
    id_by_ident = {r["identifier"]: r["id"] for r in items_rows}
    ko_to_ident = {}
    for iid, name in names[LANG_KO].items():
        ident = ident_by_id.get(iid)
        if ident and name not in ko_to_ident:
            ko_to_ident[name] = ident
    manual = {"요정의깃털": "fairy-feather"}   # too new for PokeAPI's Korean name data
    manual_names = {"fairy-feather": {"en": "Fairy Feather", "ja": "ようせいのハネ"}}
    out, unresolved, no_icon = [], [], []
    for ko in ITEM_KO_LIST:
        ident = ko_to_ident.get(ko) or manual.get(ko)
        if not ident:
            unresolved.append(ko)
            continue
        iid = id_by_ident.get(ident)
        mn = manual_names.get(ident, {})
        en_name = names[LANG_EN].get(iid) or mn.get("en") or ident
        ja_name = ja_of(names, iid) or mn.get("ja") or en_name
        candidates = [
            f"https://play.pokemonshowdown.com/sprites/itemicons/{ident}.png",
            f"https://play.pokemonshowdown.com/sprites/itemicons/{ident.replace('-', '')}.png",
            f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/items/{ident}.png",
            f"https://www.serebii.net/itemdex/sprites/sv/{ident.replace('-', '')}.png",
        ]
        url = None
        for u in candidates:
            try:
                fetch(u)
                url = u
                break
            except Exception:
                continue
        if url is None:
            no_icon.append((ko, ident))
        out.append({"ko": ko, "en": en_name, "ja": ja_name, "id": ident, "url": url or candidates[0]})
    print(f"  items: {len(out)}/{len(ITEM_KO_LIST)} resolved, {len(out) - len(no_icon)} icons verified")
    if unresolved:
        print(f"  UNRESOLVED (no PokeAPI Korean match): {unresolved}")
    if no_icon:
        print(f"  NO ICON found: {no_icon}")
    for it in out:
        print(f"    {it['ko']} -> {it['id']}")
    return out

def norm_ability(s: str) -> str:
    """Normalize for EN-name matching: strip '(Glastrier)' suffixes, curly quotes, case."""
    return re.sub(r"\s*\(.*\)$", "", s).replace("’", "'").lower().strip()

def build_ability_names():
    """English ability name (normalized) -> {ko, en, ja}, from PokeAPI ability_names.csv."""
    names = names_by_lang(fetch_csv("ability_names.csv"), "ability_id")
    en, ko = names[LANG_EN], names[LANG_KO]
    return {norm_ability(en[a]): {"ko": ko.get(a, en[a]), "en": en[a],
                                  "ja": ja_of(names, a) or en[a]} for a in en}

def verify_sprites(dex_out: list):
    """Spot-check sprite URLs (newest gen + tricky forms) against the gen5 sprite dir."""
    picks = []
    wanted = [("Charizard", "Mega Charizard X"), ("Raichu", "Alolan Raichu"), ("Zygarde", "10% Forme"),
              ("Rotom", "Heat Rotom"), ("Kyurem", "Black Kyurem"), ("Urshifu", "Rapid Strike Style"),
              ("Indeedee", "Female"), ("Flabébé", "default"), ("Flutter Mane", "default"),
              ("Iron Crown", "default"), ("Pecharunt", "default"), ("Ogerpon", "Wellspring Mask"),
              ("Ursaluna", "Bloodmoon"), ("Terapagos", "Stellar Form"), ("Basculegion", "Female")]
    for en, form in wanted:
        for p in dex_out:
            if p["en"] == en and p["form"] == form:
                picks.append(p)
                break
    ok = 0
    for p in picks:
        url = f"https://play.pokemonshowdown.com/sprites/gen5/{p['sid']}.png"
        try:
            fetch(url)
            ok += 1
        except Exception:
            print(f"  sprite MISSING: {p['en']} ({p['form']}) -> {p['sid']}")
    print(f"  sprite check: {ok}/{len(picks)} sample URLs resolve")

# ---------------------------------------------------------------- type chart (Gen 6+)
TYPE_CHART = {
    "Normal":   {"Rock": .5, "Ghost": 0, "Steel": .5},
    "Fighting": {"Normal": 2, "Ice": 2, "Rock": 2, "Dark": 2, "Steel": 2,
                 "Poison": .5, "Flying": .5, "Psychic": .5, "Bug": .5, "Fairy": .5, "Ghost": 0},
    "Flying":   {"Grass": 2, "Fighting": 2, "Bug": 2, "Electric": .5, "Rock": .5, "Steel": .5},
    "Poison":   {"Grass": 2, "Fairy": 2, "Poison": .5, "Ground": .5, "Rock": .5, "Ghost": .5, "Steel": 0},
    "Ground":   {"Fire": 2, "Electric": 2, "Poison": 2, "Rock": 2, "Steel": 2,
                 "Grass": .5, "Bug": .5, "Flying": 0},
    "Rock":     {"Fire": 2, "Ice": 2, "Flying": 2, "Bug": 2, "Fighting": .5, "Ground": .5, "Steel": .5},
    "Bug":      {"Grass": 2, "Psychic": 2, "Dark": 2, "Fire": .5, "Fighting": .5, "Poison": .5,
                 "Flying": .5, "Ghost": .5, "Steel": .5, "Fairy": .5},
    "Ghost":    {"Psychic": 2, "Ghost": 2, "Dark": .5, "Normal": 0},
    "Steel":    {"Ice": 2, "Rock": 2, "Fairy": 2, "Fire": .5, "Water": .5, "Electric": .5, "Steel": .5},
    "Fire":     {"Grass": 2, "Ice": 2, "Bug": 2, "Steel": 2, "Fire": .5, "Water": .5, "Rock": .5, "Dragon": .5},
    "Water":    {"Fire": 2, "Ground": 2, "Rock": 2, "Water": .5, "Grass": .5, "Dragon": .5},
    "Grass":    {"Water": 2, "Ground": 2, "Rock": 2, "Fire": .5, "Grass": .5, "Poison": .5,
                 "Flying": .5, "Bug": .5, "Dragon": .5, "Steel": .5},
    "Electric": {"Water": 2, "Flying": 2, "Electric": .5, "Grass": .5, "Dragon": .5, "Ground": 0},
    "Psychic":  {"Fighting": 2, "Poison": 2, "Psychic": .5, "Steel": .5, "Dark": 0},
    "Ice":      {"Grass": 2, "Ground": 2, "Flying": 2, "Dragon": 2,
                 "Fire": .5, "Water": .5, "Ice": .5, "Steel": .5},
    "Dragon":   {"Dragon": 2, "Steel": .5, "Fairy": 0},
    "Dark":     {"Psychic": 2, "Ghost": 2, "Fighting": .5, "Dark": .5, "Fairy": .5},
    "Fairy":    {"Fighting": 2, "Dragon": 2, "Dark": 2, "Fire": .5, "Poison": .5, "Steel": .5},
}

TYPE_KO = {
    "Normal": "노말", "Fire": "불꽃", "Water": "물", "Grass": "풀", "Electric": "전기",
    "Ice": "얼음", "Fighting": "격투", "Poison": "독", "Ground": "땅", "Flying": "비행",
    "Psychic": "에스퍼", "Bug": "벌레", "Rock": "바위", "Ghost": "고스트",
    "Dragon": "드래곤", "Dark": "악", "Steel": "강철", "Fairy": "페어리",
}

TYPE_JA = {
    "Normal": "ノーマル", "Fire": "ほのお", "Water": "みず", "Grass": "くさ", "Electric": "でんき",
    "Ice": "こおり", "Fighting": "かくとう", "Poison": "どく", "Ground": "じめん", "Flying": "ひこう",
    "Psychic": "エスパー", "Bug": "むし", "Rock": "いわ", "Ghost": "ゴースト",
    "Dragon": "ドラゴン", "Dark": "あく", "Steel": "はがね", "Fairy": "フェアリー",
}

def build_nature_ja() -> dict:
    """Nature key ('Hardy'...) -> official Japanese name, from PokeAPI CSVs."""
    ident_by_id = {r["id"]: r["identifier"] for r in fetch_csv("natures.csv")}
    names = names_by_lang(fetch_csv("nature_names.csv"), "nature_id")
    out = {}
    for nid, ident in ident_by_id.items():
        ja = ja_of(names, nid)
        if ja:
            out[ident.capitalize()] = ja
    missing = [k for k in NATURES if k not in out]
    if missing:
        print(f"  WARNING: no Japanese nature name for {missing}")
    return out

# nature: ko name + boosted/lowered stat index (1=Atk 2=Def 3=SpA 4=SpD 5=Spe, None=neutral)
NATURES = {
    "Hardy":   ("노력", None, None),   "Lonely":  ("외로움", 1, 2), "Brave":   ("용감", 1, 5),
    "Adamant": ("고집", 1, 3),         "Naughty": ("개구쟁이", 1, 4),
    "Bold":    ("대담", 2, 1),         "Docile":  ("온순", None, None), "Relaxed": ("무사태평", 2, 5),
    "Impish":  ("심술꾸러기", 2, 3),   "Lax":     ("촐랑", 2, 4),
    "Timid":   ("겁쟁이", 5, 1),       "Hasty":   ("성급", 5, 2),   "Serious": ("성실", None, None),
    "Jolly":   ("명랑", 5, 3),         "Naive":   ("천진난만", 5, 4),
    "Modest":  ("조심", 3, 1),         "Mild":    ("의젓", 3, 2),   "Quiet":   ("냉정", 3, 5),
    "Bashful": ("수줍음", None, None), "Rash":    ("덜렁", 3, 4),
    "Calm":    ("차분", 4, 1),         "Gentle":  ("얌전", 4, 2),   "Sassy":   ("건방", 4, 5),
    "Careful": ("신중", 4, 3),         "Quirky":  ("변덕", None, None),
}

# ---------------------------------------------------------------- main
def main():
    with open("pokedex.json", encoding="utf-8") as f:
        pokedex = json.load(f)

    print("scraping types from pokemondb ...")
    type_map = scrape_types()
    print(f"  {len(type_map)} rows scraped")

    dex_out, unmatched = [], []
    for p in pokedex:
        key = (p["name_en"], p["form"])
        types = type_map.get(key)
        if types is None:  # fall back to the species' default form
            types = type_map.get((p["name_en"], "default"))
            if types is None:
                for (n, _f), t in type_map.items():
                    if n == p["name_en"]:
                        types = t
                        break
        if types is None:
            unmatched.append(key)
            types = ["Normal"]
        b = p["base"]
        dex_out.append({
            "ko": p["name_ko"], "en": p["name_en"], "form": p["form"],
            "base": [b["HP"], b["Attack"], b["Defense"], b["Sp. Attack"], b["Sp. Defense"], b["Speed"]],
            "types": types,
        })
    if unmatched:
        print(f"  WARNING: no types found for {len(unmatched)} entries: {unmatched[:10]}")

    print("downloading Japanese species names from PokeAPI ...")
    species_ja = build_species_ja(pokedex)
    for p in dex_out:
        p["ja"] = species_ja.get(p["en"], p["en"])
    print(f"  {len(species_ja)} species matched")

    print("matching sprites/abilities against Showdown pokedex ...")
    sids, abils, wts, sprite_misses = build_sprite_ids(pokedex)
    ability_names = build_ability_names()
    abil_i18n, missing_ab = {}, set()
    for p, sid, ab, wt in zip(dex_out, sids, abils, wts):
        p["sid"] = sid
        p["wt"] = wt   # kg, for Grass Knot / Low Kick / Heavy Slam / Heat Crash
        stored = []
        for a in ab:
            rec = ability_names.get(norm_ability(a))
            key = rec["ko"] if rec else a           # canonical name kept in app state
            stored.append(key)
            abil_i18n[key] = {"en": rec["en"] if rec else a, "ja": rec["ja"] if rec else a}
            if not rec:
                missing_ab.add(a)
        p["abil"] = stored
    if sprite_misses:
        print(f"  no Showdown match for {len(sprite_misses)}: {sprite_misses[:10]}")
    if missing_ab:
        print(f"  abilities not in PokeAPI (kept English): {sorted(missing_ab)}")
    verify_sprites(dex_out)

    print("downloading move data from PokeAPI ...")
    moves = build_moves()
    print(f"  {len(moves)} moves")

    print("downloading learnsets from Showdown ...")
    build_learnsets(dex_out, moves)

    print("verifying item icons ...")
    items = build_items()

    # initial team from my_team.json — supports both the legacy format (name/nature/moves
    # by Korean name) and the extended format the tracker's 팀 저장 button writes
    # (form/item/ivs/evs + move ids)
    move_by_ko = {m["ko"]: m["id"] for m in moves}
    move_ids_all = {m["id"] for m in moves}
    default_team = []
    try:
        with open("my_team.json", encoding="utf-8") as f:
            team = json.load(f).get("team", [])
        dex_by_ko, dex_by_ko_form = {}, {}
        for i, p in enumerate(dex_out):
            dex_by_ko_form[(p["ko"], p["form"])] = i
            if p["form"] == "default" and p["ko"] not in dex_by_ko:
                dex_by_ko[p["ko"]] = i
        for member in team:
            idx = dex_by_ko_form.get((member["name"], member.get("form", "default")))
            if idx is None:
                idx = dex_by_ko.get(member["name"])
            if idx is None:
                print(f"  team member not in pokedex, skipped: {member['name']}")
                continue
            move_ids = []
            for mv in member.get("moves", []):
                mid = mv.get("id")
                if mid not in move_ids_all:
                    mid = move_by_ko.get(mv["name"])
                if mid:
                    move_ids.append(mid)
                else:
                    print(f"  move not matched, skipped: {mv['name']}")
            entry = {"dex": idx, "nature": member.get("nature", "Adamant"), "moves": move_ids}
            for k in ("ivs", "evs"):
                v = member.get(k)
                if isinstance(v, list) and len(v) == 6:
                    entry[k] = v
            if member.get("item"):
                entry["item"] = member["item"]
            if member.get("ability"):
                entry["ability"] = member["ability"]
            default_team.append(entry)
    except FileNotFoundError:
        pass

    print("downloading Japanese nature names from PokeAPI ...")
    nature_ja = build_nature_ja()

    data = {
        "pokedex": dex_out,
        "moves": moves,
        "items": items,
        "typeChart": TYPE_CHART,
        "typeKo": TYPE_KO,
        "typeJa": TYPE_JA,
        "abilNames": abil_i18n,
        "natures": {k: {"ko": v[0], "ja": nature_ja.get(k, k), "up": v[1], "down": v[2]}
                    for k, v in NATURES.items()},
        "defaultTeam": default_team,
    }
    js = "const PBT_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n"
    with open("data.js", "w", encoding="utf-8") as f:
        f.write(js)
    print(f"data.js written ({len(js)//1024} KB)")

if __name__ == "__main__":
    main()
