import json
from pathlib import Path, PureWindowsPath
import re
import tempfile

import pandas as pd

from . import common
from .s3_bucket_synchronization import save_state


def append_to_log_tables(frame, config):
    engine = common.sql_engine(config)
    try:
        frame.to_sql("PPOI_Change_Log", engine, if_exists="append", index=False)
        grouped = frame.groupby(["Category Name", "Country", "vintage_id"])["EditType"].value_counts().reset_index(name="count")
        summary = grouped.pivot_table(
            index=["Category Name", "Country", "vintage_id"], columns="EditType", values="count", fill_value=0,
        ).reset_index()
        summary.to_sql("PPOI_Change_Log_Summary", engine, if_exists="append", index=False)
    finally:
        engine.dispose()


def import_ppoi_logs(vintage_id, config):
    folder = Path(config.signals_root) / config.ppoi_prefix
    frames = []
    for edit_type in ("updated", "added", "closed", "removed"):
        filename = f"{edit_type}_ppoi_{vintage_id}.csv"
        path = folder / filename
        if path.exists():
            frame = pd.read_csv(path)
            if edit_type != "updated":
                frame["Area Diff(sqft)"] = pd.NA
            frame["EditType"] = edit_type
            frame["vintage_id"] = vintage_id
            frame["FileName"] = filename
        else:
            frame = pd.DataFrame()
        frames.append(frame)
    combined = pd.concat(frames, ignore_index=True)
    combined.rename(columns={"Polygon ID": "PolygonID", "Polygon Name": "PolygonName"}, inplace=True)
    append_to_log_tables(combined, config)
    return combined


def discover_ppoi(config):
    client = common.aws_session(config).client("s3")
    records = []
    pages = client.get_paginator("list_objects_v2").paginate(Bucket=config.partner_bucket, Prefix=config.ppoi_prefix)
    for page in pages:
        eligible = common.retrieve_s3_inventory(page, "*.zip")
        keys = set(eligible["key"])
        records.extend(item for item in page.get("Contents", []) if item["Key"] in keys)
    return client, records


def run(config):
    root = Path(config.signals_root).resolve()
    state_path = root / ".azira_ppoi_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if not isinstance(state, dict):
        raise ValueError("PPOI local metadata must be a JSON object")
    client, records = discover_ppoi(config)
    destinations = []
    seen = set()
    for item in records:
        key = item["Key"]
        relative = PureWindowsPath(key)
        target = root.joinpath(*relative.parts).resolve()
        if not key.startswith(config.ppoi_prefix) or relative.drive or relative.root or ".." in relative.parts or "\\" in key or any(":" in part or part.endswith((".", " ")) or PureWindowsPath(part).is_reserved() for part in relative.parts) or not target.is_relative_to(root):
            raise ValueError(f"Unsafe PPOI S3 key: {key}")
        identity = str(target).casefold()
        if identity in seen:
            raise ValueError(f"PPOI local path collision: {key}")
        seen.add(identity)
        destinations.append((item, target))
    pending = []
    for item, target in destinations:
        key = item["Key"]
        metadata = {
            "bucket": config.partner_bucket,
            "size": item["Size"],
            "etag": item["ETag"],
            "last_modified": item["LastModified"].isoformat(),
        }
        previous = state.get(key, {})
        unchanged = False
        if target.is_file() and isinstance(previous, dict):
            stat = target.stat()
            unchanged = all(previous.get(name) == value for name, value in metadata.items()) and stat.st_size == item["Size"] and previous.get("local_mtime_ns") == stat.st_mtime_ns
        if not unchanged:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="azira-ppoi-", dir=target.parent) as folder:
                temporary = Path(folder) / "download.part"
                client.download_file(config.partner_bucket, key, str(temporary))
                remote = client.head_object(Bucket=config.partner_bucket, Key=key)
                if temporary.stat().st_size != item["Size"] or remote["ContentLength"] != item["Size"] or remote["ETag"] != item["ETag"] or remote["LastModified"] != item["LastModified"]:
                    raise ValueError(f"PPOI changed during download or size differs: {key}")
                if target.exists():
                    common.rename_file_with_file_date(target)
                temporary.replace(target)
            metadata["local_mtime_ns"] = target.stat().st_mtime_ns
            metadata["processed"] = False
            state[key] = metadata
            save_state(state_path, state)
        if state[key].get("processed") is not True:
            pending.append((key, target))
    if not pending:
        print("no new PPOI log files today")
        return
    key, filename = pending[0]
    common.extract_zip_to_folder(filename, Path(config.signals_root) / config.ppoi_prefix)
    match = re.search(r"(.{3})\.zip$", str(filename))
    if not match:
        raise ValueError(f"Unrecognized PPOI filename: {filename.name}")
    import_ppoi_logs(match.group(1), config)
    state[key]["processed"] = True
    save_state(state_path, state)
    common.send_mail(config, "Azira PPOI data", "new data PPOI change log was downloaded")