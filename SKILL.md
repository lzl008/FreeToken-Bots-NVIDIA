---
name: freetoken-bots-nvidia
description: Discover, verify, and maintain zero-cost NVIDIA API-catalog endpoints for Pi child-agent workflows used by Muse, Grokbot, or Dot. Activates when the user mentions NVIDIA free tokens, NVIDIA NIM, free models （免费 token / 免费模型）, or wants to run a child agent on NVIDIA instead of the host's own quota. Use for Pi setup, free-endpoint discovery, quota-safe fallback, model recommendations, and daily health checks.
version: 1.0.0
---

# FreeToken-Bots-NVIDIA

Pi is the execution layer; Muse, Grokbot and Dot may invoke it **only if they can run shell commands or reach a separately deployed Pi service**. Do not claim to replace those products' native models. This skill routes child-agent work onto NVIDIA's own free endpoints so the host's quota is not spent — it does not add capability the host lacks.

This is the NVIDIA-only port of the OpenRouter skill. It keeps the original's discipline and drops the parts that only made sense for OpenRouter (there is no `:free` suffix and no `pricing` field on NVIDIA).

## Activation

Use when the user mentions **NVIDIA**, **NIM**, **免费 token**, **免费模型**, `build.nvidia.com`, or asks about running a child agent on free NVIDIA capacity. On first activation in a session, run Step 0, then `scan` if the cached report is older than 24h.

## Step 0 — Install Pi first (required)

Nothing works without Pi. If `command -v pi` fails, install it **before** anything else:

```bash
curl -fsSL https://pi.dev/install.sh | sh          # official installer
pi --version
```

Alternative (Node.js ≥ 22.19): `npm install -g @earendil-works/pi-coding-agent`

Notes:
- Pi keeps config in `~/.pi/agent/` (`settings.json`, `models.json`); that directory survives VM replacement but the binary may not — re-run this step whenever `pi` is off PATH.
- Then get an NVIDIA key at <https://build.nvidia.com> (any model page → **Get API Key**, format `nvapi-…`) and export it as `NVIDIA_API_KEY`. Never write it to git or echo it.

## Principles

- **Free means two independent yeses.** The published catalog marks the model as a *Free Endpoint* **and** `GET {base}/v1/models` still serves it. Either half alone authorises nothing: endpoints get retired, and the API keeps listing models that are no longer free. As of the last scan, 17 catalog-free models were already gone from the API.
- **The catalog description is evidence, not truth.** `build.nvidia.com` card text is occasionally duplicated between models, so `parameters_b` is a hint used for shortlisting. Size is never inferred from context length, ID digits, or model family; a model whose card states no size stays in `needs_parameter_verification`.
- **Tool calling needs its own probe.** A plain completion proves nothing about `tools`. `radar.py probe` makes a second, separate call with a tool definition and reports `chat_ok` and `tools_ok` independently.
- **Fail-closed model resolution.** `probe --model` refuses any id not in the latest scan (exit 2). NVIDIA has no `:free` suffix to fall back on, so there is no auto-correction path — an unknown id is simply refused, because calling it may spend paid quota.
- **Never route to a paid model automatically.** A 401/402/403/429 is not a reason to remove or rewrite the user's existing Pi provider configuration.
- **`sync` proposes, it never writes.** Pi's model schema varies by release; `models.json` is left untouched and the proposal is handed over for schema validation.
- **No API key in git, ever.** Prompt for `NVIDIA_API_KEY` if live probing is requested and it is unset.
- All unattended `pi -p` calls must use `</dev/null`.

## Usage

From this skill directory:

```bash
python3 scripts/radar.py scan
python3 scripts/radar.py status
NVIDIA_API_KEY=... python3 scripts/radar.py probe --model 'exact/nvidia-model-id'
python3 scripts/radar.py sync
python3 scripts/radar.py scan --min-params 20      # widen the pool to 20B+
python3 scripts/refresh_catalog.py                 # re-scrape build.nvidia.com
```

`scan` writes a private report to `~/.pi/agent/freetoken-bots.json` (0600). `--min-params` defaults to 100 (billions) for the flagship pool; the report also lists the smaller free chat models under `other_free_endpoints` so a 30B model can be chosen deliberately rather than by lowering the bar blindly.

## Host integration workflow

0. Step 0 complete: `pi` on PATH, `pi --version` works, `NVIDIA_API_KEY` set. Otherwise install first.
1. Inspect `~/.pi/agent/settings.json`, `~/.pi/agent/models.json` and `pi --list-models` without disclosing keys.
2. `scan`. Read `eligible` (≥ threshold, chat) — do not promote `needs_parameter_verification` by hand-waving a size.
3. `probe` every model you intend to use. Keep chat and tool results separate; distinguish a temporary 429 from exhausted credit.
4. `sync` and check the proposal against the installed host's schema. Generate a candidate diff; do **not** overwrite custom providers or `enabledModels`.
5. Require explicit approval of the diff before applying. Back up originals, write atomically, validate with `python3 -m json.tool`, run `pi --list-models`, then `pi -p 'say pong' --no-extensions </dev/null`. Revert on failure.
6. For Muse/Grokbot/Dot use a controlled shell child-agent invocation. Confirm the host can execute a local Pi command, share a workspace, and capture results. Never expose Pi's shell or API to the public internet without authentication.
7. Ask the user which model to use before major tasks; use automatic fallback only after the user opts in. Never replay side-effectful actions after uncertain completion.

## Scheduling (required, not optional)

This skill does **not** scan by itself. A daily scan is **required** — free-endpoint eligibility on NVIDIA is dynamic and the API list changes without notice.

- Run `python3 /absolute/path/to/scripts/radar.py scan` **every day at 08:00 local time** on the machine running Pi. Use cron, launchd, or a systemd timer — pick one and keep it. Example cron: `0 8 * * * /usr/bin/python3 /path/to/scripts/radar.py scan`
- **On install, tell the user explicitly:** "这个 skill 需要每天早上 8 点跑一次扫描定时任务，不配的话免费名单会过期，它自己不会动。" Do not present the skill as working without the timer.
- Each run: compare against the previous report and surface newly listed free models, delisted models, models that lost free status, and any change to `eligible`.
- Refresh the catalog (`refresh_catalog.py`) weekly; `--check` reports drift without writing. The catalog changes more slowly than the API list.
- A chat reminder does not run on that machine. Confirm repeated transient errors before marking a model unusable. Keep `eligible`, `needs_parameter_verification`, and inactive states separate.

## Model selection

Prefer verified free ≥100B tool-capable models when available, but report a smaller tool-capable free model separately when that is operationally better — many capable free NVIDIA endpoints sit at 20–90B. If nothing passes, report an empty pool rather than silently falling back to paid tokens.

## Maintenance notes

- `build.nvidia.com` answers requests carrying `Accept` or `Accept-Language` with a 202 anti-bot shell page (2.4 KB, zero model cards). Send only `User-Agent`. Documented in `refresh_catalog.py`.
- The same host intermittently serves an incomplete page. Both that and the shell parse as "a large batch of models was delisted", so `refresh_catalog.py` treats "a page yielded no cards" as transport failure (retried, then aborts without writing) and refuses to write a catalog below 70% of the committed size unless `--force`. Never weaken this: one network hiccup would otherwise erase a good catalog.
- Catalog ids and API ids differ in punctuation (`z-ai/glm-5-3` vs `z-ai/glm-5.3`). Matching literally silently drops flagships; `norm_name` normalises both sides and the mapping is recorded in the report's `catalog_aliases`.
- Only `data/nvidia_catalog.json`'s `overrides` block is safe to hand-edit; `models` is regenerated on every refresh.
