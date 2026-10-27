# Reviewer demo on Render

Deploy this prototype as one Python web service in **Oregon (US West)**. The UI is already built in `frontend/dist` and served by FastAPI. Keep one instance: the execution engine and SQLite store do not support multiple active server processes.

| Render field | Value |
| --- | --- |
| Source | Public repository `https://github.com/zhonghe0615/iac-mission-orchestrator` |
| Name | `iac-mission-orchestrator`, or another available name |
| Language | Python 3 |
| Branch | `main` |
| Region | Oregon (US West) |
| Root Directory | Leave blank |
| Build Command | `pip install -r backend/requirements.txt` |
| Start Command | `cd backend && python -m app --host 0.0.0.0 --port $PORT` |
| Instance | The smallest paid instance (currently $7/month) |
| Health Check Path | `/health` |

Set these environment variables in Render's dashboard. Never commit their secret values:

| Variable | Value |
| --- | --- |
| `DATABASE_PATH` | `/var/data/orchestrator.db` |
| `DEMO_PASSWORD` | A unique strong password; share it with reviewers separately |
| `LLM_API_KEY` | Optional OpenAI API key for live LLM planning |
| `LLM_MODEL` | `gpt-6-luna` if using that key |
| `LLM_REASONING_EFFORT` | Optional, e.g. `medium` |

Under **Advanced → Disk**, attach a 1 GB persistent disk at `/var/data`. Both the SQLite database and the simulated tank state then survive redeploys. Render's free instance cannot use a disk. The committed `.python-version` selects Python 3.12, which was used for local verification.

When `DEMO_PASSWORD` is set, the browser prompts for username `reviewer` and that password before serving the UI or API. `/health` remains public for Render's health check. This is a narrow demo access gate, not production identity or role-based access control. Without an LLM key, reviewers can still run the labelled deterministic fixture scenario. Set an API project budget before enabling live planning for external reviewers.

After deployment, check `/health`, open the protected homepage, run the fixture scenario, and confirm that a mission survives a service restart. If using a live key, run one LLM plan and verify the returned plan source and validation. Public Git repository deployments may require manual redeploy after later commits; verify the deployed commit in Render.
