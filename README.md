# Shopping List

A small self-hosted shopping list. No accounts and no dependencies beyond Python 3 (standard library only).

- Add, edit (tap an item), check off, and delete items.
- Quantity is free text: `2`, `2oz`, `a fuckton`, whatever.
- Items are grouped and sorted by store aisle (Produce → Bakery → Deli → Meat → Dairy → Frozen → … → Pet). The aisle is guessed from the name (`sections.py`); you can override it per item, and an override sticks when you rename the item.
- **Pantry** tab: scan barcodes with your phone camera to record what you already have. The 🛒 button on a pantry item adds it to the shopping list.
- **Recipes** tab: recipes you can make from your pantry, with the fewest missing ingredients first. Choose how many missing ingredients are OK (0–3 or any). Open a recipe to see its ingredients and instructions, and use "Add N missing to list" to put what you lack on the shopping list.

## Run

```sh
python3 server.py                 # http://localhost:8000
```

Data lives in `shopping.db` (SQLite) next to `server.py`. Use `--db PATH` to change it.

Every option can also be set with an environment variable: `SHOPPING_HOST`, `SHOPPING_PORT`, `SHOPPING_DB`, `SHOPPING_CERT`, `SHOPPING_KEY`.

### Container

The image runs as an unprivileged user (uid 10001), listens on port 8000, and keeps its database at `/data/shopping.db`. Mount a volume at `/data`.

```sh
docker build -t ghcr.io/6j0-org/shopping-list:0.1.0 .
docker run --rm -p 8000:8000 -v shopping-data:/data ghcr.io/6j0-org/shopping-list:0.1.0
```

To publish it, give your GitHub token the `write:packages` scope, log in, and push:

```sh
gh auth refresh -s write:packages
gh auth token | docker login ghcr.io -u "$(gh api user -q .login)" --password-stdin
docker push ghcr.io/6j0-org/shopping-list:0.1.0
```

New ghcr.io packages are private. Either make it public (the package's **Package settings → Change visibility**) or give the cluster an image pull secret.

It's deployed to the cluster from the `worclustershire-deploy` repo (`apps/shopping-list`). To release a new version, push a new tag and update `image.tag` in that app's `values.yaml`.

### Using it from a phone (camera scanning)

Browsers only allow camera access over HTTPS (or on `localhost`), so to scan from a phone, serve the app over HTTPS on your LAN.

**1. Create a self-signed certificate.** Replace `<LAN-IP>` with your computer's LAN address:

```sh
openssl req -x509 -newkey rsa:2048 -nodes -days 825 -keyout key.pem -out cert.pem -subj "/CN=shopping-list" -addext "subjectAltName=IP:<LAN-IP>,IP:127.0.0.1,DNS:localhost"
```

**2. Open port 8000 in the firewall until the next reboot or firewall reload.** This assumes an `inet filter` table with an `input` chain, the nftables default:

```sh
sudo nft insert rule inet filter input tcp dport 8000 accept
```

**3. Run the server:**

```sh
python3 server.py --host 0.0.0.0 --cert cert.pem --key key.pem
```

On the phone, open `https://<LAN-IP>:8000` and accept the certificate warning. There are no accounts, so anyone on your network can reach the list. Without HTTPS everything except the camera still works, and you can type barcode numbers in by hand.

## Barcode lookups

Barcodes are looked up server-side and cached in the database:

1. [Open Food Facts](https://world.openfoodfacts.org/data): free, open database (ODbL) of ~3M+ food products worldwide. No key needed.
2. [UPCitemdb](https://www.upcitemdb.com/api/explore) trial endpoint, as a fallback for non-food items. No key needed, but limited to about 100 lookups per day per IP.

If neither database knows the product, you type the name in yourself.

Scanning uses the browser's built-in `BarcodeDetector` (Chrome/Android). On iOS Safari and Firefox it loads [ZXing](https://github.com/zxing-js/browser) from jsDelivr instead.

## Recipes

Recipes come from [TheMealDB](https://www.themealdb.com/api.php) (~800 recipes, free test API key `1`, no signup). The free API can only filter by one ingredient at a time, so the server downloads the whole catalog (26 requests, about 2 seconds) on startup, caches it in SQLite, and refreshes it weekly. Matching happens locally (`recipes.py`):

- Words that only describe an ingredient (fresh, chopped, extra virgin, unsalted…) are ignored, plurals are folded, and British names are mapped to US ones (aubergine→eggplant, coriander→cilantro, prawn→shrimp…).
- An ingredient counts as "have" when your pantry item's name contains all of its remaining words. So "Kirkland Extra Virgin Olive Oil" covers "Olive Oil", but a pantry item named just "chicken" does **not** cover "Chicken Thighs".
- Salt, pepper and water are assumed. Oil is not, so add it to your pantry.

TheMealDB's terms say the test key is for development and education. If you deploy this publicly, get a supporter key and change `API` in `recipes.py`.

## API

| Method | Path | Body / notes |
|---|---|---|
| GET | `/api/items` | All items, sorted by aisle |
| POST | `/api/items` | `{name, quantity?, section?, barcode?}`. `section` is omitted or `"auto"` to guess it |
| PUT/PATCH | `/api/items/:id` | Any of `{name, quantity, section, checked}` |
| DELETE | `/api/items/:id` | |
| POST | `/api/items/clear-checked` | Deletes checked shopping-list items |
| GET | `/api/sections` | Aisle order |
| GET | `/api/barcode/:code` | `{found, name, quantity, source}` |
| GET | `/api/recipes?max_missing=N` | Recipes matching the pantry, fewest missing first (top 60) |
