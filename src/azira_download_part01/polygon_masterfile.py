import gzip
from pathlib import Path, PureWindowsPath
import re
import shutil

from botocore.exceptions import ClientError, NoCredentialsError
import pandas as pd

from . import common


MASTERFILE_PATTERN = re.compile(r"^Signals_Polygon_Masterfile_\d{8}_\d{3}\.tsv\.gz$", re.IGNORECASE)


def is_masterfile(key):
    return MASTERFILE_PATTERN.fullmatch(key.rsplit("/", 1)[-1]) is not None


def local_file(config, inventory_key):
    path = PureWindowsPath(inventory_key)
    if path.is_absolute() or path.drive or path.root or ".." in path.parts:
        raise ValueError(f"Inventory key is not a safe relative path: {inventory_key}")
    strip_chars = config.masterfile_strip_chars - config.masterfile_inventory_strip_chars
    if strip_chars < 0 or strip_chars >= len(inventory_key):
        raise ValueError("Invalid masterfile local/inventory strip relationship")
    path = PureWindowsPath(inventory_key[strip_chars:])
    if path.is_absolute() or path.drive or path.root or ".." in path.parts:
        raise ValueError(f"Mapped local key is not a safe relative path: {inventory_key}")
    return Path(config.masterfile_root).joinpath(*path.parts)


def mapped_key(config, s3_key):
    if config.masterfile_inventory_strip_chars < 0 or config.masterfile_inventory_strip_chars >= len(s3_key):
        raise ValueError("Invalid masterfile_inventory_strip_chars")
    key = s3_key[config.masterfile_inventory_strip_chars:]
    local_file(config, key)
    return key


def verify_mapping(config, s3_key, inventory_key):
    if not s3_key.startswith(config.masterfile_prefix) or not is_masterfile(s3_key):
        raise ValueError("S3 key is not a full masterfile in the configured prefix")
    if mapped_key(config, s3_key) != inventory_key:
        raise ValueError("S3 key mapping does not match the supplied SQL inventory key")
    client = common.aws_session(config, signals=True).client("s3")
    metadata = client.head_object(Bucket=config.signals_bucket, Key=s3_key)
    connection = common.sql_connection(config, inventory=True)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT name_key, size FROM dbo.Azira_S3_files_inventory WHERE name_key = ? AND latest = 1",
                inventory_key,
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()
    finally:
        connection.close()
    if len(rows) != 1 or rows[0][0] != inventory_key or rows[0][1] != metadata["ContentLength"]:
        raise ValueError("Latest SQL inventory row is missing, ambiguous, or differs from S3 size")
    path = local_file(config, inventory_key)
    if not path.is_file() or path.stat().st_size != metadata["ContentLength"]:
        raise ValueError("Local masterfile is missing or differs from S3 size")
    print(f"Verified mapping: {s3_key} -> {inventory_key} -> {path}")
    print("After confirming the rule applies to all masterfile keys, set masterfile_mapping_verified = true in your config.")


def check_spm_inventory_from_sql(config):
    connection = common.sql_connection(config, inventory=True)
    try:
        frame = pd.read_sql(
            "WITH xxx AS (SELECT TOP 1 *, SUBSTRING(name_key, LEN(name_key) - 9, 3) AS vintage_id "
            "FROM dbo.Azira_S3_files_inventory WHERE name_key LIKE '%polygon_masterfile%' "
            "AND name_key NOT LIKE '%diff%' AND latest = 1 AND ISNULL(isProcessed, 0) = 0 "
            "ORDER BY SUBSTRING(name_key, LEN(name_key) - 9, 3) DESC) SELECT name_key FROM xxx "
            "WHERE CAST(vintage_id AS int) > (SELECT MAX(CAST(vintage_id AS int)) FROM spm)", connection,
        )
    finally:
        connection.close()
    if len(frame) != 1:
        return None
    key = frame.at[0, "name_key"]
    common.comment(config, f"finished proc: new spm, {key}", "check_SPM_inventory_from_sql")
    return key


def update_spm_in_sql(fname, config):
    common.comment(config, "starting", "Update_SPM_in_sql")
    filename = local_file(config, fname)
    match = re.search(r"^(.*?)signals", fname, re.IGNORECASE)
    if not match:
        raise ValueError(f"Unrecognized masterfile key: {fname}")
    spm_folder = match.group(1)
    vintage_id = fname[-10:-7]
    vintage_date = spm_folder[-11:-1]
    extracted = filename.with_suffix("")
    with gzip.open(filename, "rb") as source, extracted.open("wb") as target:
        shutil.copyfileobj(source, target)
    frame = pd.read_csv(extracted, sep="\t", dtype=common.SPM_IMPORT_SCHEMA, usecols=common.SPM_IMPORT_SCHEMA.keys())
    frame = frame.rename(columns={
        "Polygon ID": "PolygonID", "Polygon Name": "PolygonName", "Polygon Friendly Name": "PolygonFriendlyName",
        "Postal 1": "Postal1", "Postal 2": "Postal2", "Sub-Sector": "SubSector", "Census ID": "CensusID",
        "ChainXY ID": "ChainXY_ID", "Google PlaceID": "Google_PlaceID",
    })
    frame["dma_id"] = ""
    frame["dma_name"] = ""
    frame["excludeReason"] = ""
    frame["vintage"] = vintage_date
    frame["vintage_id"] = vintage_id
    frame["OpenStatus"] = "open"
    engine = common.sql_engine(config)
    try:
        frame.to_sql("signals_polygon_masterfile_import", engine, if_exists="replace", index=False)
    finally:
        engine.dispose()
    try:
        common.execute_call(config, f"{config.database}.dbo.usp_Signals_polygon_masterfile_import", autocommit=True)
    except (TypeError, ValueError) as error:
        common.comment(config, f"problem with usp_Signals_polygon_masterfile_import in python: {error}", "usp_Signals_polygon_masterfile_import")
        common.send_mail(config, "new Signals_polygon_masterfile problem", "new Signals_polygon_masterfile data process did not complete")
        return
    common.update_processed(config, fname)
    common.comment(config, "finished", "Update_SPM_in_sql")
    common.send_mail(
        config, f"new Signals_polygon_masterfile successful. Vintage : {vintage_id}",
        "new Signals_polygon_masterfile data process did complete successfully.  It is ready for QC.",
    )


def acquire_masterfiles(config):
    if not config.masterfile_mapping_verified:
        raise RuntimeError("Masterfile mapping is unverified. Run verify-masterfile-mapping before enabling masterfile_mapping_verified.")
    client = common.aws_session(config, signals=True).client("s3")
    records = []
    seen = set()
    seen_paths = set()
    try:
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=config.signals_bucket, Prefix=config.masterfile_prefix)
        for page in pages:
            for item in page.get("Contents", []):
                s3_key = item["Key"]
                if not s3_key.startswith(config.masterfile_prefix) or not is_masterfile(s3_key):
                    continue
                key = mapped_key(config, s3_key)
                path = local_file(config, key)
                if key in seen or path in seen_paths:
                    raise ValueError(f"Masterfile key mapping collision: {key}")
                seen.add(key)
                seen_paths.add(path)
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        client.download_file(config.signals_bucket, s3_key, str(path))
                    except ClientError as error:
                        print(f"Error downloading {s3_key}: {error}")
                        continue
                records.append({"key": key, "last_modified": item["LastModified"], "size": item["Size"]})
    except (ClientError, NoCredentialsError) as error:
        print(f"Error accessing S3: {error}")
        return False
    if records:
        remote = pd.DataFrame(records)
        local = common.read_local_inventory_from_sql(config)
        selected = common.identify_files_to_download(remote, local)
        if not selected.empty:
            common.append_to_local_inventory_sql(config, selected)
    return True


def run(config):
    if not acquire_masterfiles(config):
        return
    fname = check_spm_inventory_from_sql(config)
    if fname is None:
        print("no new masterfile to import")
        return
    update_spm_in_sql(fname, config)