# NYRA to Kazumi — 0.6 migration

The product and assistant are now **Kazumi**. Previous releases remain historical
NYRA releases; their tags, issues and source history are not rewritten.

## Preserved data and compatibility

- `KAZUMI_*` deployment variables are preferred; `NYRA_*` are accepted for one
  migration release. A customized wake word is retained; the old `nyra` default
  becomes `kazumi`. Voice IDs, provider choices and personalization are preserved.
- Existing Windows credentials are securely copied from `NYRA_CRED:` to
  `KAZUMI_CRED:` and verified inside Credential Manager. Legacy entries remain for
  rollback. Removing a credential explicitly removes both aliases. DPAPI entropy
  remains the legacy value so existing encrypted vaults remain decryptable.
- TTS provider readiness checks the protected vault after restart, not the
  broker's transient metadata index; existing keys do not need re-entry.
- The Tauri application identifier remains `local.nyra.desktop` for upgrade,
  WebView storage and single-instance compatibility. It is not current branding.
- Historical SQLite table `nyra_identity_v1` and identity ID `nyra-core-v1` remain
  stable persistence keys. The identity name changes without replacing Memory V2,
  relationship state, Open Loops or emotion history.
- Old `NYRA_RESPONSE`, `NYRA_EMOTION_CHANGED` and
  `NYRA_EMOTIONAL_PRESENCE_SYNCED` events are accepted and normalized; new events
  use `KAZUMI_*`. Old frontend preference keys are copied once without overwriting
  already configured Kazumi preferences.
- Existing `.nyra-project.json` and PlatformIO `env:nyra` projects remain readable
  and buildable; new metadata uses `.kazumi-project.json`. Existing firmware can
  keep the nonce-bound `NYRA1` protocol. A bounded read-only handshake discovers
  the protocol before any effectful command; no reflash is required for a rename.
- Third-party VTS rigs and expressions are not renamed. Legacy plugin/token and
  parameter identities remain compatible; new plugin registrations use Kazumi.

## Default path migration

Third-party identifiers are not product branding. In particular, the npm
dependency `tinyrainbow` retains its upstream spelling and integrity hash.
Legacy `ON_NYRA_START` settings remain readable as `ON_KAZUMI_START`.

Persisted pronunciation dictionaries also migrate the old product rule:
the default spoken form "Naira" becomes "Kazumi", including partially migrated
dictionaries. Other custom terms, provider choices and voice IDs are retained.

| Legacy | Current |
| --- | --- |
| `E:\nyra` | `E:\Kazumi` |
| `%LOCALAPPDATA%\NYRA` | `%LOCALAPPDATA%\Kazumi` |
| `E:\NYRA-Projects` | `E:\Kazumi-Projects` |
| `E:\NYRA-Knowledge` | `E:\Kazumi-Knowledge` |
| `E:\Nyra-Auto-Code` | `E:\Kazumi-Auto-Code` |
| `E:\NYRA-GitHub-Public` | `E:\Kazumi-GitHub-Public` |

Custom paths must be inventoried explicitly. Stop owned application/build/flash
processes first. Checkpoint Git and uncommitted changes, protect configuration
backups, and record database integrity/table counts. Projects and knowledge are
copied and hash-verified before archiving their original directories. Same-volume
runtime moves are verified and rolled back if verification fails. Update only
structured path fields, not conversation history or arbitrary user prose.

Never merge two already-populated runtime databases automatically. Preserve both
and resolve the conflict before starting. Repair Git worktree references using
Git's supported repair command after moving SelfDev or repository roots.

## Rollback and publication

Keep the pre-migration Git checkpoint, protected configuration backups and original
credential entries until validation finishes. On failure, stop only owned Kazumi
processes, restore original directory names and configurations, and relaunch the
previous executable. Do not reset or clean the operator repository destructively.

Knowledge, projects, vaults, logs, audio, databases and third-party VTS assets stay
private. Public synchronization includes reviewed source, tests and documentation
only. Cloud voice tests require configured credentials; hardware effects require
real compatible devices and evidence, never fixtures presented as physical state.
