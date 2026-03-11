"""Pydantic schemas for the Brownfield Cartographer knowledge graph."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# --- Enums ---


class Language(str, Enum):
    PYTHON = "python"
    SQL = "sql"
    YAML = "yaml"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    UNKNOWN = "unknown"


class StorageType(str, Enum):
    TABLE = "table"
    FILE = "file"
    STREAM = "stream"
    API = "api"
    UNKNOWN = "unknown"


class TransformationType(str, Enum):
    SQL_QUERY = "sql_query"
    PYTHON_TRANSFORM = "python_transform"
    SPARK_JOB = "spark_job"
    DBT_MODEL = "dbt_model"
    AIRFLOW_TASK = "airflow_task"
    PANDAS_OP = "pandas_op"
    UNKNOWN = "unknown"


class EdgeType(str, Enum):
    IMPORTS = "IMPORTS"
    PRODUCES = "PRODUCES"
    CONSUMES = "CONSUMES"
    CALLS = "CALLS"
    CONFIGURES = "CONFIGURES"


class DomainCluster(str, Enum):
    INGESTION = "ingestion"
    TRANSFORMATION = "transformation"
    SERVING = "serving"
    MONITORING = "monitoring"
    CONFIGURATION = "configuration"
    TESTING = "testing"
    UTILITIES = "utilities"
    UNKNOWN = "unknown"


# --- Node Types ---


class ModuleNode(BaseModel):
    """Represents a code module (file) in the knowledge graph."""

    path: str
    language: Language
    purpose_statement: Optional[str] = None
    domain_cluster: DomainCluster = DomainCluster.UNKNOWN
    complexity_score: float = 0.0
    lines_of_code: int = 0
    comment_ratio: float = 0.0
    change_velocity_30d: int = 0
    is_dead_code_candidate: bool = False
    last_modified: Optional[datetime] = None
    imports: list[str] = Field(default_factory=list)
    public_functions: list[str] = Field(default_factory=list)
    classes: list[str] = Field(default_factory=list)
    pagerank_score: float = 0.0

    @field_validator("complexity_score")
    @classmethod
    def complexity_non_negative(cls, v: float) -> float:
        return max(0.0, v)

    @field_validator("comment_ratio")
    @classmethod
    def comment_ratio_bounded(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @field_validator("path")
    @classmethod
    def path_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("path must not be empty")
        return v


class DatasetNode(BaseModel):
    """Represents a data source/sink (table, file, stream)."""

    name: str
    storage_type: StorageType = StorageType.UNKNOWN
    schema_snapshot: Optional[dict] = None
    freshness_sla: Optional[str] = None
    owner: Optional[str] = None
    is_source_of_truth: bool = False

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("dataset name must not be empty")
        return v


class FunctionNode(BaseModel):
    """Represents a function or method."""

    qualified_name: str
    parent_module: str
    signature: str
    purpose_statement: Optional[str] = None
    call_count_within_repo: int = 0
    is_public_api: bool = False

    @field_validator("qualified_name")
    @classmethod
    def qualified_name_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("qualified_name must not be empty")
        return v


class TransformationNode(BaseModel):
    """Represents a data transformation operation."""

    name: str
    source_datasets: list[str] = Field(default_factory=list)
    target_datasets: list[str] = Field(default_factory=list)
    transformation_type: TransformationType = TransformationType.UNKNOWN
    source_file: str = ""
    line_range: tuple[int, int] = (0, 0)
    sql_query_if_applicable: Optional[str] = None


# --- Edge Types ---


class GraphEdge(BaseModel):
    """Represents an edge in the knowledge graph."""

    source: str
    target: str
    edge_type: EdgeType
    weight: float = 1.0
    metadata: dict = Field(default_factory=dict)

    @field_validator("weight")
    @classmethod
    def weight_positive(cls, v: float) -> float:
        return max(0.0, v)


# --- Graph Container ---


class KnowledgeGraphData(BaseModel):
    """Serializable representation of the full knowledge graph."""

    modules: dict[str, ModuleNode] = Field(default_factory=dict)
    datasets: dict[str, DatasetNode] = Field(default_factory=dict)
    functions: dict[str, FunctionNode] = Field(default_factory=dict)
    transformations: dict[str, TransformationNode] = Field(default_factory=dict)
    edges: list[GraphEdge] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


# --- Analysis Results ---


class GitVelocityEntry(BaseModel):
    """Git change frequency for a single file."""

    path: str
    commit_count_30d: int = 0
    last_commit_date: Optional[datetime] = None
    authors: list[str] = Field(default_factory=list)


class CircularDependency(BaseModel):
    """A detected circular dependency cycle."""

    cycle: list[str]
    severity: str = "warning"


class AnalysisResult(BaseModel):
    """Container for a complete analysis run."""

    target_path: str
    analysis_timestamp: datetime = Field(default_factory=datetime.now)
    knowledge_graph: KnowledgeGraphData = Field(default_factory=KnowledgeGraphData)
    git_velocity: list[GitVelocityEntry] = Field(default_factory=list)
    circular_dependencies: list[CircularDependency] = Field(default_factory=list)
    dead_code_candidates: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    sinks: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
