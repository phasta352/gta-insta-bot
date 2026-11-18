// Instagram bot - Cloudflare Worker "timer"
//
// Runs every minute (Cron Trigger) and starts the GitHub workflow when:
//   1. it's a scheduled slot (:07 and :37 past the hour, 06:07-21:37 UTC), or
//   2. you pressed a button / sent a command to the Telegram bot since the last run.
//
// It only *peeks* at Telegram (getUpdates without an offset), so the bot on GitHub
// still reads and confirms your messages exactly as before.
//
// Settings (Worker > Settings > Variables and Secrets):
//   GITHUB_TOKEN        secret  fine-grained token, this repo only, "Actions: Read and write"
//   TELEGRAM_BOT_TOKEN  secret  same value as the GitHub secret
//   GITHUB_REPO         text    e.g.  yourname/cycling-insta-bot
//   WORKFLOW_FILE       text    bot.yml
//   GITHUB_BRANCH       text    main
// Binding (Worker > Settings > Bindings): KV namespace bound as  STATE

const SLOT_MINUTES = [7, 37];
const FIRST_HOUR_UTC = 6;
const LAST_HOUR_UTC = 21;

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env, new Date(event.scheduledTime)));
  },

  // Visiting the Worker's URL shows a status page. Add ?check=1 for a full self-test.
  async fetch(request, env) {
    const url = new URL(request.url);
    const last = await env.STATE.get("last_update_id");
    const lastRun = await env.STATE.get("last_dispatch");
    const lastErr = await env.STATE.get("last_error");
    let text =
      `Bot timer is running.\n` +
      `Last Telegram update handled: ${last || "none"}\n` +
      `Last workflow start: ${lastRun || "none yet"}\n` +
      `Last error: ${lastErr || "none"}\n`;
    if (url.searchParams.get("check")) text += "\n" + (await selfTest(env));
    else text += "\nOpen this page with ?check=1 at the end of the address for a full self-test.\n";
    return new Response(text, { headers: { "content-type": "text/plain; charset=utf-8" } });
  },
};

async function selfTest(env) {
  const out = ["SELF-TEST"];
  const need = ["GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN", "GITHUB_REPO", "WORKFLOW_FILE"];
  for (const k of need) out.push(`${env[k] ? "OK " : "MISSING"}  setting ${k}`);
  out.push(`${env.STATE ? "OK " : "MISSING"}  KV binding STATE`);
  out.push(`     GITHUB_REPO = "${env.GITHUB_REPO || ""}", WORKFLOW_FILE = "${env.WORKFLOW_FILE || ""}", ` +
           `GITHUB_BRANCH = "${env.GITHUB_BRANCH || "main"}"`);

  // Telegram
  try {
    const me = await (await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/getMe`)).json();
    out.push(me.ok ? `OK   Telegram bot @${me.result.username}` : `FAIL Telegram: ${me.description}`);
    const up = await (await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/getUpdates?timeout=0&limit=100`)).json();
    out.push(up.ok ? `OK   Telegram messages waiting for the bot: ${up.result.length}`
                   : `FAIL Telegram getUpdates: ${up.description}`);
  } catch (e) { out.push(`FAIL Telegram: ${e}`); }

  // GitHub: can the token see the workflow?
  try {
    const r = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${env.WORKFLOW_FILE}`, { headers: ghHeaders(env) });
    if (r.status === 200) {
      const wf = await r.json();
      out.push(`OK   GitHub workflow found: "${wf.name}" (state: ${wf.state})`);
    } else {
      out.push(`FAIL GitHub workflow lookup: HTTP ${r.status} ${(await r.text()).slice(0, 200)}`);
      if (r.status === 404) out.push("     -> check GITHUB_REPO (owner/repo) and WORKFLOW_FILE, and that the token includes this repo");
      if (r.status === 401) out.push("     -> the token is wrong or expired");
    }
  } catch (e) { out.push(`FAIL GitHub: ${e}`); }

  out.push("\nIf everything is OK but nothing runs: check Settings > Trigger Events has the cron '* * * * *',");
  out.push("and look at the Worker's Logs (Observability) around :07 / :37 past the hour.");
  return out.join("\n");
}

function ghHeaders(env) {
  return {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "insta-bot-timer",
  };
}

async function tick(env, now) {
  const reasons = [];

  // 1. Scheduled slot?
  const h = now.getUTCHours();
  const m = now.getUTCMinutes();
  if (SLOT_MINUTES.includes(m) && h >= FIRST_HOUR_UTC && h <= LAST_HOUR_UTC) {
    reasons.push("schedule");
  }

  // 2. New Telegram activity (button press or command)?
  let newestId = null;
  try {
    const r = await fetch(
      `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/getUpdates?timeout=0&limit=100`
    );
    const data = await r.json();
    if (data.ok && data.result.length) {
      newestId = Math.max(...data.result.map((u) => u.update_id));
      const handled = Number((await env.STATE.get("last_update_id")) || 0);
      if (newestId > handled) reasons.push("telegram");
      // Stop the loading spinner on new button presses straight away.
      for (const u of data.result) {
        if (u.update_id > handled && u.callback_query) {
          await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/answerCallbackQuery`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ callback_query_id: u.callback_query.id, text: "Got it, working on it…" }),
          }).catch(() => {});
        }
      }
    }
  } catch (e) {
    console.log("Telegram check failed:", e);
  }

  if (!reasons.length) return;

  const ok = await startWorkflow(env);
  console.log(`Start workflow (${reasons.join(", ")}): ${ok === true ? "ok" : "FAILED"}`);
  if (ok !== true) {
    // Save the error, but at most every 30 minutes (the free plan allows 1,000 storage writes a day).
    const prev = await env.STATE.get("last_error_at");
    if (!prev || now - new Date(prev) > 30 * 60 * 1000) {
      await env.STATE.put("last_error", `${now.toISOString()} starting workflow: ${ok}`);
      await env.STATE.put("last_error_at", now.toISOString());
    }
    return;
  }
  {
    await env.STATE.put("last_dispatch", `${now.toISOString()} (${reasons.join(", ")})`);
    if (newestId !== null) await env.STATE.put("last_update_id", String(newestId));
  }
}

async function startWorkflow(env) {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${env.WORKFLOW_FILE}/dispatches`;
  const r = await fetch(url, {
    method: "POST",
    headers: ghHeaders(env),
    body: JSON.stringify({ ref: env.GITHUB_BRANCH || "main", inputs: { mode: "run" } }),
  });
  if (r.status === 204) return true;
  const msg = `GitHub said HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`;
  console.log(msg);
  return msg;
}
