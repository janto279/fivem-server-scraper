#!/usr/bin/env python3
"""
FiveM Server List Scraper
─────────────────────────
Scrapes the Cfx.re master server list, filters servers by
configurable criteria, detects frameworks, and exports results
to CSV and/or Google Sheets.

Usage:
    python scraper.py                  # uses config.yaml
    python scraper.py -c custom.yaml   # uses custom config
"""

import argparse
import csv
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
import yaml

# ═══════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════
STREAM_REDIR_URL = "https://servers-frontend.fivem.net/api/servers/streamRedir/"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://servers.fivem.net",
    "Referer": "https://servers.fivem.net/",
}

DEFAULT_CONFIG = {
    "filters": {
        "min_players": 15,
        "max_players": 0,
        "tags": ["roleplay"],
        "exclude_tags": [],
        "frameworks": [],
        "locales": [],
        "require_discord": False,
        "name_contains": "",
        "name_excludes": [],
    },
    "framework_scan": {
        "enabled": True,
        "max_workers": 20,
        "timeout": 3,
    },
    "output": {
        "csv": {"enabled": True, "filename": "leads.csv"},
        "google_sheets": {
            "enabled": False,
            "credentials_file": "credentials.json",
            "spreadsheet_name": "FiveM Leads",
            "worksheet_name": "Servers",
            "share_with": "",
        },
    },
}


# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════
def load_config(path: str = "config.yaml") -> dict:
    """Load YAML config, falling back to defaults for missing keys."""
    config = DEFAULT_CONFIG.copy()
    cfg_path = Path(path)
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        config = _deep_merge(config, user)
        print(f"[✓] Config loaded: {cfg_path}")
    else:
        print(f"[!] {cfg_path} not found — using defaults.")
    return config


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# ═══════════════════════════════════════════════════════════════
#  PROTOBUF HELPERS — FiveM Binary Stream Parser
# ═══════════════════════════════════════════════════════════════
def _read_varint(data: bytes, pos: int):
    """Read a protobuf varint from data at pos."""
    result = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            return None, pos
    return None, pos


def _parse_inner_fields(data: bytes) -> dict:
    """Parse protobuf fields inside a server record.
    Returns {field_num: [(wire_type, value), ...]}."""
    fields = {}
    pos = 0
    end = len(data)
    while pos < end:
        tag, pos = _read_varint(data, pos)
        if tag is None:
            break
        fn = tag >> 3
        wt = tag & 0x7
        if wt == 0:
            val, pos = _read_varint(data, pos)
            if val is None:
                break
            fields.setdefault(fn, []).append(("varint", val))
        elif wt == 2:
            length, pos = _read_varint(data, pos)
            if length is None or pos + length > end:
                break
            fields.setdefault(fn, []).append(("bytes", data[pos : pos + length]))
            pos += length
        elif wt == 5:
            if pos + 4 > end:
                break
            fields.setdefault(fn, []).append(
                ("fixed32", int.from_bytes(data[pos : pos + 4], "little"))
            )
            pos += 4
        elif wt == 1:
            if pos + 8 > end:
                break
            fields.setdefault(fn, []).append(
                ("fixed64", int.from_bytes(data[pos : pos + 8], "little"))
            )
            pos += 8
        else:
            break
    return fields


def _parse_var_submsg(data: bytes) -> tuple:
    """Parse a vars sub-message (field 12) into (key, value)."""
    key = ""
    val = ""
    pos = 0
    end = len(data)
    while pos < end:
        tag, pos = _read_varint(data, pos)
        if tag is None:
            break
        fn = tag >> 3
        wt = tag & 0x7
        if wt == 2:
            length, pos = _read_varint(data, pos)
            if length is None or pos + length > end:
                break
            text = data[pos : pos + length].decode("utf-8", errors="replace")
            pos += length
            if fn == 1:
                key = text
            elif fn == 2:
                val = text
        elif wt == 0:
            _, pos = _read_varint(data, pos)
        else:
            break
    return key, val


# ═══════════════════════════════════════════════════════════════
#  STREAM PARSER
# ═══════════════════════════════════════════════════════════════
def parse_server_stream(raw: bytes) -> list:
    """Parse the FiveM binary server stream.

    Stream format:
        [4-byte header] { [field 1: server_id] [field 2: server_blob] }*

    Each server record is preceded by a 4-byte separator.
    """
    servers = []
    pos = 0
    total = len(raw)
    errors = 0

    while pos < total - 4:
        pos += 4  # skip 4-byte header/separator

        if pos >= total:
            break

        # Field 1: server ID (tag=0x0a = field 1, length-delimited)
        tag, new_pos = _read_varint(raw, pos)
        if tag is None:
            break
        fn, wt = tag >> 3, tag & 0x7

        if fn != 1 or wt != 2:
            # Alignment recovery — scan for 0x0a
            found = False
            for scan in range(pos, min(pos + 20, total)):
                if raw[scan] == 0x0A:
                    t2, np2 = _read_varint(raw, scan)
                    if t2 and (t2 >> 3) == 1 and (t2 & 0x7) == 2:
                        pos, tag, new_pos = scan, t2, np2
                        fn, wt = 1, 2
                        found = True
                        break
            if not found:
                errors += 1
                if errors > 100:
                    break
                pos += 1
                continue

        id_len, new_pos = _read_varint(raw, new_pos)
        if id_len is None or new_pos + id_len > total:
            break
        server_id = raw[new_pos : new_pos + id_len].decode("utf-8", errors="replace")
        pos = new_pos + id_len

        # Field 2: server data blob
        tag, new_pos = _read_varint(raw, pos)
        if tag is None:
            break
        if (tag >> 3) != 2 or (tag & 0x7) != 2:
            errors += 1
            continue

        data_len, new_pos = _read_varint(raw, new_pos)
        if data_len is None or new_pos + data_len > total:
            break
        blob = raw[new_pos : new_pos + data_len]
        pos = new_pos + data_len

        servers.append((server_id, _parse_inner_fields(blob)))

    return servers


# ═══════════════════════════════════════════════════════════════
#  FRAMEWORK DETECTION
# ═══════════════════════════════════════════════════════════════
def detect_framework(resources: list) -> str:
    """Detect framework from a server's resource list."""
    if not resources:
        return "Unknown"
    res_lower = {r.lower() for r in resources if isinstance(r, str)}
    if "qbx_core" in res_lower:
        return "QBOX"
    if "qb-core" in res_lower:
        return "QBCORE"
    if "es_extended" in res_lower:
        return "ESX"
    if "vrp" in res_lower:
        return "VRP"
    if "nd_core" in res_lower:
        return "ND"
    for r in res_lower:
        if "qbx" in r:
            return "QBOX"
        if "qb-" in r:
            return "QBCORE"
        if "esx_" in r or "esx-" in r:
            return "ESX"
    return "Unknown"


def _fetch_server_resources(addr: str, timeout: int = 3) -> list:
    """Fetch resource list from a single server's info.json."""
    try:
        resp = requests.get(
            f"http://{addr}/info.json",
            headers={"User-Agent": BROWSER_HEADERS["User-Agent"]},
            timeout=timeout,
        )
        if resp.status_code == 200:
            return resp.json().get("resources", [])
    except Exception:
        pass
    return []


def _resolve_framework_worker(item: tuple, timeout: int = 3):
    """Thread worker — resolve framework for a single server."""
    server_id, _, fields = item
    f18 = fields.get(18, [])
    addr = ""
    for t, v in f18:
        if t == "bytes":
            addr = v.decode("utf-8", errors="replace")
            break
    if addr:
        resources = _fetch_server_resources(addr, timeout)
        if resources:
            fw = detect_framework(resources)
            if fw != "Unknown":
                return server_id, fw
    return server_id, None


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════
def _clean_name(name: str) -> str:
    """Remove FiveM color codes (^0-^9) and control chars."""
    if not name:
        return ""
    name = re.sub(r"\^[0-9]", "", name)
    name = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", name)
    return name.strip()


def _extract_discord(vars_dict: dict) -> str:
    """Extract Discord invite link from server vars."""
    discord = vars_dict.get("Discord", "") or vars_dict.get("discord", "")
    if discord:
        if "discord.gg/" in discord or "discord.com/" in discord:
            return discord if discord.startswith("http") else f"https://{discord}"
        if discord.startswith("http"):
            return discord
        return f"https://discord.gg/{discord}"

    for key in ("sv_projectDesc", "sv_projectName", "banner_detail"):
        text = vars_dict.get(key, "")
        m = re.search(
            r"(https?://(?:discord\.gg|discord\.com/invite)/[^\s<>\"')\]]+)", text
        )
        if m:
            return m.group(1)
        m = re.search(r"(discord\.gg/[^\s<>\"')\]]+)", text, re.IGNORECASE)
        if m:
            return f"https://{m.group(1)}"

    return ""


# ═══════════════════════════════════════════════════════════════
#  SERVER PROCESSING & FILTERING
# ═══════════════════════════════════════════════════════════════
def process_server(server_id: str, fields: dict, config: dict) -> dict | None:
    """Process a single server record and apply config filters.

    Returns a result dict or None if the server doesn't pass filters.
    """
    filters = config["filters"]

    # ── Player count ────────────────────────────────────────
    players = 0
    for t, v in fields.get(2, []):
        if t == "varint":
            players = v
            break

    if players < filters["min_players"]:
        return None
    if filters["max_players"] > 0 and players > filters["max_players"]:
        return None

    # ── Hostname ────────────────────────────────────────────
    hostname = ""
    for t, v in fields.get(4, []):
        if t == "bytes":
            hostname = v.decode("utf-8", errors="replace")
            break

    # ── Parse vars (field 12 = repeated sub-messages) ──────
    vars_dict = {}
    for t, v in fields.get(12, []):
        if t == "bytes":
            key, val = _parse_var_submsg(v)
            if key:
                vars_dict[key] = val

    # ── Display name ────────────────────────────────────────
    project_name = vars_dict.get("sv_projectName", "")
    display_name = _clean_name(project_name if project_name else hostname)

    # ── Tags filter ─────────────────────────────────────────
    tags_raw = vars_dict.get("tags", "")
    tags = [t.strip().lower() for t in tags_raw.split(",") if t.strip()]

    for required_tag in filters.get("tags", []):
        if required_tag.lower() not in tags:
            return None

    for excluded_tag in filters.get("exclude_tags", []):
        if excluded_tag.lower() in tags:
            return None

    # ── Locale filter ───────────────────────────────────────
    locale_filter = filters.get("locales", [])
    if locale_filter:
        server_locale = vars_dict.get("locale", "").strip().lower()
        if not any(loc.lower() in server_locale for loc in locale_filter):
            return None

    # ── Name filters ────────────────────────────────────────
    name_contains = filters.get("name_contains", "")
    if name_contains and name_contains.lower() not in display_name.lower():
        return None

    for exclude_word in filters.get("name_excludes", []):
        if exclude_word.lower() in display_name.lower():
            return None

    # ── Framework (from stream vars — may be "Unknown") ────
    resources_str = vars_dict.get("resources", "")
    resources = [r.strip() for r in resources_str.split(",") if r.strip()] if resources_str else []
    framework = detect_framework(resources)

    # ── Discord ─────────────────────────────────────────────
    discord = _extract_discord(vars_dict)

    if filters.get("require_discord", False) and not discord:
        return None

    # ── Locale for output ───────────────────────────────────
    locale = vars_dict.get("locale", "")

    return {
        "Server Name": display_name,
        "Players": players,
        "Discord": discord,
        "Framework": framework,
        "Locale": locale,
    }


# ═══════════════════════════════════════════════════════════════
#  OUTPUT — CSV
# ═══════════════════════════════════════════════════════════════
FIELD_NAMES = ["Server Name", "Players", "Discord", "Framework", "Locale"]


def export_csv(servers: list, filename: str = "leads.csv"):
    """Write server list to CSV with UTF-8 BOM (Excel compatible)."""
    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELD_NAMES)
        writer.writeheader()
        writer.writerows(servers)
    print(f"[✓] {len(servers)} servers → '{filename}' (UTF-8-BOM)")


# ═══════════════════════════════════════════════════════════════
#  OUTPUT — GOOGLE SHEETS
# ═══════════════════════════════════════════════════════════════
def export_google_sheets(servers: list, config: dict):
    """Write server list to a Google Sheet via Service Account."""
    gs_cfg = config["output"]["google_sheets"]
    creds_path = gs_cfg["credentials_file"]
    sheet_name = gs_cfg["spreadsheet_name"]
    ws_name = gs_cfg["worksheet_name"]
    share_with = gs_cfg.get("share_with", "")

    # Lazy imports — only needed when Sheets is enabled
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("[✗] Google Sheets requires: pip install gspread google-auth")
        print("    Install with: pip install -r requirements.txt")
        return

    if not Path(creds_path).exists():
        print(f"[✗] Credentials file not found: {creds_path}")
        print("    See README.md for setup instructions.")
        return

    print(f"[*] Connecting to Google Sheets...")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
    gc = gspread.authorize(creds)

    # Open or create spreadsheet
    try:
        spreadsheet = gc.open(sheet_name)
        print(f"    Opened existing spreadsheet: '{sheet_name}'")
    except gspread.SpreadsheetNotFound:
        spreadsheet = gc.create(sheet_name)
        print(f"    Created new spreadsheet: '{sheet_name}'")
        if share_with:
            spreadsheet.share(share_with, perm_type="user", role="writer")
            print(f"    Shared with: {share_with}")

    # Share if configured and not already shared
    if share_with:
        try:
            spreadsheet.share(share_with, perm_type="user", role="writer")
        except Exception:
            pass  # Already shared

    # Get or create worksheet
    try:
        worksheet = spreadsheet.worksheet(ws_name)
        worksheet.clear()
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=ws_name, rows=len(servers) + 1, cols=len(FIELD_NAMES))

    # Write header + data
    header = FIELD_NAMES
    rows = [header]
    for s in servers:
        rows.append([s.get(col, "") for col in FIELD_NAMES])

    worksheet.update(range_name="A1", values=rows)

    # Format header row (bold)
    try:
        worksheet.format("A1:E1", {"textFormat": {"bold": True}})
    except Exception:
        pass

    sheet_url = spreadsheet.url
    print(f"[✓] {len(servers)} servers → Google Sheets")
    print(f"    URL: {sheet_url}")


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="FiveM Server List Scraper")
    parser.add_argument(
        "-c", "--config", default="config.yaml", help="Path to config file (default: config.yaml)"
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  FiveM Server Scraper")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # ── Load config ─────────────────────────────────────────
    config = load_config(args.config)
    filters = config["filters"]
    fw_scan = config["framework_scan"]
    output = config["output"]

    print(f"\n[*] Filters:")
    print(f"    Players: {filters['min_players']}+", end="")
    if filters["max_players"] > 0:
        print(f" (max {filters['max_players']})", end="")
    print()
    print(f"    Tags: {', '.join(filters['tags']) if filters['tags'] else 'any'}")
    if filters["frameworks"]:
        print(f"    Frameworks: {', '.join(filters['frameworks'])}")
    if filters["locales"]:
        print(f"    Locales: {', '.join(filters['locales'])}")
    if filters["require_discord"]:
        print(f"    Discord: required")
    if filters["name_contains"]:
        print(f"    Name contains: '{filters['name_contains']}'")

    # ── 1) Fetch stream ────────────────────────────────────
    print(f"\n[1/4] Downloading server list...")
    try:
        resp = requests.get(
            STREAM_REDIR_URL,
            headers=BROWSER_HEADERS,
            timeout=120,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"[✗] Download failed: {e}")
        sys.exit(1)

    raw = resp.content
    print(f"      {len(raw) / 1024 / 1024:.1f} MB received.")

    # ── 2) Parse binary stream ─────────────────────────────
    print(f"\n[2/4] Parsing server records...")
    all_servers = parse_server_stream(raw)
    print(f"      {len(all_servers)} server records found.")

    # ── 3) Filter ──────────────────────────────────────────
    print(f"\n[3/4] Filtering...")
    results = []
    for server_id, fields in all_servers:
        processed = process_server(server_id, fields, config)
        if processed:
            results.append((server_id, processed, fields))

    print(f"      {len(results)} servers passed filters.")

    # ── 3.5) Deep framework scan ───────────────────────────
    unknowns = [(sid, sd, f) for sid, sd, f in results if sd["Framework"] == "Unknown"]
    if unknowns and fw_scan["enabled"]:
        workers = fw_scan["max_workers"]
        timeout = fw_scan["timeout"]
        print(f"\n[3.5] Deep framework scan ({len(unknowns)} servers, {workers} threads)...")
        resolved = 0
        result_map = {sid: sd for sid, sd, _ in results}

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_resolve_framework_worker, item, timeout): item[0]
                for item in unknowns
            }
            done_count = 0
            for future in as_completed(futures):
                done_count += 1
                if done_count % 50 == 0:
                    print(f"      Progress: {done_count}/{len(unknowns)} scanned, {resolved} resolved")
                try:
                    sid, fw = future.result()
                    if fw:
                        result_map[sid]["Framework"] = fw
                        resolved += 1
                except Exception:
                    pass

        print(f"      {resolved}/{len(unknowns)} frameworks detected.")

    # ── Apply framework filter (post-scan) ─────────────────
    framework_filter = [f.upper() for f in filters.get("frameworks", [])]
    if framework_filter:
        before = len(results)
        results = [
            (sid, sd, f)
            for sid, sd, f in results
            if sd["Framework"].upper() in framework_filter
        ]
        print(f"      Framework filter: {before} → {len(results)} servers")

    # ── Sort by player count (descending) ──────────────────
    results.sort(key=lambda x: x[1]["Players"], reverse=True)
    final = [sd for _, sd, _ in results]

    # ── Framework stats ────────────────────────────────────
    fw_counts = {}
    for s in final:
        fw = s["Framework"]
        fw_counts[fw] = fw_counts.get(fw, 0) + 1

    print(f"\n[*] Framework Distribution:")
    for fw, c in sorted(fw_counts.items(), key=lambda x: -x[1]):
        print(f"    {fw}: {c}")

    # ── 4) Export ──────────────────────────────────────────
    print(f"\n[4/4] Exporting results...")

    if output["csv"]["enabled"]:
        export_csv(final, output["csv"]["filename"])

    if output["google_sheets"]["enabled"]:
        export_google_sheets(final, config)

    if not output["csv"]["enabled"] and not output["google_sheets"]["enabled"]:
        print("[!] No output enabled. Enable csv or google_sheets in config.yaml")

    # ── Summary ────────────────────────────────────────────
    if final:
        print(f"\n{'─' * 60}")
        print(f"  TOP 10 SERVERS")
        print(f"{'─' * 60}")
        for i, s in enumerate(final[:10], 1):
            name = s["Server Name"][:42]
            print(
                f"  {i:2}. {name:<44} {s['Players']:>5} players  [{s['Framework']}]"
            )
    else:
        print("\n[!] No servers matched your filters. Try adjusting config.yaml.")


if __name__ == "__main__":
    main()
