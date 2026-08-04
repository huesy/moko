# WORLD.md — visual world model (retired)

**Status: scrapped 2026-07-13.** There is no visual world-building system in
the tree today.

## What was tried

A persistent home for Moko — rooms, objects, and spatial consistency across
selfies and photos — built on top of 2D generative image models. Several
approaches were attempted (text-only authoring, reference images, canon
sheets, 3D blocking as a layout prior, floor plans, tiles). Visual
*consistency* never landed reliably, and the value never justified the
complexity.

## What was removed

- `agent/worldmodel.py`, the `my_world` / `change_world` tools, and the
  builtin `world` skill
- Floor-plan / tile / blocking generation and the monitor World tab
- World-grounding of selfies and photos

## What replaced it

Three plain image modes in `agent/media_studio.py`:

1. **Selfie** — character reference + mood + scene context (time / weather /
   season)
2. **Photo Moko takes** — one-off scene, no persistent world
3. **General image** — `generate_image` for unrelated assets

## What was kept

The **two-worlds graph split** (`worlds` table + `world_id` on subjects):
companion world vs Moko's own. It scopes private thoughts and keeps subject
resolution from merging "the companion's kitchen" with "Moko's kitchen". A
future world feature can reuse that split when models can support it.

See `ROADMAP.md` (M11) and `CHECKLIST.md` for the short status line.
