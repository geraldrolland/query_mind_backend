"""Abstract base class for DSL query compilers.

Defines the interface that all concrete query compilers must implement.
Currently the only production implementation is
:class:`~insightly_dataset_service.dsl_compiler.postgresjsonBcompiler.PostgresJSONBCompiler`
which targets PostgreSQL JSONB storage.
"""

from abc import ABC, abstractmethod
from typing import Tuple, Dict, Any
from app.schemas.query import DslDefinitionSchema as QueryPlan


class BaseQueryCompiler(ABC):
    """Contract for DSL-to-SQL compilers.

    Subclasses must implement :meth:`compile` which translates a
    :class:`~insightly_dataset_service.schema.DslDefinitionSchema` into a
    parameterized SQL string and a params dict safe for use with
    SQLAlchemy's :func:`sqlalchemy.text`.
    """

    @abstractmethod
    def compile(self, plan: QueryPlan) -> Tuple[str, Dict[str, Any]]:
        """
        Returns:
            sql: str
            params: dict (for safe parameter binding)
        """
        pass