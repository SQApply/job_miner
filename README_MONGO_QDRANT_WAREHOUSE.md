# Job Miner MongoDB Warehouse + Qdrant Pipeline

Copy this bundle into the root of your `job_miner` repo.

## Add dependencies

Append `requirements_warehouse_additions.txt` to your existing `requirements.txt`, then run:

```powershell
pip install -r requirements.txt
```

## Start infra

```powershell
docker compose -f docker-compose.infra.yml up -d
ollama pull embeddinggemma
```

## Run ETL and matching

```powershell
python -m src.warehouse.cli health --root .
python -m src.warehouse.cli init-indexes --root .
python -m src.warehouse.cli backfill-jobs --root . --jobs-dir data\processed
python -m src.warehouse.cli backfill-resumes --root . --resumes-dir data\resumes\processed
python -m src.warehouse.cli build-job-tower --root .
python -m src.warehouse.cli build-candidate-tower --root .
python -m src.matching.cli index-jobs-from-mongo --root . --recreate
python -m src.matching.cli index-candidates-from-mongo --root . --recreate
python -m src.matching.cli match-candidates-from-mongo --root . --top-n 10
python -m src.warehouse.cli stats --root .
```

## Output

MongoDB is the source of truth. Qdrant is the vector index. Debug/demo match exports are written to:

```text
data/processed/matching/candidate_job_matches_latest.json
data/processed/matching/candidate_job_matches_latest.jsonl
data/processed/matching/candidate_job_matches_latest_summary.json
```
