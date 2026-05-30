# Job Miner MVP with Keycloak, FastAPI, Celery, PostgreSQL, MongoDB, Qdrant, and React

This MVP keeps the existing MongoDB/Qdrant/Ollama pipeline intact and adds a product layer:

- Candidate Portal: profile, recommendations, save/select/apply-click, feedback.
- Admin Portal: warehouse stats, full pipeline trigger, task/run tracking.
- Keycloak: users, roles, future tenant/group management.
- PostgreSQL: control-plane tables for users, pipeline runs, tasks, candidate actions, API logs.
- Redis + Celery: background pipeline execution.

## Run infrastructure

```cmd
cd /d C:\Users\abhin\Downloads\job_miner
docker compose -f docker-compose.mvp.yml up -d
```

Open Keycloak:

```text
http://localhost:8080
Admin user: admin
Admin password: admin
Realm: job-miner
```

Seeded application users:

```text
Admin: admin@jobminer.local / Admin@123
Candidate: candidate@jobminer.local / Candidate@123
```

## Install dependencies

```cmd
.venv\Scripts\activate.bat
python -m pip install -r requirements.txt
```

## Environment variables

```cmd
set JOB_MINER_POSTGRES_URL=postgresql+psycopg://job_miner_app:job_miner_password@localhost:5432/job_miner_control
set JOB_MINER_REDIS_URL=redis://localhost:6379/0
set JOB_MINER_MONGO_URI=mongodb://job_miner:job_miner@localhost:27017/?authSource=admin
set JOB_MINER_MONGO_DB=job_miner
set JOB_MINER_QDRANT_URL=http://localhost:6333
set JOB_MINER_OLLAMA_URL=http://localhost:11434
set JOB_MINER_KEYCLOAK_PUBLIC_BASE_URL=http://localhost:8080
set JOB_MINER_KEYCLOAK_REALM=job-miner
set JOB_MINER_KEYCLOAK_CLIENT_ID=job-miner-web
set JOB_MINER_KEYCLOAK_ENABLED=true
```

## Start backend

```cmd
uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

## Start Celery worker

```cmd
celery -A src.infrastructure.celery_app worker --pool=solo --loglevel=INFO -Q maintenance_queue,etl_queue,embedding_queue,matching_queue,llm_queue
```

## Start frontend

```cmd
cd frontend_mvp_keycloak
npm install
npm run dev
```

Open:

```text
http://localhost:5173
```

## Candidate setup

The seeded candidate user must be linked to a MongoDB candidate record one time.

Find a candidate_id:

```cmd
docker exec -it job_miner_mongo mongosh -u job_miner -p job_miner --authenticationDatabase admin job_miner
```

```javascript
db.candidate_tower_records.find({}, {candidate_id:1, resume_id:1, full_name:1}).limit(5).pretty()
```

Login as candidate and paste the candidate_id into the Candidate Portal link form.

## Admin demo

1. Login as `admin@jobminer.local`.
2. Open Admin Portal.
3. Click Run Full Pipeline.
4. Watch task status.
5. Switch to Candidate Portal or login as candidate.
6. View recommendations.
7. Save/select/apply-click jobs.
8. Add feedback.

## Data ownership

- MongoDB owns jobs, resumes, towers, matches, feedback.
- PostgreSQL owns users, tenant references, pipeline/task control, candidate actions, API logs.
- Qdrant owns vectors.
- Redis owns queue messages.
- Keycloak owns authentication and user lifecycle.
