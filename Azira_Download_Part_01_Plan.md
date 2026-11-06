# Plan: Azira Download Part 01

Date: 2026-11-05

## Agreed Boundary

Create a separate Windows Python CLI repository at
`I:\Near_download\signals\python\Azira Download Part_01`. Use the existing SQL
Server databases and procedures. Migrate exactly four capabilities with strict
lift-and-shift transformations and loading semantics:

1. Polygon masterfile download (masterfiles only) and import.
2. Geographic polygon import.
3. PPOI change logs.
4. Municipality core metrics.

Provide individual commands plus `all` in that order, stopping on uncaught
errors. Do not move the dashboard, full HD-panel downloader, address fixing,
forecasting, SLS/store-summary workflow, scheduler or database platform.
Keep the original repository unchanged.

## Phase 1: Baseline And Mapping

1. Verify a corresponding full masterfile S3 key, SQL `name_key`, and local
   relative file path. The original downloader strips 13 characters; do not
   assume this matches SQL identity. Confirm prefix layout and diff exclusion.
2. Establish anonymized fixtures and expected rows for TSV/gzip, GeoJSON,
   PPOI ZIP/CSV and the three-sheet municipality workbook. Never execute the
   original inventory script for discovery because it runs hardcoded imports
   at top level.
3. Record SQL inventory, synonym, staging, PPOI, procedure and mail/audit
   prerequisites. Obtain a matching non-production database for integration
   comparisons; deployed procedure internals are not in the original source.
4. Inspect the working Python/dependency versions and ODBC drivers. Preserve
   legacy connection semantics, including Windows authentication and mixed
   SQL Server/ODBC Driver 17 paths.

## Phase 2: Standalone Foundation

5. Check the sibling destination for existing work, then create packaging,
   configuration, CLI, documentation and tests without modifying old code.
6. Pin the used direct dependencies to observed compatible versions. Exclude
   unrelated runtime packages and create a separate virtual environment.
7. Externalize SQL settings, data roots, bucket/prefix settings and recipients
   using non-secret TOML. Use two external AWS profiles/default provider chains;
   never copy the old credential module.
8. Extract only shared schema, inventory, download/backup, ZIP, SQL audit/mail
   and SQL-result helpers. Replace wildcard imports, remove import-time work,
   and create required output directories.

## Phase 3: Capability Extraction

9. Masterfile: retain inventory vintage selection, gzip/schema/renames,
   directory-derived vintage date, `OpenStatus`, staging replace, procedure
   invocation, inventory marking and notifications. Reuse paginated S3
   acquisition for full masterfiles only. Reconcile both downloaded and skipped
   existing files with SQL inventory using the verified mapping. Keep the write
   path disabled until deployment mapping is confirmed.
10. Geographic polygons: retain partner discovery, filename metadata,
    ten-million-row limit, 2D WKT conversion, six output columns, delete/append,
    geofence procedure and inventory/notification order. Retain first-row-only
    import and batch inventory append. Do not add automatic vintage refresh.
11. PPOI: retain optional updated/added/closed/removed CSVs, metadata columns,
    non-updated area nulls, renames, detail append and category/country/vintage
    pivot append. Keep first-row processing and existing rerun behavior.
12. Municipality metrics: retain latest YYYYMM selection, DataPoints/MAUs/DAUs,
    first six identifiers, fixed month exclusions, melt, existing-vintage skip,
    staging drop/load, NVARCHAR(500), count verification, result draining,
    cross-database procedure and notifications. Workbook acquisition stays local.

## Phase 4: CLI And Verification

13. Add `polygon-masterfile`, `geographic-polygons`, `ppoi-change-logs`,
    `municipality-core-metrics`, `all`, and a read-only mapping verifier.
    Keep imports/help offline. Preserve internally swallowed legacy errors;
    do not promise stronger completion guarantees than those implementations.
14. Test transformed columns/values, source selection, date/vintage semantics,
    inventory flags/order, staging options, notifications, skip/failure paths,
    CLI routing and safe imports. Mock external systems and use synthetic data.
15. Validate fresh installation and the installed command outside both source
    roots, package compilation, dependency consistency and source isolation.
16. On an approved test SQL environment, compare old callable and new outputs
    from the same clean snapshot in separate runs. Reset between runs and
    compare downstream procedure effects before production enablement.
17. Document cutover and rollback. Disable overlapping old routines before
    enabling new scheduling; never concurrently use shared staging/inventory.

## Source Mapping

| Original source | New owning module |
| --- | --- |
| SPM_import.py; filtered S3PatternDownloader logic from Claude.py | src/azira_download_part01/polygon_masterfile.py |
| PythonGeoJSON_import.py; selected SQL_processing_JSON.py functions; process_jsons | src/azira_download_part01/geographic_polygons.py |
| File_inventory.py PPOI functions; file_list.py log writers | src/azira_download_part01/ppoi_change_logs.py |
| core_metrics_import.py | src/azira_download_part01/municipality_core_metrics.py |
| Selected file_list.py and File_inventory.py helpers | src/azira_download_part01/common.py |
| sql_results.py | src/azira_download_part01/sql_results.py |
| CoreImportTests from tests/test_workflow.py | tests/test_imports.py |

## Implementation Status

- Standalone package, TOML configuration, CLI, four modules and offline tests:
  implemented.
- Direct dependency versions and Python 3.11: inspected; isolated installation
  completed.
- Installed CLI outside the repositories: checked.
- Synthetic fixture coverage: implemented; not production data certification.
- Actual S3-to-SQL masterfile identity mapping: pending. The read-only verifier
  is available; masterfile and `all` remain blocked with default configuration.
- Read-only follow-up: SQL inventory keys contain month directories; local
   directories are date-only. Separate configurable S3-to-inventory and
   S3-to-local offsets are implemented, including local mapping collision checks.
   The example proposes inventory offset 5 and local offset 13; actual S3 identity
   remains unverified. No external AWS profile/credential environment variables
   are configured. The current local vintage 252 filename has no SQL inventory
   row, so a full three-way mapping check is still blocked.
- Further read-only checks using temporarily authorized legacy credentials
   confirmed the current S3 key and local compressed size (109,238,258 bytes).
   The newest older inventory candidate returned S3 404 under the proposed
   prefix. All 12 named SQL prerequisites are visible. The operator explicitly
   chose to keep database checks read-only; inventory reconciliation and imports
   remain unapproved and unexecuted. See VALIDATION.md for the evidence record.
- Deployed SQL result parity, representative data-volume testing and live
  capability smoke tests: pending approved environments/access.
- Production cutover, scheduled jobs and remote repository creation: not done.

## Retained Risks

Partner discovery uses one S3 listing response and a 360-day filter. Geographic
and PPOI routines process only the first discovered row. PPOI appends can
duplicate on rerun. Municipality staging can suppress a retry after a failed
post-import procedure. Staging operations are not atomic with procedure calls.
SPM date is directory-derived. SPM/geofence procedures lack the core-metrics
return/deferred-error checks. Some downloader errors remain printed/swallowed.
Fixing these limitations is a separate, explicitly authorized change.