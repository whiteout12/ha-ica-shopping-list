# ICA Shopping List for Home Assistant

Your ICA shopping lists as Home Assistant to-do lists. Add, tick off, rename and
remove items from a dashboard or an automation, and see changes made in the ICA
app within a few minutes.

Unofficial, and not affiliated with ICA.

## Install

**HACS → ⋮ → Custom repositories**, add
`https://github.com/whiteout12/ha-ica-shopping-list` as an **Integration**,
install it, restart Home Assistant, then **Settings → Devices & services → Add
integration → ICA Shopping List**.

Setup asks for your ica.se email and password, then shows your lists so you can
pick which ones become to-do entities. You can change that selection any time
under **Configure**, and lists created in ICA later appear there.

## Saving the password

The checkbox during setup decides what happens when your ICA session ends, which
it will — a session is not permanent.

| | Session ends |
|---|---|
| **Password saved** | Home Assistant signs in again by itself. You never see it. |
| **Password not saved** | The integration asks you to sign in again, and its lists stop updating until you do. |

Saved means stored in Home Assistant's config entry, in plain text, exactly as
every other integration stores credentials. Not saving is a real choice, not a
safer default — it just moves the work to you.

## What it does not do

**Quantities.** ICA rows can carry one; Home Assistant's to-do items cannot. So
this integration never sets a quantity — and, importantly, never destroys one.
Ticking an item off here leaves the "3" someone typed in the ICA app alone.

**Due dates and descriptions.** ICA has nothing to map them to.

**Reordering.** ICA stores a position and it could be supported later.

## How it holds a session

Worth knowing if you are wondering why the code is shaped the way it is. All of
it was measured against the live API rather than guessed, by a throwaway soak
harness built for the job.

- **A token is not proof of a session.** An expired ICA session still returns an
  access token, and that token is refused by the list API. A session is live when
  a token came back *and* `loginState` is not 0. Requiring `loginState == 1`
  instead rejects perfectly good sessions, which report 2 often enough to matter.
- **Signing in again reuses the stored cookies.** A login from a clean slate
  creates a *second* session and leaves the first one running, so an integration
  that renewed that way would strand a live session on your account every time.
  Signing in through the previous session's cookies replaces it. That is why the
  session is persisted, and it is why exactly one exists no matter how long this
  runs.
- **Updating an item sends the whole row back.** ICA has no partial update and no
  version field, so an update built from a Home Assistant to-do item would blank
  every field it cannot represent. Updates start from ICA's own row instead.
- **Two people editing at once: last write wins**, silently. ICA offers nothing
  to detect it with. Refreshing before each write narrows the window; it cannot
  close it.

## Development

```bash
pip install -r requirements-test.txt
pytest
```

The tests pin ICA's behaviour, not this code's structure — every one of them
covers something that was wrong at some point and failed quietly when it was.

## Licence

MIT.
