# Validation Record

Date: 2026-11-05

## Initial Scope And Authorization

The operator authorized read-only checks against the existing SQL Server and
databases, and temporary use of credentials from the original credential module.
Credentials were used internally, not displayed or copied into this repository.
No standalone AWS profiles or AWS credential environment variables are configured.
The operator subsequently chose to keep database checks read-only instead of
approving inventory reconciliation.

No imports, SQL writes, stored procedures, staging modifications, emails or
production schedules were executed. The masterfile import gate remains closed.

## Offline Verification

- 51 unittest tests cover the independent package, including the separate
   inventory/local mapping, local-path collision checks, S3-only downloads and
   the standalone six-week synchronizer.
- Initial installation, package compilation, dependency consistency, installed
  help outside both repositories and editor diagnostics passed.
- Synthetic gzip/TSV, Excel, GeoJSON and PPOI data exercise the transformation
  contracts with external systems mocked.
- This coverage does not certify deployed procedure effects or throughput at
  production data volumes.

## Read-Only Masterfile Evidence

| Item | Observation |
| --- | --- |
| Current S3 key | `hdv2/2026-10/2026-10-04/Signals_Polygon_Masterfile_20261015_252.tsv.gz` |
| S3 listing size | 109,238,258 bytes |
| S3 last modified | 2026-10-18 18:08:12 UTC |
| Local relative path | `2026-10-04/Signals_Polygon_Masterfile_20261015_252.tsv.gz` |
| Local compressed size | 109,238,258 bytes |
| Inventory rows for this filename | 0 |
| Latest inventory entries inspected | 121 |
| Corresponding local compressed files under the proposed mapping | 0 |

The observed local mapping removes the first 13 S3-key characters. Existing
inventory entries include a month directory, such as
`2026-08/2026-08-09/Signals_Polygon_Masterfile_20260820_249.tsv.gz`.
The example config proposes removing five characters (`hdv2/`) for SQL inventory
identity while retaining the 13-character local offset. The newest older
candidate returned S3 404 at `hdv2/` plus that inventory key. This may represent
obsolete inventory or a different historic namespace; it was not resolved by
changing data or inferring alternate keys.

There is no verified three-way S3/SQL/local sample. Matching the current S3 and
local sizes is not a content checksum and does not prove the historical SQL
identity rule. Do not set `masterfile_mapping_verified = true` yet.

## SQL Prerequisite Visibility

Metadata SELECTs on the existing SQL server returned visible object IDs for:

- `near_signals.dbo.Azira_S3_files_inventory`
- `near_signals.dbo.signals_polygon_masterfile_import`
- `near_signals.dbo.signals_geog_import`
- `near_signals.dbo.PPOI_Change_Log`
- `near_signals.dbo.PPOI_Change_Log_Summary`
- `near_signals.dbo.core_metrics_municipality_import`
- `near_signals.dbo.usp_Signals_polygon_masterfile_import`
- `near_signals.dbo.usp_Process_geofences`
- `near_signals.dbo.usp_comment`
- `near_signals.dbo.usp_send_near_email`
- `near_signals.dbo.spm`
- `HD_Panel_Store_Level_Summary_v2.dbo.usp_core_metrics_municipality_import`

Object visibility does not establish correct definitions, write/execute
permissions, dependency completeness, or successful procedure execution.

## Azira_S3_Bucket Synchronization

At the operator's request, a separate capability was added and run on 2026-11-05.
This authorizes S3 reads and local downloads only, not SQL processing or cutover.
The supplied local credential module is read for its literal signals credential
assignments without executing it; no credential values were displayed or copied.
The credential filename is ignored by Git. Standalone external AWS profiles
are still not configured.

- Source: `s3://signals1.5/hdv2/`.
- Destination: `I:/Near_download/HD_Panel_Data_Source/HDv2_Download`.
- Selection: S3 `LastModified` within the previous 42 days, measured in UTC;
   all file types, with pagination. Week directory dates are not the date filter.
- Mapping: remove the month directory, preserving week and nested file paths.
- Live preflight: 29 eligible objects, 2,545,627,420 bytes, six week directories,
   no mapped-path collisions and sufficient available disk space.
- First live run: 29 downloaded, zero unchanged. Size and fresh HEAD metadata
   were verified before replacement; local metadata records were written.
- Second live run: zero downloaded, 29 unchanged.
- Independent command: `azira-s3-sync`; editable installation succeeded using
   isolated build dependencies after the no-isolation attempt lacked `wheel`.
- No local file deletion, SQL operations, imports, notifications or scheduling.

The metadata comparisons are not content checksum certification. Historic SQL
inventory mapping remains unverified and is not a prerequisite for this capability.

## Geographic Local Tracking Update (2026-11-06)

Geographic polygons now use `<signals_root>/.azira_geojson_state.json` rather
than Python reads/writes to `dbo.Azira_S3_files_inventory`. S3 discovery is
paginated; the existing 360-day selection window, geometry transformation,
staging load, geofence procedure and email remain. One pending file is processed
per run. Local state retains downloaded-but-unprocessed files, detects same-size
ETag changes, and permits retrying failed SQL processing without redownload.

Focused offline tests exercise local tracking, pending files, retries, pagination,
empty discovery, unsafe paths, failed transfers and unchanged geometry output.
SQL inventory helper calls are prohibited by mocks in the tracking test.
No live geographic SQL workflow was run for this update. Stored-procedure
definitions remain external; internal inventory dependencies are not verified.
Files with no local record will be queued anew, regardless of historical SQL
inventory state.

## PPOI Local Tracking Update (2026-11-06)

PPOI change logs now use `<signals_root>/.azira_ppoi_state.json` rather than
Python reads/writes to `dbo.Azira_S3_files_inventory`. Discovery is paginated
with the original 360-day metadata filter. New/changed ZIPs download atomically
after size and fresh HEAD checks; one pending archive is processed per run.
The local processed flag is written only after detail and summary appends
complete. Downloaded archives remain pending after processing failures.

Offline tests prohibit SQL inventory helper calls and exercise pending files,
same-size ETag changes, unchanged skips, retry without redownload, pagination,
unsafe keys, collisions and transfer failures. Original CSV transformation and
summary-table tests remain. No live PPOI processing was run for this update.
Historical inventory does not seed local state. First-run reprocessing, changed
archives, lost state or partial SQL success can still cause duplicate appends.

## Remaining Processing Gates

The `download --source ...` command is independent of SQL processing. It needs
external AWS credentials and local download directories, but not the three-way
masterfile inventory mapping or SQL write approval. It preserves full S3 keys
under each download root; it does not prepare or validate the legacy import
layout. Download-only tests use mocked S3; live transfers with standalone
credentials remain unverified.

1. Resolve a real S3/SQL/local key triple, including the historic namespace or
   missing inventory state, without fabricating a validation record.
2. Configure the standalone external AWS provider without embedding legacy
   credentials or depending on the original repository at runtime.
3. Obtain explicit approval for any inventory updates or an isolated SQL test
   target. Current approval is read-only.
4. Compare procedure effects and output parity in approved sequential test runs
   from reset database snapshots; never run both projects against shared state.
5. Authorize cutover separately after validation. No commits, remotes, schedules
   or production executions have been created.