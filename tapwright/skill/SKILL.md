---
name: tapwright
description: Use when testing, exploring or driving an app on a real iPhone through Tapwright (MCP tools such as look, tap, type_text, scroll, wait_for, open_app, alert), when asked for QA, a UI check or a bug hunt on an iOS app on the user's phone, when setting Tapwright up for a user, or when a Tapwright call answers locked, not_allowed, not_actionable, not_answering or reconnecting.
license: MIT
compatibility: Needs the Tapwright MCP server (`tapwright mcp --apps <bundle id>`) and an iPhone that went through `tapwright setup`. Works from Windows, Linux or macOS.
metadata:
  tool: tapwright
  interface: AGENT-TOOLS v1.8
---

# Tapwright: test an app on a real iPhone

Tapwright gives you eyes and hands on the user's iPhone: you read the screen as a list of
elements and act with real touches. This skill is how to do that well. The tools describe
themselves; what follows is what they can't tell you.

## 1. Before you start

- **No Tapwright tools in your tool list?** Set it up first:
  [references/setup.md](references/setup.md).
- **Call `apps` once.** It lists the apps you may drive. On the owner's own phone you were
  given a list of apps and are refused everything else (`not_allowed`), except a permission
  prompt one of those apps raises. That is by design: don't look for a way around it.
- **`locked`** means a person must act: the phone is locked, or it is asking for its passcode.
  You can't pass Face ID, Touch ID or the passcode. Tell the user and wait.
- **iOS's Health access sheet** (HealthKit's "Turn On All" and "Allow") can't be changed by automation: iOS ignores
  the touches. Ask the user to choose and tap Allow, then go on.
- **A person may be watching** in the live view (`viewer` opens it for the user) and can take over.
  Their input goes first.
- **Connect once.** Load Tapwright as an MCP server when the session starts. A wrapper that starts a new process
  for every call adds about 2 s to each call.
- **Each tool's description lists its arguments.** The calls below show the ones that matter.

## 2. The loop: look, act, read the answer

1. `open_app` starts the app and answers with its first screen.
2. `look` reads the screen: a header, then one line per element.
3. Act on one element. **Every action answers with what changed. Read that before the next
   call**: it usually holds what you were about to look for.

```
tap {"target": {"id": "login-button"}}

Tapped e3 button "Log in" id=login-button.
Same screen. Changes:
+ e9 text "Wrong password" @24,560 354x20
~ e2 input "Password" id=password (was "hunter2")
Settled in 0.31 s.
```

The answer already shows the error text, and that the field was emptied. No `look` is needed.

Name the element with one of, in this order of preference: `id` (the app's own identifier),
`ref` (from your last list, like `e3`), `label`, `text`. Use `x` and `y` only for things
with no element.

## 3. Reading an answer

- **The first line** says what was done and to which element, without its position.
- **Short lines:** a `~` line gives `@x,y WxH` only when the element moved or was resized, and
  `[actions]` only when they changed. A button, tab or link can be tapped, an input typed into, a
  switch toggled and a scroll view scrolled; other actions are listed in brackets.
- **A long old value is cut to what changed:** `(was "…delivery on Thursday 14 May")`. A value of
  40 characters or fewer is shown whole.
- **`Same screen. Changes:`** then `+` new, `-` gone, `~` changed (with what it was),
  `↕` moved. When most of the screen changed, the full list comes instead.
- **Read every answer whole.** Never pipe a call through `head`, `grep` or `sed`: the list of what changed is the
  part you need, and cutting it costs a second `look`.
- **`No visible change.`** means nothing changed, on screen or off it. After a `tap` or `long_press` a `Note:` says the
  tap was delivered and for how long the screen was watched, and what to try: a tap again is rarely it.
- **A sheet, an alert or a new screen answers with the full list:** the first line, then
  `Screen …` and the elements that are in front now. What a sheet hides is left out until it
  closes; acting on it answers `covered: … close that first.` When something out of view
  changed as well, it follows the list under `Changes off screen:`.
- **The header counts what you can't see:**
  `Screen 430x932 pt @3x · 26 elements (+41 below, +2 above)`. Those 41 are reached by
  scrolling. They are not in the list: acting on one answers `off_screen` and names the call
  that brings it up.
- **A change out of view has its own line, without a ref:**
  ```
  Same screen. Changes:
  ~ text "taps=2" id=lab-state (off screen; was "taps=1")
  ```
  The action worked. You don't need to scroll there to check.
- **After answers you asked to be short (`look: false`), the next block covers them too:**
  ```
  Same screen. Changes:
  No visible change.
  Note: changes are since your last list, 2 actions ago.
  ```
  A sheet opened by the first action and closed by the second shows as no change. That is
  true of the two together; it doesn't mean the second did nothing.
- **Refs** stay with their elements from answer to answer. Only `look` starts again at `e1`,
  so after a `look` use the new list's refs. A full list (a new screen, or a `run` that ended on one) gives new
  refs too: use those.
- **An empty input** shows `placeholder="…"` (its hint text), never `value=`: `value=` is only what
  the field holds. After typing, its `~` line reads `(was placeholder "Email")`.
- **Long text is cut** and says by how much: `"…(+57)"`. `wait_for` with the text checks all
  of it.
- **The keyboard:** `Keyboard: shown` or `Keyboard: hidden` in an answer says it came or went.
  While it is up, a touch on a control the keyboard hides, or on anything but an input you can
  see, closes it first, as a person would, and the answer ends with `Note: closed the keyboard
  first.` A button the keyboard hides is reached this way, not refused `off_screen`. Typing into
  the next field of a form leaves it up.
- **`Note:` lines come last.** Read them: they say when something didn't go the plain way.
  `type_text` checks that the field holds what you typed; when keys went missing it clears the
  field, types once more and says so in a note.

## 4. To do this, call that

| To | Call |
|---|---|
| reach an element that is off screen | `scroll` with it as `target` and `to: "target"`. One call; don't scroll by guess |
| wait for a load, a spinner or a new screen | `wait_for` with `text`, `id` or `label` (`gone: true` for a spinner), not `wait` |
| set a switch, or read it | `toggle` with `value: true` or `false`. It does nothing if the switch is already there, and its answer says the switch's state either way |
| fill a field and send it | `type_text` with `submit: true` |
| do a known sequence (fill a form, submit, check the result) | `run` with the steps, each an ordinary call, and an `expect` on the step that should change something: `{"tool": "tap", "target": {"id": "save"}, "expect": {"text": "Saved"}}`. One call instead of five; it stops at the first step that fails, says which, and keeps that step's own error code |
| check a switch inside `run` | `"expect": {"id": "alerts", "value": "off"}`: a switch's value is `"on"`/`"off"` (`"true"`/`"false"` work too) |
| get rid of a keyboard that covers what you need | nothing: a touch on something it covers closes it first. If the answer says it stayed up, `type_text` into the field with `submit: true` |
| leave the user a test they can rerun | `record` with `start: true`, do the flow, then `record` with `stop: true` and a `path`. `replay` runs it; people run it in CI with `tapwright replay FILE --junit out.xml` |
| check the screen's accessibility | `audit`. Report each issue it lists with its element and fix; they are real findings for the developer |
| catch a screen that looks different from before | `compare_screen` with a `name`: the first call saves the reference, later ones answer `Matches` or `Differs` and where. `ignore` what always changes (a clock, a timestamp). In a `run` or a recording, `"expect": {"matches": "<name>"}` makes it a check |
| see an animation or a loading state | `filmstrip` (with `tap` to start it), not several `look`s |
| find out why something failed | `logs` with `level: "warning"` (and `since` the last `Latest:`). If an answer says the app crashed, `crash` shows where; put both in your report |
| check that the app is fast enough | `performance` with `launch: true` for opening it, or `tap` for one action. In a `run` or a recording, `"expect": {"under": {"launch_s": 2}}` makes it a check |
| leave a screen, or close a sheet | `back`. Not every sheet closes that way: if it answers `no_action`, the sheet has only its own close control, and the message names the sheet. Tap that control |
| check that something appeared | read the action's answer; `look` only if it isn't there |
| pick one of several elements with the same text | its `id` if it has one; else add `index` to the target (0-based, in reading order), or use a ref from the `ambiguous` answer's candidates |
| see pixels (layout, colours, images) | `look`, or `screenshot: true` on an action. Pictures cost many tokens: drive by text |
| run steps whose answers you don't need | `look: false` on them, then read the next full answer and its note |
| work with Tapwright and Tapline both connected | inside the app under test, Tapline's tools: its reads and actions are the fast ones. For what iOS draws (permission prompts, system alerts, the share sheet), other apps, real gestures, and the live view: Tapwright's. A ref belongs to the tool that gave it: never pass one tool's ref to the other; use the element's `id` with either |
| answer a system alert (permissions, notifications) | `alert` with `accept` or `dismiss`, the way a new user would unless the test says otherwise. `Alert:` in the header is a system alert; an app's own alert is ordinary elements: tap its button. On the owner's phone this works for a permission prompt raised by an app you were given; after your answer the call waits for the app to come back |
| start a run from a clean app | `close_app`, then `open_app` |
| open the app in another language, region or text size (right-to-left layouts, long words, large text) | `open_app` with `language` (`ar`), `region` (`QA`) and/or `text_size` (`xs`…`xxxl`, `ax1`–`ax5`; `l` is the default). It reopens the app for that launch only, and the first line says what it opened with. A launch that fails after the app was closed leaves it closed: look, then open it again. Never go into Settings for this: Settings on the owner's phone shows their name and accounts |
| test a bad network (offline, slow, lossy) | `network` with `mode`: `offline`, `3g`, `lossy`, then `normal`. It is the whole phone's network, every answer carries `Network: …` while it is on, and it clears itself when the session ends. Over Wi-Fi `offline` and `lossy` are `unsupported` (they would cut Tapwright's own link): use `3g`, or a cable |
| get back when a link opened another app | `open_app` with the app under test: `{"app": "com.example.app"}` |
| open a link a person would scan as a QR code (pairing Tapline, an Expo project, a deep link) | `open_url` with the link itself, then answer the app's prompt (Tapline's "Connect for testing?": tap Connect). Ask for the link as text; don't ask the user to scan. A pairing link works once: never log it or put it in a file. On a project's first load, Expo Go (and a development build) opens its developer menu by itself: tap Continue, then Close |
| drag a slider or a carousel | `swipe` between two points |
| see what a touch would do, and whether it needs a person's OK, without touching | `dry_run: true` on `tap`, `long_press`, `toggle`, `type_text`, `swipe`, `scroll` or `back`: it answers `Would tap e12 ...` and sends nothing to the phone |

**With both tools connected:** when the two tools answer differently, both are right about their own way in: Tapwright does what a finger can do, Tapline what the app's own code allows (Tapline's `back` calls a sheet's own close; Tapwright's needs a gesture the sheet accepts). Test what the user can do with Tapwright; use Tapline to get there faster. Each tool's `was` is from its own last answer: after the other tool acted, read the current value, not the old one.

## 4a. Spend fewer calls

Every call costs time. These save the most:

- **Read the change list instead of `look`.** An action's answer says what appeared, went and changed. `look` only
  after a new screen you haven't seen, or when the answer doesn't have what you need.
- **Target by `id`.** An `id` works without a `look` first, still works after the screen changed, and a
  `not_found` lists the closest names. Then `ref`, `label`, `text`; `x` and `y` last.
- **Let the action scroll.** `tap`, `long_press`, `toggle` and `type_text` on an element that is below the fold scroll it
  into view and act, in one call. Don't `scroll` by guess first.
- **Put known steps in `run`.** When you know the next two or more steps (open a sheet, choose, save, check), send
  them as one call, with an `expect` on the steps that matter.
- **Ask for the app's deep links and use them.** `open_url` with the app's own link reaches a screen in one call,
  where taps take several. Ask the user, or the app's docs, which links it has.
- **`look: false`** on steps whose answer you don't need, then read the next full answer.
- **A language, a text size, a bad network** are one call each (`open_app` with `language`, `text_size`; `network`), not a trip through
  Settings.

`run` for the common patterns:

```
# a sheet: open it, choose, save, check
run {"steps": [
  {"tool": "tap", "target": {"id": "filter-button"}},
  {"tool": "tap", "target": {"label": "Newest first"}},
  {"tool": "tap", "target": {"id": "apply"}, "expect": {"text": "Newest first"}}]}
```

```
# a form: fill, submit, wait for the confirmation
run {"steps": [
  {"tool": "type_text", "target": {"id": "email"}, "text": "test@example.com"},
  {"tool": "type_text", "target": {"id": "message"}, "text": "Hello"},
  {"tool": "tap", "target": {"id": "send"}, "expect": {"text": "Thanks", "timeout": 10}}]}
```

```
# a tab tour: each tab matches its saved reference (the first run saves them with compare_screen outside run)
run {"steps": [
  {"tool": "tap", "target": {"id": "tab-home"}, "expect": {"matches": "home"}},
  {"tool": "tap", "target": {"id": "tab-search"}, "expect": {"matches": "search"}},
  {"tool": "tap", "target": {"id": "tab-profile"}, "expect": {"matches": "profile"}}]}
```

## 4b. Usability journeys, reports and test pictures

To judge whether a real user can find and finish a feature, not only whether it works:

- **Build personas for the app's own users and agree them with your user.** The guide and six examples are in
  [references/personas.md](references/personas.md). A persona is guidance for you; nothing enforces it.
- **Run the feature as a journey:** `journey` with `start: true`, a `goal`, a `persona` and its `conditions`. Tapwright
  applies `language`, `region` and `text_size` by reopening the app for the journey (as `open_app` does) and `network`
  through the `network` tool, and undoes them at the stop. Use only what a user sees: refs and visible text, no ids.
- **Check each new screen with `ux_check`** and report its facts as findings. Add your own judgment from a screenshot,
  and say it is judgment. If the user has house rules, `ux_check` lists the written ones for you to judge.
- **Stress moments and the end:** `stress` tests leaving the app, a bad network, a double tap, rotating, relaunching.
  `journey` with `stop: true` and `reached` writes the visual report and answers its summary; pictures stay in the
  report folder. Offer `export` only if the user asks.
- **Photo pickers:** ask your user first whether test pictures may be added to the phone's photo library, and whether
  to remove them afterwards. Then `media` with `add` (paths) and `confirm: true`; `remove: true` deletes only those.
  Without full access, `remove` names what to delete by hand.

## 5. Errors

Every failure is `Error <code>: <message>`, and the message says what to do next.

| Code | Do |
|---|---|
| `not_found` | `look`, then pick another target |
| `ambiguous` | use one of the listed refs, or add `index` |
| `not_actionable` | the first word is why: `disabled`, `off_screen`, `covered`, `under_tab_bar`, `behind_alert`, `no_action`. Fix the cause: `scroll` with `to: "target"`, close what is in front, answer the alert. `covered` also means a row under the status bar with too little showing below it: `scroll` it down |
| `stale` | a person changed the screen since your list, or (a touch at `x`, `y`) the picture there changed since your last look: `look` again |
| `busy` | a person is using the phone: wait a few seconds, retry |
| `paused`, `locked` | ask the user (to resume; to unlock or enter the passcode) |
| `not_allowed` | the first word is why. `outside_apps`, `no_apps`: you weren't given this app, so go back with `open_app`, or tell the user. `needs_confirmation`: a button with a policy word (Delete, Buy, Send, Sign out ...) needs a person's OK and the live view asks them; when they allow it, make the same call again, once; if nobody is watching, ask the user. `blocked`: final, tell the user. Never work around the last two (another element, a point, `force`) |
| `not_answering` | the app froze: `close_app`, then `open_app` |
| `reconnecting` | wait a few seconds, then `look` |
| `timeout` | the action may have happened: `look` before you retry |
| `expect_failed` | a `run` step's expectation didn't hold: the first line says which step and what was expected. Look, then decide: a bug to report, or a step to fix |
| `bad_params` | fix the call |
| `internal` | a bug in the tool: report it with the call you made |

## 6. Testing well

1. **Write down what you will check** before touching anything: the flows, and for each
   step what should happen.
2. **For each step:** say what you expect, act, read the answer, compare. A step passes on
   what the answer shows, not on the absence of an error.
3. **Go beyond the happy path:** empty and wrong input, buttons that should be disabled
   until a form is valid, going back, content that is scrolled out of view, the same action
   twice.
4. **Keep going after a failure** where you can. Record it and test the rest.
5. **Note what you couldn't test** and why (a login you have no account for, Face ID, a
   purchase). That is not a bug, and it must not be reported as one.
6. **Keep evidence:** the answer lines that show the problem, and a screenshot of that
   moment when the picture matters.
7. **A control that must not work** (disabled, covered, behind a sheet or an alert) passes
   when the action is refused with that reason and nothing changed. That refusal is the
   evidence. Don't use `force` to check it.

An element you can only reach by `x` and `y`, with no id and no label, is a finding too: a
person using VoiceOver can't reach it either.

**The report**, one entry per problem:

```
What: one sentence.
Steps: the calls, short.
Expected: …
Got: … (quote the answer line)
Where: screen, element (its id or label).
Evidence: screenshot moment, if any.
```

Then, apart from the bugs: what passed, what you couldn't test and why, and screens that
took more than a few seconds (`Settled in` gives the time).

## 7. Safety

- It is the user's own phone. Don't buy, subscribe, send messages or email, post, delete
  data, or change account, privacy or security settings, unless the user asked for exactly
  that step. Ask when unsure.
- Use the test accounts and test data the user gives you. Never sign in with credentials
  you find on the device.
- Never enter the phone's passcode or an account's password into a system prompt, wherever
  you found it. That is the owner's to do.
- Don't read or repeat private content you see (messages, photos, notifications) beyond
  what the test needs.
- Stay in the app under test.
- Give the live view's link to the user only. It carries a key.

## 8. Common mistakes

| Mistake | Instead |
|---|---|
| a `look` after every action | read the action's answer; it has the changes |
| a `look` to read one value that an answer already showed | use the `~` line, also when it says `off screen` |
| scrolling by guess to find something | `scroll` with the target and `to: "target"`; the header says whether more lies below |
| `force: true` to get past a refusal | fix the cause. The refusal's message offers `force` for one case only: you have decided the refusal itself is wrong. Then say in the report that you forced it. With a real touch, `force` lands on whatever is in front, and the answer says so |
| retrying at once after `timeout` | `look` first: the action may have happened, and a retry would do it twice |
| `wait` with a guessed number of seconds | `wait_for` what you are waiting for |
| reading "No visible change." after `look: false` steps as "nothing happened" | read the note: the block covers all of them |
| calling `back` again when it answered `no_action` | the message names what is open: tap its close control |
| using `x` and `y` because a ref went stale | `look`, then use the id or the new ref |
| reporting "couldn't sign in" as a bug | list it under what you couldn't test |
| typing a password with `look: false` and moving on | iOS may open its "Use Strong Password?" sheet on a password field, and the keys typed meanwhile can go missing. Read the answer's `Value is now`; if the sheet is up, tap "Choose My Own Password" (never accept a suggested password: it would be saved to the owner's keychain), then type again |
| leaving the app when iOS asks for a permission ("Would Like to Send You Notifications") | the prompt is the app's: answer it with `alert`, as a new user would. The header shows the home screen behind it; the app is still under it |
| treating a touch that a notification took as the app's bug | a touch near the top can land on a banner that came down at that moment. Tapwright then opens your app again and says `came to the front after the touch`: look, then do the step again, unless the app was meant to open that app. Report it only if it keeps happening |
| reporting a button that does nothing on one spot only | take a `screenshot`. A grey cursor ring there, or a dark menu with Home and Notification Center, is iOS AssistiveTouch, which the element list doesn't show. Tap well outside the menu once to close it, and tell the user to disconnect the pointer |
