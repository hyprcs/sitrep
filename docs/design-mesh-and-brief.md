# Design: MeshCore and the local brief

**Status: design only. Nothing here is built yet.** This sets out how two planned
pieces would fit into sitrep, so they can be built later without reworking anything:

1. a **MeshCore** source: which mesh nodes and repeaters your node hears, and their
   battery readings, and later sending sitrep's status out over the mesh;
2. a **plain-English brief**, written by a local model through Ollama on your offline AI
   machine, that only summarises what sitrep holds, cites it, and says "nothing notable"
   when nothing is.

The code sketches below show the shape of each piece (names, settings, what goes in and
out). They follow the patterns sitrep already uses: adapters in `sitrep/adapters/`,
settings in `config.yaml`, secrets in `.env`, alert channels as a `Notifier`.

---

## 1. MeshCore

### What MeshCore offers from Python

MeshCore has an official Python library, [`meshcore`](https://github.com/meshcore-dev/meshcore_py)
(on PyPI as `meshcore`; 2.3.17 when this was written; MIT licence; Python 3.10 or newer).
It talks to a node running MeshCore's **companion radio** firmware, the firmware a phone
app normally connects to. A node running repeater firmware can't be read this way.

- **Connecting:** `MeshCore.create_serial(port, 115200)` over USB,
  `MeshCore.create_tcp(host, port)` for a node with Wi-Fi companion firmware, or
  `MeshCore.create_ble(address)` over Bluetooth.
- **Nodes heard:** `commands.get_contacts(lastmod=...)` lists every node the companion
  knows: `public_key`, `type`, `adv_name`, `last_advert`, `adv_lat`, `adv_lon`,
  `out_path_len` (hops), `lastmod`. New ones arrive as `NEW_CONTACT` events, and adverts
  as `ADVERTISEMENT` events.
- **Your node's battery:** `commands.get_bat()` returns a `BATTERY` event (`level`,
  probably in millivolts).
- **A repeater's health:** `commands.req_status(contact)` returns a `STATUS_RESPONSE`
  (battery, TX queue, noise floor, packet counts). Some repeaters only answer after
  `send_login(contact, password)`.
- **Sending:** `commands.send_chan_msg(channel, text)` to a channel, or
  `send_msg(contact, text)` to one node.

Its dependencies are `bleak` (Bluetooth), `pycayennelpp`, `pycryptodome` and
`pyserial-asyncio-fast`.

### What the source does

| Every | It does | Saved |
|---|---|---|
| As they happen | Notes each node heard (advert or new contact) | Node, type, when last heard |
| 5 minutes | Reads your own node's battery | Hourly battery, 90 days |
| 30 minutes | Asks each repeater you list in `config.yaml` for its status | Hourly battery, noise floor, 90 days |

The tile would show your node's battery, how many nodes and repeaters were heard in the
last hour and the last day, and each listed repeater's battery and when it was last heard.
The map would show repeaters that advertise a location.

**How it's judged, at first:** information only (category `info`). It never changes the
overall status. Once there's a few weeks of history, one rule is worth trying: **several
of your listed repeaters going silent at the same time** in your area could mean a local
power cut, since repeaters on mains power drop together. That would be elevated, about
those repeaters' map squares, so it backs up AEMO or posts about a blackout nearby. It
would be added only after checking the history, keeping to "rather miss a small event
than be alerted by noise".

**Privacy:**

- sitrep never reads or stores message text. It only reads contacts, adverts, battery
  and status.
- Companion nodes (people's phones) can advertise a location. sitrep keeps locations
  only for repeaters and room servers, never for people's nodes.
- Node names are kept, because that's how you recognise them.

### How sitrep connects

| Where sitrep runs | Connection |
|---|---|
| Your Windows PC | USB: the node shows up as a COM port (for example `COM5`) |
| The NAS, in Docker | USB: plug the node into the NAS and pass it into the container (`devices: ["/dev/ttyACM0:/dev/ttyACM0"]` in `docker-compose.yml`) |
| Anywhere, if the node has Wi-Fi companion firmware | TCP to the node's address on your network (simplest for Docker) |

Bluetooth is possible, but it isn't a good fit for a NAS.

Only one program can use the companion at a time. While sitrep is connected, the MeshCore
phone app can't be, unless the node is shared over TCP.

### Interface sketch (not built)

```python
# sitrep/adapters/mesh.py  (not built: the shape it would take)

class MeshSettings(StreamingSettings):
    enabled: bool = False                    # off until you set it up
    connection: Literal["serial", "tcp", "ble"] = "serial"
    port: str = "${MESH_PORT:-COM5}"         # serial port, or the node's address for tcp/ble
    tcp_port: int = 4000
    battery_every_minutes: float = 5
    status_every_minutes: float = 30
    repeaters: list[MeshRepeater] = []       # the repeaters to ask for status, by name
    # Passwords for repeaters that need a login go in .env (MESH_REPEATER_PASSWORDS).


class MeshRepeater(Settings):
    name: str                                # as it advertises itself
    password_env: str | None = None          # the .env variable holding its password, if any


class MeshAdapter(StreamingAdapter):
    id = "mesh"
    name = "Mesh (MeshCore)"
    category = Category.INFO                 # information only, at first
    content_budget_minutes = None            # a quiet mesh is normal

    async def stream(self) -> None:
        """Connect, then: subscribe to NEW_CONTACT and ADVERTISEMENT; read contacts once;
        every battery_every_minutes call get_bat(); every status_every_minutes call
        req_status() for each listed repeater; report() after each. Raises on disconnect,
        so the base class reconnects with backoff."""


# The data it would report, in Assessment.details:
@dataclass(frozen=True)
class MeshNode:
    key: str            # the first 12 hex characters of the public key
    name: str
    kind: str           # "companion", "repeater", "room" or "sensor"
    last_heard: float
    hops: int | None
    lat: float | None   # repeaters and room servers only
    lon: float | None
    battery_mv: int | None
```

New database table, `mesh_nodes` (`key`, `name`, `kind`, `first_heard`, `last_heard`,
`lat`, `lon`), kept while a node is heard and deleted 90 days after it was last heard.
Battery and noise readings use the existing hourly `gauges` table (series
`battery:<key>`, `noise:<key>`).

### Later: sending sitrep's status over the mesh

This is the useful part when the internet is down: sitrep can still reach phones on the
mesh. It would be a second alert channel, next to Telegram, built on the same `Notifier`
base class (`sitrep/alerts.py`):

```python
class MeshNotifier(Notifier):
    name = "Mesh"
    # Sends to one private channel (set up in the MeshCore app; its name and secret go in
    # .env), never the public channel.

    async def check(self) -> None:
        """Connected to the node, and the channel exists."""

    async def send(self, alert: Alert) -> None:
        """One short message, at most 130 characters, for example:
        'sitrep CRITICAL 14:05 Australia offline (IODA+Radar)'
        'sitrep: internet down 13:52, back 14:20'"""
```

Limits, because radio airtime is shared:

- only Critical, and the all-clear after a Critical;
- the "can't reach the internet" and "back online" messages;
- at most one message every 10 minutes;
- never Elevated or routine messages.

The notifier shares the source's connection, because only one program can use the
companion at a time.

### To settle when building

1. **Firmware:** check your node runs companion firmware (USB, BLE or Wi-Fi), and how it
   connects to the computer that will run sitrep.
2. **Repeaters:** which ones to watch, and whether they need a password for status.
3. **Contact types:** what each `type` number means (expected: 1 companion, 2 repeater,
   3 room server, 4 sensor). Check against the firmware.
4. **Status fields:** the exact field names in `STATUS_RESPONSE`, and the unit of the
   battery `level` (expected: millivolts).
5. **The channel:** which one to send status to.

---

## 2. The local brief (Ollama)

### The rules (from the spec)

- It may only summarise items sitrep actually holds.
- Every statement must cite the IDs of the items it's based on.
- It must say "nothing notable" when nothing is.

Three more rules follow from sitrep's own terms and design:

- **No Guardian or Sydney Morning Herald content.** Both outlets ban AI use of their
  content. Their headlines (marked `no_ai` in `config.yaml`) are never sent to the model.
  The brief notes how many were left out.
- **No post text.** Bluesky post text is never saved, so the model only sees counts,
  spikes and emerging terms.
- **Advice, not alarms.** The brief never changes a level and never triggers or appears
  in an alert. Scoring and alerts stay rule-based, so a model that's wrong, slow or
  switched off can't cause or hide an alert.

### How it works

```
sitrep ──(1) builds a pack of numbered items──► rules decide: anything notable?
                                                 │ no  → "Nothing notable in the last 6 hours." (no model)
                                                 │ yes ↓
        ◄─(3) checks every citation and number── (2) Ollama on your AI machine writes JSON
```

1. **The pack.** sitrep gathers the last 6 hours of:
   - the overall status and each category's level;
   - raised signals, story clusters with more than one kind of source, and convergences;
   - flagged emerging terms;
   - notable items (anything elevated or critical, notable official items, foreign
     advice changes, cloud incidents, aircraft circling);
   - the gaps panel, so the brief can say what sitrep can't see.

   Each entry gets a short ID (`i01`, `i02`, …) that maps back to a real sitrep item,
   story or signal. Small models copy short IDs far more reliably than long ones. At
   most 40 entries; anything older or less notable is left out first.
2. **"Nothing notable" is decided by rules, not the model.** If the pack has no raised
   signal, no multi-source story, no convergence, no flagged term and no elevated item,
   sitrep writes "Nothing notable in the last 6 hours." itself and doesn't call the
   model. So the guarantee doesn't depend on the model's judgement.
3. **The model's answer has a fixed shape.** Ollama's `/api/chat` is called with
   `format` set to a JSON schema, `temperature` 0 and `stream` false:

   ```json
   {"nothing_notable": false,
    "points": [{"text": "Optus customers in NSW are losing internet; IODA and posts agree.",
                "cites": ["i03", "i07"]}]}
   ```

4. **Every point is checked before it's shown.** A point is dropped if:
   - it cites nothing, or cites an ID that isn't in the pack;
   - it contains a number that doesn't appear in the items it cites. This is a simple
     guard against invented figures.

   If the pack had something notable but no point survives, the dashboard shows "The
   brief couldn't be checked" and lists the notable items plainly instead.

### Where it shows

A **Brief** panel at the top of the dashboard shows:
- each point, with its citations as small links to the items;
- when it was written, and by which model;
- the line "Written by a local model from sitrep's own items; check the cited items."

It's rewritten every 30 minutes, whenever the overall status changes, and when you click
**Write a new brief**. The last brief is kept, so the panel isn't empty while the model
is busy or the AI machine is off. If the AI machine can't be reached, the panel says so
and shows the rule-based list of notable items.

### Interface sketch (not built)

```python
# sitrep/brief.py  (not built: the shape it would take)

class BriefSettings(Settings):
    enabled: bool = False
    ollama_url: str = "${OLLAMA_URL:-}"      # your AI machine, e.g. http://ai-box:11434 on the tailnet
    model: str = "${OLLAMA_MODEL:-}"         # any model that handles JSON schemas (Ollama 0.5 or newer)
    hours: float = 6
    max_items: int = 40
    every_minutes: float = 30
    timeout_seconds: float = 120


@dataclass(frozen=True)
class PackEntry:
    id: str          # "i01"
    ref: str         # what it points to in sitrep: "item:rfs:12345", "story:ab12", "signal:ioda"
    kind: str        # "status", "signal", "story", "convergence", "term", "item", "gap"
    text: str        # one line, as the dashboard would say it
    level: str
    at: float | None


@dataclass(frozen=True)
class Pack:
    entries: tuple[PackEntry, ...]
    notable: bool    # decided by rules: is there anything to summarise?
    withheld: int    # headlines left out because their outlets ban AI use


@dataclass(frozen=True)
class Brief:
    written_at: float
    model: str | None             # None when sitrep wrote "nothing notable" itself
    nothing_notable: bool
    points: tuple[BriefPoint, ...]
    dropped: int                  # points that failed the checks
    problem: str | None = None    # "AI machine unreachable", "couldn't be checked", ...


def build_pack(db: Database, views: list[SourceView], stories, convergence, terms,
               now: float, s: BriefSettings) -> Pack: ...

async def write_brief(pack: Pack, http: httpx.AsyncClient, s: BriefSettings) -> Brief: ...

def check_points(raw: dict, pack: Pack) -> tuple[list[BriefPoint], int]: ...
```

Two new API routes: `GET /api/brief` (the latest brief) and `POST /api/brief` (write a
new one now). Briefs are kept for 7 days, in a `briefs` table, so you can look back at
what it said.

### To settle when building

1. Your AI machine's address on the tailnet, and the model it runs.
2. How long a brief takes to write there. If it's over a minute, the 30-minute timer
   might be too frequent.
3. Whether the ABC's terms are fine with AI summaries of its headlines. If not, its
   feeds get marked `no_ai` too.

---

## Order to build them in

1. **MeshCore source.** It's information only, so it's low risk, and it gives the mesh
   history needed for the power-cut rule.
2. **Brief.** It's independent of the mesh. Build it once the AI machine's address and
   model are known.
3. **Mesh notifier.** It depends on the MeshCore connection, and is most useful once
   you've chosen a private channel.
