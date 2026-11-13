# Personas for usability journeys

A persona is a short description of one kind of user, so a journey is judged by the right eyes. No tool enforces it:
you build it, agree it with your user, and keep to it while you test. A persona's conditions are values of
`language`, `region`, `text_size` and `network`; pass these as `conditions` on `journey { start }`; each tool applies
them its own way.

## Build one for the app at hand

Ask the user who really uses this app, then write five lines per persona:

1. **Who:** age, job, how often they use the app, what device they have.
2. **Knows:** what they already know about the app and about apps like it.
3. **Wants:** the one goal of this journey, in their words ("log my breakfast").
4. **Situation:** where they are and what else is going on (on a bus, one hand free, in a hurry).
5. **Conditions:** the `language`, `region`, `text_size` and `network` that fit, if any.

Two or three personas that differ a lot beat six that are alike. Name the persona in `journey { start }`, and read each
screen as they would: what they would notice first, what they would tap, where they would hesitate.

## Examples to copy, extend or ignore

**First-timer.** Has never seen the app; knows only what the store page told them. Wants to finish the first task
without reading help. Reads every label, hesitates at jargon and at icons without words, never scrolls to find a
hidden option. Conditions: none, a fresh install.

**Busy regular.** Uses the app daily, knows where things are, wants it done in as few taps as possible, one hand, while
walking. Notices every extra step and every changed button name. Conditions: `network: 3g`.

**Older user with large text.** Reads slowly, avoids small targets, does not guess what an unlabelled icon does. Wants
the same task with text at the largest size, so cut-off text and overlapping controls show up.
Conditions: `text_size: ax5`.

**Right-to-left speaker.** Reads Arabic; expects the layout mirrored, back arrows pointing the other way, numbers and
mixed text in order. Wants a task done in their own language. Conditions: `language: ar · region: QA`.

**Poor connection.** On a train or abroad, the network drops and crawls. Wants to know what happened when something
does not load, and to retry without losing what they typed. Conditions: `network: 3g`, and a `network` stress step to
`offline` at the worst moment (a field half filled, a form being sent).

**One-handed on a big phone.** Holds a large phone in one hand and can only reach the lower half with a thumb. Wants
to finish the task without stretching or using the other hand. Conditions: none, a large phone.

Each line above is a starting point: change the details to match the app's real users. Write the conditions on one line
the way the examples do, for example `language: ar · region: QA · text_size: xl · network: 3g`.
