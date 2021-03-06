from typing import List, no_type_check, Any

from fugue.collections.partition import PartitionCursor
from fugue.dataframe import (
    ArrayDataFrame,
    DataFrame,
    DataFrames,
    LocalDataFrame,
    to_local_bounded_df,
)
from fugue.exceptions import FugueWorkflowError
from fugue.execution import SQLEngine
from fugue.execution.execution_engine import _generate_comap_empty_dfs
from fugue.extensions.processor import Processor
from fugue.extensions.transformer import CoTransformer, Transformer, _to_transformer
from triad.collections import ParamDict
from triad.collections.schema import Schema
from triad.utils.assertion import assert_or_throw
from triad.utils.convert import to_instance, to_type


class RunTransformer(Processor):
    @no_type_check
    def process(self, dfs: DataFrames) -> DataFrame:
        df = dfs[0]
        tf = _to_transformer(
            self.params.get_or_none("transformer", object),
            self.params.get_or_none("schema", object),
        )
        tf._workflow_conf = self.execution_engine.conf
        tf._params = self.params.get("params", ParamDict())  # type: ignore
        tf._partition_spec = self.partition_spec  # type: ignore
        ie = self.params.get("ignore_errors", [])
        self._ignore_errors = [to_type(x, Exception) for x in ie]

        if isinstance(tf, Transformer):
            return self.transform(df, tf)
        else:
            return self.cotransform(df, tf)

    def transform(self, df: DataFrame, tf: Transformer) -> DataFrame:
        tf._key_schema = self.partition_spec.get_key_schema(df.schema)  # type: ignore
        tf._output_schema = Schema(tf.get_output_schema(df))  # type: ignore
        tr = _TransformerRunner(df, tf, self._ignore_errors)  # type: ignore
        return self.execution_engine.map(
            df=df,
            map_func=tr.run,
            output_schema=tf.output_schema,  # type: ignore
            partition_spec=tf.partition_spec,
            on_init=tr.on_init,
        )

    @no_type_check
    def cotransform(self, df: DataFrame, tf: CoTransformer) -> DataFrame:
        assert_or_throw(
            df.metadata.get("serialized", False), "must use serialized dataframe"
        )
        tf._key_schema = df.schema - list(df.metadata["serialized_cols"].values())
        # TODO: currently, get_output_schema only gets empty dataframes
        empty_dfs = _generate_comap_empty_dfs(
            df.metadata["schemas"], df.metadata.get("serialized_has_name", False)
        )
        tf._output_schema = Schema(tf.get_output_schema(empty_dfs))
        tr = _CoTransformerRunner(df, tf, self._ignore_errors)
        return self.execution_engine.comap(
            df=df,
            map_func=tr.run,
            output_schema=tf.output_schema,
            partition_spec=tf.partition_spec,
            on_init=tr.on_init,
        )


class RunJoin(Processor):
    def process(self, dfs: DataFrames) -> DataFrame:
        if len(dfs) == 1:
            return dfs[0]
        how = self.params.get_or_throw("how", str)
        on = self.params.get("on", [])
        df = dfs[0]
        for i in range(1, len(dfs)):
            df = self.execution_engine.join(df, dfs[i], how=how, on=on)
        return df


class RunSetOperation(Processor):
    def process(self, dfs: DataFrames) -> DataFrame:
        if len(dfs) == 1:
            return dfs[0]
        how = self.params.get_or_throw("how", str)
        func: Any = {
            "union": self.execution_engine.union,
            "subtract": self.execution_engine.subtract,
            "intersect": self.execution_engine.intersect,
        }[how]
        distinct = self.params.get("distinct", True)
        df = dfs[0]
        for i in range(1, len(dfs)):
            df = func(df, dfs[i], distinct=distinct)
        return df


class Distinct(Processor):
    def process(self, dfs: DataFrames) -> DataFrame:
        assert_or_throw(len(dfs) == 1, FugueWorkflowError("not single input"))
        return self.execution_engine.distinct(dfs[0])


class RunSQLSelect(Processor):
    def process(self, dfs: DataFrames) -> DataFrame:
        statement = self.params.get_or_throw("statement", str)
        engine = self.params.get_or_none("sql_engine", object)
        if engine is None:
            engine = self.execution_engine.default_sql_engine
        elif not isinstance(engine, SQLEngine):
            engine = to_instance(engine, SQLEngine, args=[self.execution_engine])
        return engine.select(dfs, statement)


class Zip(Processor):
    def process(self, dfs: DataFrames) -> DataFrame:
        how = self.params.get("how", "inner")
        partition_spec = self.partition_spec
        # TODO: this should also search on workflow conf
        temp_path = self.params.get_or_none("temp_path", str)
        to_file_threshold = self.params.get_or_none("to_file_threshold", object)
        return self.execution_engine.zip_all(
            dfs,
            how=how,
            partition_spec=partition_spec,
            temp_path=temp_path,
            to_file_threshold=to_file_threshold,
        )


class Rename(Processor):
    def process(self, dfs: DataFrames) -> DataFrame:
        assert_or_throw(len(dfs) == 1, FugueWorkflowError("not single input"))
        columns = self.params.get_or_throw("columns", dict)
        return dfs[0].rename(columns)


class DropColumns(Processor):
    def process(self, dfs: DataFrames) -> DataFrame:
        assert_or_throw(len(dfs) == 1, FugueWorkflowError("not single input"))
        if_exists = self.params.get("if_exists", False)
        columns = self.params.get_or_throw("columns", list)
        if if_exists:
            columns = set(columns).intersection(dfs[0].schema.keys())
        return dfs[0].drop(list(columns))


class SelectColumns(Processor):
    def process(self, dfs: DataFrames) -> DataFrame:
        assert_or_throw(len(dfs) == 1, FugueWorkflowError("not single input"))
        columns = self.params.get_or_throw("columns", list)
        return dfs[0][columns]


class _TransformerRunner(object):
    def __init__(
        self, df: DataFrame, transformer: Transformer, ignore_errors: List[type]
    ):
        self.schema = df.schema
        self.metadata = df.metadata
        self.transformer = transformer
        self.ignore_errors = tuple(ignore_errors)

    def run(self, cursor: PartitionCursor, df: LocalDataFrame) -> LocalDataFrame:
        self.transformer._cursor = cursor  # type: ignore
        df._metadata = self.metadata
        if len(self.ignore_errors) == 0:
            return self.transformer.transform(df)
        else:
            try:
                return to_local_bounded_df(self.transformer.transform(df))
            except self.ignore_errors:  # type: ignore
                return ArrayDataFrame([], self.transformer.output_schema)

    def on_init(self, partition_no: int, df: DataFrame) -> None:
        s = self.transformer.partition_spec
        self.transformer._cursor = s.get_cursor(  # type: ignore
            self.schema, partition_no
        )
        df._metadata = self.metadata
        self.transformer.on_init(df)


class _CoTransformerRunner(object):
    def __init__(
        self, df: DataFrame, transformer: CoTransformer, ignore_errors: List[type]
    ):
        self.schema = df.schema
        self.metadata = df.metadata
        self.transformer = transformer
        self.ignore_errors = tuple(ignore_errors)

    def run(self, cursor: PartitionCursor, dfs: DataFrames) -> LocalDataFrame:
        self.transformer._cursor = cursor  # type: ignore
        if len(self.ignore_errors) == 0:
            return self.transformer.transform(dfs)

        else:
            try:
                return to_local_bounded_df(self.transformer.transform(dfs))
            except self.ignore_errors:  # type: ignore
                return ArrayDataFrame([], self.transformer.output_schema)

    def on_init(self, partition_no: int, dfs: DataFrames) -> None:
        s = self.transformer.partition_spec
        self.transformer._cursor = s.get_cursor(  # type: ignore
            self.schema, partition_no
        )
        self.transformer.on_init(dfs)
