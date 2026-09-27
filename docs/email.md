# Email (password-reset links)

Run·dere sends one kind of email: a one-time password-reset link. It talks to
**any SMTP server**; nothing is tied to a provider. Without SMTP, reset still
works — an admin copies a link from **Settings → Users → Reset link**.

## Settings

| Variable | Meaning |
|---|---|
| `TX_SMTP_HOST` | SMTP server host. Empty = email off. |
| `TX_SMTP_PORT` | `587` for STARTTLS (default), `465` for implicit TLS. |
| `TX_SMTP_SSL` | `true` for implicit TLS (port 465). Default `false`. |
| `TX_SMTP_STARTTLS` | Upgrade with STARTTLS when not using SSL. Default `true`. |
| `TX_SMTP_USER` / `TX_SMTP_PASSWORD` | Login, if the server needs one. |
| `TX_SMTP_FROM` | Sender address. Defaults to `TX_SMTP_USER`. |
| `TX_PUBLIC_BASE_URL` | How users reach Run·dere, e.g. `https://yonder.example.net`. Reset links are built from it; without it they use whatever URL the request came in on. |

## Example: Gmail with an app password

Gmail needs 2-step verification on the account and an
[app password](https://myaccount.google.com/apppasswords) (16 characters);
your normal Google password won't work.

```
TX_SMTP_HOST=smtp.gmail.com
TX_SMTP_PORT=587
TX_SMTP_USER=you@gmail.com
TX_SMTP_PASSWORD=<app password>
TX_SMTP_FROM=you@gmail.com
TX_PUBLIC_BASE_URL=https://yonder.example.net
```

(Port 465 also works with `TX_SMTP_SSL=true`.) Gmail rewrites the sender to
the authenticated account, so keep `TX_SMTP_FROM` the same address.

Any other provider or a local relay works the same way — only the host, port
and login change.

## Check it

1. Set your email under **Settings → Account**.
2. **Settings → Users → Email delivery → Send test.** A failure shows the
   server's actual error.
3. Sign out and use **Forgot password** to receive a real reset link.
