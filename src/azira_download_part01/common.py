from datetime import datetime, timedelta
import fnmatch
import os
from pathlib import Path
import tempfile
import zipfile

import boto3
import pandas as pd
import pyodbc
import pytz
import sqlalchemy
from sqlalchemy.engine import URL


SPM_IMPORT_SCHEMA = dict.fromkeys([
    "Polygon ID", "Polygon Name", "Polygon Friendly Name", "Address", "City",
    "Province", "Country", "Postal 1", "Postal 2", "Lat", "Long", "Category",
    "Sub-Sector", "Sector", "Census ID", "Municipality", "Admin", "ChainXY ID",
    "PlaceKey", "Google PlaceID", "brand_id", "cat1_id", "cat2_id",
], "string")


def sql_connection(config, *, inventory=False, core=False, database=None, autocommit=False):
    server = config.core_sql_server if core else config.inventory_server if inventory else config.sql_server
    driver = config.bulk_driver if core else config.driver
    connection = pyodbc.connect(
        f"DRIVER={{{driver}}};SERVER={server};"
        f"DATABASE={database or config.database};Trusted_Connection=yes;",
        **({"timeout": 10} if core else {}),
    )
    connection.autocommit = autocommit
    if core:
        connection.timeout = 0
    return connection


def sql_engine(config, *, core=False):
    server = config.core_sql_server if core else config.sql_server
    odbc = (
        f"DRIVER={{{config.bulk_driver}}};SERVER={server};"
        f"DATABASE={config.database};Trusted_Connection=yes;"
    )
    return sqlalchemy.create_engine(
        URL.create("mssql+pyodbc", query={"odbc_connect": odbc}),
        **({"fast_executemany": True} if core else {}),
    )


def execute_call(config, procedure, params=(), *, autocommit=False):
    connection = sql_connection(config, autocommit=autocommit)
    try:
        cursor = connection.cursor()
        try:
            placeholders = ", ".join("?" for value in params)
            statement = f"{{CALL {procedure} ({placeholders})}}" if params else f"{{CALL {procedure} }}"
            if params:
                cursor.execute(statement, params)
            else:
                cursor.execute(statement)
            connection.commit()
        finally:
            cursor.close()
    finally:
        connection.close()


def comment(config, note, project):
    execute_call(config, "dbo.usp_comment", (note, project or "Python processing S3 data"))


def send_mail(config, subject, body):
    execute_call(config, "dbo.usp_send_near_email", (
        config.recipients, subject or "Python processing S3 data", body or "email send from python",
    ))


def read_local_inventory_from_sql(config):
    connection = sql_connection(config, inventory=True)
    try:
        return pd.read_sql(
            "SELECT name_key as [key], * FROM dbo.Azira_S3_files_inventory WHERE latest = 1",
            connection,
        )
    finally:
        connection.close()


def update_processed(config, key_name):
    connection = sql_connection(config)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "UPDATE dbo.Azira_S3_files_inventory SET isProcessed = 1, processedDate = GETDATE() "
                "WHERE name_key = ? AND ISNULL(isProcessed, 0) = 0", key_name,
            )
            connection.commit()
        finally:
            cursor.close()
    finally:
        connection.close()


def identify_files_to_download(remote, local):
    if local is None:
        return remote
    remote = remote.copy()
    remote["last_modified"] = remote["last_modified"].astype(str).str.slice(0, 19)
    merged = pd.merge(remote, local, on="key", how="left", indicator=True)
    unmatched = merged[merged["_merge"] == "left_only"].drop(columns=["_merge"]).copy()
    unmatched["version"] = 1
    difference = (
        ((merged["last_modified_x"].astype(str) != merged["last_modified_y"]) & merged["last_modified_y"].notnull())
        | ((merged["size_x"] != merged["size_y"]) & merged["size_y"].notnull())
    )
    changed = merged[difference].copy()
    changed["version"] = 2
    selected = pd.concat([unmatched, changed], ignore_index=True)
    selected = selected.drop(columns=["_merge", "last_modified_y", "size_y"])
    selected.columns = [column.replace("_x", "") for column in selected.columns]
    return selected


def append_to_local_inventory_sql(config, frame):
    engine = sql_engine(config)
    try:
        frame = frame.drop(columns=["name_key", "latest"])
        frame = frame.rename(columns={"key": "name_key", "version": "latest"}).copy()
        frame["last_modified"] = frame["last_modified"].astype(str).str.slice(0, 19)
        frame = frame[["name_key", "last_modified", "size", "latest"]]
        frame["download_date"] = datetime.now()
        frame.to_sql("Azira_S3_files_inventory", engine, if_exists="append", index=False)
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text(
                "UPDATE dbo.Azira_S3_files_inventory SET latest = 0 WHERE name_key IN "
                "(SELECT name_key FROM dbo.Azira_S3_files_inventory WHERE latest = 2) AND latest = 1"
            ))
            connection.execute(sqlalchemy.text("UPDATE dbo.Azira_S3_files_inventory SET latest = 1 WHERE latest = 2"))
            connection.commit()
    finally:
        engine.dispose()


def retrieve_s3_inventory(response, pattern):
    now = pytz.timezone("America/New_York").localize(datetime.now())
    records = [
        {"key": item["Key"], "last_modified": item["LastModified"], "size": item["Size"]}
        for item in response.get("Contents", [])
        if fnmatch.fnmatch(item["Key"], pattern)
        and now - timedelta(days=360) <= item["LastModified"] <= now
    ]
    return pd.DataFrame(records, columns=["key", "last_modified", "size"])


def aws_session(config, *, signals=False):
    profile = config.signals_profile if signals else config.partner_profile
    return boto3.Session(
        profile_name=profile or None,
        **({"region_name": config.signals_region} if signals else {}),
    )


def download_s3_files(config, source):
    if source == "polygon-masterfile":
        from .polygon_masterfile import is_masterfile
        bucket, prefix, root = config.signals_bucket, config.masterfile_prefix, config.masterfile_root
        matches = is_masterfile
    elif source == "geographic-polygons":
        bucket, prefix, root = config.partner_bucket, config.geojson_prefix, config.signals_root
        matches = lambda key: fnmatch.fnmatch(key, f"{prefix}*.json")
    elif source == "ppoi-change-logs":
        bucket, prefix, root = config.partner_bucket, config.ppoi_prefix, config.signals_root
        matches = lambda key: fnmatch.fnmatch(key, f"{prefix}*.zip")
    else:
        raise ValueError(f"Unsupported S3 download source: {source}")
    root = Path(root).resolve()
    client = aws_session(config, signals=source == "polygon-masterfile").client("s3")
    downloaded = 0
    skipped = 0
    pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
    for page in pages:
        for item in page.get("Contents", []):
            key = item["Key"]
            if not key.startswith(prefix) or not matches(key):
                continue
            relative = Path(key)
            target = (root / relative).resolve()
            if relative.is_absolute() or relative.drive or ".." in relative.parts or not target.is_relative_to(root):
                raise ValueError(f"Unsafe S3 download key: {key}")
            if target.is_file() and target.stat().st_size == item["Size"]:
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="azira-download-", dir=target.parent) as folder:
                temporary = Path(folder) / "download.part"
                client.download_file(bucket, key, str(temporary))
                if temporary.stat().st_size != item["Size"]:
                    raise ValueError(f"Downloaded size differs from S3 listing: {key}")
                temporary.replace(target)
            downloaded += 1
            print(f"Downloaded s3://{bucket}/{key} -> {target}", flush=True)
    print(f"{source}: {downloaded} downloaded, {skipped} existing files skipped", flush=True)


def discover_partner(config, prefix, pattern):
    session = aws_session(config)
    response = session.client("s3").list_objects_v2(Bucket=config.partner_bucket, Prefix=prefix)
    remote = retrieve_s3_inventory(response, pattern)
    local = read_local_inventory_from_sql(config)
    selected = identify_files_to_download(remote, local)
    return session.resource("s3").Bucket(config.partner_bucket), selected


def rename_file_with_file_date(path):
    path = Path(path)
    modified = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d_%H-%M-%S")
    renamed = path.with_name(f"old_{path.stem}_{modified}{path.suffix}_bak")
    os.rename(path, renamed)


def download_new_bucket_files(bucket, root, selected, category):
    root = Path(root)
    log_dir = root / "files_download_log"
    log_dir.mkdir(parents=True, exist_ok=True)
    selected.to_csv(log_dir / f"files_to_download_{category}_{datetime.now():%Y%m%d_%H%M%S}.csv")
    for index, row in selected.iterrows():
        target = root / row["key"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            rename_file_with_file_date(target)
        bucket.download_file(row["key"], str(target))


def extract_zip_to_folder(zip_path, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (folder / member.filename).resolve()
            if not target.is_relative_to(folder.resolve()):
                raise ValueError(f"Unsafe archive member: {member.filename}")
            if target.is_file():
                target.unlink()
            archive.extract(member, folder)