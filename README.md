# Inventory

A parts inventory for people who build on perfboard.

Every inventory tool ever written assumes stock leaves and never comes back,
because they were all designed for warehouses. That is not how a hobby bench
works. You build something, it holds your parts hostage for three weeks, you
tear it down, and most of them come back, minus the ones soldered into the
board, minus the ones you let the smoke out of.

So parts here don't get *consumed*. They get **held**.

## The idea

A project doesn't use parts up. It borrows them. Allocating a DHT22 to a build
doesn't change how many you own. It changes how many you can reach for.

When you tear the project down, you go through the parts list and say what
became of each one:

| | what happens |
|---|---|
| **Returned** | back on the shelf, available again |
| **Soldered in** | inside a finished board, gone for good |
| **Broken** | burned out, gone for good |

Soldered and broken are the only two ways a part ever leaves your inventory.
Everything else is a loan.

The teardown screen is the point of this app. Every other screen exists to make
that one possible.

## How the numbers work

One number is stored. The other two are derived, so they can't drift.

```
qty_owned    stored     everything still yours: loose in the bin
                        plus whatever active projects are holding

held         derived    sum of allocated - returned - soldered - broken,
                        across ACTIVE projects only

available    derived    qty_owned - held        <- the number you look at
```

Archiving a project releases everything it was holding, which is what makes
teardown work without any special-casing.

### Worked example, ten 10k resistors

| | owned | held | available |
|---|---|---|---|
| starting stock | 10 | 0 | **10** |
| allocate 4 to a build | 10 | 4 | **6** |
| hand 1 back mid-build | 10 | 3 | **7** |
| tear down: 2 soldered, 1 broken | 7 | 0 | **7** |

Two are inside a finished board forever, one is dead, seven are loose. Note
that soldered and broken don't move `available`, because those parts were already
held, so leaving both numbers cancels out. Only returning gives you anything
back.

### When you haven't got enough

Allocation doesn't refuse you. It takes what's there and records the rest as
**short**, because running out is information rather than an error:

```
qty_wanted     what this build needs
qty_allocated  what it actually got - never more than wanted
short          the difference, and the reason the shopping list exists
```

Short parts aren't held by anyone. They don't exist yet.

Shortfall can only exist attached to a build, so `Part.qty_to_buy` covers the
other half: "I'm running low on 10k resistors", which has nothing to do with
any project. The bench page's shopping list totals both sources per part,
because you buy per part rather than per project.

Receiving a delivery through `Part.receive()` takes the part back off the list.
That is deliberately separate from `adjust_stock()`, because only a purchase
satisfies a want: stock returning from a reversed teardown should not tell you
that you have finished shopping.

### Taking a teardown back

Teardown is the only operation here that destroys information, and it is two
clicks from a list page. Archived projects can be reopened: the project goes
back on the bench and everything written off as soldered or broken returns to
stock, recorded as its own movement so the undo is as visible as the teardown.

Parts handed back *during* the build stay handed back. That needs
`ProjectPart.teardown_returned`, because `qty_returned` mixes early returns with
teardown returns and only the second kind should be reversed. Soldered and
broken need no equivalent, since nothing but a teardown ever sets them.

### Where a number came from

Every change to `qty_owned` writes a `StockMovement` saying what happened and
what the balance became: opening balance, delivery, recount, teardown loss.
Teardown losses name the project that ate them. A part's own page shows the
whole history.

`qty_owned` stays a stored column because every list page reads it, but nothing
may move it except `Part.adjust_stock()`, which writes the movement in the same
transaction. `Part.save()` opens the ledger when a part is created, so a
quantity can't exist without a line explaining it.

```bash
python manage.py check_stock          # do the two agree?
python manage.py check_stock --fix    # write a movement explaining any gap
```

Those can only disagree if something changed a quantity without going through
`adjust_stock()`, which would be a bug. The command is how you find out rather
than assuming it never happened.

### What the database enforces

Not just the forms. These are `CheckConstraint`s and a `UniqueConstraint`, so
they hold even if a future view has a bug in it:

- one line per part per project
- a line must want something
- `allocated` can never exceed `wanted`
- `returned + soldered + broken` can never exceed `allocated`

And in application code: you can't allocate more than is available, you can't
edit `qty_owned` below what's currently held, and a teardown must account for
every part exactly, with no leftovers. A teardown that fails validation changes
nothing at all, because the whole thing runs in one transaction.

## Running it locally

```bash
python -m venv venv
venv\Scripts\Activate.ps1          # Windows; source venv/bin/activate elsewhere
pip install -r requirements.txt
```

Create a `.env` next to `manage.py`, modelled on `.env.sample`:

```
SECRET_KEY=anything-for-local-use
DEBUG=True
```

Then:

```bash
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

### Demo data

```bash
python manage.py seed_demo --user demo --password something
```

Thirty-six realistic parts and four projects, chosen so that between them they
show every state the model has: one holding a lot, one with parts already handed
back mid-build, one torn down with real losses, and one torn down clean. One
part is left fully committed so `available: 0` appears on the list. Safe to
re-run; it wipes and rebuilds that user's data.

### Tests and linting

```bash
pip install -r requirements-dev.txt
python manage.py test core
ruff check .
ruff format --check .
```

Two hundred and seven of them, mostly on the arithmetic. The invariant that soldered and
broken decrement `qty_owned` in the same transaction as the teardown is the one
thing in this app that can silently corrupt every number, so it's covered from
several directions.

## Finding your way around

`/guide/` is a short how-it-works page covering the whole loop: getting parts
in, allocating, running short, and tearing down. It needs no login, so someone
deciding whether to sign up can read it first. The **Inventory** wordmark in
the nav goes there; **Bench** is the dashboard.

## The bench

`/` is a dashboard: what's on the bench with held and short counts, a shopping
list totalling shortfall per part across every live build, and the parts you're
closest to running out of. The parts table itself lives at `/parts/` and sorts
on any column, and availability ascending answers "what am I nearly out of".

## Tags

Tags are a comma-separated string on the part, normalised to a single ", "
separated form on save with duplicates collapsed. That is what lets the parts
list filter on a whole tag in SQL: a substring match would make `i2c` also
match `i2c-pullup`.

Tags are clickable everywhere, the add form autocompletes from tags already in
use, and `/tags/` lists them with counts and renames one across every part at
once. A part's page shows others sharing its tags. Whether any of them actually
substitutes is a judgement the app does not make.

## Duplicates

`value` is free text, so `10k`, `10 K` and `10kohm` are one resistor to you and
three rows to a database, with your count split across all three.

`match_key()` compares parts with case, spacing, hyphens, micro signs and the
word "ohm" ignored. Full stops are deliberately kept: stripping them would fold
`4.7k` into `47k` and invent a duplicate that isn't one, which is worse than
missing a real one. A word boundary keeps a part named "Ohmite" intact.

The importer refuses near-duplicates, a part's own page warns when it spots a
twin, and `/parts/duplicates/` lists every group.

Merging folds one part into another and deletes it. Quantities and want lists
add up, project lines combine where both parts appear in the same build, and
the history moves across with every `balance_after` recomputed in date order,
because two interleaved running balances would be nonsense and the ledger has
to keep reconciling.

## Getting a bin in

Entering a hundred parts one form at a time is how these tools die with an
empty database, so there are two paths.

**Paste a list** at `/parts/import/`, one part per line:

```
name, quantity, value, package, tag, tag, ...
```

Only name and quantity are required. Everything from the fifth field onward
becomes tags, which is what lets tags contain commas without escaping. Blank
lines and `#` comments are ignored.

```
10k resistor, 180, 10k, through-hole, passive, resistor
DHT22, 4
ESP32 devkit, 2, , module, mcu, wifi
```

It's all or nothing: one malformed line rejects the whole paste with the line
numbers, because a half-applied import leaves you unable to tell what landed.
An over-long value or package is reported rather than quietly truncated.

By default a line naming a part you already own is refused. Tick **add
quantities to parts I already have** and it is treated as a delivery instead,
which lands in the ledger and clears the shopping list, so the importer works
for restocking as well as first entry.

**Or one at a time** with the add form, which reopens empty with the cursor
back in the name field so a pile goes in without touching the mouse.

## Following a part around

Click any part name for its own page: how many are owned, held and available,
which projects are holding them right now, and every finished build that
consumed some, with what was returned, soldered in and broken. That answers
"where did my 10ks go", which the data always supported and nothing used to
show you.

**Add stock** lives there too, for when a delivery arrives: type how many came
and it adds to what you own. Correcting a miscount is a different operation
and stays on the edit form where you set the absolute number, so there's never
a question of which one you meant.

## Accounts

Anyone with the URL can sign up at `/accounts/signup/`. To close that, set
`SIGNUP_CODE` to any string in the environment, and the signup form then asks for
it and rejects anything else. No deploy needed; it's read at request time.

### Password reset

Reset needs a mail server. Until one is configured the reset page says so
plainly instead of showing "check your inbox" for an email that will never
arrive, and the login page doesn't offer the link at all. That check runs per
request, so setting `EMAIL_HOST` turns the whole flow on without a redeploy.

To enable it, set these in the environment:

```
EMAIL_HOST=smtp.your-provider.com
EMAIL_PORT=587
EMAIL_HOST_USER=your-username
EMAIL_HOST_PASSWORD=your-app-password
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL=Inventory <you@yourdomain.com>
```

Then check it actually works before trusting it with a reset link:

```bash
python manage.py test_email you@example.com
```

That prints which backend is live and the host it's talking to, sends a real
message, and shows the actual exception if it fails rather than failing
silently.

Two things that catch people out: port 587 is for TLS and 465 is for SSL, and
most providers (Gmail included) want an app-specific password rather than your
account password.

**Without a mail server**, resetting a forgotten password is a shell command:

```python
python manage.py shell -c "from django.contrib.auth import get_user_model; U=get_user_model(); u=U.objects.get(username='...'); u.set_password('...'); u.save()"
```

## Backups

Once your bin is in here the data is irreplaceable and lives on one Postgres
instance.

```bash
python manage.py backup                  # backup-<timestamp>.json
python manage.py backup --to-stdout      # pipe it somewhere yourself
python manage.py loaddata backup-....json
```

The flag is `--to-stdout` rather than `--stdout` on purpose: `call_command()`
maps a flag's name onto its dest, so a `--stdout` flag swallows the `stdout=`
kwarg callers use to redirect output and the redirect silently stops working.

## Rate limiting

Signup is open and login accepts anything, so both are throttled per IP: ten
failed logins in five minutes, five signups an hour. Only failures count, so a
successful login clears the tally and using the app normally never trips it.

No dependency for this. One cache-backed counter is enough for a single
container, and the honest limitation is written down in `core/throttle.py`: the
default cache is per process, so with several workers the effective limit is
roughly the configured one times the worker count. That still turns unlimited
guessing into slow guessing.

## Continuous integration

Every push and pull request runs `.github/workflows/ci.yml`: lint, format check,
Django system checks, a missing-migrations check, the full test suite, and
`check --deploy` with production settings. That last one means a security
setting can't quietly regress, and the migration check catches a model changed
without `makemigrations`, which deploys fine and then fails against the real
database.

## Deployment

Railway, Postgres, WhiteNoise for static files, gunicorn.

`GET /healthz/` is an unauthenticated liveness probe returning JSON. It touches
the database, because a container that booted but can't reach Postgres isn't
healthy and shouldn't be handed traffic. It's exempt from the HTTPS redirect so
an internal probe over plain HTTP doesn't get a 301 and read as a failure.

| Variable | |
|---|---|
| `SECRET_KEY` | required |
| `DEBUG` | `False` in production |
| `DATABASE_URL` | injected by Railway when you link the Postgres service |
| `RAILWAY_PUBLIC_DOMAIN` | injected once you generate a domain; drives `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` |
| `SIGNUP_CODE` | optional; set it to close open signup |
| `EMAIL_HOST` | optional; setting it switches mail from console to SMTP |
| `EMAIL_PORT` `EMAIL_HOST_USER` `EMAIL_HOST_PASSWORD` `EMAIL_USE_TLS` | SMTP details |
| `DEFAULT_FROM_EMAIL` | the From address on reset mail |
| `TIME_ZONE` | display timezone, defaults to `America/New_York`; storage is always UTC |

With `DEBUG=False` the app forces HTTPS, marks session and CSRF cookies secure,
and sends a one-hour HSTS header. `SECURE_PROXY_SSL_HEADER` is set alongside
the HTTPS redirect and is not optional, because Railway terminates TLS at its edge and
forwards over plain HTTP, so without that header Django would think every
request was insecure and redirect forever.

`python manage.py check --deploy` should report only the mail backend error
until you configure SMTP. Two HSTS warnings are silenced deliberately; the
reasoning is in `settings.py` next to them.

Two things that are easy to get wrong on Railway specifically:

- **Dashboard settings override the `Procfile`.** If a custom start command is
  saved in the UI, the `Procfile` is ignored entirely and pushing changes to it
  does nothing.
- **Migrations run from Settings → Deploy → Pre-Deploy Command**, not from the
  `Procfile` `release:` line. `collectstatic` goes in Settings → Build → Custom
  Build Command.

## Stack

Django 6.1, Postgres in production and SQLite locally, Pico.css. No JavaScript.

## Layout

```
core/
  models.py      Part, Project, ProjectPart - the quantity rules live here
  views.py       accounts, parts and projects CRUD, allocation, teardown
  forms.py       signup, part form, allocation form, teardown formset
  admin.py       a power-user view over the same data
  tests.py
  templates/core/   app pages
  static/           favicon
  management/commands/   seed_demo, test_email
templates/registration/  login, signup, password reset
                         (project level so they beat the admin's copies)
config/          settings, urls, wsgi
.github/workflows/ci.yml
```

## License

MIT. See `LICENSE`.
