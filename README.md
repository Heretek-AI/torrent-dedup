# torrent-dedup

Collect torrent infohashes from multiple qBittorrent WebUI instances into a
single CSV, with one column per host.

## Setup

1. Copy the example host list and fill in real credentials:

   ```bash
   cp qbittorrent-hosts.example.csv qbittorrent-hosts.csv
   ```

2. Install the dependency:

   ```bash
   pip install requests
   ```

3. Run:

   ```bash
   python dump_torrent_hashes.py
   ```

Output is written to `torrent-hashes.csv`: the header row lists the hosts, and
each column contains that host's sorted, de-duplicated infohashes. Hosts that
fail still get a column (empty) and are reported on the console.

## Options

| Option | Default | Description |
|---|---|---|
| `--hosts` | `qbittorrent-hosts.csv` | Credentials CSV (`host,username,password`) |
| `--out` | `torrent-hashes.csv` | Output CSV |
| `--only` | all hosts | Query only the given URLs or ports, e.g. `--only 8083` |
| `--workers` | `8` | Hosts queried in parallel |
| `--timeout` | `60` | Per-request timeout in seconds |
| `--page-size` | `5000` | Torrents fetched per API call |
| `--retries` | `2` | Retries for timeouts and 5xx responses |
| `--insecure` | off | Skip TLS verification for self-signed HTTPS hosts |

Exit codes: `0` all hosts succeeded, `1` some failed, `2` none succeeded.

Both `qbittorrent-hosts.csv` and `torrent-hashes.csv` are gitignored because
they contain credentials and local data.
