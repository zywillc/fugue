from typing import Any, Dict, Iterable, List, Optional, Tuple

import dask.dataframe as pd
import pandas
from fugue.dataframe import DataFrame, LocalDataFrame, PandasDataFrame
from fugue.dataframe.dataframe import _input_schema
from triad.collections.schema import Schema
from triad.utils.assertion import assert_arg_not_none, assert_or_throw
from fugue_dask._utils import DASK_UTILS
from fugue_dask._constants import DEFAULT_CONFIG
from fugue.exceptions import FugueDataFrameInitError, FugueDataFrameOperationError


class DaskDataFrame(DataFrame):
    """DataFrame that wraps Dask DataFrame. Please also read
    |DataFrameTutorial| to understand this Fugue concept

    :param df: :class:`dask:dask.dataframe.DataFrame`,
      pandas DataFrame or list or iterable of arrays
    :param schema: |SchemaLikeObject| or :class:`spark:pyspark.sql.types.StructType`,
      defaults to None.
    :param metadata: |ParamsLikeObject|, defaults to None
    :param num_partitions: initial number of partitions for the dask dataframe
      defaults to 0 to get the value from `fugue.dask.dataframe.default.partitions`
    :param type_safe: whether to cast input data to ensure type safe, defaults to True

    :raises FugueDataFrameInitError: if the input is not compatible

    :Notice:

    * For :class:`dask:dask.dataframe.DataFrame`, schema must be None
    """

    def __init__(  # noqa: C901
        self,
        df: Any = None,
        schema: Any = None,
        metadata: Any = None,
        num_partitions: int = 0,
        type_safe=True,
    ):
        try:
            if num_partitions <= 0:
                num_partitions = DEFAULT_CONFIG.get_or_throw(
                    "fugue.dask.dataframe.default.partitions", int
                )
            if df is None:
                schema = _input_schema(schema).assert_not_empty()
                df = []
            if isinstance(df, DaskDataFrame):
                super().__init__(
                    df.schema, df.metadata if metadata is None else metadata
                )
                self._native: pd.DataFrame = df._native
                return
            elif isinstance(df, (pd.DataFrame, pd.Series)):
                if isinstance(df, pd.Series):
                    df = df.to_frame()
                pdf = df
                schema = None if schema is None else _input_schema(schema)
            elif isinstance(df, (pandas.DataFrame, pandas.Series)):
                if isinstance(df, pandas.Series):
                    df = df.to_frame()
                pdf = pd.from_pandas(df, npartitions=num_partitions)
                schema = None if schema is None else _input_schema(schema)
            elif isinstance(df, Iterable):
                schema = _input_schema(schema).assert_not_empty()
                t = PandasDataFrame(df, schema)
                pdf = pd.from_pandas(t.native, npartitions=num_partitions)
                type_safe = False
            else:
                raise ValueError(f"{df} is incompatible with DaskDataFrame")
            pdf, schema = self._apply_schema(pdf, schema, type_safe)
            super().__init__(schema, metadata)
            self._native = pdf
        except Exception as e:
            raise FugueDataFrameInitError(e)

    @property
    def native(self) -> pd.DataFrame:
        """The wrapped Dask DataFrame

        :rtype: :class:`dask:dask.dataframe.DataFrame`
        """
        return self._native

    @property
    def is_local(self) -> bool:
        return False

    def as_local(self) -> LocalDataFrame:
        return PandasDataFrame(self.as_pandas(), self.schema, self.metadata)

    @property
    def is_bounded(self) -> bool:
        return True

    @property
    def empty(self) -> bool:
        return DASK_UTILS.empty(self.native)

    @property
    def num_partitions(self) -> int:
        return self.native.npartitions

    def _drop_cols(self, cols: List[str]) -> DataFrame:
        cols = (self.schema - cols).names
        return self._select_cols(cols)

    def _select_cols(self, cols: List[Any]) -> DataFrame:
        schema = self.schema.extract(cols)
        return DaskDataFrame(self.native[schema.names], schema, type_safe=False)

    def peek_array(self) -> Any:
        self.assert_not_empty()
        return self.as_pandas().iloc[0].values.tolist()

    def persist(self, **kwargs: Any) -> "DaskDataFrame":
        self._native = self.native.persist(**kwargs)
        return self

    def count(self) -> int:
        return self.as_pandas().shape[0]

    def as_pandas(self) -> pandas.DataFrame:
        return self.native.compute().reset_index(drop=True)

    def rename(self, columns: Dict[str, str]) -> "DataFrame":
        try:
            schema = self.schema.rename(columns)
        except Exception as e:
            raise FugueDataFrameOperationError(e)
        df = self.native.rename(columns=columns)
        return DaskDataFrame(df, schema, type_safe=False)

    def as_array(
        self, columns: Optional[List[str]] = None, type_safe: bool = False
    ) -> List[Any]:
        return list(self.as_array_iterable(columns, type_safe=type_safe))

    def as_array_iterable(
        self, columns: Optional[List[str]] = None, type_safe: bool = False
    ) -> Iterable[Any]:
        return DASK_UTILS.as_array_iterable(
            self.native,
            schema=self.schema.pa_schema,
            columns=columns,
            type_safe=type_safe,
        )

    def _apply_schema(
        self, pdf: pd.DataFrame, schema: Optional[Schema], type_safe: bool = True
    ) -> Tuple[pd.DataFrame, Schema]:
        if not type_safe:
            assert_arg_not_none(pdf, "pdf")
            assert_arg_not_none(schema, "schema")
            return pdf, schema
        DASK_UTILS.ensure_compatible(pdf)
        if pdf.columns.dtype == "object":  # pdf has named schema
            pschema = Schema(DASK_UTILS.to_schema(pdf))
            if schema is None or pschema == schema:
                return pdf, pschema.assert_not_empty()
            pdf = pdf[schema.assert_not_empty().names]
        else:  # pdf has no named schema
            schema = _input_schema(schema).assert_not_empty()
            assert_or_throw(
                pdf.shape[1] == len(schema),
                ValueError(f"Pandas datafame column count doesn't match {schema}"),
            )
            pdf.columns = schema.names
        return DASK_UTILS.enforce_type(pdf, schema.pa_schema, null_safe=True), schema
