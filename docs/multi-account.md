# Multiple dorm accounts — 1.7.0 (profiles) / 1.8.0 (all accounts scheduled)

## What changed

The dormitory check-in subsystem kept everything under **one** directory, so one machine could
only ever hold one student's settings, check-in points, login session and daily state. Version
1.7.0 made that directory per account and added a switcher on the 寝室打卡 page; version 1.8.0
made **every account that has 自动打卡 enabled run on its own schedule**, not just the one the
page happens to be showing.

The data layer needed almost nothing: `Store(root)` already hangs every file it owns —
`settings.json`, `location-points.json`, `location-sample.json`, `session/credential.dat`,
`signed.json`, `daily.json`, `pending.json`, `submit-attempts.json`, `status.json`,
`history.log`, `login.log` — off its root, and the map picker, the distance check against the
school's own published check-in point and the "already signed today" shortcut all read through
that store. "One account, one directory" therefore gives per-account settings, per-account
check-in points and per-account daily state with no changes inside `dorm_checkin.py`.

1.7.0 was **stage one**: exactly one `DormController` existed, switching accounts was a handover,
and only the active account was scheduled. 1.8.0 keeps one controller **per account**; switching
is now purely a view change, and the three constraints that a single account never needed live
at the pool level:

| Constraint | Where | Why |
| --- | --- | --- |
| 错峰 `STAGGER_SECONDS = 90` | `DormAccounts` assigns account *i* an offset of *i*×90 s; `DormController._stagger_ready` holds that account's first check of the day until window start + offset | several accounts share one IP; hitting the school API in the same second buys nothing |
| One login at a time | `dorm_login.LoginGate` (process-wide singleton, injectable) | the SSO hop is already sensitive to replay (`400` on mixed cookie generations), and the 2026-09-24 incident ended in a **machine-level account lockout** |
| Machine-wide daily cap `MAX_DAILY_MACHINE_LOGIN_RENEWALS = 6` | `dorm_accounts.MachineLoginBudget` at `accounts\login-budget.json` | the per-account cap of 3 is per account; the blast radius of N accounts is the machine |
| Unattended login must be possible at all | `DormController._unattended_login_blocker` | a login that provably cannot succeed must not be counted (see below) |

The order inside `DormController._run` is deliberate: window → **can we log in unattended at
all?** → per-account cap → machine budget → login gate → *then* count the attempt. A tick refused
because another account holds the gate, or because there is nothing to recover, must not burn this
account's daily allowance.

## Unattended login: two ways, and one hard "no"

Since 2026-10-05 the user's choice is that a **first login may also be unattended**. `login()` in
non-interactive mode therefore proceeds when either:

1. **Renewal** — valid school cookies exist; the SSO chain is re-walked with them (cheapest and
   most reliable);
2. **Full login** — no session at all, but unified-auth credentials are saved for that account:
   the headless browser opens the login page and `idm_login.attempt_silent_login` fills the
   credentials and solves the captcha with the local model (at most 2 submissions).

Both are bounded by the same per-account cap (3/day), the machine budget (6/day) and the login
gate (one at a time), and they only happen inside the account's own check-in window.

When **neither** is possible (no session and no saved credentials, or the credential file cannot
be decrypted) the attempt is refused *before* the browser is opened and — this is the 2026-10-05
fix — **before anything is counted**. The reason says what to do: 从没登录过 / 会话已过期 /
凭据解密不了, each ending in 「请点『学校登录 / 重新登录』…，或先在面板里保存该账号的统一认证凭据」.
The first version instead burned the account's daily allowance on a renewal that could never
succeed: the real machine showed two wasted attempts out of the account's three, two of the
machine's six, and a message that only said 「没有可恢复的学校会话」.

Two related fixes came out of the same evening: `login()` now reports
「统一认证提示『用户名或密码错误』」 in unattended mode as well (the most actionable thing to say
once a from-scratch login has really been attempted), and the "restored account does not match"
guard only applies when a session was actually being resumed — starting from nothing used to trip
it, which reported a *successful* first login as an error.

## Layout

```
%LOCALAPPDATA%\youziauth\
  accounts\
    accounts.json          # {v, active, accounts:[{id, name, created_at}]}
    <12-hex id>\           # one account profile, exactly the old dorm directory
      settings.json  location-points.json  location-sample.json  signed.json
      daily.json  pending.json  submit-attempts.json  status.json
      history.log  login.log  session\credential.dat
      idm\credential.dat   # unified-auth credentials for THIS account
```

`accounts.json` is read on every access (like `Store`), so an external edit takes effect at once.
A damaged or version-mismatched registry is reported, never silently rebuilt: rebuilding would
show the user an empty account list and look like data loss. Deleting the file is nevertheless a
supported repair — the next start adopts the surviving profile directories by name instead of
creating a new one, so nothing on disk is orphaned.

## Upgrading from ≤ 1.6.14

On the first start of 1.7.0, if `accounts.json` does not exist:

1. `%LOCALAPPDATA%\youziauth\dorm` is renamed (single atomic `os.replace`, not copy-and-delete)
   into `accounts\<new id>\` and registered as 账号 1. A failed rename is reported with both
   paths; nothing is half-migrated and no data is deleted.
2. `%LOCALAPPDATA%\youziauth\idm` is moved into that account's `idm\` when it can be attributed
   unambiguously (exactly one account). Otherwise it is left alone, because guessing whose
   credentials they are is worse than asking the user to save them again.
3. With no legacy data at all, a single empty 账号 1 is created.

Verified end to end against a synthetic legacy profile: settings, saved points, the daily
`signed.json` record and `idm\credential.dat` all survive; the old directories are gone; a
restart reuses the same account.

## Behaviour contract

| Item | Value |
| --- | --- |
| Accounts | at most 5 (`MAX_ACCOUNTS`); the last one cannot be deleted |
| Active account | `accounts.json → active`; selects what the 寝室打卡 page shows and edits. Switching is a view change and is never refused |
| Per-account state | schedule, check-in points, simulated sample, school session, unified credentials, daily counters, history |
| Automatic check-in | **every account with `enabled`**, on its own controller, its own window, its own interval, its own stagger offset |
| Cross-account guards | one login chain at a time (`LoginGate`), machine daily login budget (`login-budget.json`), 90 s stagger between accounts |
| Cancelling | `dorm_cancel` cancels every account (the button works even when only a background account is busy) |
| Deleting | refused for the account that is currently checking in; other accounts are unaffected |
| Bridge snapshot | `state.accounts = {error, max, active, busy, items:[{id, name, active, missing, has_idm_credentials, idm_username, enabled, state, message, busy, signed_today}]}` — no paths |
| Bridge actions | `account_add {name}`, `account_switch {id}`, `account_rename {id, name}`, `account_delete {id, confirmed:true}` |
| `state.dorm` | unchanged in shape; always describes the active account only |
| Missing profile | reported as `missing` per account; using it is refused with an actionable message, but switching away and deleting it still work |
| No account layer | injected controllers (tests, legacy callers) produce an empty `accounts` block and the UI hides the bar |

## Decisions worth keeping

- **The account name is user data, not an ID.** The identifier is a random 12-hex string used as
  a path component and validated against `[0-9a-f]{12}` on load, so a hand-edited registry
  cannot point a profile outside `accounts\`.
- **Credentials follow the account.** `DormController` carries an `idm_store`, and
  `dorm_login.login(..., idm_store=...)` threads it into `resolve_idm_credentials`. Falling back
  to the global store would silently sign one student in with another student's saved password,
  so the fallback only exists for injected/legacy callers.
- **Refusals must not cost the account anything.** The login gate is checked before the daily
  counters are bumped, and the machine budget before that: an account that is merely waiting for
  another account to finish logging in is not "one attempt poorer".
- **A busy controller is never closed while it works.** Deleting an account that is checking in is
  refused; the pool gate around poll/delete/close exists so a delete cannot read "not busy" in the
  sliver where a poll is starting a check. The test that covers this was checked against an
  ungated subclass, which reproduces the slip (delete wins, then the check is cancelled).
- **The account layer never raises from its constructor.** A window that cannot open cannot tell
  the user what is wrong. Construction records the failure, every operation then fails with that
  message, and `snapshot()` keeps the rest of the page working (same boundary-isolation rule the
  dorm settings block already follows).
- **`--legacy-ui` (Tk) keeps working without a switcher.** `CampusAuthGui` goes through the same
  account layer, polls **every** account and shows the active one in its panel; switching happens
  in the main desktop UI.

## Validation

- Python: `python -m unittest discover -s tests` → **684 tests, OK** (601 before 1.7.0), including
  `tests/test_dorm_accounts.py` (46), 20 new bridge tests, 3 for the legacy Tk scheduler and the
  2026-10-05 unattended-login cases (`login()` from scratch with saved credentials, bad-credential
  reporting, the blocker that refuses before counting).
  Covered: registry CRUD and limits, name cleaning, corrupt/empty/foreign-id registries,
  adoption after a deleted registry, legacy directory and credential migration, profile
  isolation (settings, points, token, credentials), per-account stagger offsets, the pool gate
  (a delete may not slip in while a poll is starting that account's check), missing-profile
  handling, the machine budget (cap, day rollover, damaged counter → fail-closed), the login gate
  (automatic skip without spending the allowance, manual refusal, release on failure), the legacy
  UI polling every account, and that a broken registry degrades only the dorm block of the
  snapshot.
- Frontend: `node --test tests/test_desktop_ui.cjs` → **111 tests, 111 pass** (91 before 1.7.0),
  including per-account status suffixes and their priority, the multi-account summary, account
  controls staying usable while a background account checks in, the cancel button lighting up on
  `accounts.busy`, the unsaved-edit confirmation, add / rename / delete payloads and
  `confirmed:true`, and old snapshots without the account layer.
- Integration smoke against the real factory (no network, isolated `LOCALAPPDATA`): first run
  creates one profile, two accounts hold different settings and different point lists, switching
  back restores them, and deletion removes exactly one directory.
- Stage-two smoke against the real controllers, with the clock moved into the check-in window:
  three accounts got offsets `[0, 90, 180]`; at 21:00:30 only the first attempted, at 21:01:30 the
  second joined, at 21:03:00 the third — each with its own daily counter at 1 and the shared
  `login-budget.json` at `used: 3`. `playwright.sync_api` was never imported, i.e. **no browser
  was launched**, and the login gate was free again at the end.
- Legacy-upgrade smoke: a synthetic pre-1.7.0 profile (settings, points, `signed.json`,
  `youziauth\idm`) is migrated on first start, the old directories are gone, the record and
  credentials are inside the account, and a restart reuses the same account.
- Demo path over real HTTP (`campus_auth_desktop.py --preview-server`): `GET /api/state` carries
  the two demo accounts, `POST /api/action` drives add / rename / delete / switch, a
  non-confirmed delete and an unknown id are both refused, and exactly one account stays active.
- Real-DOM smoke in Edge (outside the repo): 28 assertions across wide/narrow/full/unreadable
  states, no page errors. Screenshots kept at `build/qa/multi-account/dorm-accounts-wide.png`,
  `…-narrow.png` and `…-error.png` (the 1280×900 wide shot is also what the 寝室打卡 page looks
  like with the switcher in place).

## Known limits

- **Sequential, not parallel.** Accounts are staggered and share one login chain by design; a
  second account that needs to log in while the first is mid-login simply skips that tick and
  retries on its own `_renew_at` cadence (5 minutes). With 5 accounts the last one starts its
  evening 6 minutes after the window opens, which is fine for a two-hour window but worth knowing
  if someone configures a very short one.
- **Submissions are still per account and per task.** Nothing about the school's own windows,
  radius checks or read-back rules changed; each account is checked exactly as it was alone.
- **One machine, one IP.** Every account's browser login and API traffic leaves from the same
  computer. The machine budget bounds the total, but a school that fingerprints the client would
  still see several students behind one host — that risk cannot be engineered away here.
- **`--legacy-ui` (Tk)** shows and polls the active account only; it has no switcher, so use the
  main desktop UI to change which account it points at.
- **The school's check-in is a presence check.** Pointing a second account at its own dormitory
  with 模拟定位 means submitting for someone who is not standing there; that is the user's call,
  and the README now says plainly that several students' credentials share one machine.

## Reproduction

```powershell
python -m unittest discover -s tests
node --test tests/test_desktop_ui.cjs
# isolated end-to-end run (no network, no real user data):
$env:LOCALAPPDATA = "$env:TEMP\youziauth-smoke"
python campus_auth_desktop.py --preview        # demo snapshot with two demo accounts
```
