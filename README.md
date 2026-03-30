# AlphaScope

AlphaScope is a Flask-based multi-market insight terminal for:

- US stocks
- Indian stocks
- Crypto
- Forex

It includes live charts, AI-guided explanations, real market/news context, theme support, and a portfolio workspace.

## Local run

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

## Push to GitHub

If this folder is not already a Git repository:

```powershell
git init
git add .
git commit -m "Initial AlphaScope app"
```

Create a new empty GitHub repo, then connect and push:

```powershell
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

## Deploy on Render

This project includes a `render.yaml` and uses `gunicorn`.

### Option 1: From GitHub repo

1. Push the code to GitHub.
2. Go to Render.
3. Create a new Web Service from your GitHub repository.
4. Render should detect:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`

### Option 2: Use the included Render blueprint

1. Push this repo to GitHub.
2. In Render, choose `New +` -> `Blueprint`.
3. Select the repo.
4. Render will read `render.yaml`.

## Notes

- The SQLite database is fine for local use and light demos.
- For serious production use, switch from SQLite to Postgres.
- If Ollama is not running in the deployed environment, AlphaScope will use fallback logic for AI summaries.
