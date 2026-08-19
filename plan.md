# Inventory System — Build Plan

A manual-entry electronics parts inventory web app.

Target: **Hack Club Stardance, Sept 30 2026** · Budget: **~33h** (40h ceiling)

---

## The idea

Parts don't get *consumed*. They get **held**.

A project takes parts out of the bin and holds them hostage until you tear it down. When you tear it down you get most of them back — minus the ones soldered into the board, minus the ones you let the smoke out of.

That's **custody**, not consumption. Every other inventory tool models a warehouse, where stock leaves and never returns. This models a hobbyist bench, where it usually does.

The teardown screen is the only original thing in this app. Protect it.

---

## Scope

**In:**
- Accounts (Django built-in auth)
- Parts: create, edit, delete, search
- Projects: create, allocate parts, view allocations
- The project's allocation table **is** the BOM — there is no separate BOM feature
- Availability check on allocation (this is the shortfall feature)
- Teardown: line-by-line returned / soldered / broken, project archived
- Deployed, with demo data and a README

**Out — do not build these:**
- KiCad or netlist import
- Locations, drawers, storage hierarchy
- Photo or OCR part identification
- Supplier catalog, purchase orders, MOQ, receiving
- Movement ledger / audit history
- Typed part categories with dynamic fields
- Substitute-part relations (tags cover this)
- Dark mode, CSV import, barcodes, sharing between users

---

## Data model

### The numbers

**Stored on the part:**

- `qty_owned` — how many are still yours to use: loose in the bin **plus** locked in active projects. Parts soldered into a finished board or burned out have left this number permanently.

**Derived, never stored:**

- `held` — how many are locked in active projects right now
- `available` = `qty_owned − held` ← the number you actually look at

### Per allocation line

Every `ProjectPart` row (one part, one project) carries four counts:

| Field | Meaning |
|---|---|
| `qty_allocated` | how many the project took from the bin |
| `qty_returned` | came back, reusable |
| `qty_soldered` | permanently in the board, not coming back |
| `qty_broken` | burned out, dead |

```
remaining_on_line = allocated − returned − soldered − broken

held(part) = SUM(remaining_on_line) over lines whose project.status = 'active'
```

Archived projects contribute nothing to `held`. That's what makes teardown work.

### The invariant

> **When `qty_soldered` or `qty_broken` increases by _n_, `qty_owned` decreases by _n_, in the same transaction.**

Soldered and broken are the only two ways a part leaves permanently. Get this wrong and every number in the app is silently incorrect with no way to detect it.

### Worked example — 10× 10k resistors

| Event | owned | held | available |
|---|---|---|---|
| Starting stock | 10 | 0 | **10** |
| Allocate 4 to "Weather Station" | 10 | 4 | **6** |
| Return 1 early (design changed) | 10 | 3 | **7** |
| Teardown: 2 soldered, 1 broken | 7 | 0 | **7** |

Physically: started with 10, two are inside a finished board forever, one is dead, seven loose in the bin. ✓

Note that **soldered and broken don't change `available`** — those parts were already held, so removing them from both `owned` and `held` cancels out. Only `returned` gives you anything back.

### Every action

| Action | `qty_owned` | line field | `available` |
|---|---|---|---|
| Add stock (bought / found) | **+n** | — | +n |
| Allocate n to a project | — | `allocated` +n | −n |
| Return n early | — | `returned` +n | +n |
| Teardown → returned n | — | `returned` +n | +n |
| Teardown → soldered n | **−n** | `soldered` +n | no change |
| Teardown → broken n | **−n** | `broken` +n | no change |
| Fix a miscount | set directly | — | changes |

### Validation rules

1. **Allocating:** requested qty ≤ `available`. Read and write inside one transaction, or two fast clicks both pass the check.
2. **At teardown:** `returned + soldered + broken` must equal `remaining` **exactly**, per line. Not ≤. Forcing every part to be accounted for is the entire point of the screen.
3. **Editing `qty_owned`:** cannot go below `held`. You can't own 3 while 5 sit inside projects.

### Known limitation

If you scrap a finished board a year later and recover parts, the app won't know. Manually bump `qty_owned`. One edit — do not build a feature for this.

---

## Schema

```
User                       (Django built-in — don't write this)

Part
  user           FK → User
  name           CharField      "10k resistor", "6-pin blue gyro board"
  package        CharField      "0805", "DIP-8", "module"
  value          CharField      "10k", "100nF"   ← text, NOT numeric
  pin_count      IntegerField   nullable
  voltage        CharField      nullable
  notes          TextField      blank
  tags           CharField      comma-separated; also does substitutes
  qty_owned      IntegerField   default 0

Project
  user           FK → User
  name           CharField
  description    TextField      blank
  status         CharField      'active' | 'archived'
  created_at     DateTimeField  auto_now_add
  archived_at    DateTimeField  nullable

ProjectPart                    ← this is the BOM
  project        FK → Project   on_delete=CASCADE
  part           FK → Part      on_delete=PROTECT
  qty_allocated  IntegerField
  qty_returned   IntegerField   default 0
  qty_soldered   IntegerField   default 0
  qty_broken     IntegerField   default 0
  note           TextField      blank

  unique_together: (project, part)
  check: qty_returned + qty_soldered + qty_broken <= qty_allocated
```

`value` is text, not a number. "10k", "4.7µF" and "3V3" don't share a numeric type, and the moment you try to make them one you've built a unit parser. Tags handle grouping.

---

## Stack

- **Django** — you already know Python; auth, ORM, admin and transactions come free
- **SQLite** locally, **Postgres** in production (Railway provisions it)
- **Railway** or **Fly** for hosting
- **Pico.css** or **Simple.css** — classless, one `<link>` tag. Do not learn Tailwind this summer.
- **HTMX** only if autosave survives the cut list

---

## Build steps

- [x] **1 · Scaffold and deploy empty** — 3h
      Live at web-production-d6073.up.railway.app · Postgres linked · admin reachable.
      Railway notes: dashboard settings override the Procfile. Start command must be
      cleared or match. Migrations run via **Settings → Deploy → Pre-Deploy Command**,
      not the Procfile `release:` line. collectstatic runs via **Settings → Build →
      Custom Build Command**. `getpass` prompts don't work over `railway ssh` from
      PowerShell — use `--noinput` with `DJANGO_SUPERUSER_*` env vars, or the shell.

- [x] **2 · Four models, makemigrations, migrate** — 1.5h
      `Part`, `Project`, `ProjectPart` + `ProjectStatus`. `held`/`available` derived, never stored. Three DB constraints on `ProjectPart`. `Part.clean()` blocks `qty_owned` dropping below held. `Part.objects.with_availability()` annotates both in one query.

- [x] **3 · Register all four in admin** — 0.5h
      **A working inventory app now exists.** Rows scoped to the logged-in user, ownership stamped on create, held/available as sortable columns, allocation lines edited inline on the project page with autocomplete on part.

- [ ] **4 · Enter your real bin through the admin** — not coding hours
      Do this early. If your parts are still fake at hour 30, the app isn't real and you'll know it.

- [x] **5 · Parts pages** — 10h
      List with search across name/value/package/tags/notes, availability from one annotated query, add form with save-and-add-another, edit, delete guarded by PROTECT.

- [x] **6 · Projects and allocation** — 9h
      Project list split active/archived, detail page is the BOM, allocation form tops up an existing line instead of failing, early returns, line removal.

- [x] **7 · Teardown formset** — 6h
      Per-line returned/soldered/broken pre-filled to remaining. Must account for exactly what's held. One transaction: writes counts, decrements qty_owned, archives. Admin links out to it rather than duplicating it.

- [ ] **8 · Demo data, README, 90-second video** — 3h
      seed_demo command and README done. **Video still to do** — that's yours.

**Total: 33h. Ceiling 40h — spend the 7h buffer on steps 1 and 7**, which are the two that blow up.

---

## Cut list — decided in advance

If behind at hour 28, cut in this order without agonising:

1. Autosave → plain Save buttons — **−6h**
2. Tag filtering → plain search box only — **−3h**
3. Returning parts outside teardown — **−2h**

**Never cut the teardown flow.** It's the only original thing in the app.
**Never cut the README and video.** Cheapest points available.

---

## Rules

- **Log hours honestly as you go.** They're literally the payout, and reconstructing them in October means guessing low.
- **Every step ends deployed and working.** Never carry a broken `main` across a step boundary.
- **Don't refactor mid-step.** Write down what's bothering you and keep going.
- **Front-load August.** Steps 1–7 before school starts; step 8 survives being done tired.

---

## Time tracking

**Hackatime for code. Lapse for browser, setup, and everything else.**

Run both — Hackatime is passive and never forgets the editor time; Lapse covers what it can't see. Confirm with the Stardance channel that Lapse is acceptable before relying on it.

| Step | Editor (Hackatime) | Browser (Lapse) | Terminal |
|---|---|---|---|
| 1 · Scaffold + deploy | 1h05 | **1h15** | 40m |
| 2 · Models | 1h10 | 10m | 10m |
| 3 · Admin | 20m | 5m | 5m |
| 4 · Enter your bin | — | **all of it** | — |
| 5 · Parts pages | 7h | 2h30 | 30m |
| 6 · Projects + allocation | 6h30 | 2h | 30m |
| 7 · Teardown formset | 4h | **1h45** | 15m |
| 8 · Ship | 45m | 1h15 | — |
| **Total** | **~21h** | **~9h** | **~2h** |

Plus ~1h recording the video.

**Switch on blocks of 15 minutes or more.** Below that, let it ride — managing trackers costs more attention than it recovers.

The three long browser blocks worth switching for:

- **Step 1** — Railway setup, env vars, deploy logs, deployment docs (~1h15, one continuous sitting)
- **Step 7** — Django formset documentation (~1h45, dense, expect two sittings)
- **Step 8** — seeding demo data through the admin, screenshots (~1h15)

Terminal mostly takes care of itself: WakaTime-style trackers count gaps between heartbeats up to a timeout, so short `runserver` / `migrate` detours between file edits get absorbed. Real loss is 30–60 min across the whole build. Not worth solving.

**Open question for the channel:** does Step 4 (entering your real bin through the admin) count as development time, or is it data entry? Ask before you do it, not after.

---

## Stardance arithmetic

Payout = logged hours × peer multiplier (12 voters, 1–9 on originality, technicality, usability, storytelling).

| | hours | at 10× | at 12× |
|---|---|---|---|
| This app | ~33 | 330 | 396 |
| Hardware build | ~30 | 300 | 360 |
| **Total** | **63** | **630** | **756** |

962 needs either more logged hours or a higher multiplier. The gap between 10× and 12× is worth ~130 stardust — which is why the teardown flow and the video earn their hours. Getting from a 10 to a 12 is cheaper than 30 extra hours of work.
