<div align="center">

# 🎮 FiveM Server Scraper

**Scrape, filter, and export the entire FiveM server list in seconds.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://python.org)

</div>

---

## ✨ Features

- 🔍 **Smart Filtering** — Filter by player count, tags, framework, locale, Discord, server name
- 🏗️ **Framework Detection** — Automatically detects ESX, QBCore, QBox, VRP, ND from resource lists
- 📊 **CSV Export** — UTF-8-BOM encoded for perfect Excel/Google Sheets compatibility
- 📋 **Google Sheets** — Auto-export results directly to a Google Sheet
- ⚡ **Fast** — Parallel framework scanning with configurable thread count
- 🛠️ **Configurable** — All options in a single `config.yaml` file

## 📦 Installation

```bash
# Clone the repository
git clone https://github.com/janto279/fivem-server-scraper.git
cd fivem-server-scraper

# Install dependencies
pip install -r requirements.txt

# Create your config
cp config.example.yaml config.yaml
```

## 🚀 Usage

```bash
# Run with default config
python scraper.py

# Run with a custom config file
python scraper.py -c my_config.yaml
```

### Example Output

```
============================================================
  FiveM Server Scraper
  2025-01-15 14:30:00
============================================================
[✓] Config loaded: config.yaml

[*] Filters:
    Players: 15+
    Tags: roleplay

[1/4] Downloading server list...
      18.2 MB received.

[2/4] Parsing server records...
      35189 server records found.

[3/4] Filtering...
      956 servers passed filters.

[3.5] Deep framework scan (612 servers, 20 threads)...
      344/612 frameworks detected.

[*] Framework Distribution:
    Unknown: 268
    ESX: 180
    VRP: 81
    QBCORE: 49
    QBOX: 33

[4/4] Exporting results...
[✓] 956 servers → 'leads.csv' (UTF-8-BOM)
```

## ⚙️ Configuration

Edit `config.yaml` to customize filtering and output:

### Filters

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `min_players` | int | `15` | Minimum active players |
| `max_players` | int | `0` | Maximum active players (0 = no limit) |
| `tags` | list | `["roleplay"]` | Required tags (AND logic) |
| `exclude_tags` | list | `[]` | Excluded tags |
| `frameworks` | list | `[]` | Filter by framework: `ESX`, `QBCORE`, `QBOX`, `VRP`, `ND` |
| `locales` | list | `[]` | Filter by locale: `en-US`, `tr`, `de`, etc. |
| `require_discord` | bool | `false` | Only include servers with a Discord link |
| `name_contains` | string | `""` | Server name must contain this text |
| `name_excludes` | list | `[]` | Exclude servers with these words in name |

### Framework Scan

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | bool | `true` | Deep scan each server's resources |
| `max_workers` | int | `20` | Parallel thread count |
| `timeout` | int | `3` | Per-server connection timeout (seconds) |

### Output

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `csv.enabled` | bool | `true` | Export to CSV file |
| `csv.filename` | string | `"leads.csv"` | CSV output filename |
| `google_sheets.enabled` | bool | `false` | Export to Google Sheets |

### Configuration Examples

**Only ESX servers with 50+ players:**
```yaml
filters:
  min_players: 50
  frameworks: ["ESX"]
```

**Turkish roleplay servers with Discord:**
```yaml
filters:
  tags: ["roleplay"]
  locales: ["tr"]
  require_discord: true
```

**Exclude "test" and "dev" servers:**
```yaml
filters:
  name_excludes: ["test", "dev", "template"]
```

## 📋 Google Sheets Setup

To export results directly to Google Sheets:

### 1. Create a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select an existing one)
3. Enable the **Google Sheets API** and **Google Drive API**:
   - Go to **APIs & Services** → **Library**
   - Search for "Google Sheets API" → **Enable**
   - Search for "Google Drive API" → **Enable**

### 2. Create a Service Account

1. Go to **APIs & Services** → **Credentials**
2. Click **Create Credentials** → **Service Account**
3. Name it (e.g., `fivem-scraper`) → **Create**
4. Skip optional steps → **Done**
5. Click on the service account → **Keys** tab
6. **Add Key** → **Create new key** → **JSON** → **Create**
7. Save the downloaded file as `credentials.json` in the project directory

### 3. Configure

Edit `config.yaml`:

```yaml
output:
  google_sheets:
    enabled: true
    credentials_file: "credentials.json"
    spreadsheet_name: "FiveM Leads"
    worksheet_name: "Servers"
    share_with: "your-email@gmail.com"  # optional
```

> **Note:** The `share_with` option automatically shares the spreadsheet with the specified email address when created. If you skip this, you can manually share the sheet with your Google account from the Service Account's email.

### 4. Run

```bash
python scraper.py
```

The script will create (or update) the spreadsheet and print the URL.

## 🏗️ How It Works

1. **Downloads** the full FiveM server list (~18 MB binary stream) from the Cfx.re master server
2. **Parses** the custom Protobuf-like binary format to extract server records
3. **Filters** servers based on your `config.yaml` criteria
4. **Scans** individual servers (via `info.json`) to detect frameworks (parallel, configurable threads)
5. **Exports** results to CSV and/or Google Sheets

### Framework Detection

The scraper identifies frameworks by checking the server's resource list:

| Resource | Framework |
|----------|-----------|
| `qbx_core` | **QBOX** |
| `qb-core` | **QBCORE** |
| `es_extended` | **ESX** |
| `vrp` | **VRP** |
| `nd_core` | **ND** |

## 📝 Output Format

The CSV/Sheet contains these columns:

| Column | Description |
|--------|-------------|
| Server Name | Cleaned server name (color codes removed) |
| Players | Current active player count |
| Discord | Discord invite link (if available) |
| Framework | Detected framework (ESX/QBCORE/QBOX/VRP/ND/Unknown) |
| Locale | Server locale (e.g., en-US, tr) |

## 🤝 Contributing

Contributions are welcome! Feel free to:

- Open issues for bugs or feature requests
- Submit pull requests
- Improve documentation

## 📜 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

## ⚠️ Disclaimer

This tool is for educational and research purposes. Please respect the [Cfx.re Terms of Service](https://fivem.net/terms) when using this scraper. Do not abuse the API with excessive requests.
