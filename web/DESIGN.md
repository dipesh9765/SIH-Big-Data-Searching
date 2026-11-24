# Design notes

This is a tool people use to find things in their own files, so the interface is built like a
reference book's index: a ruled ledger of entries, set in a book face for titles and a plain system
face for everything else. The same tokens (`src/tokens.css`) are used by the other web projects in
this portfolio, so one change of a role changes the whole product.

The rules come from the [Hallmark](https://github.com/nutlope/hallmark) skill's published floor and
its "app screens" guidance. I applied them by hand and enforce the mechanical ones with
`npm run check:design` (`scripts/check-design.mjs`), which runs in CI.

## Decisions

| Area | Decision | Why |
| --- | --- | --- |
| Colour | Paper, panel, raised, ink in three steps, one accent (vermilion). Light and dark follow the system. | One accent means the accent means something: primary action, current place, focus. |
| Type | Book face for the wordmark and page titles; system UI face; monospace for data. Scale ratio 1.2. | A search tool is mostly reading. No web fonts, so nothing loads late and nothing is shipped. |
| Shape | Radii of 3px and 6px. No pills, no gradients, no shadows for decoration. | Tools are not toys. |
| Results | A table-like ledger: a gutter (file kind, or a timestamp for media hits), then title, snippet, facts. | Tables before cards. A hit in a recording shows the minute it was found at. |
| State | Tab, query and ranking live in the URL (`?tab=&q=&ranking=`). Back, Forward and reload work. | State in the URL. |
| Data regions | Each has loading (skeleton), empty (says what to do), error (message and Try again) and full states. | Every region can fail. |
| Destructive | Deleting a file asks first and says what is lost. | |
| Motion | Two durations, transform and opacity only, off under `prefers-reduced-motion`. | |
| Copy | Plain sentences. No invented numbers; the count in the bar is the server's. | |

## What is deliberately absent

No hero, no eyebrow labels, no tracked capitals, no middle-dot meta strings, no arrows in links,
no decorative dots, no emoji icons, no card-in-card, no `transition: all`, no `100vw`.

## How it was checked

- `npm run check`: lint (strict type-aware), types, design check, 54 tests.
- Driven in a real browser against a stand-in service at 375px and desktop width, light and dark:
  no horizontal overflow, error / empty / "did you mean" / library states render, suggestions open
  only on the person's own typing.
- Not checked: screenshots reviewed by a designer, screen-reader testing beyond the Testing
  Library role queries, Safari.
