# Running Sentinel in Docker

```powershell
python models/migrate_model.py qwen2.5:3b   # once: copy the model into models/qwen2.5-3b/
docker compose up -d --build
```

Then open <http://localhost:8000> and sign in with `DASHBOARD_PASSWORD` from `.env`.

## Services

| Service | What it does |
| --- | --- |
| `ollama` | The AI server. Its models live in the `ollama_data` volume. |
| `model-init` | Runs once. It creates the base model from `models/<model-name>/Modelfile`, so no internet download is needed. If the folder is missing, it falls back to `ollama pull`. |
| `sentinel` | The FastAPI app on port 8000. Its data (SQLite) lives in the `sentinel_data` volume. `./models` is mounted so the self-improving model's files are written into the project. |

Settings come from `.env` (`env_file`). Compose overrides `OLLAMA_HOST`, `SENTINEL_DB_PATH` and `SENTINEL_MODELS_DIR` with container paths.

## Things to know

- **"Sentinel server container" is the container, not your laptop.** To diagnose the laptop itself, open **Add Agent** and run the Windows command on the laptop (as Administrator). The agent connects to `http://localhost:8000`, which the container port is mapped to.
- **Memory.** The `ollama` container loads the model inside Docker's VM. On a laptop with little free RAM, close heavy apps, or point the app at Ollama on the host instead. To do that, set `OLLAMA_HOST: http://host.docker.internal:11434` for the `sentinel` service and remove the `ollama` and `model-init` services.
- **Logs:** `docker compose logs -f sentinel`
- **Stop:** `docker compose down` (data is kept). `docker compose down -v` also deletes the volumes.
