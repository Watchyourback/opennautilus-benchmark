import template from "../../scripts/leaderboard.html";

// 私人排行榜：页面由 Cloudflare Access 放行（校验其签发的 JWT），其余一律 404。
// 数据是 KV 里的单个键 runs（整份 JSON 数组），由本机 scripts/leaderboard.py --upload 用 wrangler 写入。
let jwks;
const b64 = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));

async function verifyAccess(jwt, env) {
  const [h, p, sig] = (jwt ?? "").split(".");
  if (!sig) return false;
  try {
    const header = JSON.parse(atob(h.replace(/-/g, "+").replace(/_/g, "/")));
    const claims = JSON.parse(new TextDecoder().decode(b64(p)));
    if (header.alg !== "RS256" || claims.exp < Date.now() / 1000) return false;
    if (claims.iss !== `https://${env.ACCESS_TEAM}` || ![claims.aud].flat().includes(env.ACCESS_AUD)) return false;
    jwks ??= await (await fetch(`https://${env.ACCESS_TEAM}/cdn-cgi/access/certs`)).json();
    const jwk = jwks.keys.find((k) => k.kid === header.kid);
    if (!jwk) { jwks = undefined; return false; }
    const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    return crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64(sig), new TextEncoder().encode(`${h}.${p}`));
  } catch {
    return false;
  }
}

const notFound = () => new Response("Not found", { status: 404 });

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/") {
      if (!(await verifyAccess(request.headers.get("Cf-Access-Jwt-Assertion"), env))) return notFound();
      const data = JSON.stringify((await env.RESULTS.get("runs", "json")) ?? []).replaceAll("<", "\\u003c");
      return new Response(template.replace("__ENTRIES__", () => data), { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Robots-Tag": "noindex" } });
    }
    return notFound();
  },
};
