---
name: tapwright
description: Use when testing, exploring or driving an app on a real iPhone through Tapwright (MCP tools such as look, tap, type_text, scroll, wait_for, open_app, alert), when asked for QA, a UI check or a bug hunt on an iOS app on the user's phone, when setting Tapwright up for a user, or when a Tapwright call answers locked, not_allowed, not_actionable, not_answering or reconnecting.
license: MIT
compatibility: Needs the Tapwright MCP server (`tapwright mcp --apps <bundle id>`) and an iPhone that went through `tapwright setup`. Works from Windows, Linux or macOS.
metadata:
  tool: tapwright
  interface: AGENT-TOOLS v1.6
---

# Tapwright: test an app on a real iPhone

Tapwright gives you eyes and hands on the user's iPhone: you read the screen as a list of
elements and act with real touches. This skill is how to do that well. The tools describe
themselves; what follows is what they can't tell you.

## 1. Before you start

- **No Tapwright tools in your tool list?** Set it up first:
  [references/setup.md](references/setup.md).
- **Call `apps` once.** It lists the apps you may drive. On the owner's own phone you were
  given a list of apps and are refused everything else (`not_allowed`). That is by design:
  don't look for a way around it.
- **`locked`** means a person must act: the phone is locked, or it is asking for its passcode.
  You can't pass Face ID, Touch ID or the passcode. Tell the user and wait.
- **A person may be watching** in the live view (`viewer` gives the link) and can take over.
  Their input goes first.
- **Each tool's description lists its arguments.** The calls below show the ones that matter.

## 2. The loop: look, act, read the answer

1. `open_app` starts the app and answers with its first screen.
2. `look` reads the screen: a header, then one line per element.
3. Act on one element. **Every action answers with what changed. Read that before the next
   call**: it usually holds what you were about to look for.

```
tap {"target": {"id": "login-button"}}

Tapped e3 button "Log in" id=login-button @24,610 354x48.
Same screen. Changes:
+ e9 text "Wrong password" @24,560 354x20
~ e2 input "Password" id=password @24,500 354x48 [tap, type] (was "hunter2")
Settled in 0.31 s.
```

The answer already shows the error text, and that the field was emptied. No `look` is needed.

Name the element with one of, in this order of preference: `id` (the app's own identifier),
`ref` (from your last list, like `e3`), `label`, `text`. Use `x` and `y` only for things
with no element.

## 3. Reading an answer

- **The first line** says what was done and to which element.
- **`Same screen. Changes:`** then `+` new, `-` gone, `~` changed (with what it was),
  `↕` moved. When most of the screen changed, the full list comes instead.
- **`No visible change.`** means nothing changed, on screen or off it.
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
  ~ text "taps=2" id=lab-state @16,1400 398x60 (off screen; was "taps=1")
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
  so after a `look` use the new list's refs.
- **Long text is cut** and says by how much: `"…(+57)"`. `wait_for` with the text checks all
  of it.
- **The keyboard:** `Keyboard: shown` or `Keyboard: hidden` in an answer says it came or went.
  While it is up, a touch on anything but the field you are typing in closes it first, as a
  person would, and the answer ends with `Note: closed the keyboard first.` Typing into the
  next field of a form leaves it up.
- **`Note:` lines come last.** Read them: they say when something didn't go the plain way.

## 4. To do this, call that

| To | Call |
|---|---|
| reach an element that is off screen | `scroll` with it as `target` and `to: "target"`. One call; don't scroll by guess |
| wait for a load, a spinner or a new screen | `wait_for` with `text`, `id` or `label` (`gone: true` for a spinner), not `wait` |
| set a switch, or read it | `toggle` with `value: true` or `false`. It does nothing if the switch is already there, and its answer says the switch's state either way |
| fill a field and send it | `type_text` with `submit: true` |
| leave a screen, or close a sheet | `back`. Not every sheet closes that way: if it answers `no_action`, the sheet has only its own close control, and the message names the sheet. Tap that control |
| check that something appeared | read the action's answer; `look` only if it isn't there |
| pick one of several elements with the same text | its `id` if it has one; else add `index` to the target (0-based, in reading order), or use a ref from the `ambiguous` answer's candidates |
| see pixels (layout, colours, images) | `look`, or `screenshot: true` on an action. Pictures cost many tokens: drive by text |
| run steps whose answers you don't need | `look: false` on them, then read the next full answer and its note |
| work with Tapwright and Tapline both connected | inside the app under test, Tapline's tools: its reads and actions are the fast ones. For what iOS draws (permission prompts, system alerts, the share sheet), other apps, real gestures, and the live view: Tapwright's. A ref belongs to the tool that gave it: never pass one tool's ref to the other; use the element's `id` with either |
| answer a system alert (permissions, notifications) | `alert` with `accept` or `dismiss`, the way a new user would unless the test says otherwise. `Alert:` in the header is a system alert; an app's own alert is ordinary elements: tap its button |
| start a run from a clean app | `close_app`, then `open_app` |
| get back when a link opened another app | `open_app` with the app under test: `{"app": "com.example.app"}` |
| drag a slider or a carousel | `swipe` between two points |

**With both tools connected:** when the two tools answer differently, both are right about their own way in: Tapwright does what a finger can do, Tapline what the app's own code allows (Tapline's `back` calls a sheet's own close; Tapwright's needs a gesture the sheet accepts). Test what the user can do with Tapwright; use Tapline to get there faster. Each tool's `was` is from its own last answer: after the other tool acted, read the current value, not the old one.

## 5. Errors

Every failure is `Error <code>: <message>`, and the message says what to do next.

| Code | Do |
|---|---|
| `not_found` | `look`, then pick another target |
| `ambiguous` | use one of the listed refs, or add `index` |
| `not_actionable` | the first word is why: `disabled`, `off_screen`, `covered`, `under_tab_bar`, `behind_alert`, `no_action`. Fix the cause: `scroll` with `to: "target"`, close what is in front, answer the alert |
| `stale` | a person changed the screen since your list: `look` again |
| `busy` | a person is using the phone: wait a few seconds, retry |
| `paused`, `locked` | ask the user (to resume; to unlock or enter the passcode) |
| `not_allowed` | you weren't given this app: go back with `open_app`, or tell the user |
| `not_answering` | the app froze: `close_app`, then `open_app` |
| `reconnecting` | wait a few seconds, then `look` |
| `timeout` | the action may have happened: `look` before you retry |
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
| treating a touch that a notification took as the app's bug | a touch near the top can land on a banner that came down at that moment. Tapwright then opens your app again and says `came to the front after the touch`: look, then do the step again, unless the app was meant to open that app. Report it only if it keeps happening |
| reporting a button that does nothing on one spot only | take a `screenshot`. A grey cursor ring there, or a dark menu with Home and Notification Center, is iOS AssistiveTouch, which the element list doesn't show. Tap well outside the menu once to close it, and tell the user to disconnect the pointer |
