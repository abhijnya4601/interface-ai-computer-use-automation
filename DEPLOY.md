# Hosting the live console

`webconsole/` needs a machine that stays running. This gets it online at a stable link that
works even when your laptop is off.

The image's default command (`webconsole/serve.sh`) runs both the mock bank (`:5050`, internal)
and the console on `$PORT` (public). Discovered capabilities go to `$CAPABILITIES_DIR` — point
that at a volume to persist them.

---

## Free, no credit card

### Hugging Face Spaces  (always-on, free, Docker) — recommended

Everything the Space needs is already in the repo: the `Dockerfile` (default command runs
`webconsole/serve.sh`), the HF config block at the top of `README.md` (`sdk: docker`,
`app_port: 7860`), and UID 1000 / `SPACE_ID`-aware port defaults so it "just runs" there.

1. huggingface.co → **New Space** → SDK **Docker** → **Blank**. No card required.
2. Space **Settings → Variables and secrets**:
   - secret **`CONSOLE_ACCESS_KEY`** = a random string — your link token. *Set this* — without
     it the console generates a new key on every restart and the link keeps changing.
   - (optional) secret `ANTHROPIC_API_KEY` — or leave unset and let viewers paste their own in
     the Discover tab.
3. Push this repo to the Space:
   ```bash
   git remote add space https://huggingface.co/spaces/<you>/<space-name>
   git push space <branch>:main       # first push: use a HF access token as the password
   ```
4. It builds (~5–10 min — Chromium + deps; watch the **Logs** tab), then:
   `https://<you>-<space-name>.hf.space/?key=<CONSOLE_ACCESS_KEY>`

Free Spaces sleep after ~48h idle and wake on the next request (~30s). `/data` is ephemeral on
the free tier, so capabilities discovered through the console reset on a rebuild — the 5 curated
ones always reseed from the image. Replay mode needs no key; Discover needs one.

**Auto-deploy on push:** `.github/workflows/deploy-hf.yml` mirrors your branch to the Space on
every push once you set the repo **secret `HF_TOKEN`** (a HF write token) and **variable
`HF_SPACE`** (`<you>/<space-name>`). Until both are set it's a no-op.

### A quick tunnel from your own machine  (link now, not always-on)

Free, no account. Needs your Mac + the local servers + the tunnel all running.

```bash
brew install cloudflared          # once
make app                          # terminal 1 — mock bank
make console                      # terminal 2 — prints the ?key=
cloudflared tunnel --url http://localhost:5055   # terminal 3 — prints https://<random>.trycloudflare.com
```

Share `https://<random>.trycloudflare.com/?key=<the key make console printed>`.

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

### Render

New **Web Service** from the repo, Docker runtime. Set:

| key | value |
|---|---|
| `PORT` | `5055` (Render injects its own; the console honours `$PORT`) |
| `TARGET_BASE` | `http://localhost:5050` |
| `CAPABILITIES_DIR` | `/data/capabilities` + attach a **Disk** mounted at `/data` |
| `CONSOLE_ACCESS_KEY` | a random string |
| `CONSOLE_ALLOW_BYO_KEY` | `1` |
| `ANTHROPIC_API_KEY` | *(optional — see below)* |

Start command: `bash webconsole/serve.sh`.

### A plain VPS ($5 box)

```bash
git clone <repo> && cd <repo>
export CONSOLE_ACCESS_KEY=... ANTHROPIC_API_KEY=...   # the second is optional
docker compose up -d bank console                     # console on :5055
# put Caddy or nginx in front for HTTPS on your domain
```

---

## Keys, cost, and who can do what

- **Replay mode needs no key.** It's free and never writes to real data — safe to leave fully open to anyone with the link.
- **Discover mode calls the Anthropic API and costs money per run.** Two ways to allow it:
  - **Bring-your-own-key (default, `CONSOLE_ALLOW_BYO_KEY=1`):** each viewer pastes their own key in the UI. It's sent to Anthropic through the server for that run and **never written to disk or logged**; it lives only in the viewer's browser tab (sessionStorage). Over HTTPS (Fly/Render give you this) it isn't sniffable in transit — but it does pass through the server process in memory, so only host an instance people you trust are pointing their keys at.
  - **Server key (`ANTHROPIC_API_KEY` set):** you pay for every discovery run anyone with the link starts. Only do this behind a link token you treat as a secret, or set `CONSOLE_ALLOW_BYO_KEY=0` and hand the link to a small group.
- **Discovered capabilities are shared.** Every discovery a viewer runs compiles a new
  `capabilities/<name>__<hash>.v1.json` on the volume and appears in everyone's Replay list.
  They land as `lifecycle: draft` with the agent's proposed rules unratified — clearly marked
  as not-yet-verified.
- One run at a time per instance. A run auto-stops at its wall-clock budget (discovery 180s).
