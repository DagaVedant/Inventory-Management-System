# Inventory

A parts inventory for people who build on perfboard.

Every inventory tool ever written assumes stock leaves and never comes back,
because they were all designed for warehouses. That is not how a hobby bench
works. You build something, it holds your parts hostage for three weeks, you
tear it down, and most of them come back — minus the ones soldered into the
board, minus the ones you let the smoke out of.

So parts here don't get *consumed*. They get **held**.

## The idea

A project doesn't use parts up. It borrows them. Allocating a DHT22 to a build
doesn't change how many you own — it changes how many you can reach for.

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
that soldered and broken don't move `available` — those parts were already
held, so leaving both numbers cancels out. Only returning gives you anything
back.

### What the database enforces

Not just the forms — these are `CheckConstraint`s and a `UniqueConstraint`, so
they hold even if a future view has a bug in it:

- one line per part per project
- allocations must be positive
- `returned + soldered + broken` can never exceed `allocated`

And in application code: you can't allocate more than is available, you can't
edit `qty_owned` below what's currently held, and a teardown must account for
every part exactly — no leftovers. A teardown that fails validation changes
nothing at all, because the whole thing runs in one transaction.

## Running it locally

```bash
python -m venv venv
venv\Scripts\Activate.ps1          # Windows; source venv/bin/activate elsewhere
pip install -r requirements.txt
```

Create a `.env` next to `manage.py` — see `.env.sample`:

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

Twenty realistic parts and four projects: two on the bench holding parts, two
torn down — one of which cost a regulator and a servo. Safe to re-run; it wipes
and rebuilds that user's data.

### Tests

```bash
python manage.py test core
```

Sixty of them, mostly on the arithmetic. The invariant that soldered and
broken decrement `qty_owned` in the same transaction as the teardown is the one
thing in this app that can silently corrupt every number, so it's covered from
several directions.

## Accounts

Anyone with the URL can sign up at `/accounts/signup/`. To close that, set
`SIGNUP_CODE` to any string in the environment — the signup form then asks for
it and rejects anything else. No deploy needed; it's read at request time.

Password reset needs a mail server. Until one is configured the reset page says
so plainly instead of showing a "check your inbox" message for an email that
will never arrive, and the login page doesn't offer the link at all. Resetting
a forgotten password meanwhile is a shell command on the server:

```python
python manage.py shell -c "from django.contrib.auth import get_user_model; U=get_user_model(); u=U.objects.get(username='...'); u.set_password('...'); u.save()"
```

## Deployment

Railway, Postgres, WhiteNoise for static files, gunicorn.

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
the HTTPS redirect and is not optional — Railway terminates TLS at its edge and
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
  views.py       parts and projects CRUD, allocation, teardown
  forms.py       part form, allocation form, teardown formset
  admin.py       a power-user view over the same data
  tests.py
  management/commands/seed_demo.py
config/          settings, urls, wsgi
```
