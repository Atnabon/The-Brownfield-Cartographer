"""Multi-language AST parsing with tree-sitter and LanguageRouter.

Uses tree-sitter to extract:
- Import statements (Python)
- Function/class definitions with signatures
- SQL table references  
- YAML structure keys
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from src.models import Language

logger = logging.getLogger(__name__)

# Map file extensions to Language enum
EXTENSION_MAP: dict[str, Language] = {
    ".py": Language.PYTHON,
    ".sql": Language.SQL,
    ".yml": Language.YAML,
    ".yaml": Language.YAML,
    ".js": Language.JAVASCRIPT,
    ".ts": Language.TYPESCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".tsx": Language.TYPESCRIPT,
}

# File/directory patterns to skip
SKIP_PATTERNS = {
    "__pycache__",
    ".git",
    "node_modules",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".egg-info",
    ".eggs",
}


@dataclass
class ImportInfo:
    """Extracted import information."""

    module: str
    names: list[str] = field(default_factory=list)
    is_relative: bool = False
    alias: Optional[str] = None


@dataclass
class FunctionInfo:
    """Extracted function information."""

    name: str
    signature: str
    line_start: int
    line_end: int
    is_public: bool = True
    decorators: list[str] = field(default_factory=list)
    docstring: str | None = None


@dataclass
class ClassInfo:
    """Extracted class information."""

    name: str
    bases: list[str] = field(default_factory=list)
    line_start: int = 0
    line_end: int = 0
    methods: list[str] = field(default_factory=list)
    docstring: str | None = None


@dataclass
class ModuleAnalysis:
    """Complete analysis result for a single module."""

    path: str
    language: Language
    imports: list[ImportInfo] = field(default_factory=list)
    functions: list[FunctionInfo] = field(default_factory=list)
    classes: list[ClassInfo] = field(default_factory=list)
    lines_of_code: int = 0
    comment_lines: int = 0
    blank_lines: int = 0
    complexity_score: float = 0.0
    errors: list[str] = field(default_factory=list)


from typing import Optional


class LanguageRouter:
    """Routes files to the correct parser based on file extension."""

    @staticmethod
    def detect_language(file_path: str | Path) -> Language:
        path = Path(file_path)
        suffix = path.suffix.lower()
        return EXTENSION_MAP.get(suffix, Language.UNKNOWN)

    @staticmethod
    def should_skip(path: Path) -> bool:
        for part in path.parts:
            if part in SKIP_PATTERNS:
                return True
        return False

    @staticmethod
    def get_analyzable_files(root: Path) -> list[tuple[Path, Language]]:
        """Walk directory and return all analyzable files with their languages."""
        results = []
        if not root.is_dir():
            lang = LanguageRouter.detect_language(root)
            if lang != Language.UNKNOWN:
                results.append((root, lang))
            return results

        for path in sorted(root.rglob("*")):
            if path.is_file() and not LanguageRouter.should_skip(path):
                lang = LanguageRouter.detect_language(path)
                if lang != Language.UNKNOWN:
                    results.append((path, lang))
        return results


class TreeSitterAnalyzer:
    """Multi-language AST analyzer using tree-sitter.

    Falls back to regex-based parsing if tree-sitter grammars are unavailable.
    """

    def __init__(self):
        self._parsers: dict[Language, object] = {}
        self._ts_available = False
        self._init_tree_sitter()

    def _init_tree_sitter(self):
        """Try to initialize tree-sitter parsers."""
        try:
            import tree_sitter
            import tree_sitter_python
            self._ts_available = True
            py_lang = tree_sitter.Language(tree_sitter_python.language())
            parser = tree_sitter.Parser(py_lang)
            self._parsers[Language.PYTHON] = parser
            logger.info("tree-sitter Python parser initialized")
        except (ImportError, Exception) as e:
            logger.warning(f"tree-sitter not available, using regex fallback: {e}")
            self._ts_available = False

        # Try SQL parser
        try:
            import tree_sitter
            import tree_sitter_sql
            sql_lang = tree_sitter.Language(tree_sitter_sql.language())
            parser = tree_sitter.Parser(sql_lang)
            self._parsers[Language.SQL] = parser
            logger.info("tree-sitter SQL parser initialized")
        except (ImportError, Exception) as e:
            logger.debug(f"tree-sitter SQL not available: {e}")

    def analyze_file(self, file_path: Path, language: Language) -> ModuleAnalysis:
        """Analyze a single file and return structured analysis."""
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ModuleAnalysis(
                path=str(file_path),
                language=language,
                errors=[f"Failed to read file: {e}"],
            )

        lines = content.splitlines()
        loc = len(lines)
        blank = sum(1 for line in lines if not line.strip())
        comments = self._count_comments(lines, language)

        if language == Language.PYTHON:
            return self._analyze_python(file_path, content, lines, loc, blank, comments)
        elif language == Language.SQL:
            return self._analyze_sql(file_path, content, lines, loc, blank, comments)
        elif language == Language.YAML:
            return self._analyze_yaml(file_path, content, lines, loc, blank, comments)
        else:
            return ModuleAnalysis(
                path=str(file_path),
                language=language,
                lines_of_code=loc,
                blank_lines=blank,
                comment_lines=comments,
            )

    def _count_comments(self, lines: list[str], language: Language) -> int:
        count = 0
        if language == Language.PYTHON:
            in_docstring = False
            for line in lines:
                stripped = line.strip()
                if stripped.startswith('#'):
                    count += 1
                elif '"""' in stripped or "'''" in stripped:
                    count += 1
                    if stripped.count('"""') == 1 or stripped.count("'''") == 1:
                        in_docstring = not in_docstring
                elif in_docstring:
                    count += 1
        elif language == Language.SQL:
            for line in lines:
                stripped = line.strip()
                if stripped.startswith('--'):
                    count += 1
        elif language == Language.YAML:
            for line in lines:
                if line.strip().startswith('#'):
                    count += 1
        return count

    def _analyze_python(
        self, file_path: Path, content: str, lines: list[str],
        loc: int, blank: int, comments: int,
    ) -> ModuleAnalysis:
        """Analyze Python source with tree-sitter or regex fallback."""
        analysis = ModuleAnalysis(
            path=str(file_path),
            language=Language.PYTHON,
            lines_of_code=loc,
            blank_lines=blank,
            comment_lines=comments,
        )

        if self._ts_available and Language.PYTHON in self._parsers:
            try:
                self._analyze_python_ts(content, analysis)
                return analysis
            except Exception as e:
                logger.debug(f"tree-sitter parse failed for {file_path}, falling back: {e}")

        # Regex fallback
        self._analyze_python_regex(content, lines, analysis)
        return analysis

    def _analyze_python_ts(self, content: str, analysis: ModuleAnalysis):
        """Parse Python using tree-sitter AST."""
        import tree_sitter

        parser = self._parsers[Language.PYTHON]
        tree = parser.parse(content.encode("utf-8"))
        root = tree.root_node

        for child in root.children:
            if child.type == "import_statement":
                text = content[child.start_byte:child.end_byte]
                match = re.match(r'import\s+([\w.]+)(?:\s+as\s+(\w+))?', text)
                if match:
                    analysis.imports.append(ImportInfo(
                        module=match.group(1),
                        alias=match.group(2),
                    ))

            elif child.type == "import_from_statement":
                text = content[child.start_byte:child.end_byte]
                match = re.match(r'from\s+(\.*)(\w[\w.]*)\s+import\s+(.+)', text)
                if match:
                    dots = match.group(1)
                    module = match.group(2)
                    names_str = match.group(3).strip()
                    names = [n.strip().split(" as ")[0].strip()
                             for n in names_str.split(",") if n.strip()]
                    analysis.imports.append(ImportInfo(
                        module=module,
                        names=names,
                        is_relative=len(dots) > 0,
                    ))
                else:
                    # Handle 'from . import something'
                    match2 = re.match(r'from\s+(\.+)\s+import\s+(.+)', text)
                    if match2:
                        names_str = match2.group(2).strip()
                        names = [n.strip().split(" as ")[0].strip()
                                 for n in names_str.split(",")]
                        analysis.imports.append(ImportInfo(
                            module=".",
                            names=names,
                            is_relative=True,
                        ))

            elif child.type == "function_definition":
                self._extract_python_function_ts(content, child, analysis)

            elif child.type == "decorated_definition":
                # Get decorators and inner definition
                decorators = []
                inner = None
                for sub in child.children:
                    if sub.type == "decorator":
                        dec_text = content[sub.start_byte:sub.end_byte].strip().lstrip("@")
                        decorators.append(dec_text)
                    elif sub.type == "function_definition":
                        inner = sub
                    elif sub.type == "class_definition":
                        inner = sub

                if inner and inner.type == "function_definition":
                    self._extract_python_function_ts(content, inner, analysis, decorators)
                elif inner and inner.type == "class_definition":
                    self._extract_python_class_ts(content, inner, analysis)

            elif child.type == "class_definition":
                self._extract_python_class_ts(content, child, analysis)

        # Calculate complexity
        analysis.complexity_score = self._calc_python_complexity(content)

    def _extract_python_function_ts(
        self, content: str, node, analysis: ModuleAnalysis,
        decorators: list[str] | None = None,
    ):
        """Extract function info from a tree-sitter function_definition node."""
        text = content[node.start_byte:node.end_byte]
        # Get function name from first identifier child
        name = None
        params = ""
        for child in node.children:
            if child.type == "identifier" and name is None:
                name = content[child.start_byte:child.end_byte]
            elif child.type == "parameters":
                params = content[child.start_byte:child.end_byte]

        if name:
            is_public = not name.startswith("_")
            sig = f"def {name}{params}"
            analysis.functions.append(FunctionInfo(
                name=name,
                signature=sig,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                is_public=is_public,
                decorators=decorators or [],
            ))

    def _extract_python_class_ts(self, content: str, node, analysis: ModuleAnalysis):
        """Extract class info from a tree-sitter class_definition node."""
        name = None
        bases = []
        methods = []

        for child in node.children:
            if child.type == "identifier" and name is None:
                name = content[child.start_byte:child.end_byte]
            elif child.type == "argument_list":
                bases_text = content[child.start_byte:child.end_byte]
                bases_text = bases_text.strip("()")
                bases = [b.strip() for b in bases_text.split(",") if b.strip()]
            elif child.type == "block":
                for block_child in child.children:
                    if block_child.type == "function_definition":
                        for sub in block_child.children:
                            if sub.type == "identifier":
                                methods.append(content[sub.start_byte:sub.end_byte])
                                break
                    elif block_child.type == "decorated_definition":
                        for sub in block_child.children:
                            if sub.type == "function_definition":
                                for subsub in sub.children:
                                    if subsub.type == "identifier":
                                        methods.append(content[subsub.start_byte:subsub.end_byte])
                                        break
                                break

        if name:
            analysis.classes.append(ClassInfo(
                name=name,
                bases=bases,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                methods=methods,
            ))

    def _analyze_python_regex(
        self, content: str, lines: list[str], analysis: ModuleAnalysis,
    ):
        """Regex-based Python analysis fallback."""
        # Imports
        for line in lines:
            stripped = line.strip()
            match = re.match(r'^import\s+([\w.]+)(?:\s+as\s+(\w+))?', stripped)
            if match:
                analysis.imports.append(ImportInfo(
                    module=match.group(1), alias=match.group(2),
                ))
                continue
            match = re.match(r'^from\s+(\.*)(\w[\w.]*)\s+import\s+(.+)', stripped)
            if match:
                dots = match.group(1)
                module = match.group(2)
                names_str = match.group(3).strip()
                if names_str.startswith("("):
                    names_str = names_str.strip("()")
                names = [n.strip().split(" as ")[0].strip()
                         for n in names_str.split(",") if n.strip()]
                analysis.imports.append(ImportInfo(
                    module=module, names=names, is_relative=len(dots) > 0,
                ))

        # Functions
        for i, line in enumerate(lines):
            match = re.match(r'^(\s*)def\s+(\w+)\s*\(([^)]*)\)', line)
            if match:
                indent = match.group(1)
                name = match.group(2)
                params = match.group(3)
                if len(indent) == 0:  # top-level function
                    analysis.functions.append(FunctionInfo(
                        name=name,
                        signature=f"def {name}({params})",
                        line_start=i + 1,
                        line_end=i + 1,
                        is_public=not name.startswith("_"),
                    ))

        # Classes
        for i, line in enumerate(lines):
            match = re.match(r'^class\s+(\w+)(?:\(([^)]*)\))?:', line)
            if match:
                name = match.group(1)
                bases_str = match.group(2) or ""
                bases = [b.strip() for b in bases_str.split(",") if b.strip()]
                analysis.classes.append(ClassInfo(
                    name=name, bases=bases, line_start=i + 1, line_end=i + 1,
                ))

        analysis.complexity_score = self._calc_python_complexity(content)

    def _calc_python_complexity(self, content: str) -> float:
        """Estimate cyclomatic complexity from branch keywords."""
        keywords = ["if ", "elif ", "else:", "for ", "while ", "except ", "except:",
                     "with ", "and ", "or "]
        score = 1.0
        for line in content.splitlines():
            stripped = line.strip()
            for kw in keywords:
                if stripped.startswith(kw) or f" {kw}" in stripped:
                    score += 1
                    break
        return score

    def _analyze_sql(
        self, file_path: Path, content: str, lines: list[str],
        loc: int, blank: int, comments: int,
    ) -> ModuleAnalysis:
        """SQL file analysis using tree-sitter AST or regex fallback."""
        analysis = ModuleAnalysis(
            path=str(file_path),
            language=Language.SQL,
            lines_of_code=loc,
            blank_lines=blank,
            comment_lines=comments,
        )

        if self._ts_available and Language.SQL in self._parsers:
            try:
                self._analyze_sql_ts(content, analysis)
                return analysis
            except Exception as e:
                logger.debug(f"tree-sitter SQL parse failed for {file_path}, falling back: {e}")

        # Regex fallback for table references
        tables = set()
        # FROM / JOIN patterns
        for match in re.finditer(
            r'(?:FROM|JOIN)\s+([`"\']?[\w.]+[`"\']?)', content, re.IGNORECASE
        ):
            table = match.group(1).strip('`"\'')
            if table.upper() not in ("SELECT", "WHERE", "SET", "VALUES"):
                tables.add(table)

        # CREATE TABLE
        for match in re.finditer(
            r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?([`"\']?[\w.]+[`"\']?)',
            content,
            re.IGNORECASE,
        ):
            table = match.group(1).strip('`"\'')
            tables.add(table)

        for table in tables:
            analysis.imports.append(ImportInfo(module=table, names=[], is_relative=False))

        return analysis

    def _analyze_sql_ts(self, content: str, analysis: ModuleAnalysis):
        """Parse SQL using tree-sitter AST to extract structural elements."""
        parser = self._parsers[Language.SQL]
        tree = parser.parse(content.encode("utf-8"))
        root = tree.root_node

        tables = set()
        # Walk the AST to find table references, CTEs, and query structure
        self._walk_sql_node(root, content, tables, analysis)

        for table in tables:
            analysis.imports.append(ImportInfo(module=table, names=[], is_relative=False))

    def _walk_sql_node(self, node, content: str, tables: set, analysis: ModuleAnalysis):
        """Recursively walk SQL AST to extract structural elements."""
        node_type = node.type

        # Extract table/relation references
        if node_type in ("relation", "table_reference", "object_reference"):
            text = content[node.start_byte:node.end_byte].strip('`"\' ')
            if text and text.upper() not in (
                "SELECT", "WHERE", "SET", "VALUES", "TABLE", "AS", "ON",
            ):
                tables.add(text)

        # Extract CTE definitions as function-like entries
        if node_type in ("cte", "common_table_expression"):
            text = content[node.start_byte:node.end_byte]
            # Get CTE name from first identifier child
            for child in node.children:
                if child.type in ("identifier", "name"):
                    cte_name = content[child.start_byte:child.end_byte].strip()
                    analysis.functions.append(FunctionInfo(
                        name=f"CTE:{cte_name}",
                        signature=f"WITH {cte_name} AS (...)",
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        is_public=True,
                    ))
                    break

        # Extract CREATE statements
        if node_type in ("create_table_statement", "create_view_statement", "create_statement"):
            text = content[node.start_byte:node.end_byte]
            match = re.match(
                r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?([`"\'?\w.]+)',
                text, re.IGNORECASE,
            )
            if match:
                target = match.group(1).strip('`"\'')
                analysis.functions.append(FunctionInfo(
                    name=f"CREATE:{target}",
                    signature=f"CREATE {target}",
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    is_public=True,
                ))

        for child in node.children:
            self._walk_sql_node(child, content, tables, analysis)

    def _analyze_yaml(
        self, file_path: Path, content: str, lines: list[str],
        loc: int, blank: int, comments: int,
    ) -> ModuleAnalysis:
        """YAML file analysis extracting pipeline-relevant key hierarchies."""
        analysis = ModuleAnalysis(
            path=str(file_path),
            language=Language.YAML,
            lines_of_code=loc,
            blank_lines=blank,
            comment_lines=comments,
        )

        try:
            import yaml as pyyaml
            data = pyyaml.safe_load(content)
            if isinstance(data, dict):
                self._extract_yaml_keys(data, "", lines, analysis)
        except Exception:
            # Fallback: extract top-level keys via regex
            for i, line in enumerate(lines):
                match = re.match(r'^(\w[\w_-]*):', line)
                if match:
                    analysis.functions.append(FunctionInfo(
                        name=match.group(1),
                        signature=f"key: {match.group(1)}",
                        line_start=i + 1,
                        line_end=i + 1,
                        is_public=True,
                    ))

        # Detect pipeline-specific patterns in the YAML
        fname = Path(file_path).name.lower()
        if any(kw in fname for kw in ("dag", "pipeline", "workflow", "schedule")):
            analysis.imports.append(ImportInfo(
                module=f"pipeline_config:{fname}", names=[], is_relative=False,
            ))

        # Detect dbt schema references
        if isinstance(data, dict) if 'data' in dir() else False:
            pass  # handled by _extract_yaml_keys
        for line in lines:
            ref_match = re.search(r"ref\(\s*['\"](\w+)['\"]\s*\)", line)
            if ref_match:
                analysis.imports.append(ImportInfo(
                    module=f"dbt_ref:{ref_match.group(1)}", names=[], is_relative=False,
                ))

        return analysis

    def _extract_yaml_keys(
        self, data: dict, prefix: str, lines: list[str], analysis: ModuleAnalysis,
        depth: int = 0, max_depth: int = 3,
    ):
        """Recursively extract YAML key hierarchies relevant to pipeline config."""
        if depth > max_depth:
            return

        # Pipeline-relevant keys to track at any depth
        pipeline_keys = {
            "sources", "models", "seeds", "snapshots", "tests",
            "schedule", "schedule_interval", "dag_id", "task_id",
            "operator", "dependencies", "depends_on", "upstream",
            "tables", "columns", "materialized", "schema", "database",
            "vars", "config", "tags", "meta", "description",
        }

        for key, value in data.items():
            full_key = f"{prefix}.{key}" if prefix else str(key)

            # Find approximate line number for this key
            line_num = 1
            key_str = str(key)
            for i, line in enumerate(lines):
                if re.match(rf'\s*{re.escape(key_str)}\s*:', line):
                    line_num = i + 1
                    break

            is_pipeline_relevant = key.lower() in pipeline_keys or depth == 0

            if is_pipeline_relevant:
                sig = f"key: {full_key}"
                if isinstance(value, list):
                    sig += f" [{len(value)} items]"
                elif isinstance(value, dict):
                    sig += f" {{{len(value)} keys}}"

                analysis.functions.append(FunctionInfo(
                    name=full_key,
                    signature=sig,
                    line_start=line_num,
                    line_end=line_num,
                    is_public=True,
                ))

            if isinstance(value, dict):
                self._extract_yaml_keys(value, full_key, lines, analysis, depth + 1, max_depth)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        self._extract_yaml_keys(
                            item, full_key, lines, analysis, depth + 1, max_depth,
                        )

    def analyze_directory(self, root: Path) -> list[ModuleAnalysis]:
        """Analyze all files in a directory tree."""
        router = LanguageRouter()
        files = router.get_analyzable_files(root)
        results = []

        for file_path, language in files:
            try:
                analysis = self.analyze_file(file_path, language)
                results.append(analysis)
            except Exception as e:
                logger.error(f"Failed to analyze {file_path}: {e}")
                results.append(ModuleAnalysis(
                    path=str(file_path),
                    language=language,
                    errors=[str(e)],
                ))

        return results
