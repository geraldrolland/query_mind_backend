"""
PostgreSQL JSONB Query Compiler Module

This module provides a production-grade compiler that translates Domain-Specific Language (DSL)
query plans into optimized PostgreSQL queries for JSONB data storage. It handles validation,
compilation, and execution of queries against dataset records stored as JSONB.

Author: Insightly Team
Last Modified: March 12, 2026
"""

import logging
from decimal import ROUND_HALF_UP, Decimal
from datetime import datetime
from typing import Dict, List, Tuple, Any, Optional

from dateutil.parser import parse as parse_date
from sqlalchemy import text
from sqlmodel import Session

from .base import BaseQueryCompiler
from app.schemas.query import DslDefinitionSchema as QueryPlan
from app.db import get_engine

# Configure module logger
logger = logging.getLogger(__name__)


class PostgresJSONBCompiler(BaseQueryCompiler):
    """
    Compiles and executes query plans against PostgreSQL JSONB storage.
    
    This compiler transforms abstract query plans (containing filters, sorts, aggregations,
    optional limits, etc.) into safe, parameterized PostgreSQL queries. It validates all inputs
    against the dataset schema and ensures type safety throughout the query lifecycle.
    
    Attributes:
        TABLE_NAME: Name of the database table storing dataset rows
        OPERATOR_MAP: Mapping of DSL operators to SQL operators
        TYPE_VALIDATORS: Allowed Python types for each schema type
    """

    # Database configuration
    TABLE_NAME = "datasetrow"

    # Operator mappings for query compilation
    OPERATOR_MAP = {
        "eq": "=",
        "neq": "!=",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
        "before": "<",
        "after": ">",
    }

    # Type validation mapping for schema field types
    TYPE_VALIDATORS = {
        "number": (int, float, Decimal),
        "string": (str,),
        "boolean": (bool,),
        "date": (datetime,)
    }

    def __init__(self):
        """Initialize the compiler with empty schema state."""
        self.schema: Optional[Dict[str, Any]] = None

    def _fetch_dataset_schema(self, dataset_id: int) -> Dict[str, Any]:
        """
        Fetch the dataset schema from the database.
        
        Args:
            dataset_id: The unique identifier of the dataset
            
        Returns:
            Dictionary containing the dataset schema definition
            
        Raises:
            ValueError: If dataset is not found
        """
        try:
            with Session(get_engine()) as session:
                sql_query = text("SELECT dataset_schema FROM dataset WHERE id = :dataset_id")
                result = session.execute(sql_query.bindparams(dataset_id=dataset_id)).mappings().first()
                
                if not result:
                    raise ValueError(f"Dataset with id {dataset_id} not found")
                
                dataset_schema = result.get("dataset_schema")
                if not dataset_schema:
                    raise ValueError(f"Dataset {dataset_id} has no schema defined")
                    
                return dataset_schema
        except Exception as e:
            logger.error(f"Error fetching dataset schema for id {dataset_id}: {str(e)}")
            raise

    def _validate_field_type(self, field_name: str, value: Any, expected_type: str) -> Any:
        """
        Validate and potentially convert a field value to match the expected type.
        
        Attempts to parse date strings and validates that the value type matches
        the schema definition for the field.
        
        Args:
            field_name: Name of the field being validated
            value: The value to validate
            expected_type: Expected type from schema ('number', 'string', 'boolean', 'date')
            
        Returns:
            The validated (and potentially converted) value
            
        Raises:
            ValueError: If value type doesn't match expected type
        """
        allowed_types = self.TYPE_VALIDATORS.get(expected_type, ())

        # List-valued operators (between/in/nin): validate each element
        if isinstance(value, list):
            return [
                self._validate_field_type(field_name, item, expected_type)
                for item in value
            ]

        # Special handling for date fields - attempt to parse string dates
        if expected_type == "date" and isinstance(value, str):
            try:
                parsed_date = parse_date(value)
                return parsed_date.isoformat()
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to parse date value '{value}' for field '{field_name}': {str(e)}")
        
        # Validate value type matches expected type
        if not isinstance(value, allowed_types):
            raise ValueError(
                f"Type mismatch for field '{field_name}': "
                f"expected {expected_type}, got {type(value).__name__}"
            )
        
        return value

    def _validate_select_fields(self, select_fields: List[str], schema: Dict[str, Any]) -> None:
        """
        Validate that all selected fields exist in the dataset schema.
        
        Args:
            select_fields: List of field names to select
            schema: Dataset schema definition
            
        Raises:
            ValueError: If any field is not found in schema
        """
        if not select_fields:
            return
            
        invalid_fields = [field for field in select_fields if field not in schema]
        if invalid_fields:
            raise ValueError(
                f"Selected fields not found in dataset schema: {', '.join(invalid_fields)}"
            )

    def _validate_filters(self, filters: List, schema: Dict[str, Any]) -> None:
        """
        Validate filter conditions against the dataset schema.
        
        Ensures that:
        1. All filtered fields exist in the schema
        2. Operators are allowed for each field type
        3. Filter values match the expected field type
        
        Args:
            filters: List of filter objects from the query plan
            schema: Dataset schema definition
            
        Raises:
            ValueError: If validation fails for any filter
        """
        if not filters:
            return
            
        for filter_obj in filters:
            field_name = filter_obj.field
            operator = filter_obj.operator
            value = filter_obj.value
            
            # Validate field exists
            field_rule = schema.get(field_name)
            if not field_rule:
                raise ValueError(f"Filter field '{field_name}' not found in dataset schema")
            
            # Validate operator is allowed for this field
            allowed_operators = field_rule.get("allowed_operators", [])
            if operator not in allowed_operators:
                raise ValueError(
                    f"Operator '{operator}' not allowed for field '{field_name}'. "
                    f"Allowed operators: {', '.join(allowed_operators)}"
                )
            
            # Validate and convert value type
            expected_type = field_rule.get("type")
            filter_obj.value = self._validate_field_type(field_name, value, expected_type)

    def _validate_metrics(self, metrics: List, schema: Dict[str, Any]) -> None:
        """
        Validate metric/aggregation definitions against the schema.
        
        Ensures that:
        1. Fields used in metrics exist (except for 'count')
        2. Fields are marked as aggregatable in the schema
        3. Count metrics don't specify a field
        
        Args:
            metrics: List of metric objects from the query plan
            schema: Dataset schema definition
            
        Raises:
            ValueError: If validation fails for any metric
        """
        if not metrics:
            return
            
        for metric in metrics:
            metric_type = metric.metric_type
            field_name = metric.field
            
            # Count metrics should not have a field specified
            if metric_type == "count" and field_name:
                raise ValueError("Count metric should not specify a field")
            
            # Other metrics must specify an existing aggregatable field
            if metric_type != "count":
                if not field_name:
                    raise ValueError(f"Metric type '{metric_type}' requires a field")
                    
                field_rule = schema.get(field_name)
                if not field_rule:
                    raise ValueError(f"Metric field '{field_name}' not found in dataset schema")
                
                is_aggregatable = field_rule.get("aggregatable", False)
                if not is_aggregatable:
                    raise ValueError(
                        f"Field '{field_name}' is not marked as aggregatable in schema. "
                        f"Cannot apply '{metric_type}' aggregation."
                    )

    @staticmethod
    def _group_entry(entry: Any) -> Tuple[str, Optional[str]]:
        """Normalize a group_by entry to (field, granularity_or_None)."""
        if isinstance(entry, str):
            return entry, None
        return entry.field, entry.granularity

    @staticmethod
    def _bucket_expr(field: str, granularity: str) -> str:
        return f"date_trunc('{granularity}', (data->>'{field}')::date)::date"

    @staticmethod
    def _bucket_alias(field: str, granularity: str) -> str:
        return f"{field}_{granularity}"

    def _validate_group_by(self, group_by_fields: List, schema: Dict[str, Any]) -> None:
        """
        Validate GROUP BY fields exist in the schema.

        Entries may be plain field names or GroupByFieldSchema dicts
        ({field, granularity}); granularity is only valid for date fields.

        Args:
            group_by_fields: List of field names or group specs to group by
            schema: Dataset schema definition

        Raises:
            ValueError: If any field is not found in schema
        """
        if not group_by_fields:
            return

        invalid_fields = []
        for entry in group_by_fields:
            field, granularity = self._group_entry(entry)
            field_rule = schema.get(field)
            if not field_rule:
                invalid_fields.append(field)
                continue
            if granularity and field_rule.get("type") != "date":
                raise ValueError(
                    f"Granularity grouping is only supported for date fields; "
                    f"'{field}' has type '{field_rule.get('type')}'"
                )
        if invalid_fields:
            raise ValueError(
                f"GROUP BY fields not found in dataset schema: {', '.join(invalid_fields)}"
            )

    def _validate_sorts(self, sorts: List, schema: Dict[str, Any], metrics: List = None) -> None:
        """
        Validate sort/ORDER BY fields.
        
        Sort fields can be either:
        1. Fields from the dataset schema
        2. Aliases of computed metrics
        
        Args:
            sorts: List of sort objects from the query plan
            schema: Dataset schema definition
            metrics: List of metrics (for alias validation)
            
        Raises:
            ValueError: If any sort field is invalid
        """
        if not sorts:
            return
            
        metric_aliases = {metric.alias for metric in (metrics or [])}
        
        for sort in sorts:
            field_name = sort.field
            
            # Check if it's a metric alias
            if field_name in metric_aliases:
                continue
                
            # Otherwise, must be in schema
            if field_name not in schema:
                raise ValueError(
                    f"Sort field '{field_name}' not found in dataset schema "
                    f"and is not a metric alias"
                )
    
    def validate_dsl(self, query_plan: QueryPlan, dataset_id: int) -> None:
        """
        Validate the entire query plan against the dataset schema.
        
        This is the main validation entry point that orchestrates all validation checks.
        It fetches the dataset schema, stores it for later use, and validates:
        - Selected fields
        - Filter conditions and operators
        - Metric definitions and aggregations
        - GROUP BY clauses
        - ORDER BY clauses
        
        Args:
            query_plan: The complete query plan to validate
            dataset_id: ID of the dataset to validate against
            
        Raises:
            ValueError: If any validation check fails
        """
        logger.info(f"Validating query plan for dataset {dataset_id}")
        
        # Fetch and cache the dataset schema
        dataset_schema = self._fetch_dataset_schema(dataset_id)
        self.schema = dataset_schema
        
        # Validate each component of the query plan
        try:
            self._validate_select_fields(query_plan.select, dataset_schema)
            self._validate_filters(query_plan.filters, dataset_schema)
            self._validate_metrics(query_plan.metrics, dataset_schema)
            self._validate_group_by(query_plan.group_by, dataset_schema)
            self._validate_sorts(query_plan.sorts, dataset_schema, query_plan.metrics)
            
            logger.info(f"Query plan validation successful for dataset {dataset_id}")
        except ValueError as e:
            logger.error(f"Query plan validation failed for dataset {dataset_id}: {str(e)}")
            raise

    def _compile_select_clause(self, select_fields: List[str]) -> str:
        """
        Generate the SELECT clause for regular field selection.
        
        Extracts JSONB fields using the ->> operator which returns text values.
        
        Args:
            select_fields: List of field names to select
            
        Returns:
            Compiled SELECT clause SQL string (without 'SELECT' keyword)
        """
        if not select_fields:
            return ""
            
        select_parts = [f"data->>'{field}' AS {field}" for field in select_fields]
        return ", ".join(select_parts)

    def _compile_metrics_clause(self, metrics: List) -> str:
        """
        Generate SQL for aggregation functions (metrics).
        
        Supports: COUNT, SUM, AVG, MIN, MAX
        Numeric fields are cast to ::numeric for proper aggregation.
        
        Args:
            metrics: List of metric objects from the query plan
            
        Returns:
            Compiled aggregation SQL string
        """
        if not metrics:
            return ""
            
        metric_parts = []
        for metric in metrics:
            metric_type = metric.metric_type
            field_name = metric.field
            alias = metric.alias
            
            if metric_type == "count":
                # COUNT(*) for total row count
                metric_parts.append(f"COUNT(*) AS {alias}")
            elif metric_type in ("sum", "avg", "min", "max"):
                # Numeric aggregations require type casting
                metric_parts.append(
                    f"{metric_type.upper()}((data->>'{field_name}')::numeric) AS {alias}"
                )
            else:
                logger.warning(f"Unknown metric type '{metric_type}' for field '{field_name}'")
                
        return ", ".join(metric_parts)

    def _compile_where_clause(self, filters: List, dataset_id: str, params: Dict) -> Tuple[str, int]:
        """
        Generate WHERE clause with parameterized filter conditions.
        
        Ensures SQL injection safety by using parameter binding for all values.
        Automatically casts numeric fields for proper comparison.
        
        Args:
            filters: List of filter objects from the query plan
            dataset_id: Dataset ID for base condition
            params: Dictionary to populate with query parameters (mutated in-place)
            
        Returns:
            Tuple of (WHERE clause SQL, next available parameter counter)
        """
        param_counter = 0
        where_conditions = ["datasetrow.dataset_id = :dataset_id"]
        params["dataset_id"] = dataset_id
        
        if not filters:
            return " AND ".join(where_conditions), param_counter
            
        for filter_obj in filters:
            field_name = filter_obj.field
            operator = filter_obj.operator
            value = filter_obj.value

            field_type = self.schema.get(field_name, {}).get("type", "string")
            type_cast = "::numeric" if field_type == "number" else ""

            if operator in ("between", "in", "nin"):
                if not isinstance(value, list) or not value:
                    raise ValueError(
                        f"Operator '{operator}' for field '{field_name}' requires "
                        "a non-empty list value"
                    )
                if operator == "between" and len(value) != 2:
                    raise ValueError(
                        f"Operator 'between' for field '{field_name}' requires "
                        "exactly 2 values"
                    )

            if operator == "between":
                param_a = f"param_{param_counter}"
                param_counter += 1
                param_b = f"param_{param_counter}"
                param_counter += 1
                condition = (
                    f"(data->>'{field_name}'){type_cast} "
                    f"BETWEEN :{param_a} AND :{param_b}"
                )
                where_conditions.append(condition)
                params[param_a] = value[0]
                params[param_b] = value[1]
            elif operator in ("in", "nin"):
                sql_operator = "IN" if operator == "in" else "NOT IN"
                placeholders = []
                for item in value:
                    param_name = f"param_{param_counter}"
                    param_counter += 1
                    placeholders.append(f":{param_name}")
                    params[param_name] = item
                condition = (
                    f"(data->>'{field_name}'){type_cast} "
                    f"{sql_operator} ({', '.join(placeholders)})"
                )
                where_conditions.append(condition)
            elif operator == "contains":
                param_name = f"param_{param_counter}"
                param_counter += 1
                condition = f"(data->>'{field_name}') LIKE '%' || :{param_name} || '%'"
                where_conditions.append(condition)
                params[param_name] = value
            else:
                sql_operator = self.OPERATOR_MAP.get(operator)
                if not sql_operator:
                    raise ValueError(
                        f"Unmapped operator '{operator}' for field '{field_name}'"
                    )
                param_name = f"param_{param_counter}"
                param_counter += 1
                condition = (
                    f"(data->>'{field_name}'){type_cast} {sql_operator} :{param_name}"
                )
                where_conditions.append(condition)
                params[param_name] = value

        return " AND ".join(where_conditions), param_counter

    def _compile_group_by_clause(self, group_by_fields: List) -> str:
        """
        Generate GROUP BY clause.

        Entries may be plain field names or {field, granularity} dicts for
        date bucketing (e.g. date_trunc('year', ...)).

        Args:
            group_by_fields: List of field names or group specs

        Returns:
            Complete GROUP BY clause SQL string (including 'GROUP BY' keyword)
        """
        if not group_by_fields:
            return ""

        group_parts = []
        for entry in group_by_fields:
            field, granularity = self._group_entry(entry)
            if granularity:
                group_parts.append(self._bucket_expr(field, granularity))
            else:
                group_parts.append(f"(data->>'{field}')")
        return f"GROUP BY {', '.join(group_parts)}"

    def _compile_order_by_clause(self, sorts: List, has_metrics: bool, bucket_map: Dict[str, str] = None) -> str:
        """
        Generate ORDER BY clause.
        
        Handles sorting by:
        1. Regular schema fields (with proper type casting for numbers)
        2. Computed metric aliases
        3. Date-bucketed group fields (uses the bucket expression)

        Args:
            sorts: List of sort objects from the query plan
            has_metrics: Whether the query includes metrics (affects field reference)
            bucket_map: Optional mapping of field name -> granularity for bucketed groups

        Returns:
            Complete ORDER BY clause SQL string (including 'ORDER BY' keyword)
        """
        if not sorts:
            return ""
            
        order_parts = []
        metric_aliases = set()  # Track metric aliases if needed
        
        for sort in sorts:
            field_name = sort.field
            direction = sort.direction.upper()

            if bucket_map and field_name in bucket_map:
                order_parts.append(f"{self._bucket_expr(field_name, bucket_map[field_name])} {direction}")
                continue

            # Check if sorting by a metric alias (no JSONB extraction needed)
            if has_metrics and field_name not in self.schema:
                order_parts.append(f"{field_name} {direction}")
            else:
                # Sorting by schema field - determine if numeric casting needed
                field_type = self.schema.get(field_name, {}).get("type", "string")
                if field_type == "number":
                    order_parts.append(f"(data->>'{field_name}')::numeric {direction}")
                else:
                    order_parts.append(f"(data->>'{field_name}') {direction}")
        
        return f"ORDER BY {', '.join(order_parts)}" if order_parts else ""

    def compile(self, plan: QueryPlan, dataset_id: str) -> Tuple[str, Dict[str, Any]]:
        """
        Compile a query plan into a parameterized SQL query.
        
        This method orchestrates the compilation of all query components:
        - SELECT fields and/or aggregation metrics
        - WHERE conditions (filters)
        - GROUP BY clauses
        - ORDER BY clauses
        - Optional LIMIT (plan.limit)
        
        The resulting SQL uses parameter binding to prevent SQL injection.
        
        Args:
            plan: The validated query plan to compile
            dataset_id: ID of the dataset to query
        Returns:
            Tuple of (SQL query string, parameters dictionary)
            
        Example:
            >>> compiler.compile(plan, "dst_123")
            ("SELECT data->>'name' AS name FROM dataset_records WHERE dataset_id = :dataset_id",
             {"dataset_id": "dst_123"})
        """
        logger.info(f"Compiling query plan for dataset {dataset_id}")
        
        params: Dict[str, Any] = {}
        
        # Compile SELECT clause (regular fields)
        select_sql = self._compile_select_clause(plan.select)
        
        # Compile GROUP BY clause (needed before SELECT if grouping)
        group_by_sql = self._compile_group_by_clause(plan.group_by)
        
        # Compile aggregation metrics
        metrics_sql = self._compile_metrics_clause(plan.metrics)
        
        # Build final SELECT portion
        # When metrics are present, PostgreSQL requires every non-aggregated
        # SELECT column to appear in GROUP BY. If the plan groups, SELECT is
        # built from the group fields + metrics (stray select columns are
        # dropped). If it selects fields with metrics but no group_by, infer
        # grouping from the select fields so the query stays valid.
        bucket_map: Dict[str, str] = {}
        for entry in plan.group_by:
            field, granularity = self._group_entry(entry)
            if granularity:
                bucket_map[field] = granularity

        select_parts = []
        if metrics_sql and select_sql and not group_by_sql:
            group_by_sql = self._compile_group_by_clause(plan.select)
            group_select_fields = plan.select
        else:
            group_select_fields = plan.group_by

        if group_by_sql:
            group_select_parts = []
            for entry in group_select_fields:
                field, granularity = self._group_entry(entry)
                if granularity:
                    group_select_parts.append(
                        f"{self._bucket_expr(field, granularity)} AS {self._bucket_alias(field, granularity)}"
                    )
                else:
                    group_select_parts.append(f"(data->>'{field}') AS {field}")
            select_parts.append(", ".join(group_select_parts))
        elif select_sql:
            select_parts.append(select_sql)

        if metrics_sql:
            select_parts.append(metrics_sql)

        final_select = ", ".join(select_parts) if select_parts else "*"
        
        # Compile WHERE clause with filters
        where_sql, _ = self._compile_where_clause(plan.filters, dataset_id, params)
        
        # Order by
        order_by_sql = self._compile_order_by_clause(plan.sorts, bool(plan.metrics), bucket_map)
        
        # Optional row limit
        limit_sql = ""
        if plan.limit is not None:
            limit_sql = "LIMIT :limit"
            params["limit"] = plan.limit
        
        # Assemble final SQL query
        sql_parts = [
            f"SELECT {final_select}",
            f"FROM {self.TABLE_NAME}",
            f"WHERE {where_sql}",
        ]
        
        if group_by_sql:
            sql_parts.append(group_by_sql)
        if order_by_sql:
            sql_parts.append(order_by_sql)
        if limit_sql:
            sql_parts.append(limit_sql)
        
        final_sql = " ".join(sql_parts)
        
        # Normalize whitespace
        final_sql = " ".join(final_sql.split())
        
        logger.debug(f"Compiled SQL: {final_sql}")
        logger.debug(f"Parameters: {params}")
        
        return final_sql, params
    
    def _format_decimal_value(self, value: Decimal, precision: int = 2) -> str:
        """
        Format a Decimal value to a string with specified precision.
        
        Uses ROUND_HALF_UP rounding mode for consistent behavior.
        
        Args:
            value: Decimal value to format
            precision: Number of decimal places (default: 2)
            
        Returns:
            String representation of the rounded decimal
        """
        quantize_str = f"0.{'0' * precision}"
        return str(value.quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP))

    def _process_query_result(self, raw_result: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Process raw database query results.
        
        Converts Decimal values to properly formatted strings to ensure
        JSON serialization compatibility and consistent decimal representation.
        
        Args:
            raw_result: List of row dictionaries from database
            
        Returns:
            Processed list with Decimals converted to strings
        """
        processed_data = []
        
        for row in raw_result:
            processed_row = {}
            for key, value in row.items():
                if isinstance(value, Decimal):
                    # Convert Decimal to formatted string
                    processed_row[key] = self._format_decimal_value(value)
                elif isinstance(value, datetime):
                    processed_row[key] = value.isoformat()
                else:
                    processed_row[key] = value
            processed_data.append(processed_row)
        
        return processed_data
    
    def extract_data_from_db(self, sql_queries: List[Tuple[str, Dict[str, Any]]]) -> List[List[Dict[str, Any]]]:
        """
        Execute SQL queries and extract results from the database.
        
        Handles multiple queries in batch, executes them safely with parameter binding,
        and processes the results for consistent formatting.
        
        Args:
            sql_queries: List of (SQL string, parameters dict) tuples to execute
            
        Returns:
            List of result sets, where each result set is a list of row dictionaries
            
        Raises:
            Exception: If database query execution fails
        """
        all_results = []
        
        for sql_query, params in sql_queries:
            try:
                logger.debug(f"Executing query: {sql_query}")
                logger.debug(f"With parameters: {params}")
                
                # Execute query with parameter binding
                with Session(get_engine()) as session:
                    compiled_query = text(sql_query)
                    result = session.execute(compiled_query.bindparams(**params)).mappings().all()
                
                # Convert result rows to dictionaries
                raw_data = [dict(row) for row in result]
                
                # Process results (e.g., format Decimals)
                processed_data = self._process_query_result(raw_data)
                
                all_results.append(processed_data)
                
                logger.info(f"Query executed successfully, returned {len(processed_data)} rows")
                
            except Exception as e:
                logger.error(f"Database query execution failed: {str(e)}")
                logger.error(f"Query: {sql_query}")
                logger.error(f"Parameters: {params}")
                raise
        
        return all_results

    def execute_dsl(self, dsl_definition, dataset_id: str) -> List[List[Dict[str, Any]]]:
        """
        Execute a complete DSL query plan from validation to result extraction.
        
        This is the main entry point for query execution. It orchestrates:
        1. Validation of the query plan against the dataset schema
        2. Compilation of the plan into SQL
        3. Execution and result extraction
        
        Args:
            dsl_definition: A ``DslDefinitionSchema`` instance or a compatible
                raw dict (validated into the schema internally).
            dataset_id: ID of the dataset to query
        Returns:
            List of result sets (currently single result, but structured for future multi-query support)
            
        Raises:
            ValueError: If validation fails
            Exception: If compilation or execution fails
            
        Example:
            >>> results = compiler.execute_dsl({"select": ["name", "age"]}, "dst_123")
            >>> print(results[0])  # First (and typically only) result set
        """
        logger.info(f"Executing DSL query plan for dataset {dataset_id}")
        
        try:
            if isinstance(dsl_definition, QueryPlan):
                plan = dsl_definition
            else:
                plan = QueryPlan(**dsl_definition)
            
            # Step 1: Validate query plan against schema
            self.validate_dsl(plan, dataset_id)
            
            logger.info(f"Query plan for dataset {dataset_id}: {plan.model_dump()}")
            
            # Step 2: Compile plan to SQL
            sql_query, params = self.compile(plan, dataset_id)
            
            # Step 3: Execute and extract results
            results = self.extract_data_from_db([(sql_query, params)])
            
            logger.info(f"DSL execution completed successfully for dataset {dataset_id}")
            return results
            
        except Exception as e:
            logger.error(f"DSL execution failed for dataset {dataset_id}: {str(e)}")
            raise


# Global singleton instance for convenience
# This allows importing and using the compiler without instantiation
jsonBcompiler = PostgresJSONBCompiler()