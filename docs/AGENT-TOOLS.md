# Agent tools: one interface for Tapline and Tapwright

Shared by Tapline and Tapwright. Version 1, agreed by both projects and approved by the user
on 2026-11-09. Version 1.1 (2026-11-09) adds the `not_actionable` reason `no_action`, also
agreed by both projects and approved by the user. Version 1.2 (2026-11-09) spells out the
exact answer and message wording that both tools found while building and cross-checking on a
real phone, also agreed by both projects and approved by the user. Version 1.3 (2026-11-09) adds
what the second cross-check found: duplicate folding, bidi marks, `ambiguous` wording, inputs'
actions, touching the visible part, and the cap note. Both projects agree on it, and the user approved it on 2026-11-09. Each repo keeps an identical copy in `docs/`. It changes only by agreement
between the two projects and the user's approval, like `VIEWER-PROTOCOL.md`. Each tool's
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
| `{label: "Log in"}` | the accessibility label, exact, ignoring case | **Change Tapwright:** no substring fallback here; that's `text` |
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
  `Keyboard: hidden` (Tapwright), and `Network: N requests in flight` (Tapline). `Note:`
  lines stay at the end of the answer. Then:
  - `+ <element line>` for a new element, with its ref and `[actions]`;
  - `- <role> "<text>"` for a gone element, with no ref and no position. Without text it's
    `- <role> id=<id>`, and without that, `- <role>`;
  - `~ <element line> (was "<old>")` when exactly one field changed. When several changed:
    `(was text="…", value="…")`, naming each one. The fields are text, label, value,
    checked, selected, disabled and actions. **A frame never appears in `~`**;
  - `↕ <element line>` for a frame-only change of more than 1 pt in x, y, width or height,
    size included. Up to 5 are listed; more become the single line `↕ N elements moved`;
  - `No visible change.` when nothing changed.
- **The baseline** is the agent's list as of its last answer: the last `look`, or the list
  carried through every action answer since. It's never just the last full list. Scroll
  down and then back up gives `↕` both times.
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
Screen 430x932 pt @3x · 26 elements
App: Settings (com.apple.Preferences)
Route: Home › Profile (id="42")
Alert: "Allow notifications?" [Don't Allow] [Allow]
Keyboard: shown
Network: 2 requests in flight
```

- The first line is always there. The others appear only when they apply and the tool knows
  them.
- `Route:` and `Network:` are Tapline only.
- `Keyboard:` is Tapwright only. Its keys aren't listed; type with `type_text`.
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
  - Text is clipped to 80 characters, with whitespace flattened, JSON-quoted.
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
- A full list renumbers from `e1`.
- After an action, elements still on screen keep their refs, and new ones get new numbers.
- A ref from before a full list is invalid.
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
- **Touch the visible part.** Before refusing with `covered` or `under_tab_bar`, a tool clips
  the element to the screen and to what's outside the bars and covers drawn over it. If the part
  left is at least 10 pt high and 10 pt wide, the tool touches its centre (Tapline hit-tests
  it), as a person would. Example: a card at y 808–898 with the tab bar from 849 is tapped at
  y ≈ 828. The tool refuses only when that part is smaller. `force: true` acts at the
  element's own centre.
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
      `Candidates:`. A tool that knows focus may add "and none is focused".
  - **`no_action` reasons:** `has no tap action`, `has no long-press action`,
    `is not a switch`, `is not an input`, `is not inside a scroll view`. At a point:
    `Nothing at (x, y) takes <a tap, typing, a toggle, a scroll>.`

| Code | Meaning | What the agent does | Raised by |
|---|---|---|---|
| `not_found` | nothing matches the target, or the ref isn't on the last list | look, then pick another target | both |
| `ambiguous` | several matches and no `index` | use one of the candidates' refs, or add `index` | both. **Change Tapwright:** it picks silently today |
| `not_actionable` | the element can't take the action as a real touch would: `disabled`, `off_screen`, `covered` (by what), `under_tab_bar`, `behind_alert`; or it doesn't take that action at all: `no_action` | fix the cause (scroll, close the alert), or pass `force: true`; for `no_action`, pick another element or tool | both. Tapwright today: none. Plan: `disabled`, `behind_alert` and `no_action` first; `covered` is a gap |
| `stale` | a `ref` from a list made before a person acted from the viewer | look again | both. **Change Tapline:** the host remembers "a person acted since the agent's last list". A look, or an action answer carrying a full list, clears it, and the next answer gets `Note: a person used the app since your last look.` (Tapwright says "the phone"). `id`, `label` and `text` targets keep working |
| `busy` | a person drove for the whole ~30 s wait | wait a few seconds, retry | both (VIEWER-PROTOCOL "Control") |
| `paused` | the user paused the agent | ask the user to resume | both |
| `locked` | the phone is locked | ask the user to unlock it | Tapwright |
| `in_background` | the app is in the background, and only the person can bring it back | ask the user to open the app | Tapline |
| `reconnecting` | the link dropped and is coming back | wait a few seconds, then look | both. Tapline's `disconnected` maps here |
| `not_connected` | nothing to drive: no app connected, or no phone found | follow the setup steps in the message | both. Replaces Tapline's `no_app` and `unknown_session` |
| `timeout` | no answer in time; the action may have happened | look before retrying | both |
| `bad_params` | the call's arguments are invalid | fix the call | both. **Change Tapwright:** it uses `unsupported` for this today |
| `unsupported` | this tool, app or phone can't do it | use the alternative named in the message | both |
| `internal` | a bug | report it | both |

## 5. Tools

### 5.1 Shared core: both tools offer all of these

| Tool | Parameters | Notes |
|---|---|---|
| `look` | `screenshot`, `width`, `labels` | the full list (§3) |
| `tap` | `target`, `force`, `look`, `screenshot` | **Change Tapline:** `press` becomes `tap`, to match the viewer protocol, Appium and Maestro, and "what a real tap would do" |
| `long_press` | `target`, `seconds` (default 1.0), `force`, `look`, `screenshot` | Tapline ignores `seconds` and calls the handler |
| `type_text` | `text`, `target`, `replace` (default **true**), `submit`, `look`, `screenshot` | answers `Typed into <el>. Value is now "…"`. Without a target: the focused field, else the only field, else `ambiguous`. **Change Tapwright:** `replace` defaults to false today, and it doesn't echo the value |
| `scroll` | `direction` (default `down`), `amount` (0.1–1.0 of the scroll area, default 0.8), `to` (`start` or `end`), `target`, `look`, `screenshot` | "down shows what's below". **Change Tapline:** `amount` is in points today. **Change Tapwright:** `amount` is a share of the screen today; it adds `target` (scroll inside that element) and `to` (repeat until nothing changes) |
| `toggle` | `target`, `value`, `force`, `look`, `screenshot` | **Change Tapwright:** new. It reads the switch's value and taps only if it differs |
| `back` | `look`, `screenshot` | **Change Tapwright:** new. It taps the navigation bar's back button, else swipes from the left edge, else `unsupported` |
| `open_url` | `url`, `look`, `screenshot` | Tapline opens it inside the app; Tapwright opens it on the phone, so another app may take it (the answer says which app came up) |
| `wait` | `seconds` (0.1–30, required), `look`, `screenshot` | answers `Waited 1.0 s.` and the change block, with no settle line. Out-of-range values are `bad_params`. **Change Tapwright:** today it clamps silently and always takes a screenshot |
| `wait_for` | one of `text`, `id`, `label`; `gone`; `network_idle`; `timeout` (0.5–30 s, default 10); `look`, `screenshot` | `Done after 1.2 s.` plus the changes, or `timeout`. **Change Tapwright:** new. It polls the screen, its looks are slow (see QUALITY-BAR), and `network_idle` is `unsupported` |
| `apps` | none | the apps the agent can drive, with the current one marked. Tapline: the connected apps. Tapwright: the installed apps. **Change Tapwright:** `list_apps` becomes `apps` |
| `open_app` | `app` (id or name) | Shared description: "Drive this app from now on." Tapline selects that connected app (today's `select_app`). Tapline can't launch apps, so when the app isn't connected it answers `not_connected` with how to connect it, and the description says so. Tapwright launches the app or brings it to the front (today's `launch_app`). An exact id wins (Tapline: a session id; Tapwright: a bundle id). Otherwise names match ignoring case. Several matches give `Error ambiguous: N connected apps match "X". Pick one by its session id:` (Tapwright: `installed apps` and `bundle id`), followed by their `apps` lines |
| `viewer` | none | the live view's link. **Change Tapwright:** new; today only `tapwright view` gives it |

### 5.2 Extras: only where a tool can do them, same conventions

| Tool | Where | Why only there |
|---|---|---|
| `close_app` (today's `terminate_app`) | Tapwright | Tapline lives inside the app |
| `alert` (`accept`, `dismiss`, `text`) | Tapwright | Tapline can't reach OS alerts |
| `press_button` (`home`, `volumeUp`, `volumeDown`) | Tapwright | hardware buttons are outside the app |
| `swipe` (`x0, y0, x1, y1, seconds`) | Tapwright | Tapline calls handlers and can't make raw gestures |
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
