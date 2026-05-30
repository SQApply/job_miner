from __future__ import annotations

import re
from typing import Any, Iterable

from .serializers import as_str_list

# Broad parser labels/categories. These should not be stored as candidate skills.
NON_SKILL_LABELS = {
    "programming language",
    "programming languages",
    "framework",
    "frameworks",
    "framework and tools",
    "frameworks and tools",
    "tools",
    "tools and platforms",
    "generative ai technologies",
    "gen ai technologies",
    "vector databases",
    "nosql and sql databases",
    "sql and nosql databases",
    "deployment platform",
    "deployment platforms",
    "ci/cd",
    "cicd",
    "ai/ml techniques",
    "machine learning techniques",
    "databases",
    "cloud platforms",
    "cloud platform",
    "data engineering tools",
    "analytics tools",
    "development tools",
}

# Phrases containing these fragments are usually category labels, not concrete skills.
NON_SKILL_FRAGMENTS = (
    " category",
    " categories",
    " technologies",
    " techniques",
    " platform",
    " platforms",
    " language",
    " languages",
    " framework",
    " frameworks",
    " tools",
    " databases",
)

# Common abbreviations/aliases to keep display consistent.
SKILL_ALIASES = {
    "py spark": "PySpark",
    "pyspark": "PySpark",
    "spark": "Spark",
    "apache spark": "Spark",
    "python": "Python",
    "java": "Java",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "sql": "SQL",
    "nosql": "NoSQL",
    "aws": "AWS",
    "azure": "Azure",
    "gcp": "GCP",
    "llm": "LLM",
    "rag": "RAG",
    "nlp": "NLP",
    "opencv": "OpenCV",
    "fastapi": "FastAPI",
    "mlflow": "MLflow",
    "pytorch": "PyTorch",
    "tensorflow": "TensorFlow",
    "scikit-learn": "scikit-learn",
    "sklearn": "scikit-learn",
    "xgboost": "XGBoost",
    "lightgbm": "LightGBM",
    "mongodb": "MongoDB",
    "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL",
    "qdrant": "Qdrant",
    "pinecone": "Pinecone",
    "faiss": "FAISS",
    "langchain": "LangChain",
    "langgraph": "LangGraph",
    "kubernetes": "Kubernetes",
    "docker": "Docker",
    "bentoml": "BentoML",
    "zenml": "ZenML",
    "airflow": "Airflow",
    "kafka": "Kafka",
    "databricks": "Databricks",
    "snowflake": "Snowflake",
}

# Conservative known skills used only to extract from free text. Explicit parsed list values are
# also accepted as long as they are not broad category labels.
KNOWN_SKILL_TERMS = {
    "python", "java", "javascript", "typescript", "sql", "nosql", "scala", "r",
    "spark", "apache spark", "pyspark", "databricks", "snowflake", "redshift",
    "aws", "azure", "gcp", "s3", "lambda", "glue", "emr",
    "airflow", "kafka", "hadoop", "hive", "dbt",
    "docker", "kubernetes", "terraform", "jenkins", "github actions", "ci/cd",
    "fastapi", "django", "flask", "react", "angular", "node.js", "spring boot",
    "pytorch", "tensorflow", "scikit-learn", "sklearn", "xgboost", "lightgbm",
    "mlflow", "zenml", "bentoml", "opencv", "nlp", "llm", "rag", "langchain", "langgraph",
    "mongodb", "postgresql", "postgres", "mysql", "oracle", "qdrant", "pinecone", "faiss",
    "power bi", "tableau", "excel", "jira", "confluence",
}


def _normalize_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower()).strip(" .,:;()[]{}")


def canonicalize_skill(value: Any) -> str | None:
    """Return a clean display skill or None when the value is a broad label/noise."""
    text = str(value or "").strip()
    if not text:
        return None

    text = re.sub(r"\s+", " ", text).strip(" -•\t\n\r")
    if not text:
        return None

    key = _normalize_key(text)
    if not key:
        return None

    if key in NON_SKILL_LABELS:
        return None

    # Category-like values such as "Generative AI Technologies" and
    # "Deployment Platform" should not be displayed as skills.
    if len(key.split()) <= 5 and any(fragment in f" {key}" for fragment in NON_SKILL_FRAGMENTS):
        if key not in KNOWN_SKILL_TERMS:
            return None

    if len(text) > 80:
        return None

    return SKILL_ALIASES.get(key, text)


def unique_skills(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        skill = canonicalize_skill(value)
        if not skill:
            continue
        key = _normalize_key(skill)
        if key in seen:
            continue
        seen.add(key)
        out.append(skill)
    return out


def extract_known_skills_from_text(text: str) -> list[str]:
    low = " " + re.sub(r"[^a-zA-Z0-9+#./-]+", " ", text.lower()) + " "
    found: list[str] = []
    for term in sorted(KNOWN_SKILL_TERMS, key=len, reverse=True):
        if re.search(r"(?<![a-zA-Z0-9+#.-])" + re.escape(term) + r"(?![a-zA-Z0-9+#.-])", low):
            found.append(term)
    return unique_skills(found)


def build_canonical_candidate_skills(profile: dict[str, Any]) -> list[str]:
    """Build the single candidate skills list used by tower, matching, and LLM reranking.

    Rules:
    - Keep one canonical `skills` list.
    - Exclude broad category labels from parser output.
    - Prefer explicit parsed fields, then add known skills discovered in summary/experience/projects.
    - Do not mix domains into skills. Domains remain a separate profile signal.
    """
    explicit_values: list[Any] = []
    for field in (
        "skills",
        "primary_skills",
        "secondary_skills",
        "programming_languages",
        "tools_and_platforms",
        "certifications",
    ):
        explicit_values.extend(as_str_list(profile.get(field)))

    skills = unique_skills(explicit_values)

    free_text_parts = [
        str(profile.get("summary") or ""),
        str(profile.get("headline") or ""),
        str(profile.get("current_title") or ""),
    ]
    for section in ("experience", "projects"):
        for item in profile.get(section) or []:
            if isinstance(item, dict):
                free_text_parts.append(" ".join(str(v) for v in item.values() if v is not None))
            else:
                free_text_parts.append(str(item))

    return unique_skills([*skills, *extract_known_skills_from_text(" ".join(free_text_parts))])
