# POS System

Flask POS system backed by Google Sheets.

## Required Environment Variables

- `POS_APP_PASSWORD`: Login password for the POS site.
- `POS_SECRET_KEY`: Flask session secret.
- `GOOGLE_CREDENTIALS_JSON`: Full Google service account JSON content.
- `SPREADSHEET_ID`: Google Sheet ID.

## Local Run

```powershell
python -m pip install -r requirements.txt
$env:POS_APP_PASSWORD="your-password"
$env:POS_SECRET_KEY="change-this"
python serve.py
```

Open `http://127.0.0.1:5000/login`.

## Cloud Deploy

This app needs a Python web service host such as Render or Railway. It cannot run on GitHub Pages because GitHub Pages only serves static files.

On the cloud host, set the required environment variables above. Do not upload `credentials.json` to GitHub.
