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
| Regional reachability | Connectivity | How fast sitrep reaches Sydney, Singapore, Tokyo, US West, US East and Europe: spots overseas routes failing while Australia is fine |
| [IODA](https://ioda.inetintel.cc.gatech.edu/) (Georgia Tech) | Network measurement | Australia, NSW or a big carrier dropping off the internet |
| [Cloudflare Radar](https://radar.cloudflare.com/au) | Network measurement | Outage reports, traffic drops and route hijacks for Australia and the big carriers |
| AWS, Azure, Google Cloud and Cloudflare status | Network measurement | Incidents in their Sydney regions: many sites broken while your line and the telcos are fine |
| [ASD's ACSC](https://www.cyber.gov.au/) | Official | Cyber security alerts and advisories |
| [ransomware.live](https://www.ransomware.live/) | Cyber intel | Australian organisations that ransomware gangs claim to have hit |
| US State Department and [UK FCDO](https://www.gov.uk/foreign-travel-advice/australia) travel advice | Official | When a foreign government changes what it tells its citizens about travelling to Australia |
| [NSW RFS](https://www.rfs.nsw.gov.au/fire-information/fires-near-me) | Official | Fires and other incidents across NSW, by alert level and distance from home |
| [Bureau of Meteorology](http://www.bom.gov.au/nsw/warnings/) | Official | Severe weather, thunderstorm, fire weather and flood warnings that cover Sydney |
| [Live Traffic NSW](https://www.livetraffic.com/) | Official | Road hazards and major events, for the map (never raises the level on its own) |
| [Sydney Trains and Metro alerts](https://transportnsw.info/alerts) | Official | Unplanned suspensions across the network or several lines (not planned trackwork) |
| [FuelCheck NSW](https://www.fuelcheck.nsw.gov.au/app) | Stress | Sudden jumps in unleaded and diesel prices, near home and across NSW |
| [AEMO](https://www.aemo.com.au/) | Stress | The NSW grid: supply warnings, load shedding, sudden drops in demand |
| ABC News, the Sydney Morning Herald, Guardian Australia | News | Headlines and the Sydney suburbs they name, for the map, story clusters and emerging terms |
| [Bluesky](https://bsky.app/) | Social | Sudden spikes in posts about trouble in Sydney and Australia |
| [adsb.lol](https://adsb.lol/), checked with [adsb.fi](https://adsb.fi/) | Sensors | Aircraft over Sydney: far fewer than usual (an airspace closure), and helicopters circling over one spot |
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
[The map](#the-map) ·
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
Sources are grouped into categories: **connectivity** (your own line), **network
measurement**, **official**, **cyber intel**, **news**, **social**, **stress** (the
grid, and later fuel) and **sensors**. Some categories have no sources yet; they fill
in as sources are added.

Every raised source also says what it's about (its *scope*): Australia-wide trouble with
the **internet**, **cyber**, **power**, **fuel** or **security**; **Sydney** as a whole;
a 2 km square of the map; or one story. The overall status goes up when:

- **one category is critical** (overall: *Critical*), or
- **two categories are elevated about what's plausibly the same event** (overall:
  *Elevated*): the same story, the same or neighbouring squares of the map, both about
  Sydney as a whole, or both national with related themes. Related means the same theme,
  or internet trouble together with cyber or power trouble (big cyber attacks and power
  cuts both take networks down). So IODA seeing Optus drop and a Bluesky spike of
  "Optus down" posts back each other up, but an NSW grid warning and an unrelated ACSC
  advisory don't. You can change which themes count as related in `config.yaml`.

One elevated category on its own, or several that aren't about the same thing, leaves
the overall status at *Normal* (the reason says what's elevated).

**Stories.** sitrep also groups items from different sources that are about the same
event into a story (see [Story clusters](#story-clusters)). A story with two kinds of
source (official, news, social) is elevated in each category it spans, so it's enough to
make the overall status *Elevated* by itself. It only becomes critical when all three kinds
agree, it's in Greater Sydney, and it includes an emergency-level item (an RFS Emergency
Warning, a BoM severe warning) or matches one of your keyword groups. A big motorway crash
with news coverage and posts about it is elevated, not critical. The telcos' own status
pages are shown for information only and never count.

**Can't reach the internet** is its own state, shown when your router answers but
nothing outside does.

**Every source is in one of four states**, and only *ok* sources count:

- **ok**: its latest data is current, so its level counts;
- **learning**: it's still working out what "normal" looks like (Bluesky's first 7 days),
  or waiting for its first update;
- **fetch failing**: its last two tries failed, or it hasn't answered for a while;
- **frozen**: it answers, but the newest timestamp inside its data is too old. A
  source can keep answering while its data has stopped changing, so "the fetch worked"
  isn't proof that the data is current. Each source's limit is
  `content_age_budget_minutes` in `config.yaml` (AEMO 20 minutes, Bluesky 5, IODA 90,
  Radar 180). Quiet feeds such as ACSC's can go days without a new item, so they have
  no limit and only show a problem when fetching fails.

Fetch failing and frozen sources show their last-known state, greyed out, never as normal.

**Backlog doesn't alert.** sitrep matters most right after an outage, which is exactly
when it's catching up on hours of feed items. So every item (an ACSC advisory, an AEMO
notice, a Bluesky spike) remembers when sitrep first saw it. One that was already more
than 15 minutes old by then is shown on the dashboard and the timeline, but can't set
off an alert. Slower sources get their poll interval plus their usual publishing delay
instead (ACSC: 25 minutes), or they could never alert at all. A new version of an item
(an updated warning, say) counts as new. So after an outage the dashboard can show
*Elevated* or *Critical* while your phone only gets one "while you were offline" summary.

To stop flickering, a higher level has to be seen twice in a row (about a minute)
before it shows, and it only comes back down after 15 calm minutes.

## What's on the dashboard

One page, in dark mode, that works on a phone. From the top:

- **Overall status:** the level, a plain-English reason, how long it's been that way,
  and when it will calm down if nothing new happens.
- **What sitrep can't see right now:** shown only when a source is fetch failing,
  frozen or learning, with one plain line each, such as "No Australian networks (IODA)
  data for 40 minutes" or "Bluesky posts frozen since 2:02 pm". Hidden when everything
  is ok.
- **Stories from more than one kind of source:** shown only when there are some, with
  triangulated ones (official, news and social) first, each listing what's in it.
- **Categories:** the four categories and why each is at its level.
- **Sources:** one tile per source with its status, reason, when it last updated, how
  old the newest data inside its last answer was, and a link to the source. A source
  that's fetch failing or frozen says so and shows its last-known state. The "Your
  connection" tile can show each individual check.
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
- **Map** (top right): a map of Greater Sydney with home and every incident sources
  have placed on it. See [The map](#the-map).

Hover over (or tap) a chart to see the exact time and value. If the page loses touch
with sitrep itself, it says so and keeps showing what it last heard.

## The map

Click **Map** at the top of the dashboard for a map of Greater Sydney showing home and
every incident sources have placed on it, coloured by how much each matters here: red
for critical, amber for elevated, orange for a Watch and Act or Emergency Warning
further away or a major road hazard, grey for the rest; road hazards have a blue
outline. Click a point for its details. Today that's the RFS incidents and Live
Traffic's road hazards; train disruptions and news stories join as those sources are
added.

**It works with no internet at all.** The mapping library, its fonts and icons are part
of sitrep, and the streets come from a basemap file sitrep keeps on your computer or NAS.
Make that file once (about 40 MB, a minute or so):

```powershell
.venv\Scripts\python -m sitrep map
```

With Docker (and on the NAS), run it inside the container instead:
`docker compose exec sitrep python -m sitrep map`. The file goes in `data/map/`, so it
survives updates. Until it exists, the map shows the points on a plain background and
says how to add streets. Run the command again whenever you'd like newer streets; every
few months is plenty.

The streets come from [Protomaps](https://protomaps.com/)' daily build of
[OpenStreetMap](https://www.openstreetmap.org/). The command reads only the Greater
Sydney part of that worldwide file (from the Blue Mountains to the Central Coast, up to
street level). The area and detail are under `map` in `config.yaml`. Map data
© OpenStreetMap contributors, under the [ODbL](https://opendatacommons.org/licenses/odbl/);
the map credits it in its corner.

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

### Where home is

sitrep measures distances from home: how far an RFS fire is, for example. By default,
home is the middle of Macquarie Park (the point the Australian Bureau of Statistics
gives for the suburb). To use your own suburb or exact spot, add it to `.env`:

```text
HOME_PLACE=Epping
HOME_LAT=-33.7727
HOME_LON=151.0824
```

To find a spot's numbers, right-click it in Google Maps or OpenStreetMap: the first
number is the latitude (negative in Australia), the second the longitude. They go in
`.env` rather than `config.yaml` so your exact location never ends up on GitHub.

### Transport for NSW token

Live Traffic hazards and the Sydney Trains and Metro alerts come from Transport for
NSW's Open Data Hub, which needs a free token:

1. Go to <https://opendata.transport.nsw.gov.au> and click **Register**. Fill in the
   form, then click the link in the email they send.
2. Log in, click your **profile icon** (top right), then **API Tokens**.
3. Type a name such as `sitrep` and click **Create API Token**.
4. Copy the token straight away: it's shown only once. (If you lose it, delete it and
   make another.)
5. Paste it into `.env` after `TFNSW_API_KEY=`, on one line with no quotes, and
   restart sitrep.

One token covers both feeds. The data is Transport for NSW's, under CC BY 4.0.

### FuelCheck key

Fuel prices come from FuelCheck on API.NSW, a different site from the Transport for NSW
Open Data Hub, with its own free key **and** secret:

1. Go to <https://api.nsw.gov.au>, click **Register**, fill in your details and click
   **Create a new account**. The confirmation email can take up to a day; click the
   activation link in it.
2. Log in and open the [Fuel API page](https://api.nsw.gov.au/Product/Index/22). Click
   **Subscribe** next to the Free plan (2,500 calls a month). If it asks you to make an
   app, go to **My Account**, click **Add a new App**, pick the **Fuel API** and click
   **Create APP**.
3. The app shows as **Pending** until someone at API.NSW approves it; you get an email
   when they have.
4. Your **Key** and **Secret** are then under **My Account**. Paste them into `.env`
   after `FUELCHECK_API_KEY=` and `FUELCHECK_API_SECRET=`, each on one line with no
   quotes, and restart sitrep.

API.NSW locks an account after 3 bad sign-ins in a row, so if it refuses the key and
secret, sitrep stops trying (the tile says so) until you fix them and restart. Don't
paste them into the "Try this API" box or any other website.

### ransomware.live key

The Australian ransomware victims come from ransomware.live's API, which needs a free key:

1. Go to <https://my.ransomware.live>, type your email address and click **Send Login
   Link**. There's no password.
2. Click the link in the email they send. The portal shows your API key.
3. Paste it into `.env` after `RANSOMWARE_LIVE_API_KEY=`, on one line with no quotes,
   and restart sitrep.

The key is for non-commercial use. ransomware.live asks to be credited ("Source:
Ransomware.live", with a link), which the dashboard does, and doesn't allow its data to
be passed on as a feed or API, so keep the dashboard private. Without a key the tile
shows "fetch failing" and says the key is missing.

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

- **Worse news goes out straight away.** Critical makes a sound; elevated arrives
  silently, because with this many categories it will come more often (set
  `elevated_makes_sound: true` under `alerts` to change that). For example:
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
- **No alerts for 2 minutes after sitrep starts or your internet comes back**, while
  the sources catch up.
- **While your internet is down, nothing can be sent.** Once it's back (and the 2 minutes
  are up), one message says how long it was down (if it was at least 5 minutes), how many
  items each source collected meanwhile, the top 5, and how things stand now. It's silent
  unless the dashboard is critical. You get the same message if sitrep itself wasn't
  running for 5 minutes or more (a power cut, say).
- **The same event can't alert twice within 30 minutes**, though it can alert again if
  it gets worse (elevated, then critical). While you've been told it's critical, a new
  critical event gets its own message ("Critical, another event"), so a second emergency
  isn't hidden behind the first.
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
        scope: national:power          # what a spike is about: sydney, or national:<theme>
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

### Regional reachability

Every minute sitrep opens a connection to two hosts in each of six regions (Sydney,
Singapore, Tokyo, US West, US East and Europe) and times the TCP connection and the TLS
handshake. Nothing else is sent: no page is fetched, so each check is a few kilobytes.
The two hosts in a region belong to different providers (AWS's regional API addresses,
plus Vultr's, Hetzner's or Oracle's published test hosts), so one provider's trouble
doesn't look like a broken route.

The hosts really sit in their regions. Addresses like 1.1.1.1 or Google's are
*anycast*: they answer from a Sydney data centre whatever is happening to the routes
overseas, so they'd hide exactly what this source looks for. The hosts were chosen by
measuring from a Macquarie Park NBN line: each one's time fits its distance (Sydney
5 ms, Tokyo 130, US West 140 to 150, Singapore 180, US East 205, Europe 300 to 320).
Singapore is slower than its distance suggests because traffic from that line goes via
Tokyo.

| What it sees | Status |
|---|---|
| Two or more overseas regions **slow, losing packets or unreachable** while Sydney is fine ("overseas route degraded, Australia fine", which is how a Pacific cable fault shows up first) | Elevated |
| One overseas region in trouble | Shown, but normal |
| Sydney in trouble too | Normal: the problem is closer to home, and "Your connection" judges that |

- A host is **slow** when its handshakes over the last 5 minutes are at least 30 ms and
  25% above its usual for that hour of the week. sitrep learns the usual from the last
  4 weeks, so for its first 7 days it shows **learning** and can only see the next two.
- A host is **losing packets** when 3 of its last 10 tries failed or took over a second
  (a sign of a lost and resent first packet), and **down** when its last 3 failed.
- A region only counts as in trouble when **both** its hosts agree.

It's never critical on its own: it's one home line's view of the world. It backs up
Cloudflare Radar, IODA and cloud status, which are about Australia's internet too. The
hosts and thresholds are under `sources.reach` in `config.yaml`.

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

### Cloud and CDN status

Every 5 minutes sitrep checks four providers' public status feeds for an incident in
Sydney. A lot of Australian websites and apps run on these, so this covers the case
where many sites are broken while your own line and the telcos are fine. No keys needed.

| Provider | What's checked | Counts when |
|---|---|---|
| AWS | The Sydney (ap-southeast-2) feeds for EC2, S3, Lambda and DynamoDB | A service's newest update doesn't say it's operating normally (for up to 12 hours) |
| Azure | Azure's status feed | An item names Australia East |
| Google Cloud | Its incident list | An incident without an end currently affects australia-southeast1 |
| Cloudflare | Its status API: unresolved incidents and maintenance in progress | An unresolved incident includes the Sydney data centre (SYD) or names Sydney |

Any of those makes this source **elevated**, and the reason names the incident. It's
never critical by itself. It's about the internet in Australia, so it backs up other
signs of internet trouble (IODA, Radar, posts about sites being down): a cloud incident
alone leaves the overall status Normal.

Things to know:

- **AWS:** AWS's website terms rule out "robots", so sitrep reads only the per-service
  RSS feeds, which are there to be polled, never the status page. The services are under
  `sources.cloud.aws_services` (`rds` and `apigateway` also have Sydney feeds).
- **Azure:** its public feed lists only widespread incidents. A problem in Australia East
  alone shows only in Azure Service Health, which needs an Azure login.
- **Cloudflare:** its status page often shows Sydney as "degraded" with no incident
  behind it, so per-location statuses are ignored; maintenance in Sydney is shown but
  doesn't count. Only incidents touching Sydney count: counting every "major" or
  "critical" Cloudflare incident would have flagged 7 in the two months before this was
  built, nearly all overseas or for niche products. Cloudflare asks programs polling it
  to give a contact in their name; set `SITREP_CONTACT` in `.env` if you'd like to.
- Every check asks "has it changed?" first, so most cost a few hundred bytes.

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

### Ransomware victims (ransomware.live)

[ransomware.live](https://www.ransomware.live/) watches ransomware gangs' leak sites and
lists each organisation a gang claims to have hit. Every hour sitrep asks its API for
Australian victims (it needs a free key: see [ransomware.live key](#ransomwarelive-key)).
Its data is cached for 30 minutes and its fair-use rules count faster polling as a
breach, so sitrep won't accept an interval under 30 minutes.

| What happens | Status |
|---|---|
| A new Australian victim (about one every two days) | Info: on the timeline |
| A new victim in healthcare, energy and utilities (which includes water), government, finance, transport or telecoms | Elevated, for 24 hours |
| At least 5 Australian victims in 24 hours, far more than usual for the last 90 days | Elevated |

It's never critical by itself: a listing is a gang's claim, often weeks after the attack,
not proof that something is down now. It's about cyber trouble in Australia, so it backs
up an ACSC alert or a carrier dropping off the internet. ransomware.live has no telecoms
sector, so telcos are spotted by name (Telstra, Optus, "broadband" and so on: the list is
`sources.ransomware.telco_words` in `config.yaml`, next to the sectors).

Run over the 59 Australian victims from July to October 2026, this marks 10 as elevated
(about 2.5 a month), and no day reached a burst.

sitrep keeps each victim's name, the gang, the sector, when ransomware.live first saw it
and a link to its ransomware.live page, for 7 days. Never the leak-site links, screenshots
or descriptions.

### Foreign travel advice

Every hour sitrep reads what two foreign governments tell their citizens about travelling
to Australia. They rarely change it, so a change means something big has happened or is
expected.

- **US State Department:** its travel advisory for Australia, Level 1 ("exercise normal
  precautions") to Level 4 ("do not travel"), from `cadataapi.state.gov` (Australia's
  code there is AS). No key needed.
- **UK FCDO:** its [travel advice for Australia](https://www.gov.uk/foreign-travel-advice/australia),
  from GOV.UK's content API: whether it advises against travel to part or all of
  Australia, and every update with a one-line note. No key needed. Contains public sector
  information licensed under the
  [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
- **US Embassy in Canberra:** its security alerts are a link on the tile only. Its website
  refuses automated requests, and it has no feed.

| What happens | Status |
|---|---|
| Either government raises its level (the US from Level 1 to 2, or the FCDO starting to advise against travel to part of Australia) | Critical, with a sound, for 24 hours |
| A new FCDO update whose note mentions terrorism, security, safety, unrest, a shooting, an attack and so on | Elevated, for 24 hours |
| A lowered level, a reissue at the same level, any other FCDO update | Info: on the timeline only |

The FCDO updates its page often for small reasons (insurance wording, the bushfire
season), and its notes name the page they changed ("'Safety and security' page"), which
doesn't count. Run over the FCDO's whole history for Australia since 2013, this rule
marks 11 of 115 updates: the Bondi Beach attack in December 2025, the terrorism threat
updates of 2024, and the Bourke Street attack in Melbourne in 2018. An update that names
a Sydney suburb is about that suburb, so it can join a story about it as an official
source. The words are under `sources.advice.warning_words` in `config.yaml`.

A change sitrep only notices after a restart or an outage shows on the dashboard, but
goes into the "while you were offline" summary rather than paging you.

### NSW RFS incidents

Every 5 minutes sitrep reads the [NSW Rural Fire Service](https://www.rfs.nsw.gov.au/)'s
public feed of current incidents: the same list as Fires Near Me, as GeoJSON, about
60 kB. It asks "has it changed?" first, so most checks are a few hundred bytes. No key
is needed.

Each incident has an alert level from the Australian Warning System, set by the RFS:
**Advice**, **Watch and Act** or **Emergency Warning** (plus "Not applicable" and
"Planned burn" for the rest). sitrep works out how far each one is from home.

| What the RFS says | Status |
|---|---|
| An **Emergency Warning** within 15 km of home | Critical |
| An **Emergency Warning** within 50 km, or a **Watch and Act** within 15 km | Elevated |
| Anything else | Normal (shown, and counted across NSW) |

The distances are `critical_km`, `emergency_elevated_km` and `watch_and_act_km` under
`sources.rfs` in `config.yaml`. Open the tile's **Show incidents near home** to see
everything within 50 km, worst first. Watch and Act and Emergency Warnings near home
also go on the timeline.

An elevated RFS on its own leaves the overall status at Normal, like any single
category: it needs something else about the same area (a news story, a road closure)
to back it up. An Emergency Warning within 15 km is critical on its own, so it always
sends a message, with a sound.

The feed's times are in UTC without saying so; sitrep checked that against the Sydney
time each incident's description gives. A warning updated after an outage counts as
new; an old copy can't set off a message. If nothing anywhere in NSW has been updated
for 6 hours, the feed has probably stopped moving on, and the tile shows **frozen**.

The data is © State of New South Wales (NSW Rural Fire Service), under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), which the dashboard credits.
NSW SES, NSW Police and Fire and Rescue NSW don't publish a feed sitrep could use (SES
gives one only on request), so they aren't included. Some Fire and Rescue NSW incidents
do appear in the RFS feed.

### BoM warnings

The Bureau of Meteorology's website doesn't allow programs ("does not support web
scraping"), so sitrep uses the anonymous FTP service BoM provides for them. Every 10
minutes (every 5 while a thunderstorm warning covers Sydney) it lists BoM's NSW files and
downloads only warnings that are new or changed, a few kilobytes each. Forecasts in the
same folder are skipped after the first look.

| What BoM says, for Sydney | Status |
|---|---|
| A warning about **very dangerous thunderstorms** | Critical |
| A **Severe Weather**, **Severe Thunderstorm**, **Fire Weather** or **Major Flood** warning | Elevated |
| Minor and moderate floods, marine and surf warnings, frost | Normal (listed) |
| Any warning elsewhere in NSW | Normal (listed) |

"For Sydney" means the warning lists the Sydney Metropolitan district (`NSW_PW005`) or
an area named in `sources.bom.area_names` in `config.yaml` (Greater Sydney, and the
rivers BoM issues flood warnings for, such as the Hawkesbury and Nepean). Severe weather,
thunderstorm and major flood warnings are emergency-level items, which can make a story
or a convergence alert critical later; a fire weather warning is a forecast of
conditions, so it isn't. BoM doesn't issue Emergency Warnings; the NSW SES does, and
has no public feed.

**BoM's terms allow personal use only.** Keep the dashboard private (on your tailnet),
and don't publish or pass on what it shows. For the same reason the tests use made-up
files in BoM's format, never real ones.

### Live Traffic NSW

Every 2 minutes sitrep reads [Live Traffic NSW](https://www.livetraffic.com/)'s open road
incidents, fires and floods, and every 15 minutes its major events, from Transport for
NSW's Open Data Hub (about 2,300 of the 60,000 requests a day its free plan allows). It
needs a free token: see [Transport for NSW token](#transport-for-nsw-token).

**It never raises the level on its own.** Crashes and breakdowns happen many times a
day; a big motorway crash shouldn't send you anything. Its hazards go on the
[map](#the-map) (blue outlines) and the tile's **Show hazards near home** list, major
ones go on the timeline, and every hazard is kept for the signals that look across
sources (story clusters and convergence), so a crash, a news story and a police
helicopter in the same place can be recognised as one event. Only hazards Transport
for NSW marks "major" count as an official source in a story.

"Open" includes planned works that can run for years (the Harbour Bridge maintenance,
for instance), so the map and list show unplanned hazards, major ones and major events
on now, and leave long-term roadworks out. If no hazard anywhere in NSW has been updated
for 3 hours, the tile shows **frozen**.

The data is © Transport for NSW, under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

### Sydney Trains and Metro

Every 2 minutes sitrep reads the Sydney Trains and Sydney Metro service alerts from
Transport for NSW's Open Data Hub, with the same token as Live Traffic (about 17 kB each,
compressed).

| What the alerts say | Status |
|---|---|
| An **unplanned suspension** (no trains running) in force now, across the **whole network**, **3 or more lines**, or the **whole Metro line** | Elevated |
| A suspension on one or two lines, or delays | Normal (shown on the tile) |
| Planned trackwork, and station updates (lifts, ramps, stairs) | Normal (counted, not judged) |

Most alerts are planned trackwork published days ahead ("buses replace trains between
Hornsby and Strathfield on Saturday"). Transport for NSW doesn't mark them as planned,
but gives their cause as maintenance or construction, which is how sitrep tells them
apart. A network-wide suspension is about Sydney as a whole, so it backs up other
Sydney-wide signals (a spike in Bluesky posts about an incident in Sydney, say); see the
`trains-down` replay. The number of lines is `lines_for_elevated` under `sources.transit`
in `config.yaml`.

Station alerts also list the bus routes that stop there; only train and metro lines are
counted. The data is © Transport for NSW, under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

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

AEMO is in the **stress** category, with a national **power** scope. Its 5-minute
figures carry their own times, so if they stop moving on for 20 minutes the source shows
as **frozen** rather than quietly repeating old figures. A notice sitrep first sees long
after AEMO issued it (after an outage, say) still shows, but can't set off an alert.

aemo.com.au itself sits behind a bot check, so sitrep doesn't use it; the dashboard
data and NEMWeb are open to programs. AEMO's
[copyright permissions](https://aemo.com.au/privacy-and-legal-notices/copyright-permissions)
allow its material to be used for any purpose, as long as AEMO is credited, which
the dashboard does.

### Fuel prices (FuelCheck)

Every 2 hours sitrep downloads every NSW station's current prices from
[FuelCheck](https://www.fuelcheck.nsw.gov.au/app) (one call, about 2 MB) and works out
the median price of unleaded 91 and diesel within 10 km of home and across NSW. Each is
saved once an hour, so it knows the last 30 days. It needs a free key and secret: see
[FuelCheck key](#fuelcheck-key). That's about 430 of the free plan's 2,500 calls a month.

A fuel is **elevated** when its median jumps more than 10 c/L above the **highest** it's
been in the last 30 days (not counting the last day). Sydney's prices go round a cycle
every few weeks, rising 20 to 40 c/L in a day or two and then easing, so comparing with
the average would flag every cycle; a jump only counts when it goes past the cycle's
usual peak. Fuel is never critical, and nothing else is about fuel, so it shows on its
tile but doesn't change the overall status. It's **learning** for its first 7 days.

FuelCheck has no field for a station running out of fuel. A station that stops listing a
fuel might be out of it, or might not, so for now the tile only shows how many stations
near home list each fuel (and the cheapest one). That count is saved too, so a rule can
be added later once there's real data to judge it by.

The fuels, the distance and the jump size are under `sources.fuel` in `config.yaml`
(FuelCheck's other codes include E10, P95, P98 and LPG). Data: NSW Government FuelCheck,
CC BY-SA.

### News headlines

sitrep reads the RSS feeds of ABC News (Just In, and NSW), the Sydney Morning Herald
(NSW) and Guardian Australia, every 10 or 15 minutes, asking "has it changed?" first.
From each item it keeps only the headline, the link, the outlet, the time and any NSW
place the headline names, for 7 days. **It never fetches the articles.** The tile's
**Show latest headlines** links each one to the outlet, and headlines that name a Sydney
suburb go on the [map](#the-map) (white outline) for 6 hours.

News never raises the level on its own. Headlines are what story clusters are made from
(the next part of step 4): a group of items from news, official and social sources about
one event.

**Place names.** sitrep finds suburbs with a list of every NSW suburb and locality and
the point the ABS gives each (`sitrep/data/places_nsw.csv`, 4,554 places, 932 of them in
Greater Sydney). Matching is deliberately strict, because a wrong place is worse than
none:

- names only match with their capitals, as whole words ("Ryde", not "ruse");
- names that are also somewhere else (Liverpool, Newcastle, Richmond, Epping, Penrith)
  only count when the story is clearly about Sydney or NSW;
- outside Greater Sydney, one-word names (Bell, Paterson) need the same clue, because
  they're often surnames;
- a name that's part of another one doesn't count: the Sunshine Coast, the Penrith
  Panthers, Parramatta Road;
- "Sydney" on its own means the whole city, not the CBD suburb.

**Terms.** The outlets allow personal, non-commercial use of their feeds, with a link
back, which the dashboard gives. The Guardian and Nine (SMH) also ban any AI use of their
content: place-finding here is plain rules, and those feeds are marked `no_ai` in
`config.yaml` so a language model (the planned local brief) never sees their headlines.
The feeds are under `sources.news`; any of them can be switched off by deleting its line.

### Story clusters

Every 30 seconds sitrep looks at the last 6 hours of items (headlines, warnings, road
hazards, train disruptions, and Bluesky posts that name a Sydney place) and groups the
ones about the same event. Two items are the same story when both hold:

- **the same place:** the same suburb, the same or a neighbouring 2 km square, or both
  about Sydney as a whole; and
- **overlapping words:** two distinctive words in common, or the same event word (crash,
  fire, storm, flood, shooting, stabbing, evacuation, lockdown, explosion, outage, siege,
  collapse, train, police), where "bushfire" and "blaze" both count as "fire", and so on.

Corroboration counts **kinds of source, not outlets**: three outlets running the same wire
story are one kind. An official item only counts when it matters on its own: a major Live
Traffic hazard, an RFS Watch and Act or Emergency Warning, a BoM severe warning for Sydney,
or an unplanned train disruption. Routine ones can still join a story; they just don't
count as "official".

| A story with | Status |
|---|---|
| Two kinds of source | Elevated, in each category it spans |
| All three kinds: official, news and social ("triangulated") | Elevated, shown first |
| All three, in Greater Sydney, with an emergency-level item or a keyword-group match | Critical |

A story counts while its newest item is less than 2 hours old; after that it's still shown
for the rest of the 6 hours, but no longer raises anything. Stories with more than one kind
of source are kept for 7 days and go on the timeline.

**Bluesky posts and privacy.** To find posts about local events, sitrep keeps the last 6
hours of posts that name a Sydney suburb (or Sydney itself) **in memory only**, and drops
a post the moment its author deletes it. When a story is saved, or put on the timeline, a
post appears as "a Bluesky post": never its text, link or author. "Sydney" followed by
another name ("Sydney Sweeney", the "Sydney Morning Herald") isn't counted as Sydney.

### Emerging terms

The keyword groups only catch what you thought to list. Emerging terms catch the rest.
Every headline and Bluesky post about Sydney or NSW is broken into terms (single words,
and two-word phrases so names like "Macquarie Park" stay whole), and each mention is
counted. A term is flagged when **all** of these hold:

- more than 5 mentions in the last 2 hours;
- more than 3 times its usual for 2 hours, from the last 7 days;
- from at least 2 different sources (different outlets, or different Bluesky accounts);
- it wasn't flagged in the last 30 minutes (one that stays busy stays flagged; one that
  goes quiet and comes back within 30 minutes isn't flagged again).

A flagged term makes **news** or **social** elevated (whichever most of its mentions came
from), never critical. If the term is a suburb, it's about that suburb's square of the map;
otherwise it's about Sydney as a whole. So it can back up another signal about the same
place, but can't make the overall status Elevated on its own. The tile's **Show busiest
terms** lists flagged terms first (suburbs before other words) with example headlines and
posts. It needs 7 days of history, and shows **learning** until then.

Everyday words never count, and the list under `terms: stopwords:` in `config.yaml` is
yours to edit: add any word that keeps coming up without meaning anything. The numbers
above are there too.

**Privacy.** Only terms and counts are kept, for 8 days. A Bluesky account is counted by
a one-way code (to tell 2 accounts from 1), never its handle, and no post text is saved.

### Convergence

Story clusters need items to share words. Convergence doesn't: it looks for several
**kinds** of event in one small area at the same time. A grass fire, a closed road and two
people posting about smoke in one suburb don't share words, and none of them matters on
its own, but together they're worth a look.

Greater Sydney is divided into squares about 2 km across. Every 30 seconds sitrep places
the last 2 hours of events in their squares. The kinds are:

| Kind | From |
|---|---|
| fire | an RFS incident (not planned burns) |
| road hazard | a Live Traffic hazard that's unplanned or major (not roadworks or events) |
| train disruption | an unplanned train disruption, at the stations its message names |
| news | a headline naming a suburb there |
| posts | Bluesky posts from **at least 2 different people** naming a suburb there |
| aircraft circling | a helicopter, or an aircraft below 3,000 ft, circling one spot for 10 minutes (see [Aircraft over Sydney](#aircraft-over-sydney)) |

When 3 or more different kinds land in one square or the squares around it, that's a
convergence, named after the place most of its events mention. Touching busy squares join
into one. It shows on the **Convergence** tile and as an amber circle on the map.

| A convergence with | Status |
|---|---|
| 3 or more kinds | Elevated, in each category its events come from |
| ...inside Greater Sydney, with an RFS Emergency Warning or a keyword-group match | Critical |

It's about its own square of the map, so it backs up anything else about the same area,
but a convergence and a story built from the same posts don't count as two separate
sources agreeing. The numbers are under `convergence:` in `config.yaml`. Posts are
listed as "A Bluesky post", never their text.

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

### Aircraft over Sydney

Every minute sitrep asks [adsb.lol](https://adsb.lol/) for the aircraft within 40 km of
the CBD. adsb.lol collects what aircraft broadcast (ADS-B) from volunteers' receivers. No
key needed. It does two things with the answer.

**Counting.** How many aircraft are in the air there, not counting ones on the ground or
airport vehicles, against what's usual for this hour of the week (the same hour over the
last 4 weeks; while it's learning, the same hour of the day over the last week). Far
fewer than usual could mean the airspace has been closed, but an adsb.lol problem is much
more likely. So when the count stays at 30% of usual or less for 3 checks in a row, sitrep
asks a second, independent network, [adsb.fi](https://adsb.fi/), once. Only if adsb.fi
also sees far fewer is it **elevated**, about Sydney as a whole; otherwise the tile says
it's probably an adsb.lol problem. It needs at least 8 aircraft usually in the air to
judge, so it says nothing at night, during Sydney Airport's curfew.

**Circling.** A helicopter (at any height), or any aircraft below 3,000 ft, that stays
within 2 km of one spot for 10 minutes or more is circling there: police, rescue and news
helicopters do that over an incident. It shows on the tile and on the map (purple
outline), goes on the timeline, and counts as "aircraft circling" for
[convergence](#convergence). It never raises anything on its own, and since not every
police aircraft broadcasts, nothing circling means nothing. Circuits near Sydney Airport
and Bankstown, balloons, gliders, and aircraft below 300 ft (landing) don't count.

Positions are kept in memory for 15 minutes only. sitrep saves the hourly average count
and each circling event (the aircraft's callsign and type, the place, how long, how
high), never anyone's track. The numbers are under `sources.adsb` in `config.yaml`.

**Terms.** adsb.lol's data is under the
[ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/), and it asks anyone using its
API "for production purposes" to get in touch. adsb.fi's open data is for personal,
non-commercial use, credited with a link. Both are credited on the tile.
airplanes.live was the first choice for the second network, but it refuses automated
access and its terms forbid it.

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
| 3 | IODA sees Telstra dropping, so two categories agree about Australia's internet | **Elevated** alert, silently |
| 4 | IODA says Australia is down | **Critical** alert, with a sound |
| 5 | Everything calms down | **All clear**, silently, about 2 minutes later |
| 6 | Your internet drops out for about a minute | nothing (it can't send while offline) |
| 7 | Your internet is back | after a short wait, a silent message saying how long it was down and what came in meanwhile |
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

### Replaying scenarios: would this have alerted?

```powershell
.venv\Scripts\python -m sitrep simulate --replay
```

This plays a set of realistic scenarios through the real scoring and alert rules, with
the real timings from `config.yaml`, on a pretend clock, so a 3-hour outage takes a
moment. It prints what the dashboard would show and which messages would be sent, with
or without a sound. Nothing is sent, no dashboard starts, and your database isn't
touched. The scenarios so far:

| Scenario | What happens | Result |
|---|---|---|
| `optus-outage` | IODA sees Optus drop, "Optus down" posts spike on Bluesky, then IODA sees Australia-wide trouble | Elevated (silent), then Critical (with a sound) |
| `grid-and-cyber` | An NSW LOR2 and an unrelated ACSC advisory | No alert (it would be a silent Elevated if any two national signals counted) |
| `telco-cyber-attack` | ACSC warns of a cyber incident at a telco, then IODA sees Telstra drop | Elevated (silent) |
| `second-emergency` | Australia drops off the internet, then NSW load shedding starts | Critical, then "Critical, another event", both with a sound |
| `back-after-outage` | 3 hours offline; the sources catch up on old advisories and notices | One silent "while you were offline" summary |
| `pacific-cable-fault` | US West and US East suddenly 100 ms slower, Sydney fine; 25 minutes later Radar sees Australian traffic dip | Nothing until Radar agrees, then Elevated (silent) |
| `bushfire-near-home` | A fire in Lane Cove National Park, 3 km away, goes from Watch and Act to Emergency Warning | Nothing for the Watch and Act on its own, then Critical (with a sound) |
| `storm-sydney` | BoM warns of severe thunderstorms in Sydney, then upgrades it to very dangerous thunderstorms | Nothing for the first on its own, then Critical (with a sound) |
| `m2-crash` | A truck rolls on the M2 at Macquarie Park: a major Live Traffic hazard, two news stories and posts from two people about the traffic (a story and a convergence) | Elevated (silent) and never critical |
| `unexplained-activity` | Near Kellyville: an RFS grass fire at Advice level, an ordinary road hazard, two people posting about smoke. No shared words, so no story | Elevated (silent): a convergence |
| `bushfire-in-the-news` | An RFS Emergency Warning 20 km away, then the news, then posts from people nearby | Elevated (silent), then Critical (with a sound) |
| `cbd-lockdown` | A police operation in the CBD, headlines about shots fired and a lockdown, posts from inside | Elevated (silent), then Critical (with a sound) two minutes later |
| `police-helicopter` | A helicopter circles over Parramatta; a headline about police there; two people post | Elevated (silent): a story, and a convergence of aircraft circling, news and posts |
| `fuel-price-jump` | Diesel near home jumps 15 c/L past its 30-day high | No alert: the fuel tile turns Elevated on its own |
| `telco-ransomware` | A gang lists an Australian broadband provider; 40 minutes later IODA sees its network drop | Nothing for the listing on its own, then Elevated (silent) |
| `sydney-cloud-outage` | AWS posts an EC2 disruption in Sydney; 15 minutes later people post that sites are down | Nothing for AWS on its own, then Elevated (silent) |
| `foreign-advice-raised` | The US raises Australia from Level 1 to Level 2 | Critical (with a sound) |
| `foreign-advice-routine` | The US reissues its advisory at the same level; the FCDO adds a bushfire-season note | No alert |
| `trains-down` | A power failure stops trains on every line; Bluesky posts about an incident in Sydney spike | Elevated (silent): both are about Sydney as a whole |

To replay just one: `python -m sitrep simulate --replay optus-outage`.

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

**A tile says "Fetch failing".** That source's last tries failed, usually because the
service itself is having trouble. The tile shows the last error and the last-known
state. sitrep keeps retrying, waiting longer each time, up to 30 minutes apart, and
tries every source again straight away when your internet comes back.

**A tile says "Frozen".** The source answers, but its data has stopped moving on: the
newest timestamp inside it is older than the source's `content_age_budget_minutes`.
That usually means the service's own data feed is stuck. If a source is often frozen
for no good reason, raise its budget in `config.yaml`.

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

Each `Assessment` also says how new its data is (`content_at`, the newest timestamp
inside the data, never the fetch time; or `None` if the data has no dates), and, when
it's raised, what it's about (`signals`, each with a stable key and a scope). A source
made of items (advisories, notices, headlines) passes each one through `see_item()`,
which remembers when sitrep first saw it and says whether it's new enough to alert on.
Set the class's `scope`, `content_budget_minutes` and `publish_delay_minutes` to suit
the source.

The base classes handle the timers, timeouts, retries with backoff and the four states,
and catch every error, so a broken source can't crash sitrep. Add the class to `REGISTRY`
in `sitrep/adapters/__init__.py`, give it a block under `sources` in `config.yaml`,
and save some sample responses under `tests/fixtures/` for its tests: real ones if
the source's licence lets you share them, made-up ones in its format if not. A second social
source (X, say) would also let the social category reach Critical when it and Bluesky
spike together.

**Alert channels** live in `sitrep/alerts.py`. Subclass `Notifier`, write `check()`
(are the settings right?) and `send(alert)`, and add it in `Runtime._build_alerts`
in `sitrep/runtime.py`. The rules for when to send are shared, so a new channel (for
example, pushing the overall status over a MeshCore mesh) gets the same cooldown as
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
  basemap.py      python -m sitrep map: downloads the Greater Sydney basemap
  geo.py          distances and the 2 km map grid
  places.py       finding NSW suburbs in headlines (the list is in data/)
  stories.py      story clusters: items from different sources about one event
  terms.py        emerging terms: words suddenly much busier than usual
  scenarios.py    scenario replays (simulate --replay)
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
  kept for 7 days and hourly totals and the timeline for 90 days. The items each source
  has seen (titles, links and times, never article text) are kept for 7 days. Older
  data is deleted automatically.
- sitrep identifies itself honestly to every service it polls (`sitrep-bot/<version>`,
  or `sitrep/<version> (…; RSS reader)` for the ACSC feeds, as explained above), plus
  the contact you set in `SITREP_CONTACT`.
- From Bluesky, only counts and a small sample of matching posts are kept, and samples
  are deleted when their authors delete them (see "Bluesky" above). Posts about Sydney
  places are held in memory for 6 hours for story clusters and never saved. Emerging terms
  keep only the terms and counts, with accounts as one-way codes, for 8 days.
- From ransomware.live, only each Australian victim's name, gang, sector, time and
  ransomware.live link are kept, for 7 days.
- From adsb.lol, aircraft positions are held in memory for 15 minutes and never saved:
  only the hourly count and circling events (callsign, type, place, duration) are kept.
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
- `advice/fcdo_australia.json`: the FCDO's travel advice for Australia, trimmed. Contains
  public sector information licensed under the
  [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
  `advice/us_advisories.json` is made up, in the State Department's format.
- `aemo/`: © AEMO, used under AEMO's
  [copyright permissions](https://aemo.com.au/privacy-and-legal-notices/copyright-permissions).
- `radar/`: Cloudflare Radar's data, under [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)
  (non-commercial use, crediting Cloudflare Radar).
- `rfs/`: © State of New South Wales (NSW Rural Fire Service), under
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- `livetraffic/` and `transit/`: © Transport for NSW, under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- `ioda/` and `telcos/`, and the BoM files, news feeds, cloud status feeds, ransomware
  victims, fuel prices and aircraft written inside `test_bom.py`, `test_news.py`,
  `test_cloud.py`, `test_ransomware.py`, `test_fuel.py` and `test_adsb.py`: made up, in
  each source's format. IODA marks its data "All Rights Reserved", BoM and the news
  outlets allow personal use only, ransomware.live doesn't allow republishing, the telcos
  and cloud providers don't say their feeds can be shared, adsb.lol's ODbL would bind
  the tests to its own licence, and the fuel prices were written before sitrep had a
  FuelCheck key, so none of their real data is included.
- `probes/`: recorded by sitrep itself.

The map page uses these, copied into `sitrep/static/vendor/` with their licences (checked
against their publishers' checksums when they were added):

- [MapLibre GL JS](https://maplibre.org/) 6.16.0, BSD 3-Clause.
- [PMTiles](https://github.com/protomaps/PMTiles) 4.5.0 (JavaScript) and the `pmtiles`
  Python package 3.8.1, BSD 3-Clause.
- [Protomaps basemaps](https://github.com/protomaps/basemaps) 5.7.2 (the map style),
  BSD 3-Clause; its fonts (Noto Sans) under the SIL Open Font License, and its icons,
  derived from Mapzen's MIT-licensed icons.

The place-name list, `sitrep/data/places_nsw.csv`, is made from the ABS's Suburbs and
Localities 2026, © Commonwealth of Australia (Australian Bureau of Statistics), under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); see `sitrep/data/README.md`.

The basemap file itself isn't part of sitrep: `python -m sitrep map` makes it from
OpenStreetMap data, © OpenStreetMap contributors, under the ODbL.
