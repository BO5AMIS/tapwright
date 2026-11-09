# Live viewer protocol

Shared by Tapline and Tapwright, so one viewer page (and later an adapter for T3 Code's
Device panel) can show either tool's live picture of a phone, with an overlay of the
elements and input from a person. Version 1, agreed between the two projects; not
published. Version 1.1 (2026-11-09) adds the error codes `not_actionable` and `internal`;
agreed between the two projects and approved by the user.

- **Server:** a tool's own process: Tapline's host, or `tapwright serve`.
- **Client:** the viewer page, or an adapter.

## Access

- HTTP and WebSocket on the server's viewer address: `127.0.0.1` by default, the computer's
  Tailscale address only when the server is started with its remote-viewer option.
- Every request carries the server's **viewer token**. The token is separate from any app or
  device secret and is never written to logs.
  - Non-browser clients (adapters, scripts) send `Authorization: Bearer <token>` on every
    request, including the WebSocket handshake.
  - `?token=<token>` is accepted only on the page load that sets the cookie. The server
    answers that load with the page itself and sets the cookie (`HttpOnly; SameSite=Strict;
    Path=/`), with `Referrer-Policy: no-referrer` and `Cache-Control: no-store`; the page
    then removes the query from the address bar (`history.replaceState`). Not a redirect:
    when the link is clicked on another site, Chrome sends the redirected request as
    `cross-site` and without the `Strict` cookie, and a reload stays that way (measured), so
    the page would never load. No other request accepts the token in the URL.
  - A browser page served by the server itself authenticates `/stream` and `/ws` with that
    cookie.
  - A request authenticated by the cookie is refused when it carries a `Sec-Fetch-Site`
    header whose value isn't `same-origin` or `none` (cookies are shared across ports, and
    `SameSite` ignores them: a page on `localhost:3000` is the same site).
- Servers check the `Host` header (against DNS rebinding) and, on WebSocket upgrades,
  `Origin`: the page's own origin, or none.
- Action endpoints outside this protocol don't accept the cookie at all (Tapwright's agent
  API at `/api/*` is Bearer only).

## Coordinates and time

- Positions are `0..1` of the frame **as shown, upright**; the server handles orientation.
  Sizes in `0..1` are fractions of the frame's width and height.
- Times (`X-Captured-At`, `at`) are the server's clock in epoch milliseconds.

## `GET /stream`

`multipart/x-mixed-replace; boundary=frame`. Each part is one JPEG with `Content-Type:
image/jpeg`, `Content-Length`, and:

| Header | Meaning |
|---|---|
| `X-Seq` | increasing number, per server |
| `X-Captured-At` | when the frame was captured, if the server knows it; otherwise when the server received it |
| `X-Coverage` | `screen` (the whole phone screen) or `app-window` (only the app's own window) |
| `X-Source` | `wda` or `viewshot` |
| `X-Tree-Rev` | optional: the element-list revision the frame matches |
| `X-Size` | optional: `<w>x<h>`, the frame's size in points |

Latest frame wins: a slow client gets the newest frame and skips stale ones, never falling
behind. A server may stop capturing while no client watches. An `img` element can show the
stream as is; a `fetch`-based client reads the headers.

## `WS /ws`

JSON text messages. Both sides ignore types and fields they don't know.

**Client to server**

| Message | |
|---|---|
| `{ "type": "gesture", "points": [[t_ms, x, y], …] }` | one pointer's path (no multi-touch), sent on release; `t_ms` starts at 0 on the first point. The server decides: tap (short, little movement), long press (held 500 ms or more), drag. |
| `{ "type": "scroll", "x", "y", "dx", "dy" }` | scroll at `x`, `y` by `dx`, `dy` (fractions of the frame); `dy > 0` shows content further down, like a mouse wheel |
| `{ "type": "text", "text": "…" }` | type into the focused field |
| `{ "type": "key", "key": "backspace" \| "enter" }` | |
| `{ "type": "button", "name": "home" \| "volumeUp" \| "volumeDown" }` | |
| `{ "type": "pause", "on": true }` | pause or resume the agent |
| `{ "type": "overlay", "on": true }` | send overlays (`false` to stop); servers send none until asked, since reading the elements can be slow |
| `{ "type": "quality", "preset": "sharp" \| "balanced" \| "light" }` | stream quality, for everyone watching |

**Server to client**

| Message | |
|---|---|
| `{ "type": "status", "protocol": 1, "state", "detail", "via", "fps", "driver", "paused", "size": { "w", "h" }, "accepts": […], "quality" }` | `state`: `live`, `connecting`, `reconnecting`, `locked` (the phone is locked; the server can't unlock it) or `no-device`. `detail`: optional plain text (needs setup, phone not found …). `via`: how the phone is reached (`usb`, `wifi`, `tailscale`). `driver`: `agent`, `person` or `null`. `size` in points. `accepts`: the input this server supports, from `tap`, `longPress`, `drag`, `scroll`, `text`, `enter`, `backspace`, `home`, `volume`, `quality`; clients hide the rest. `quality`: optional, the current stream preset (`sharp`, `balanced`, `light`) when the server accepts `quality`. |
| `{ "type": "overlay", "rev", "at", "elements": [{ "ref", "type", "label", "id", "rect": { "x", "y", "w", "h" } }] }` | the elements on screen while overlays are on. `rev` matches `X-Tree-Rev` when the server has revisions; `at` is when it was read, so a client can dim an overlay older than the latest action. |
| `{ "type": "action", "by": "agent" \| "person", "kind", "label", "rect", "point", "at", "ok", "code" }` | what was just done, for the viewer to show (a highlight or a tap mark). `rect`, `point` (in 0..1), `at`, `ok` optional; when `ok` is `false`, `code` says why (see errors). |
| `{ "type": "error", "message", "code" }` | `code`, optional and machine-readable: `busy` (a person is driving), `paused`, `stale` (the screen changed; look again), `locked`, `reconnecting`, `unsupported`, `unauthorized`, `not_actionable` (the input reached the app, but a real touch there would do nothing or couldn't reach what's there; the message starts with the reason word, as in AGENT-TOOLS.md §4, e.g. `no_action: Nothing at (120, 640) takes a touch.`), `internal` (a bug or a failure on the phone side; the message says which) |

## Control

One action queue per server, for the agent and people alike. A person's input goes first:
an agent action waits until the person has been idle for about 3 seconds and gives up after
about 30 with `busy`. While paused, agent actions are refused with `paused`. An action by
element ref from before the screen changed is refused with `stale`; by id, label or text it
is looked up again.

## Per tool

| | Tapwright | Tapline |
|---|---|---|
| `X-Source` / `X-Coverage` | `wda` / `screen` | `viewshot` / `app-window` |
| Frames | about 22 per second (10 on the light preset), full size, over USB (measured) | on change, for about a second after each action, and once a second otherwise; at most about 3 per second (a capture takes about 30-40 ms on an iPhone, measured) |
| `locked` | the phone is locked | also while the app is in the background (iOS pauses it; only the person can bring it back) |
| `X-Tree-Rev` | none | yes (React commits) |
| Overlay | slow to read (1.5 to 7 s on busy screens, measured), so only on request | quick, on request |
| `accepts` | everything | `tap`, `longPress`, `scroll`, `text`, `enter`, `backspace`, `quality` (capture size) |
| Sees | the whole screen: keyboard, status bar, system alerts | only the app's window: no keyboard, status bar or system alerts |
| A person's input refused | never `not_actionable`: viewer taps are real touches | `not_actionable` with the reason (`no_action`, `covered`, `under_tab_bar`, `disabled` …), since Tapline calls handlers instead of touching |

T3 Code's Device panel is reached through a separate adapter in each tool, not through this
protocol.
