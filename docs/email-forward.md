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
  under **Capture → Other ways to capture → Email forward**. The old address
  then stops working.
- **Anything else is dropped.** Mail to an address that matches no one is moved
  to the dropped folder and counted. This includes the bare `trips@` address, a
  regenerated token, mail that's too large, and mail over the rate limit.
  Run·dere never stores it, and it never becomes an ownerless capture.
- **Captured mail** is kept raw in full (D-15), filed into the done folder in
  your mailbox, and processed at once, like a capture from the capture page.
  The same message is never captured twice for the same person.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `TX_INBOX_ADDRESS` | `""` | The mailbox's address, e.g. `trips@example.com`. People's addresses are built from it. |
| `TX_INBOX_IMAP_HOST` | `""` | IMAP server, e.g. `imap.gmail.com`. Empty = email forward off. |
| `TX_INBOX_IMAP_PORT` | `993` | |
| `TX_INBOX_IMAP_SSL` | `true` | `false` = a plain connection upgraded with STARTTLS (usually port 143). |
| `TX_INBOX_IMAP_USER` / `TX_INBOX_IMAP_PASSWORD` | `""` | The login. Use an app password. It's read from the environment only, never stored in the database or logged. |
| `TX_INBOX_FOLDER` | `INBOX` | Where forwarded mail arrives. |
| `TX_INBOX_DONE_FOLDER` | `Rundere/Captured` | Captured mail is moved here. Created if missing. |
| `TX_INBOX_DROPPED_FOLDER` | `Rundere/Dropped` | Mail that wasn't captured is moved here. Created if missing. |
| `TX_INBOX_POLL_SECONDS` | `120` | How often the mailbox is read (at least 30). |
| `TX_INBOX_MAX_BYTES` | `10000000` | Larger messages are dropped without being downloaded. |
| `TX_INBOX_RATE_LIMIT` | `30` | Captures per address per hour. |

Restart after changing them.

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

## Check it

1. **Capture → Other ways to capture → Email forward.** Your address appears.
   Admins also see the mailbox status, when it was last read, any last error,
   and the last 7 days' counts.
2. Forward a confirmation to your address. After the next read it's in your
   review queue.
3. Forward something to `trips+wrong@…`. It ends up in the dropped folder, and
   the admin panel counts it as "unmatched".

## Gmail forwarding rules

To forward automatically, Gmail first emails a confirmation code to the target
address. That email arrives in your review queue like any other capture. Open
it, use the code or link, then ignore the item.
