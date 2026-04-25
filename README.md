# IG Tool License Server

License management server for the Instagram Automation Tool. Deploys to **Vercel** with **Upstash Redis** for storage.

---

## Quick Setup (5 minutes)

### 1. Create Upstash Redis Database

1. Go to [upstash.com](https://upstash.com) and create a free account
2. Click **Create Database** → pick a region close to your users
3. Copy these two values from the database detail page:
   - **UPSTASH_REDIS_REST_URL** (e.g., `https://xyz.upstash.io`)
   - **UPSTASH_REDIS_REST_TOKEN** (long string)

### 2. Deploy to Vercel

**Option A — Git (recommended):**
```bash
# Push this license-server folder to a GitHub repo, then:
# Go to vercel.com → New Project → Import that repo
```

**Option B — Vercel CLI:**
```bash
npm i -g vercel
cd license-server
vercel              # Follow the prompts
vercel --prod       # Deploy to production
```

### 3. Set Environment Variables

In the Vercel dashboard → your project → **Settings → Environment Variables**, add:

| Variable | Value |
|---|---|
| `UPSTASH_REDIS_REST_URL` | Your Upstash REST URL |
| `UPSTASH_REDIS_REST_TOKEN` | Your Upstash REST Token |
| `ADMIN_PASSWORD` | A strong password for the admin dashboard |

Then click **Redeploy** from the Deployments page.

### 4. Access Admin Dashboard

Open `https://your-project.vercel.app` in your browser. Log in with your `ADMIN_PASSWORD`.

### 5. Update the Desktop App

In `newautomationfix.py`, update the `LICENSE_SERVER_URL` in the `LicenseManager` class:
```python
LICENSE_SERVER_URL = "https://your-project.vercel.app"
```

---

## API Endpoints

### App Endpoints (called by the desktop app)

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/validate` | Validate license key + HWID |
| `POST` | `/api/activate` | Activate license on a machine |
| `POST` | `/api/trial` | Create a free trial (tied to HWID) |
| `GET` | `/api/health` | Health check |

### Admin Endpoints (called by dashboard, require `X-Admin-Password` header)

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/admin/generate` | Generate a new license key |
| `GET` | `/api/admin/keys` | List all license keys |
| `GET` | `/api/admin/stats` | Dashboard statistics |
| `POST` | `/api/admin/revoke` | Revoke a license |
| `POST` | `/api/admin/extend` | Extend a license |
| `POST` | `/api/admin/delete` | Permanently delete a license |
| `POST` | `/api/admin/deactivate` | Remove a machine from a license |

---

## Subscription Tiers

| Tier | Price | Features | Max Machines | Max Profiles |
|---|---|---|---|---|
| **Trial** | Free (14 days) | Home Feed Warmup | 1 | 1 |
| **Basic** | $29/mo | Home Feed + DM Outreach | 1 | 1 |
| **Pro** | $49/mo | All Features | 3 | 3 |
| **Agency** | $99/mo | All Features + Unlimited Profiles | 10 | Unlimited |

---

## Stripe Integration (Optional)

To automate payments + key generation:

1. Set up Stripe subscription plans matching the tiers above
2. Create a Stripe webhook endpoint (add to `api/index.py`)
3. On `checkout.session.completed` → auto-generate a license key and email it
4. On `customer.subscription.deleted` → auto-revoke the license key

---

## Security Notes

- Change `ADMIN_PASSWORD` to a strong, unique password
- The admin dashboard uses the password in HTTP headers (fine over HTTPS on Vercel)
- License keys are stored in Redis with no personal data
- HWID fingerprinting uses CPU ID + MAC + volume serial (no PII)
- Rate limiting is handled by Vercel's built-in edge middleware
