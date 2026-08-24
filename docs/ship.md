# inventory system

an electronics parts inventory that tracks how many of each component you own, how many are locked inside projects right now, and how many you can actually grab off the bench.

**try it:** https://inventory-system-manager.vercel.app
**demo login:** `demo` / `stardancedemo`

## what it is

- every inventory tool assumes stock leaves and never comes back. they were built for warehouses
- on a hobby bench parts come *back*. so here they don't get used up, they get **held**
- start a project, it holds parts. tear it down, say what happened to each one: returned, soldered in, or broken
- only soldered and broken actually leave your inventory
- shopping list builds itself out of what your live builds asked for and couldn't get
- every quantity change is logged with the balance it produced and the project that caused it
- `10k` and `10KΩ` are one resistor to me and two rows to a database. it catches those and merges them

## what was hard

- picking what to store. i store `owned` only, and compute held and available, so they can't drift apart
- allocation used to refuse you if you were short. that meant a project could never *be* short, which meant the shopping list had nothing to build from. had to let it take what exists and record the rest
- making teardown undoable needed a whole extra field, because returned parts come from two different places and mixing them makes it unreversible
- fuzzy matching without being *too* fuzzy. `4.7k` and `47k` are different resistors so you can't just strip punctuation
- tests found 4 bugs i thought were fine. 2 of them were failing silently

## what im proud of

- the ledger. every number on the site has a receipt, and there's a command that recounts and tells you if it's lying
- teardown being reversible. it was a one way door and that was scary to use
- 208 tests, mostly on the arithmetic
- no javascript, anywhere

## how to test it

- log in as demo, go to **bench**
- open **weather station**, hit tear down, mark a few soldered and a few broken, submit
- watch owned drop, then hit **reopen** and watch it all come back
- **bench** again for the shopping list. 12 parts short across live builds
- click any part for its **history** table. every row says what changed and what it left behind
- **parts > duplicates**. `10k`/`10KΩ` and `470uF`/`470 µF` are sitting there as separate rows. merge one and watch the quantities add up
- or add a part called `10K ohm` yourself and watch it get caught immediately
