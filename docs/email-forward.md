# Email forward (capture by forwarding a confirmation)

Each person gets their own address, like `trips+k3x9q2mzt7ab@example.com`.
Forwarding a booking confirmation to it puts the booking in **their** review
queue a few minutes later. Nothing is filed until they approve it.
(SPEC-9 D-162, D-190; YONDER-199.)

## How it works

- **You own the mailbox.** Run·dere reads one mailbox you control, such as
  `trips@example.com`, over IMAP. It never sends mail from it.
- **The address identifies the person.** `trips+<token>@example.com` is
  delivered to `trips@example.com` (plus addressing), and the token says whose
  capture it is. The sender doesn't matter, so people can forward from any of
  their accounts. The token is random, and each person can regenerate theirs
  under **Capture Hub → Other ways to capture → Email forward**. The old address
  then stops working.
- **Mail to an unknown plus address is dropped.** A token that matches no one
  (a regenerated one, a typo), mail that's too large, and mail over the rate
  limit are moved to the dropped folder and counted. Run·dere never stores it,
  and it never becomes an ownerless capture.
- **Everything else is left alone.** Mail that isn't sent to a `trips+…@`
  address, including the bare `trips@` address and anything else in a shared
  mailbox, is never read past its recipient headers, moved, marked read or
  recorded.
- **Captured mail** is kept raw in full (D-15), filed into the done folder in
  your mailbox, and processed at once, like a capture from the Capture Hub.
  The same message is never captured twice for the same person.
- **Read in real time.** Run·dere holds an IMAP IDLE connection, so the mail
  server tells it the moment mail arrives. If the server doesn't support IDLE
  (most do: Gmail, Fastmail, iCloud, Microsoft 365, Dovecot), it reads every
  `TX_INBOX_POLL_SECONDS` instead. The admin panel shows which mode is in use.
- **"Got it" email.** When SMTP is set up ([email.md](email.md)), the person
  gets a "Got it: <subject>" email as soon as their forward is captured. It goes
  to the email on their Run·dere account, never to whoever sent the message.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `TX_INBOX_ADDRESS` | `""` | The mailbox's address, e.g. `trips@example.com`. People's addresses are built from it. |
| `TX_INBOX_IMAP_HOST` | `""` | IMAP server, e.g. `imap.gmail.com`. Empty = email forward off. |
| `TX_INBOX_IMAP_PORT` | `993` | |
| `TX_INBOX_IMAP_SSL` | `true` | `false` = a plain connection upgraded with STARTTLS (usually port 143; Proton Bridge's 1143). |
| `TX_INBOX_IMAP_CA_FILE` | `""` | Trust exactly this certificate (PEM) instead of the system's CAs, e.g. Proton Bridge's self-signed one. The hostname isn't checked then: the pinned certificate is the guarantee. |
| `TX_INBOX_IMAP_VERIFY` | `true` | `false` turns certificate checks off entirely. Only for a link you trust; prefer `TX_INBOX_IMAP_CA_FILE`. |
| `TX_INBOX_IMAP_USER` / `TX_INBOX_IMAP_PASSWORD` | `""` | The login. Use an app password. It's read from the environment only, never stored in the database or logged. |
| `TX_INBOX_FOLDER` | `INBOX` | Where forwarded mail arrives. |
| `TX_INBOX_DONE_FOLDER` | `Rundere/Captured` | Captured mail is moved here. Created if missing. |
| `TX_INBOX_DROPPED_FOLDER` | `Rundere/Dropped` | Mail that wasn't captured is moved here. Created if missing. |
| `TX_INBOX_IDLE` | `true` | Read in real time with IMAP IDLE when the server supports it. |
| `TX_INBOX_POLL_SECONDS` | `120` | Without IDLE: how often the mailbox is read (at least 30). |
| `TX_INBOX_ACKNOWLEDGE` | `true` | Send the "Got it" email (needs SMTP and an email on the person's account). |
| `TX_INBOX_MAX_BYTES` | `10000000` | Larger messages are dropped without being downloaded. |
| `TX_INBOX_RATE_LIMIT` | `30` | Captures per address per hour. |
| `TX_INBOX_DEDICATED` | unset | Whether the inbox folder is Run·dere's alone (D-208). Unset: an admin decides on **Settings → Server mail**. `true` / `false`: fixed here, and the page can't change it. See [A Run·dere-only folder](#a-rundere-only-folder). |
| `TX_INBOX_DROPPED_DAYS` | `30` | Mail in the dropped folder is moved to Trash after this many days (SPEC-8 D-196). |
| `TX_INBOX_TRASH_FOLDER` | auto | Where cleared mail goes. Empty = the mailbox's own Trash (the `\Trash` folder). |
| `TX_RETENTION_MONTHS` | `12` | A captured email — Run·dere's copy and the one in the done folder — is removed this long after its trip ends (or after it arrived, with no trip). What was read from it stays. |

**Clean-up (D-196).** Once a day Run·dere clears its own two folders, and
nothing else in the mailbox: mail in the done folder goes to Trash on the day
Run·dere removes its own copy of that capture, and mail in the dropped folder
goes to Trash after `TX_INBOX_DROPPED_DAYS`. It always *moves* mail to Trash
and never deletes it outright, so anything can be recovered until the mail
provider empties the Trash. Captures still waiting in review are never removed.
`python -m app.cli retention --dry-run` shows what the next sweep would do.

Restart after changing them.

## A Run·dere-only folder

Mail in the inbox folder that isn't sent to a forwarding address is normally
left where it is (D-190), because the folder may be part of someone's own
mailbox. If only Run·dere reads the folder, that rule strands mail sent to the
bare address, a mistyped address or spam. Common cases are a mailbox of its
own, or a folder that only a mail filter fills (like the Proton example
below). In those cases, mark the folder Run·dere-only (D-208):

- **Settings → Server mail → Run·dere-only folder → Make it Run·dere-only…**
  The page first shows how many messages in the folder would move to the
  dropped folder, and warns again if the folder is `INBOX`. Nothing changes
  until you confirm.
- Or set `TX_INBOX_DEDICATED=true` to fix it in the server's configuration.

From the next read, mail without a forwarding address moves to the dropped
folder. Only the outcome is recorded ("unmatched: not sent to a personal
address"), with no sender, subject or body. The dropped folder is emptied to
Trash after `TX_INBOX_DROPPED_DAYS`. Leave this off if the folder is part of a
personal inbox.

## The mailbox must accept plus addressing

Mail to `trips+anything@example.com` has to arrive in `trips@example.com`.
Gmail, Google Workspace, Fastmail, iCloud and Microsoft 365 all do this, as do
most hosts. On your own mail server, set the recipient delimiter to `+`
(Postfix: `recipient_delimiter = +`). A catch-all on a dedicated domain also
works.

### Example: a Gmail account

Use a dedicated account, not your personal one. Turn on 2-step verification
and make an [app password](https://myaccount.google.com/apppasswords).

```
TX_INBOX_ADDRESS=your-trips-address@gmail.com
TX_INBOX_IMAP_HOST=imap.gmail.com
TX_INBOX_IMAP_USER=your-trips-address@gmail.com
TX_INBOX_IMAP_PASSWORD=<app password>
```

Gmail shows the done and dropped folders as labels.

The server's TLS certificate is always checked (the system's trusted CAs and
the hostname), so the mailbox password can't be collected by anyone in the
middle. The admin panel shows which mode is in use.

## Example: Proton Mail through Proton Bridge

Proton has no plain IMAP; [Proton Bridge](https://proton.me/mail/bridge) provides
it, and plus addressing works on Proton addresses, custom domains included.
Three things trip people up:

1. **Bridge only listens on its own machine's `127.0.0.1`.** Inside the
   Rundere container, `127.0.0.1` is the container itself, so pointing
   `TX_INBOX_IMAP_HOST` there gives *connection refused*. Make Bridge reachable
   from the container — either:
   - **Run Bridge in its own container** on the same Docker network (a
     community image that wraps the official Bridge package; sign in once with
     its CLI). Use that container's name as `TX_INBOX_IMAP_HOST` and the IMAP
     port its README gives. This keeps working after a reboot without anyone
     signed in to a desktop.
   - **Keep the desktop Bridge and relay its port** to the Docker host, e.g. a
     `socat TCP-LISTEN:1143,bind=172.17.0.1,fork TCP:127.0.0.1:1143` service, add
     `extra_hosts: ["host.docker.internal:host-gateway"]` to the stack, and set
     `TX_INBOX_IMAP_HOST=host.docker.internal`. The desktop Bridge only runs while
     you're signed in to that desktop.
2. **Bridge's IMAP port uses STARTTLS**, so set `TX_INBOX_IMAP_SSL=false`.
3. **Bridge's certificate is self-signed.** In Bridge, *Settings → Advanced →
   Export TLS certificates* (desktop) and mount only `cert.pem` — never the
   exported `key.pem` — into the Run·dere container; set `TX_INBOX_IMAP_CA_FILE`
   to its path. Use the **Bridge password** Bridge shows for the account, not
   your Proton password.

4. **The IMAP account is the whole Proton account.** If the inbox address is
   one address of a bigger account, Bridge's IMAP username is that account
   (shown by Bridge's `info`), and its INBOX holds *all* of that account's mail.
   Run·dere only ever reads, moves or records mail sent to its own plus
   addresses (`trips+…@`); everything else, including mail to the bare
   `trips@`, is left exactly where it is and is never marked read. Still, give
   it its own folder: in Proton, add a filter *if the recipient contains
   `trips+` → move to folder `Rundere`*, and point the reader at it. Bridge
   shows Proton folders under `Folders/`, and new folders can only be made
   there, so name the done and dropped folders under `Folders/` too.

```
TX_INBOX_ADDRESS=trips@example.com
TX_INBOX_IMAP_HOST=proton-bridge              # the Bridge container's name
TX_INBOX_IMAP_PORT=143                        # 1143 when talking to Bridge directly
TX_INBOX_IMAP_SSL=false
TX_INBOX_IMAP_CA_FILE=/data/artifacts/proton-bridge-cert.pem
TX_INBOX_IMAP_USER=<the IMAP username Bridge's `info` shows>
TX_INBOX_IMAP_PASSWORD=<the Bridge password Bridge's `info` shows>
TX_INBOX_FOLDER=Folders/Rundere
TX_INBOX_DONE_FOLDER=Folders/Rundere Captured
TX_INBOX_DROPPED_FOLDER=Folders/Rundere Dropped
```

Bridge 3 keeps its certificate inside its vault rather than as a file, and a
Bridge in a container makes its own (different from a desktop Bridge's). Read
it off the connection once and pin that copy.

## Check it

1. **Capture Hub → Other ways to capture → Email forward.** Your address appears.
   Admins also see the mailbox status, when it was last read, any last error,
   and the last 7 days' counts.
2. Forward a confirmation to your address. Within seconds it's in your review
   queue, and the "Got it" email arrives if SMTP is set up.
3. Forward something to `trips+wrong@…`. It ends up in the dropped folder, and
   the admin panel counts it as "unmatched".

## When forwarded mail isn't picked up

Admins have three tools on **Settings → Server mail**:

- **Check connection** signs in and opens the folder read-only. It reports how
  many messages the folder holds, and how many of those are addressed to a
  forwarding address. The reader files those as it reads, so any that are
  still there after a read have been missed.
- **Read inbox now** does what the reader does, but from the first message in
  the folder, so it also takes mail the reader passed over. It reports what it
  did: captured, dropped, or left alone because it wasn't sent to a forwarding
  address.
- **Since start** adds up every read since the server started, including mail
  left alone and replies that had no UID. A server's reply with no UID is also
  logged as a warning, and that message is tried again on the next read.

If forwarded mail is still in the folder an hour later, the health monitor
raises an admin alert, "Email inbox: forwarded mail waiting", on
**Settings → Notifications** and to any alert target set up there. It finds
that mail with the server's own search, not the reader's, so it still works
if the reader misreads the server's replies.

Mail in the folder that isn't addressed to a forwarding address is left where
it is, by design: the folder may be part of someone's own mailbox.

## Gmail forwarding rules

To forward automatically, Gmail first emails a confirmation code to the target
address. That email arrives in your review queue like any other capture. Open
it, use the code or link, then ignore the item.
