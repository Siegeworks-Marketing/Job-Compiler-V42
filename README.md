# Siegeworks Job Compiler

Real jobs. Searched live. Ranked by AI.

Built and distributed by [Siegeworks Marketing LLC](https://siegeworks.com).

---

## What This Tool Does

Searches LinkedIn, Indeed, BuiltIn, Greenhouse, Lever, and Workday in real time using Claude AI's web search capability. Optionally checks a pre-built catalog of fresh listings before running the live search, deduplicates the results, then scores and ranks every listing against your resume — all in your browser.

**Your data never leaves your browser.** Your resume is processed client-side only. Your Anthropic API key is stored in session memory only (cleared when you close the tab).

---

## Privacy & Security

- **Resume:** Never transmitted to any server. Processed locally in your browser only.
- **API key:** Stored in `sessionStorage` only. Cleared automatically when you close the tab. Never logged by the proxy.
- **Search params:** Role, location, and filter preferences are saved to `localStorage` for convenience. Non-sensitive.
- **Dismissed listings:** Saved to `localStorage` so previously closed listings don't reappear on future searches.
- **Proxy secret:** Visible in the distributed HTML file. This is intentional — it is a traffic filter, not a user-data secret. See the proxy notes below.

---

## Setup Guide

This guide assumes you are starting from scratch with no prior GitHub, Vercel, or Anthropic setup.

---

### Part 1 — GitHub Repository

**Step 1: Create a GitHub account**

Go to [github.com](https://github.com) and sign up for a free account if you don't have one.

**Step 2: Create a new repository**

1. Click the **+** icon in the top-right corner → **New repository**
2. Name it something like `job-compiler` or `siegeworks-job-compiler`
3. Set visibility to **Public** (required for free GitHub Pages and unlimited Actions minutes)
4. Check **Add a README file**
5. Click **Create repository**

**Step 3: Upload the project files**

You have two options:

*Option A — GitHub web interface (no Git required):*
1. In your new repo, click **Add file → Upload files**
2. Drag and drop all files from this project, preserving the folder structure:
   - `index.html` (root)
   - `vercel.json` (root)
   - `data/listings.json`
   - `scripts/fetch_jobs.py`
   - `.github/workflows/fetch-jobs.yml`
   - `api/proxy.js`
3. Click **Commit changes**

*Option B — Git command line:*
```bash
git clone https://github.com/YOUR-USERNAME/YOUR-REPO-NAME.git
cd YOUR-REPO-NAME
# Copy all project files here, then:
git add .
git commit -m "Initial commit"
git push
```

**Step 4: Enable GitHub Pages**

1. In your repo, go to **Settings → Pages**
2. Under **Source**, select **Deploy from a branch**
3. Select **main** branch, **/ (root)** folder
4. Click **Save**
5. Wait 1–2 minutes. Your tool will be live at:
   `https://YOUR-USERNAME.github.io/YOUR-REPO-NAME/`

---

### Part 2 — Vercel Proxy

The proxy lets users' API calls route through a server-side function, which is required for the web search tool to work reliably. Without it, the tool falls back to a direct browser connection (less reliable).

**Step 5: Create a Vercel account**

Go to [vercel.com](https://vercel.com) and sign up with your GitHub account.

**Step 6: Deploy the proxy**

1. From the Vercel dashboard, click **Add New → Project**
2. Click **Import** next to your GitHub repository
3. Vercel will detect the `api/proxy.js` function automatically
4. Before clicking Deploy, go to **Environment Variables** and add:
   - Key: `PROXY_SECRET`
   - Value: choose any strong password (e.g., `siegeworks2025abc`) — write this down
5. Click **Deploy**
6. After deployment completes, copy your Vercel URL (e.g., `https://your-project.vercel.app`)

**Step 7: Update the tool with your proxy URL**

Open `index.html` and find this section near the top:

```javascript
const BAKED_PROXY_URL    = "";
const BAKED_PROXY_SECRET = "";
const BAKED_CATALOG_URL  = "";
```

Fill it in:

```javascript
const BAKED_PROXY_URL    = "https://your-project.vercel.app/api/proxy";
const BAKED_PROXY_SECRET = "siegeworks2025abc";   // your PROXY_SECRET value
const BAKED_CATALOG_URL  = "https://YOUR-USERNAME.github.io/YOUR-REPO-NAME/data/listings.json";
```

Commit this change to GitHub. GitHub Pages will redeploy automatically within 1–2 minutes.

---

### Part 3 — GitHub Actions Catalog Pipeline

The catalog pipeline runs every 6 hours, fetches fresh job listings from free APIs, prunes listings older than 7 days, and commits the updated catalog to your repo.

**Step 8: Verify the workflow is enabled**

1. In your repo, go to the **Actions** tab
2. If prompted to enable workflows, click **I understand my workflows, go ahead and enable them**
3. You should see **Fetch Job Catalog** listed

**Step 9: Run the workflow manually the first time**

1. Click **Fetch Job Catalog**
2. Click **Run workflow → Run workflow**
3. Watch it run (takes ~30 seconds)
4. After it completes, check `data/listings.json` — it should now contain listings

**Step 10 (Optional): Add Adzuna for broader job coverage**

Adzuna is a free job API that adds US on-site/hybrid listings beyond the remote-focused free APIs. Register at [developer.adzuna.com](https://developer.adzuna.com) (free).

Once you have your App ID and App Key:

1. In your GitHub repo, go to **Settings → Secrets and variables → Actions**
2. Click **New repository secret** and add:
   - `ADZUNA_APP_ID` — your Adzuna App ID
   - `ADZUNA_APP_KEY` — your Adzuna App Key

The workflow will automatically use these on the next run.

---

### Part 4 — Anthropic API Key (for users)

Users need their own Anthropic API key to run searches. This keeps your costs at $0 — all API usage bills to each user's own account.

**Recommended: Tell users to set a spend limit before use.**

Direct users to: [console.anthropic.com](https://console.anthropic.com)
- Create an account (free)
- Generate an API key
- Set a monthly spend limit under **Billing → Spend limits** (e.g., $5–$10/month)

---

## Distributing the Tool

Once set up, your tool lives at:

```
https://YOUR-USERNAME.github.io/YOUR-REPO-NAME/
```

Share that URL. Anyone who visits it can use the tool immediately — no download, no install.

For advanced users who want to run the tool offline, they can download `index.html` and use the proxy URL hash method:

```
file:///path/to/index.html#proxy=https://your-project.vercel.app/api/proxy&secret=YOUR-SECRET
```

---

## Updating the Tool

To update the HTML tool after setup:

1. Edit `index.html` in your GitHub repo (via the web editor or Git)
2. Commit the change
3. GitHub Pages redeploys automatically within 1–2 minutes

To update the catalog fetch script:

1. Edit `scripts/fetch_jobs.py`
2. Commit and push
3. The next scheduled run will use the updated script

---

## Troubleshooting

**"Connection failed" or CORS error**
- Verify `BAKED_PROXY_URL` points to your Vercel deployment
- Check that the Vercel deployment is live at the URL you specified
- Ensure `PROXY_SECRET` in Vercel matches `BAKED_PROXY_SECRET` in the HTML

**"Invalid API key"**
- The key must start with `sk-ant-api03-`
- Keys are session-only — users must re-enter after closing the tab

**GitHub Actions workflow fails**
- Go to Actions tab → click the failed run → read the error log
- Most common cause: Python dependency issue (run `pip install httpx` locally to test)

**Catalog is empty after workflow runs**
- Check the Actions run log for API errors
- Remotive and Jobicy require no keys — if both fail, it may be a network issue on GitHub's runners (rare, retry)

**Tool loads but catalog shows 0 listings**
- Verify `BAKED_CATALOG_URL` is set to the correct GitHub Pages URL
- Check that `data/listings.json` exists and has been populated by the workflow
- Confirm GitHub Pages is enabled and the site is live

---

## File Structure

```
/
├── index.html                    # Main tool (served by GitHub Pages)
├── vercel.json                   # Vercel function config
├── README.md                     # This file
├── data/
│   └── listings.json             # Job catalog (updated by Actions every 6 hours)
├── scripts/
│   └── fetch_jobs.py             # Catalog fetch + prune script
├── .github/
│   └── workflows/
│       └── fetch-jobs.yml        # Cron schedule for catalog updates
└── api/
    └── proxy.js                  # Vercel API proxy
```

---

## License

Free to use and distribute. Built by Siegeworks Marketing LLC.
If this tool helped you land an interview, a coffee is appreciated: [venmo.com/u/Chris-Wendt-6](https://venmo.com/u/Chris-Wendt-6)
