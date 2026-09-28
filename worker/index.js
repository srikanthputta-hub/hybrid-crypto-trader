// Cloudflare Worker for the hybrid-crypto-trader dashboard.
//
// Two jobs:
//  1. fetch() -- serve the static dashboard (web/) via the ASSETS binding,
//     exactly like the old assets-only Worker did.
//  2. scheduled() -- on the Cron Trigger below (see wrangler.jsonc), call
//     GitHub's API to fire refresh-dashboard.yml's workflow_dispatch event.
//
// Why this exists: that workflow's own `schedule:` trigger is GitHub's
// native cron, which is NOT reliable -- observed gaps of up to ~2 hours
// between runs in practice, despite being configured for every 5 minutes.
// Cloudflare Cron Triggers don't have that problem, so this Worker is the
// actual mechanism keeping the bot cycling near-continuously. The workflow
// itself still has `concurrency: { group: refresh-dashboard, cancel-in-progress: false }`,
// so back-to-back dispatches queue safely instead of clashing.
//
// Requires a GITHUB_DISPATCH_TOKEN secret: a fine-grained GitHub personal
// access token scoped to ONLY this repo, with "Actions: read and write"
// permission. Set it via `wrangler secret put GITHUB_DISPATCH_TOKEN` or the
// Cloudflare dashboard (Workers & Pages -> this Worker -> Settings ->
// Variables and Secrets) -- never commit it.

const OWNER = "srikanthputta-hub";
const REPO = "hybrid-crypto-trader";
const WORKFLOW_FILE = "refresh-dashboard.yml";

export default {
  async fetch(request, env) {
    return env.ASSETS.fetch(request);
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatchRefresh(env));
  },
};

async function dispatchRefresh(env) {
  if (!env.GITHUB_DISPATCH_TOKEN) {
    console.error("GITHUB_DISPATCH_TOKEN is not set -- cannot dispatch refresh-dashboard.yml");
    return;
  }

  const res = await fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hybrid-crypto-trader-cron-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main" }),
    }
  );

  if (!res.ok) {
    console.error(`workflow_dispatch failed: ${res.status} ${await res.text()}`);
  }
}
