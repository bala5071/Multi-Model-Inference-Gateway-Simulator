# 55. The console's components and typed settings forms (UI3)

UI3 of `docs/52-console-ui-plan.md`, planned in its section 16 (the first commit of this PR) with
the two answers in 16.7. Presentation, structure and accessibility, plus one console write the
answers added (the organization's form). No route, permission or API change.

## The components (`frontend/src/ui/`)

Each is our own, on native elements, styled by the tokens through `src/styles/components.css`
(no style attribute in markup, no CSS-in-JS), with vitest tests for its roles, names and keys.

- **Button** by role: primary, secondary, danger, quiet; a loading state that keeps its label
  (hidden), so its width, and says "Saving..." (or the caller's words) to screen readers.
- **TextField, NumberField, SelectField, TextareaField**: a visible label; the hint and the error
  tied by `aria-describedby`; `aria-invalid`. A number's unit is written after it and describes
  it. The error shown is the gateway's answer; a field adds no rule.
- **Toggle**: a checkbox with the switch role. **Chips**: a list of values, each removable by its
  own button named after it, one added with Enter or Add. Nothing is judged on the page.
- **Tabs**: the WAI-ARIA tabs pattern (one tab in the Tab order, the arrows, Home, End). First
  used by Incidents' state filter, which was a select: the same states, the same query.
- **DataTable**: inside `TableScroll`, its header sticky; a column given a sort value is a header
  button with `aria-sort`, sorting **the current page only**, said under the table (Q9); empty,
  loading (skeleton rows, `aria-busy`) and error states. `sortRows` is pure and tested.
- **StatusPill**: a colour and the gateway's word, never the colour alone; `toneOf` picks only
  the colour. **Card**, **StatTiles/StatTile** (a description list).
- **Skeleton**: placeholder lines hidden from screen readers, which hear "Loading..."; no shimmer
  under reduced motion (the global rule stops it).
- **Tooltip**: `popover="manual"` in the top layer (no scroll box clips it), placed under its
  trigger through the CSSOM, never a style attribute (the CSP trial runs over it). Opens on focus
  and hover, stays while hovered, closes on Escape (WCAG 1.4.13). The trigger is a button, so it
  takes the focus; the words describe it. It replaces the dash's `title`. The browser's
  `[popover]` rule scrolls; the tooltip sets `overflow: visible` (axe's scrollable-region-focusable
  found it).
- **Dialog, ConfirmDialog**: the native `<dialog>` with `showModal()`. A confirmation names the
  thing and the consequence; Cancel comes first, so the focus starts there and the destructive
  button is never the default. Revoking a key and deleting a channel, the two actions that asked
  before, go through it; nothing that did not ask now asks. It closes before the action, so a
  step-up is its own dialog. The step-up dialog is native too.
- **Toasts** moved here, with a `failed` tone (`role="alert"`), which only repeats an error that is
  also beside its form.
- **Menu**: UI2's account menu logic as `useMenuButton`, the account menu its first user.

## The typed settings form (`src/console/EditForm.tsx`)

On both settings screens; it replaces the JSON table.

- **The kinds.** The gateway does not send a setting's kind, so `src/console/settingKinds.ts` keeps
  a copy of the names and kinds of `CATALOGUE` (`gateway/settings.py`), pinned to it by
  `tests/test_dashboard.py`. Only the kind: never a bound or a cap. A setting missing from the copy
  is drawn as a JSON field. A number's unit is read from its name (`_ms`, `tokens`, ...).
- **Controls:** a toggle for each `bool`, a number field with its unit for each `int`, chips for
  `tiers`, "all providers" or chips for `providers`, alias rows for `aliases`, a text field for
  `quality` (the gateway's words, not an enum copied here).
- Beside each setting, its **source and caps** (the application's `effective` answer; for the
  organization, what it set and the platform's cap). "Reset to the inherited value" (the gateway's
  `unset`) where this level set the value; Undo on a changed field.
- **Changed**: each change with its old and new value, in words, before saving. Then the gateway's
  dry run as before (valid, refused with its reason, or "saving asks for your password"). Save is
  disabled until the dry run says valid.
- **The Save bar** stays in view while anything is changed: the count, Discard, Save (which asks
  for the password first when the dry run says the change loosens where prompts go, as before).
- **Advanced**: a `<details>` holding the JSON table of before, on the same draft, so both views
  make one change. `settingsForm.ts` holds the pure part (tested).
- **Presets** as cards; **history** as a timeline with Revert to the previous version.

**Organization settings** (answer 16.7.1) get the same form on the organization's endpoints. It is
shown only when the gateway's dry run of no change (`POST .../settings/validate`, which needs
`org_settings.write`) answers yes; any refusal or doubt shows no form and no Revert. Saving and
reverting go through the step-up as the gateway asks (`org_settings.loosen_egress`). Note 47's "It
shows; it changes nothing" is superseded. No permission changed: the gateway allowed the write
before; the console now offers it.

**The key shown once** stays visible (answer 16.7.2): restyled, Copy and "I have stored it" as
before.

## Found on the way

- The phone's drawer closed only when the place changed, so a link to the place already shown
  left it open (the admin's landing is Applications). It now closes on any link it holds.

## Checks

- **CSP trial** over every converted screen (an organization admin and reader): the tooltip, the
  dialogs, the tabs; nothing blocked, nothing fetched from another origin.
- **Keyboard only** (e2e): the switch, a number, a chip added and removed, Discard, the tabs, a
  sort, a confirmation (the focus starts on Cancel, Escape gives it back), a tooltip.
- **axe** in both themes on each new state (the form changed, Advanced open, the organization's
  form, tabs, a sorted table, a confirmation, channels, a tooltip) and on **every role's screens at
  390 px**, which also checks no page is wider than the phone.
- **Contrast**: two new pairs, a danger button's label on its background and a toggle's thumb on
  its track, in both themes.
- **Visual regression**: the settings form, the organization's form, a confirmation, the incidents
  tabs, beside UI2's shots.

## Not built here

UI4: the Applications list, the application page and its tabs, then the monitoring.
