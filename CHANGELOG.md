# Changelog

## 1.9.0, "the Token Update"

Fewer calls per task (AGENT-TOOLS 1.17 §30). Nothing is taken away: every 1.8.5 call, argument and answer still
works, and `--no-hints` gives 1.8.5's behaviour back.

- Routes save themselves: the way a session went from the app's start to a screen (taps, back, open_url and the scrolls
  before them) is kept as that screen's route in the app's map folder (`"auto": true`), with no `record` call, and
  `drive {"route": "<name>"}` replays it in one call. A route is named by the name given with `map {"name"}`, else by
  the id of the move that reaches the place, else `place_<n>`: never by a label or a title. Navigation only: any typing
  (in a `run` too), a toggle, a tap that stays, a touch the policy held, and a tap that leaves a form by a control that
  may submit it break the way from there; a person's route carries it on only when all its steps are navigation. One
  route per place: a shorter way replaces it, one whose own steps fail twice is dropped (not reaching its start never
  counts), and at most 50 per app. A person's route is never replaced.
- The first answer of a session that shows an app says what it can reach in one call with nothing to give, the most
  used first, else the most recently reached: `Known: plan_trip, dest_card_kyoto (one call each: drive {"route":
  "<name>"})`. A name the redaction would change is left out; none in a user's-eyes journey.
- The MCP instructions begin with the shared token rules: known steps in one `run` with an `expect`, target by id, a
  screenshot only to judge looks (`screenshot: false` on a `look` that only reads), use `Known:`, hand a long task's
  driving to one small agent, a bug report is `bug_report`.
- `Hint:` lines in the shared words, at most one per answer and each once per session (a daemon restart starts the
  session again), none on a safety refusal or a pause, in the order logs > known way > form > screenshot > long task: the
  app's own logged error or a crash since `logs` was last read (never a tool error), three taps along a known way, a
  form opened (2 or more inputs and a button), a screenshot the text already covered, a long task (from the 20th call,
  worded so a helper agent carries on).
- A tap whose target the keyboard covers presses a Done when a touch outside, a drag and the dismiss key don't close it
  (Wayfare's trip form, whose last field is multiline): only a button named exactly Done, and only an input accessory's
  attached to the keyboard, or the keyboard's own Done key while the focused field is multiline and holds a readable
  value, and only when the tap's target is a submit button the floor doesn't hold (Save trip after Notes). Never return, Go, Send, Search, Next or Join. A line break Done adds at the end is taken back with one
  backspace; any other change is left alone; the answer says either. Otherwise the tap is refused as in 1.8.5, with a
  hint that Return can submit.
- `--no-hints` (on `tapwright mcp`, `serve` and `call`, or `TAPWRIGHT_NO_HINTS=1`): no `Known:` or `Hint:` lines, no
  route saved by itself and none shown, no Done press; `mcp --no-hints` also gives 1.8.5's instructions.
- Tests keep the tool's home in a folder of their own: a daemon test no longer writes maps or reports to
  `~/.tapwright`.

## 1.8.5 (2026-11-19), "the Bug Report"

- New tool `bug_report` (and `tapwright bug`): one page for whoever fixes a bug, readable in a minute and holding nothing
  private (AGENT-TOOLS 1.16.2 §29). A folder per bug under `~/.tapwright/bugs/<app id>/<bug id>/` with `report.md`,
  `report.html` (the same page, one file, no script, every text escaped), `repro.json` (the finding's script, `${typed}`
  for any typed value), `finding.json` (the finding with its `bug` record) and, only when allowed, `picture.jpg`. The page:
  title, what happened, how to see it (steps written for a person, the last 15), reproduced, environment, where (the place
  and its key's signature, the top 8 native frames with the app's own marked `*`, addresses and offsets dropped, and a
  system sheet that was in front by its kind only), logs (20 lines at most), picture, seen before.
- Three sources: a finding of an explore report (`finding`, `run` defaulting to the newest report of the app in front),
  this session's last `run` or `replay` that stopped `expect_failed` (`from: "failure"`), and what you saw while driving
  (`from: "now"` with a one-line `note` and `expected`: the last 10 touches of the app become the steps; a touch that
  can't be named, such as a person's in the live view, starts the path after it; `unsupported` with `serve --no-log`).
  `bug` shows a kept bug again, and `with_picture: true` adds its private picture while its source file still exists.
- One bug, many sightings: the signature is the first 16 hex of a SHA-256 over the kind, the place's KEY (never its name:
  a place is `place_<n>` until named, and an explore report now keeps each finding's place key, so a renamed place keeps
  its bug), the first line of `got` with numbers, times, dates, hex and UUIDs as `#`, and for a crash its exception type
  and top app frame's symbol. The same signature again adds to `seen` (count once per source, last time, builds) and
  rewrites only the page's `Seen before` line. A finding with no place key is never merged. The newest 100 bugs of an app
  are kept. `tapwright bug list [--app ID]` lists them.
- Redaction at every sink (§29.6): one single-pass redactor over every vault value in every form, the one-time codes and
  every value typed in this session (registered before it is typed; the explorer's public fake values are not), applied
  before any cut, to the page, both files, the script, the finding, the title, the answer and the CLI. Paths are cut to
  file names (no user folder). Before anything is written every text file is grepped for every form of every value and
  every 12-character piece of each; a hit writes nothing and names the file, never the value. A redactor that fails hides
  the text. A run folder is read only inside the explore home, and each file it names only inside it.
- The reproduction check (§29.4): `verify` (1 to 10 runs, `true` for 3; by default 3 for a finding or a failed check)
  relaunches the app (its data kept), drives to the script's start over the map and runs `repro.json`, counting the runs
  that end with the same signature: `3 of 3 runs`, `1 of 3 runs (flaky)`, or `0 of 3 runs: not reproduced`, which keeps
  the bug. Every move is the agent's own call through the ONE dispatch gate of 1.8.0 (the skip floor, judged on the
  element the touch really goes to after resolution and scroll), with nobody asked: a typed step or a step whose target
  holds a floor or policy word stops the check before anything is touched (`not checked (step 3 needs a person (a typed
  value))`), and a word only the actual recipient holds stops it at dispatch with no move of that step. A person, Pause
  or a locked phone stops it (`stopped (busy)`); no explore starts while it runs. A check that can't run never stops the
  bug from being made: the page says why (`no reset reached a known place`, `an unreachable place has no steps`, `reset
  "app"` that Tapwright has no `app_command` for, `no phone connected`). A later check replaces the `Reproduced` line and
  the reset line. `tapwright bug verify <bug id> [--verify N]` and `tapwright bug <folder> <finding> --verify N` check
  through the daemon; exit 1 when it didn't reproduce, 2 when a person or a refusal stopped it.
- Export (§29.7), only when your user asked: `{"to": "zip"}` writes `<bug id>.zip` next to the folder, with no network, a
  fixed time and no user name or path; `{"to": "issue", "repo": "owner/name", "confirm": true}` opens a GitHub issue with
  the user's own `gh` (title the page's, body `report.md`), and commits `repro.json` (and the picture when it may go) to
  `qa-reports` under `.tapwright/bugs/<app folder>/<bug id>/`, so two apps' bugs with one id never share files. Without
  `confirm`: `not_allowed: needs_confirmation`; a public repository, or one whose visibility can't be read, needs `public:
  true`. A private picture goes only with the export's own `with_picture: true`. Every text sent is grepped again with the
  live values and the export is refused if any is found (`export refused: a vault value was found in report.md.`, never
  the value), nothing sent. A new audit kind `export`: one line per export, a refused one too, its detail the target and
  the bug id, never content; with no audit line nothing is sent. An export that fails after its line was written (a
  zip that can't be written, an issue `gh` refuses) adds a second line with that outcome. `tapwright bug export <bug id> --to zip|issue [--repo
  owner/name] [--public] [--with-picture] [--yes] [--app ID]` asks `Send to GitHub? [y/N]` (`--yes` without a terminal).
- Privacy (review R185): a link is kept only as the pattern of a place's link it matched, never its query, fragment or a
  token in it (one that matches none starts the path after it); a row of a list with no id is `the 1st row of the list`,
  never its text; paths are cut in `got`, `expected` and the note too (`file://` URLs and UNC paths as well, a path ending
  at a user folder is `~`). A bug of an app that isn't one of the daemon's test apps (`serve --apps`) keeps no screen text
  and no log lines beyond its own normalized `got`, its picture is private, and any export of it, a zip too, needs
  `confirm`; its page says why. An explore finding records the system sheet that was in front (`system`), and a picture
  taken with one in front is private for any app. The issue's body escapes `@` and `#`, so it mentions nobody.
- Safety of the folder: changes are made under one lock per app (shared with the command line); a check reads the bug again
  before it writes, so a sighting made meanwhile is kept and a removed bug isn't made again; retention never removes a bug a
  check holds; an update re-reads the kept bug through the redactor, so a value added to the vault since is hidden. A failed
  run's steps are kept as replayed (their arguments and `expect`), so its check can reproduce it. While a daemon runs, the
  command line's make and export go through it (its live values); without one, GitHub needs `--accounts` and the answer
  says which values were checked.

- Phone checks (W177): `verify` works on a development build. After the relaunch the check waits (up to 30 s) until the
  app's screen is no loading screen (`Loading from Metro…`, `Downloading n%`, a spinner, a blank screen), then drives to
  the start, and tries the way once more when the screen isn't the start (a link opened while the app was loading was
  lost, so every run was `not checked (no reset reached a known place)`); a check that met the deadline says `the app was
  still loading after 30 s`. `explore` waits the same way before its first read and after each relaunch (started on the
  loading screen it answered `0 places, frontier empty`).
- `bug_report from: "failure"` finds the failure a thin `tapwright call run` left: each thin call is a process with a client
  of its own (its parent process), so the run and the report were two clients. A thin call now also takes the last failed
  check a thin call left; a failure that can't be kept is said in serve.log instead of being swallowed. A thin call's
  first touch also knows the place the last thin read left, so `from: "now"` pages start `Start on <place>.`
- Explore reports no longer keep typed field values: an element line of a text field (an accessibility or usability issue
  quoting `id=bug-note value="…"`) shows `value=<field value>` in `report.md`, `report.json` and every bug file.
- Pages: an accessibility or usability bug's title is its rule and target (`Contrast: id bug-note`); `from: "now"` logs
  are the app's own lines (no XCTest or system framework lines), the same link opened twice in a row is one step, and a
  bug no check was asked for says `Reproduced: not checked.`. The count of a bug is of sightings (two explore runs: count
  2), with how often they saw it in all kept as `seen.occurrences`.
- The phone's lock is seen again: the runner (`tw-16.15.3.8`, version 8; `tapwright setup --force` installs it) reads the
  lock state from iOS on every check. It kept a flag only iOS's change notices set, so a locked phone could read as not
  locked (the owner's lock went unseen and `unlock` answered "The phone isn't locked."), and an unlocked one with
  Notification Center open as locked.
- `unlock` finds iOS 26's passcode keypad (seen on a Face ID iPhone with iOS 26.5, where it answered that the
  passcode entry couldn't be found): the keypad is on the screen but under a view whose frame lies outside it, so
  the tree listed every key as off the screen. Such a keypad (each digit's key once, every key's centre on the screen,
  every other check as before) is used only after the runner's hit test confirms that touches at four keys (always 1,
  3, 5 and 0, whatever the passcode is) land on those keys in SpringBoard; iOS 26 answers each key's round face,
  unnamed, inside the key. Without the hit test (an older runner, the check off, landscape) or the screen's size, the
  answer is `not_proven`.

- A bug made `from: "failure"` names the place the run's answer named (a thin run's own read; it said "an unnamed
  screen"), and its Logs are the app's own lines, as `from: "now"` keeps them, not the run's log block with iOS's lines.

- Review (PR #21, CodeRabbit): the bugs folder's lock is a pid lock: a holder that is slow for minutes keeps it (it was
  taken over after 60 s), and only its holder releases it. A rewrite of a bug (a check's result, a sighting, a picture) is
  staged whole and swapped in, so its files are always of one version, and a failed write leaves the old one whole; an
  export reads the bug once, under the lock, so the picture and whether it may go come from that one version. A check's
  result is never written into a bug removed and made again meanwhile, each check holds the bug on its own (a second check
  that stops at once no longer ends the first one's hold), and an update whose `repro.json` can't be read again writes
  nothing. A bug is read only inside the bugs folder (an app folder that is a link is never followed).
- `serve --phone` no longer makes every app a test app for bug reports: only `--apps` (or `--dedicated`) names them, so
  another app's bug stays private and its export needs `confirm`.
- A check's run starts from a fresh process (a refused `close_app` makes no run), counts nothing that shows after its 60 s,
  reads a dead end's place and its back button from one screen read, and refuses a script with a step that isn't one
  rather than running a shorter path. A bug check reads the caller's `--policy` words, as explore does.
- Export: each zip has its own staging file (two exports of one bug no longer move each other's bytes into place); a file
  that can't be read or a zip that can't be written is a plain refusal; an issue that failed after its files were committed
  says so, names the branch and folder, and the export again makes the issue without uploading the same files again.
- `tapwright bug`: `--accounts` while the daemon runs is refused (the daemon uses its own vault; the file was ignored);
  `--app` reaches the daemon, so a bug id two apps hold exports that app's bug; an export's exit is 0 when it was written
  (a saved `0 of 3` made it 1); a check that paused exits 2 even when the page keeps an earlier `0 of 3`.
- A field with no label: its value (shown as its quoted text) is hidden in explore's and the check's accessibility and
  usability findings, as a labeled field's `value=` was. Explore's JS errors are the app's own lines only. A slider's `50%`
  is no loading screen. This computer's home folder with a space in its name is cut whole (`/Users/Jane Doe`). A `from:
  "now"` page names a start only when the first step's place is known, keeps two opens of a link that lost its query, and
  a source seen again after it left the last 50 is not counted again.
- Review round 2 (PR #21): a swap a writer left half done is mended under the lock before any bug is looked up (a report
  of the same finding no longer makes a new bug over it), and a backup that can't be put back is kept, never removed. A
  check that couldn't run keeps a check another one finished meanwhile (decided under the lock). A picture added to a bug
  made again meanwhile is refused. Explore's and `from: "now"`'s picture is private when a system sheet was in front as
  it was taken (a share sheet that came up after the last read).
- Review round 3 (PR #21): a value typed in the session that is also in what the tool writes itself (a year such as
  `2026`, the word `place`, the build number, a run of a bug id's hex) no longer refuses every bug and every export until
  a restart, and a check or sighting no longer turns a kept bug's dates or id into `<typed value>` (which made it sort
  first for retention, or unreadable). The redactor and the grep gate read only the text that came from the app or the
  session (got, expected, the note, logs, frames, the component stack, the steps' words, road names, a check's reason);
  the tool's ids, times, place names, versions, builds and the page's own words are written as they are. A typed value in
  the app's text is hidden as before, and a version or build that holds a vault value is still refused. An export gates a
  bug's app text when its files are as the tool built them, and every text whole when they aren't (a page edited by hand).
- `bug_report`'s wait in `tapwright call` and the MCP server covers each run's two launch waits (up to 30 s each), so a
  check of a development build that loads slowly no longer times out while the daemon is still checking.
- A backup of a bug that couldn't be removed whole (Windows, a file held open) is never put back as the bug: a replaced
  version is renamed before it is removed, and only a whole backup is restored. The daemon's first bug call no longer
  waits up to 15 s on its event loop while the command line holds an app's bugs: retention then skips that app until the
  next call.

## 1.8.0 (2026-11-19), "the Explorer"

- New tool `explore` (and `tapwright explore`): it searches one app by itself, with no model, and reports what breaks. A
  deterministic breadth-first walk of the screens nobody has driven yet: each new place and road goes to the app's map
  (with an `explored` record per place, and a Coverage line in `map`), and the run ends with a report folder under
  `~/.tapwright/explore/<app>/<time>/` (`report.json`, `report.md`, a picture and a repro script per finding): crashes
  with their stack, error-level log lines, dead ends, slow screens, refused touches, accessibility and usability issues.
  `minutes`, `steps`, `depth`, `from`, `focus`, `fill` (test values only into fields marked `-test-safe`, or a vault
  fixture marked `"explore": true`), `continue` past a crash. It never signs in again after a reset: §25.8's `account`
  can't pass the skip floor (it would type a password and submit a form), so 1.8.0 refuses it in plain words.
- What explore never does, on the whole-phone daemon of 1.7.5: it stays in one app (the one `--apps` names, else the
  app in front; never Home, Settings, Wallet or Passwords) and never touches what its skip floor holds: Delete, Buy,
  Send, Post, Pay, Sign out and the like, a card, account, privacy and security screens, a system share sheet, a
  password field, another app, a point or a swipe. It reads an element's label, value and id (an icon button's label may
  be a glyph or an SF Symbol name such as `trash`; the id `delete-row` still holds it), and the common symbol names of
  delete, send, share, pay and sign out are floor words. A switch with no id and no label is never flipped (listed as
  skipped); a flip whose flip back a pause, a person or a locked phone prevented stops the run naming the switch and
  what it was, and so does a flip that gets no clean answer (a timeout, a dropped line, a screen that can't be read, any
  error): the switch is named as one that may be flipped. A held touch is skipped for every build only when the floor
  names its word; a hold the dry run gives without one (another app in front, a sheet seen for a moment) is retried by
  a later build. The floor is one gate checked as each move goes out, on the element
  the touch really lands on after its lookup and any scroll (a dry run meets the same gate, `force` too); a `--policy` file's words
  and its `explore.skip` list only add to it (a caller's policy is combined with `serve --policy`, never put in its
  place; a caller's `explore.submit` ids are limited to serve's list; a `tapwright call explore --policy` or `call replay
  --policy` with `repair` sends its file too, and no other call reads it). An id that carries content is kept in no action
  signature (the map and the report), as in `seen`. An alert over another app is never answered. It answers a permission prompt with its denial, by the button's name;
  submits no form; sets a flipped switch back; asks nobody. Pause and a person's input stop or hold it; a locked phone
  ends it with `locked` ("the phone reads as locked (or Notification Center is open over it)"), and explore never
  unlocks the phone (the agent may call `unlock`, then explore again). No
  picture is kept while the phone is being unlocked or Notification Center may be open. A redactor that fails hides
  the text instead of passing it on.
- Self-repairing replays: `replay` with `repair: "suggest"` adds the likely fix to a failure (a renamed, moved or hidden
  target, a new screen in the way) and tries no candidate (the script's own steps still run); `repair: "apply"` tries up to 3 candidates per break, each a real
  touch through the same skip floor (and so is its way back after a rejected one: a saved road through Sign out is never
  driven; a road a repair drives or inserts is taps only, never a toggle or a long press; the floor's app is the script's
  `app`), accepts one only when its check holds and the whole script then runs to its end,
  and keeps a repair only after the repaired file itself has replayed green from the script's fresh start, the repaired and inserted steps
  through the skip floor, the script's own unchanged steps as a plain replay runs them (apply takes about twice as long as a plain replay: the client waits up to 8 minutes, and
  nothing is kept when the client stopped waiting); then it
  writes `<script>.repaired.json` and `<script>.repair.md` beside the script (the original is unchanged). A script that
  can't start fresh (`"fresh": false`, or no app) is only suggested: `apply` and `--adopt` refuse it. A failing
  `expect`, a typing step, a native Back and a draftsOnly script are never repaired; at most 5 repairs a run; every
  trial is an audit line of kind `repair`. Adopting is the user's: `tapwright replay FILE --repair apply --adopt`
  (JUnit carries the suggestion). Recorded steps now keep `place` and `seen` (role, screen area, component, and the id's
  stem, never text, and none for an id that carries content) so a repair can tell what moved.
- A list scrolled differently is the same place (the map, routes, explore and repair). A place's key no longer counts
  the identifiers inside a scrolling container (a React Native list's cards, a header that scrolls away): it goes by the
  identifiers outside them, the lists' own identifiers, and, for a list without one, the families of its rows' ids
  (`dest-card`, never the `kyoto` of `dest-card-kyoto`). Before, Back from a detail to a list left scrolled a few rows read
  as another place, so explore called it a dead end, relaunched, and ended `cant_get_back` with little explored. Two
  screens whose lists differ stay two places. A map saved before keeps its places and keys: a read is matched to an old
  place when its fixed ids are all among the old place's ids and something beyond the tab bar agrees too (a fixed id or
  a list id that isn't the tab bar's, else the rows' families kin to the old place's own content), so a screen it never
  held (a detail pushed in a tab, whose only fixed ids are the tab bar's) becomes a new place instead of being taken for
  the tab's list; new places get the new key. Two reads with no list compare as before 1.8.0 (the part for lists is left
  out), so two screens of other kinds and tabs no longer merge on the tab bar's ids.
- `tapwright call explore` waits for its `minutes` + 6 minutes, and `tapwright call replay` with `repair: "apply"` up to 8
  minutes (run, replay and drive 200 s), as the MCP server does: before, the thin call gave up after 120 s while the run
  went on.
- `tapwright serve --policy` and `tapwright mcp --policy` read and check the file at start (its words reach explore's and
  a repair's skip floor). From 1.7.5 to 1.7.9 the flag was ignored, so a host config written by `tapwright install --policy
  FILE` whose file has since been moved or broken now stops the server from starting, with a message that says how to fix
  it (restore the file, or run `tapwright install` again).

## 1.7.9 (2026-11-19)

- `media add` takes videos: mp4, mov and m4v, told apart from HEIC pictures by their `ftyp` brand (an old .mov by its
  first atom). Up to 200 MB each, 400 MB and 5 videos a call; the picture limits are unchanged (20 MB each, 40 MB a
  call, 20 items). A video is copied into the runner's own container over house_arrest (never base64 in a request,
  never whole in memory) and the runner adds it by file, moved into the library. A file that changes between its check
  and its copy is refused. The staged folder is removed when the runner refuses the add or the copy fails; after a
  timeout (the runner's own or no answer) the runner removes the files when the library's change ends, and the next
  video add sweeps folders older than an hour (after a dropped line the reconnect stops the runner, so only that sweep
  removes them). An add whose end isn't known answers `timeout` (no answer in time) or `reconnecting` (a dropped
  line, the copy's included), and the next `media` call with the same files says so once instead of adding them
  twice. While a video is copied and imported the heartbeat waits and other calls
  answer `busy` ("a video is being added to Photos"), as for iOS's photo prompt; no other add starts until it is done,
  however long the copy takes. A pause or a person driving is checked again once the copy is done, right before the
  add goes to the runner: paused, nothing is sent, the staged folder is removed and the add answers `paused`. A vendor
  brand counts as a video by any mp4 or QuickTime compatible brand in its `ftyp` box. Videos need the runner's route
  version 7 (`tw-16.15.3.7`); an older runner answers `unsupported`, naming `tapwright setup --force`, and still adds
  pictures. The MCP client and `tapwright call media` wait up to 15 minutes. Checked on an iPhone 14 Pro Max with iOS 26.5 over USB: a 130 KB clip in 4 s, a 150 MB clip in 17 s (other calls answered `busy` meanwhile, nothing reconnected, the staging folder empty after); over Wi-Fi not yet.
- `type_text` with an empty text and `replace` (the default) clears the field, as in 1.7.8, without the empty key
  request that came back as an internal error; with `submit: true` it clears the field and then presses the submit
  key, and with `replace: false` and `submit: true` it presses the submit key alone. An empty text with
  `replace: false` and no `submit` answers `bad_params`. A recorded secure field's step whose text is still empty is
  refused on replay.
- `unlock` with no passcode saved says the phone reads as locked (or Notification Center is open over it), as the
  other answers of a lock state that may be Notification Center do.
- The unlock's limit of 3 wakes in 10 minutes that type nothing is kept in the record of attempts
  (`~/.tapwright/unlock-state.json`), so a restarted daemon or another process counts them too; a record whose wakes
  can't be read wakes nothing.

## 1.7.8 (2026-11-18)

- New tool `unlock`: an agent unlocks a locked phone with the passcode saved for it (`phone.passcode` in the file
  Tapwright is started with, `--accounts FILE`), in every mode. The agent never sees or sends the passcode, and every
  rule of the passcode unlock holds: only on iOS's lock screen with its keypad proven, never on the XCTest prompt, at
  most 2 attempts an hour and 3 a day, none after a refusal, and an audit line with the name only. With no passcode
  saved it answers `locked` and the agent asks the user. A call of its own: as a step of `run`, `replay`, `drive` or a
  journey it is refused. Every `locked` answer for a locked phone now says "call unlock (it types the passcode saved
  for this phone), or ask the user to unlock it"; the owner stops an agent that has the passcode by pausing in the
  live view. Not yet proven on a Face ID iPhone with iOS 26: there the passcode unlock didn't find the keypad in 1.7,
  and then it types nothing and answers `locked` (docs/setup.md).
- `press_button home` closes Notification Center while the phone reads as locked. iOS sets SpringBoard's lock state
  while Notification Center is pulled down over an unlocked phone, so every call, the home press that closes it too,
  was refused `locked`. Home is pressed only when a fresh read proves that sheet: its header and its Clear button on
  the screen, on one row, the header not a notification's text, and an enabled flashlight. On the dark lock screen
  nothing is pressed, and the answer says the phone reads as locked (or Notification Center is open where Tapwright
  can't prove it). A lit lock screen showing the same sheet (a notification woke it) may pass the proof, and Home there
  may wake Face ID; Pause in the live view is the owner's stop.
- While the phone is being unlocked (from the wake to the check), no picture of the screen reaches an agent:
  `filmstrip`, `compare_screen` and screenshots answer `busy` ("the phone is being unlocked"), a journey keeps no
  picture, and `tapwright record-video` leaves those frames out as it does Notification Center's (iOS's keypad shows
  each key as it is touched). The owner's live view still shows it. The keypad's keys write no trace line each.
- The unlock checks Pause and a person's input again right before the first key, and wakes the lock screen at most 3
  times in 10 minutes without typing (each wake presses Home and may swipe up: a Face ID try). After an unlock that
  found the phone not locked, and after the automatic unlock of `serve --phone`, the next call no longer reads a stale
  `locked`.
- Content another app draws inside the app in front (Safari's share sheet, X's photo picker) is listed where it is on
  the screen. XCTest gives its frames in the host view's own space, so "View More" was listed 531 points too high, and
  a tap on it opened the video behind while answering "Tapped". Frames are moved only when the tree shows it plainly:
  a direct child with the host's size at (0, 0), the host clearly away from (0, 0), on the screen and outside any
  scroll view's content (a parallax header is not a host), every frame inside the host's own space, and some clearly
  outside its place on the screen. A tap, toggle, long press, focusing tap or scroll on such an element goes out only
  when the runner's hit test (route version 6) names that element, or an element inside it, at the touch point;
  otherwise it is refused `covered` and nothing is touched. Where the frames can't be placed, elements in that view
  are never touched by their frame, even with `force` (and `back` doesn't press a bar in such a view); a tap by point
  still works. The check adds about 5 ms to a read of a deep React Native tree.
- While the app in front isn't answering, `close_app` closes it at once. Before, a reconnect attempt in the meantime
  made it answer `reconnecting: … look before acting` again and again (2 min 40 s with X). `press_button home` also
  gets through: it asks the runner for the home screen without a session, once, with a 10 s cap, and only when the
  phone says it isn't locked. With no answer in time, the answer says the home screen may still come up.
- `type_text` at a point focuses the field at that point, with or without `force`. Before, it tapped the field's
  centre, which in X's composer was under the app's floating toolbar, and the GIF picker opened. A point under the
  keyboard (even with `force`) or under a control the app draws over the field is refused `covered`.
- `type_text` with `replace` proves the field right before clearing it: it must be the one input at its place in a
  fresh read (under the agent's point, or under the focusing tap when another field shares its label or identifier),
  and the focused element's frame must be that input's. A label or identifier alone proves nothing: two posts of a
  thread share a label, and a list may repeat one identifier on every row. Otherwise the answer is `stale` and
  nothing is cleared or typed. Typing again without a tap is only for the very field typed into last.
- A field that goes away while its focus is checked answers `stale`, not `internal` (while the focusing tap's focus is
  awaited, it is checked again until the wait ends). When the keyboard was closed to
  try the focusing tap again and the field didn't survive the new read, the answer is `stale`, not `not_found`.
- The field that has the keyboard's focus is typed into even when the keyboard's accessory area is drawn over it
  (Safari's address field). No tap is made on it; if the focus has moved meanwhile, the answer is `stale`.
- `open_url` into Safari says when the link didn't show: Safari's address field shows another host, or Safari kept
  the page that was open (same tab, same page, same text). Before, it answered "opened" while Safari stayed on a video.
  A redirect to another host (a short link) gets no note.
- The keyboard reflex's short drag runs only on passive content, every point of it at least 12 pt from anything that
  takes a touch; with no such place it doesn't drag. When its touch opened another screen (X's photo picker took the
  keyboard's place, and the agent's Post, Cancel or point tap then landed on the picker), the call stops with `stale`
  and nothing else is touched. When nothing closed the keyboard, the answer says "couldn't close the keyboard first".
- A label matches as the list shows it: a no-break or narrow no-break space (iOS writes one before "PM") or a run of
  spaces counts as one plain space. A photo picker cell listed as "Video, twelve seconds, 17 November, 7:19 PM" was
  `not_found` by that label.
- `type_text` without a target types only into the app the agent's last touch or `open_app` went to. When another app
  came to the front since (Safari, after a tap in Files), it answers `stale` and types nothing; a `look` clears it.
  `open_app` also drops the field typed into last. The app in front is the one the typing's own read names, and the
  refusal keeps the app the last touch went to, so the same call again is refused again.
- `type_text`'s answer gives a field's value only as a read after the typing shows it. After a submit Safari's address
  field is another element, and the answer gave the address from before the submit as if it were the new one.
- Forgetting a reflex or a place always wins over a map save under way: an edit no longer takes a forgotten reflex
  that the save had written back into the file for one it wants (the flaky CI test on ubuntu, Python 3.10).
- Notification Center as iOS 26 shows it is recognised: a notification in a `ListCell`, or the cover sheet's date on
  the screen, proves it open (it has no title and no scroll view there). A banner or the home screen still proves
  nothing.
- The agent guide and the skill say how to close the keyboard with the tools there are, and that X disables the
  composer's Photo library button while the text holds a link (attach media first, then add the link). They don't
  suggest `submit` to close the keyboard: what Return does is up to the app (it can submit a form or send).
- Content another app draws (see above) is proven again where each touch goes: every later drag of a `scroll`, and a
  double tap's second touch. The keyboard reflex never drags a scroll view in such content, `back` never presses a
  Back button of it inside an ordinary bar, and an element of it below the screen keeps its mark, so `scroll` with
  `to: "target"` refuses one whose place can't be told.
- Typing again without a tap also checks that the focused element is still where the field is listed (an automatic
  scroll can carry it off and bring another field with its identifier to its place). `replace` after a focusing tap,
  with an identifier only one field has, clears it only where it is still under the tap, or where the content around
  it moved with it (the keyboard scrolled the form); otherwise `stale`.
- A tap by point that first closed the keyboard is checked against the read the reflex made: when a control now lies
  under the point that wasn't there when the agent aimed (the reflex's drag scrolled it there), the answer is `stale`
  and nothing else is touched; `force` touches there anyway. The app a touch is meant for is the one the newest read
  before it names, and a cover-sheet date partly above the screen's top still proves Notification Center open.
- A field the keyboard moves is typed into on the first try again (a chat composer with no label that rises with the
  keyboard, a search field that narrows as Cancel comes in): when it is the only input on the screen before and after
  the focusing tap and moved only up or down, it is the tapped one. With another input on the screen the proof by place
  stands.
- `back` with a sheet in front whose frames can't be placed is refused `covered` instead of touching beside it. While
  such a frame is listed, the keyboard reflex uses only the keyboard's own dismiss key, and a target whose place can't
  be told doesn't close the keyboard first. A refusal after a touch on the way (the reflex, a scroll to the target)
  says "nothing else was touched". `look` and `open_url` set the app that typing without a target goes to.

## 1.7.7 (2026-11-17)

- New: `tapwright record-video --out FILE.mp4 [--seconds N] [--fps N]` records the live view as an H.264 MP4 in real
  time (needs `pip install "tapwright[video]"`), until Ctrl+C or `--seconds`, and finishes the file either way. Frames
  keep their capture times (variable frame rate), so a still screen lasts as long as it did. `FILE.frames.json` gives
  each frame's `seq`, capture time in epoch ms and in the action log's ISO form, and its place in the video, so a frame
  lines up with `~/.tapwright/log/actions-*.jsonl`. A recording shows whatever is on the phone's screen: record test data.
- The recording leaves out Notification Center: `GET /stream?for=recording` sends no frame from while a notification
  read may have had it open (the live view itself is unchanged), and says so in an `X-Hides` header; the command
  refuses an older `tapwright serve`. It fails closed: from before the swipe until 3 s after the close was confirmed
  (open-ended when closing wasn't shown to work), no frame, none whose arrival the daemon can't tell, and never the
  picture that was newest at the end of a read. A recording of a quiet screen no longer ends after 30 s (keep-alive
  parts), a Ctrl+C never lands inside the encoder, queued frames are written on stop and the queue is bounded, an
  encoder that can't open for a file is skipped, and the video and its frame list appear together or not at all.
  It is a command only: no new agent tool.
- Checked on an iPhone (USB, iOS 26): a 30 s recording at 1290x2796, 584 frames, decodes cleanly, and a Notification
  Center read during it left a gap from before the swipe to 3 s after it.
- Known limit: with the AirPlay video feed (`serve --video`), a still screen next to a Notification Center read stays
  hidden, so a recording started just after one may begin with no picture until the screen changes.

## 1.7.6 (2026-11-17)

- A call whose target isn't an object (`"target": "Workouts"`) is refused as `bad_params` again, naming the target
  forms. In 1.7.5 its action-log line failed and the agent got `internal`. A log line that can't be written no longer
  replaces any refusal.
- A Back button an app lays on its navigation bar as a sibling (X's profile page) is the bar's Back: `back` finds it,
  and a tap on it is no longer refused as covered by its own bar. Content scrolled under a bar is still covered.
- The agent guide and the skill say how to press Home: `press_button {"name": "home"}`.

## 1.7.1 (2026-11-15)

- A tap on a button can recover when the runner hits passive icon/text content that the accessibility tree cannot
  place. It tries a small number of inset points and uses one only when a fresh hit test identifies the exact same
  button. The chosen point is checked again before delivery. Changed actions, other apps, known overlays and nested
  actionable controls still refuse; policy and confirmation checks still apply to the button.
- `alert {"action":"cancel"}` provides explicit recovery from a system alert when the owner asks to cancel it.
  It requires a unique enabled Cancel button and verifies the alert again immediately before pressing that button
  by name. In personal mode, an allowed app must still be in the foreground beneath SpringBoard. It returns no
  private alert text or screenshot and never accepts the alert, enters a password, or opens its settings.
  Use it as a standalone call, then `open_app` to return to the app under test.
- Recovery guidance now describes the human live viewer accurately and explains how to recover without moving
  the viewer's private key into another browser or weakening the app list.

## 1.7.0, "the Control Update" (2026-11-14)

Tapwright can now be trusted with more of the phone, for longer, with the owner in control. Shared interface:
AGENT-TOOLS 1.16.2 (sha256 ef8e7b63), frozen with Tapline.

- **Grants** (§19): standing permissions in the policy file version 2, offered in the host's dialog right after a
  person's OK, with limits per day and hour, use counts that no stalled process can undo, an edit lock, the kill switch
  (`--revoke-all`), `tapwright grants`, and a `Grants:` line in `session_report`. Some things are never grantable: a
  card, Settings' account, privacy and security panes, Passwords, Wallet.
- **The vault** (§20): a version 2 `--accounts` file holds accounts, cards, addresses, identities, notes, test fixtures
  and the phone's passcode. Every value is kept out of every answer, log, map, report and picture, in any encoding.
  `tapwright vault protect` stores it with Windows DPAPI or the macOS keychain, in a form Tapline reads too.
- **One-time codes** (§20.4, §24.3): per app and off by default. Tapwright reads a sign-in code from the notification
  of the app the setting names (Messages, Mail, …), types it once with `${otp.<app>}`, and never shows it. Tapline's host
  gets it over the proven loopback link (`POST /otp`). Checked end to end on the phone with Tapline.
- **Notifications** (§24.2): the `notifications` tool lists the given apps' notifications, newest first, and
  `{open: n}` opens one. Every other app's notification is dropped before anything sees it, and nothing is captured
  while Notification Center is open.
- **Content from others** (§21): text other people wrote is marked `(from others)` in Messages, Mail and shared Notes.
  The instructions say all screen text an agent didn't write is data. A drafts-only grant or route stops at the draft
  for a person.
- **Automations** (§22): `tapwright schedule add ROUTE --at "09:00 daily"`, `--on-file DIR` or `--on-notification APP`
  runs a saved route with no model. Only time schedules wake the PC (Task Scheduler). Runs are skipped while a person
  uses the phone; each run writes a report and an audit line.
- **The audit log** (§23): grants, approvals, vault uses, codes, schedules and exports, one line each, never a value;
  `tapwright audit-log`.
- **The whole phone** (§24): `--phone` mode for a phone kept for automation. Starter routes for Reminders, Notes,
  Calendar, Mail and Messages: `tapwright starters install`. Mail and Messages stop before Send for a person. A
  watchdog, and `doctor --unattended`.
- **The passcode unlock (experimental):** `phone.passcode` in the vault is typed only on iOS's proven lock screen, only
  at a scheduled run's start or, with `--phone`, at the start of a new call. It is never typed during a run: locking the
  phone still stops everything. At most 2 attempts an hour and 3 a day, and a refusal holds until
  `tapwright unlock-reset`. On the test iPhone (Face ID, iOS 26) it did not yet find the passcode keypad: it answered
  `locked` and typed nothing. And iOS won't reconnect over USB to a locked phone. Treat unattended runs as needing an
  unlocked phone for now.
- **Runner tw-16.15.3.5**: the tree says which field has focus, so typing into a focused field no longer opens iOS's
  caret menu. A touch is checked against what iOS would really deliver it to, so an element behind a bar is refused
  (`covered`), and a cover the tree only guessed is dropped when iOS says the touch lands on the target.
- **Found on the phone and fixed:**
  - Notification Center is read as iOS 26 lays it out.
  - A notification opens through its row's Open action.
  - A sheet's fields stay listed while the keyboard's accessory bar is up (Reminders).
  - `serve --apps` accepts an app by its name or its bundle id in every call.
  - A learned map never keeps an element id that carries content (Calendar's event titles).
- `drive` takes `look`, and an app alert's `Alert:` line carries its message (1.13.3).
- **Safety, from the release review:**
  - A one-time code is typed only into a code field.
  - Drafts-only stops at Send, Post, Publish, Reply and Comment, whatever the policy file says, and `submit` is
    refused there. Known Mail and Messages Send identifiers work with translated labels too; an unreadable grants
    file never lifts the restriction.
  - Final input checks run after banner waits and runner setup. A changed draft, revoked grant, changed touch point,
    or different actionable recipient stops delivery. Typing focuses at the point that was checked.
  - A full swipe across a list row (which deletes in Mail, Notes and Reminders) needs a person's OK.
  - Scheduled runs never overlap, and wait 90 s after anyone used the phone, including an external one-time-code
    handover. Their report metadata and displayed paths are redacted; failed folder scans keep the previous baseline.
  - Dry runs never unlock the phone. Unlock attempts have ownership tokens, and an unknown XCTest prompt stops typing.
  - Audit pruning cannot overwrite a concurrent append. Vault protection serializes writes, preserves a changed
    source file, and refuses line breaks in macOS keychain paths.
  - Schedule creation and route replacement share a lock; both grants and schedules protect the route they name.
  - Account deletion and payments are recognised by pattern, and are never grantable.
  - `tapwright audit-log` lists and deletes the audit log.
  - Every program that can read `~/.tapwright/serve.json` is trusted like you; keep it private (see the README).
- **Known limits:**
  - `notifications {open}` on a stack of one app's notifications may refuse; Notification Center is then closed, and
    `open_app` opens the app.
  - A read while a notification banner is up may say Notification Center didn't open; read again.
  - Shared spec readings beyond AGENT-TOOLS 1.16.2 are listed in docs/setup.md.

## 1.6.2 (2026-11-14)

- A WebDriverAgent session re-created in the middle of a run now gets the same settings as the first one (no waiting for
  animations such as a blinking caret, and the stream's current quality) right after it starts. Before, a tap and then
  typing after a re-created session took 3.7 to 5.0 s; now it takes 1.7 s. A quality change keeps the requested settings if the connection drops and is
  retried.
- `alert` takes only `accept`, `dismiss` or `text`. Any other action is refused as `bad_params` and names the allowed
  values; nothing from the call is put into a URL.
- Typing into a field that already has focus can still open iOS's caret menu; a fix that reads focus from the runner
  comes in 1.7.

## 1.6.1 (2026-11-14)

- A place is `place_<n>` until a person names it. Tapwright no longer names a place after a repeated navigation title
  (a private title such as "Chat with Sara" would have become a stored place name). Maps that already hold
  title-derived names keep them. Same rule as AGENT-TOOLS 1.13.2, §18.1.
- Tapwright never names a place after a navigation title, and learned roads keep no title in plain text (a native Back
  is learned as `back`). Scripts, routes and reflexes you record or define keep what you recorded.
  A native Back button is labelled with the title of the screen under it, so a road learned from a tap on it is `back`
  with no name; a road that would aim by a title (or keep one among its other names) keeps none of it. Whether a tap was
  on the Back button is decided before the screen is read again, so a screen that comes back with another title (a chat
  renamed) doesn't hide it, and only a tap on it is learned as `back` (a long press is not). A recorded script or route
  keeps its steps, waits, expectations and `ignore` entries as recorded, a title included, so it replays the same way.
- `crash` with `pull` also replaces a number, true, false or null in the report's JSON whose text is a test account's
  value (a PIN, a user id) with the placeholder, and checks the finished file; a report that still holds a value is not
  saved, and the answer says so. A road repaired during `drive` keeps none of the titles the screens showed on the way.
  A recorded step aimed by a placeholder is saved from the element the action really touched (a toggle's switch).
- A test account's password or username that a screen shows (a revealed password) is now replaced by its placeholder
  (`<tester2's password>`) in everything that leaves Tapwright, in any form (escaped, collapsed, cut): text answers and
  errors, change lists (a `(was …)` cut no longer keeps a password's end), alerts, the audit's element lines (`id=`) and
  XCTest's words, OCR lines, the live view's captions and confirmation requests, the host's OK dialog, recorded scripts
  and routes, maps (ids and title hashes), journey reports (`report.md`, `report.html`, `journey.json`, an exported
  issue's title) and the action log. Each is redacted before it is cut.
  Inside, Tapwright keeps the screen's own words, so the safety policy, typing and matching work as they do without
  accounts: a password `Delete` doesn't hide a Delete button (or an alert's Delete) from the confirmation or a block
  rule, and replacing a field's text erases all of it, not as many characters as its placeholder has. A target or an
  expectation matches the screen's words or their placeholder, so `{"text": "<tester2's username>"}` finds what an
  answer showed that way, and a script that names the value itself still replays.
  A recording never saves a selector that holds a value or its placeholder: it takes the id, else the label or text,
  else the point of the element the touch really reached (not of the list the agent had before the call, which may be
  empty before a first look). A step that can't be saved that way, or a wait or expectation on a value, refuses the save
  with a message that names the step and never the value; nothing is saved and no step is dropped without a word. A
  `compare_screen` ignore that holds a value is saved by the element's id or rectangle. As a script or route is written, only its content (labels, texts, ids, values, links) is redacted,
  never a key, a tool, the format or a `${account.…}`, so an account whose username is `tap` leaves the saved tool
  `tap`. A redacted file is checked as `replay` reads it before it is reported saved.
  Every sink is guarded where it writes, so a path that was missed can't leak: a diagnostic (an app's log line, a
  crash reason, the logs a failed step carries) is redacted before any length cut, and a value is never kept in the log
  ring; the daemon's log (`serve.log`) redacts every message and traceback with the accounts the daemon has loaded; a map
  holds no account value (the selected tab's id, a repaired road's target; the writer drops a road or replaces a value
  that still holds one, never a key); and a value that the screen's text shows without its invisible direction marks
  is redacted in that form too.
  Every shape of a value is redacted, and `holds` knows each. What is guaranteed: a value is found as it is; with
  whitespace normalized (any run of whitespace or `+` is one space, the ends may be trimmed) and direction marks
  anywhere; percent-encoded (`%HH` in either case, UTF-8); with JSON or Python escapes (`\uXXXX` and surrogate pairs,
  `\UXXXXXXXX`, `\xHH`, `\\`, `\"`, `\'`, `\/`, `\b`, `\f`, `\n`, `\t`, `\r`); and any composition of those, each
  layer a whole-text encoder (a `%r` inside a JSON crash report, a URL inside a JSON string) or a different escape per
  character, decoded pass by pass to convergence. Only those escapes are decoded: any other backslash is a literal
  character, so a password `pass\word` doesn't hide the word `password`. Every occurrence at every level, overlapping
  ones included, is mapped back to the characters it came from and redacted whole, and a log message, a traceback or a
  report shows none. Decoding stops at a work limit of 16 passes; a whitespace-separated token that
  would still change is redacted whole as `<a value encoded too deep to check>` and counts as holding a value, so
  nothing is released unchecked. Searching is linear in the text and the values. A username or password is 3 to 256
  characters (at most 256 as written, at least 3 once decoded and trimmed, so three spaces are refused) and must settle
  within the limit, and a file holds at most 32 accounts (64 values); a file that breaks this is refused when it loads,
  naming the account and the field (or the number of accounts), never the
  value. Not covered: other encodings (base64, HTML entities, a hash, a change of case, a value split by other text), and a
  backslash left bare right before an escaped character (`\` + `\x73`), which JSON and Python, and the matcher, read
  as an escaped backslash; and text around a value that ends or starts with an incomplete escape (`%2`, `\`, `\u00`)
  and so is decoded together with the value's first or last characters (boundary fusion), which the matcher catches
  in the common cases as a best effort, without a guarantee.
  A route's declared variables are filled in every string field and an undeclared `${name}` is literal text in every
  field, typed text included; a literal `${` is written `$${` in every field the recorder saves (in the copy it writes,
  so a save that fails and is tried again escapes it once), so a replay taps the element that was recorded; the save
  reads every field's literal runs decoded. A value ever
  loaded from any accounts file stays redacted for the daemon's life, even after the file changes (typing uses only the
  current ones). A crash report saved by `crash` with `pull` (and the copy `replay` puts beside a failing script) is a
  sanitized copy in the same format: each JSON document (or JSON line) is parsed and every decoded string scrubbed, so
  a value its bytes hold only escaped is caught too. The map writer redacts only content fields (never the format, a hash, a key or a
  name), and a redacted map that wouldn't read never replaces the file.
  Only escapes are case-insensitive; the value's own letters keep their case.
  A recording refuses to save typed text that holds a value from before an accounts-file reload, as it does for a current
  one, and checks typed text again at the save against the file as it is then.
  Typed account values are recorded as `${account.username}` / `${account.password}` (text that is exactly the value, a
  username in a text field, a password in a secure one); text that only holds a value is refused naming the step. A
  recorded selector is counted and checked the way the replaying tool resolves it (a toggle counts switches first), and a
  toggle that changes nothing is still recorded by its switch.
- `tapwright call TOOL -` reads its JSON from standard input as UTF-8 on every system. On Windows it was read in the
  console's code page, so Arabic or Japanese piped in was typed as mojibake. Input that isn't UTF-8 is refused, not
  sent.
- QUALITY-BAR 2.5 (`docs/QUALITY-BAR.md`).
- The spec is AGENT-TOOLS 1.13.2 (`docs/AGENT-TOOLS.md`, the same bytes as Tapline's). Besides §18.1, it says that
  pictures are free of a password only while its field stays secure (§18.6), adds `"by": "chat"` to `confirm_closed`
  for an OK given in the chat (§11.3), and marks the passcode unlock (`phone.passcode`) as planned.

## 1.6.0, "the Speed Update" (2026-11-13)

An agent on the phone should feel almost instant. A real run (10 workers, 760 calls) showed the largest cost was
outside the tools (a new process for every call), then the number of decisions, then the size of each. 1.6 goes after
all three, with no model inside Tapwright and no API key. On ground the tool knows, no model is asked anything; on new
ground a small, fast model drives and the tool remembers the way. The interface is AGENT-TOOLS 1.13, shared with
Tapline.

**A call without the plumbing**
- `tapwright call TOOL '{json}'` runs one tool in the running daemon and loads only Python's standard library, for a
  host that can't keep an MCP connection (`--apps` or `$TAPWRIGHT_APPS` for the scope, `--client`, `--json`; exit 0,
  2 for a refusal, 3 when the daemon can't be reached). Loading the MCP server once is still the fastest route.
- `tapwright serve --apps A,B` (or `--dedicated`, `--policy FILE`) is a ceiling: no call, MCP or thin, may name
  more apps than that or drop a word from the policy.
- `session_report` counts seconds, not only calls: the agent's thinking time between calls, the tool's time, by tool,
  and a line when every call came from a new process.

**Shorter answers, fewer calls**
- `brief: true` on any action, `run` or `drive` (`tapwright mcp --brief` for all): the first line, at most 12 lines
  of changes, then `…<n> more: look shows them.` A new screen lists its actionable elements and rows only.
- Steps gain `if` (do this step only when that is on screen) and `optional` (skip it when its own target isn't
  there), in `run`, scripts and routes, so one decision covers a flow with a maybe-screen. Time spent waiting for a
  person doesn't count toward a batch's 120 s.

**The map, and `drive`**
- Tapwright recognises a screen again whatever its content or language (a signature of the fixed elements, with
  the title kept only as a hash) and saves each move between screens as a road, in one readable file per app
  (`~/.tapwright/maps/<bundle id>/map.json`, or `--maps DIR` to share it with a project and with Tapline; two tools
  can write one file, merged on load). A list row's text, typed text and secure values are never in it.
- `map` lists the places, roads, routes and reflexes, shows the way to a place, and names places, adds a place's
  link, adds and removes reflexes, and forgets a place. Every full answer says `Place: <name>.`
- `drive` goes to a place by a link or the shortest chain of proven roads and answers once (`Arrived at language
  by 3 taps in 1.9 s`). It repairs a stale road by another name of the same element, plans again from another
  known place, and otherwise stops with the new error `off_route`, having touched nothing more. It never claims an
  arrival it didn't check, and every move passes the same scope, policy and confirmation as a single call.
- Routes: `record` with `route` saves a flow that starts from a named place and takes variables (script version
  2); `drive` runs it with `with`, `account` and `then` (and `by`: `any`, `taps`, `link`).
- Test accounts: `--accounts FILE` keeps named accounts on the user's computer. The agent says `account: "tester2"`
  and the tool types the values, a password only into a secure field. They are never in an answer, log, map, script or
  report, and a file inside a git work tree that git doesn't ignore is refused.
- Reflexes: the tool waits out a loading indicator before calling a target `not_found`, moves the keyboard out of
  the way, and runs the reflexes the user or agent declared for the app (one step each, never an action the policy
  flags), saying every firing.
- `alert` answers an alert that another app draws about a given app (Settings asking `Delete "Wayfare"?`), under the
  policy; a risky answer still asks a person.
- A journey in a user's eyes has no jumps, reflexes or shortcuts unless it allows them (`allow`: `drive`, `reflexes`,
  `fast_animations`).

**Agreed with Tapline after AGENT-TOOLS 1.13 (the 1.13.1 clarifications)**
- `off_screen` now means no scroll can bring the element on screen (a touch scrolls to an off-screen target by itself),
  and says to look, then target something else or report it.
- A script, reference picture, route or place name is ASCII, and one that differs from an existing name only in case is
  refused (`bad_params`, naming the existing one) before anything is written.
- A double-tap stress sends no second touch when the control's id, label or text changed after the first: it answers
  `changed`.
- A route's drive line is `Arrived at home by route sign_in in 3.2 s.`, and `session_report` flags five thin calls in a
  row, and a retry after an id's `not_found` says to target what the latest look shows.

**A driver for new ground**
- `tapwright install` and `tapwright skill install` write the driver agent `phone-driver`: a subagent on the host's
  fastest small model (Claude Code: `.claude/agents/phone-driver.md` on Haiku, low effort, the driving tools of
  Tapwright and Tapline; Codex: `.codex/agents/phone-driver.toml` on `gpt-6.1-terra`), with a short handbook:
  `map` first, `drive` when the map has the way, several moves in one `run`, read every answer whole, stop on
  `needs_confirmation`, `locked` or `paused`, and finish in five lines. `--driver-model NAME` changes the model.
  For a host with no agent files the handbook is `references/driver.md` in the skill, to paste as a brief.
- The skill's "Driving fast" gives the main agent its rules: known ground is one `drive`, unknown ground goes to
  the driver (one for the session), accounts by name, never step by step.

**A person's OK in chat, and fewer hands on the phone**
- A touch that needs a person's OK (Buy, Delete, Send...) is asked in the host's own dialog (MCP elicitation) as
  well as in the live view: Allow once or Don't allow, the first answer wins, and the screen is read again after an
  allow. `--no-dialog` turns it off for a host that can't show it. The agent can never answer it.
- A locked phone, the XCTest passcode prompt, iOS's Health sheet and an iOS alert are asked of the person the same
  way, and the call goes on by itself when it clears (up to 120 s).
- `tapwright doctor --unattended` says what would stop a run with nobody there (the phone locked with no passcode
  given, Enable UI Automation off, the computer not trusted, Developer Mode off, a low battery) and how to fix each.

**Faster touches, and a more honest read**
- The runner on the phone is now the fork `tw-16.15.3.4` (Appium's 16.15.3 with Tapwright's changes). It reads the
  screen in a compact form and checks which app is in front inside the tap itself, so a tap needs fewer round trips.
  `tapwright setup` installs it.
- The wait for a still screen is cut when the live picture already proves the screen is still, so a read right after
  a tap doesn't wait again.
- `scroll` with `to: "end"` or `"start"` sends the drags one after the other, then waits once and reads once, where
  it waited and read after every drag.
- `back` always checks that something changed, with or without a `look`, by the picture before and after and by the
  tree. When nothing changed it answers `not_actionable: no_action`, as before.
- A tap on an element that another view is drawn over is refused `covered`, as a person's finger would be stopped,
  even when that view isn't in the list (a named plain view over a button). `force` still works. iOS's scroll bars
  never count as covering.
- Elements that lie wholly off screen to the left or right (the screen before, kept under a stack navigator) are no
  longer listed.
- The note after a touch that changed nothing names the touch: `the tap was delivered…`, `the long press was
  delivered…`, `the double tap was delivered…`.
- `TAPWRIGHT_TRACE=1` writes one line of timing for each touch and each read to `~/.tapwright/trace.jsonl`, for
  finding where a slow call spends its time. Off by default; no screen content is written.
- Fixed: `type_text` and `drive` failed through MCP in a 1.6 build. A typing that doesn't land says why in the trace.

**Measured** (the same iPhone 14 Pro Max, Wayfare's benchmark lab, Tapline's runner, 1.0.0 against 1.6.0, over USB):
a tap 0.74 to 0.61 s, a toggle 0.60 to 0.42 s, a scroll by a real touch 2.05 to 1.64 s. Misses in the touch checks
(R1, R2) went from 6 to 3 of 48, and a tap on a covered element is now refused. Typing and a text look are the same
as in 1.0.0, and closing and opening a development build are not faster. Wi-Fi varies from minute to minute, and
scroll and typing over Wi-Fi came out slower in this run. The tables and the notes are in
[docs/benchmarks.md](docs/benchmarks.md).

**Not in 1.6:** fast animations, the Safari and web-page tools, the Wi-Fi radio work, unlocking the phone with a given passcode and the starter map for iOS Settings move to a later update.

## 1.5.1 (2026-11-13)

Small fixes from the first real-app runs. No new tools.

- `close_app` is faster. It ends the app through iOS's own process control, then tells WebDriverAgent, which waited a
  median of 46 s for a real app to finish quitting. The answer is the same, and a slow or failing second step is
  logged, not waited for. Without the tunnel, if iOS refuses, takes over 5 s or finds no process to end, it falls back
  to WebDriverAgent alone as before; `TAPWRIGHT_CLOSE=wda` forces that. `open_app` with a language, region or text size
  closes a running app the same way. A bundle id nothing has gives `not_found`.
- A `tap` or `long_press` that changes nothing keeps `No visible change.` and adds a note: the tap was delivered, and
  nothing on screen changed within N s, with what to try (a text field: `type_text`; a point with no element; else
  `wait_for` what you expect, then report it as a finding). Agents couldn't tell whether the touch landed, so they
  looked again and retried. iOS's Health access sheet keeps its own note.
- The skill and the agent guide say four things more: read every answer whole (never pipe a call through `head`,
  `grep` or `sed`), send known steps as one `run`, ask for the app's deep links and use them with `open_url`, and
  connect once as an MCP server (a wrapper that starts a new process for every call adds about 2 s to each call).
- React Native radio and checkbox options are listed, with their checked state (`radio "Daily" checked=true`). iOS
  gives them no accessibility trait, so they were typed `Other` and left out of `look` unless they had an id. They are
  found by React Native's role words in the label or value (`radio button`, `checkbox`, English only), so no other
  `Other` changes: a card, a scroll indicator or a plain labelled view is listed as before. `audit` flags each such
  element once: no trait, so VoiceOver doesn't announce it as a control.
- The first touch by id, label or text after `open_app` reads the screen again before it aims. The list `open_app`
  answered with can miss a search field drawn a moment later, and a row under it was touched at its centre, beneath
  the field. The row is now touched at its visible part.
- A plain `open_app` after one with a language, region or text size says what it undid:
  `Opened Example (com.example.app), back to the phone's own text size.`
- `media`: the first add, which makes iOS ask the person whether the runner may add photos, answers `needs_a_person`
  at once. Tapwright never answers that prompt itself. The runner can do nothing else until the person answers (two
  minutes at most), so meanwhile other calls answer `busy` right away instead of waiting, and Tapwright doesn't take
  the silence for a lost connection. The add finishes once the person answers, and calling `media` again with the
  same pictures picks it up rather than adding them twice.
- The journey's stop line names the errors its report counts: `Findings: none. Errors: 2 (1 not_found, 1 covered).`
- The live view's link with its key is never printed (the start line of `serve`, "already running", and `view`). An app
  around the terminal had copied such links with the key hidden as `?token=SECRET` and opened them as tabs, which
  could only fail. `viewer` leaves an open live view alone instead of opening another tab (`health` reports the
  connected pages), and no test opens a real browser.

## 1.5.0, "the Real User Update" (2026-11-13)

Apps are made or broken by whether a user can find a feature, understand it and finish it easily. An agent can now
test that, going through a feature as a real user would, and file a visual report. The interface is AGENT-TOOLS 1.12,
shared with Tapline.

**A journey, in a user's eyes**
- `journey` with a `goal` ("log breakfast by scanning its barcode") and, if useful, a `persona` and `conditions`
  (language, region, text size, network). While it runs, the agent sees what a user sees: no ids, nothing off screen,
  no jumping to a target, and icon-only buttons shown as such, with what VoiceOver says kept apart.
- The tool measures the trip: taps, screens, typing, scrolling, time (waits apart), wrong turns, dead ends, errors,
  hesitations, and the taps to the goal.
- Stress moments: leaving the app and coming back, a bad network mid-flow, a double tap, rotating, relaunching, each
  checking that the user's progress survived.

**Personas as guidance, never enforced**
- The skill shows how to build personas for the app at hand, with example personas to copy, extend or ignore (the same
  file as Tapline's). No setup is required: a journey can start with only a goal.

**Each screen against the basics**
- `ux_check`: competing choices, one clear main action, thumb reach, icon-only buttons, cut-off text, contrast, target
  size, naming that changes between screens, and error or empty states that don't say what to do. Facts first, each
  naming its element and basis; the agent adds its own judgment and says it is judgment.
- House rules (`--ux-rules FILE`): the user's own taste, as text the agent judges, or as rules the tool checks
  (`max_taps_to_goal`, `way_back`).

**A visual report**
- When a journey stops, a report folder (Markdown, HTML, screenshots, a filmstrip, marked-up findings, and a GIF of
  the journey from the live stream), with its summary in the answer and at most two key pictures. `export` puts it in a
  GitHub PR or issue, only when the user asks.

**Test pictures for photo pickers**
- `media` adds pictures to the phone's photo library for an app's photo picker, after the user's OK, and removes only
  what it added. It needs the runner this release pins, tw-16.15.3.3, which has the photo routes (`tapwright setup
  --force` installs it); an older runner answers `unsupported` and says so.

## 1.4.0, "the Fewer Steps Update" (2026-11-13)

The same check should cost an agent no more calls on the iPhone than on Android. A QA pass found iPhone lanes took
about 1.5 times the calls: failed targets, scrolling to search, detours through iOS Settings, and a `look` after every
action. The interface is AGENT-TOOLS 1.11, shared with Tapline.

**See where the calls go**
- The action log now records reads too, which agent made each call, and how long each answer was (never typed text,
  pictures or log text).
- `session_report` (and `tapwright report`): calls per tool, failures by code, and the waste to cut, with call numbers:
  a `look` right after an answer that already had the changes, retries after `not_found`, scroll hunts, and actions
  that could have been one `run`.

**One call where it took several**
- `tap`, `long_press`, `toggle` and `type_text` on an element that is off screen scroll it into view and act, in one
  call (`Scrolled to it, then tapped …`). The safety check runs before the scroll.
- `not_found` names up to 3 closest matches (`Closest: e12 button "Save draft" id=save-draft, …`).

**No more trips through Settings**
- `open_app` takes `language`, `region` and `text_size` (`xs`…`xxxl`, `ax1`–`ax5`, `system`): the app opens as if the
  phone were set that way, for that launch only, through Apple's launch arguments. Nothing in Settings changes.
- `network`: `offline`, `3g`, `lossy` or `normal`, through the phone's own network conditioner. It affects the whole
  phone, every answer says so while it's on, and it goes back to normal by itself when the agent that set it goes
  quiet, the connection drops or the daemon stops. Over Wi-Fi, `offline` and `lossy` are refused, as they would cut
  Tapwright's own link.

**Fixes from the review of this release**
- A secure field's text never reaches a recording, even when the field was below the screen: the step is saved from the
  field `type_text` really typed into, after any scroll.
- A `network` condition lives only as long as the agent that set it. The agent's MCP process sends a heartbeat every
  few seconds and a goodbye when its stdio closes; the condition is cleared on the goodbye, 20 s after the heartbeat
  stops (the process was killed), after 10 minutes without a call, and at the 30-minute cap. A condition whose
  connection to the phone closes is noticed at once. A daemon that starts and finds a condition left by one that
  crashed clears it before any agent's first call.
- One agent never clears another's condition by accident: a failing `run` clears only the condition it set, a
  replaced or ended condition is told to its owner in its next answer, and a goodbye from one agent leaves the
  other's condition alone.
- `network` checks a per-app policy against the app in front now, not the one at the last read, and refuses when the
  phone can't say which it is and a policy blocks `network` for some app.
- A plain `open_app` (or `text_size: system`) after one with a language, region or text size closes the running app
  and opens it with the defaults, and says so.
- `tap`, `long_press`, `toggle` and `type_text` with an `index` on an off-screen match follow that element through the
  scroll by its own identifier (when nothing else in the tree has it) and act on it, never on another match, and the
  policy checks the element that is touched. A match without an identifier of its own is scrolled into view and not
  touched (`stale`: look, then target it by its ref): as a list scrolls its matches move, drop and change, and an
  `index` can then name another one.

**Habits that save calls**
- The skill has `run` examples for common patterns and a "spend fewer calls" list: read the change list instead of
  calling `look`, target by id, let the action scroll, and put known steps in `run`.
- The shared benchmark (QUALITY-BAR 2.4) adds calls per check: five everyday checks on Wayfare, held to no more calls
  than Android.

## 1.3.1 (2026-11-13)

Small fixes from an app tester's pass.

- `tapwright doctor` checks where Tapwright runs from. In a git checkout with uncommitted changes it warns; with
  unresolved merge markers (`<<<<<<<`) in the package it fails. An installed package is fine, and no git means no
  check. The setup docs and the agent guide now say that QA and unattended runs use a pinned release in their own
  venv, never a development checkout.
- A "needs a person" list: `doctor` shows, under `Needs a person:`, what only the phone's owner can clear and can
  be read (the XCTest passcode prompt, a locked phone, Developer Mode off, a computer the phone doesn't trust), with
  what to do. The first answer each agent gets carries the same as `Note:` lines.
- A tool called with an argument it doesn't take is now refused over the HTTP API too: `bad_params: audit doesn't
  take labels, ocr; it takes types.` The same holds for the steps of `run` and scripts.
- `audit`: a finding whose element isn't in the screen's tree is named by XCTest's own label or id and frame
  (`an element "Total" at @16,300 120x20`) before it falls back to "the screen".
- The live view's page for a link without a valid key now says what to do (run `tapwright view`, or ask the agent to
  use its `viewer` tool), instead of one line a person couldn't act on.

## 1.3.0, "the Diagnostics Update" (2026-11-13)

A failure now comes with its cause: the app's log, its crash, and how it performed. The interface is AGENT-TOOLS
1.10, shared with Tapline.

**The app's log**
- `logs`: the app's own entries in the phone's unified log (os_log, NSLog, and React Native's console in a release
  build), from the app's process only, never another's. `level`, `grep`, `limit` and `since` narrow it; each answer
  ends with `Latest:` to pass back as `since`.

**Crashes**
- When the app crashes during a call, the answer says so in one line (`Note: the app crashed: …`), on error answers
  too; a crash found later is told once on the next answer, with its time.
- `crash`: the app's crash reports on the phone since the session began: when, the exception and the top frames,
  the app's own marked. `pull: true` copies the full report to `~/.tapwright/crashes/`.

**Performance**
- `performance`: memory and CPU of the app's process and the screen's frame rate, while waiting, around a `tap`, or
  from a cold start to the first settled screen (`launch: true`).
- In `run` and scripts a `performance` step can hold limits: `"expect": {"under": {"launch_s": 2, "memory_mb": 300,
  "drops": 0}}`.
- A limit of its own: a batch of samples already in flight when the window opens carries no timestamp, so it can't be
  told apart from the window's own and can skew a number at the very edge of the window. Samples are counted by when
  they arrive, not by when the phone took them. Samples still waiting when the window ends, or lost to a process list
  that couldn't be read, are said, and fail memory and CPU limits.

**Failures explain themselves**
- A `run` or `replay` step that fails adds the app's warnings and errors since that step began. `tapwright replay
  --junit` puts them in the test case's output, and pulls a crash report next to the script.

## 1.2.0, "the Regression Update" (2026-11-12)

A test now checks how screens look, not only what they say. The interface is AGENT-TOOLS 1.9, shared with
Tapline.

**Visual checks**
- `compare_screen`: the first call for a `name` saves the screen as a reference picture; later calls answer
  `Matches` or `Differs`, with how much changed and where (each region named by the elements it touches), and on a
  difference a picture with the changes in red. `threshold` is the percent that may change and still match
  (default 0.5); `ignore` paints out elements or areas that always change. The status bar is never compared.
- In `run` and scripts it is a check: `expect: {"matches": "home"}` on a step, or a `compare_screen` step. A screen
  that differs stops the batch as `expect_failed`, naming where it changed.
- `record` folds a `compare_screen` after an action into that step, and saves the reference pictures next to the
  script in `<script>.references/`, so a script and its pictures travel together. A `run` made while recording keeps
  its visual checks too (`expect.matches` and `compare_screen` steps). A script keeps one picture for each name, so a
  recording that compares one name in two apps is refused.
- `tapwright replay` takes `--update-references` to save new references after an intended design change. A failed
  check writes its picture to `<script>.diff/`, and the JUnit failure names it. A picture
  far larger than any screen, or one a script's reference would stretch to such a size, is refused before it is
  resized. A recording refuses an update that would change a picture it already compared, before anything is written.
- The safe write covers the reference pictures (the store's and a script's `.references/` folder) and a script's
  `.diff/` folder, and nothing else: they are written whole or not at all, and never through a link (a symlinked
  folder is refused, a symlinked file is replaced). On Linux and macOS every folder is held open while the file is
  written. On Windows the folders are checked again before and after the move, which narrows the window for a folder
  swapped for a link, but can't close it: a process running as the same user that swaps a folder at that very moment
  can still get a write through.

**Watching motion**
- `filmstrip`: several frames in one numbered picture, for animations and loading states, taken from the live
  stream. With `tap`, frame 1 is taken before the tap and the other frames follow it, so the strip shows what the tap
  sets off. A filmstrip is composed in memory. With `tapwright mcp --screenshots DIR`, filmstrips and the picture
  `compare_screen` returns for a difference are saved to that folder by the ordinary screenshot writer, which is
  outside the safe write above.

## 1.1.1 (2026-11-12)

- A touch that had to close the keyboard first failed with an internal error while a layer was open in front
  (an AutoFill menu, a sheet). Found by an app's release-candidate pass on the phone.

## 1.1.0, "the Autopilot Update" (2026-11-12)

Agents get more done per call, a session becomes a test you keep, and every touch that matters asks you
first. The interface is AGENT-TOOLS 1.8, shared with Tapline.

**A faster tap**
- Tapwright installs its own build of the on-phone runner (Appium's WebDriverAgent 16.15.3 plus a fast tap and drag,
  github.com/bo5amis/WebDriverAgent, checked by its SHA-256). Over USB a tap is 0.47 s (0.69 before) and a
  scroll 1.07 s (1.45); over Wi-Fi a tap and a toggle are within Wi-Fi's bar. `TAPWRIGHT_WDA=appium tapwright setup --force` goes back to
  Appium's runner. After upgrading, `tapwright doctor --fix` (or `setup`) sees the older runner and installs this one.

**Do more per call**
- `run`: several steps in one call, each an ordinary tool call with an optional `expect` (text, id or
  label appears or goes; a value; a timeout). It stops at the first step that fails, says which, and keeps
  that step's own error code; a missed expectation is `expect_failed`. One answer for the whole batch.
- `dry_run: true` on a touch tool or on `run`: what it would do, the safety policy included, without touching.

**From a session to a test**
- `record` saves what the agent does as a script (the agent-tools script format Tapline shares); `replay`
  runs it again.
- `tapwright replay FILE… (--apps ID[,ID] | --dedicated) [--junit OUT]` runs scripts with no agent, for CI: each
  reopens its app and waits for it, exit 0 when all held, 1 when one stopped, 2 when one couldn't start. The apps
  the scripts may use come from `--apps` (or `--dedicated`), never from a script's own `app`; with neither it
  refuses and exits 2.
- `tapwright install` takes `--policy FILE` and `--no-policy`. A rerun keeps the entry's `--policy`, `--no-policy`
  and `--screenshots`, and says so when it replaces a policy. The config file keeps its permission mode, and a
  symlinked config is written through.

**Find problems for the developer**
- `audit`: Apple's own XCTest accessibility audit of the screen (contrast, tap targets under 44x44 pt,
  missing labels, clipped text, text that doesn't scale, traits) plus duplicate ids; each issue names the
  element and the fix.
- `look` with `ocr: true` reads the text in the picture that no element carries (games, canvases, web
  views) as refs `o1`, `o2` a touch can take. Optional: `pip install "tapwright[ocr]"`.

**Safety**
- A touch on Buy, Purchase, Subscribe, Pay, Delete, Remove, Send, Post, Publish, Sign out, Log out or Erase
  needs a person's OK in the live view (Allow once / Don't allow). `tapwright mcp --policy FILE` sets your
  own words and blocks; `--no-policy` turns it off for a test phone with test data.
- A touch by position is refused as `stale` when the picture there changed since the agent's last look.
- The action log: one JSON line per action in `~/.tapwright/log/`, typed text never written, two weeks kept.

**Setup**
- `tapwright install --client claude|codex|cursor --apps …` connects an agent in one go: the MCP server and
  the skill.
- `tapwright doctor --fix` repairs the usual breaks (developer disk image, WebDriverAgent missing or its
  profile running out, the network pairing); `doctor` warns 30 days before the signing expires.
- `tapwright setup` run again skips what is done and ends with the next commands.

**Fixes**
- Closing the keyboard before a touch: the touch goes inside a list first, then a short pull on the list,
  and WebDriverAgent's slow attempt isn't repeated in an app where it failed. A control the keyboard still
  covers is refused with how to close it.
- A person's OK is taken only from the live view page, with request ids that can't be guessed.

**Live view**
- Every control by keyboard with a visible focus ring; keys on the page's own controls never reach the
  phone; screen readers hear each step; high contrast and reduced motion; the confirmation prompt.

## 1.0.0

The first release: an agent sees and drives a real iPhone from Windows, Linux or macOS, through
WebDriverAgent and a developer tunnel, over USB or Wi-Fi; the live view; personal mode.
