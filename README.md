# Azira Download Part 01

Standalone Windows CLI for four existing Azira business capabilities. It uses
the existing SQL Server databases and stored procedures; it does not deploy a
new database or run the broader HD-panel/store-summary workflow.

## Status

The package and offline tests are implemented. A separate virtual environment
has been installed and the CLI checked outside the original repository.
Read-only S3 access and SQL prerequisite visibility have been checked using
temporarily authorized legacy credentials. Standalone AWS profiles, SQL
integration parity and production cutover are not verified.
Polygon masterfile processing and `all` are blocked by default until the
masterfile key mapping is verified. No production pipeline was run during
implementation.

The separate `download` command downloads S3 files without SQL access, inventory
updates, imports or email. It does not require the masterfile import mapping gate.

`Azira_S3_Bucket Synchronization` is a separate, installed capability for recent
HDv2 files in week-only local folders. On 2026-11-05 it downloaded 29 files
(2,545,627,420 bytes) across six week folders; the repeat run skipped all 29.
It used the operator-supplied local signals credential file, not external profiles.

## Requirements

- Windows with Python 3.11. This is the verified runtime; other versions have not
  been certified. The environment still requires its Python base installation.
- SQL Server connectivity under the current Windows identity.
- Both `SQL Server` and `ODBC Driver 17 for SQL Server` ODBC drivers, matching
  the legacy procedure and bulk-loading connection paths.
- Two external AWS profiles, or the normal boto3 credential provider chain.
- Access to the existing data directories and SQL objects described below.

The runtime dependencies are pinned to the directly observed original versions.
The old repository and its virtual environment are not runtime dependencies.

## Setup

From this repository's root in PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item config.example.toml config.local.toml
```

Edit the ignored local configuration for your environment. Use a test SQL
instance for integration validation, including all three server settings and
the two database settings. All values are top-level TOML fields. Unknown fields
and invalid types are rejected. TOML single-quoted strings support literal
Windows backslashes.

Configure `azira-signals` and `azira-partner` using your approved AWS credential
workflow outside this repository. Never add credentials to TOML or source files.
An empty profile string selects boto3's default provider chain. Do not copy the
legacy credential module. Profiles identify the existing signals and partner
accounts; bucket access must be provisioned separately.

## Commands

### Azira_S3_Bucket Synchronization

This independent capability synchronizes all file types in `s3://signals1.5/hdv2/`
whose S3 `LastModified` timestamp is within the last six weeks (42 days, UTC).
It does not use SQL, send email, decompress files, or run imports. It is not
included in the processing `all` command or the general-purpose `download` command.

```powershell
.\.venv\Scripts\python.exe -m azira_download_part01.s3_bucket_synchronization
```

After reinstalling the editable package, the standalone executable is also available:

```powershell
.\.venv\Scripts\azira-s3-sync.exe
.\.venv\Scripts\azira-part01.exe azira-s3-bucket-synchronization
```

Defaults target `I:/Near_download/HD_Panel_Data_Source/HDv2_Download`.
The month folder is removed, while the week and any nested file paths are retained:
`hdv2/2026-10/2026-10-11/example.zip` becomes
`HDv2_Download/2026-10-11/example.zip`. The six-week window uses object modification
metadata, not the week folder date. Older objects and folder markers are ignored.
Local files are never deleted merely because they disappear from S3 or age out.

The synchronizer reads the two literal signals credential assignments from the
local credential module without executing its code. By default it looks for
`AWS_Credentials.py` beside the synchronizer; `sync_credentials_file` can instead
point to an external file. Credential values are not logged or copied. The file
is gitignored. Do not build or distribute a package while credentials reside
inside its source directory; keep them externally for distribution. If the file
is absent, the synchronizer uses the configured signals profile/default AWS chain.
An explicitly configured missing credential file should be corrected before use.

Optional top-level TOML settings are `sync_bucket`, `sync_prefix`, `sync_root`,
and `sync_credentials_file`; pass `--config config.local.toml` to either executable
or the module. Leave the import mapping gate disabled.

`.azira_s3_sync.json` in the destination records S3 key, size, ETag, modification
time and the local modification time. New files, changed remote metadata, or
missing/modified local files trigger downloads. Files without a prior metadata
record download once to establish their identity, even if already present.
ETags are compared as metadata, not assumed to be MD5 checksums. Transfers use
temporary files and verify size plus a fresh S3 HEAD before replacing local files.
Failures return exit code 1; already completed files remain recorded for reruns.

### Download Files Only

After configuring external AWS credentials and local directories:

```powershell
.\.venv\Scripts\azira-part01.exe download --source polygon-masterfile --config config.local.toml
.\.venv\Scripts\azira-part01.exe download --source geographic-polygons --config config.local.toml
.\.venv\Scripts\azira-part01.exe download --source ppoi-change-logs --config config.local.toml
.\.venv\Scripts\azira-part01.exe download --source all --config config.local.toml
```

This lists all pages under the configured prefixes, selects full masterfile
TSV gzip files, geographic JSON files or PPOI ZIP files, and downloads them.
There is no date filter in this download-only path. `all` downloads all three
S3 sources sequentially; municipality Excel files are local inputs, not an S3
source. No manually supplied S3 key or matching SQL inventory row is needed.

Full S3 keys are preserved under `masterfile_root` for masterfiles and
`signals_root` for partner files. For example, `hdv2/2026-10/2026-10-04/file.tsv.gz`
downloads to `<masterfile_root>/hdv2/2026-10/2026-10-04/file.tsv.gz`.
This layout is independent of the legacy SQL import layout and does not certify
or reconcile its mapping. Keep `masterfile_mapping_verified = false`.

Existing files with matching S3 listing sizes are skipped; this is not a checksum
comparison. Missing or different-sized files download to temporary files, are
size-checked, and then replace their destinations. AWS or transfer errors return
exit code 1. Downloads require S3 list/get access, local disk space and external
AWS credentials, but do not connect to SQL or send email.

### Download And Process

```powershell
.\.venv\Scripts\azira-part01.exe --help
.\.venv\Scripts\azira-part01.exe polygon-masterfile --config config.local.toml
.\.venv\Scripts\azira-part01.exe geographic-polygons --config config.local.toml
.\.venv\Scripts\azira-part01.exe ppoi-change-logs --config config.local.toml
.\.venv\Scripts\azira-part01.exe municipality-core-metrics --config config.local.toml
.\.venv\Scripts\azira-part01.exe all --config config.local.toml
```

`--config` is accepted before or after the command. Without it, non-secret legacy
defaults apply and AWS uses the default credential chain. `python -m
azira_download_part01` is also supported. Imports and help do not create clients,
download data, send email or open SQL connections.

`all` runs masterfiles, geographic polygons, PPOI logs and municipality metrics
sequentially. An uncaught exception stops the batch and returns exit code 1.
Existing internally swallowed errors remain swallowed; exit code 0 is not a
strong guarantee that every legacy SQL operation completed successfully.

The processing commands above perform real downloads, table replacement/deletion, procedure
execution, inventory updates and email once configured. There is no dry-run
mode. Do not run production commands as a smoke test.

## Masterfile Mapping Gate

The original HDv2 downloader strips the first 13 characters from a full S3 key
to construct the local relative path. Read-only SQL checks found month-prefixed
inventory keys, while local files live under date-only directories. These are
different identities and must not be used interchangeably.

`masterfile_strip_chars` controls the S3-to-local offset.
`masterfile_inventory_strip_chars` controls the S3-to-SQL-inventory offset.
The local path removes their difference from the inventory key. The example
proposes 13 and 5 respectively for an `hdv2/YYYY-MM/YYYY-MM-DD/...` S3 layout.
Both in-code offsets default to 13 for compatibility with earlier configurations;
use the explicit example settings only after checking the real S3 layout.

Before enabling masterfile processing, obtain a corresponding full S3 key,
latest SQL inventory key and existing local file. Against an approved environment:

```powershell
.\.venv\Scripts\azira-part01.exe verify-masterfile-mapping --config config.local.toml --s3-key '<full S3 key>' --inventory-key '<SQL name_key>'
```

This command performs an S3 HEAD and a parameterized SQL SELECT, then checks the
local file. It requires an exact configured mapping and one latest inventory
row, and compares file sizes across all three locations. It does not download,
update SQL, commit a transaction, send email or modify configuration.
It verifies identity and size, not content checksums or procedure correctness.

After a successful check, confirm the rule applies across the masterfile key
layout, then set `masterfile_mapping_verified = true`. Adjust
both strip offsets and `masterfile_prefix` only from verified evidence.
The proposed S3 offsets are not a verified deployment mapping.

Read-only validation on 2026-11-05 found no external AWS profiles or credential
environment variables. At the operator's request, credentials were subsequently
loaded from the original credential module inside temporary verification
processes only; no credential values were printed or copied into this repository.
The standalone runtime continues to use external profiles/provider chains.
Five recent inventory candidates included month folders
but did not have corresponding local files at their exact inventory paths.
The local `2026-10-04/Signals_Polygon_Masterfile_20261015_252.tsv.gz` exists
(109,238,258 bytes) and matches S3 listing metadata for
`hdv2/2026-10/2026-10-04/Signals_Polygon_Masterfile_20261015_252.tsv.gz`,
but no inventory row matches that filename. None of 121 latest masterfile
inventory entries had a corresponding local compressed file under the proposed
date-only mapping. The newest older inventory candidate also returned S3 404
when checked at `hdv2/` plus its inventory key. Therefore a
complete S3/SQL/local identity-and-size match is still pending. Do not enable
the write gate or insert inventory rows merely to make verification pass.

See [VALIDATION.md](VALIDATION.md) for the read-only validation record and gates.

Acquisition lists S3 with pagination, downloads only full masterfile TSV gzip
files, excludes diff/unrelated HD-panel files, and retains local skip-existing
behavior. It reconciles inventory for downloaded and already-existing matches
before applying the original newest-vintage import query. Ambiguous mapped
keys are rejected. Existing changed local files are still skipped, as in the
legacy downloader; inspect such files before importing.

## Data And SQL Contracts

| Capability | Source | Load and procedure |
| --- | --- | --- |
| Polygon masterfile | Full masterfile `.tsv.gz` under the configured signals bucket HDv2 prefix | Replace `dbo.signals_polygon_masterfile_import`; call `dbo.usp_Signals_polygon_masterfile_import` |
| Geographic polygons | Partner bucket `signals_weekly_jsons/*.json` | Delete/append `dbo.signals_geog_import`; call `dbo.usp_Process_geofences` |
| PPOI logs | Partner bucket `ppoi_change_log/*.zip` and updated/added/closed/removed CSVs | Append `dbo.PPOI_Change_Log` and `dbo.PPOI_Change_Log_Summary` |
| Municipality metrics | Latest local `core_metrics_HDV2_external_YYYYMM_NorthAmerica.xlsx` | Drop/reload `dbo.core_metrics_municipality_import`; call `HD_Panel_Store_Level_Summary_v2.dbo.usp_core_metrics_municipality_import` |

SQL prerequisites vary by capability. Only masterfile tracking uses
`dbo.Azira_S3_files_inventory`; masterfile selection also uses the `spm` synonym.
Audit/email calls use `dbo.usp_comment` and `dbo.usp_send_near_email`.
Geographic polygons and PPOI logs do not read or write SQL inventory in Python.
Procedure definitions and their downstream objects remain external; their
internal inventory dependencies have not been certified. The Windows identity
needs inventory reads/writes for masterfiles, staging
creation/drop/delete/insert rights, and procedure execution rights. Email uses
the existing SQL mail procedure and configured recipients.

Geographic polygons track downloads and processing in
`<signals_root>/.azira_geojson_state.json`, comparing bucket, key, size, ETag,
S3 modification time and local file modification time. They list S3 with
pagination and retain the 360-day metadata filter. Downloads use temporary
files and verify size plus fresh HEAD metadata before replacing files; existing
copies are backed up. One pending file is processed per run and marked locally
only after the SQL processing call succeeds. Other files stay pending for later
runs; failed SQL processing can be retried without downloading again.

No SQL inventory is consulted to initialize local state. On the first run,
eligible files without a local record are downloaded and queued, even if they
were previously processed under the old inventory. Back up the local state file;
removing it can cause files to be processed again. This changes tracking only:
staging deletion/loading, the geofence procedure and email still use SQL.

PPOI logs use the same local-tracking approach in
`<signals_root>/.azira_ppoi_state.json`, without consulting SQL inventory.
S3 listing is paginated and retains the 360-day metadata filter. All new or
changed ZIPs are downloaded with size and fresh HEAD metadata verification;
one pending archive is extracted and appended to the detail and summary tables
per run. It is marked processed locally only after both table appends succeed.
Other downloaded archives stay pending. Existing ZIPs are backed up before
replacement, and unchanged processed archives are skipped.

The first run queues eligible archives without local records even if historical
SQL inventory marked them processed. Keep the state file backed up. PPOI appends
are not deduplicated or one atomic transaction: deleting local state, processing
a changed archive, or retrying after partial SQL success may create duplicate
detail/summary rows. SQL table loading and completion email remain enabled.

## Preserved Limitations

- Geographic/PPOI discovery retains a 360-day date filter, despite legacy names
  referring to 60 days. Both now use paginated S3 listing.
- Geographic/PPOI download a batch but process only one pending file per run,
  tracking downloaded and processed files locally instead of in SQL inventory.
- PPOI appends can duplicate detail and summary rows on rerun. Missing CSV
  types remain optional; an entirely missing/empty set may fail during loading.
- SPM vintage date comes from the relative directory, not the filename date.
  The schema preserves string values and adds `OpenStatus = open`.
- GeoJSON keeps the existing ten-million-row read limit, 2D WKT conversion,
  six staging columns and chunk sizes. No reprojection or geometry repair
  was added.
- Municipality skips when a vintage is already in staging, even if a previous
  post-import procedure failed. It retains the three worksheets, six key
  columns and Oct/Nov/Dec 2025 exclusions.
- Replacing/clearing staging and running procedures are not one atomic
  transaction. SPM/geofence calls do not gain core-metrics-style result draining
  or nonzero-return-code checks.
- No automatic retries, scheduler, dashboard, automatic vintage-date
  refresh or broader HD-panel/SLS steps are included.

Extraction-only safety changes are explicit configuration, external credentials,
lazy CLI imports, required directory creation, resource cleanup, parameterized
inventory key updates, invalid filename diagnostics, and rejection of archive
path traversal. Data transformation and legacy load contracts are retained.

## Verification And Cutover

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m pip check
```

Tests use synthetic TSV/gzip, GeoJSON, PPOI CSV/ZIP and Excel data. SQL, AWS,
email and audit calls are mocked. They check CLI routing/isolation, selection,
transformed rows, inventory semantics, staging options, skips, deferred SQL
errors and the read-only mapping verifier. This is not certification of the
deployed stored procedures or production-scale memory/performance.

Before cutover, compare legacy callable output and this package on the same
approved non-production SQL snapshot in separate sequential runs, resetting
between runs. Verify schemas/rows, downstream procedure effects, inventory
flags and notification payloads. Never execute the old inventory script merely
to obtain a baseline: it contains hardcoded top-level imports and an `exit()`.

Stop scheduling the four overlapping old routines before enabling this project.
Do not run both against shared staging/inventory concurrently. Keep the old
repository unchanged for rollback. If rolling back, stop the new schedule first,
inspect partial staging/inventory state, then re-enable the old routines with
the operator's approval. No schedules or remote repository have been created.

See [Azira_Download_Part_01_Plan.md](Azira_Download_Part_01_Plan.md) for the migration plan.