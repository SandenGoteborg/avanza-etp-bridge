# Avanza Agent Bridge

A small read-only bridge that fetches the most traded Swedish ETPs from
Avanza's public website endpoints and publishes a structured JSON snapshot.

It is designed to complement a scheduled ChatGPT morning brief:

1. GitHub Actions fetches Avanza data before 07:00.
2. The workflow writes `data/latest.json`.
3. A scheduled ChatGPT task opens the raw JSON URL and combines the snapshot
   with issuer websites, news, regulation and retail signals.

## Important limitations

- The Avanza endpoints are undocumented and may change without warning.
- The bridge uses public market data only and does not log in to Avanza.
- Avanza turnover is a directional activity signal, not complete Swedish
  market share.
- A product appearing in the top list is not necessarily newly issued.
- Check Avanza's current terms and permitted-use rules before relying on this
  workflow operationally.

## Deploy in GitHub

1. Create a new **public** GitHub repository, for example
   `avanza-etp-bridge`.
2. Upload all files from this folder, preserving the directory structure.
3. Open the repository's **Actions** tab.
4. Enable workflows if GitHub asks you to do so.
5. Open **Fetch Avanza ETP snapshot** and select **Run workflow**.
6. Confirm that `data/latest.json` appears after the run.

No password or API key is needed for this first version.

## JSON address for ChatGPT

After deployment, the raw URL will be:

```text
https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/YOUR_REPOSITORY/main/data/latest.json
```

Replace the two placeholders.

The scheduled ChatGPT task can then be instructed to:

```text
Open the following Avanza snapshot and use it as the structured activity input:
https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/YOUR_REPOSITORY/main/data/latest.json

Verify fetched_at_utc and coverage before using the data. If the file is stale,
missing or incomplete, state that clearly. Treat turnover as an Avanza activity
signal, not total Swedish market share.
```

## Run locally

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
python src/fetch_avanza.py
```

## Schedule

The GitHub workflow runs at 03:30 UTC on weekdays. This is before 07:00 local
Swedish time in both winter and summer. GitHub does not guarantee an exact
start time, which is why the workflow includes a buffer.

## Data included

The bridge requests the top 200:

- warrants, turbos and Mini Futures;
- certificates, including Bull & Bear and trackers, filtered to:
  Vontobel, Société Générale, Nordea, Morgan Stanley, J.P. Morgan,
  Handelsbanken and BNP Paribas.

The JSON contains raw normalized rows plus summary aggregations by issuer,
underlying and direction.
