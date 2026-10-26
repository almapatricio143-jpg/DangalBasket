# DangalBasket

A Flask student marketplace and service-sharing platform with
enrollment-verified student registration, profiles, product and service
listings, stock-aware product orders, transaction management, seller reviews,
listing reports, and administrator tools.

## Run locally

The Flask development server runs in the foreground and stops when its VS Code
task or terminal closes. After reopening VS Code, start it from the project
root with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_local_server.ps1
```

The script uses the project's `.venv` Python and starts the Flask application
on `127.0.0.1:50003`. It refuses to start if that port is already listening
and reports the listener's process details when available. The VS Code
`Run DangalBasket` task uses the same script and can run automatically when the
workspace opens if automatic tasks are allowed; you can also run it manually
with **Tasks: Run Task** → **Run DangalBasket**. If the automatic task already
started the server, running the command again reports the existing listener
instead of starting a duplicate.

Successful startup prints Flask's `Running on http://127.0.0.1:50003` message.
Open `http://127.0.0.1:50003/` in the browser. Keep the task or terminal open
while developing; run the command again the next time you open the project.
The local development server watches Python files and reloads after code
changes; look for Flask's `Restarting with stat` message in the task output.
If startup reports that the port is occupied by another process, inspect the
reported PID and command line before taking any action.

## Public deployment on Render

The `render.yaml` Blueprint defines a **Free** web service, uses Gunicorn through
`wsgi.py`, binds to Render's `$PORT`, enables secure session cookies, and uses
`/tmp/dangalbasket-demo/` for its SQLite database and uploaded files. Production
startup refuses missing storage paths or paths inside the application folder,
so the deployed app cannot fall back to the local `instance/` database.

Render Free services have an ephemeral filesystem, spin down after 15 minutes
without traffic, and can take about a minute to resume. The demo database and
uploads can be lost on restart, redeploy, or spin-down. Do not use this free
demo for real user data, enrollment certificates, or information that must
persist. No local database, account, listing, order, message, or upload is
migrated. See Render's [free-tier limitations](https://render.com/docs/free).

To deploy, publish the reviewed source to a GitHub repository, then create a
Render Blueprint from that repository. Render generates `SECRET_KEY`; the
Blueprint sets `DANGALBASKET_ENV=production`, `SESSION_COOKIE_SECURE=1`, and
all demo storage paths. The health check is `/healthz`. After the first
deployment, create a demo user. To enable admin functionality, temporarily set
a one-time `ADMIN_BOOTSTRAP_TOKEN` in Render, run
`flask --app "app:create_app()" provision-admin YOUR_USERNAME` in the service
Shell and enter the token at the hidden prompt, then remove the token. This
controlled first-admin command approves the selected account so it can sign
in; it refuses to run after an administrator already exists.

The application has no configured email provider; account decisions and
notifications are in-app. No GitHub repository or Render service is created by
this project configuration.

Open `http://127.0.0.1:50003/` to sign in or choose **Create account**. After
signing in, the home page opens the marketplace. The SQLite database is created
at `instance/dangalbasket.sqlite3`. Sign in to publish listings, open **My
profile**, and place orders. Public member profiles are available at
`/users/<username>` and listings at `/listings/<id>`.

Members can update their name and an optional bio of up to 300 characters from
**Profile settings**, with a live profile preview and character count. Profile
photos accept JPG, PNG, or WEBP images up to 2 MB and appear on both the
settings preview and public profile. Choose a photo and select **Save changes**
to update it together with the name and bio; changes are previewed before saving.

The **Notifications** page separates order, message, and account updates,
supports category filters and paging, and marks updates as read only when opened
or when **Mark all as read** is selected. Receipt updates link to their
transaction details. Account notifications report administrator decisions about
student registration; rejected applicants can open the notice to resubmit.

The app safely adds `role` (defaulting existing/new accounts to `student`),
`avatar_filename`, `approval_status` (existing accounts default to `approved`),
registration-request and decision timestamps, `bio`, `certificate_filename`,
and the optional legacy `student_number` to the existing `users` table.
Registration requires a username, student number,
full name, password, and 1 to 5 supporting pictures, such as a registration
form, ID, or certificate of enrollment. Student numbers are unique without
regard to letter case. The registration form does not collect an email address;
existing email addresses remain stored and can still be used to sign in. New
accounts remain pending until an administrator approves or rejects them. The
48-hour review period is an expected response window, not an automatic
approval; pending accounts remain blocked after that period, and overdue
requests are prioritized in the admin review queue. Pending accounts cannot
sign in; rejected applicants can sign in only to view their account
notification and resubmit registration documents. Approval and rejection
notifications are stored once with the decision timestamp and next steps, and
are shown in the notification center after sign-in. Administrators can review
every submitted image.
Startup makes the existing email column optional while preserving account IDs,
email addresses, and referencing data. It also upgrades the existing orders
status constraint in place while preserving all order IDs and records, and
creates `reviews`, `listing_reports`, `order_events`, and `admin_actions`.
Startup performs this compatible SQLite schema update automatically; back up
the database before deploying an application update. Profile photos are limited
to 2 MB (`MAX_AVATAR_BYTES`) and listing images to 4 MB
(`MAX_LISTING_IMAGE_BYTES`). Registration document images are limited to 4 MB
each (`MAX_CERTIFICATE_BYTES`) and stored privately under
`instance/enrollment-certificates/`. Validated images are decoded and
re-encoded as PNG under their respective upload folders. Only administrators
can view pending registration documents from the student-registration review
page. Documents and their database references are removed when an administrator
records an approval or rejection; a failed deletion prevents the decision from
being committed.

### Back up and verify a restore

Use SQLite's backup API to take a consistent snapshot, rather than copying a
database file that may be in use. Run the following from the project root with
the same Python environment used by the application. Create the protected
backup directory first, keep it outside the repository, and choose a new
backup filename each time:

```powershell
$env:DANGALBASKET_BACKUP = 'D:\secure-backups\dangalbasket-before-update.sqlite3'
@'
import os
import sqlite3
from pathlib import Path

source_path = Path("instance/dangalbasket.sqlite3")
backup_path = Path(os.environ["DANGALBASKET_BACKUP"])
if not source_path.is_file():
    raise FileNotFoundError(f"Database not found: {source_path}")
if backup_path.exists():
    raise FileExistsError(f"Refusing to overwrite backup: {backup_path}")
with sqlite3.connect(source_path) as source:
    with sqlite3.connect(backup_path) as backup:
        source.backup(backup)
        integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = backup.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign_key_errors:
            raise RuntimeError(
                f"Backup verification failed: {integrity}; {foreign_key_errors}"
            )
print(f"Verified database backup: {backup_path}")
'@ | .\.venv\Scripts\python.exe -
```

To verify restoration without replacing the application database, restore into
a new file and run the same integrity checks:

```powershell
$env:DANGALBASKET_BACKUP = 'D:\secure-backups\dangalbasket-before-update.sqlite3'
$env:DANGALBASKET_RESTORE = 'instance/dangalbasket-restored.sqlite3'
@'
import os
import sqlite3
from pathlib import Path

backup_path = Path(os.environ["DANGALBASKET_BACKUP"])
restore_path = Path(os.environ["DANGALBASKET_RESTORE"])
if not backup_path.is_file():
    raise FileNotFoundError(f"Backup not found: {backup_path}")
if restore_path.exists():
    raise FileExistsError(f"Refusing to overwrite restore target: {restore_path}")
with sqlite3.connect(backup_path) as backup:
    with sqlite3.connect(restore_path) as restored:
        backup.backup(restored)
        integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_errors = restored.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign_key_errors:
            raise RuntimeError(
                f"Restore verification failed: {integrity}; {foreign_key_errors}"
            )
print(f"Verified restored database: {restore_path}")
'@ | .\.venv\Scripts\python.exe -
```

The example targets the Windows `.venv`; use the deployment's Python
executable and path syntax elsewhere. This database-only procedure does not
include profile photos, listing images, or enrollment certificates. For a
coordinated database-and-uploads bundle, use the procedure below. These local
procedures do not prove a production cutover. Protect backup files with
appropriate access controls and encryption, and periodically test retention
and restore access.

### Coordinated database and upload backup

The bundle command uses SQLite's online backup API for the database and
includes `profile-photos`, `listing-images`, and `enrollment-certificates`.
Before running it, stop all application workers and other processes that can
change the database or upload directories, and keep them stopped until backup
creation finishes. This quiescence is required because SQLite's backup
snapshot and the filesystem copy are not one atomic cross-resource snapshot.
Create a protected backup directory outside the repository and select a new
archive name each time:

```powershell
New-Item -ItemType Directory -Force 'D:\secure-backups' | Out-Null
.\.venv\Scripts\python.exe scripts\backup_restore.py backup `
  --instance-dir instance `
  --output 'D:\secure-backups\dangalbasket-20261015.zip'
```

The command refuses an existing output, verifies the copied database's
`integrity_check` and `foreign_key_check`, checks that every database-referenced
image/certificate exists, and verifies the ZIP CRC before publishing the
archive. The destination directory must already exist. Run backup only while
writes are stopped.

To restore, first stop application workers and choose a new destination path
that does not exist. The restore is staged and checked before it is published;
it refuses to overwrite an existing target:

```powershell
New-Item -ItemType Directory -Force 'D:\secure-recovery' | Out-Null
.\.venv\Scripts\python.exe scripts\backup_restore.py restore `
  --archive 'D:\secure-backups\dangalbasket-20261015.zip' `
  --destination 'D:\secure-recovery\dangalbasket-restored-20261015'
```

The destination contains `database.sqlite3` and the three upload directories.
The script verifies database integrity, foreign keys, and referenced files,
but does not switch the running application to the restored copy. Validate the
restored application in isolation before following the deployment's approved
database-path and upload-path cutover procedure. The ZIP format used here is
not encrypted; enrollment certificates contain sensitive student information,
so store bundles only in access-controlled encrypted storage and apply the
institution's retention and deletion policy. These tests and instructions do
not establish production recovery time, production cutover, or backup
availability.

Students can publish products or service listings. Products use the fixed
product categories in `app.py`, have positive unit stock, and can be ordered
through checkout. Service listings use the `Services` category, are
contact-only, and do not participate in stock management or checkout. The
listing form only shows and requires available quantity for products. Both
types can be searched and filtered on the marketplace and appear on sellers'
public profiles. Prices and product order-time price snapshots are stored in
integer cents. SQLite `BEGIN IMMEDIATE` plus a conditional stock update ensures
concurrent purchases cannot oversell. Marketplace browse and search show at
most 24 results per page and preserve the active query, category, and listing
type and price range while navigating between pages. Sellers can mark product
listings unavailable without changing their stock; unavailable listings remain
visible but cannot be ordered until the seller restores availability.
Products with stock at `0` are marked **PRE-ORDER**; products with any available
stock show **BUY NOW**. Pre-orders reserve any units still in stock, while
zero-stock pre-orders do not decrement inventory. Zero-stock requests are
explicit backorders: they do not consume or oversell inventory and may take
longer to fulfill. Removed listings remain unavailable. Order records store
their type and whether inventory was reserved so cancellation restores only
units actually reserved.
Sellers can open **My listings** to add stock to active or sold-out listings;
restocking allocates replenished units to outstanding pre-orders in creation
order before making any remaining stock available. Only fully covered pre-orders
are reserved, and their buyers are notified. Restocking a sold-out item makes it
active if stock remains; archived listings cannot be restocked.

Provide a persistent, secret `SECRET_KEY` outside source control for
deployments; without one, a random key is generated and sessions expire when
the process restarts. Set `SESSION_COOKIE_SECURE=1` when serving over HTTPS.
Set `DANGALBASKET_ENV=production` in production deployments. Production startup
fails safely if `SECRET_KEY` is missing or empty, or if
`SESSION_COOKIE_SECURE=1` is not set; configure the same persistent secret and
secure-cookie setting for every application worker. When `DANGALBASKET_ENV` is
unset or not `production`, local development retains the generated-key fallback
and does not require secure cookies.
Failed sign-ins are limited to five attempts per client IP per 15 minutes;
registration accepts five POST requests per client IP per hour. The limiter
stores only HMAC-keyed client-IP fingerprints in SQLite and prunes expired
counters as later requests arrive. Keep `SECRET_KEY` stable across restarts and
workers so those fingerprints remain consistent.

Orders are the transaction records; supported statuses are `pending`,
`confirmed`, `completed`, and `cancelled`. Sellers confirm pending orders and
mark confirmed orders completed. Buyers may cancel pending or confirmed
orders; cancellation restores reserved stock atomically and only once. Every
status change is recorded in `order_events`. Listing owners can archive a
listing only after its pending/confirmed orders are resolved. Archival uses the
existing `removed` status so historical order, report, and review records remain.

Only the buyer of a completed transaction can submit one 1–5 star review with
written feedback. Seller rating aggregates and feedback on profiles/listings
are calculated from those stored reviews. Authenticated users can report
listings; reports enter administrator review and do not automatically affect
listing visibility. Admins can inspect report and listing details, archive the
reported listing while preserving its transaction history, resolve a report,
or dismiss it with recorded resolution details. Startup upgrades existing
listing reports to support dismissed status and resolution details without
discarding report history.

Authenticated users can contact sellers from product listings and message
service providers from service listings. Conversations are associated with a
listing, buyer, and seller; both participants can read and reply from
**Messages**. Message content is limited to 2,000 characters, and users outside
a conversation cannot access it. Buyers review the quantity and
total at checkout, then choose **Cash**, **GCash**, or **Maya** before placing an order;
the selected method is recorded with the transaction, while payment itself is
arranged directly between the participants and is not processed by the app.
Open a conversation from **Messages** to see the listing and participant
details with the message history. Type a message and press **Enter** to send;
use **Shift+Enter** for a new line. Sending uses the existing participant-only,
CSRF-protected conversation form.
In-app **Notifications** track new messages and order creation/status updates,
with an unread count in the navigation.
Signed-in members can use **Report a bug / Contact admins** in the sidebar to
open one private, persistent support conversation with the administrators.
Administrators can reply to every member from **Admin support**; messages and
unread counts are shared across the administrator team. Support conversations
are visible only to their member and administrators.
Conversations, messages, support chat, payment-method storage, and notifications are created
or upgraded automatically by SQLite startup initialization; no separate
migration command is required. Existing orders receive the default payment
method, **Cash**.

## Administrator provisioning

Registration always assigns the `student` role. Admin access is checked against
the database on every admin request. To provision the first administrator,
set a one-time bootstrap token in the environment and run the Flask CLI command
for an existing account:

```powershell
$env:ADMIN_BOOTSTRAP_TOKEN = (& .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))")
.\.venv\Scripts\python.exe -m flask --app "app:create_app()" provision-admin EXISTING_USERNAME
Remove-Item Env:ADMIN_BOOTSTRAP_TOKEN
```

The command prompts for the token without echoing it and refuses to run if an
administrator already exists. Keep the bootstrap token private and unset it
after use. Admin pages are available at `/admin`.
Existing administrators can promote approved student accounts to administrator
from **Admin dashboard → Manage users**; each promotion is recorded in
`admin_actions`.

Administrators can review each account's total and pending listing-report
counts from **Manage users**, then ban a non-administrator account with a
required reason or reinstate it later. Bans remain in effect until an
administrator reinstates the account; banned users cannot sign in or continue
using an existing session. Ban and reinstatement decisions are recorded in
`admin_actions`, and the ban reason and administrator are retained while the
ban is active. Listing reports also link directly to the seller's account and
report history. Reports do not automatically ban accounts.

## Transactions and reports

Sign in and choose **My transactions** to filter buyer/seller orders and status.
Sellers confirm and complete orders; buyers may cancel before completion.
Completed buyers may review the seller once. Report a listing from its detail
page. An owner can archive their listing after active orders are resolved.
Administrators can review registration documents and approve or reject
registrations, and manage users, listings, reports, and transactions from the
admin dashboard. Registration decisions and listing/report actions are audited.

## Run tests

```powershell
python -m unittest discover -s tests -v
```

The tests use temporary SQLite databases and disable CSRF only in the ordinary
test configuration. CSRF protection remains enabled by default for browser
forms.
