import logging
from typing import Any, Callable, List, Optional, Union

import pandas as pd
from fugue._utils.io import load_df, save_df
from fugue.collections.partition import (
    EMPTY_PARTITION_SPEC,
    PartitionCursor,
    PartitionSpec,
)
from fugue.dataframe import (
    DataFrame,
    DataFrames,
    LocalBoundedDataFrame,
    LocalDataFrame,
    PandasDataFrame,
    to_local_bounded_df,
)
from fugue.dataframe.utils import get_join_schemas, to_local_df
from fugue.execution.execution_engine import (
    _DEFAULT_JOIN_KEYS,
    ExecutionEngine,
    SQLEngine,
)
from qpd_pandas.engine import PandasUtils
from sqlalchemy import create_engine
from triad.collections import Schema
from triad.collections.dict import ParamDict
from triad.collections.fs import FileSystem
from triad.utils.assertion import assert_or_throw


class SqliteEngine(SQLEngine):
    """Sqlite execution implementation.

    :param execution_engine: the execution engine this sql engine will run on
    """

    def __init__(self, execution_engine: ExecutionEngine):
        super().__init__(execution_engine)

    def select(self, dfs: DataFrames, statement: str) -> DataFrame:
        sql_engine = create_engine("sqlite:///:memory:")
        for k, v in dfs.items():
            v.as_pandas().to_sql(k, sql_engine, if_exists="replace", index=False)
        df = pd.read_sql_query(statement, sql_engine)
        return PandasDataFrame(df)


class NativeExecutionEngine(ExecutionEngine):
    """The execution engine based on native python and pandas. This execution engine
    is mainly for prototyping and unit tests.

    Please read |ExecutionEngineTutorial| to understand this important Fugue concept

    :param conf: |ParamsLikeObject|, read |FugueConfig| to learn Fugue specific options
    """

    def __init__(self, conf: Any = None):
        super().__init__(conf)
        self._fs = FileSystem()
        self._log = logging.getLogger()
        self._default_sql_engine = SqliteEngine(self)

    def __repr__(self) -> str:
        return "NativeExecutionEngine"

    @property
    def log(self) -> logging.Logger:
        return self._log

    @property
    def fs(self) -> FileSystem:
        return self._fs

    @property
    def default_sql_engine(self) -> SQLEngine:
        return self._default_sql_engine

    @property
    def pl_utils(self) -> PandasUtils:
        """Pandas-like dataframe utils"""
        return PandasUtils()

    def stop(self) -> None:  # pragma: no cover
        return

    def to_df(
        self, df: Any, schema: Any = None, metadata: Any = None
    ) -> LocalBoundedDataFrame:
        return to_local_bounded_df(df, schema, metadata)

    def repartition(
        self, df: DataFrame, partition_spec: PartitionSpec
    ) -> DataFrame:  # pragma: no cover
        self.log.warning(f"{self} doesn't respect repartition")
        return df

    def map(
        self,
        df: DataFrame,
        map_func: Callable[[PartitionCursor, LocalDataFrame], LocalDataFrame],
        output_schema: Any,
        partition_spec: PartitionSpec,
        metadata: Any = None,
        on_init: Optional[Callable[[int, DataFrame], Any]] = None,
    ) -> DataFrame:
        if partition_spec.num_partitions != "0":
            self.log.warning(
                f"{self} doesn't respect num_partitions {partition_spec.num_partitions}"
            )
        cursor = partition_spec.get_cursor(df.schema, 0)
        if on_init is not None:
            on_init(0, df)
        if len(partition_spec.partition_by) == 0:  # no partition
            df = to_local_df(df)
            cursor.set(df.peek_array(), 0, 0)
            output_df = map_func(cursor, df)
            assert_or_throw(
                output_df.schema == output_schema,
                f"map output {output_df.schema} mismatches given {output_schema}",
            )
            output_df._metadata = ParamDict(metadata, deep=True)
            output_df._metadata.set_readonly()
            return self.to_df(output_df)
        presort = partition_spec.presort
        presort_keys = list(presort.keys())
        presort_asc = list(presort.values())
        output_schema = Schema(output_schema)

        def _map(pdf: pd.DataFrame) -> pd.DataFrame:
            if len(presort_keys) > 0:
                pdf = pdf.sort_values(presort_keys, ascending=presort_asc)
            input_df = PandasDataFrame(
                pdf.reset_index(drop=True), df.schema, pandas_df_wrapper=True
            )
            cursor.set(input_df.peek_array(), cursor.partition_no + 1, 0)
            output_df = map_func(cursor, input_df)
            return output_df.as_pandas()

        result = self.pl_utils.safe_groupby_apply(
            df.as_pandas(), partition_spec.partition_by, _map
        )
        return PandasDataFrame(result, output_schema, metadata)

    def broadcast(self, df: DataFrame) -> DataFrame:
        return self.to_df(df)

    def persist(self, df: DataFrame, level: Any = None) -> DataFrame:
        return self.to_df(df)

    def join(
        self,
        df1: DataFrame,
        df2: DataFrame,
        how: str,
        on: List[str] = _DEFAULT_JOIN_KEYS,
        metadata: Any = None,
    ) -> DataFrame:
        key_schema, output_schema = get_join_schemas(df1, df2, how=how, on=on)
        d = self.pl_utils.join(
            df1.as_pandas(), df2.as_pandas(), join_type=how, on=key_schema.names
        )
        return PandasDataFrame(d.reset_index(drop=True), output_schema, metadata)

    def union(
        self,
        df1: DataFrame,
        df2: DataFrame,
        distinct: bool = True,
        metadata: Any = None,
    ) -> DataFrame:
        assert_or_throw(
            df1.schema == df2.schema, ValueError(f"{df1.schema} != {df2.schema}")
        )
        d = self.pl_utils.union(df1.as_pandas(), df2.as_pandas(), unique=distinct)
        return PandasDataFrame(d.reset_index(drop=True), df1.schema, metadata)

    def subtract(
        self,
        df1: DataFrame,
        df2: DataFrame,
        distinct: bool = True,
        metadata: Any = None,
    ) -> DataFrame:
        assert_or_throw(
            distinct, NotImplementedError("EXCEPT ALL for NativeExecutionEngine")
        )
        assert_or_throw(
            df1.schema == df2.schema, ValueError(f"{df1.schema} != {df2.schema}")
        )
        d = self.pl_utils.except_df(df1.as_pandas(), df2.as_pandas(), unique=distinct)
        return PandasDataFrame(d.reset_index(drop=True), df1.schema, metadata)

    def intersect(
        self,
        df1: DataFrame,
        df2: DataFrame,
        distinct: bool = True,
        metadata: Any = None,
    ) -> DataFrame:
        assert_or_throw(
            distinct, NotImplementedError("INTERSECT ALL for NativeExecutionEngine")
        )
        assert_or_throw(
            df1.schema == df2.schema, ValueError(f"{df1.schema} != {df2.schema}")
        )
        d = self.pl_utils.intersect(df1.as_pandas(), df2.as_pandas(), unique=distinct)
        return PandasDataFrame(d.reset_index(drop=True), df1.schema, metadata)

    def distinct(
        self,
        df: DataFrame,
        metadata: Any = None,
    ) -> DataFrame:
        d = self.pl_utils.drop_duplicates(df.as_pandas())
        return PandasDataFrame(d.reset_index(drop=True), df.schema, metadata)

    def load_df(
        self,
        path: Union[str, List[str]],
        format_hint: Any = None,
        columns: Any = None,
        **kwargs: Any,
    ) -> LocalBoundedDataFrame:
        return self.to_df(
            load_df(
                path, format_hint=format_hint, columns=columns, fs=self.fs, **kwargs
            )
        )

    def save_df(
        self,
        df: DataFrame,
        path: str,
        format_hint: Any = None,
        mode: str = "overwrite",
        partition_spec: PartitionSpec = EMPTY_PARTITION_SPEC,
        force_single: bool = False,
        **kwargs: Any,
    ) -> None:
        if not partition_spec.empty:
            self.log.warning(  # pragma: no cover
                f"partition_spec is not respected in {self}.save_df"
            )
        df = self.to_df(df)
        save_df(df, path, format_hint=format_hint, mode=mode, fs=self.fs, **kwargs)
