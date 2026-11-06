from pathlib import Path
import re

import pandas as pd
import sqlalchemy
from sqlalchemy.types import NVARCHAR

from . import common
from .sql_results import drain_results


EXCEL_FILE_PATTERN = re.compile(r"^core_metrics_HDV2_external_(\d{6})_NorthAmerica\.xlsx$", re.IGNORECASE)
WORKSHEETS = ["DataPoints", "MAUs", "DAUs"]
ID_COL_COUNT = 6
SKIP_LABELS = ["Dec2025", "Nov2025", "Oct2025"]
SQL_TABLE = "core_metrics_municipality_import"


def resolve_latest_excel_file(config):
    candidates = []
    for path in Path(config.excel_dir).glob("core_metrics_HDV2_external_*_NorthAmerica.xlsx"):
        match = EXCEL_FILE_PATTERN.match(path.name)
        if match:
            candidates.append((int(match.group(1)), path, match.group(1)))
    if not candidates:
        raise FileNotFoundError(
            f"No matching Excel files found in {config.excel_dir}. Expected names like "
            "core_metrics_HDV2_external_YYYYMM_NorthAmerica.xlsx"
        )
    latest_vintage, latest_path, vintage = max(candidates, key=lambda item: item[0])
    return latest_path.name, str(latest_path), vintage


def run(config):
    print("WORKFLOW_PHASE: Resolve latest workbook", flush=True)
    filename, excel_path, vintage = resolve_latest_excel_file(config)
    start_msg = f"core_metrics_municipality import starting | file: {filename} | vintage: {vintage}"
    print(start_msg, flush=True)
    common.send_mail(config, "core_metrics_municipality import: starting", start_msg)
    common.comment(config, start_msg, "core_metrics_municipality import")
    engine = common.sql_engine(config, core=True)
    try:
        with engine.connect() as connection:
            table_exists = connection.execute(sqlalchemy.text(
                f"SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = '{SQL_TABLE}'"
            )).scalar()
            existing = connection.execute(sqlalchemy.text(
                f"SELECT COUNT(*) FROM {SQL_TABLE} WHERE vintage = :v"
            ), {"v": vintage}).scalar() or 0 if table_exists else 0
        if existing > 0:
            skip_msg = (
                f"core_metrics_municipality import skipped | vintage {vintage} already present in "
                f"[{SQL_TABLE}] ({existing:,} rows). No new file to process."
            )
            print(skip_msg, flush=True)
            common.send_mail(config, "core_metrics_municipality import: skipped (no new file)", skip_msg)
            common.comment(config, skip_msg, "core_metrics_municipality import")
            print("WORKFLOW_SKIPPED", flush=True)
            return
        frames = []
        for sheet in WORKSHEETS:
            print(f"WORKFLOW_PHASE: Read and transform {sheet}", flush=True)
            frame = pd.read_excel(excel_path, sheet_name=sheet)
            id_cols = list(frame.columns[:ID_COL_COUNT])
            value_cols = [
                column for column in frame.columns[ID_COL_COUNT:]
                if not any(label in str(column) for label in SKIP_LABELS)
            ]
            melted = frame[id_cols + value_cols].melt(
                id_vars=id_cols, value_vars=value_cols, var_name="metric_column", value_name="value",
            )
            melted["source_sheet"] = sheet
            melted["vintage"] = vintage
            frames.append(melted)
        combined = pd.concat(frames, ignore_index=True)
        print("WORKFLOW_PHASE: Replace import table and load rows", flush=True)
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text(
                f"IF OBJECT_ID('{SQL_TABLE}', 'U') IS NOT NULL DROP TABLE {SQL_TABLE}"
            ))
            connection.commit()
        dtype_map = {column: NVARCHAR(length=500) for column in combined.select_dtypes(include="object").columns}
        combined.to_sql(SQL_TABLE, engine, schema="dbo", if_exists="append", index=False, dtype=dtype_map)
        with engine.connect() as connection:
            row_count = connection.execute(sqlalchemy.text(f"SELECT COUNT(*) FROM {SQL_TABLE}")).scalar()
        if row_count != len(combined):
            raise RuntimeError(f"Import verification failed: expected {len(combined):,} rows, found {row_count}")
        procedure = f"{config.core_procedure_database}.dbo.usp_core_metrics_municipality_import"
        print(f"WORKFLOW_PHASE: Execute {procedure}", flush=True)
        common.comment(config, f"Executing stored procedure: {procedure}", "core_metrics_municipality import")
        connection = common.sql_connection(config, core=True, database=config.core_procedure_database, autocommit=True)
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    f"DECLARE @rc int; EXEC @rc = {procedure}; "
                    "IF @rc <> 0 THROW 51000, 'Core metrics procedure returned nonzero status', 1;"
                )
                drain_results(cursor)
                connection.commit()
            finally:
                cursor.close()
        finally:
            connection.close()
        common.comment(config, f"Stored procedure {procedure} completed successfully.", "core_metrics_municipality import")
        done_msg = (
            f"core_metrics_municipality import complete | file: {filename} | vintage: {vintage} | "
            f"rows imported: {row_count:,} | table: [{config.database}].[dbo].[{SQL_TABLE}] | "
            f"stored procedure: {procedure} executed"
        )
        print(done_msg, flush=True)
        common.send_mail(config, "core_metrics_municipality import: complete (new file processed)", done_msg)
        common.comment(config, done_msg, "core_metrics_municipality import")
    finally:
        engine.dispose()