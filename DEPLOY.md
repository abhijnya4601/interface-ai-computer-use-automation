# Hosting the live console

`webconsole/` needs a machine that stays running. This gets it online at a stable link that
works even when your laptop is off.

The image's default command (`webconsole/serve.sh`) runs both the mock bank (`:5050`, internal)
and the console on `$PORT` (public). Discovered capabilities go to `$CAPABILITIES_DIR` — point
that at a volume to persist them.

---

## Free, no credit card

> Note: Hugging Face **Docker** Spaces are now a paid feature, and HF's free Gradio/Static
> Spaces can't run a Flask + Playwright backend. The free routes below are Render, a Cloudflare
> tunnel, or GitHub Pages for the static demo.

### 1. Render  (Docker web service) — try this first

Render's free web-service tier runs a Dockerfile, gives you HTTPS, and doesn't ask for a card to
start. It **spins down after ~15 min idle** and cold-starts in ~1 min, and it's **512 MB RAM** —
tight for Chromium, but the container flags (`--disable-dev-shm-usage --no-sandbox`, already set)
make replay runs work; heavy discovery runs may hit the limit.

1. Push your branch to GitHub (`git push -u origin <branch>`).
2. render.com → **New → Web Service** → connect the repo → pick your branch.
   - **Runtime**: Docker · **Instance type**: Free
   - **Health check path**: `/health`
3. **Environment** → add:
   | key | value |
   |---|---|
   | `CONSOLE_ACCESS_KEY` | a random string — your link token |
   | `CONSOLE_ALLOW_BYO_KEY` | `1` |
   | `ANTHROPIC_API_KEY` *or* `OPENAI_API_KEY` *or* `GEMINI_API_KEY` | *(optional — only if you want to pay for Discover runs yourself; otherwise viewers paste their own)* |
   Render sets `PORT` itself; `serve.sh` honours it. No disk on free → discovered capabilities
   reset on redeploy (the 5 curated ones reseed).
4. **Create Web Service.** First build ~5–10 min. Then:
   `https://<service>.onrender.com/?key=<CONSOLE_ACCESS_KEY>`

Every `git push` to that branch redeploys it automatically.

### 2. Cloudflare named tunnel  (free account, no card) — from a machine you keep on

A stable public `https://` URL forwarding to the console running on your own machine (Mac mini,
an always-plugged-in laptop, a home box). Full RAM, but only up while that machine + the tunnel
run.

```bash
brew install cloudflared
cloudflared tunnel login                    # opens browser, free account
cloudflared tunnel create live-console
cloudflared tunnel route dns live-console live-console.<your-cf-domain>   # or use the *.cfargotunnel.com URL it gives
# then, with the mock bank + console already running locally (make app / make console):
cloudflared tunnel run --url http://localhost:5055 live-console
```

Run `cloudflared` as a launchd/systemd service so it restarts with the machine. Share
`https://live-console.<your-cf-domain>/?key=<the key make console printed>`.

*(The zero-setup version — `cloudflared tunnel --url http://localhost:5055`, no login — also
works but the URL is random and changes every restart.)*

### 3. GitHub Pages  (the static demo only)

`docs/live-demo.html` is a self-contained page that replays captured runs — the dashboards, the
redaction sinks, the tenant patch, a task box. Not the live agent, but zero maintenance and
truly always-on. Repo **Settings → Pages → Deploy from a branch → `main` / `docs`**, then it's
at `https://<you>.github.io/<repo>/live-demo.html`.

---

## Needs a card on file

### Fly.io  (reads the Dockerfile; card required even on the free allowance)

```bash
fly launch --no-deploy                       # keep the fly.toml already in the repo; rename `app`
fly volumes create console_data --size 1     # persists discovered capabilities

fly secrets set CONSOLE_ACCESS_KEY="$(openssl rand -base64 12)"   # the link token
#   optional — only if YOU want to pay for discovery runs instead of viewers bringing a key:
# fly secrets set ANTHROPIC_API_KEY=sk-ant-...

fly deploy
fly open                                      # then append  ?key=<the CONSOLE_ACCESS_KEY>
```

The share link is `https://<app>.fly.dev/?key=<CONSOLE_ACCESS_KEY>`. A cookie carries the key
after the first visit. `auto_stop_machines` scales it to zero when idle; the next request
cold-starts it in a few seconds. Set `min_machines_running = 1` in `fly.toml` to keep it warm.
`shared-cpu-1x` / 1 GB runs it comfortably (~a few dollars a month).

### A plain VPS ($4–5 box: Hetzner / DigitalOcean)

```bash
git clone <repo> && cd <repo>
export CONSOLE_ACCESS_KEY=... ANTHROPIC_API_KEY=...   # the second is optional
docker compose up -d bank console                     # console on :5055
# put Caddy or nginx in front for HTTPS on your domain
```

---

## Keys, cost, and who can do what

- **Replay mode needs no key.** It's free and never writes to real data — safe to leave fully open to anyone with the link.
- **Discover mode calls a hosted model and costs money per run.** It works with **Anthropic (Claude), OpenAI (GPT), or Google (Gemini)** — the viewer picks in the UI, or it's auto-detected from the key prefix (`sk-ant-` / `sk-` / `AIza`). Same loop, prompt and tools for all three; the model IDs default to `claude-sonnet-5` / `gpt-4o` / `gemini-2.0-flash` and are overridable with `ANTHROPIC_MODEL` / `OPENAI_MODEL` / `GEMINI_MODEL`. Two ways to allow it:
  - **Bring-your-own-key (default, `CONSOLE_ALLOW_BYO_KEY=1`):** each viewer pastes their own key in the UI. It's sent to that provider through the server for that run and **never written to disk or logged**; it lives only in the viewer's browser tab (sessionStorage). Over HTTPS (Fly/Render give you this) it isn't sniffable in transit — but it does pass through the server process in memory, so only host an instance people you trust are pointing their keys at.
  - **Server key (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` set):** you pay for every discovery run anyone with the link starts; the first of those env vars that's set is used. Only do this behind a link token you treat as a secret, or set `CONSOLE_ALLOW_BYO_KEY=0` and hand the link to a small group.
- **Discovered capabilities are shared.** Every discovery a viewer runs compiles a new
  `capabilities/<name>__<hash>.v1.json` on the volume and appears in everyone's Replay list.
  They land as `lifecycle: draft` with the agent's proposed rules unratified — clearly marked
  as not-yet-verified.
- One run at a time per instance. A run auto-stops at its wall-clock budget (discovery 180s).
