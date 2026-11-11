"""Recipe catalog from TheMealDB, cached in SQLite, matched against the pantry.

TheMealDB's free API (key "1") has no "search by several ingredients" call, so
we download the whole catalog (~800 recipes, one request per first letter) and
do the matching ourselves.
"""

import json
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://www.themealdb.com/api/json/v1/1/search.php?f={}"
REFRESH_SECONDS = 7 * 24 * 3600

# Things nearly every kitchen has; never counted as missing.
STAPLES = {"salt", "pepper", "black pepper", "water", "ice", "boiling water", "cold water"}

# Words that describe an ingredient rather than identify it.
DESCRIPTORS = set("""
    fresh large small medium big chopped sliced diced minced crushed grated shredded
    extra virgin organic unsalted salted plain free range raw dried dry ground whole
    kosher sea fine coarse granulated light low fat reduced sodium frozen canned tinned
    can jar of and the boneless skinless red white yellow baby cooked uncooked
    finely roughly freshly to taste for serving optional pinch handful
    a an in or
""".split())

# British / alternate names -> the name a US shopper would use.
SYNONYMS = [
    ("spring onion", "green onion"), ("scallion", "green onion"),
    ("coriander leaf", "cilantro"), ("coriander", "cilantro"),
    ("aubergine", "eggplant"), ("egg plant", "eggplant"),
    ("courgette", "zucchini"), ("chilli", "chili"), ("chile", "chili"),
    ("garlic clove", "garlic"), ("king prawn", "shrimp"), ("tiger prawn", "shrimp"), ("prawn", "shrimp"),
    ("chilly", "chili"), ("leave", "leaf"),  # artifacts of "chillies", "leaves"
    ("minced beef", "ground beef"), ("beef mince", "ground beef"), ("mince", "ground beef"),
    ("double cream", "heavy cream"), ("single cream", "cream"),
    ("caster sugar", "sugar"), ("icing sugar", "powdered sugar"),
    ("plain flour", "flour"), ("self raising flour", "flour"), ("all purpose flour", "flour"),
    ("bicarbonate of soda", "baking soda"), ("rocket", "arugula"),
    ("tomato puree", "tomato paste"), ("passata", "tomato sauce"),
    ("stock cube", "stock"), ("broth", "stock"), ("bouillon", "stock"),
    ("yoghurt", "yogurt"), ("capsicum", "bell pepper"), ("garbanzo", "chickpea"),
]


def _singular(word):
    if len(word) <= 3 or word.endswith("ss"):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("oes", "ches", "shes", "xes")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def normalize(name):
    """'Extra Virgin Olive Oils' -> ('olive', 'oil')"""
    text = name.lower().replace("-", " ")
    text = re.sub(r"[^a-z ]+", " ", text)
    text = " ".join(_singular(w) for w in text.split())
    for old, new in SYNONYMS:
        text = re.sub(rf"\b{old}\b", new, text)
    return tuple(w for w in text.split() if w not in DESCRIPTORS)


# --- Catalog --------------------------------------------------------------


class Catalog:
    def __init__(self, db, db_lock, user_agent):
        self.db, self.db_lock, self.user_agent = db, db_lock, user_agent
        self._fetch_lock = threading.Lock()
        self._recipes = None
        self._last_attempt = 0
        self.error = None
        with db_lock:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS recipes (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
            db.commit()

    def _fetched_at(self):
        with self.db_lock:
            row = self.db.execute("SELECT value FROM meta WHERE key = 'recipes_fetched_at'").fetchone()
        return float(row[0]) if row else 0

    def _fetch_letter(self, letter):
        req = urllib.request.Request(API.format(letter), headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp).get("meals") or []

    def refresh(self):
        """Download the catalog. Keeps the old copy if the download fails."""
        with self._fetch_lock:
            # Another thread may have refreshed (or just failed) while we waited.
            if not self._stale() or time.time() - self._last_attempt < 300:
                return
            self._last_attempt = time.time()
            try:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    meals = [m for batch in pool.map(self._fetch_letter, "abcdefghijklmnopqrstuvwxyz") for m in batch]
            except Exception as e:  # network trouble; report and fall back to cache
                self.error = f"Couldn't download recipes from TheMealDB: {e}"
                return
            if not meals:
                self.error = "TheMealDB returned no recipes"
                return
            parsed = [self._parse(m) for m in meals]
            with self.db_lock:
                self.db.execute("DELETE FROM recipes")
                self.db.executemany(
                    "INSERT OR REPLACE INTO recipes (id, data) VALUES (?, ?)",
                    [(r["id"], json.dumps(r)) for r in parsed],
                )
                self.db.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('recipes_fetched_at', ?)",
                    (str(time.time()),),
                )
                self.db.commit()
            self._recipes = None
            self.error = None

    @staticmethod
    def _parse(m):
        ingredients, seen = [], set()
        for i in range(1, 21):
            name = (m.get(f"strIngredient{i}") or "").strip()
            if not name:
                continue
            key = normalize(name)
            if key in seen:  # e.g. garlic listed twice (sauce + filling)
                continue
            seen.add(key)
            ingredients.append({"name": name, "measure": (m.get(f"strMeasure{i}") or "").strip()})
        return {
            "id": m["idMeal"],
            "name": m["strMeal"],
            "category": m.get("strCategory") or "",
            "area": m.get("strArea") or m.get("strCountry") or "",
            "thumb": m.get("strMealThumb") or "",
            "source": m.get("strSource") or "",
            "youtube": m.get("strYoutube") or "",
            "instructions": (m.get("strInstructions") or "").strip(),
            "ingredients": ingredients,
        }

    def _stale(self):
        return time.time() - self._fetched_at() >= REFRESH_SECONDS

    def ensure_fresh(self, background=False):
        if not self._stale() or self._fetch_lock.locked():
            return
        if background:
            threading.Thread(target=self.refresh, daemon=True).start()
        else:
            self.refresh()

    def recipes(self):
        if self._recipes is None:
            if not self._fetched_at():
                self.refresh()  # first run: block until we have something
            with self.db_lock:
                rows = self.db.execute("SELECT data FROM recipes").fetchall()
            self._recipes = [json.loads(r[0]) for r in rows]
        return self._recipes


# --- Matching -------------------------------------------------------------


def _find_in_pantry(ingredient_key, pantry):
    """Pantry item whose name contains every identifying word of the ingredient.
    'olive oil' matches 'Kirkland Extra Virgin Olive Oil'; 'chicken thigh' does
    not match plain 'chicken'."""
    if not ingredient_key:
        return None
    need = set(ingredient_key)
    for item_name, item_words in pantry:
        if need <= item_words:
            return item_name
    return None


def match(recipes, pantry_names, max_missing=None, limit=60):
    pantry = [(n, set(normalize(n))) for n in pantry_names]
    results = []
    for r in recipes:
        have = missing = 0
        ingredients = []
        for ing in r["ingredients"]:
            key = normalize(ing["name"])
            staple = " ".join(key) in STAPLES or not key
            found = None if staple else _find_in_pantry(key, pantry)
            if found:
                have += 1
            elif not staple:
                missing += 1
            ingredients.append({**ing, "staple": staple, "have": bool(found), "pantry_item": found})
        if have == 0 or (max_missing is not None and missing > max_missing):
            continue
        results.append({**r, "ingredients": ingredients, "have": have, "missing": missing})
    results.sort(key=lambda r: (r["missing"], -r["have"], r["name"]))
    return results[:limit]
