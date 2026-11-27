# UN PASS — e-mail

## Where the settings go

In the `.env` beside `docker-compose.yml` (the same file that holds
`ESIGN_SENTINEL_KEY`). UN PASS reads the environment first, then `.env`.

    EMAIL_HOST=smtp.office365.com
    EMAIL_PORT=587
    EMAIL_USE_TLS=True
    EMAIL_HOST_USER=unpass@your.org
    EMAIL_HOST_PASSWORD=…
    DEFAULT_FROM_EMAIL=unpass@your.org
    SITE_URL=https://unpass.your.org      # links in e-mails point here

Then `docker compose restart web worker beat`. `.env` is git-ignored: it
survives `git pull` and replacing `docker-compose.yml`, which is how the mail
settings were lost before.

## Is it working?

    scripts/owner.sh unpass_doctor --mail
    scripts/owner.sh unpass_doctor --mail-test you@your.org

`--mail-test` sends a real message on the spot and prints the mail server's
own answer, with what it means. Every failed e-mail is now logged with that
answer and remembered, so `unpass_doctor` shows the last failure, and a
sender who sends an envelope while e-mail is broken is told on the page.

| The server says | Means |
|---|---|
| `535 5.7.139` | Office 365 refused the password: wrong password, or SMTP AUTH is off for the mailbox (Microsoft 365 admin → the user → Mail → Manage email apps → Authenticated SMTP) |
| `550 5.7.30` | Office 365 no longer accepts passwords here — use Graph (below) |
| `SendAsDenied` | `DEFAULT_FROM_EMAIL` is not the mailbox you sign in as |
| timed out | outbound 587 is blocked by a firewall |

## Office 365 after December 2026: Microsoft Graph

Microsoft switches password SMTP off by default for existing tenants at the
end of December 2026. Switch before then. In Entra ID: register an app,
give it the **application** permission Graph → `Mail.Send`, grant admin
consent, create a client secret, and (strongly recommended) restrict it to
the one mailbox with `New-ApplicationAccessPolicy`. Then in `.env`:

    EMAIL_BACKEND=accounts.mail_graph.GraphEmailBackend
    MS_GRAPH_TENANT_ID=…
    MS_GRAPH_CLIENT_ID=…
    MS_GRAPH_CLIENT_SECRET=…
    DEFAULT_FROM_EMAIL=unpass@your.org

No mailbox password is stored anywhere. Messages up to about 3 MB with
attachments go in one request; a larger one is refused with a clear error.
