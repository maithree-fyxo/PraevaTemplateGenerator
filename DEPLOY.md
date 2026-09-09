# Deploying the Praeva Template Generator

GitHub stores the code; a hosting service **runs** it. (GitHub Pages can't — it
only serves static files, and this is a Python backend.) Below is the quickest
free path, plus alternatives.

---

## Step 1 — Put the code on GitHub

The project is already a git repository with an initial commit. Create an empty
repo on GitHub (no README/gitignore), then from the project folder:

```bash
git remote add origin https://github.com/<you>/praeva-template-generator.git
git branch -M main
git push -u origin main
```

`.env` and `.secrets.json` are gitignored, so no secrets are pushed.

---

## Step 2 — Deploy it (pick one)

### Option A — Render (recommended, free, deploys from GitHub)

1. Sign in at <https://render.com> with GitHub.
2. **New + → Blueprint**, choose your repo. Render reads `render.yaml`.
   (Or **New + → Web Service** and set Build `pip install -r requirements.txt`,
   Start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.)
3. Add your Ezekia key as an environment variable **in the dashboard**:
   `EZEKIA_TOKEN = <your key>` and `EZEKIA_USE_MOCK = 0`.
   (On hosted platforms the filesystem resets on each deploy, so set the token
   as an env var rather than via the in-app Settings — env is secure and
   persistent. The in-app Settings screen is still handy for local use.)
4. Deploy. You get a URL like `https://praeva-template-generator.onrender.com`.

> Free Render services sleep after inactivity and take ~30s to wake on the first
> request — fine for testing.

### Option B — Railway

<https://railway.app> → New Project → Deploy from GitHub. It auto-detects the
`Procfile`. Add `EZEKIA_TOKEN` / `EZEKIA_USE_MOCK=0` under Variables.

### Option C — Any Docker host (Fly.io, Cloud Run, etc.)

A `Dockerfile` is included:

```bash
docker build -t praeva-gen .
docker run -p 8000:8000 -e EZEKIA_TOKEN=<key> -e EZEKIA_USE_MOCK=0 praeva-gen
```

### Option D — Just run it locally (no hosting needed to test)

```bash
pip install -r requirements.txt
python run.py            # http://127.0.0.1:8000
```

---

## Important: this app has no login

Anyone who has the URL can use it — and, in demo mode, generate the sample deck;
with a token configured, generate from your live Ezekia data. For testing that's
usually fine (the Render URL is obscure), but **do not treat the hosted URL as
private**. Before wider use, add authentication (e.g. a login/reverse proxy) or
keep the deployment access-restricted. Keep the Ezekia token in the host's
environment variables, never in the repo.
