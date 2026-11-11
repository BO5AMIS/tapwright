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
a form leaves the keyboard up, and both tools say `Keyboard: shown` and `Keyboard: hidden`. Each repo keeps an
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
Tapped e3 button "Log in" id=login-button @24,610 354x48.
Same screen. Changes:
+ e13 button "Done" @24,700 354x48 [tap]
- text "Loading"
~ e5 text "3 items" (was "2 items")
↕ 7 elements moved
Settled in 0.31 s.
```

- **The first line** is the verb in the past tense, then the element's line **without its
  `[actions]`**, then a full stop: `Tapped e3 button "Log in" id=login-button @24,610 354x48.`
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
    checked, selected, disabled and actions. **A frame never appears in `~`**;
  - `~ <element line without a ref> (off screen; was "<old>")` for an element that lies above
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

`<ref> <role> "<text or label>" [label="…"] [id=…] [value="…"] [disabled] [selected] [checked=true|false] @x,y WxH [actions]`

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
the front after the touch: a notification banner over the top of the screen can take a touch there. Went
back to Example (com.example.app); the touch may not have reached it. Look, then try again.` A permission
prompt the touch raised doesn't count. Tapline's touches go through the app's own touch system, so a
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
    e46 tab "Nutrition" [tap]
  Retry with one of the refs, or add "index" (0-based).
  ```
  Up to 5 candidates are listed, each indented two spaces. If there are more, one further
  indented line follows: `  (+N more; use a ref or "index")`.
- For `not_actionable`, the reason is the message's first word, so scripts can read it
  without a new field: `disabled`, `off_screen`, `covered`, `under_tab_bar`,
  `behind_alert` or `no_action`. For example:
  `Error not_actionable: covered: e47 button "Save" is covered by e45 button "Cancel"; close it or pass force: true.`
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

| Code | Meaning | What the agent does | Raised by |
|---|---|---|---|
| `not_found` | nothing matches the target, or the ref isn't on the last list | look, then pick another target | both |
| `ambiguous` | several matches and no `index` | use one of the candidates' refs, or add `index` | both. **Change Tapwright:** it picks silently today |
| `not_actionable` | the element can't take the action as a real touch would: `disabled`, `off_screen`, `covered` (by what), `under_tab_bar`, `behind_alert`; or it doesn't take that action at all: `no_action` | fix the cause (`scroll` with `to: "target"`, close the alert), or pass `force: true`; for `no_action`, pick another element or tool | both. Tapwright today: none. Plan: `disabled`, `behind_alert` and `no_action` first; `covered` is a gap |
| `stale` | a `ref` from a list made before a person acted, from the viewer or (v1.5) with a finger on the phone | look again | both. **Change Tapline:** the host remembers "a person acted since the agent's last list". A look, or an action answer carrying a full list, clears it, and the next answer gets `Note: a person used the app since your last look.` (Tapwright says "the phone"). `id`, `label` and `text` targets keep working |
| `busy` | a person drove for the whole ~30 s wait | wait a few seconds, retry | both (VIEWER-PROTOCOL "Control") |
| `paused` | the user paused the agent | ask the user to resume | both |
| `locked` | the phone is locked | ask the user to unlock it | Tapwright |
| `in_background` | the app is in the background, and only the person can bring it back | ask the user to open the app | Tapline |
| `not_answering` | the app in front stopped answering (v1.4) | close the app and open it again (`close_app`, then `open_app`, where the tool has them), or ask the user to | both. Tapwright: the app doesn't answer accessibility queries; `close_app` still closes it. Tapline: the app is connected but didn't answer a call, or sent nothing for 45 s, while not in the background |
| `reconnecting` | the link dropped and is coming back | wait a few seconds, then look | both. Tapline's `disconnected` maps here |
| `not_connected` | nothing to drive: no app connected, or no phone found | follow the setup steps in the message | both. Replaces Tapline's `no_app` and `unknown_session` |
| `timeout` | no answer in time; the action may have happened | look before retrying | both. Tapwright adds two messages for a phone that gave one request no answer while the line is alive (over Wi-Fi a single request can stall). After a touch: `the phone didn't answer within <N> s; the action may have happened: look before you retry.`, after the touch's own length plus 10 s; the session stays. From `wait_for`, when not one read of the screen came back in its time, or in 3 s if that is longer: `The phone didn't answer within <N> s, so it isn't known whether <kind> "<value>" is on screen; look, then wait again.` It says "it didn't happen" only after it has read the screen at least once |
| `bad_params` | the call's arguments are invalid | fix the call | both. **Change Tapwright:** it uses `unsupported` for this today |
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
| `scroll` | `direction` (default `down`), `amount` (0.1–1.0 of the scroll area, default 0.8), `to` (`start`, `end` or `target`), `target`, `look`, `screenshot` | "down shows what's below". **`to: "target"` (v1.6)** scrolls the scroll views that hold the `target` element until its whole rect is on screen and clear of the bars, 8 pt inside the edge it came in over (an element larger than that space is brought to its start). It answers `Scrolled <scroll view line> to <element line>.` and the changes; when it stops before the element is clear, the first line says `toward` in place of `to`, so it never reads like success. An element that is clear already: nothing is scrolled, the first line is `<element line> is already on screen.`, and the change block follows. Likewise `to: "start"` or `"end"` with the scroll view already there: nothing is dragged, and the first line is `<scroll view line> is already at its start.` (or `end`), with the usual answer after it. Without a target, with a point as target, or with a `direction`, it is `bad_params`. When it stops before the element is clear: `Note: it doesn't scroll further: the target is as far into view as it gets.` (the end of the scroll view), or for Tapwright's drags `Note: the target wasn't reached in 12 s; scroll again to go on.` A tap still doesn't scroll by itself. A row a lazy list hasn't made yet is `not_found`. **Change Tapline:** `amount` is in points today. **Change Tapwright:** `amount` is a share of the screen today; it adds `target` (scroll inside that element) and `to` (repeat until nothing changes) |
| `toggle` | `target`, `value`, `force`, `look`, `screenshot` | **Change Tapwright:** new. It reads the switch's value and taps only if it differs |
| `back` | `look`, `screenshot` | **Change Tapwright:** new. It taps the navigation bar's back button, else swipes from the left edge, else `unsupported`. When a sheet, modal or popover is open on top, `back` closes that first, as "up one screen", and its first line is `Closed the sheet.` (v1.4). Tapwright taps outside a popover, on its dismiss region; Tapline calls the top Modal's `onRequestClose`. A sheet without one that closes on a tap on its backdrop gets that tap from both tools (v1.6); one with neither answers `no_action`, and the message names the sheet |
| `open_url` | `url`, `look`, `screenshot` | Tapline opens it inside the app; Tapwright opens it on the phone, so another app may take it (the answer says which app came up) |
| `wait` | `seconds` (0.1–30, required), `look`, `screenshot` | answers `Waited 1.0 s.` and the change block, with no settle line. Out-of-range values are `bad_params`. **Change Tapwright:** today it clamps silently and always takes a screenshot |
| `wait_for` | one of `text`, `id`, `label`; `gone`; `network_idle`; `timeout` (0.5–30 s, default 10); `look`, `screenshot` | `Done after 1.2 s.` plus the changes, or `timeout` (`Waited <N> s for <kind> "<value>" to appear; it didn't happen.`, or to disappear; Tapwright has a second message, see `timeout` in §4). **Change Tapwright:** new. It polls the screen, its looks are slow (see QUALITY-BAR), and `network_idle` is `unsupported` |
| `apps` | none | the apps the agent can drive, with the current one marked. Tapline: the connected apps. Tapwright: the installed apps. **Change Tapwright:** `list_apps` becomes `apps` |
| `open_app` | `app` (id or name) | Shared description: "Drive this app from now on." Tapline selects that connected app (today's `select_app`). Tapline can't launch apps, so when the app isn't connected it answers `not_connected` with how to connect it, and the description says so. Tapwright launches the app or brings it to the front (today's `launch_app`). An exact id wins (Tapline: a session id; Tapwright: a bundle id). Otherwise names match ignoring case. Several matches give `Error ambiguous: N connected apps match "X". Pick one by its session id:` (Tapwright: `installed apps` and `bundle id`), followed by their `apps` lines |
| `viewer` | none | the live view's link. **Change Tapwright:** new; today only `tapwright view` gives it |

### 5.2 Extras: only where a tool can do them, same conventions

| Tool | Where | Why only there |
|---|---|---|
| `close_app` (today's `terminate_app`) | Tapwright | Tapline lives inside the app |
| `alert` (`accept`, `dismiss`, `text`) | Tapwright | Tapline can't reach OS alerts |
| `press_button` (`home`, `volumeUp`, `volumeDown`) | Tapwright | hardware buttons are outside the app |
| `rotate` (`portrait`, `landscape_left`, `landscape_right`) | Tapwright | turning the phone is outside the app. Both tools follow a turn, whoever makes it |
| `swipe` (`x0, y0, x1, y1, seconds`) | both (Tapline since v1.6) | Tapwright makes a real drag. Tapline drags through React Native's own touch system (a pan view, a sheet's swipe to close); a gesture that react-native-gesture-handler or a native view handles (sliders, maps), or a drag along a native scroll view, answers `no_action` there and its message names what can make it |
| `check_layout` | Tapline now; Tapwright later, partly | needs layout data. Tapwright can check tap-target sizes, overlaps and contrast from the accessibility tree and the screenshot, but not truncation |
| `compare_screen`, `filmstrip` | Tapline now; Tapwright in part 2 | both are image work. Tapwright's 22 fps stream suits `filmstrip`; part 2's planned `watch` becomes `filmstrip` |
| `record`, `replay` | Tapline now; Tapwright later | scripts by id, label or text work for both |
| `logs`, `reload`, `app_command`, `pair` | Tapline | they need the app's JS runtime |

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
- Read-only tools set `readOnlyHint`: `look`, `apps`, `viewer`, `wait`, `wait_for`, `logs`,
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
