import argparse
import importlib
import sys

from .config import load_config


CAPABILITIES = {
    "polygon-masterfile": "polygon_masterfile",
    "geographic-polygons": "geographic_polygons",
    "ppoi-change-logs": "ppoi_change_logs",
    "municipality-core-metrics": "municipality_core_metrics",
}


def run_capability(name, config):
    module = importlib.import_module(f".{CAPABILITIES[name]}", __package__)
    return module.run(config)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="azira-part01")
    parser.add_argument("--config", help="Non-secret TOML configuration file")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (*CAPABILITIES, "all"):
        command = commands.add_parser(name)
        command.add_argument("--config", default=argparse.SUPPRESS)
    download = commands.add_parser("download", help="Download S3 files only; no SQL or email")
    download.add_argument("--config", default=argparse.SUPPRESS)
    download.add_argument("--source", required=True, choices=("polygon-masterfile", "geographic-polygons", "ppoi-change-logs", "all"))
    sync = commands.add_parser("azira-s3-bucket-synchronization", help="Azira_S3_Bucket Synchronization: recent HDv2 files, week-only folders")
    sync.add_argument("--config", default=argparse.SUPPRESS)
    verify = commands.add_parser("verify-masterfile-mapping")
    verify.add_argument("--config", default=argparse.SUPPRESS)
    verify.add_argument("--s3-key", required=True)
    verify.add_argument("--inventory-key", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "azira-s3-bucket-synchronization":
            from .s3_bucket_synchronization import synchronize
            synchronize(config)
        elif args.command == "download":
            from .common import download_s3_files
            sources = ("polygon-masterfile", "geographic-polygons", "ppoi-change-logs") if args.source == "all" else (args.source,)
            for source in sources:
                download_s3_files(config, source)
        elif args.command == "verify-masterfile-mapping":
            from .polygon_masterfile import verify_mapping
            verify_mapping(config, args.s3_key, args.inventory_key)
        else:
            names = CAPABILITIES if args.command == "all" else [args.command]
            for name in names:
                print(f"Running {name}", flush=True)
                run_capability(name, config)
        return 0
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        return 1