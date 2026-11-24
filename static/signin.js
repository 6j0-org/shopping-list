// Sign-in with a 6j0.org identity (https://gitlab.com/6j0-org/id).
//
// 1. startIdSignIn: make a random nonce and an ephemeral ECDH P-256 key pair, and go to
//    <idOrigin>/authorize?app=<idApp>&nonce=<nonce>&epk=<x>.<y>.
// 2. The identity server signs the person in and sends the browser back here with
//    #id_token=<signed token>&epk=…&key=…&state=<nonce>. The fragment never reaches any server.
// 3. finishIdSignIn: check the nonce and have the server verify the token, which starts a session.
//
// The identity server always hands over this app's encryption key (`key`, encrypted to the ECDH key), since other
// 6j0.org apps encrypt their data with it. A shopping list can be shared and isn't end-to-end encrypted, so it ignores
// the key and throws the private key away right after making it: only the nonce is kept.

const PENDING = "shopping-list:id-pending";
const PENDING_MAX_AGE_MS = 10 * 60 * 1000;

const b64u = (bytes) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

export const hasIdCallback = () => new URLSearchParams(location.hash.slice(1)).has("id_token");

export async function startIdSignIn({ idOrigin, idApp }) {
  const pair = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  const { x, y } = await crypto.subtle.exportKey("jwk", pair.publicKey);
  const nonce = b64u(crypto.getRandomValues(new Uint8Array(24)));
  localStorage.setItem(PENDING, JSON.stringify({ nonce, created: Date.now() }));
  location.assign(`${idOrigin}/authorize?${new URLSearchParams({ app: idApp, nonce, epk: `${x}.${y}` })}`);
}

// Returns the server's answer, { ok }. Throws with a message fit to show people.
export async function finishIdSignIn() {
  const frag = new URLSearchParams(location.hash.slice(1));
  history.replaceState(null, "", location.pathname + location.search); // drop the token from the address bar
  let pending = null;
  try { pending = JSON.parse(localStorage.getItem(PENDING) || "null"); } catch {}
  localStorage.removeItem(PENDING);
  if (!pending || pending.nonce !== frag.get("state") || Date.now() - pending.created > PENDING_MAX_AGE_MS)
    throw new Error("That sign-in link expired or was started somewhere else. Please sign in again.");

  let r;
  try {
    r = await fetch("/api/id/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: frag.get("id_token"), nonce: pending.nonce }),
    });
  } catch { throw new Error("Can't reach the server. Check your connection."); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Sign-in failed (HTTP ${r.status}).`);
  return data;
}
