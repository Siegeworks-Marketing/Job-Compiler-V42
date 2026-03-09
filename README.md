# Siegeworks Job Compiler

**AI-powered job search that finds live listings and ranks them against your resume.**

🔗 [siegeworks-marketing.github.io/Job-Compiler-V42](https://siegeworks-marketing.github.io/Job-Compiler-V42/)

---

## What It Does

1. You describe the role you're looking for
2. Claude searches the live web across LinkedIn, Builtin, Greenhouse, Workday, Lever, and more
3. Every listing is verified — expired and closed roles are filtered out before you see them
4. Your resume is scored against every result (0–100 match score)
5. You get a ranked list with trust badges, salary info, and a ready-to-use Action Plan

---

## What You Need

- An **Anthropic API key** from [console.anthropic.com](https://console.anthropic.com)

That's it.

> **Billing note:** Each search costs roughly $0.20–$0.50 in API credits. Add a small credit balance at console.anthropic.com → Billing before your first search.

---

## How to Use

### 1 — Enter Your Search

| Field | What to Enter |
|---|---|
| **Job Title** | The role you're targeting. Be specific — "Senior Program Manager" returns better results than "PM" |
| **Location** | City or metro area. Leave blank + check Remote for remote-only |
| **Remote** | On-site / Hybrid / Remote / Any |
| **Salary Min** | Optional. Filters out roles below your floor |
| **Seniority** | Match your actual level — reduces irrelevant results |

Paste your resume into the resume box, enter your API key, then click **Search**.

---

### 2 — Read Your Results

Each job card shows a **match score** (0–100) and a **trust badge** that tells you what we know about the listing:

| Badge | Meaning |
|---|---|
| ✓ VERIFIED OPEN (green) | Server confirmed the listing is live with an Apply button |
| ATS URL (blue) | ATS link detected — verify manually before applying |
| DIRECT LINK | Job board URL (LinkedIn, Indeed, Builtin) |
| UNVERIFIED | Included but treat with skepticism |

**Apply Now** only appears on verified listings. For others, use the LinkedIn/Indeed search buttons on the card.

---

### 3 — Build Your Action Plan

Check the box on any role you want to pursue. Switch to the **Action Plan** tab to get ready-to-use prompts for:

- Resume tailoring
- Cover letter
- LinkedIn outreach message
- Interview prep

Each prompt is pre-filled with the job title and description. Click to copy, then paste directly into Claude or ChatGPT.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Invalid API key" | Re-copy from console.anthropic.com — no leading spaces |
| "Rate limited" | Wait 60 seconds and try again |
| "Credit balance too low" | Add credits at console.anthropic.com → Billing |
| Search returns 0 results | Try a broader title or remove the salary filter |

---

## Privacy

- Your API key is stored only in your browser — never logged on any server
- Your resume is sent only to Anthropic for scoring — nowhere else
- No accounts, no data collection, no tracking

---

*Built by Siegeworks Marketing LLC*
