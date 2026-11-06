import argparse
import ast
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import sys
import tempfile

import boto3

from .config import load_config


NAME = "Azira_S3_Bucket Synchronization"


def s3_client(config):
    credentials_path = Path(config.sync_credentials_file) if config.sync_credentials_file else Path(__file__).with_name("AWS_Credentials.py")
    if not credentials_path.is_file():
        if config.sync_credentials_file:
            raise FileNotFoundError("Configured synchronization credential file does not exist")
        return boto3.Session(profile_name=config.signals_profile or None, region_name=config.signals_region).client("s3")
    tree = ast.parse(credentials_path.read_text(encoding="utf-8-sig"))
    names = {"signals_aws_access_key_id", "signals_aws_secret_access_key"}
    credentials = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    credentials[target.id] = ast.literal_eval(statement.value)
    if any(not isinstance(credentials.get(name), str) or not credentials[name] for name in names):
        raise ValueError("Credential file must define nonempty signals AWS credential strings")
    return boto3.Session(
        aws_access_key_id=credentials["signals_aws_access_key_id"],
        aws_secret_access_key=credentials["signals_aws_secret_access_key"],
        region_name=config.signals_region,
    ).client("s3")


def destination(root, prefix, key):
    if not key.startswith(prefix):
        raise ValueError("S3 key is outside the synchronization prefix")
    relative = key[len(prefix):]
    parts = relative.split("/")
    if len(parts) < 3 or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"Expected month/week/file S3 layout: {key}")
    try:
        week = date.fromisoformat(parts[1])
    except ValueError:
        raise ValueError(f"Invalid week directory in S3 key: {key}") from None
    if parts[1] != week.isoformat() or parts[0] != week.strftime("%Y-%m"):
        raise ValueError(f"Month/week directories do not match: {key}")
    local_key = PurePosixPath(*parts[1:]).as_posix()
    windows = PureWindowsPath(local_key)
    if windows.drive or windows.root or "\\" in local_key or any(":" in part or part.endswith((".", " ")) or PureWindowsPath(part).is_reserved() for part in parts[1:]):
        raise ValueError(f"Unsafe S3 synchronization key: {key}")
    target = root.joinpath(*parts[1:]).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"S3 synchronization key escapes destination: {key}")
    return local_key, target


def save_state(path, state):
    with tempfile.TemporaryDirectory(prefix="azira-sync-state-", dir=path.parent) as folder:
        temporary = Path(folder) / "state.json"
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)


def synchronize(config, *, client=None, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=6)
    root = Path(config.sync_root).resolve()
    if not config.sync_prefix or not config.sync_prefix.endswith("/"):
        raise ValueError("sync_prefix must be nonempty and end with /")
    state_path = root / ".azira_s3_sync.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if not isinstance(state, dict):
        raise ValueError("Synchronization metadata must be a JSON object")
    client = client if client is not None else s3_client(config)
    selected = []
    seen = set()
    pages = client.get_paginator("list_objects_v2").paginate(Bucket=config.sync_bucket, Prefix=config.sync_prefix)
    for page in pages:
        for item in page.get("Contents", []):
            if item["Key"].endswith("/") or not cutoff <= item["LastModified"] <= now:
                continue
            local_key, target = destination(root, config.sync_prefix, item["Key"])
            identity = local_key.casefold()
            if identity in seen:
                raise ValueError(f"Multiple S3 keys map to the same local file: {local_key}")
            seen.add(identity)
            selected.append((item, local_key, target))
    downloaded = 0
    skipped = 0
    for item, local_key, target in selected:
        metadata = {
            "bucket": config.sync_bucket,
            "key": item["Key"],
            "size": item["Size"],
            "etag": item["ETag"],
            "last_modified": item["LastModified"].isoformat(),
        }
        previous = state.get(local_key, {})
        if target.is_file() and isinstance(previous, dict):
            stat = target.stat()
            if all(previous.get(name) == value for name, value in metadata.items()) and stat.st_size == item["Size"] and previous.get("local_mtime_ns") == stat.st_mtime_ns:
                skipped += 1
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="azira-sync-", dir=target.parent) as folder:
            temporary = Path(folder) / "download.part"
            client.download_file(config.sync_bucket, item["Key"], str(temporary))
            remote = client.head_object(Bucket=config.sync_bucket, Key=item["Key"])
            if temporary.stat().st_size != item["Size"] or remote["ContentLength"] != item["Size"] or remote["ETag"] != item["ETag"] or remote["LastModified"] != item["LastModified"]:
                raise ValueError(f"S3 object changed during synchronization or download size differs: {item['Key']}")
            modified = item["LastModified"].timestamp()
            os.utime(temporary, (modified, modified))
            temporary.replace(target)
        metadata["local_mtime_ns"] = target.stat().st_mtime_ns
        state[local_key] = metadata
        save_state(state_path, state)
        downloaded += 1
        print(f"Synchronized s3://{config.sync_bucket}/{item['Key']} -> {target}", flush=True)
    print(f"{NAME}: {downloaded} downloaded, {skipped} unchanged; last 6 weeks only", flush=True)
    return downloaded, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(prog="azira-s3-sync", description=NAME)
    parser.add_argument("--config", help="Non-secret TOML configuration file")
    args = parser.parse_args(argv)
    try:
        synchronize(load_config(args.config))
        return 0
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())