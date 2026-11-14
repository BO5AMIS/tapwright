# Agent tools: one interface for Tapline and Tapwright

Shared by Tapline and Tapwright. Version 1, agreed by both projects and approved by the user
on 2026-11-09. Version 1.1 (2026-11-09) adds the `not_actionable` reason `no_action`, also
agreed by both projects and approved by the user. Version 1.2 (2026-11-09) spells out the
exact answer and message wording that both tools found while building and cross-checking on a
real phone, also agreed by both projects and approved by the user. Version 1.3 (2026-11-09) adds
what the second cross-check found: duplicate folding, bidi marks, `ambiguous` wording, inputs'
actions, touching the visible part, and the cap note. Both projects agree on it, and the user approved it on 2026-11-09. Version 1.4 (2026-11-10) adds: closing the keyboard first, `covered` for what
an open sheet hides, numbering that carries through full lists, the code `not_answering`, and
`back` closing what is open on top. The user handed these decisions to the two projects on
2026-11-10, and both agree on it. Version 1.5 (2026-11-10) adds what agents new to the tools ran
into: the header counts what lies off screen, text is cut at 200 characters and a cut says how
much is missing, a match in the alert in front wins over matches behind it, a person's touch on
the phone leaves the same note as viewer input, `type_text` takes `force`, and a call outside the
apps an agent was given answers `not_allowed`. Version 1.6 (2026-11-10) adds what a second first-time agent ran into: `scroll` with
`to: "target"` brings an element onto the screen, changes off screen are shown, a note says when a
change block covers more than one action, a forced action says so, typing into the next field of
a form leaves the keyboard up, and both tools say `Keyboard: shown` and `Keyboard: hidden`. Version 1.7
(2026-11-11) adds `placeholder="…"` to the element line: an empty input shows its placeholder that way, never as
`value=`, which a first-time agent read as a field already filled in. Version 1.8 (2026-11-12) adds batches that are also scripts (`run`, `record`, `replay`), the accessibility audit (`audit`), the safety policy with a person's confirmation and dry runs, the action log, the rule for secure fields, and shorter answers (§13). Both projects agree on it under the plan the owner approved on 2026-11-12. Version 1.9 (2026-11-12) adds visual checks (§14): `compare_screen` in both tools with a pass bar (`threshold`) and `ignore`, the `matches` expectation and `compare_screen` steps in `run` and scripts, and references that travel with a script; Tapwright adds `filmstrip`. Both projects agree on it under the plan the owner chose on 2026-11-12. Version 1.10 (2026-11-12) adds diagnostics (§15): `logs` in both tools with `level`, `grep` and `limit`, the crash note and `crash`, `performance` with `under` limits in `run` and scripts, and failures that carry the app's recent logs. Both projects agree on it under the plan the owner chose on 2026-11-12. Version 1.11 (2026-11-12) adds fewer steps (§16): reads in the call log and `session_report`, actions that scroll to an off-screen target and `not_found` with the closest matches, language, region and text size without Settings, network conditions, and a calls-per-check bar against Android. Both projects agree on it under the plan the owner chose on 2026-11-12. Version 1.12 (2026-11-12) adds journeys and usability (§17): `journey` in a user's eyes with what it measures, personas as guidance, `ux_check` with optional house rules, stress moments, a visual report with its export, and `media` for photo pickers. Both projects agree on it under the plan the owner chose on 2026-11-12. Version 1.13 (2026-11-12) adds driving: `drive`, `map`, places, roads, routes and test accounts, reflexes, `brief` answers, `if` and `optional` steps, the thin call, a person's OK through the host's dialog, and seconds in `session_report`. Both projects agree on it under the plan the owner chose on 2026-11-12. Version 1.13.1 (2026-11-13) clarifies 1.13 where building it in both tools showed the text silent or unclear: list rows in `brief`, a change under the `compare_screen` threshold that is still named, `off_screen` after §16.2, the retry advice for an id, ASCII names and names that differ only in case, a double tap's second tap, the viewer's `confirm_closed`, the no-change note naming its action, what `allow` lifts in a journey, when reflexes are checked, some wording, the declined answer, and a road's `move`. Both leads agree on it. Version 1.13.2 (2026-11-13) corrects four places where building 1.13 showed the text wrong or too strong: what pictures show of a password, the viewer's `confirm_closed` when the chat dialog answers first, the passcode unlock marked as planned, and Tapwright's place names no longer taken from a title. Both leads agree on it. Each repo keeps an
identical copy in `docs/`. It changes only by
agreement between the two projects, like `VIEWER-PROTOCOL.md`. Each tool's
**Change** items below are its work to reach this interface.

An agent that has used one tool should be able to use the other without relearning anything:
the same tool names, parameters, answers and errors. The tools differ only where what they
reach differs. Tapline works inside one React Native app and reads its React tree. Tapwright
works across the whole iPhone and reads the accessibility tree through WebDriverAgent.

How it was made: a side-by-side reading of both tool surfaces (Tapline
`packages/host/src/tools.ts`, `format.ts`, `mcp.ts`; Tapwright `mcp_server.py`, `daemon.py`,
`ui.py`, `session.py`). For each difference I picked one behaviour and give the reason.
**Change** marks what a tool has to change.

## 1. Principles

1. **No guessing.** When a target is unclear, or an action can't land where a real touch
   would, the tool says so with a code and the candidates or the reason. It never picks one
   silently. This is the reliability bar: no wrong presses.
2. **Small answers by default.** Actions return what changed, as text. Full lists and
   screenshots come when asked or when the screen changed. Small and mid-size models drive,
   and the lead stays text-only.
3. **One meaning per name.** A parameter, code or word means the same thing in both tools.
4. **Where a tool can't do something,** it returns `unsupported` with the reason and the
   nearest thing it can do. It doesn't hide the tool.
5. **Points everywhere.** Positions and sizes are in points, origin at the top-left of the
   screen. Screenshot pixels are stated separately.

## 2. Targets

Every tool that acts on an element takes one `target` object with exactly one kind. Tapline's
nested form wins: the schema enforces "exactly one", and every tool uses the same object.
**Change Tapwright** (flat `ref`/`id`/`label`/`x`/`y`/`index` today).

| Kind | Matches | Notes |
|---|---|---|
| `{ref: "e3"}` | an element of the agent's last list | refused with `stale` if someone else changed the screen since that list (§4) |
| `{id: "login-button"}` | exact identifier | React Native's `testID` is the iOS accessibility identifier, so the same value works in both tools. **Change Tapline:** the key `testID` becomes `id` |
| `{label: "Log in"}` | the accessibility label, exact, ignoring case | **Change Tapwright:** no substring fallback here; that's `text`. Tapline: an element without a label of its own matches by its text, the way iOS forms its label (v1.6), so the same call finds a button that only shows "OK" in both tools |
| `{text: "Log in"}` | visible text: an exact match first, then a substring; Tapwright matches the label or the value | |
| `{x, y}` | what a touch at that point would hit, in points | Tapwright touches the screen there. Tapline can't touch, so it hit-tests: a point in the iOS tab bar's area is the tab under it, and with a React Native `Modal` open, the modal's content is tested first and what's behind it counts as covered. Later this goes through Fabric's hit test. For `toggle`, `type_text` and `scroll`, it's the element of the right kind under the point. When there's none: `not_actionable: no_action: Nothing at (x, y) takes <a tap, typing, a toggle, a scroll>.` |

- `index` (0-based, in reading order) picks among several matches. It applies to `id`,
  `label` and `text`, not to `ref` or `{x, y}`. **Change Tapline:** today `index` works
  only with `text`. **Change Tapwright:** today the default pick re-sorts the matches and a
  non-zero index doesn't.
- `id`, `label` and `text` are looked up on the live screen at call time, so they never go
  stale.
- **A nested pair:** when a `label` or `text` target matches both an element and another match
  that contains it, the one that takes the action wins (a button over its own text), as a
  finger's touch would. If both or neither take it, they stay `ambiguous`.
- **The layer in front (v1.5):** when several elements match an `id`, `label` or `text`
  target and some of them are in the alert that is in front, only those count: a person sees
  one "Cancel", not the one behind the alert too. `index` then picks among those. With no
  match in the alert nothing changes, so a target behind it still answers `behind_alert`.
  What an open sheet or popover hides isn't matched at all (it answers `covered`, §4).
- **Tapwright `id`:** WebDriverAgent mirrors the label into the identifier when an app sets
  none. A real identifier wins over a mirror.

## 3. Answers

Every answer is one text item, plus an image only when asked. A call that fails is an error
(§4).

### 3.1 After an action

```
Tapped e3 button "Log in" id=login-button.
Same screen. Changes:
+ e13 button "Done" @24,700 354x48
- text "Loading"
~ e5 text "3 items" (was "2 items")
↕ 7 elements moved
Settled in 0.31 s.
```

- **The first line** is the verb in the past tense, then the element's line **without its
  position, size or `[actions]`** (§13), then a full stop: `Tapped e3 button "Log in" id=login-button.`
- **Then the changes.** The block always starts with `Same screen. Changes:`.
- **Header lines that changed come first:** `Alert: "<title>" [Button] …` when an OS or app
  alert appears or changes, `Alert: gone` when it closes, `Keyboard: shown` or
  `Keyboard: hidden` when the keyboard came or went, and `Network: N requests in flight` (Tapline). `Note:`
  lines stay at the end of the answer. Then:
  - `+ <element line>` for a new element, with its ref and `[actions]`;
  - `- <role> "<text>"` for a gone element, with no ref and no position. Without text it's
    `- <role> id=<id>`, and without that, `- <role>`;
  - `~ <element line> (was "<old>")` when exactly one field changed. When several changed:
    `(was text="…", value="…")`, naming each one. The fields are text, label, value,
    checked, selected, disabled and actions. An input that showed its placeholder and now
    holds a value reads `(was placeholder "<old>")` (v1.7). **A frame never appears in `~`**;
  - `~ <element line without a ref, position or [actions]> (off screen; was "<old>")` (§13) for an element that lies above
    or below the screen, before and now, whose text, label, value, checked, selected or
    disabled changed (v1.6): the effect of a tap that is scrolled out of view. Off-screen
    elements that are new, gone or moved aren't listed (a scroll moves all of them). Up to 5
    are listed, then `~ N more changed off screen`. What an open sheet hides is left out.
    An element that was on the agent's list and has since scrolled off screen counts too.
    An element off screen counts as the same element when it has the same identifier, or the
    same role in the same place; a changed text with neither isn't shown (a list that reuses
    its cells hands an off-screen cell to another row, and that is no change);
  - `↕ <element line>` for a frame-only change of more than 1 pt in x, y, width or height,
    size included. Up to 5 are listed; more become the single line `↕ N elements moved`;
  - `No visible change.` when nothing changed, on screen or (v1.6) off it.
- **The baseline** is the agent's list as of its last answer: the last `look`, or the list
  carried through every action answer since. It's never just the last full list. Scroll
  down and then back up gives `↕` both times.
  - **When the block covers more than this action (v1.6).** An answer without a list (an
    action, a `wait` or a `wait_for` with `look: false`) leaves the baseline where it was, so
    the next change block covers that action too: a sheet opened by one tap and closed by the
    next reads `No visible change.`. That answer then ends
    `Note: changes are since your last list, 2 actions ago.` The count is the answers without a
    list since the last list or change block, plus this one. An answer that carries the full
    list has no such note.
- **The change block is a literal diff,** including the element just acted on: typing
  "eggs" into a field that held "egg" shows `~ … value="eggs" … (was "egg")`. Typing the
  same text again is `No visible change.`.
- **A tap that changes nothing (v1.13).** After `No visible change.` from a `tap`, `long_press` or `double_tap`, the
  answer adds one note: `Note: the tap was delivered, and nothing on screen changed within <N> s.` `<N>` is how long
  the screen was watched, to one decimal: at least 0.3 s in Tapwright, at least 0.5 s in Tapline.
  - The note names the action (1.13.1): `the tap was delivered…`, `the long press was delivered…`, `the double tap was
    delivered…`.
  - Each tool may add one clause after it.
  - Tapwright: a hint by what was touched (a text field: `type_text` types into it; a point with no element; else
    wait for what you expect, then try once more, then report it).
  - Tapline: `onPress ran and returned`, `onPress threw <message>`, `it has no handler of its own; its parent took
    it`, or `a request it started is still running`.
  - Tapwright ships this in 1.5.1, ahead of 1.13, with this wording.
- **The full list replaces the changes** when:
  - the screen or route changed;
  - more than max(8, half the elements) changed;
  - everything vanished. That case shows the empty-screen line (§3.2) and
    `Note: something outside the app (a system sheet or browser) may cover it.` A tool may
    add what to do next; Tapline adds `Tapline can't see it: ask the user, or use Tapwright.`

  The full list starts with the header (§3.2).
  - **Off-screen changes follow the full list too (v1.6):** the line `Changes off screen:`, then
    the same `~ … (off screen; was "…")` lines, after the list and before `Settled in`. Only when the list came because most of the
    same screen changed. Not when the screen or route changed, another app is in front, a
    person acted, the link dropped, or the agent had never looked: there the old off-screen
    elements are no baseline to compare with.
- **The last line** gives how long the tool waited for the screen to settle:
  `Settled in 0.31 s.` (§6). `wait` answers `Waited 1.0 s.` and the change block instead,
  with no settle line.
- `look: false` returns only the first line. That's the fastest form, for scripted steps.
- **No screenshot after actions by default.** `screenshot: true` on any action adds one, at
  `look`'s defaults.
- **Change Tapwright:**
  - today it returns the full list plus a 400 px JPEG after every action;
  - it needs element matching across lists to produce changes and keep refs (§3.4). It has
    no React identity, so it matches on role, id or label, and position.

### 3.2 Header

```
Screen 430x932 pt @3x · 26 elements (+41 below, +2 above)
App: Settings (com.apple.Preferences)
Route: Home › Profile (id="42")
Alert: "Allow notifications?" [Don't Allow] [Allow]
Keyboard: shown
Network: 2 requests in flight
```

- The first line is always there. The others appear only when they apply and the tool knows
  them.
- **What lies off screen (v1.5).** After the count of listed elements, the first line counts
  the elements that lie wholly below and wholly above the screen: `(+41 below, +2 above)`,
  `(+41 below)` or `(+2 above)`; nothing when there are none. Below comes first. Left and
  right aren't counted. It counts what exists now: a long list that makes its rows as they
  scroll into view has more below than the count says. Scroll to reach them. What an open
  sheet or popover hides isn't counted: scrolling can't reach it until the sheet is closed.
- `Route:` and `Network:` are Tapline only.
- `Keyboard:` comes from both (Tapline since v1.6). Its keys aren't listed; type with `type_text`.
- `Alert:` comes from both. Tapwright can press its buttons with `alert`; Tapline can't press
  OS alerts and says so.
- An empty screen shows `(nothing visible; the app may still be loading)`.
- No session ids in the header. `apps` lists them, for `open_app`.

### 3.3 Element line

`<ref> <role> "<text or label>" [label="…"] [id=…] [value="…"] [placeholder="…"] [disabled] [selected] [checked=true|false] @x,y WxH [actions]`

- **The quoted part** is the element's **own** text, or its label when there's no text.
  - It's never its descendants' text. An accessible element (a button or a text, i.e. what
    VoiceOver reads as one item) carries the text inside it, the way iOS forms its label, so
    a pressable wrapping "Log in" is `button "Log in"`.
  - A container that isn't accessible (a scroll view, a plain view) quotes nothing:
    `e1 scroll @0,0 430x778 [scroll]`.
  - Text is cut at 200 characters, with whitespace flattened, JSON-quoted. A cut ends with
    how many characters are missing: `"…the first 200…(+57)"` (v1.5; it was 80, with a bare
    `…`). Labels and values are cut the same way. `wait_for` and `{text}` targets check the
    whole text, also the part the line doesn't show.
- **`label="…"`** is added only when the label differs from the text, ignoring case and
  whitespace. On Tapwright the label *is* the text, so it never appears.
- **Duplicates:** when an element and its nearest listed ancestor have the same quoted text
  and the same rect (within 1 pt), only one is listed. It's the one that takes an action;
  if both or neither do, the ancestor. That holds even when their roles differ: a labelled
  wrapper around an input lists only `input` (WebDriverAgent shows it that way too).
- **Invisible direction marks** (U+200E, U+200F, U+202A–U+202E, U+2066–U+2069) are removed
  from text, label and value, before matching and before display.
- **`id=` is always shown when present.** **Change Tapline:** `testID=` becomes `id=`.
- **`value=` is shown when not empty.** **Change Tapwright:** today it's shown only when it
  differs from the label.
- **`placeholder="…"`** is shown for an input that is empty and has a placeholder, instead of
  `value=` (v1.7). An input shows `value=` or `placeholder=`, never both. On Tapwright,
  WebDriverAgent reports an empty field's placeholder as its value; Tapwright tells them apart
  by the placeholder attribute.
- **Roles are lowercase, from one vocabulary.** **Change Tapwright:** today it shows
  capitalised XCUIElementType names. **Change Tapline:** `textfield` becomes `input`, and
  `scrollview` becomes `scroll`. Tapline uses the React Native `accessibilityRole` or `role`
  when one is set, otherwise the column below:

| Role | Tapwright (XCUIElementType) | Tapline (React Native) |
|---|---|---|
| `button` | Button | role button, imagebutton or togglebutton; anything pressable without a role |
| `link` | Link | role link |
| `text` | StaticText | Text; role text or summary |
| `header` | StaticText with the `Header` trait | role header |
| `input` | TextField, SecureTextField, SearchField, TextView | TextInput; role search or textbox |
| `switch` | Switch, Toggle | Switch; role switch |
| `slider` | Slider | Slider; role adjustable |
| `image` | Image, Icon | Image; role image or img |
| `tab` | Tab, a Button inside a TabBar | role tab, and native tabs |
| `cell` | Cell. WebDriverAgent gives cells no label, so a cell quotes nothing and its texts follow as `text`; a tap on them lands on the cell | none: list rows come out as `button` or `text` |
| `scroll` | ScrollView, Table, CollectionView | ScrollView, FlatList, SectionList, VirtualizedList |
| `alert` | none in the list: OS alerts are the `Alert:` header line, and their buttons stay in the list as `button`, tappable | an in-app element with role alert stays in the list (it's app content); OS alerts are the `Alert:` header line |
| `other` | Other | a container with no role (View and the like) |
| anything else | the type in lowercase | the role in lowercase (radio, checkbox, progressbar, menu, menuitem, tablist, timer, toolbar …), otherwise `other` |

- **`[actions]`** lists what the agent can do with the element: `tap`, `longPress`, `type`,
  `toggle`, `scroll`. Tapline reads them from the handlers. Tapwright infers them from the
  role: `button`, `link`, `cell` and `tab` get `tap`; `input` gets `tap, type`; `switch`
  gets `toggle`; `scroll` gets `scroll`. An input always lists `tap, type` on both tools,
  because a tap focuses it, as a finger's does.
- **Selection state:** `selected` and `checked=` come where the tool knows them. Tapwright
  reads them from the value or the selected flag.
- **The list holds up to 200 elements,** then `(+N more below; scroll)`.

- A secure (password) field's value is never shown as text. Its line shows one `•` per character
  (`value="••••••"`), which is what iOS itself reports. `type_text` into it answers
  `Typed into <line>. The value is hidden (secure field).` The action log (§12) never holds it either.

### 3.4 Refs

`e1`, `e2`… in reading order.
- `look` numbers from `e1`.
- After an action, elements still on screen keep their refs, and new ones get new numbers,
  counting on from the highest ref given since the last look. That holds for a full list in an
  action's answer too (v1.4), so a ref the agent still remembers never lands on another element:
  it answers `not_found`.
- After another app came to the front, after a person acted, or after the link dropped, nothing
  is carried: every element gets a new number.
- **Change Tapwright:** today it renumbers after every action.

### 3.5 Screenshots

- `look` takes:
  - `screenshot` (default true);
  - `width` (160–1320 px, default 600);
  - `labels` (draw each actionable element's box and ref).
- Format: JPEG at quality about 0.75. The answer says the size in pixels.
- **A file mode** for tiered agents is a server option. The answer gives
  `Screenshot: <path> (WxH)`, keeps the newest 50, and sends no image. The lead never has to
  load images.
- **Change Tapwright:** today it has a fixed 400 px and no `labels` or file mode.

### 3.6 Notes

Plain lines at the end, starting with `Note: `, for example:
- `Note: a person used the phone since your last look.`
- `Note: no screenshot: the app doesn't support it.`

**A person's touch (v1.5).** The note about a person is given once, in the next answer, after
input from the live viewer and also after a finger on the phone itself; `{ref}` targets are
`stale` until the next full list either way. Tapline sees a touch inside its app (not one inside
a `Modal`'s own window, and not one on what iOS draws) and says "the app".

**A touch a notification took (Tapwright only).** A notification banner that comes down at the moment
of a real touch near the top of the screen can take that touch and bring another app to the front.
Tapwright then opens the app again and says so, for example: `Note: Mail (com.apple.mobilemail) came to
the front after the touch: the app opened it, or a notification banner over the top of the screen took
the touch. Went back to Example (com.example.app). If the app was meant to open it, that worked; if not,
look, then try again.` For the home screen, Notification Center or Control Center (a drag from the top
edge pulls them down) the note says the touch may not have reached the app. A permission prompt the touch
raised, or a view from another process over the app (Sign in with Apple, Apple Pay) doesn't count; when
another app comes to the front a moment later, the agent's next call says so (an action then answers
`stale` and does nothing). Tapline's touches go through the app's own touch system, so a
banner can't take them; when its app leaves the front for any reason, the next call answers
`in_background`, and calls work again once the app is back in front. Either way the agent is told.

**Change both:** Tapline puts notes in parentheses today, and Tapwright sends them as separate
text items.

## 4. Errors

- An error is MCP `isError: true` with one text item: `Error <code>: <message>`. The message
  says what to do next. `ambiguous` reads, exactly:
  ```
  Error ambiguous: 2 elements match text "Nutrition".
  Candidates:
    e32 text "Nutrition" @76,730 263x23
    e46 tab "Nutrition"
  Retry with one of the refs, or add "index" (0-based).
  ```
  Up to 5 candidates are listed, each indented two spaces. If there are more, one further
  indented line follows: `  (+N more; use a ref or "index")`.
- For `not_actionable`, the reason is the message's first word, so scripts can read it
  without a new field: `disabled`, `off_screen`, `covered`, `under_tab_bar`,
  `behind_alert` or `no_action`. For example:
  `Error not_actionable: covered: e47 button "Save" is covered by e45 button "Cancel"; close it or pass force: true.`
- **`off_screen` after §16.2 (1.13.1).** The scroll advice for `off_screen` in the next bullet is superseded by §16.2:
  `tap`, `long_press`, `toggle` and `type_text` scroll an off-screen target into view themselves. `off_screen` now means
  that no scroll can bring the element on screen. The message says what to do then: it lies in a part of the screen that
  nothing scrolls (behind a fixed bar, or outside every list), so look at the screen, then target something else or
  report it.
- **Where scrolling is the fix (v1.6),** the message names the one call that does it, in these
  words: `(scroll with this target and to: "target")`. That is `off_screen`, `under_tab_bar`,
  and `covered` by a bar. Tapline:
  `… is off screen (at 16,-1903; the screen is 430x932). Scroll to it first (scroll with this target and to: "target"), or pass force: true to call its handler anyway.`
  Tapwright, whose touch can't reach what is off screen even with `force`:
  `…; scroll to it first (scroll with this target and to: "target").`
- **A forced action says so (v1.6).** When `force: true` let through an action the checks would
  have refused, the answer ends `Note: forced (<reason>): …` with the reason word
  (`covered`, `off_screen`, `under_tab_bar`, `behind_alert`), then the tool's own sentence,
  because `force` means different things:
  - Tapline calls the element's handler: `Note: forced (covered): a person couldn't have done this.`
  - Tapwright touches the element's centre: `Note: forced (covered): the touch may have landed on what is in front.`
  A forced action the checks would have allowed has no note.
- **Touch the visible part.** Before refusing with `covered` or `under_tab_bar`, a tool clips
  the element to the screen and to what's outside the bars and covers drawn over it. If the part
  left is at least 10 pt high and 10 pt wide, the tool touches its centre (Tapline hit-tests
  it), as a person would. Example: a card at y 808–898 with the tab bar from 849 is tapped at
  y ≈ 828. The tool refuses only when that part is smaller. `force: true` acts at the
  element's own centre.
- **Behind an open sheet (v1.4).** A target that matches no listed element but matches one that
  an open sheet, modal, popover or other layer hides answers:
  `Error not_actionable: covered: <element line, no ref> is behind <line of what is in front>; close that first.`
  - "What is in front" is the line, with its ref, of the first element the layer lists that has
    a label or an id. For example:
    `covered: button "Save" id=lab-save @16,-40 193x49 is behind e1 other "dismiss popup" id=PopoverDismissRegion @0,0 430x932; close that first.`
  - `force` can't help: the element isn't on the list.
  - For a target that isn't listed, the reasons are tried in this order: `behind_alert`, this
    one, `off_screen`, then `not_found`. A covering sheet comes before `off_screen`, because
    scrolling can't help until it is closed.
- **Closing the keyboard first (v1.4).** When the keyboard is up and the target of a touch (`tap`,
  `long_press`, `toggle`, or `type_text`'s focusing tap on another input of which less than 10 pt show above the keyboard)
  lies outside the focused input, the tool closes the keyboard first, without pressing return, and
  the answer ends with `Note: closed the keyboard first.` A person's first tap outside the field
  often only closes the keyboard, and the keyboard covers controls near the bottom; the tool does
  both steps in one and says so. For another input with at least 10 pt above the keyboard, nothing is
  closed (v1.6): focus moves to it, as a person's tap on the part that shows moves it, and there
  is no note. If the focus doesn't
  move, the keyboard is closed the old way. With a hardware keyboard connected no keyboard shows,
  and nothing is closed. Nothing is
  closed for a call the tool refuses, or while a system alert is in front. The note is given only
  when the keyboard is still closed as the call answers: Tapwright's `type_text` into a covered
  input focuses it after closing, so the keyboard is up again and it leaves the note out; Tapline's
  `type_text` sets the value without focusing, so it keeps the note.
- `no_action` means the element, or the screen for `back`, doesn't take this action, and
  `force` doesn't help. Examples:
  - `type_text` into something that isn't an input;
  - `toggle` on something that isn't a switch;
  - `scroll` outside any scroll view;
  - a long press on a tab;
  - `back` with nothing to go back to.

  For `tap` and `long_press`, it applies only when nothing under the touch would respond: not
  the element and not its ancestors. A tap on a label inside a tappable row goes to the row, as
  a finger's would, so it is no `no_action` (R2: no false refusals). Tapwright's taps are real
  touches, so Tapwright never answers `no_action` to a tap.
- **Change Tapwright:** today it never sets `isError`, prefixes `<code>:`, `error:` or
  `not found:`, and treats "not found" as a normal answer.
- **Message wording.** Elements are named by their element line, or for gone ones by
  `<role> "<text>"`, never by component or class names.
  - **`not_found`:**
    - id: `nothing on screen has id "x"; look, then pick another target.`
    - label: `nothing on screen has label "x"; use {text} for a partial match, or look.`
    - text: `nothing on screen shows "x"; look, then pick another target.`
    - ref: `e12 isn't on screen any more; look again.`
  - **Typing without a target:**
    - no input: `Error not_found: No input on screen`;
    - several: `Error ambiguous: <n> inputs on screen; pass a target to pick one:`, then
      `Candidates:`. A tool that knows focus may add "and none is focused". Here the block's last
      line is `Retry with one of the refs as the target.`, since there is no target for an
      `index` to count in.
  - **`no_action` reasons:** `has no tap action`, `has no long-press action`,
    `is not a switch`, `is not an input`, `is not inside a scroll view`. At a point:
    `Nothing at (x, y) takes <a tap, typing, a toggle, a scroll>.`

- New code **`expect_failed`**: a step's expectation in `run` (§9) didn't hold. What the agent does: look,
  then fix the step or report a bug. Raised by both.
- New code **`off_route`** (v1.13): `drive` or a route step stopped because the screen isn't where the road leads
  (§18.4). Nothing else was touched. What the agent does: go on by hand from the screen the answer shows; what it
  does is remembered. Raised by both, only from `drive` and route steps.
- `stale` also covers a touch by position (Tapwright): `Error stale: the screen around (x, y) changed since your
  last look; look again, or pass force: true to touch there anyway.` It compares the picture within 40 pt of the
  point with the moment of the agent's last list; after an answer without a list (`look: false`) it isn't
  checked. Tapline may adopt it where it has pictures.
- `not_allowed` gets two more reason words (§11): `needs_confirmation` (a person must OK this touch first)
  and `blocked` (the policy forbids it; don't look for another way).

| Code | Meaning | What the agent does | Raised by |
|---|---|---|---|
| `not_found` | nothing matches the target, or the ref isn't on the last list | look, then pick another target | both |
| `ambiguous` | several matches and no `index` | use one of the candidates' refs, or add `index` | both. **Change Tapwright:** it picks silently today |
| `not_actionable` | the element can't take the action as a real touch would: `disabled`, `off_screen`, `covered` (by what), `under_tab_bar`, `behind_alert`; or it doesn't take that action at all: `no_action` | fix the cause (`scroll` with `to: "target"`, close the alert; for `off_screen` see §16.2: when no scroll can bring it on screen, look, then target something else or report it), or pass `force: true`; for `no_action`, pick another element or tool | both. Tapwright today: none. Plan: `disabled`, `behind_alert` and `no_action` first; `covered` is a gap |
| `stale` | a `ref` from a list made before a person acted, from the viewer or (v1.5) with a finger on the phone | look again | both. **Change Tapline:** the host remembers "a person acted since the agent's last list". A look, or an action answer carrying a full list, clears it, and the next answer gets `Note: a person used the app since your last look.` (Tapwright says "the phone"). `id`, `label` and `text` targets keep working |
| `busy` | a person drove for the whole ~30 s wait | wait a few seconds, retry | both (VIEWER-PROTOCOL "Control") |
| `paused` | the user paused the agent | ask the user to resume | both |
| `locked` | the phone is locked | ask the user to unlock it | Tapwright |
| `in_background` | the app is in the background, and only the person can bring it back | ask the user to open the app | Tapline |
| `not_answering` | the app in front stopped answering (v1.4) | close the app and open it again (`close_app`, then `open_app`, where the tool has them), or ask the user to | both. Tapwright: the app doesn't answer accessibility queries; `close_app` still closes it. Tapline: the app is connected but didn't answer a call, or sent nothing for 45 s, while not in the background |
| `reconnecting` | the link dropped and is coming back | wait a few seconds, then look | both. Tapline's `disconnected` maps here |
| `not_connected` | nothing to drive: no app connected, or no phone found | follow the setup steps in the message | both. Replaces Tapline's `no_app` and `unknown_session` |
| `timeout` | no answer in time; the action may have happened | look before retrying | both. Tapwright adds two messages for a phone that gave one request no answer while the line is alive (over Wi-Fi a single request can stall). After a touch: `the phone didn't answer within <N> s; the action may have happened: look before you retry.`, after the touch's own length plus 10 s; the session stays. From `wait_for`, when not one read of the screen came back in its time, or in 3 s if that is longer: `The phone didn't answer within <N> s, so it isn't known whether <kind> "<value>" is on screen; look, then wait again.` It says "it didn't happen" only after it has read the screen at least once |
| `expect_failed` | an expectation of a `run` step didn't hold (§9.2) | look, then fix the step or report the bug | both |
| `off_route` | `drive` or a route step stopped because the screen isn't where the road leads; nothing else was touched (v1.13, §18.4) | go on by hand from the screen shown; what you do is remembered | both |
| `bad_params` | the call's arguments are invalid | fix the call | both. **Change Tapwright:** it uses `unsupported` for this today. (v1.11) An argument the tool doesn't take is never ignored: `Error bad_params: audit doesn't take labels, ocr; it takes types.` (a tool with none: `…; it takes no arguments.`), on every path (MCP, HTTP, `run` steps, where a step's message starts `step 3: `) |
| `unsupported` | this tool, app or phone can't do it | use the alternative named in the message | both |
| `not_allowed` | a read or a touch outside the apps the agent was given (v1.5). The message's first word is the reason: `outside_apps` (another app is in front, or the call names one), `no_apps` (the agent was given none) | for `outside_apps`, go back to a given app with `open_app`; for `no_apps`, tell the user | Tapwright, in personal mode (it was `unsupported` with the same reason words). Tapline drives one app and never answers it |
| `internal` | a bug | report it | both |

## 5. Tools

### 5.1 Shared core: both tools offer all of these

| Tool | Parameters | Notes |
|---|---|---|
| `look` | `screenshot`, `width`, `labels` | the full list (§3) |
| `tap` | `target`, `force`, `look`, `screenshot` | **Change Tapline:** `press` becomes `tap`, to match the viewer protocol, Appium and Maestro, and "what a real tap would do" |
| `long_press` | `target`, `seconds` (default 1.0), `force`, `look`, `screenshot` | Tapline ignores `seconds` and calls the handler |
| `type_text` | `text`, `target`, `replace` (default **true**), `submit`, `force`, `look`, `screenshot` | answers `Typed into <el>. Value is now "…"`. Without a target: the focused field, else the only field, else `ambiguous`. **Change Tapwright:** `replace` defaults to false today, and it doesn't echo the value |
| `scroll` | `direction` (default `down`), `amount` (0.1–1.0 of the scroll area, default 0.8), `to` (`start`, `end` or `target`), `target`, `look`, `screenshot` | "down shows what's below". **`to: "target"` (v1.6)** scrolls the scroll views that hold the `target` element until its whole rect is on screen and clear of the bars, 8 pt inside the edge it came in over (an element larger than that space is brought to its start). It answers `Scrolled <scroll view line> to <element line>.` and the changes; when it stops before the element is clear, the first line says `toward` in place of `to`, so it never reads like success. An element that is clear already: nothing is scrolled, the first line is `<element line> is already on screen.`, and the change block follows. Likewise `to: "start"` or `"end"` with the scroll view already there: nothing is dragged, and the first line is `<scroll view line> is already at its start.` (or `end`), with the usual answer after it. Without a target, with a point as target, or with a `direction`, it is `bad_params`. When it stops before the element is clear: `Note: it doesn't scroll further: the target is as far into view as it gets.` (the end of the scroll view), or for Tapwright's drags `Note: the target wasn't reached in 12 s; scroll again to go on.` (Superseded in 1.13.1 by §16.2: `tap`, `long_press`, `toggle` and `type_text` scroll an off-screen target into view themselves.) A row a lazy list hasn't made yet is `not_found`. **Change Tapline:** `amount` is in points today. **Change Tapwright:** `amount` is a share of the screen today; it adds `target` (scroll inside that element) and `to` (repeat until nothing changes) |
| `toggle` | `target`, `value`, `force`, `look`, `screenshot` | **Change Tapwright:** new. It reads the switch's value and taps only if it differs |
| `back` | `look`, `screenshot` | **Change Tapwright:** new. It taps the navigation bar's back button, else swipes from the left edge, else `unsupported`. When a sheet, modal or popover is open on top, `back` closes that first, as "up one screen", and its first line is `Closed the sheet.` (v1.4). Tapwright taps outside a popover, on its dismiss region; Tapline calls the top Modal's `onRequestClose`. A sheet without one that closes on a tap on its backdrop gets that tap from both tools (v1.6); one with neither answers `no_action`, and the message names the sheet |
| `open_url` | `url`, `look`, `screenshot` | Tapline opens it inside the app; Tapwright opens it on the phone, so another app may take it (the answer says which app came up) |
| `wait` | `seconds` (0.1–30, required), `look`, `screenshot` | answers `Waited 1.0 s.` and the change block, with no settle line. Out-of-range values are `bad_params`. **Change Tapwright:** today it clamps silently and always takes a screenshot |
| `wait_for` | one of `text`, `id`, `label`; `gone`; `network_idle`; `timeout` (0.5–30 s, default 10); `look`, `screenshot` | `Done after 1.2 s.` plus the changes, or `timeout` (`Waited <N> s for <kind> "<value>" to appear; it didn't happen.`, or to disappear; Tapwright has a second message, see `timeout` in §4). **Change Tapwright:** new. It polls the screen, its looks are slow (see QUALITY-BAR), and `network_idle` is `unsupported` |
| `apps` | none | the apps the agent can drive, with the current one marked. Tapline: the connected apps. Tapwright: the installed apps. **Change Tapwright:** `list_apps` becomes `apps` |
| `open_app` | `app` (id or name) | Shared description: "Drive this app from now on." Tapline selects that connected app (today's `select_app`). Tapline can't launch apps, so when the app isn't connected it answers `not_connected` with how to connect it, and the description says so. Tapwright launches the app or brings it to the front (today's `launch_app`). An exact id wins (Tapline: a session id; Tapwright: a bundle id). Otherwise names match ignoring case. Several matches give `Error ambiguous: N connected apps match "X". Pick one by its session id:` (Tapwright: `installed apps` and `bundle id`), followed by their `apps` lines |
| `viewer` | none | the live view's link. **Change Tapwright:** new; today only `tapwright view` gives it |
| `run` | `steps` (1–50, required), `trace`, `dry_run`, `look`, `screenshot` | §9 |
| `audit` | `types` | §10 |

### 5.2 Extras: only where a tool can do them, same conventions

| Tool | Where | Why only there |
|---|---|---|
| `close_app` (today's `terminate_app`) | Tapwright | Tapline lives inside the app |
| `alert` (`accept`, `dismiss`, `text`) | Tapwright | Tapline can't reach OS alerts |
| `press_button` (`home`, `volumeUp`, `volumeDown`) | Tapwright | hardware buttons are outside the app |
| `rotate` (`portrait`, `landscape_left`, `landscape_right`) | Tapwright | turning the phone is outside the app. Both tools follow a turn, whoever makes it |
| `swipe` (`x0, y0, x1, y1, seconds`) | both (Tapline since v1.6) | Tapwright makes a real drag. Tapline drags through React Native's own touch system (a pan view, a sheet's swipe to close); a gesture that react-native-gesture-handler or a native view handles (sliders, maps), or a drag along a native scroll view, answers `no_action` there and its message names what can make it |
| `check_layout` | Tapline now; Tapwright later, partly | needs layout data. Tapwright can check tap-target sizes, overlaps and contrast from the accessibility tree and the screenshot, but not truncation |
| `compare_screen`, `filmstrip` | both (Tapwright since v1.9) | image work: §14 |
| `record`, `replay` | both (v1.8) | scripts by id, label or text work for both (§9.4) |
| `journey`, `ux_check`, `media` | both (v1.12) | §17. What each tool can do inside them differs, and the answers say so |
| `session_report`, `network` | both (v1.11) | §16. Tapline's `network` is an `app_command` |
| `logs`, `crash`, `performance` | both (v1.10) | diagnostics: §15. What each tool reads differs, and the answers say so |
| `drive`, `map` | both (v1.13) | §18. Places are route keys in Tapline and signatures in Tapwright, and each tool drives only the roads it can |
| `reload`, `app_command`, `pair` | Tapline | they need the app's JS runtime |

`dry_run` (§11.4) is a parameter of every touch tool (`tap`, `long_press`, `toggle`, `type_text`, `swipe`,
`scroll`, `back`) and of `run` (v1.8).

Tools that are only in one tool must name the alternative in their `unsupported` message.
For example, Tapline: `Error unsupported: Tapline can't press OS alerts; ask the user, or use
Tapwright.`

## 6. Timing

- **Settle:** after an action, wait until the screen is still, then answer. Today both use
  fixed sleeps: Tapline 350 ms on the host plus 30 ms in the app; Tapwright 0.6 s, 2.0 s
  after a launch and 1.5 s after `open_url`. The rule: the screen counts as still after
  100 ms without a change, with a cap of 2 s. Answers report `Settled in N s`, and
  QUALITY-BAR.md sets the targets.
  - **Tapline** watches React commits inside the app, in the same request, so it costs no
    screenshots and no extra round trip. Native-driver animations don't commit, so the tree
    can be still while pixels move. That's fine for an element list.
  - **Tapwright** compares the stream's frames on the PC. When the picture didn't change, it
    can answer `No visible change.` without reading the tree again.
- **The cap:** when the settle ends at the tool's cap with the screen still changing, the
  answer ends with `Note: the screen was still changing after <cap> s (an animation or video); positions may be mid-change.`
  `<cap>` is the tool's own cap: 2 s on both, and 4 s on Tapwright after a launch or
  `open_url`.
- **Timeouts:** each tool answers within its own limit or returns `timeout`, never hangs.
  The numbers belong in QUALITY-BAR.md, including on 3G.
- **A person driving:** agents wait, as VIEWER-PROTOCOL "Control" says. They wait without an
  error up to about 30 s, then get `busy`.

## 7. Descriptions and instructions

- Shared tools carry the same description text in both tools, word for word, so agents read
  the same guidance. Tool-specific sentences go at the end.
- The MCP `instructions` share one core text: the workflow, targets, codes and safety rules.
  Each tool adds a tool-specific part.
- Read-only tools set `readOnlyHint`: `look`, `apps`, `viewer`, `wait`, `wait_for`, `audit`, `logs`,
  `check_layout`, `compare_screen`.

## 8. Agreed with the Tapline agent (2026-11-09)

Tapline accepts every **Change Tapline** item:
- `id` in place of `testID`, with no alias, since nothing is published;
- `open_app` in place of `select_app`;
- `stale` after a person acts;
- the settle rule, using React commits;
- the role vocabulary;
- `scroll.amount` as a share of the scroll area: the app's wire method keeps points and
  converts.

Tapline also moves its JPEG quality to 0.75, and its session ids out of the header into
`apps`.

Today, Tapline's refs follow the React element itself, and a gone element is `not_found`. A
person driving makes the agent wait for 3 s of idle and return `busy` after 30 s. A pause
returns `paused` at once. That already matches VIEWER-PROTOCOL "Control".

Still to check, by Tapwright: whether WebDriverAgent shows the header trait (§3.3), and what
checking `covered` costs (`hittable`, §4).

## 9. Batches and scripts

### 9.1 A step

A step is one tool call written as an object: `{"tool": "<name>", ...its parameters, "expect": {...}}`.
Allowed tools: `tap`, `long_press`, `type_text`, `scroll`, `toggle`, `back`, `swipe`, `open_url`, `wait`,
`wait_for`, `compare_screen` (a check, §14.2), `performance` (v1.10, §15.3), `network` (v1.11, §16.4), and where the tool has them `alert`,
`press_button`, `rotate`, `open_app`, `close_app`, `app_command`. `drive` (v1.13, §18.4) is allowed as a step;
it counts as one step, however many moves it makes. Not
allowed (`bad_params`): `look`, `run`, `audit`, `apps`, `viewer`, `record`, `replay`, `filmstrip`, `logs`, `crash`, `session_report`, `journey`, `ux_check`, `media`, `map`,
and where the tool has them `inspect` and `check_layout`. A step's own `look` and
`screenshot` are ignored; the batch answers once.

Targets are resolved when the step runs, against the screen as it is then. Refs (`{ref}`) are allowed in
`run` (they resolve against the latest list, which every step keeps current, §3.4) but not in a saved script
(§9.4).

**Conditional steps (v1.13).** A step may carry:
- `if`: one of `text`, `id`, `label` (as in `expect`, §9.2), with optional `gone: true`. The step runs only when that
  is on screen, or, with `gone`, only when it isn't.
- `optional: true`: the step is skipped when its own target isn't on screen.

Both are one check, made once right after the step before has settled, with no wait of their own; `wait_for` is the
step that waits. "On screen" here means in the element list, so an element below the fold counts as there, and a step
that then runs scrolls to it as any step does (§16.2). `if` matches as `wait_for` does: the same text, id and label
rules, including its check of the whole text, so the two never disagree about one screen. A skipped step's `expect`
isn't checked. `if` and `optional` work in `run`, `replay` and routes (§18.5).

### 9.2 `expect`

Optional on every step. One of `text`, `id`, `label` (same matching as `wait_for`), with optional:
- `gone: true`: it must not be on screen;
- `value`: with `id` or `label`, the element's value must equal it (a switch: `"on"` or `"off"`; `"true"` and `"false"` also work, as its line shows `checked=`);
- `timeout`: seconds, 0.5–30, default **2**.

Or (v1.9) `matches`: the name of a reference picture; the screen must match it within `threshold` (percent,
default 0.5, with optional `ignore`), as `compare_screen` checks it (§14). It may stand alone or beside `text`, `id`
or `label`; then both must hold. It is checked once, after the step settles and after any text, id or label
expectation held.

Or (v1.10), on a `performance` step only, `under`: limits the measurement must keep (§15.3).

After the step's action settles, the tool waits for the expectation like `wait_for` does, up to its timeout. It
matches as `wait_for` does: exact text first, then part of a text. Tapline looks through the app's whole list, off
screen too; Tapwright looks at what is on the screen.

### 9.3 `run`

Runs the steps in order and stops at the first step whose call fails or whose expectation doesn't hold. The
executor is the tool's choice (a host loop, a runner on the phone, inside the app); the answer is the same.

**Answer when every step held:**
```
Ran 5 of 5 steps in 3.4 s.
Same screen. Changes:
~ text "Trips: 0" id=trip-count (was "Trips: 1")
```
- Line 1: `Ran <n> of <n> steps in <s> s.`
- Then the change block for the whole batch: everything since the list the agent had before the call, with
  the usual rules (§3.1: full list when most of the screen changed, off-screen changes, refs kept).
- Then notes, each once, prefixed with its step: `Note (step 2): closed the keyboard first.`

**Answer when a step stopped it** (`isError: true`; the code is the failing step's code, or `expect_failed`):
```
Error expect_failed: Ran 3 of 5 steps; stopped at step 4 (tap "Save"): its action ran, but text "Saved" didn't appear within 2 s.
Same screen. Changes:
+ e51 text "Name is required" @16,300 398x18
```
```
Error not_actionable: Ran 1 of 4 steps; stopped at step 2 (tap "Delete"): covered: e47 button "Delete" is covered by e45 button "Cancel"; close it or pass force: true.
```
- `Ran <k>` counts steps whose action and expectation both held. When the failing step's action ran and only its
  expectation missed, the reason starts with `its action ran, but`; the change block shows what it did.
- `(<step>)` names the step: its tool and its target as the agent wrote it (`tap "Save"`, `tap id=save`,
  `type_text "Email"`, `scroll down`, `wait 1.0`).
- After the first line: the change block since the batch began, then notes. Steps after it don't run.

**`trace: true`** adds one line per step that ran, after line 1: two spaces, the step's number, its own first
answer line and its time: `  1. Tapped e3 button "Save" id=save. (0.2 s)`.

**A skipped step (v1.13)** is said after line 1, in the form `trace` uses, whether or not `trace` is on:
`  3. skipped (if: "Not now" isn't on screen).`, for `if` with `gone` (1.13.1) `  3. skipped (if: "Loading" is on screen).`, or, for `optional`, `  3. skipped (optional: "Not now" isn't on screen).`

**Limits:** at most 50 steps (`bad_params` above that); the batch as a whole times out at 120 s (`timeout`,
with `Ran <k> of <n>` in the message). Time spent waiting for a person's OK (§11.3) or for a "needs a person"
moment to clear (§11.5) doesn't count toward the 120 s. A person taking over between steps stops the batch the usual way
(`busy` or `paused` after the wait, §6), with `Ran <k> of <n>` in the message. Policy checks (§11) apply to
every step; a `needs_confirmation` stops the batch at that step.

### 9.4 Scripts: `record`, `replay`, and the file

A script is a saved batch:
```json
{"format": "agent-tools-script", "version": 1, "name": "sign up then plan a trip",
 "app": "com.example.app", "fresh": true,
 "steps": [{"tool": "tap", "target": {"id": "signup"}, "expect": {"text": "Create account"}}]}
```
- `app`: bundle id (Tapwright) or app name/id (Tapline). `fresh` (default true): close and reopen the app
  first where the tool can; Tapline reloads the JS instead.
- Refs are `bad_params` in a script (they don't survive a session). Targets by `id`, `label`, `text`, or
  `{x, y}` (kept, but a replay of a point target answers `Note (step N): a point target; it breaks when the layout changes.`).
- A step may carry `route` (the screen's route names when it was recorded, e.g. `["Tabs", "Explore"]`). A
  tool that knows routes (Tapline) checks it after the step and its expectation: a step that ends on another
  screen stops the replay with `expect_failed: … it ended on Tabs › Trips; the recording ended on Tabs ›
  Explore`. Tools without routes ignore it; it is never passed to the step's own call.
- A script may begin with steps that bring the app to a known state (`app_command` for sign-in, sample
  data or a reset, where the tool has it) instead of relying on `fresh`: Tapline's reload keeps the app's
  storage.
- **`record`** (`start: true` with optional `name`, then `stop: true` with `path`): every action that
  succeeds while recording becomes a step. The target is saved as the best of: the element's `id`, else its
  `label`, else its `text`, else the point. When that target matches several elements at record time, its
  `index` is saved too. An action that failed isn't saved. `stop` answers
  `Saved 12 steps to <path>.`
- **`replay`** (`path`, `look`, `screenshot`): runs the file like `run` and answers the same way.
- **CLI, no agent:** `tapwright replay FILE…` / `tapline replay FILE…` run scripts in order; each prints the
  `run` answer. Exit code 0 if all held, 1 if one stopped, 2 if a script couldn't start (no phone, no app,
  bad file). `--junit out.xml` writes one test case per script (the stop message as the failure).
- (v1.9) A script may carry reference pictures in `<script name>.references/` beside it, and `record` folds a
  `compare_screen` into the step before it (§14.2, §14.3).
- (v1.13) A script may be version 2 (§18.5): it adds `start` (a place), `end`, `variables` and `${…}` values, and a
  `replay` of it first drives to `start`. A step of either version may carry `if` or `optional` (§9.1). Version 1
  files are unchanged. A `replay` reads the map (§18.9) but never writes it.

## 10. Accessibility audit: `audit`

Checks the screen in front for accessibility problems and answers with a report in one shared format.

- `types` (optional, default all): any of `contrast`, `hit_region`, `description`, `dynamic_type`,
  `clipped_text`, `trait`, `element_detection`, `overlap`, `duplicate_id`. A type the tool can't check is
  listed in the answer's last line, never an error.
- Tapwright runs XCTest's own audit (`performAccessibilityAudit`, iOS 17+: contrast, hit_region,
  description, dynamic_type, clipped_text, trait, element_detection) and adds `duplicate_id` from the tree.
- Tapline runs its checks in the app (contrast, hit_region, description, clipped_text, trait: a pressable
  without a role or a switch without a state, overlap, duplicate_id) and adds each issue's source, as a path
  relative to the app's project, never absolute (`at src/screens/SignUpScreen.tsx:112`).

**Answer:**
```
Audit of "Sign up": 3 issues (contrast 1, hit_region 1, description 1).
1. contrast: e12 text "Already have an account?" @16,700 220x18: the text is hard to read against its background. Fix: raise the contrast to at least 4.5:1.
2. hit_region: e15 button "×" @390,60 20x20: too small to tap reliably (20x20 pt). Fix: make it at least 44x44 pt.
3. description: e9 button @16,60 30x30: no label, so VoiceOver says only "button". Fix: give it an accessibility label.
Checked: contrast, hit_region, description, dynamic_type, clipped_text, trait, element_detection, duplicate_id. Not checked here: overlap.
```
- Issue line: `<n>. <type>: <element line or "the screen">: <what is wrong>. Fix: <how>.`, plus Tapline's
  `   at <file>:<line>` on its own line when known.
- At most 30 issues; then `(+N more issues; pass types to narrow it.)`.
- None: `Audit of "Sign up": no issues found.` and the `Checked:` line.
- The name in quotes is the screen's title where the tool knows it (Tapline: the route's), else the app's
  name (Tapwright: XCTest gives no screen title).
- An element off screen is audited only where the tool can (XCTest audits what is on screen).
- (v1.11) An issue whose element isn't in the tree is named from what the audit itself reports, text first as in an element line: `an element "Total" at @16,300 120x20`; "the screen" only when nothing at all is known.

## 11. Safety policy, confirmation and dry runs

### 11.1 The policy

Both MCP servers take `--policy FILE` (JSON). Without one, the built-in policy applies:
```json
{"version": 1,
 "confirm": ["Buy", "Purchase", "Subscribe", "Pay", "Delete", "Remove", "Send", "Post", "Publish",
             "Sign out", "Log out", "Erase"],
 "block": [],
 "apps": {"com.example.app": {"confirm": [], "block": ["Delete account"]}}}
```
- A word matches the touched element's own label or text, whole words, ignoring case (`Delete trip` matches
  `Delete`; `Deleted` doesn't). For a point target, the element under the point.
- Touches checked: `tap`, `long_press`, `toggle`, a `swipe` that moves less than 10 pt (a touch where it
  starts), and, where an app's own alert shows as `Alert:` (Tapwright), `alert` `accept` on a button with a
  policy word ("Delete trip?" [Cancel] [Delete]). Nothing is checked on `type_text`'s `submit` (the form's
  button tap is what the policy checks); a permission prompt's Allow carries no policy word. testIDs and
  accessibility identifiers are never matched. A touch at a point is checked against what is under the point
  now, read again when the agent's last list can't vouch for it.
- Per-app lists replace the global ones for that app. `"confirm": []` turns confirmation off (a dedicated
  test device with test data).
- `--no-policy` is the same as `{"confirm": [], "block": []}`.

### 11.2 Answers

- Blocked: `Error not_allowed: blocked: button "Delete account" is blocked by the policy ("Delete account"); don't try another way.`
- Needs a person's OK:
  `Error not_allowed: needs_confirmation: button "Delete trip" needs a person's OK (policy word "Delete"). The live view asks them now; when they allow it, make the same call again. If nobody is watching, ask your user.`
- The agent may never confirm by itself. There is no parameter that skips the check.

### 11.3 The person's OK (VIEWER-PROTOCOL 1.5)

- Server → viewer: `{"type": "confirm", "id": "c7", "by": "agent", "kind": "tap", "caption": "Tap \"Delete trip\"", "word": "Delete", "expires": <ms>}`.
- The viewer shows it over the phone: the caption, **Allow once** and **Don't allow**, keyboard reachable
  (Enter = Don't allow by default, focus starts on Don't allow).
- Viewer → server: `{"type": "confirm_answer", "id": "c7", "allow": true}`. Any viewer may answer; the
  first answer wins; the request expires after 120 s.
- An allow lets exactly one matching call through within 120 s: the same tool on the same element of the
  same app. Its answer ends `Note: allowed by a person (in the live view).` After an expiry the retry answers
  needs_confirmation again, with `Nobody answered.` added. After a refusal, for 60 s the same touch asks no
  one: it answers `needs_confirmation:` today's first sentence, then `They declined.`, then `Ask your user what to do.`
  (1.13.1; no advice to make the same call again); after that a retry asks again, with `They declined.` added.
- On the phone's side the live view is the only place a request is answered, for both tools: a prompt on the phone
  could be pressed by an agent. (v1.13) The host's dialog below is drawn by the host, not on the phone.

**The host's dialog (v1.13).** At the same moment as the live view's request, the tool asks the host through MCP
elicitation (`elicitation/create`, form mode), when the client's capabilities say it can show one.
- **The caption is the tool's own words.** Text from the app is quoted, cut to 80 characters and stripped of control
  characters: `Allow: tap "Delete trip" in Wayfare? (policy word "Delete")`.
- **One required field,** `answer`, with the choices `Allow once` and `Don't allow` and no default. Only `accept` with
  `Allow once` allows. `accept` without it, `decline`, `cancel` or an error is Don't allow.
- **The first answer from either place wins.** When the live view answers first, the elicitation is aborted; when the
  dialog answers first, the viewer gets `confirm_closed` with `"by": "chat"` (1.13.2).
- One dialog at a time per server; a second request waits behind the first.
- **Blocked words never reach a dialog:** a blocked touch is refused as in §11.2.
- **With a dialog, the call waits** for the answer, up to the 120 s, instead of answering `needs_confirmation` at once.
  - **Allowed:** before it acts, the tool checks again that it is the same element, with the same label, still
    touchable, in the same app in front. If so it acts, and the answer ends `Note: allowed by a person (in chat).` or
    `Note: allowed by a person (in the live view).` If not: `Error not_allowed: needs_confirmation: the screen
    changed while waiting for the OK; ask again.`
  - **Refused or expired:** `Error not_allowed: needs_confirmation: …` as above, with `They declined.` or `Nobody
    answered.` added; a declined answer is today's first sentence, `They declined.`, then `Ask your user what to do.`,
    with no advice to make the same call again (1.13.1). After a refusal the 60 s rule above holds.
  - In `run`, `drive` and routes, the step waits the same way, and a refusal stops the batch there.
- **A host with no dialog** answers at once, as in §11.2. The agent repeats the call once the person allows it in the
  live view, and the answer ends `Note: allowed by a person (in the live view).`
- **Where each OK came from** (chat or the live view) is in the action log, in `session_report` and in journey reports.
- **An opt-out:** `--no-dialog` on both servers, for a host that passes elicitation requests to the model instead of
  showing them to a person.
- The dialog is drawn by the host and answered by the person; nothing the model writes can answer it. There is still
  no parameter that skips a check, and no approving through the agent.
- **To test, per host:** that it shows the dialog to a person, and that a call held open for 120 s isn't cut off by
  the host's own time limit for a tool call.

### 11.4 Dry run

`dry_run: true` on a touch tool resolves the target and runs every check, then touches nothing:
`Would tap e12 button "Delete trip" at 215,640.` plus, when the policy applies, ` It needs a person's OK (policy word "Delete").`
On `run`: checks every step's parameters, resolves step 1 only (later steps depend on what step 1 does) and
answers `Would run 5 steps; step 1 would tap e12 button "Sign up" at 215,640.`

### 11.5 Needs a person (v1.13)

When a call can't go on without a person, the tool asks through the host's dialog (§11.3), with exactly what to do:
`Needs a person: unlock the phone. I'll go on by myself when it's unlocked.` The one required field is `Done` or
`Can't now`.

- **The moments.**
  - **Both tools:** an iOS alert over the app that the tool may not answer; a journey's stress moment that needs hands
    (§17.4); a media picker the person must use (§17.6).
  - **Tapwright:** the phone locked (planned: and no passcode given; the passcode unlock is not shipped by either tool yet, §18.6); the XCTest passcode prompt; iOS's Health sheet.
  - **Tapline:** the app never paired; the app not open, not in front, or closed; the app not answering twice; a native
    crash (close and reopen it). Tapline can't tell a locked phone from an app in the background, so for it the phone
    locked is the app not in front: `Needs a person: open <app> on the phone (unlock it first if it's locked).`
- The call waits until it can go on, up to 120 s. The tool closes the dialog by itself as soon as the moment clears
  (the app connects, the alert goes, the picker returns, the phone unlocks), then goes on. `Can't now`, or the limit,
  answers the usual error.
- `busy` and `paused` are not such moments: they are a person already at the phone.
- A host without a dialog, or `--no-dialog`: today's behaviour.

## 12. Action log

- One JSON object per line, appended to `~/.tapwright/log/actions-YYYY-MM-DD.jsonl` (Tapline:
  `~/.tapline/log/…`). Files older than 14 days are deleted at start. `--no-log` turns it off.
- Fields: `t` (ISO 8601 UTC, ms), `source` (`tapwright` | `tapline`), `by` (`agent` | `person`), `tool`,
  `target` (as given), `element` (its line without the ref; none for a point), `ok`, `code` and `reason` (on
  failure), `ms`, `app`, `batch` (`{"id": "...", "step": 3}` inside `run`), `dry_run`, `confirmed` (true when
  a person's OK let it through).
- Typed text is never logged, only its length: `"chars": 12`. Secure fields: never anything but the length.
- The viewer's Steps list and this log describe the same actions, with the same captions (VIEWER-PROTOCOL
  1.4 tables).

## 13. Answer size

**Measured** (2026-11-12, Tapline). Text tokens were estimated as characters / 3.6, the same estimate for
both tools; screenshots were counted apart, as w×h/750. Error answers are left out.
- Tapline: 321 answers from the five cold-agent runs.
- Tapwright: 141 answers from its agent sessions.

| | look median / p95 | action median / p95 |
|---|---|---|
| Tapline | 307 / 464 | 114 / 450 (changes only: 69 / 158; a full list: 340 median) |
| Tapwright | 226 / 383 | 230 / 413 |

The bench records the same numbers on the lab (`J6` rows, report only), for a before and after on equal
screens.

**Rules** (simulated on the 321 Tapline answers: the action median goes from 114 to 77 tokens, −32%, and
all tokens fall by 11%):

1. **An action's first line names the element without its position and size.**
   Example: `Tapped e3 button "Log in" id=login-button.` The list or change block that follows has
   `@x,y wxh`. This covers every action verb (Tapped, Long-pressed, Typed into, Toggled, Scrolled…), and
   the first line of a dry run, `Would tap e12 button "Delete trip" at 215,640.`, keeps its point.
2. **A `~` line shows the element's `@x,y wxh` and `[actions]` only when they are among what changed.**
   Example: `~ e6 text "20" (was "17")`. A move keeps its own `↕` line, with the position.
3. **A long old value is cut to what changed.** An old value of 40 characters or less is shown whole. A
   longer one is cut to the part that differs, plus 12 characters on each side, with `…` where it was cut:
   - Count characters as Unicode code points.
   - `p` is the length of the common prefix of the old and new values.
   - `s` is the length of the common suffix of what remains of each after `p`.
   - The shown text is `old[max(0, p−12) .. len−max(0, s−12)]`, with `…` before it when the start is above
     0 and `…` after it when the end is below `len`.
   - When nothing would be cut, the whole value stays.
   - This applies to each old value in `(was …)` and `(off screen; was …)`.

   Examples:
   - `pressed=- typed= typed2= switch=off taps=2 long=0 slow=0 anim=off sheet=closed alert=none` → typed
     becomes `hello` → `(was "…sed=- typed= typed2= swi…")`
   - `Total: 12 items in your basket, delivery on Thursday 14 May` → `(was "…delivery on Thursday 14 May")`
   - `Tap me 17` → `(was "Tap me 17")` (40 characters or less: whole)
4. **The list leaves out the actions a role implies.** Those are: `[tap]` on a button, tab or link;
   `[tap, type]` on an input; `[toggle]` on a switch; `[scroll]` on a scroll view. Any other set is listed in
   full, for example `e4 button "Save" [tap, longPress]` or `e9 text "Help" [tap]`. The shared instructions
   carry this line: "A button, tab or link can be tapped, an input typed into, a switch toggled and a scroll
   view scrolled; other actions are listed in brackets."
5. *(Tapline only, below 1%.)* type_text's first line leaves out `value=`, because the sentence `Value is
   now "…"` says it.

**Guard.** The 16:00 cold-agent run and the A-charter run decide whether the rules stay. If any misread is
traced to a rule, that rule is rolled back: rule 4 first, then rule 3.

### 13.4 `brief` (v1.13)

Shorter answers, for an agent that knows its way around.
- `brief: true` on any action, on `run` and on `drive`, or `--brief` when the server starts. An argument overrides the
  server's setting.
- The answer is: its first line; the `Place:` line when known (§18.1); then the change block, at most 12 lines. When
  the block is cut, the last line is `…<n> more: look shows them.`
- A cut change block keeps its `+` text lines ahead of its `~` and `↕` lines.
- A new screen shows its headers, its actionable elements (buttons, inputs, switches, links, tabs, list rows (`cell`), radios and checkboxes) and every text
  element that is new since the last screen, together at most 12 lines, newest text first, then the same `…<n> more`
  line. New text stays because it is what says an action failed: "Wrong password", "Card declined", an empty state's
  text.
- Tapline counts any pressable element as actionable, which includes its rows (1.13.1).
- Never cut: the `Alert:`, `Network:`, `Keyboard:` and `Place:` lines, notes, errors and `Reflex:` lines.
- `look` is never brief.

## 14. Visual checks (v1.9)

### 14.1 `compare_screen`

Parameters:
- `name` (required): `^[A-Za-z0-9][\w.-]{0,60}$`, as script names.
  - **Names are ASCII (1.13.1).** In every name rule, `\w` means ASCII `[A-Za-z0-9_]`.
  - **A name that differs from an existing one only in case is refused** (`bad_params`, naming the existing one), before
    anything is written: `home` and `HOME` are one file on Windows and macOS. This holds for scripts, references,
    routes and places alike.
- `update`: save the screen as the reference again.
- `width`: px, as `look`'s. A reference is compared only at the size it was saved at.
- `threshold`: the share of the screen, in percent (0–100, default **0.5**), that may change and still match.
- `ignore`: a list of targets painted out on both pictures before comparing: `{id}`, `{label}`, `{text}`, `{ref}`,
  or `{x, y, width, height}` in points. Targets are resolved again on each compare, so a masked clock moves with
  its element. A target that isn't on the screen is skipped, with a note.

Rules:
- The system's bars are never compared. (Tapline's pictures are the app's window and have none; Tapwright
  paints out the status bar.)
- A pixel counts as changed by each tool's own colour tolerance (pictures are compressed; Tapline's is 0.1 of
  the largest channel). Regions are joined neighbouring cells.
- **References belong to the tool that saved them**: Tapline's are the app's window (`.jpg`), Tapwright's the
  whole screen (`.png`, in `~/.tapwright/references/<bundle id>/<name>.png`). Each tool reads only its own.

Answers:
- First call, or `update: true`: `Saved the reference "<name>" (600x1304 px).`
- No changed pixel, or within the threshold: `Matches the reference "<name>": 0.12% changed.` (0 changed:
  `0% changed`.) No picture.
- **A change under the threshold is still named (1.13.1).** The threshold stays whole-screen, and it is still the pass
  bar. Under it, the answer names each changed cluster that is at least 0.1% of the screen, and every changed cluster
  that overlaps a text element, whatever its size:
  `Matches the reference "k-explore" (0.33% changed, under the 0.5% threshold): changed at e9 text "from €1,362", …`
- Above the threshold, `isError` false (it is a finding, not a failure of the call):
  ```
  Differs from the reference "checkout": 3.4% changed, in 2 regions (points):
  - @16,329 193x48: button "Save" id=lab-save
  - @16,620 398x44
  ```
  Each region names up to two of the smallest named elements it touches, as element lines without the ref. At
  most 8 region lines, then `(+N more regions)`. Then the picture: the screen now, faded, with the changed pixels
  in red.
- A reference of another size: `Error bad_params: the reference "<name>" is 600x1304 px and this screen is
  600x1200; take it at the same width, or update: true.`
- Personal mode (Tapwright): as `look`; only a given app's screen is compared or saved.
- It is a read: no policy, no dry run. The action log has `compare_screen` with the name and the percentage,
  never the picture.

### 14.2 In `run` and scripts

- A `compare_screen` step is allowed in `run` and scripts. There it never saves (`update` is `bad_params`), and
  it holds as `expect: {"matches": …}` does: a match within `threshold` holds; anything else stops the batch as
  `expect_failed`:
  `Error expect_failed: Ran 3 of 5 steps; stopped at step 4 (compare_screen "home"): 3.4% changed from the
  reference "home" (threshold 0.5%), in 2 regions (points): @16,329 193x48, @16,620 398x44.`
  For `matches` on an action step the reason begins `its action ran, but`, as for other expectations.
- **A match with names stays a match (1.13.1).** In `run` and `replay` a change under the threshold holds, so no script
  breaks, and the step's line carries the names. `session_report` (§16.1) may count "matched with text changes" as a
  line to look at.
- A missing reference: `… stopped at step 4 (compare_screen "home"): no reference "home" yet; save one with
  compare_screen { name: "home" }.`
- `record` folds a `compare_screen` called right after an action into that step's `expect: {"matches": "<name>"}`
  (with `threshold` and `ignore` when given), like `wait_for` today. Otherwise it is a step of its own. `ignore`
  refs are saved as the element's id, label or text; a ref with none of them can't be saved (`bad_params` at record
  time, naming the element).

### 14.3 References travel with a script

- `record`'s `stop` copies each reference the steps use into `<script dir>/<script name>.references/`. The answer
  adds `, and 2 reference pictures to <folder>`.
- `matches` and `compare_screen` steps in a replay look first in `<script dir>/<script name>.references/`, then in
  the tool's own store. Both tools can share the folder (`<name>.jpg` Tapline, `<name>.png` Tapwright).
- CLI: `tapwright replay` / `tapline replay` take `--update-references`: the checks save into the script's
  references folder instead of comparing; exit 0 when every step ran. A failed check writes its picture to
  `<script dir>/<script name>.diff/<name>.<ext>`, and the JUnit failure message ends with `(diff: <path>)`.

### 14.4 `filmstrip`

Tapline's since 1.0; Tapwright adds it with the same parameters: `frames` (2–12, default 6), `interval_ms`
(100–2000, default 250), `frame_width` (80–400 px, default 200), `tap` (a target to tap first; the touch goes
through the policy, §11, and `dry_run` isn't offered).
- Answer: `Filmstrip: <n> frames about <ms> ms apart, numbered in order, left to right and top to bottom; frame 1
  is just before the tap, the rest follow it.` (the last clause only with `tap`), then the picture. Tapline adds
  `Each capture takes a few hundred ms on a phone, so very quick animations fall between frames.`; Tapwright takes
  its frames from the live stream and doesn't.
- Personal mode (Tapwright): as `look`.

## 15. Diagnostics (v1.10)

A failure comes with its cause: the app's log, its crash, and how it performed.

### 15.1 `logs`

Parameters: `since` (epoch ms: only newer entries), `level` (`error`, `warning` or `info`, default `info`: that level
and above), `grep` (a plain substring, case-insensitive), `limit` (1–500, default 100: the newest).

Answer: one line per entry, oldest first, `HH:MM:SS.mmm <level> <text>`. Text is cut at 300 characters, saying how
much is missing, as element text is (§3.3). The last line is `Latest: <epoch ms>`, to pass back as `since`. With
no entries: `No log entries` plus what was asked (`at warning or above`, `matching "timeout"`, `since <time>`),
then the `Latest:` line.

- **Tapline:** its own ring of the app's console and network lines. `error` is `console.error`, an uncaught JS
  error, or a failed request with status 500 and up. `warning` is `console.warn`, or a failed request below 500.
  `info` is `console.log`, `info` and `debug`.
- **Tapwright:** the app's own entries in the phone's unified log (os_log, NSLog, and React Native's console in a
  release build), from the app's process only, never another process's. It keeps the last 2000 per app while it
  runs, from when the app is opened or `logs` is first used. In personal mode only a given app's entries are read.
- The action log records `logs` with the count of lines, never their text.

### 15.2 Crashes

**The note.** When the app under test stops during a call, the answer ends with one of:
- `Note: the app crashed: <one line: the error or exception type, and where>. logs and crash show more.`
- (Tapline, when the app went away without a JS error) `Note: the app disconnected during this call (it may have
  crashed; Tapline can't read native crash reports, Tapwright's crash can).`

The note goes on error answers too (a call the crash cut short usually ends `not_connected` or `not_answering`).
When the crash only reaches the tool later (Tapline: the app sends a saved JS error when it connects again), the
next answer carries the note once, with when it happened: `Note: the app crashed at 10:42:07: …`.

**`crash`.** Parameters: `all` (the last 5, not only the newest), and (Tapwright) `pull`.
- Answer per crash: the time, the error or exception type and reason, the crashed thread's top 8 frames with the
  app's own frames marked `*`, and (Tapline) the component stack.
- **Tapwright:** the app's crash reports on the phone since the session began, and only the app's own. `pull: true`
  copies the full `.ips` into `~/.tapwright/crashes/<bundle id>/` and names the file. With none: `No crash report
  for <app> since the session began.`
- **Tapline:** the newest fatal JS error since the app connected. The SDK catches it in React Native's global error
  handler before the app dies, keeps it on the phone, and sends it on the next connection. In a development build
  the stack is mapped to the app's files and lines, as `inspect` does. With none: `No JS crash since the app
  connected. A native crash doesn't show here; Tapwright's crash reads those.`

### 15.3 `performance`

Parameters: `launch: true` (Tapwright: close the app and cold-start it; Tapline: a JavaScript reload in a
development build), or `seconds` (0.5–30, default 3: measure while waiting), with optional `tap` (a target tapped
first, through the policy, §11, as `filmstrip` does).

Answer: a first line, then the numbers in one line, then what wasn't measured.
- `Opened in 1.42 s (to the first settled screen).` (Tapwright) /
  `Reloaded in 1.12 s (to the first settled screen; JavaScript only, not a cold start).` (Tapline, development
  builds; release builds answer `unsupported`) / `Measured 3.0 s.`
- Tapwright: `Memory 182 MB (peak 190). CPU 34% (peak 88%). Frames 57 fps (lowest 41); 3 seconds under 45 fps.` (the
  process's memory and CPU, and the frames the whole screen drew). iOS gives Tapwright one frame rate a second and
  no frame times, so its drops are seconds under 45 fps; seconds when nothing was drawn (a still screen) don't
  count. When nothing was drawn at all: `Frames: none drawn (the screen didn't change).`
- Tapline: `JS heap 48 MB (peak 52). JS frames 57 fps (lowest 41); 3 drops over 50 ms; JS thread busy at most
  180 ms.`, and in a development build `Slowest renders: TripList 48 ms (3 times), DayCard 12 ms.`
- fps is the average over the window; `lowest` is the lowest one-second sample (the whole window when it is shorter),
  the number `fps_min` checks.
- Last line, when anything wasn't measured: `Not measured here: CPU, process memory.`

**In `run` and scripts** a `performance` step is allowed (§9.1) and may carry `expect: {"under": {…}}`, with keys
`launch_s`, `memory_mb` (process; Tapwright), `js_heap_mb` (Tapline), `cpu_percent` (Tapwright), `fps_min` (the lowest
sample must be at least this), `drops` (Tapline: drops over 50 ms; Tapwright: seconds under 45 fps), `busy_ms`
(Tapline). A key the tool doesn't measure is ignored with
`Note: <key> isn't measured by <tool>; not checked.` Any other key is `bad_params`; `under` goes only on a
`performance` step. A limit that isn't kept stops the batch: `Error expect_failed: … stopped at step 3 (performance
launch): opened in 2.6 s, over 2.0 s; 4 drops, over 0.` `record` saves a `performance` call as a step, without
`under`.

### 15.4 Failures carry their diagnostics

- When a step of `run` or `replay` fails, the answer adds, after the change block, `Logs (since step <n>):` and up
  to 20 entries at warning or above since that step began, when there are any, and the crash note when the app
  stopped.
- CLI `--junit`: the same lines go into the test case's `<system-out>`. Tapwright also pulls a crash report next to
  the script, as `<script dir>/<script name>.diff/<script name>.crash.ips`, and names it there. Tapline puts its
  crash detail in `<system-out>` only.

### 15.5 Not in 1.10

Network capture, energy, disk and thermal numbers, and symbolicating native frames. Tapwright shows a crash
report's own symbols; full symbols need the app's dSYM files.

## 16. Fewer steps (v1.11)

The same check should cost an agent no more calls than on Android. This section is what each call does to make the
next one unnecessary.

### 16.1 The call log and `session_report`

- The action log (§12) also records reads (`look`, `wait_for`, `logs`, `audit`, `compare_screen`, …) with
  `"kind": "read"`, and every entry gains the agent's client id and the size of its answer in characters. Typed text,
  pictures and log text are still never written.
- `session_report` (a read; also `tapline report` / `tapwright report`, with `--since <time>` and, in Tapwright,
  `--client <id>`): `since` (epoch ms, default the start of this agent's session). Answer:
  ```
  Session: 212 calls in 31 min (look 64, tap 71, scroll 38, run 4, …). Failed: 19 (not_found 12, ambiguous 4, not_actionable 3).
  Waste:
  - 22 looks right after an action whose answer already had the changes (calls 14, 18, 31, …)
  - 6 retries after not_found that found the same element by another target (calls 40, 77, …): use its id first
  - 4 scroll hunts of 3 or more scrolls for an element already in the list (calls 90–93, …): let the action scroll
  - 9 runs of 3 or more actions with no answer read in between (calls 101–104, …): one run each
  ```
  At most 5 call numbers per flag, then `…`. A flag with nothing to show is left out; with no waste: `Waste: none.`
- **A retry whose failed target was an id (1.13.1).** When a retry after `not_found` had failed with an id (`{id: "save"}`,
  then `{text: "Save"}` finds the element), the retry flag ends `: target what the latest look shows (its id there, when
  it has one)` in place of `: use its id first`.
- A scroll hunt is exact when the element was in the tree's off-screen list before the first scroll; otherwise the
  flag says `(maybe)`.
- **Seconds (v1.13).** Every entry of the action log, reads too, carries `"ms"`, the call's time in the tool (§12). The
  report gains one line, after the `Session:` line:
  ```
  Time: 31 min: thinking between calls 22 min (median 3.4 s), in the tool 9 min (median 0.7 s; tap 0.5 s, look 0.2 s, …).
  ```
  - Thinking time is the gap between one call's answer and this client's next call. A gap over 2 minutes counts as
    idle and is left out.
- **New waste flags (v1.13),** in the same list and with the same limit of 5 call numbers:
  - `- 5 calls in a row on a way the map knows (calls 12–16): drive {"to": "language"} is one call`
  - `- 14 calls each from a new process (calls 1–14): load the MCP server once; each call paid about 2 s`
  - (1.13.1) A flag starts at 3 calls in a row on a way the map knows, and at 5 thin calls each from a new process.

### 16.2 Off-screen targets and closest matches

- `tap`, `long_press`, `toggle` and `type_text` on a target that is in the tree but off screen scroll it into view,
  clear of the bars, and then act, in one call. The policy check (§11) runs on the target before the scroll. The first
  line says so: `Scrolled to it, then tapped e41 button "Save" id=save.` `dry_run` answers
  `Would scroll to it, then tap e41 button "Save" id=save at 215,640.` (the point it expects after the scroll).
- When no scroll can bring it on screen (it isn't inside a scroll view, or a scroll ends with it still covered), the
  action is refused as before, `not_actionable: off_screen`, and the message says why (1.13.1): it lies in a part of the
  screen that nothing scrolls (behind a fixed bar, or outside every list), so look at the screen, then target something
  else or report it. This is the only meaning `off_screen` has now.
- An element a list hasn't made yet (a lazy row) isn't in the tree: it stays `not_found`.
- `not_found` adds up to 3 closest matches, each as an element line with its ref and, when off screen, where:
  `Closest: e12 button "Save draft" id=save-draft, e30 text "Saved" (off screen, below).` Close means: the same words
  in another case or order; one contained in the other; or a small edit distance (at most 2 for up to 10 characters,
  3 above). A match on only a common word ("the", "a", "of", "to", "and", "in") doesn't count. With none, no
  `Closest:` part.

### 16.3 Language, region and text size

Shared values:
- `language`: a BCP 47 code (`ar`, `en`, `en-GB`).
- `region`: an ISO 3166 country code (`QA`).
- `text_size`: one of these, with the font scale each means (the number React Native reports as `fontScale`):

  | size | xs | s | m | l (iOS default) | xl | xxl | xxxl | ax1 | ax2 | ax3 | ax4 | ax5 |
  |---|---|---|---|---|---|---|---|---|---|---|---|---|
  | scale | 0.82 | 0.88 | 0.94 | 1.0 | 1.12 | 1.24 | 1.35 | 1.65 | 1.94 | 2.35 | 2.76 | 3.12 |

  And `system` (both): the phone's own setting. Tapwright: as with no `text_size`. Tapline: it clears the override,
  `Text size: the phone's own (1.0×).` with the real scale. In Tapline a text size set by the agent also goes back to
  the phone's own when the session ends.

- **Tapwright:** `open_app` takes `language`, `region` and `text_size`. It (re)launches the app with Apple's launch
  arguments (`-AppleLanguages`, `-AppleLocale`, `-UIPreferredContentSizeCategoryName`), for that launch of that app
  only; nothing in Settings changes. A running app is closed first, and the answer's first line says what it opened
  with: `Opened Wayfare (com.example.wayfare) in ar, QA, text size xl.` The next `open_app` without them is the app as the phone
  sets it.
- **Tapline:** `app_command` names `language` (`{ language }`), `region` (`{ region }`) and `text_size`
  (`{ size }`), which the app provides (`init` adds them for i18next and expo-localization). `text_size` needs the app
  to use the SDK's font-scale helper; without it, `unsupported`, naming `audit`'s `dynamic_type` check and
  Tapwright's `open_app`. A switch to or from a right-to-left language needs a restart in React Native: in a
  development build Tapline reloads the app and says so; in other builds the answer says the app must be restarted.

### 16.4 Network conditions

- `network` (Tapwright: a tool; Tapline: a built-in `app_command`), `{ mode: "offline" | "3g" | "lossy" | "normal" }`.
  - `offline`: no request gets through. `3g`: about 400 ms more latency and 750 kbit/s. `lossy`: about 1 in 5 requests
    fails, the rest a little slower. `normal`: as before. Tapwright uses Apple's nearest profiles (3G; Very Bad
    Network for `lossy`), so its numbers differ from these. A policy whose `block` list holds `network` (§11.1) refuses it in both tools (`normal` is always allowed): `Error not_allowed: blocked: the safety policy blocks network here.` (for one app: `… blocks network for <app>.`); Tapwright's condition also ends 30 minutes after it was set.
  - The answer is one line, `Network: 3g (Tapwright, whole phone).` / `Network: 3g (Tapline).`, and while a condition
    is on every answer's header carries the same line.
  - It returns to `normal` by itself when the session ends or the tool loses the phone or the app, so the phone is never
    left offline.
- **Tapwright:** the phone's own network conditioner (Apple's developer condition inducer). It is phone-wide: other apps
  are affected too, which is why the line says `whole phone`. Tapwright's link over USB keeps working; over Wi-Fi,
  `offline` and `lossy` would cut Tapwright off, so they are `unsupported` with that reason.
- **Tapline:** the app's own JavaScript requests (and WebSockets where the SDK wraps them); not images the native side
  loads or native SDKs, which the answer names. Tapline's own connection is never conditioned.

### 16.5 The skill, and the benchmark

- Both skills carry `run` examples for the common patterns (sheet → choose → save → check; form → submit → wait for the
  confirmation; a tab tour with `compare_screen`) and a "spend fewer calls" list: read the change list instead of
  `look`; target by id; let the action scroll; put known steps in `run`.
- QUALITY-BAR gains a calls-per-check section: a fixed list of checks on Wayfare (open a screen, change a setting,
  fill a form, find an item in a long list, check an error state), each scored by calls and seconds. The bar: no more
  calls than the same check on Android.

## 17. Journeys and usability (v1.12)

An app is made or broken by whether a user can find a feature, understand it and finish it easily. This section lets an
agent test that: go through a feature as a user would, measure the trip, check each screen against the basics, and file
a visual report. Nothing here is required setup: an agent can start a journey with only a goal.

### 17.1 `journey`

- `start: true` with `goal` (free text: "log breakfast by scanning its barcode") and optional `persona` (free text, or
  the name of an example persona, §17.2), `conditions` (any of 1.4's `language`, `region`, `text_size`, `network`,
  applied at the start and undone at the stop), `allow` (shortcuts the journey may use: `open_url`, and in Tapline test
  `app_command`s) and `eyes` (`"user"`, the default, or `"agent"`).
- `stop: true` with `reached` (true or false) and optional `why`, `export` (§17.5). Only one journey per agent at a
  time; starting another while one runs is `bad_params`.
- Answer to `start`: `Journey started: "<goal>" as <persona or "no persona">, in a user's eyes.` and the screen.

**In a user's eyes** (the default while a journey runs):
- Only what is visibly on the glass: elements clipped by their scroll view, behind a sheet or under the keyboard are left
  out, and the header says `more below` / `more above` only where a view can really scroll further (no counts).
- No ids. An element's line shows its visible text; an icon-only button shows as `e12 button (icon only)`, with its
  accessibility label apart as `(VoiceOver: "Settings")`. `placeholder` stays (it is on the glass).
- Targets: `ref`, visible `text` (only text a person can read on the glass: never an icon-only button's VoiceOver label,
  nor an element clipped out of view), and `{x, y}`. `id` and `label` answer `bad_params: in a journey, ids and labels are
  hidden; use a ref or the visible text`.
- No shortcuts: act-after-scroll (§16.2) and `scroll` `to: "target"` are off; `wait_for` looks at the screen only;
  `open_url` and test `app_command`s are refused unless `allow` names them, and the report lists the ones used.
- `eyes: "agent"` turns all this off for the rest of the journey (the report says so).

**Measured while it runs** (by the tool, not the agent): taps, screens, typing, scrolls, time (with waits for loading
apart), wrong turns (a screen entered and left by `back` without acting), dead ends, errors hit, hesitations (two or
more reads without an action), and the taps from the first screen to the goal.

### 17.2 Personas as guidance

No tool enforces a persona. The skill teaches the agent to build them for the app at hand (who uses it, what they
already know, what they want, in what situation, which conditions fit) and to agree them with its user. A reference
file of example personas (first-timer, busy regular, older user with large text, right-to-left speaker, poor
connection, one-handed on a big phone), a few lines each with the conditions they suggest, is a starting point to copy,
extend or ignore.

### 17.3 `ux_check`

One screen against the basics. Facts first (each with the element it concerns), then the agent's own judgment:
- the number of competing choices (actionable elements on screen);
- whether there is one clear main action (size, position, contrast);
- main actions out of thumb reach for this screen size (the lower part of the screen);
- icon-only buttons with no label;
- cut-off text, contrast and target size (from `audit`);
- the same action named differently on screens seen in this journey;
- an error or empty state that doesn't say what to do next.

Answer: `UX check of <screen>: <n> findings.` then one line each `<n>. <kind>: <element line>: <what> (<basis>)`, where the
basis is `measured`, `best practice: <rule>` or `house rule: <rule>`. Tapline adds the file and line in a development
build.

**House rules** (optional): `--ux-rules FILE` (JSON, like the policy file: `{"version": 1, "rules": ["core features within
2 taps of home", {"max_taps_to_goal": 4}, {"way_back": true}]}`). A rule written as text is handed to the agent to judge;
a rule written as one of the measured keys is checked by the tool: `max_taps_to_goal` (a journey that reaches its goal
in more taps breaks it) and `way_back` (`true`: every screen in the journey has a visible way back, a back button, a
close control or a tab bar). An unknown key is `bad_params` when the file is read. Each rule appears in the report.

### 17.4 Stress moments

`journey` with `stress` (during a journey): `background` (the app leaves and comes back), `network` (with `mode`, a 1.4
condition starting mid-flow), `double_tap` (with a `target`: the same touch twice, 80 ms apart; refused when the safety policy flags the target, since
one OK never covers two touches, and otherwise each touch is checked), `rotate` and
`relaunch`, and `survives` (what must still be there after: `{text}` or `{goal_step}`). Answer:
`Stress: background; it survived: text "2 eggs" is still in the field.` or `…; it didn't survive: …`.
- **Tapwright** does all of them (`relaunch` closes and opens the app).
- **Tapline** does `network` and `double_tap`; `relaunch` is a reload in a development build, said as `reload, not a
  relaunch`; `background` and `rotate` ask the person (a needs-a-person line, "switch to another app and back, then say
  done"), or are done by Tapwright when it drives the same phone. None is faked with an in-app event. When the person
  has done it, the agent calls `journey` with `stress` and `done: true`, and the tool checks `survives` then.
- **A double tap's second tap needs the same element (1.13.1).** It answers `changed` and doesn't tap again when the
  control's id, label or text changed after the first tap: the safety check only saw the first, and a recycled row can
  turn "Continue" into "Delete account".

### 17.5 The visual report

`journey` `stop` writes one report folder (`report.md`, `report.html`, and its pictures) and answers its summary:
```
Journey "log breakfast by scanning its barcode" as a first-timer: reached in 9 taps, 1 min 52 s (34 s waiting), 2 wrong turns, 3 hesitations.
Findings: 2 high, 3 medium, 1 low. Report: <folder>
```
then at most 2 small key pictures. The report holds: the goal, persona and conditions; reached or not; the numbers;
the path screen by screen with a filmstrip; each finding with its severity, screen, a marked-up screenshot and its
basis; and a GIF of the journey (360 px wide, taps drawn as rings, at most 3 MB; Tapwright from its live stream,
Tapline from frames it keeps after each settled action), or a short video where there is one. Pictures stay in files,
never in the agent's context beyond the stop answer.

**Export** (only when the user asks, since it writes to their GitHub) needs `confirm: true` (the agent says its user
asked). It uploads `report.md`, `report.html`, the GIF, the filmstrip and the finding pictures in one commit.
`{"to": "pr", "repo": "...", "pr": 310}` commits them into that PR's branch under `.tapwright/reports/<name>/`
(Tapline: `.tapline/reports/<name>/`) and comments on the PR with the summary and the pictures linked; when the PR's
branch lives in another repository (a fork), it is refused unless `fork: true`, and the answer names that repository.
`{"to": "issue", "repo": "..."}` pushes them to the repository's `qa-reports` branch and opens an issue that links
them. A public repository is refused unless `public: true` is also given. Both use `gh` with the user's own sign-in,
and the answer says what was written where.

### 17.6 `media`: test pictures for photo pickers

`media` with `add` (paths of pictures on the computer) and `confirm: true` puts them in the phone's photo library for an
app's photo picker; `remove: true` deletes only what it added. The skill says to ask the user first whether pictures may
be added, and whether to remove them afterwards. Without `confirm`, `bad_params` saying so.
- **Tapwright:** through its own runner on the phone. iOS asks the person once to let the runner add photos; Tapwright
  adds with add-only access, so it never reads the library. `remove` needs the runner's full access, which iOS asks for
  once, and iOS asks the person to confirm each delete (Tapwright doesn't tap that for them). Without full access,
  `remove` answers what to delete by hand (their names, from Recently Added).
- **Tapline:** through the app's own `expo-media-library` (or `@react-native-camera-roll/camera-roll`), with the app's
  own permission prompt and delete confirmation. Without either, `unsupported`, naming Tapwright's `media`. Tapline also
  has `picker: true`: the next image-picker call returns the picture without the library; the report marks it
  `(picker skipped)`.

## 18. Driving (v1.13)

An agent should never be asked to decide what the tool already knows. A screen the tool has seen, a way between two
screens it has driven, a link that reaches a screen: these are remembered, and the tool drives them at its own speed
with no model in the loop. A small, fast model drives only where nothing is known yet, and what it does is remembered
for next time.

### 18.1 Places

A **place** is a screen the tool can recognise again. Each place has a key, a name and, when known, a link.

- **A route key** (Tapline, in an app with React Navigation or Expo Router):
  `{"route": ["(tabs)", "settings", "language"], "tab": "settings", "layer": "sheet:Filters"}`
  - `route`: the route *pattern* names, never the params (`/user/123` is `["user", "[id]"]`);
  - `tab`: the selected tab's route name, or absent;
  - `layer`: what lies on top, `sheet:<name>`, `modal:<name>` or `alert`, or absent.
- **A signature key** (Tapwright; Tapline in an app without a router, or where one route holds several steps):
  `{"signature": "<hex>", "parts": {...}}`. The signature is built only from what doesn't change with content or
  language:
  - the identifiers of elements that aren't list rows;
  - the kinds of the top-level containers, in order (navigation bar, tab bar, list, form, web view);
  - the selected tab's identifier, else its position;
  - what lies on top (a sheet, an alert; the keyboard isn't counted).

  `parts` keeps those pieces, so two reads can be compared part by part. Two reads are the same place when at least
  the tool's threshold of parts agree (each tool tunes its own). The navigation title breaks a tie, compared per
  language as the hashes of §18.9.
- **Never in a key or a name:** a list row's text, a param's value, typed text, anything from a secure field.
- **The name.** Names are ASCII, `[a-z0-9_]`, lower case with `_` for spaces (the case rule of §14.1 holds); a second place with the same name gets
  `_2`. A navigation title can be private ("Chat with Sara", "Order #10432"), and a name ends up in the map, in
  `map`'s listing and in every `Place:` line, so a name never comes from content:
  - **Tapline** names a place from its route only: the last route name, skipping groups such as `(tabs)`. When the
    last segment is a param, the name is its parent plus the param's name, so `trips/[id]` is `trips_id`. `index`
    takes its parent's name, so `trips/index` is `trips`. Tapline never needs titles.
  - **Tapwright** never names a place after its navigation title (1.13.2): a repeated title such as "Chat with Sara"
    would become a stored place name. A place is `place_<n>` until a person names it with `map`. A map that already
    holds title-derived names keeps them. Tapline's route-based naming is unchanged.
  - `map {"name": ...}` renames the current place.
- **Known before any visit:** an Expo Router app's places can be listed from its route tree, each with its link
  (`scheme://path`). A place whose route has params takes their values as variables (`trips_id` needs `id`).
  Such a place has no roads until one is driven.
- **Between the tools:** when both are connected to the same app, Tapwright may take the place from Tapline (§18.11).

**The `Place:` line.**
- An action that arrives at a known place says so after its first line: `Place: language.` A place seen for the
  first time says `Place: new (call it with map name).` It is saved under the name the rules above give.
- The line goes on `look` (as a line of the header, §3.2), on every answer that shows a new screen, and in `brief`
  (§13.4) and `drive` answers. A same-screen answer (`Same screen. Changes:` or `No visible change.`) doesn't carry
  it: nothing moved, so the line would only repeat the last one.
- It appears only once the app has a map: at least one place saved.

### 18.2 Roads

A **road** is one move that left a place and arrived at another, as it was seen:
```json
{"from": "settings", "to": "language", "move": {"tool": "tap", "target": {"id": "language-row"}},
 "names": {"id": "language-row", "label": "Language", "text": "Language"},
 "by": "tapwright", "seen": 7, "failed": 0, "ms": 840, "last": "2026-11-12T18:20:00Z", "build": "1.4.0 (212)"}
```
- A move is saved when it is made by `tap`, `long_press`, `toggle`, `back`, `open_url` or `swipe`, and leaves a known
  or new place and arrives at another, in `run`, `drive` and single calls alike. These are not saved:
  - reads, a person's input from the live view, and actions in a dry run;
  - a move that needed a person's OK, so that `drive` never plans through a Delete or a Buy. The answer says
    `Note: not saved as a road (it needed a person's OK).`;
  - `type_text`, and any move whose effect depends on typed text (a search that leads to a results screen): the text
    isn't stored, so the road couldn't be driven again. Typed paths belong in routes, which have variables (§18.5);
  - anything a `replay` does (§18.9).
- `move` targets by `id`, else `label`, else `text`, never by `ref`. A point only for something with no element.
- **A road's `move` is a step as `run` takes it (1.13.1)**, so both tools can read each other's roads and replay them with
  no translation. The shapes: a swipe is `{"tool": "swipe", "x0", "y0", "x1", "y1", "seconds"?}`, in points; tap,
  long_press and toggle carry `target` (id, label or text, never a ref); back is `{"tool": "back"}`; open_url carries the
  cut-down link. Nothing from the screen is stored.
  `names` keeps every name the element had, for repair (§18.4).
- A list row is saved only by its id. A row with no id is saved as `{"row": true}`: the road exists, but `drive`
  can't replay it and stops there with `off_route` (the driver chooses the row).
- `by` is the tool that drove it. A tool replays only its own roads, unless a road is marked `"by": "both"` after
  each tool has driven it once.
- `build` is the app's version and native build number (`CFBundleShortVersionString (CFBundleVersion)`). A
  JavaScript reload is not a new build. A road whose build isn't the app's current one is **unproven** until it is
  driven cleanly once.
- A road that fails twice in a row (its target is missing, or it arrives somewhere else) is dropped, and the answer
  says so.

### 18.3 Links

- A place reached by `open_url` remembers that URL as its link. A link can carry a secret (a magic sign-in, a
  password-reset or an invite link holds a token in its query or its path), so it is saved with these rules:
  - only scheme and path; the query and the fragment are dropped;
  - a path segment is kept only where a known place's pattern has fixed text; where the pattern has a param, the
    segment becomes its variable (`wayfare://trips/42` is saved as `wayfare://trips/[id]`). Patterns come from an Expo Router
    app's route tree, which says which segments are params;
  - a link whose path matches no pattern isn't saved, and the answer says `Link not saved: it may hold a value; add it
    with map link.`
- `map {"link": "wayfare://settings", "place": "settings"}` adds one by hand, and keeps it as given.
- A **jump** is an `open_url` of the place's link. Tapline doesn't move the app with its own navigation from inside,
  and Tapwright doesn't use private ways into an app: a jump goes through the app's linking, as a person's tap on
  the link would.

### 18.4 `drive`

```json
{"to": "language"}
{"to": "trips_id", "with": {"id": "42"}}
{"route": "sign_in", "account": "tester2"}
{"to": "language", "then": [{"tool": "tap", "target": {"text": "العربية"}, "expect": {"text": "اللغة"}}]}
{"to": "language", "by": "taps"}
```
- `to`: a place's name. `route`: a saved route (§18.5). One of the two.
- `with`: values for a place's params or a route's variables. `account`: a test account's name (§18.6).
- `then`: ordinary `run` steps (§9), run after arriving, in the same call.
- `by`: `"any"` (default: a jump when the place has a link and the moment allows one, else roads), `"taps"` (roads
  only, never a jump), `"link"` (a jump only).
- `brief` as on any action (§13.4).

**How it travels.**
1. Where am I? The current place, from a read.
2. The way: a jump if the place has a link and `by` allows one. A jump doesn't need to know where it starts, so it
   works from an unknown place. Otherwise the chain of proven roads with the fewest moves (unproven roads count
   double); from an unknown place that is `off_route`, before anything is touched. No way: `Error not_found: no known
   way from settings to language; drive it once (the driver), and it is remembered.` Two ways of equal cost: the one
   used most recently.
3. Each move goes through the same checks as a single call (scope, policy, confirmation, `force` never implied).
   Between moves the tool waits only until the next move's target is on screen, steady and touchable (cap: the
   road's own `ms` times 3, at least 2 s, at most 10 s). No answer is built for the steps in between.
4. After each move it checks that it is where the road leads. After the last, it checks the destination.

**When the road is wrong.**
1. The target is missing: the same element by its other `names`, then the closest match of §16.2. A match is taken
   only when it is the only one, and it goes through the policy like any target. The road is updated only after the
   arrival check passes, not when the match is made, and the answer then says `Road repaired: "Language" (was
   id=language-row).`
2. It arrived somewhere else that is a known place: it plans again from there, at most twice per call.
3. Otherwise it stops, with nothing more touched:
   `Error off_route: stopped at step 2 of 3 (tap "Language"): it isn't on this screen. Now at: settings_general
   (known). Done: 1 move. Nothing else was touched.` followed by the screen as a normal action answer shows it.

**The answer.**
```
Arrived at language by 3 taps in 1.9 s (settings > general > language).
Place: language.
<the destination's screen, as an action's answer shows a new screen>
```
- How it travelled is always said: `by link`, `by 3 taps`, `by link then 1 tap`, and `, 1 reflex` when one fired.
- (1.13.1) A route's drive line names the route and not its steps: `Arrived at home by route sign_in in 3.2 s.`
- Unproven roads driven cleanly: `Note: 2 roads proven again for build 1.5.0 (220).`
- With `then`, the steps follow as in `run`'s answer (§9.3).
- A person's input pauses a drive between moves, as it pauses any call (Tapline: 3 s of quiet). `paused` and
  `locked` stop it with the usual errors, plus `Done: <n> moves.`

**`dry_run`** answers the way it would take and touches nothing:
`Would drive to language by 3 taps: settings > general > language (all proven).`

### 18.5 Routes

A **route** is a script (§9.4) in version 2, saved by name, that starts at a place instead of a fresh start.
```json
{"format": "agent-tools-script", "version": 2, "app": "com.example.wayfare", "name": "sign_in",
 "start": "welcome", "end": "home", "variables": ["account"],
 "steps": [
   {"tool": "tap", "target": {"text": "Continue"}},
   {"tool": "type_text", "target": {"id": "email"}, "text": "${account.username}"},
   {"tool": "type_text", "target": {"id": "password"}, "text": "${account.password}", "secure": true},
   {"tool": "tap", "target": {"text": "Sign in"}},
   {"tool": "tap", "target": {"text": "Not now"}, "optional": true},
   {"tool": "wait_for", "text": "Home"}]}
```
- `record` with `"route": "<name>"` saves what follows as a route from the current place; `record` with `stop` writes
  it, with `start` and `end` set from the places it saw.
- While recording, `type_text` takes `"as": "<name>"`: the step is saved with the text `${<name>}`, and the name goes
  into `variables`. Without `as`, the text is saved as typed. A secure field's text is never saved. A literal `${` in
  typed text is written `$${`.
- `start` is a place: `drive {"route": ...}` drives there first if the app is elsewhere.
- `replay` of a version 2 file starts fresh, as for any script, then drives to `start` (by link or roads; an
  `off_route` there fails the replay), then runs the steps. `drive` runs it from wherever the app is.
- A route is a way between its `start` and `end` for `drive {"to": ...}`, counted as one move per step.
- `if` and `optional` (§9.1) work in routes. Version 1 files keep working everywhere.

### 18.6 Test accounts

Both servers take `--accounts FILE` when they start:
```json
{"version": 1, "accounts": {
  "tester2": {"username": "tester2@example.com", "password": "…", "apps": ["com.example.wayfare"]},
  "phone":   {"passcode": "…"}}}
```
- An agent refers to an account by name (`"account": "tester2"`, or `${account.username}` in a step). The tool types
  the values. They are never in the action log, the map, a script, a report's text or the live view's captions.
- **An account value is the whole text of a step, never part of one.** `"text": "${account.username}"` works;
  `"text": "hi ${account.password}"` is `bad_params`, since joining text would get around the secure-field rule.
- `password` is typed only into a secure field, `username` only into a text input that isn't secure. Any other
  target: `Error bad_params: an account's password goes only into a password field; e14 is a text field.`
- **What can be seen.** The password never in text answers: secure fields show dots. While a field holds exactly an
  account's username, text answers show `value=<tester2's username>`. Pictures (screenshots, journey frames, the GIF)
  show what is on screen. They are free of a password only while its field stays secure: a "show password" toggle puts
  it in pictures (1.13.2). A username in a plain field is visible in pictures anyway.
- `apps`: the apps the account may be typed into (absent: any given app).
- `phone.passcode` (Tapwright only; **planned**, not shipped by either tool yet) is typed by the tool itself, only on the lock screen's keypad, when a call needs
  the phone and it is locked. No step or tool can type it anywhere else.
- **An accounts file inside a git work tree that doesn't ignore it:** the server starts but loads no accounts. Every
  use of an account answers `Error bad_params: accounts file is inside the git repo at <path> and not ignored; move
  it or add it to .gitignore.` `doctor` and the session's first answer say it too.
- `map` and `session_report` list account names, never values.

### 18.7 Reflexes

A reflex is a rule the tool applies by itself when its moment comes, between the steps of any call.
- **Built in, in both tools:** close the keyboard when it covers the next target (both do this today); while a
  loading indicator is the only thing changing, or a request the action started is in flight (where the tool can
  see one), keep waiting up to the step's cap instead of answering `not_found`. Tapwright also waits out a
  notification banner (it does today).
- **Declared, per app,** in the map: `{"name": "rate_prompt", "when": {"text": "Rate Wayfare"}, "do": {"tool": "tap",
  "target": {"text": "Not now"}}, "max": 1, "at": "home"}`.
  - `when`: `text`, `label` or `id` on screen; `do`: one step; `max`: firings per call (default 1, at most 3).
  - `at`: optional, a place. The reflex fires only there, so a "Not now" for a rating prompt doesn't fire on a screen
    where "Not now" means something else.
  - Added with `map {"reflex": {...}}`, removed with `map {"forget_reflex": "rate_prompt"}`.
  - A `do` the policy flags (§11) is refused when it is added. It is checked again at the moment the reflex fires,
    because the policy file can change and the `do` target can carry another word on another screen; a reflex whose
    `do` is flagged then doesn't fire.
- Every firing is in the answer: `Reflex: closed "Rate Wayfare" (tapped "Not now").`
- **Never reflexes:** a permission prompt (how it is answered is the test's decision), a policy-flagged action, a
  person's OK.
- **Off** in a user's-eyes journey unless its start allows `reflexes` (§17.1): there the interruption is a finding.
- **When they are checked (1.13.1):** at place changes, and before answering a failure. Not between every `run` step.

### 18.8 `map`

A read (no `look`), and the way to edit what a map holds.
```
map                         → this app's places, routes, reflexes, account names; the current place
map {"to": "language"}      → the way drive would take, and whether each road is proven
map {"name": "checkout"}    → names the current place
map {"link": "wayfare://x", "place": "x"} / {"reflex": {...}} / {"forget_reflex": "..."} / {"forget": "place"}
```
The listing, one line each, at most 60 places then `…`:
```
Map of Wayfare (com.example.wayfare), build 1.5.0 (220): 23 places, 41 roads (38 proven), 4 routes, 1 reflex. Now at: home.
Places: home (link wayfare://), settings (link wayfare://settings), language, profile, trips_id (link wayfare://trips/[id]), …
Routes: sign_in (welcome → home; account), switch_language (settings → language; language), …
Reflexes: rate_prompt.
Accounts: tester1, tester2.
```

### 18.9 The map file

- **One folder per app,** named by the app's own id, never its container's: in Expo Go every app reports Expo Go's
  bundle id, so the key is the app's id from its config. By default it is in each tool's home
  (`~/.tapwright/maps/<bundle id>/`, `~/.tapline/maps/<bundle id>/`). Both servers take `--maps DIR` to keep it with a
  project (`<DIR>/<bundle id>/`), so a team, CI and both tools share one folder. It holds `map.json`, in the format
  `agent-tools-map` version 1, and `routes/<name>.json` (§18.5), so a person can review each route.
  ```json
  {"format": "agent-tools-map", "version": 1, "app": "com.example.wayfare", "titleKey": "<random hex>",
   "places": {"language": {"key": {...}, "link": null, "titles": {"en": "3fa9c1d27b0e44a8", "ar": "9c01e7a5d2b84f36"}, "last": "..."}},
   "roads": [ ... ], "routes": {"sign_in": "routes/sign_in.json"}, "reflexes": [ ... ]}
  ```
- **Titles are kept only as hashes.** A navigation title is often private, and with `--maps` in a project the file is
  committed. Each title is stored as HMAC-SHA-256 with a random key kept in the map file (one per map), cut to 16 hex
  characters, per language. Equality is all the tie-break in §18.1 needs. A person who wants readable titles names the
  place. Tapline stores no titles.
- **Writing** is read, merge and write under a lock file (`<file>.lock`, created exclusively, counted as stale after
  10 s). The file on disk is merged with the tool's memory by place key and by road (`from`, `move`, `to`), the newest
  `last` winning, so one tool's save never drops the other's work. The result goes to a temporary file, then a rename
  over the map. On Windows a rename over a file the other tool has open can fail with `EPERM` or `EBUSY`, so it is
  retried up to 5 times, 50 ms apart.
- **`replay` reads the map and never writes it** (`tapwright replay`, `tapline replay` and the `replay` tool alike), so
  a CI run with `--maps` pointed at the repo doesn't change tracked files as a side effect.
- A person may edit the file by hand; a file that doesn't parse is left alone, and the tool says so and keeps
  driving without it.

### 18.10 The thin call

For hosts that can't keep an MCP connection: `tapwright call <tool> '<json>'` and `tapline call <tool> '<json>'`.
- It sends the call to the tool's running process (Tapwright's daemon; Tapline's host) over loopback with the token
  that process already uses, and loads nothing beyond the language's standard library. A test checks it.
- **With the thin call the model writes the command line, so a call may only narrow what its process was started
  with.** Scope (`--apps`, `--dedicated`), the policy (`--policy FILE`, `--no-policy`), `--no-dialog` and the accounts
  (`--accounts FILE`) are fixed when the daemon or host starts. A call may give a smaller `--apps` or a stricter
  policy. One that tries to widen any of them answers `Error bad_params: <flag> is set when <tool> starts, not per
  call`. Tapwright refuses every call when its daemon was started without `--apps` or `--dedicated`, as its MCP
  server does.
- `--maps DIR` and `--ux-rules FILE` as the MCP server takes them.
- **Tapline** serves `/api/<tool>` only from a standalone `tapline host`, never from a host inside an agent's MCP
  server, so one agent's session can't be driven by another's thin calls.
- `--client NAME`: the agent's identity, so its report and journey stay its own (default: the parent process's id).
- Output: the answer's text on stdout (a picture, when there is one, is saved and its path printed);
  `--json` prints `{"text", "image_path"}`. A refusal prints its error line on stderr and exits with 2; a failure to
  reach the tool, 3.
- `POST /api/<tool>` with the JSON arguments is the documented interface underneath, for a host that keeps its own
  connection: headers `Authorization: Bearer <token>`, `X-Agent-Client` and the scope headers each tool already
  uses. `X-Agent-Client` is the one shared name for the client; Tapwright still accepts `X-Tapwright-Client`, and
  Tapline takes only the new one. Without either header, the client is the connection.

### 18.11 A place over the link (Tapline → Tapwright)

- Tapline's host answers `GET /place?app=<bundle id>` on loopback, with the token that guards `POST /agent-touch`:
  `{"app", "route": [...], "tab", "layer", "seenAt": <epoch ms>, "inFront": true}`. Never params.
- The app sends its host a small event whenever its route, tab or layer changes, so the answer comes from memory.
- Tapwright asks each Tapline port (7210–7219) until one has the app, and keeps that port.
- Tapwright uses it only when `seenAt` is later than its own last touch and `inFront` is true; otherwise its own
  signature. A place found this way is stored with its route key.
- (1.13.1) `seenAt` is the host's clock when the event arrived. A place found up front isn't saved until it is visited.

### 18.12 How travel is reported, and journeys

- Every answer that moved by other than the agent's own touches says how: `by link`, `by 3 taps`, `1 reflex`,
  `Road repaired`. A report (§17.5) keeps these, so it can tell a user's path from a shortcut.
- In a user's-eyes journey (§17.1), `drive` may use only roads, and only roads made of taps a person could make:
  no jumps, no reflexes, no fast animations, unless the journey's start allows them (`allow` gains `drive`,
  `reflexes`, `fast_animations`). An interruption is then a finding, as §17 says. (1.13.1) `drive` lifts roads-only (jumps
  are allowed); `reflexes` lifts the reflex ban; `fast_animations` lifts the animation ban.
- In such a journey each move is aimed by the visible text it had (`names.text`), since a person can't see ids. A road
  with no visible text can't be driven there and stops with `off_route`: `it has no text a person could read`.

### 18.13 Web pages

The web tools are specified after their gates pass, not in this version.
