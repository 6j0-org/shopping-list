# Shopping List

A small self-hosted shopping list. Python 3's standard library, plus [`cryptography`](https://cryptography.io/) to check sign-in tokens.

- Add, edit (tap an item), check off, and delete items. The search box at the top of the List and Pantry pages (turned on in Settings) narrows them to items whose name contains every word you type. Name fields suggest names from TheMealDB's list of about 1,000 generic ingredients (the one recipes use) as you type. A name changed when editing an item is checked against that list, so it says "Peanut Butter", not "pnut butter": a name that isn't on it offers the closest few, or to save it anyway (for things like paper towels). Names typed into the add forms are added as typed; the same check is there, but turned off (`CHECK_WHEN_ADDING` in `static/app.js`), because its matches weren't good enough.
- Quantity is free text: `2`, `2oz`, `a fuckton`, whatever. You set it by editing an item; the add forms only ask for it with "Quantity when adding" turned on in Settings.
- Items are grouped and sorted by store aisle (Produce → Bakery → Deli → Meat → Dairy → Frozen → … → Pet). The aisle is guessed from the name (`sections.py`); you can override it per item by editing it (or when adding, with "Aisle when adding" turned on in Settings), and an override sticks when you rename the item.
- **Pantry** tab: scan barcodes with your phone camera to record what you already have. The camera stays on: each product it finds in the barcode databases goes straight into the pantry with a beep, so you can scan one item after another. A barcode the databases don't know gets a low beep instead: set that item aside, keep scanning, and add the set-aside items by hand when you close the scanner. The 🛒 button on a pantry item adds it to the shopping list.
- **Settings** tab: turn on the extras that are hidden to keep things simple: search, a quantity box and an aisle menu on the add forms, and typing a barcode number on the Pantry page. All are off to start, and they're saved with each person's account, so they follow you across devices and people sharing a list can set them differently. Turning one off only hides it; nothing already saved changes.
- **Recipes** tab: recipes you can make from your pantry, with the fewest missing ingredients first. Choose how many missing ingredients are OK (0–3 or any; 1 to start). Open a recipe to see its ingredients and instructions, and use "Add N missing to list" to put what you lack on the shopping list.
- **Accounts:** people sign in with an anonymous identity from a [6j0.org identity server](https://gitlab.com/6j0-org/id) (`id.6j0.org`), so one passkey (or username and password) covers every 6j0.org app. Everyone who signs in gets their own list and pantry, empty to start.
- **Share** button: make an invite link and send it to someone (a household, say). When they open it and choose Join, you share one list and pantry. Each link works once, for 7 days. Anyone on a list can invite people, take people off it, or leave; whoever is taken off or leaves starts over with an empty list.
- **Siri** button: make a personal token so an iPhone or iPad shortcut can add items when you ask Siri ("Hey Siri, add to shopping list"). The dialog walks through building the shortcut. The token can only add items to your list (`POST /api/shortcut/items`), and it follows you if you join someone else's list. Making a new one replaces the old one, and you can turn it off. Names from Siri lose dictation's trailing period and take TheMealDB's spelling when they're one of its ingredients ("peanut butter." becomes "Peanut Butter").
- Leave it alone for 15 seconds and, one time in ten, Hot Dog Man leans in from the side of the screen (`static/hotdog.js`). One click, tap, or key press sends him away, and it doesn't reach whatever was under him. He stays out of open dialogs, like the scanner. Once you've sent him away, an "I like hot dogs" setting appears in Settings: it's on, and turning it off keeps him away for good.
- **Feedback** button: signed-in people read everyone's feedback, post their own, and reply in each post's thread (see [Feedback](#feedback)).

## Run

```sh
pip install -r requirements.txt   # cryptography, for Ed25519 sign-in tokens
ID_ORIGIN=https://id.6j0.org python3 server.py   # http://localhost:8000
```

The identity server must list this app in its `APPS`, with the origin people open it at (e.g.
`APPS=shopping-list=https://shopping-list.6j0.org`), since it only sends sign-ins back to that origin.

**Upgrading from one shared list.** Before lists were per person, there was one list, shared by the identities in
`ALLOWED_SUBS`. On startup, the server puts the items that belong to no list, and the identities in `ALLOWED_SUBS`
that don't have a list yet, on one list together, and logs what it did. Without `ALLOWED_SUBS`, those items stay put
(and the server says so at startup), and everyone who signs in starts with an empty list. Once it has run, you can
remove `ALLOWED_SUBS`.

Data lives in `shopping.db` (SQLite) next to `server.py`, and feedback in `feedback.jsonl` next to the database. Use `--db PATH` to change it.

Every option can also be set with an environment variable (`python3 server.py --help` lists them all):

| Variable | Default | Purpose |
|---|---|---|
| `SHOPPING_HOST` | `127.0.0.1` | Listen address (`0.0.0.0` for your LAN) |
| `SHOPPING_PORT` | `8000` | Listen port |
| `SHOPPING_DB` | `shopping.db` next to `server.py` | SQLite database |
| `SHOPPING_FEEDBACK` | `feedback.jsonl` next to the database | [Feedback](#feedback) file |
| `SHOPPING_CERT`, `SHOPPING_KEY` | unset | TLS certificate and key (PEM) to serve HTTPS |
| `ID_ORIGIN` | required | The 6j0.org identity server, e.g. `https://id.6j0.org` |
| `ID_APP` | `shopping-list` | This app's name in the identity server's `APPS`. Never rename it. |
| `ID_JWKS_URL` | `ID_ORIGIN/.well-known/jwks.json` | Where to fetch the identity server's public keys (e.g. an in-cluster Service URL) |
| `ALLOWED_SUBS` | empty | Only for [upgrading from one shared list](#run): the 6j0.org identities (their `sub`), comma- or space-separated, that shared it. |
| `ADMIN_SUB` | unset | The 6j0.org identity that can close and delete anyone's feedback, and whose posts and replies are labeled "Developer". |
| `ORIGIN` | unset | This app's public origin, e.g. `https://shopping-list.6j0.org`. `https:` turns on `Secure` cookies. |

To try it locally, run the identity server on `localhost` and this app on `127.0.0.1` (cookies ignore the port, so
two apps on one hostname would overwrite each other's session cookie):

```sh
# in the id repo
APPS=shopping-list=http://127.0.0.1:8000 ORIGIN=http://localhost:4000 npm start
# here
ID_ORIGIN=http://localhost:4000 python3 server.py   # open http://127.0.0.1:8000
```

### Sign-in

1. "Sign in with 6j0.org" (`static/signin.js`) makes a random nonce and an ephemeral ECDH key pair, and goes to
   `ID_ORIGIN/authorize?app=shopping-list&nonce=…&epk=…`.
2. The identity server signs the person in and sends the browser back to `/` with a signed token (an Ed25519 JWT)
   in the URL fragment, which never reaches a server. It also sends this app's encryption key, encrypted to the
   ECDH key. Other 6j0.org apps encrypt their data with it; a list can be shared and isn't end-to-end encrypted, so the
   page ignores it (and never kept the ECDH private key).
3. The page checks the nonce and posts the token to `/api/id/login`. `idtoken.py` verifies the signature against
   the identity server's JWKS, plus `iss`, `aud`, `exp`, `nonce`, and one-time use (`jti`). The server then finds
   or creates the account for the token's `sub` (with a new, empty list) and sets a session cookie (`HttpOnly`,
   `SameSite=Lax`, 30 days; the database stores only its SHA-256).

Deleting an identity at `id.6j0.org` makes the identity server `POST` a signed notice to `/api/id/deleted`. The
server then deletes that account, its sessions, and everything it wrote in Feedback. If nobody else is on its list,
the list and its items go too; otherwise they stay with the others.

Requests marked `Sec-Fetch-Site: cross-site` that change data are refused.

### Feedback

The Feedback button in the header opens a page where signed-in people read everyone's feedback and post their own:
plain text, up to 2048 characters. Each post is a chat: tapping it opens its thread, where anyone can reply. Posts
and replies show no author (the post's author is "Tester" in its thread); the server files each one under the
account's random public ID so people can edit and delete their own. Each account can post, reply, or edit 100 times
a day, and everyone together 500 times a day. Set `ADMIN_SUB` to your identity's `sub` to close threads as Done or
Won't do (or reopen them), delete anyone's posts, and have yours labeled "Developer".

Feedback is shared and isn't encrypted, and the page says so. It's kept in `feedback.jsonl`, one JSON object per
line, appended to and never edited in place: posts, replies (`{"reply": post id, …}`), status changes
(`{"status": post id, "value": "done"}`), edits (`{"edit": post or reply id, "body": …}`), deletions
(`{"delete": id}`), and erased accounts (`{"erase": account id}`). Read it with `tail -f`, `grep`, or `jq`. The
server loads it at startup and, if anything was deleted or edited, rewrites it without the deleted posts and their
replies and with edits folded in, so deleted and replaced text is gone from the disk after the next restart.

`feedback.py` is a Python port of `feedback.js` from the other 6j0.org apps (bored, glaze-journal), with the same
behavior and file format, so the same `feedback.jsonl` reads in either. `static/feedback.js` draws the page, and is
the same file those apps use, unchanged (style it with the `fb-*` classes in `style.css`).

### Tests

```sh
python3 -m unittest discover -s tests -t .
```

`tests/test_feedback.py` tests `feedback.py` on its own (a port of the Node apps' feedback test, plus checks that it
reads files the Node version wrote). `tests/test_idtoken.py` and `tests/test_server.py` run against a stand-in
identity server (`tests/fakeid.py`) that signs tokens like the real one: sign-in, a list per person, invites, the
upgrade from one shared list, feedback, identity
deletion, and refused tokens.

### Container

The image runs as an unprivileged user (uid 10001), listens on port 8000, and keeps its database and `feedback.jsonl` in `/data`. Mount a volume at `/data`.

```sh
docker build -t ghcr.io/6j0-org/shopping-list:0.1.0 .
docker run --rm -p 8000:8000 -v shopping-data:/data \
  -e ID_ORIGIN=https://id.6j0.org -e ORIGIN=https://shopping-list.6j0.org \
  ghcr.io/6j0-org/shopping-list:0.1.0
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

On the phone, open `https://<LAN-IP>:8000` and accept the certificate warning. Signing in only works at an origin the identity server lists in its `APPS` (here, `https://<LAN-IP>:8000`). Without HTTPS everything except the camera still works, and you can type barcode numbers in by hand.

## Barcode lookups

Barcodes are looked up server-side and cached in the database:

1. [Open Food Facts](https://world.openfoodfacts.org/data): free, open database (ODbL) of ~3M+ food products worldwide. No key needed.
2. [UPCitemdb](https://www.upcitemdb.com/api/explore) trial endpoint, as a fallback for non-food items. No key needed, but limited to about 100 lookups per day per IP.

If neither database knows the product, you type the name in yourself.

The page's footer credits the open services the app runs on: Open Food Facts, TheMealDB, the 6j0.org identity server, and ZXing on jsDelivr. UPCitemdb is a commercial trial API, so it isn't listed.

Scanning uses the browser's built-in `BarcodeDetector` (Chrome/Android). On iOS Safari and Firefox it loads [ZXing](https://github.com/zxing-js/browser) from jsDelivr instead.

## Recipes

Recipes come from [TheMealDB](https://www.themealdb.com/api.php) (~800 recipes, free test API key `1`, no signup). The free API can only filter by one ingredient at a time, so the server downloads the whole catalog (26 requests, about 2 seconds) on startup, caches it in SQLite, and refreshes it weekly, along with TheMealDB's ingredient list (one more request), which the list and the Pantry use to suggest names. Matching happens locally (`recipes.py`):

- Words that only describe an ingredient (fresh, chopped, extra virgin, unsalted…) are ignored, plurals are folded, and British names are mapped to US ones (aubergine→eggplant, coriander→cilantro, prawn→shrimp…).
- An ingredient counts as "have" when your pantry item's name contains all of its remaining words. So "Kirkland Extra Virgin Olive Oil" covers "Olive Oil", but a pantry item named just "chicken" does **not** cover "Chicken Thighs".
- Salt, pepper and water are assumed. Oil is not, so add it to your pantry.

TheMealDB's terms say the test key is for development and education. If you deploy this publicly, get a supporter key and change `API` in `recipes.py`.

## API

Everything except `/api/config`, `/api/id/login`, `/api/id/deleted`, `/api/logout`, and `/api/shortcut/items` (which
takes an API token instead) needs a session; without one it answers 401. Items and recipes are those of the signed-in person's list; an item on another list answers 404.

| Method | Path | Body / notes |
|---|---|---|
| GET | `/api/config` | `{idOrigin, idApp}` |
| POST | `/api/id/login` | `{token, nonce}` → `{ok}` and sets the session cookie |
| POST | `/api/id/deleted` | `{token}`, a signed deletion notice from the identity server; erases that account |
| POST | `/api/logout` | Ends the session |
| GET | `/api/me` | `{isAdmin, settings}`; `settings` is `{search, quantity, aisle, barcodeTyping, hotDogs, metHotDogMan}`, each true or false (`hotDogs` starts true, the rest false; `metHotDogMan` turns true when you first send Hot Dog Man away, and shows the `hotDogs` setting) |
| PUT/PATCH | `/api/settings` | Any of the settings (true or false) → all of them |
| GET | `/api/token` | `{token: null, created?, lastUsed?}`: whether you have an API token (times in ms; `lastUsed` is 0 if never) |
| POST | `/api/token` | → `{token, created, lastUsed}`, a new API token, replacing any old one. The only time the token is shown |
| DELETE | `/api/token` | Turns off your API token |
| POST | `/api/shortcut/items` | With `Authorization: Bearer <API token>` instead of a session: `{name, quantity?}` → `{item, message}`; `message` (also on errors, with `error`) is meant to be spoken, like "Added Peanut Butter to the shopping list." |
| GET | `/api/list` | `{members: [{id, you}]}`, the people on your list; `id` is the account's random public ID, never its `sub` |
| POST | `/api/list/invites` | → `{code, days}`; the invite link is `/#join=<code>`, single use |
| POST | `/api/list/join` | `{code}` → `{members, moved}`. If you were alone on your list, its `moved` items come along and it's deleted |
| DELETE | `/api/list/members/:id` | Takes that person (or, with your own `id`, you) off the list, onto a new empty one → `{members}` of your list |
| GET | `/api/items` | All items, sorted by aisle |
| POST | `/api/items` | `{name, quantity?, section?, barcode?}`. `section` is omitted or `"auto"` to guess it |
| PUT/PATCH | `/api/items/:id` | Any of `{name, quantity, section, checked}` |
| DELETE | `/api/items/:id` | |
| POST | `/api/items/clear-checked` | Deletes checked shopping-list items |
| GET | `/api/sections` | Aisle order |
| GET | `/api/barcode/:code` | `{found, name, quantity, source}` |
| GET | `/api/ingredients` | `{ingredients}`: TheMealDB's ingredient names, sorted, for suggesting item names |
| GET | `/api/recipes?max_missing=N` | Recipes matching the pantry, fewest missing first (top 60) |
| GET | `/api/feedback` | `{items: [{id, body, created, edited, updated, admin, mine, canDelete, status, replies}], maxLength, isAdmin}`, most recently active first |
| POST | `/api/feedback` | `{body}` → the new item (400 if empty or over 2048 characters, 429 over the daily limits) |
| GET | `/api/feedback/:id` | The item plus `{messages: [{id, body, created, edited, admin, mine, op}], canReply, canClose}` |
| PUT | `/api/feedback/:id` | `{body}` → the thread, with your post edited |
| PUT | `/api/feedback/:id/replies/:replyId` | `{body}` → the thread, with your reply edited |
| POST | `/api/feedback/:id/replies` | `{body}` → the thread |
| POST | `/api/feedback/:id/status` | `{status}` (`open`, `done`, or `wontdo`) → the thread; `ADMIN_SUB` only |
| DELETE | `/api/feedback/:id` | Your own, or anyone's for `ADMIN_SUB` |
