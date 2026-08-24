# inventory system

an electronics parts inventory that tracks how many of each component you own,
how many are allocated to projects, and how many are actually available.

![the bench dashboard](docs/screenshot.png)

## try it

**[inventory-system-manager.vercel.app](https://inventory-system-manager.vercel.app)**

sign up with any username and password, it asks for nothing else. or log into
the demo, already loaded with 38 parts and 4 projects:

```
username: demo
password: stardancedemo
```

## the idea

every other inventory tool assumes stock leaves and never comes back, because
they were built for warehouses. on a hobby bench it comes back. you tear a build
down and most of it returns, minus the parts soldered in and the ones you let
the smoke out of.

so parts don't get used up here, they get **held**:

```
owned      everything you have, loose or inside a live project
held       sitting inside a project right now
available  owned minus held
```

## features

- **teardown**: say what became of every part. returned, soldered in, or broken.
  only the last two leave your inventory
- **reopen**: teardown is reversible, in two clicks
- **shopping list**: what live builds couldn't get, plus anything you flagged,
  totalled per part
- **history**: every quantity change logged with the balance it produced
- **duplicates**: `10k` and `10kΩ` are one resistor. it spots them and merges
  them. `4.7k` and `47k` it leaves alone
- **paste import**: a whole bin at once, one part per line
- **tags**: filter by one, autocomplete so typos don't split a group, rename
  across every part

## running it locally

python 3.12 or newer, sqlite by default, no system dependencies.

```bash
python -m venv venv
venv\Scripts\Activate.ps1        # or: source venv/bin/activate
pip install -r requirements.txt
```

add a `.env` next to `manage.py` holding `SECRET_KEY=anything` and `DEBUG=True`,
then:

```bash
python manage.py migrate
python manage.py seed_demo --user me --password something
python manage.py runserver
```

## how it works

**one stored number, two derived.** `qty_owned` is the only quantity in the
database. `held` and `available` come from one annotated query, so they cannot
drift out of sync with it.

**nothing moves that number except `Part.adjust_stock()`**, which takes a row
lock and writes the history line in the same transaction. `check_stock`
reconciles the two on demand, because a guarantee you can't verify is a hope.

**allocation doesn't refuse you.** it takes what exists and records the rest as
short, because running out is information rather than an error. that shortfall
is what the shopping list is built from.

some rules live in the database rather than in the forms, so they hold even if a
view is wrong later: one line per part per project, allocated never exceeds
wanted, accounted never exceeds allocated.

## stack

django 6.1, postgres on [neon](https://neon.tech) and sqlite locally,
whitenoise, [pico.css](https://picocss.com). deployed on vercel. no javascript.
