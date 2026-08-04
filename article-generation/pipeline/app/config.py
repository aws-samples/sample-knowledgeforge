"""
YAML-driven configuration loader for the ECS article pipeline.

Loads config.yaml at startup, applies environment variable overrides,
validates required fields, and provides a get_clients() factory for
AWS service clients.

Env var naming convention: section keys joined by underscore, uppercased.
Example: bedrock.llm_model_id -> BEDROCK_LLM_MODEL_ID
"""

import logging
import os
import sys
from dataclasses import dataclass, field, fields, asdict
from typing import Dict

import boto3
import yaml
from botocore.config import Config

logger = logging.getLogger(__name__)


# ── Nested dataclasses ──────────────────────────────────────────────


@dataclass
class AwsConfig:
    region: str = "eu-west-1"


@dataclass
class S3Config:
    input_bucket: str = ""
    output_bucket: str = ""


@dataclass
class SqsConfig:
    queue_url: str = ""
    visibility_timeout_seconds: int = 1800
    wait_time_seconds: int = 20
    max_messages: int = 1


@dataclass
class DynamoDBConfig:
    metrics_table: str = ""


@dataclass
class BedrockRetryConfig:
    max_attempts: int = 3
    base_delay: int = 2
    max_delay: int = 30


@dataclass
class BedrockClientConfig:
    read_timeout: int = 120
    connect_timeout: int = 10
    retry_mode: str = "adaptive"
    max_attempts: int = 5


@dataclass
class BedrockConfig:
    llm_model_id: str = ""
    embedding_model_id: str = ""
    max_tokens: int = 8192
    temperature: float = 0.1
    retry: BedrockRetryConfig = field(default_factory=BedrockRetryConfig)
    client: BedrockClientConfig = field(default_factory=BedrockClientConfig)


@dataclass
class GuardrailConfig:
    guardrail_id: str = ""
    guardrail_version: str = "DRAFT"


@dataclass
class VectorsConfig:
    top_k: int = 5


@dataclass
class PipelineRunConfig:
    max_concurrent_themes: int = 3
    max_sample_tickets: int = 100
    max_notes_per_ticket: int = 2000
    tenant_folders: str = ""


@dataclass
class EcsConfig:
    cpu: int = 1024
    memory: int = 2048


@dataclass
class EcrConfig:
    repository_name: str = "article-pipeline"
    max_image_count: int = 10


@dataclass
class PromptsConfig:
    kb_system: str = ""
    rca_system: str = ""


@dataclass
class TagsConfig:
    project: str = "article-pipeline"
    environment: str = "production"
    owner: str = "devops-team"


# ── Top-level config ────────────────────────────────────────────────


@dataclass
class PipelineConfig:
    aws: AwsConfig = field(default_factory=AwsConfig)
    s3: S3Config = field(default_factory=S3Config)
    sqs: SqsConfig = field(default_factory=SqsConfig)
    dynamodb: DynamoDBConfig = field(default_factory=DynamoDBConfig)
    bedrock: BedrockConfig = field(default_factory=BedrockConfig)
    guardrail: GuardrailConfig = field(default_factory=GuardrailConfig)
    vectors: VectorsConfig = field(default_factory=VectorsConfig)
    pipeline: PipelineRunConfig = field(default_factory=PipelineRunConfig)
    ecs: EcsConfig = field(default_factory=EcsConfig)
    ecr: EcrConfig = field(default_factory=EcrConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    tags: TagsConfig = field(default_factory=TagsConfig)


# ── Env-var override mapping ────────────────────────────────────────

# Maps (section, field) -> env var name.  Built dynamically from the
# dataclass hierarchy so every flat + one-level-nested field is covered.

# Explicit registry of config dataclasses, used to resolve string type
# annotations (forward references) into their classes without using globals(),
# which avoids dynamic globals() indexing.
_CONFIG_TYPES: Dict[str, type] = {
    cls.__name__: cls
    for cls in (
        AwsConfig, S3Config, SqsConfig, DynamoDBConfig, BedrockRetryConfig,
        BedrockClientConfig, BedrockConfig, GuardrailConfig, VectorsConfig,
        PipelineRunConfig, EcsConfig, EcrConfig, PromptsConfig, TagsConfig,
        PipelineConfig,
    )
}


def _resolve_type(annotation):
    """Resolve a dataclass field annotation to its class.

    Field annotations may be actual classes or string forward references;
    string references are looked up in the explicit _CONFIG_TYPES registry.
    """
    if isinstance(annotation, str):
        return _CONFIG_TYPES.get(annotation)
    return annotation


_ENV_MAP: Dict[tuple, str] = {}


def _build_env_map() -> None:
    """Populate _ENV_MAP from PipelineConfig field hierarchy."""
    for section_field in fields(PipelineConfig):
        # Resolve string annotations to actual types
        section_cls = _resolve_type(section_field.type)
        if section_cls is None or not hasattr(section_cls, "__dataclass_fields__"):
            continue
        section_name = section_field.name
        for child_field in fields(section_cls):
            child_cls = _resolve_type(child_field.type)
            # Nested dataclass (e.g. bedrock.retry, bedrock.client)
            if child_cls is not None and hasattr(child_cls, "__dataclass_fields__"):
                for grandchild in fields(child_cls):
                    env_key = f"{section_name}_{child_field.name}_{grandchild.name}".upper()
                    _ENV_MAP[(section_name, child_field.name, grandchild.name)] = env_key
            else:
                env_key = f"{section_name}_{child_field.name}".upper()
                _ENV_MAP[(section_name, child_field.name)] = env_key


_build_env_map()


# ── Helpers ──────────────────────────────────────────────────────────


def _coerce(value: str, target_type):
    """Coerce a string env-var value to the target field type."""
    if target_type is bool:
        return value.lower() in ("1", "true", "yes")
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    return value


def _apply_env_overrides(cfg: PipelineConfig) -> None:
    """Override config values with environment variables where set."""
    for path_tuple, env_key in _ENV_MAP.items():
        env_val = os.environ.get(env_key)
        if env_val is None:
            continue

        if len(path_tuple) == 2:
            section_name, field_name = path_tuple
            section = getattr(cfg, section_name)
            target_type = type(getattr(section, field_name))
            setattr(section, field_name, _coerce(env_val, target_type))
        elif len(path_tuple) == 3:
            section_name, nested_name, field_name = path_tuple
            section = getattr(cfg, section_name)
            nested = getattr(section, nested_name)
            target_type = type(getattr(nested, field_name))
            setattr(nested, field_name, _coerce(env_val, target_type))


def _dict_to_config(data: dict) -> PipelineConfig:
    """Build a PipelineConfig from a raw YAML dict."""
    cfg = PipelineConfig()

    for section_field in fields(PipelineConfig):
        section_name = section_field.name
        section_data = data.get(section_name)
        if section_data is None:
            continue

        section_cls = _resolve_type(section_field.type)
        if section_cls is None:
            continue

        section_obj = getattr(cfg, section_name)

        if not hasattr(section_cls, "__dataclass_fields__"):
            continue

        for child_field in fields(section_cls):
            child_name = child_field.name
            if child_name not in section_data:
                continue
            child_val = section_data[child_name]

            child_cls = _resolve_type(child_field.type)

            # Nested dataclass (e.g. bedrock.retry)
            if child_cls is not None and hasattr(child_cls, "__dataclass_fields__") and isinstance(child_val, dict):
                nested_obj = getattr(section_obj, child_name)
                for gf in fields(child_cls):
                    if gf.name in child_val:
                        setattr(nested_obj, gf.name, child_val[gf.name])
            else:
                setattr(section_obj, child_name, child_val)

    return cfg


# ── Public API ───────────────────────────────────────────────────────


def load_config(path: str) -> PipelineConfig:
    """Load pipeline configuration from a YAML file.

    1. Read and parse the YAML file at *path*.
    2. Map the parsed dict into a PipelineConfig dataclass tree.
    3. Apply environment variable overrides (env vars take precedence).
    4. On missing file or invalid YAML, log a descriptive error and
       ``sys.exit(1)``.

    Returns
    -------
    PipelineConfig
        Fully resolved configuration object.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError:
        logger.error("Configuration file not found: %s", path)
        sys.exit(1)
    except yaml.YAMLError as exc:
        logger.error("Invalid YAML in configuration file %s: %s", path, exc)
        sys.exit(1)

    if not isinstance(raw, dict):
        logger.error(
            "Configuration file %s does not contain a YAML mapping (got %s)",
            path,
            type(raw).__name__,
        )
        sys.exit(1)

    cfg = _dict_to_config(raw)
    _apply_env_overrides(cfg)
    return cfg


def get_clients(config: PipelineConfig) -> dict:
    """Create fresh boto3 clients from the given config.

    Each call creates a new boto3 Session so concurrent asyncio tasks
    each get their own connection pool (boto3 clients are not
    thread-safe).

    Returns a dict with keys: s3, s3vectors, bedrock, dynamodb.
    """
    bedrock_boto_config = Config(
        retries={
            "max_attempts": config.bedrock.client.max_attempts,
            "mode": config.bedrock.client.retry_mode,
        },
        read_timeout=config.bedrock.client.read_timeout,
        connect_timeout=config.bedrock.client.connect_timeout,
    )

    session = boto3.Session(region_name=config.aws.region)
    return {
        "s3": session.client("s3"),
        "s3vectors": session.client("s3vectors"),
        "bedrock": session.client("bedrock-runtime", config=bedrock_boto_config),
        "dynamodb": session.resource("dynamodb").Table(config.dynamodb.metrics_table),
    }
