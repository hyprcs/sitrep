# sitrep

A self-hosted dashboard that tells you at a glance whether something big is going
wrong in Sydney or Australia: an internet or telco collapse, a major cyber incident,
or a major physical incident. It runs on your own computer or NAS, keeps working
when your internet is down, and only raises the alarm when several independent
sources agree.

> **sitrep is not an emergency service.** In an emergency call **000**. For official
> warnings use [Hazards Near Me NSW](https://www.nsw.gov.au/emergency/hazards-near-me-app),
> the NSW SES, and ABC Emergency.

## What it watches

| Source | Category | What it adds |
|---|---|---|
| Your own connection | Connectivity | Tells "my line is down" apart from "the internet is down" |
| [IODA](https://ioda.inetintel.cc.gatech.edu/) (Georgia Tech) | Network measurement | Australia, NSW or a big carrier dropping off the internet |
| [Cloudflare Radar](https://radar.cloudflare.com/au) | Network measurement | Outage reports, traffic drops and route hijacks for Australia and the big carriers |
| [ASD's ACSC](https://www.cyber.gov.au/) | Official | Cyber security alerts and advisories |
| [AEMO](https://www.aemo.com.au/) | Official | The NSW grid: supply warnings, load shedding, sudden drops in demand |
| [Bluesky](https://bsky.app/) | Social | Sudden spikes in posts about trouble in Sydney and Australia |
| TPG and Aussie Broadband | Information only | What the telcos' own status pages say |

On top of that: alerts on your phone through Telegram, and a simulate mode to
practise with.

**Status.** Everything above works, and is tested on Windows both directly and in
Docker. The Synology NAS steps are written up below but haven't been tried on a NAS yet.
Wikipedia was planned as a second social source but isn't included
([why](#wikipedia-not-included)).

**Contents:**
[How it decides](#how-it-decides-the-overall-status) ·
[The dashboard](#whats-on-the-dashboard) ·
[Quick start on Windows](#quick-start-on-windows-no-docker) ·
[Docker](#running-it-with-docker) ·
[Synology NAS](#deploying-on-a-synology-nas-container-manager) ·
[Settings](#settings) ·
[Keywords and thresholds](#changing-keywords-and-thresholds) ·
[Sources](#sources) ·
[Simulate mode](#simulate-mode-a-practice-emergency) ·
[Troubleshooting](#troubleshooting) ·
[Adding a source](#adding-a-source-or-an-alert-channel) ·
[Data and privacy](#data-and-privacy) ·
[Licence](#licence)

## How it decides the overall status

Every source reports **normal**, **elevated** or **critical**, with a one-line reason.
Sources are grouped into four categories: **connectivity** (your own line), **network
measurement**, **official** and **social**. The overall status only goes up when:

- **two or more categories** are elevated at the same time (overall: *Elevated*), or
- **one category is critical** (overall: *Critical*).

The telcos' own status pages are shown for information only and never count.

**Can't reach the internet** is its own state, shown when your router answers but
nothing outside does. A source that hasn't updated recently is shown as **stale**
with its last-known state, never as normal. Sources that learn what "normal" looks
like show **learning** for their first 7 days and can't raise alerts until then.

To stop flickering, a higher level has to be seen twice in a row (about a minute)
before it shows, and it only comes back down after 15 calm minutes.

## What's on the dashboard

One page, in dark mode, that works on a phone. From the top:

- **Overall status:** the level, a plain-English reason, how long it's been that way,
  and when it will calm down if nothing new happens.
- **Categories:** the four categories and why each is at its level.
- **Sources:** one tile per source with its status, reason, when it last updated and a
  link to the source. A source that stops updating shows **Stale** with its last-known
  state. The "Your connection" tile can show each individual check.
- A section for each source once it has data: **Australian networks** (IODA's
  measurements for Australia, NSW and each carrier over 24 hours), **Australian
  traffic** (Cloudflare Radar, today against last week), **What the telcos say**,
  **On Bluesky** (each keyword group against what's usual, with example posts behind
  a click), **Latest from the ACSC**, and **Electricity grid** (demand against
  yesterday, and AEMO's notices).
- **Your connection, last 24 hours:** a strip showing when your line had trouble.
- **Timeline, last 24 hours:** every change in status, new ACSC items, AEMO notices,
  Bluesky spikes and widespread telco outages.
- At the bottom: how many sources are live, and whether Telegram alerts are on.

Hover over (or tap) a chart to see the exact time and value. If the page loses touch
with sitrep itself, it says so and keeps showing what it last heard.

## Quick start on Windows (no Docker)

You need [Python](https://www.python.org/downloads/) 3.12 or newer, and sitrep's
files: on GitHub, click **Code → Download ZIP** and unzip it, or if you use Git,
`git clone https://github.com/hyprcs/sitrep.git`. Open PowerShell in the sitrep
folder, then run these one at a time.

1. Create a private Python environment for sitrep:
   ```powershell
   python -m venv .venv
   ```
2. Install what sitrep needs:
   ```powershell
   .venv\Scripts\python -m pip install -r requirements-dev.txt
   ```
3. Create your private settings file:
   ```powershell
   copy .env.example .env
   ```
4. Open it and set at least `ROUTER_ADDRESS` (see [Your router's address](#your-routers-address)),
   and if you like `HOME_ISP` and `HOME_CARRIER` (see [Your carrier](#your-carrier)):
   ```powershell
   notepad .env
   ```
5. Start sitrep:
   ```powershell
   .venv\Scripts\python -m sitrep
   ```
6. Open <http://localhost:8080> in your browser. Press **Ctrl+C** in PowerShell to stop it.

## Running it with Docker

Docker runs sitrep in a sealed box with everything it needs, which is how it runs
on a NAS too.

**On Windows**, set Docker up once:

1. Docker Desktop runs on Windows' Linux subsystem (WSL 2). Open PowerShell **as
   administrator** and run:
   ```powershell
   wsl --install --no-distribution
   ```
   then restart the computer.
2. In an ordinary PowerShell window, install Docker Desktop:
   ```powershell
   winget install -e --id Docker.DockerDesktop
   ```
3. Start **Docker Desktop** from the Start menu, accept its terms (it's free for
   personal use), and wait for **Engine running** in the bottom-left corner. It asks
   you to sign in; sitrep doesn't need that, so you can skip it.
4. Open a **new** PowerShell window, so it can find the `docker` command.

Then, in the sitrep folder (after creating `.env` as in steps 3 and 4 above):

```powershell
docker compose up -d --build
```

The first build takes under a minute. sitrep then runs in the background, and starts
again by itself whenever Docker Desktop starts. `docker compose ps` shows **(healthy)**
once it's up. Don't also run `python -m sitrep` at the same time: both would use port
8080 and the same `data` folder.

- See what it's doing: `docker compose logs -f sitrep`
- Stop it: `docker compose down`
- After changing `config.yaml`: `docker compose restart sitrep`
- After changing `.env`: `docker compose up -d` (a restart doesn't re-read `.env`)
- After updating sitrep's code: `docker compose up -d --build`

## Deploying on a Synology NAS (Container Manager)

Written for a DS920+ with DSM 7.2. It should work on any Intel or AMD Synology
that runs Container Manager.

1. In **Package Center**, install **Container Manager**.
2. In **File Station**, open the `docker` shared folder (Container Manager creates it)
   and make a folder inside it called `sitrep`.
3. Upload sitrep's files into `docker/sitrep`: everything in this repository, plus
   your `.env`. You don't need `.venv`, `data`, `.git` or `.claude`.
4. Open **Container Manager → Project → Create**.
   - **Project name:** `sitrep`
   - **Path:** `/docker/sitrep`
   - **Source:** *Use existing docker-compose.yml*
5. Click **Next**, skip the web portal settings, click **Next** again, tick
   *Start the project once it is created*, and click **Done**. The first build takes a
   few minutes.
6. Open `http://<your NAS's address>:8080`.

To update later: upload the new files, then in **Container Manager → Project →
sitrep**, choose **Action → Build**.

If the Synology firewall is on, allow port 8080 for your home network and Tailscale.

### Reaching it from your phone with Tailscale

Install the Tailscale package from Package Center, sign in, and open
`http://<your NAS's Tailscale name>:8080` from any device on your tailnet.

sitrep has no login. It's meant for your home network and tailnet only, so:

- **don't** forward port 8080 on your router;
- **don't** turn on Tailscale Funnel for it.

When your internet is down, Tailscale may not be able to connect. At home, use the
NAS's local address instead, for example `http://192.168.1.20:8080`.

## Settings

There are two files.

- **`.env`** is private and is never committed: your router's address, your ISP's
  name, and API tokens. Start from `.env.example`.
- **`config.yaml`** holds everything else: which sources are on, how often they're
  checked, and the alert thresholds. Every setting has a comment. It can pull values
  from `.env` with `${NAME:-default}`.

Restart sitrep after changing either file. If a setting is misspelt or invalid, sitrep
refuses to start and says exactly which line is wrong.

### Your router's address

On Windows, run `ipconfig` and look for **Default Gateway** (for example
`192.168.1.1` or `10.0.0.1`). Put it in `.env` as `ROUTER_ADDRESS`. If your router
doesn't answer on port 80, try `443` for `ROUTER_PORT`.

### Your carrier

Set `HOME_CARRIER` in `.env` to the network your ISP runs on, and the dashboard marks
it as "your network". It must match one of the carrier names in the `carriers` list
at the top of `config.yaml`: Telstra, Optus, TPG group, Vocus or Aussie Broadband.
Many smaller ISPs resell a big carrier's network; Belong, for example, runs on
Telstra's. If the name doesn't match, sitrep refuses to start and says so.

The `carriers` list is shared by every source that looks at carriers (IODA and
Cloudflare Radar), so each carrier's network numbers (ASNs) live in one place.

### Cloudflare token

The Cloudflare Radar source needs a free API token. It can only read Radar's public
data; it can't change anything in your Cloudflare account.

1. Sign up at <https://dash.cloudflare.com/sign-up> if you don't have an account. You
   don't need a website or a payment method.
2. Open <https://dash.cloudflare.com/profile/api-tokens> (**My Profile → API Tokens**)
   and click **Create Token**.
3. At the bottom, next to **Create Custom Token**, click **Get started**.
4. Fill in the form:
   - **Token name:** `sitrep Radar (read only)`
   - **Permissions:** **Account**, **Radar**, **Read**. Nothing else.
   - **Account Resources:** **Include**, your account.
   - **Client IP Address Filtering:** leave empty (your home address can change).
   - **TTL:** optional. An end date a year away is a good habit; sitrep warns on the
     dashboard two weeks before it expires.
5. Click **Continue to summary**, then **Create Token**, and copy the token. Cloudflare
   shows it only once.
6. Paste it into `.env` after `CLOUDFLARE_API_TOKEN=`, on one line with no quotes, and
   restart sitrep.

If the token expires or is deleted, the Radar tile turns grey and says Cloudflare
refused it. Make a new one the same way and replace the line in `.env`.

### Telegram alerts

sitrep can message your phone through Telegram when the overall level changes. It's
optional: without the two settings below it simply doesn't send anything. You make
your own bot, so messages go only to you (or a group you choose).

1. In Telegram, open a chat with **@BotFather** (Telegram's official bot for making
   bots; it has a blue tick) and send `/newbot`.
2. Give it a display name, such as `sitrep`, then a username ending in `bot`, such as
   `macpark_sitrep_bot`. Usernames are public, so leave your own name out of it.
3. BotFather replies with a **token**: a long line that looks like
   `123456789:AAH…`. Treat it like a password. Paste it into `.env` after
   `TELEGRAM_BOT_TOKEN=`, on one line with no quotes.
4. Tap the link BotFather gives you (`t.me/<your bot's username>`) and press **Start**.
   A bot can't message you until you've messaged it first.
5. In the sitrep folder, run:
   ```powershell
   .venv\Scripts\python -m sitrep telegram
   ```
   It checks the token, finds your chat, and prints a line like
   `TELEGRAM_CHAT_ID=123456789`. Copy that line into `.env`.
6. Run the same command again. It sends a test message; check it arrived.
7. Restart sitrep. The bottom of the dashboard now says **Telegram alerts on**.

It's easiest to do this on your Windows computer, then copy the finished `.env` to the
NAS. With Docker, the same check is `docker compose exec sitrep python -m sitrep telegram`.

To send alerts to a household group instead, add the bot to the group, send
`/start@<your bot's username>` in the group, and run step 5 again: it lists the group
too (group IDs start with a minus sign).

Optionally, set `SITREP_DASHBOARD_URL` in `.env` (for example
`http://ds920:8080`, your NAS's Tailscale name) to put an "Open the dashboard" link
in every message.

**What gets sent, and when.** Only changes in the overall level, never single sources:

- **Worse news goes out straight away, with a sound.** For example:
  > 🛑 **sitrep: Critical**
  > AEMO electricity grid: AEMO directed load shedding in Victoria 27 minutes ago; AEMO
  > declared an actual LOR3 in Victoria 34 minutes ago: there isn't enough supply, so
  > some customers are being cut off; …
  > Since 7:06 pm.
  > Open the dashboard (a link, if you set SITREP_DASHBOARD_URL)
- **The all-clear is sent silently, and only once it has stuck.** The overall level
  already needs 15 calm minutes to come down. The message waits a further
  `all_clear_after_minutes` (30) after that. If things flare up again in the meantime,
  nothing is sent at all, because what you were last told is still true. So one
  situation gives you one alert and one all-clear, however much it flickers. That's
  the cooldown.
- **While your internet is down, nothing can be sent.** Once it's back, one message
  says how long it was down (if it was at least 5 minutes).
- A restart doesn't repeat an alert: sitrep remembers what it last told you.

If Telegram can't be reached, sitrep keeps trying (from 30 seconds apart up to 30
minutes apart) and sends the newest alert when it gets through. If the settings are
wrong (a mistyped token, a wrong chat ID, a blocked bot), the dashboard says so at the
top, in plain English.

The settings are under `alerts` in `config.yaml`.

### Changing keywords and thresholds

Everything here is in `config.yaml`, and every setting there has a comment saying
what it does. Restart sitrep after a change. If a value is wrong, sitrep won't start
and says which line.

**Bluesky keyword groups** (`sources.bluesky.groups`). A post matches a group when it
contains at least one phrase from **every** list in the group. Phrases match whole
words and ignore case. End one with `*` to match the start of a word: `evacuat*`
matches "evacuate", "evacuated" and "evacuation". For example, to watch for blackouts
in Sydney, add:

```yaml
      - id: sydney_blackout          # lower-case letters, digits and _ only
        name: Blackout in Sydney     # what the dashboard shows
        all_of:
          - [Sydney, NSW, New South Wales]
          - [blackout*, power outage, power cut, no power]
```

Each extra list makes a group stricter, and stricter groups give fewer false spikes.
A new group spends 7 days learning what's usual before it can raise a spike (the
dashboard shows it as *Learning*), so adding one never sets off false alarms.
Keep a group's `id` the same when you change its phrases, so it keeps its history.

**ACSC warning signs** (`sources.acsc.warning_signs`) are word lists that work the same
way, except that every phrase matches the start of a word. See
[ACSC alerts and advisories](#acsc-alerts-and-advisories).

**Thresholds.** The ones you're most likely to want to change:

| Setting | Default | What it does |
|---|---|---|
| `scoring.elevated_when_categories` | 2 | Categories that must be elevated at once to make the overall status Elevated |
| `scoring.raise_after_checks` | 2 | A higher level must be seen this many checks in a row (30 seconds apart) before it shows |
| `scoring.lower_after_minutes` | 15 | Calm minutes before the overall status comes back down |
| `scoring.learning_days` | 7 | How long Bluesky (and each new keyword group) learns before it can raise alerts |
| `alerts.all_clear_after_minutes` | 30 | How much longer the Telegram all-clear waits once things have calmed |
| `sources.bluesky.spike_probability` | 0.000001 | How unlikely a count must be to be a spike; smaller means fewer, surer spikes |
| `sources.bluesky.spike_min_people` | 5 | The fewest people that can make a spike |
| `sources.radar.traffic_elevated_below` | 0.6 | Traffic below this share of last week, for 30 minutes, is elevated |
| `sources.radar.country_critical_below` | 0.5 | Australian traffic below this share of last week is critical |
| `sources.ioda.carrier_down_methods` | 2 | Methods that must agree before a carrier counts as down |
| `sources.ioda.critical_when_carriers_down` | 2 | Carriers down at once that make IODA critical |
| `sources.aemo.region` | NSW1 | Your grid region (QLD1, VIC1, SA1 or TAS1 for other states) |
| `sources.aemo.demand_drop` | 0.25 | A fall in demand within half an hour that counts as elevated |
| `sources.telcos.state` | NSW | Your state, for the telco cards |
| `sources.probes.offline_after_rounds` | 3 | Rounds (30 seconds each) with nothing outside answering before "Can't reach the internet" |

To switch a source off completely, set its `enabled: false`.

## Sources

### Connectivity probes

Every 30 seconds sitrep checks, all at once:

- your **router**, with a plain connection to its web page port;
- **1.1.1.1, 8.8.8.8 and 9.9.9.9** (Cloudflare, Google and Quad9), with a connection
  to port 443;
- **DNS** through your router (which uses your ISP's DNS) and through Cloudflare;
- **websites on four different networks** (Google, Cloudflare, Apple and ABC on
  Akamai). Only the headers are fetched, never the page.

It uses ordinary connections rather than ping, so Docker needs no special
permissions. The result:

| What it sees | Status |
|---|---|
| Everything answers | Normal |
| The same check fails two rounds running, or your ISP's DNS stops answering | Elevated, naming what's failing |
| Router answers but nothing outside does, three rounds running (~90 s) | **Can't reach the internet** |
| Router doesn't answer either | **Can't reach the internet**, pointing at your home network |

The dashboard also shows a 24-hour strip of your connection.

### IODA (Australian networks)

[IODA](https://ioda.inetintel.cc.gatech.edu/) (Internet Outage Detection and
Analysis, run by Georgia Tech) watches every network on the internet in independent
ways, called methods:

- **routing (BGP):** how many of a network's address blocks the rest of the internet
  can still find a route to;
- **active probing:** how many of its address blocks answer pings;
- **a network telescope** (stray background traffic). It fires often on small
  Australian networks in quiet weeks, so sitrep ignores it (`ignore_methods`).

sitrep watches Australia, NSW and five carriers: Telstra (AS1221), Optus (AS4804,
AS7474), the TPG group (TPG AS7545, Vodafone AS133612, iiNet AS4802), Vocus (AS4826)
and Aussie Broadband (AS4764). Every 5 minutes it asks IODA for its alerts, with one
call for the country, one for NSW and one for all the carriers. Every 15 minutes it
fetches the last 24 hours of active probing for the charts. IODA needs no key.

| What IODA sees | Status |
|---|---|
| A carrier dips on one method | Shown as a **dip** on the dashboard, but not counted |
| Australia or NSW down on one method | Elevated |
| A carrier down on two methods at once | Elevated |
| Australia or NSW down on two methods, or two carriers down | Critical |

Single-method dips are common and rarely outages anyone notices. On 18 October 2026,
active probing for Australia, NSW and every carrier fell to about 20% of usual for an
hour while routing didn't move. That looks like a glitch in IODA's own measurements,
and sitrep says so when it sees that pattern. On its own it raises the network
category to elevated, which isn't enough to change the overall status.

The **Australian networks** section of the dashboard shows each network's active
probing over the last 24 hours, compared with its usual level. Hover or tap a line
to see a time and value.

To watch another state, find its region code with IODA's search, for example
<https://api.ioda.inetintel.cc.gatech.edu/v2/entities/query?entityType=region&search=Victoria>,
and add it under `areas` in `config.yaml`.

### Cloudflare Radar

[Cloudflare Radar](https://radar.cloudflare.com/au) shows what Cloudflare sees of the
internet. Cloudflare carries a large share of the world's web traffic, so it notices
when a country or a network goes quiet. Every 10 minutes sitrep asks Radar for:

- **outages Cloudflare's staff have reported**, keeping those in Australia or on one
  of the carriers;
- **confirmed traffic anomalies**: drops Cloudflare's detector found and a person checked;
- **route hijacks** involving Australia (another network claiming a carrier's
  addresses) and **route leaks** (shown, not counted);
- **traffic** for Australia and each carrier over the last day, compared with the same
  time a week earlier.

That's about 10 calls every 10 minutes, well inside Cloudflare's limit of 1,200 calls
per 5 minutes. Radar's traffic numbers run about an hour and a half behind.

| What Radar sees | Status |
|---|---|
| Cloudflare reports an outage in Australia or on a carrier | Elevated |
| A confirmed traffic anomaly in Australia or on a carrier | Elevated |
| A hijack of a carrier's addresses with confidence 8 or more, in the last 6 hours | Elevated |
| Traffic below 60% of last week for 30 minutes (Australia or a carrier) | Elevated |
| Cloudflare reports a nationwide Australian outage | Critical |
| Australian traffic below 50% of last week for 30 minutes | Critical |

For example, during the Optus outage of 8 November 2023, Optus's traffic fell to
7–13% of the week before for six hours, while Australia as a whole only fell to 90%.
sitrep would have shown Radar as elevated about two hours after it started:
Cloudflare's numbers arrive about 90 minutes late, then the drop has to last 30 minutes.

The **Australian traffic** section of the dashboard shows today against the same time
last week, each carrier's traffic as a share of last week, and Cloudflare's outage
reports from the last 7 days. Radar's data is published under
[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/): free to use for
non-commercial purposes, crediting Cloudflare Radar.

### What the telcos say

Two telcos publish their outage lists in a form programs may read, and sitrep checks
both every 10 minutes:

- **TPG**: a small summary of which services have incidents now, then the incident
  list for each of those services (about 30 kB in all).
- **Aussie Broadband**: its outage feed (about 35 kB).

Telcos post planned maintenance all the time (TPG had 37 maintenance windows on its
NBN service on a quiet afternoon in October 2026), so sitrep sets maintenance apart
from real, unplanned outages, and counts the ones in your state (`sources.telcos.state`).
A telco's card turns amber ("Widespread outage") when it reports an unplanned outage
across several states or the whole country, one it rates major or critical, one the
ACMA's outage rules class as major, one affecting 10,000 customers or more, or five
or more unplanned outages in your state at once.

These cards are **for information only and never change the overall status.** A
telco's own status page tends to lag its outages, and shows only what it chooses to.

The other telcos are links to their own status pages: Telstra, Belong, Optus,
Vodafone, iiNet, NBN Co and Vocus. Their pages only show outages for an address you
type in, their terms rule out automated checks, or (Vocus) they refuse a request that
honestly says it comes from a program, and sitrep won't pretend to be a browser to get
around that. Your provider (`HOME_ISP`) and the network it uses (`HOME_CARRIER`) are
tagged. sitrep never uses Downdetector, directly or through sites that republish it.

Two things sitrep deliberately doesn't keep: the name and email of the TPG staff
member who posted each incident (TPG's data includes them; sitrep drops them as soon
as it reads them), and Aussie Broadband's own wording, which its terms don't allow
republishing. Only facts such as areas, services and customer numbers are kept and shown.

### ACSC alerts and advisories

The [Australian Cyber Security Centre](https://www.cyber.gov.au/) (part of the
Australian Signals Directorate) publishes alerts and advisories as two RSS feeds.
sitrep checks both every 10 minutes. It asks "has it changed?" first, so an
unchanged feed costs almost nothing to check.

| What the ACSC publishes | Status |
|---|---|
| An item from the last 24 hours that mentions a warning sign | Elevated |
| Anything else | Normal |

The warning signs are word lists in `config.yaml` (`sources.acsc.warning_signs`):
**active exploitation**, **critical infrastructure**, **telecommunications** and
**widespread impact**. Add or remove phrases there. A phrase matches from the start
of a word, ignoring case, so `telecommunication` also matches "telecommunications".
The ACSC never makes sitrep critical on its own: it titles many routine items
"Critical vulnerabilities in …", so the word "critical" alone means little.

Between August and October 2026, 6 of the ACSC's 16 items had a warning sign, so
expect this tile to be elevated for a day every week or two. One elevated category
isn't enough to change the overall status.

The dashboard lists the latest items, and new ones also appear on the timeline.

**A note on how sitrep names itself here.** Everywhere else sitrep identifies itself
as `sitrep-bot/<version>`. cyber.gov.au silently ignores requests whose name
contains "bot", so for the ACSC feeds only, sitrep calls itself
`sitrep/<version> (self-hosted situational-awareness dashboard; RSS reader)`. That's
still a truthful description: it reads RSS feeds that exist to be read by programs,
and it doesn't pretend to be a web browser. If you'd rather not, set
`sources.acsc.enabled: false` in `config.yaml`.

ACSC material is © Commonwealth of Australia under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); the dashboard credits it.

### AEMO (electricity grid)

[AEMO](https://www.aemo.com.au/) runs the east-coast electricity grid (the NEM).
Every 5 minutes sitrep reads the summary behind AEMO's public
[NEM data dashboard](https://www.aemo.com.au/energy-systems/electricity/national-electricity-market-nem/data-nem/data-dashboard-nem):
each region's demand, price and market flags, plus AEMO's 10 latest market notices
(about 12 kB per check). At start-up it also fetches the last day of notices from
[NEMWeb](https://nemweb.com.au/Reports/Current/Market_Notice/), AEMO's public file
server, and two days of 5-minute demand for the chart (about 0.9 MB, once). If ever
all 10 notices in a check are new to sitrep, it fills in the gap from NEMWeb again.

| What AEMO says about your region (NSW unless you change it) | Status |
|---|---|
| An actual **LOR3** (not enough supply, so customers are being cut off), a **load shedding** direction, or a **suspended market** | Critical |
| An actual **LOR2** (one big failure away from load shedding), **capped prices** after a run of extreme ones, or a **significant power system event** | Elevated |
| Demand **falls 25% or more** within half an hour, two readings in a row | Elevated |
| An actual LOR3, load shedding or a suspended market **elsewhere in the NEM** | Elevated |
| A **forecast** LOR3, an actual LOR1, or anything else | Normal (forecasts are shown, not counted) |

Prices are shown but not judged, because price spikes are routine on hot evenings.

Why 25% for demand: on ordinary days, passing clouds over rooftop solar move NSW
demand by up to about 13% in half an hour, and Victoria's by up to about 18%. When
transmission towers fell in Victoria on 13 February 2024, Victorian demand fell 27%.
The tests replay that day, and NSW's June 2022 reserve warnings and market
suspension, from AEMO's own records.

To watch another state, set `sources.aemo.region` in `config.yaml` to `QLD1`,
`VIC1`, `SA1` or `TAS1`. The dashboard lists AEMO's notices about your region, and
notices about load shedding anywhere, and they also appear on the timeline.

aemo.com.au itself sits behind a bot check, so sitrep doesn't use it; the dashboard
data and NEMWeb are open to programs. AEMO's
[copyright permissions](https://aemo.com.au/privacy-and-legal-notices/copyright-permissions)
allow its material to be used for any purpose, as long as AEMO is credited, which
the dashboard does.

### Bluesky

sitrep keeps one connection open to Bluesky's
[Jetstream](https://bsky.network/docs/jetstream/) service (version 2), which sends
every new public post on Bluesky as it's made: about 25 a second. **That's about
2 GB of download a day**, all day, so keep it in mind on a capped connection
(`sources.bluesky.enabled: false` turns it off).

Each post is checked against keyword groups in `config.yaml`. A post matches a group
when it contains a phrase from every list in it. The starting groups are:

| Group | A post needs… |
|---|---|
| Sydney or NSW incident | Sydney or NSW, and shooting, explosion, lockdown, evacuate (or similar) |
| Telco down | Telstra, Optus, TPG, NBN, Vodafone, iiNet, Aussie Broadband or Belong, and down, outage, not working (or similar) |
| Internet down in Australia | "internet down", "no internet" (or similar), and Australia, Sydney or NSW |
| Cyber attack in Australia | cyber attack, ransomware or data breach, and Australia, Sydney or NSW |

The text of a linked article's preview counts too, so sharing a news story about an
Optus outage counts as a post about it.

**How it's judged.** sitrep counts the different people (not posts) matching each group
every minute, so one person posting repeatedly counts once. For each group it learns
how many people usually post at each hour of the week, from the last four weeks. A
group **spikes** when the number of people in the last 5 minutes would be less likely
than one in a million at the usual rate (a Poisson test), and at least 5 people posted.
For a group that's usually quiet, that means about 8 people within 5 minutes. A spike
makes Bluesky **elevated**. Bluesky on its own never makes sitrep critical: the social
category only goes critical when two social sources spike together, and for now Bluesky
is the only one.

For its **first 7 days** Bluesky shows *learning* and can't raise alerts, because it
doesn't yet know what's usual for each hour of the week.

**What's kept.** Only the counts, and up to 20 example posts per group (text, link and
time) so you can see what's being said. Examples are deleted after 7 days, as soon as
the author deletes the post, or when their account is deleted, deactivated, suspended
or taken down. Nothing else from the stream is stored.

**Breaks.** If the connection drops, sitrep picks up from where it left off (Jetstream
keeps about a day), so no posts are missed. After a break of more than two hours it
starts from now instead of downloading everything it missed.

### Wikipedia (not included)

The plan included Wikipedia's live feed of edits: new English articles about
incidents in Sydney or Australia, and sudden bursts of edits to articles such as
Telstra or Optus. Wikimedia refuses programs that don't include contact details (an
email address, a website or a Wikipedia username) with every request, and sitrep
sends none by default, so it's left out for now. Without it, Bluesky is the only social
source, and the social category can reach Elevated but not Critical.

## Simulate mode: a practice emergency

To see the dashboard and your Telegram alerts work without anything real happening, run:

```powershell
.venv\Scripts\python -m sitrep simulate
```

Then open <http://localhost:8081>. It takes about 7 minutes:

| Step | What happens | Telegram |
|---|---|---|
| 1 | Everything is normal | nothing |
| 2 | Bluesky spikes: lots of "Telstra down" posts | nothing: one category isn't enough |
| 3 | IODA sees Telstra dropping, so two categories agree | **Elevated** alert, with a sound |
| 4 | IODA says Australia is down | **Critical** alert, with a sound |
| 5 | Everything calms down | **All clear**, silently, about 2 minutes later |
| 6 | Your internet drops out for about a minute | nothing (it can't send while offline) |
| 7 | Your internet is back | a silent message saying how long it was down |
| 8 | Finished | |

Waits are shortened so the whole story fits in 7 minutes: in real life the overall level
needs 15 calm minutes to come down, and the all-clear waits 30 more.

**It never touches your real sitrep.** It runs as a separate copy on the next port up
(8081), with its own database (`data/simulate.db`, wiped each time), so your history,
Bluesky's learning and your real alerts are left alone. You can run it while your real
sitrep keeps running. Every source is replaced by a stand-in that follows the script,
so nothing is fetched from the internet.

**It's clearly marked.** A dashed banner across the top of the page says
**SIMULATION: nothing on this page is real**, the browser tab's title starts with
"SIMULATION", and every source's reason ends in "(simulated)". Telegram messages are
real messages to your chat, and each starts with **🧪 SIMULATION: not a real alert**.

Stop it with **Ctrl+C**. To use a different port: `python -m sitrep simulate --port 8090`.
With Docker:
`docker compose run --rm -p 8081:8081 sitrep python -m sitrep simulate`.

## Troubleshooting

**sitrep won't start.** It prints what's wrong and where, for example a misspelt
setting in `config.yaml` or a `HOME_CARRIER` that isn't one of the carriers.

**Port 8080 is already in use.** Change `server.port` in `config.yaml` (Windows), or
the left-hand number under `ports` in `docker-compose.yml` (Docker).

**It says "Can't reach the internet" but your internet works,** or "Your connection"
stays elevated. Open the "Your connection" tile and click **Show each check** to see
which checks fail. If only the router fails, `ROUTER_ADDRESS` (or `ROUTER_PORT`) is
probably wrong. If some outside checks always fail, something on your network may be
blocking them (an ad blocker such as Pi-hole, say); take those checks out of
`sources.probes` in `config.yaml`.

**A tile says "Stale".** That source hasn't updated for a while, usually because the
service itself is having trouble. The tile shows the last error and the last-known
state. sitrep keeps retrying, waiting longer each time, up to 30 minutes apart.

**The Cloudflare Radar tile is grey.** The token is missing, has expired, or doesn't
have Radar read access. See [Cloudflare token](#cloudflare-token).

**Bluesky says "Learning".** That's normal for its first 7 days, and for 7 days after
you add a keyword group.

**Telegram alerts aren't arriving.** If Telegram rejects the settings, the top of the
dashboard says why. Run `python -m sitrep telegram` to check the token and send a
test message. Remember that Telegram only hears about changes in the overall status,
not about single sources.

**The page says "Can't reach the sitrep server".** sitrep itself has stopped, or your
phone has lost touch with your tailnet. The page keeps showing what it last heard and
tries again every 15 seconds.

## Adding a source or an alert channel

sitrep is built so that new sources and new ways of sending alerts slot in without
changing the rest.

**Sources** live in `sitrep/adapters/`, one file each. A source is a class with an
`id`, a `name`, a `category` and a settings class, and it reports an `Assessment`: a
level and a one-line reason, plus optional details for the dashboard. There are three
shapes, all in `sitrep/adapters/base.py`:

- `PollingAdapter` fetches something on a timer: write `poll()`. Post counts from the
  X API would be one of these.
- `StreamingAdapter` keeps a connection open: write `stream()`. Bluesky is one.
- `PushAdapter` receives data that something else sends to `POST /api/ingest/<id>`:
  write `ingest(payload)`. A Raspberry Pi with a microphone on the balcony, posting
  siren counts, would be one of these.

The base classes handle the timers, timeouts, retries with backoff and staleness, and
catch every error, so a broken source can't crash sitrep. Add the class to `REGISTRY`
in `sitrep/adapters/__init__.py`, give it a block under `sources` in `config.yaml`,
and save some sample responses under `tests/fixtures/` for its tests: real ones if
the source's licence lets you share them, made-up ones in its format if not. A second social
source (X, say) would also let the social category reach Critical when it and Bluesky
spike together.

**Alert channels** live in `sitrep/alerts.py`. Subclass `Notifier`, write `check()`
(are the settings right?) and `send(alert)`, and add it in `Runtime._build_alerts`
in `sitrep/runtime.py`. The rules for when to send are shared, so a new channel (for
example, pushing the overall status over a Meshtastic mesh) gets the same cooldown as
Telegram. Telegram's version, in `sitrep/telegram.py`, is a worked example.

Where things are:

```text
sitrep/
  __main__.py     starts sitrep (python -m sitrep), plus the telegram and simulate commands
  config.py       reads config.yaml and .env
  runtime.py      runs the sources, the scorer, housekeeping and alerts
  scoring.py      turns source levels into category levels and the overall status
  alerts.py       decides when to send an alert; the Notifier base class
  telegram.py     sends alerts through Telegram
  simulate.py     simulate mode
  db.py           the SQLite database
  api.py          the JSON API the page reads
  adapters/       one file per source
  static/         the dashboard page: plain HTML, CSS and JavaScript, nothing from the internet
tests/            the tests, with sample responses in tests/fixtures/
```

## Running the tests

```powershell
.venv\Scripts\python -m pytest
```

## Data and privacy

- Everything is stored in one file, `data/sitrep.db`. Minute-by-minute counts are
  kept for 7 days and hourly totals and the timeline for 90 days. Older data is
  deleted automatically.
- sitrep identifies itself honestly to every service it polls (`sitrep-bot/<version>`,
  or `sitrep/<version> (…; RSS reader)` for the ACSC feeds, as explained above), plus
  the contact you set in `SITREP_CONTACT`.
- From Bluesky, only counts and a small sample of matching posts are kept, and samples
  are deleted when their authors delete them (see "Bluesky" above).
- Simulate mode uses its own file, `data/simulate.db`, which is wiped each time it starts.
- sitrep downloads about 2 GB a day, nearly all of it Bluesky's stream. Everything
  else together is a small fraction of that.
- The page loads nothing from the internet: no CDNs, no web fonts, no trackers.
- Telegram alerts, if you set them up, go through Telegram's servers. They contain
  the overall level and its reason, nothing about you. The bot token stays in `.env`
  and never appears in sitrep's logs or on the dashboard.

## Licence

sitrep's code is under the [MIT licence](LICENSE): use it, change it and share it,
keeping the copyright notice.

What the dashboard shows belongs to the services it comes from, under their own terms
(see [Sources](#sources)). The sample responses the tests use, in `tests/fixtures/`:

- `acsc/`: © Commonwealth of Australia, under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- `aemo/`: © AEMO, used under AEMO's
  [copyright permissions](https://aemo.com.au/privacy-and-legal-notices/copyright-permissions).
- `radar/`: Cloudflare Radar's data, under [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)
  (non-commercial use, crediting Cloudflare Radar).
- `ioda/` and `telcos/`: made up, in each source's format. IODA marks its data "All
  Rights Reserved" and the telcos don't say their feeds can be shared, so none of
  their real data is included.
- `probes/`: recorded by sitrep itself.
