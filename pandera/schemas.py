"""Core pandera schema class definitions."""
# pylint: disable=too-many-lines

import copy
import json
import warnings
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import pandas as pd
from packaging import version

from . import constants, dtypes, errors
from .checks import Check
from .dtypes import PandasDtype, PandasExtensionType
from .error_formatters import (
    format_generic_error_message,
    format_vectorized_error_message,
    reshape_failure_cases,
    scalar_failure_case,
)
from .error_handlers import SchemaErrorHandler
from .hypotheses import Hypothesis

N_INDENT_SPACES = 4

CheckList = Optional[
    Union[Union[Check, Hypothesis], List[Union[Check, Hypothesis]]]
]

PandasDtypeInputTypes = Union[str, type, PandasDtype, PandasExtensionType]

if version.parse(pd.__version__).major < 1:  # type: ignore
    # pylint: disable=no-name-in-module
    from pandas.core.dtypes.dtypes import ExtensionDtype, registry

    def is_extension_array_dtype(arr_or_dtype):
        # pylint: disable=missing-function-docstring
        dtype = getattr(arr_or_dtype, "dtype", arr_or_dtype)
        return (
            isinstance(dtype, ExtensionDtype)
            or registry.find(dtype) is not None
        )


else:
    from pandas.api.types import is_extension_array_dtype  # type: ignore


def _inferred_schema_guard(method):
    """
    Invoking a method wrapped with this decorator will set _is_inferred to
    False.
    """

    @wraps(method)
    def _wrapper(schema, *args, **kwargs):
        new_schema = method(schema, *args, **kwargs)
        if new_schema is not None and id(new_schema) != id(schema):
            # if method returns a copy of the schema object,
            # the original schema instance and the copy should be set to
            # not inferred.
            new_schema._is_inferred = False  # pylint: disable=protected-access
            return new_schema
        schema._is_inferred = False  # pylint: disable=protected-access

    return _wrapper


class DataFrameSchema:
    """A light-weight pandas DataFrame validator."""

    def __init__(
        self,
        columns: Dict[Any, Any] = None,
        checks: CheckList = None,
        index=None,
        transformer: Callable = None,
        coerce: bool = False,
        strict=False,
        name: str = None,
    ) -> None:
        """Initialize DataFrameSchema validator.

        :param columns: a dict where keys are column names and values are
            Column objects specifying the datatypes and properties of a
            particular column.
        :type columns: mapping of column names and column schema component.
        :param checks: dataframe-wide checks.
        :param index: specify the datatypes and properties of the index.
        :param transformer: a callable with signature:
            pandas.DataFrame -> pandas.DataFrame. If specified, calling
            `validate` will verify properties of the columns and return the
            transformed dataframe object.
        :param coerce: whether or not to coerce all of the columns on
            validation.
        :param strict: whether or not to accept columns in the dataframe that
            aren't in the DataFrameSchema.
        :param name: name of the schema.

        :raises SchemaInitError: if impossible to build schema from parameters

        :examples:

        >>> import pandera as pa
        >>>
        >>> schema = pa.DataFrameSchema({
        ...     "str_column": pa.Column(pa.String),
        ...     "float_column": pa.Column(pa.Float),
        ...     "int_column": pa.Column(pa.Int),
        ...     "date_column": pa.Column(pa.DateTime),
        ... })

        Use the pandas API to define checks, which takes a function with
        the signature: ``pd.Series -> Union[bool, pd.Series]`` where the
        output series contains boolean values.

        >>> from pandera import Check
        >>>
        >>> schema_withchecks = pa.DataFrameSchema({
        ...     "probability": pa.Column(
        ...         pa.Float, pa.Check(lambda s: (s >= 0) & (s <= 1))),
        ...
        ...     # check that the "category" column contains a few discrete
        ...     # values, and the majority of the entries are dogs.
        ...     "category": pa.Column(
        ...         pa.String, [
        ...             pa.Check(lambda s: s.isin(["dog", "cat", "duck"])),
        ...             pa.Check(lambda s: (s == "dog").mean() > 0.5),
        ...         ]),
        ... })

        See :ref:`here<DataFrameSchemas>` for more usage details.

        """
        if checks is None:
            checks = []
        if isinstance(checks, (Check, Hypothesis)):
            checks = [checks]

        self.columns = {} if columns is None else columns

        if coerce:
            missing_pandas_type = [
                name
                for name, col in self.columns.items()
                if col.pandas_dtype is None
            ]
            if missing_pandas_type:
                raise errors.SchemaInitError(
                    "Must specify dtype in all Columns if coercing "
                    "DataFrameSchema ; columns with missing pandas_type:"
                    + ", ".join(missing_pandas_type)
                )

        if transformer is not None:
            warnings.warn(
                "The `transformers` argument has been deprecated and will no "
                "longer have any effect on validated dataframes. To achieve "
                "the same goal, you can apply the function to the validated "
                "data with `transformer(schema(df))` or "
                "`schema(df).pipe(transformer)`",
                DeprecationWarning,
            )

        self.checks = checks
        self.index = index
        self.strict = strict
        self.name = name
        self._coerce = coerce
        self._validate_schema()
        self._set_column_names()

        # this attribute is not meant to be accessed by users and is explicitly
        # set to True in the case that a schema is created by infer_schema.
        self._IS_INFERRED = False

    @property
    def coerce(self):
        """Whether to coerce series to specified type."""
        return self._coerce

    # the _is_inferred getter and setter methods are not public
    @property
    def _is_inferred(self):
        return self._IS_INFERRED

    @_is_inferred.setter
    def _is_inferred(self, value: bool):
        self._IS_INFERRED = value

    def _validate_schema(self):
        for column_name, column in self.columns.items():
            for check in column.checks:
                if check.groupby is None or callable(check.groupby):
                    continue
                nonexistent_groupby_columns = [
                    c for c in check.groupby if c not in self.columns
                ]
                if nonexistent_groupby_columns:
                    raise errors.SchemaInitError(
                        "groupby argument %s in Check for Column %s not "
                        "specified in the DataFrameSchema."
                        % (nonexistent_groupby_columns, column_name)
                    )

    def _set_column_names(self):
        def _set_column_handler(column, column_name):
            if column.name is not None and column.name != column_name:
                warnings.warn(
                    f"resetting column for {column} to '{column_name}'."
                )
            elif column.name == column_name:
                return column
            return column.set_name(column_name)

        self.columns = {
            column_name: _set_column_handler(column, column_name)
            for column_name, column in self.columns.items()
        }

    @property
    def dtype(self) -> Dict[str, str]:
        """
        A pandas style dtype dict where the keys are column names and values
        are pandas dtype for the column. Excludes columns where regex=True.

        :returns: dictionary of columns and their associated dtypes.
        """
        regex_columns = [
            name for name, col in self.columns.items() if col.regex
        ]
        if regex_columns:
            warnings.warn(
                "Schema has columns specified as regex column names: %s "
                "Use the `get_dtype` to get the datatypes for these "
                "columns." % regex_columns,
                UserWarning,
            )
        return {n: c.dtype for n, c in self.columns.items() if not c.regex}

    def get_dtype(self, dataframe: pd.DataFrame) -> Dict[str, str]:
        """
        Same as the ``dtype`` property, but expands columns where
        ``regex == True`` based on the supplied dataframe.

        :returns: dictionary of columns and their associated dtypes.
        """
        regex_dtype = {}
        for _, column in self.columns.items():
            if column.regex:
                regex_dtype.update(
                    {
                        c: column.dtype
                        for c in column.get_regex_columns(dataframe.columns)
                    }
                )
        return {
            **{n: c.dtype for n, c in self.columns.items() if not c.regex},
            **regex_dtype,
        }

    def validate(
        self,
        check_obj: pd.DataFrame,
        head: Optional[int] = None,
        tail: Optional[int] = None,
        sample: Optional[int] = None,
        random_state: Optional[int] = None,
        lazy: bool = False,
        inplace: bool = False,
    ) -> pd.DataFrame:
        # pylint: disable=too-many-locals,too-many-branches
        """Check if all columns in a dataframe have a column in the Schema.

        :param pd.DataFrame dataframe: the dataframe to be validated.
        :param head: validate the first n rows. Rows overlapping with `tail` or
            `sample` are de-duplicated.
        :param tail: validate the last n rows. Rows overlapping with `head` or
            `sample` are de-duplicated.
        :param sample: validate a random sample of n rows. Rows overlapping
            with `head` or `tail` are de-duplicated.
        :param random_state: random seed for the ``sample`` argument.
        :param lazy: if True, lazily evaluates dataframe against all validation
            checks and raises a ``SchemaErrors``. Otherwise, raise
            ``SchemaError`` as soon as one occurs.
        :param inplace: if True, applies coercion to the object of validation,
            otherwise creates a copy of the data.
        :returns: validated ``DataFrame``

        :raises SchemaError: when ``DataFrame`` violates built-in or custom
            checks.

        :example:

        Calling ``schema.validate`` returns the dataframe.

        >>> import pandas as pd
        >>> import pandera as pa
        >>>
        >>> df = pd.DataFrame({
        ...     "probability": [0.1, 0.4, 0.52, 0.23, 0.8, 0.76],
        ...     "category": ["dog", "dog", "cat", "duck", "dog", "dog"]
        ... })
        >>>
        >>> schema_withchecks = pa.DataFrameSchema({
        ...     "probability": pa.Column(
        ...         pa.Float, pa.Check(lambda s: (s >= 0) & (s <= 1))),
        ...
        ...     # check that the "category" column contains a few discrete
        ...     # values, and the majority of the entries are dogs.
        ...     "category": pa.Column(
        ...         pa.String, [
        ...             pa.Check(lambda s: s.isin(["dog", "cat", "duck"])),
        ...             pa.Check(lambda s: (s == "dog").mean() > 0.5),
        ...         ]),
        ... })
        >>>
        >>> schema_withchecks.validate(df)[["probability", "category"]]
           probability category
        0         0.10      dog
        1         0.40      dog
        2         0.52      cat
        3         0.23     duck
        4         0.80      dog
        5         0.76      dog
        """

        if self._is_inferred:
            warnings.warn(
                "This %s is an inferred schema that hasn't been "
                "modified. It's recommended that you refine the schema "
                "by calling `add_columns`, `remove_columns`, or "
                "`update_columns` before using it to validate data."
                % type(self),
                UserWarning,
            )

        error_handler = SchemaErrorHandler(lazy)

        if not inplace:
            check_obj = check_obj.copy()

        # dataframe strictness check makes sure all columns in the dataframe
        # are specified in the dataframe schema
        if self.strict:

            # expand regex columns
            col_regex_matches = []  # type: ignore
            for colname, col_schema in self.columns.items():
                if col_schema.regex:
                    try:
                        col_regex_matches.extend(
                            col_schema.get_regex_columns(check_obj.columns)
                        )
                    except errors.SchemaError:
                        pass

            expanded_column_names = frozenset(
                [n for n, c in self.columns.items() if not c.regex]
                + col_regex_matches
            )

            for column in check_obj:
                if column not in expanded_column_names:
                    msg = (
                        f"column '{column}' not in DataFrameSchema"
                        f" {self.columns}"
                    )
                    error_handler.collect_error(
                        "column_not_in_schema",
                        errors.SchemaError(
                            self,
                            check_obj,
                            msg,
                            failure_cases=scalar_failure_case(column),
                            check="column_in_schema",
                        ),
                    )

        # column data-type coercion logic
        lazy_exclude_columns = []
        for colname, col_schema in self.columns.items():
            if col_schema.regex:
                try:
                    matched_columns = col_schema.get_regex_columns(
                        check_obj.columns
                    )
                except errors.SchemaError:
                    matched_columns = pd.Index([])

                for matched_colname in matched_columns:
                    if col_schema.coerce or self.coerce:
                        check_obj[matched_colname] = col_schema.coerce_dtype(
                            check_obj[matched_colname]
                        )

            elif colname not in check_obj and col_schema.required:
                if lazy:
                    # exclude columns that are not present in the dataframe
                    # for lazy validation, the error is collected by the
                    # error_handler and should raise a SchemaErrors exception
                    # at the end of the `validate` method.
                    lazy_exclude_columns.append(colname)
                msg = (
                    f"column '{colname}' not in dataframe\n{check_obj.head()}"
                )
                error_handler.collect_error(
                    "column_not_in_dataframe",
                    errors.SchemaError(
                        self,
                        check_obj,
                        msg,
                        failure_cases=scalar_failure_case(colname),
                        check="column_in_dataframe",
                    ),
                )

            elif col_schema.coerce or self.coerce:
                check_obj.loc[:, colname] = col_schema.coerce_dtype(
                    check_obj[colname]
                )

        schema_components = [
            col
            for col_name, col in self.columns.items()
            if (col.required or col_name in check_obj)
            and col_name not in lazy_exclude_columns
        ]
        if self.index is not None:
            if self.index.coerce or self.coerce:
                check_obj.index = self.index.coerce_dtype(check_obj.index)
            schema_components.append(self.index)

        df_to_validate = _pandas_obj_to_validate(
            check_obj, head, tail, sample, random_state
        )

        check_results = []
        # schema-component-level checks
        for schema_component in schema_components:
            try:
                check_results.append(
                    isinstance(schema_component(df_to_validate), pd.DataFrame)
                )
            except errors.SchemaError as err:
                error_handler.collect_error("schema_component_check", err)

        # dataframe-level checks
        for check_index, check in enumerate(self.checks):
            try:
                check_results.append(
                    _handle_check_results(
                        self, check_index, check, df_to_validate
                    )
                )
            except errors.SchemaError as err:
                error_handler.collect_error("dataframe_check", err)

        if lazy and error_handler.collected_errors:
            raise errors.SchemaErrors(
                error_handler.collected_errors, check_obj
            )

        assert all(check_results)
        return check_obj

    def __call__(
        self,
        dataframe: pd.DataFrame,
        head: Optional[int] = None,
        tail: Optional[int] = None,
        sample: Optional[int] = None,
        random_state: Optional[int] = None,
        lazy: bool = False,
        inplace: bool = False,
    ):
        """Alias for :func:`DataFrameSchema.validate` method.

        :param pd.DataFrame dataframe: the dataframe to be validated.
        :param head: validate the first n rows. Rows overlapping with `tail` or
            `sample` are de-duplicated.
        :type head: int
        :param tail: validate the last n rows. Rows overlapping with `head` or
            `sample` are de-duplicated.
        :type tail: int
        :param sample: validate a random sample of n rows. Rows overlapping
            with `head` or `tail` are de-duplicated.
        :param random_state: random seed for the ``sample`` argument.
        :param lazy: if True, lazily evaluates dataframe against all validation
            checks and raises a ``SchemaErrors``. Otherwise, raise
            ``SchemaError`` as soon as one occurs.
        :param inplace: if True, applies coercion to the object of validation,
            otherwise creates a copy of the data.
        """
        return self.validate(
            dataframe, head, tail, sample, random_state, lazy, inplace
        )

    def __repr__(self):
        """Represent string for logging."""
        return "%s(columns=%s, index=%s, coerce=%s)" % (
            self.__class__.__name__,
            self.columns,
            self.index,
            self.coerce,
        )

    def __str__(self):
        """Represent string for user inspection."""

        def _format_multiline(json_str, arg):
            return "\n".join(
                "{}{}".format(_indent, line)
                if i != 0
                else "{}{}={}".format(_indent, arg, line)
                for i, line in enumerate(json_str.split("\n"))
            )

        columns = {k: str(v) for k, v in self.columns.items()}
        columns = json.dumps(columns, indent=N_INDENT_SPACES)
        _indent = " " * N_INDENT_SPACES
        columns = _format_multiline(columns, "columns")
        checks = (
            None
            if self.checks is None
            else _format_multiline(
                json.dumps(
                    [str(x) for x in self.checks], indent=N_INDENT_SPACES
                ),
                "checks",
            )
        )
        return (
            "{class_name}(\n"
            "{columns},\n"
            "{checks},\n"
            "{indent}index={index},\n"
            "{indent}coerce={coerce},\n"
            "{indent}strict={strict}\n"
            ")"
        ).format(
            class_name=self.__class__.__name__,
            columns=columns,
            checks=checks,
            index=str(self.index),
            coerce=self.coerce,
            strict=self.strict,
            indent=_indent,
        )

    def __eq__(self, other):
        def _compare_dict(obj):
            return {
                k: v for k, v in obj.__dict__.items() if k != "_IS_INFERRED"
            }

        # if _compare_dict(self) != _compare_dict(other):
        #     import ipdb; ipdb.set_trace()
        return _compare_dict(self) == _compare_dict(other)

    @_inferred_schema_guard
    def add_columns(
        self, extra_schema_cols: Dict[str, Any]
    ) -> "DataFrameSchema":
        """Create a copy of the :class:`DataFrameSchema` with extra columns.

        :param extra_schema_cols: Additional columns of the format
        :type extra_schema_cols: DataFrameSchema
        :returns: a new :class:`DataFrameSchema` with the extra_schema_cols
            added.
        :example:

        To add columns to the schema, pass a dictionary with column name and
        ``Column`` instance key-value pairs.

        .. testcode:: add_columns_example1

            import pandera as pa

            example_schema = pa.DataFrameSchema(
                {
                    "category" : pa.Column(pa.String),
                    "probability": pa.Column(pa.Float)
                }
            )

            example_schema.add_columns({"even_number": pa.Column(pa.Bool)})

        .. testoutput:: add_columns_example1
            :options: +NORMALIZE_WHITESPACE

            DataFrameSchema(
                columns = {
                    'category': <Schema Column: 'category' type = string>,
                    'probability': <Schema Column: 'probability' type = float>,
                    'even_number': <Schema Column: 'even_number' type = bool>
                },
                index = None, coerce = False)

        .. seealso:: :func:`remove_columns`

        """
        schema_copy = copy.deepcopy(self)
        schema_copy.columns = {
            **schema_copy.columns,
            **DataFrameSchema(extra_schema_cols).columns,
        }
        return schema_copy

    @_inferred_schema_guard
    def remove_columns(self, cols_to_remove: List[str]) -> "DataFrameSchema":
        """Removes columns from a :class:`DataFrameSchema` and returns a new
        copy.

        :param cols_to_remove: Columns to be removed from the
            ``DataFrameSchema``
        :type cols_to_remove: List
        :returns: a new :class:`DataFrameSchema` without the cols_to_remove
        :raises: :class:`~pandera.errors.SchemaInitError`: if column not in
            schema.
        :example:

        To remove a column or set of columns from a schema, pass a list of
        columns to be removed:

        .. testcode:: remove_columns_example1

            import pandera as pa

            example_schema = pa.DataFrameSchema(
                {
                    "category" : pa.Column(pa.String),
                    "probability": pa.Column(pa.Float)
                }
            )

            example_schema.remove_columns(["category"])

        .. testoutput:: remove_columns_example1
            :options: +NORMALIZE_WHITESPACE

            DataFrameSchema(
                columns = {
                    'probability': <Schema Column: 'probability' type=float>},
                index = None, coerce = False)

        .. seealso:: :func:`add_columns`

        """
        schema_copy = copy.deepcopy(self)

        # ensure all specified keys are present in the columns
        not_in_cols: List[str] = [
            x for x in cols_to_remove if x not in schema_copy.columns.keys()
        ]
        if not_in_cols:
            raise errors.SchemaInitError(
                f"Keys {not_in_cols} not found in schema columns!"
            )

        for col in cols_to_remove:
            schema_copy.columns.pop(col)

        return schema_copy

    @_inferred_schema_guard
    def update_column(self, column_name: str, **kwargs) -> "DataFrameSchema":
        """Create copy of a :class:`DataFrameSchema` with
        updated column properties.

        :param column_name:
        :param kwargs: key-word arguments supplied to
            :class:`~pandera.schema_components.Column`
        :returns: a new :class:`DataFrameSchema` with updated column
        :raises: :class:`~pandera.errors.SchemaInitError`: if column not in
            schema or you try to change the name.
        :example:

        Calling ``schema.update_column`` returns the :class:`DataFrameSchema`
        with the updated column.

        .. testcode:: update_column_example1

            import pandera as pa

            example_schema = pa.DataFrameSchema({
                    "category" : pa.Column(pa.String),
                    "probability": pa.Column(pa.Float)
                }
            )

            example_schema.update_column('category', pandas_dtype=pa.Category)

        .. testoutput:: update_column_example1
            :options: +NORMALIZE_WHITESPACE

            DataFrameSchema(
                columns = {
                    'category': <Schema Column: 'category' type=category>,
                    'probability': <Schema Column: 'probability' type=float>},
                index = None, coerce = False)

        .. seealso:: :func:`rename_columns`

        .. warning:: This method will be deprecated; it is
            recommended to use the :func:`update_columns` method instead.

        """
        # check that columns exist in schema

        if "name" in kwargs:
            raise ValueError("cannot update 'name' of the column.")
        if column_name not in self.columns:
            raise ValueError(f"column '{column_name}' not in {self}")
        schema_copy = copy.deepcopy(self)
        column_copy = copy.deepcopy(self.columns[column_name])
        new_column = column_copy.__class__(
            **{**column_copy.properties, **kwargs}
        )
        schema_copy.columns.update({column_name: new_column})
        return schema_copy

    def update_columns(
        self, update_dict: Dict[str, Dict[str, Any]]
    ) -> "DataFrameSchema":
        """
        Create copy of a :class:`DataFrameSchema` with updated column
        properties.

        :param update_dict:
        :return: a new :class:`DataFrameSchema` with updated columns
        :raises: :class:`~pandera.errors.SchemaInitError`: if column not in
            schema or you try to change the name.
        :example:

        Calling ``schema.update_columns`` returns the :class:`DataFrameSchema`
        with the updated columns.

        .. testcode:: update_columns_example1

            import pandera as pa

            example_schema = pa.DataFrameSchema({
                    "category" : pa.Column(pa.String),
                    "probability": pa.Column(pa.Float)
                }
            )

            example_schema.update_columns(
                {
                    "category": {"pandas_dtype":pa.Category}
                }
            )

        .. testoutput:: update_columns_example1
            :options: +NORMALIZE_WHITESPACE

            DataFrameSchema(
                columns = {
                    'category': <Schema Column: 'category' type = category>,
                    'probability': <Schema Column: 'probability' type = float>
                    },
                index = None, coerce = False)

        .. note:: This is the successor to the ``update_column`` method, which
            will be deprecated.


        """

        new_schema = copy.deepcopy(self)

        # ensure all specified keys are present in the columns
        not_in_cols: List[str] = [
            x for x in update_dict.keys() if x not in new_schema.columns.keys()
        ]
        if not_in_cols:
            raise errors.SchemaInitError(
                f"Keys {not_in_cols} not found in schema columns!"
            )

        new_columns: Dict[str, Dict[str, Any]] = {}
        for col in new_schema.columns:
            # check
            if update_dict.get(col):
                if update_dict[col].get("name"):
                    raise errors.SchemaInitError(
                        "cannot update 'name' \
                                             property of the column."
                    )
            original_properties = new_schema.columns[col].properties
            if update_dict.get(col):
                new_properties = copy.deepcopy(original_properties)
                new_properties.update(update_dict[col])
                new_columns[col] = new_schema.columns[col].__class__(
                    **new_properties
                )
            else:
                new_columns[col] = new_schema.columns[col].__class__(
                    **original_properties
                )

        new_schema.columns = new_columns

        return new_schema

    def rename_columns(self, rename_dict: Dict[str, str]) -> "DataFrameSchema":
        """Rename columns using a dictionary of key-value pairs.

        :param rename_dict: dictionary of 'old_name': 'new_name' key-value
            pairs.
        :returns: :class:`DataFrameSchema` (copy of original)
        :raises: :class:`~pandera.errors.SchemaInitError` if column not in the
            schema.
        :example:

        To rename a column or set of columns, pass a dictionary of old column
        names and new column names, similar to the pandas DataFrame method.

        .. testcode:: rename_columns_example1

            import pandera as pa

            example_schema = pa.DataFrameSchema({
                    "category" : pa.Column(pa.String),
                    "probability": pa.Column(pa.Float)
                }
            )

            example_schema.rename_columns({
                    "category": "categories",
                    "probability": "probabilities"
                }
            )

        .. testoutput:: rename_columns_example1
            :options: +NORMALIZE_WHITESPACE

            DataFrameSchema(
                columns = {
                    'categories': <Schema Column: 'categories' type = string>,
                    'probabilities': <Schema Column: 'probabilities' type = float>
                },
                index = None, coerce = False)

        .. seealso:: :func:`update_column`

        """
        new_schema = copy.deepcopy(self)

        # ensure all specified keys are present in the columns
        not_in_cols: List[str] = [
            x for x in rename_dict.keys() if x not in new_schema.columns.keys()
        ]
        if not_in_cols:
            raise errors.SchemaInitError(
                f"Keys {not_in_cols} not found in schema columns!"
            )

        # ensure all new keys are not present in the current column names
        already_in_columns: List[str] = [
            x for x in rename_dict.values() if x in new_schema.columns.keys()
        ]
        if already_in_columns:
            raise errors.SchemaInitError(
                f"Keys {already_in_columns} already found in schema columns!"
            )

        # We iterate over the existing columns dict and replace those keys
        # that exist in the rename_dict

        new_columns = {
            (rename_dict[col_name] if col_name in rename_dict else col_name): (
                col_attrs.set_name(rename_dict[col_name])
                if col_name in rename_dict
                else col_attrs
            )
            for col_name, col_attrs in new_schema.columns.items()
        }

        new_schema.columns = new_columns

        return new_schema

    def select_columns(self, columns: List[str]) -> "DataFrameSchema":
        """Select subset of columns in the schema.

        *New in version 0.4.5*

        :param columns: list of column names to select.
        :returns:  :class:`DataFrameSchema` (copy of original) with only
            the selected columns.
        :raises: :class:`~pandera.errors.SchemaInitError` if column not in the
            schema.
        :example:

        To subset a schema by column, and return a new schema:

        .. testcode:: select_columns_example1

            import pandera as pa

            example_schema = pa.DataFrameSchema({
                    "category" : pa.Column(pa.String),
                    "probability": pa.Column(pa.Float)
                }
            )

            example_schema.select_columns(['category'])

        .. testoutput:: select_columns_example1
            :options: +NORMALIZE_WHITESPACE

            DataFrameSchema(
                columns = {
                    'category': <Schema Column: 'category' type = string>
                    },
                index = None, coerce = False)

        .. note:: If an index is present in the schema, it will also be
            included in the new schema.

        """

        new_schema = copy.deepcopy(self)

        # ensure all specified keys are present in the columns
        not_in_cols: List[str] = [
            x for x in columns if x not in new_schema.columns.keys()
        ]
        if not_in_cols:
            raise errors.SchemaInitError(
                f"Keys {not_in_cols} not found in schema columns!"
            )

        new_columns = {
            col_name: column
            for col_name, column in self.columns.items()
            if col_name in columns
        }
        new_schema.columns = new_columns
        return new_schema

    def to_script(self, fp: Union[str, Path] = None) -> "DataFrameSchema":
        """Create DataFrameSchema from yaml file.

        :param path: str, Path to write script
        :returns: dataframe schema.
        """
        # pylint: disable=import-outside-toplevel,cyclic-import
        import pandera.io

        return pandera.io.to_script(self, fp)

    @classmethod
    def from_yaml(cls, yaml_schema) -> "DataFrameSchema":
        """Create DataFrameSchema from yaml file.

        :param yaml_schema: str, Path to yaml schema, or serialized yaml
            string.
        :returns: dataframe schema.
        """
        # pylint: disable=import-outside-toplevel,cyclic-import
        import pandera.io

        return pandera.io.from_yaml(yaml_schema)

    def to_yaml(self, fp: Union[str, Path] = None):
        """Write DataFrameSchema to yaml file.

        :param dataframe_schema: schema to write to file or dump to string.
        :param stream: file stream to write to. If None, dumps to string.
        :returns: yaml string if stream is None, otherwise returns None.
        """
        # pylint: disable=import-outside-toplevel,cyclic-import
        import pandera.io

        return pandera.io.to_yaml(self, fp)

    def set_index(
        self, keys: List[str], drop: bool = True, append: bool = False
    ) -> "DataFrameSchema":
        """
        A method for setting the :class:`Index` of a :class:`DataFrameSchema`,
        via an existing :class:`Column` or list of columns.

        :param keys: list of labels
        :param drop: bool, default True
        :param append: bool, default False
        :return: a new :class:`DataFrameSchema` with specified column(s) in the
            index.
        :raises: :class:`~pandera.errors.SchemaInitError` if column not in the
            schema.
        :examples:

        Just as you would set the index in a ``pandas`` DataFrame from an
        existing column, you can set an index within the schema from an
        existing column in the schema.

        .. testcode:: set_index_example1

            import pandera as pa

            example_schema = pa.DataFrameSchema({
                "category" : pa.Column(pa.String),
                "probability": pa.Column(pa.Float)})

            example_schema.set_index(['category'])

        .. testoutput:: set_index_example1
            :options: +NORMALIZE_WHITESPACE

            DataFrameSchema(
                columns = {
                    'probability': <Schema Column:'probability' type = float>},
                index = <Schema Index: 'category'>,
                coerce = False)

        If you have an existing index in your schema, and you would like to
        append a new column as an index to it (yielding a :class:`Multiindex`),
        just use set_index as you would in pandas.

        .. testcode:: set_index_example2

            example_schema = pa.DataFrameSchema({
                "column1": pa.Column(pa.String),
                "column2": pa.Column(pa.Int)},
                index = Index(name = "column3", pandas_dtype = pa.Int))

            example_schema.set_index(["column2"], append = True)

        .. testoutput:: set_index_example2
            :options: +NORMALIZE_WHITESPACE

            DataFrameSchema(columns = {
                    'column1': <Schema Column: 'column1' type = string>
                },
                index = MultiIndex(columns = {
                        "column3": "<Schema Column: 'column3' type = int>",
                        "column2": "<Schema Column: 'column2' type = int64>"
                        },
                    checks = [], index = None, coerce = False, strict = False),
                coerce = False)

        .. seealso:: :func:`reset_index`

        """
        # pylint: disable=import-outside-toplevel,cyclic-import
        from pandera.schema_components import Index, MultiIndex

        new_schema = copy.deepcopy(self)

        keys_temp: List = (
            list(set(keys)) if not isinstance(keys, list) else keys
        )

        # ensure all specified keys are present in the columns
        not_in_cols: List[str] = [
            x for x in keys_temp if x not in new_schema.columns.keys()
        ]
        if not_in_cols:
            raise errors.SchemaInitError(
                f"Keys {not_in_cols} not found in schema columns!"
            )

        # if there is already an index, append or replace according to
        # parameters
        ind_list: List = (
            []
            if new_schema.index is None or not append
            else list(new_schema.index.columns.values())
            if isinstance(new_schema.index, MultiIndex) and append
            else [new_schema.index]
        )

        for col in keys_temp:
            ind_list.append(
                Index(
                    pandas_dtype=new_schema.columns[col].pandas_dtype,
                    name=col,
                    checks=new_schema.columns[col].checks,
                    nullable=new_schema.columns[col].nullable,
                    allow_duplicates=new_schema.columns[col].allow_duplicates,
                    coerce=new_schema.columns[col].coerce,
                )
            )

        new_schema.index = (
            ind_list[0] if len(ind_list) == 1 else MultiIndex(ind_list)
        )

        # if drop is True as defaulted, drop the columns moved into the index
        if drop:
            new_schema = new_schema.remove_columns(keys_temp)

        return new_schema

    def reset_index(
        self, level: List[str] = None, drop: bool = False
    ) -> "DataFrameSchema":
        """
        A method for resetting the :class:`Index` of a :class:`DataFrameSchema`

        :param level: list of labels
        :param drop: bool, default True
        :return: a new :class:`DataFrameSchema` with specified column(s) in the
            index.
        :raises: :class:`~pandera.errors.SchemaInitError` if no index set in
            schema.
        :examples:

        Similar to the ``pandas`` reset_index method on a pandas DataFrame,
        this method can be used to to fully or partially reset indices of a
        schema.

        To remove the entire index from the schema, just call the reset_index
        method with default parameters.


        .. testcode:: reset_index_example1

            import pandera as pa

            example_schema = pa.DataFrameSchema({
                "probability" : pa.Column(pa.Float)},
                index = pa.Index(name = "unique_id",
                pandas_dtype = pa.Int))

            example_schema.reset_index()


        .. testoutput:: reset_index_example1
            :options: +NORMALIZE_WHITESPACE

            DataFrameSchema(columns={
                'probability': <Schema Column: 'probability' type = float>,
                'unique_id': <Schema Column: 'unique_id' type = int64>},
                index = None, coerce = False)

        This reclassifies an index (or indices) as a column (or columns).

        Similarly, to partially alter the index, pass the name of the column
        you would like to be removed to the ``level`` parameter, and you may
        also decide whether to drop the levels with the ``drop`` parameter.


        .. testcode:: reset_index_example2

            example_schema = pa.DataFrameSchema({
                "category" : pa.Column(pa.String)},
                index = pa.MultiIndex([
                    pa.Index(name = "unique_id1", pandas_dtype = pa.Int),
                    pa.Index(name = "unique_id2", pandas_dtype = pa.String)
                    ]
                )
            )
            example_schema.reset_index(level = ["unique_id1"])

        .. testoutput:: reset_index_example2
            :options: +NORMALIZE_WHITESPACE

            DataFrameSchema(columns = {
                'category': <Schema Column: 'category' type = string>,
                'unique_id1': <Schema Column: 'unique_id1' type = int64>
                },
                index=<Schema Index: 'unique_id2'>, coerce = False)

        .. seealso:: :func:`set_index`

        """
        # pylint: disable=import-outside-toplevel,cyclic-import
        from pandera.schema_components import Column, Index, MultiIndex

        new_schema = copy.deepcopy(self)

        if new_schema.index is None:
            raise errors.SchemaInitError(
                "There is currently no index set for this schema."
            )

        # ensure no duplicates
        level_temp: Union[List[Any], List[str]] = (
            list(set(level)) if level is not None else []
        )

        # ensure all specified keys are present in the index
        level_not_in_index: Union[List[Any], List[str], None] = (
            [
                x
                for x in level_temp
                if x not in list(new_schema.index.columns.keys())
            ]
            if isinstance(new_schema.index, MultiIndex) and level_temp
            else []
            if isinstance(new_schema.index, Index)
            and (level_temp == [new_schema.index.name])
            else level_temp
        )
        if level_not_in_index:
            raise errors.SchemaInitError(
                f"Keys {level_not_in_index} not found in schema columns!"
            )

        new_index = (
            None
            if (level_temp == []) or isinstance(new_schema.index, Index)
            else new_schema.index.remove_columns(level_temp)
        )
        new_index = (
            new_index
            if new_index is None
            else Index(
                pandas_dtype=new_index.columns[
                    list(new_index.columns)[0]
                ].pandas_dtype,
                checks=new_index.columns[list(new_index.columns)[0]].checks,
                nullable=new_index.columns[
                    list(new_index.columns)[0]
                ].nullable,
                allow_duplicates=new_index.columns[
                    list(new_index.columns)[0]
                ].allow_duplicates,
                coerce=new_index.columns[list(new_index.columns)[0]].coerce,
                name=new_index.columns[list(new_index.columns)[0]].name,
            )
            if (len(list(new_index.columns)) == 1) and (new_index is not None)
            else None
            if (len(list(new_index.columns)) == 0) and (new_index is not None)
            else new_index
        )

        if not drop:
            additional_columns: Dict[str, Any] = (
                {col: new_schema.index.columns.get(col) for col in level_temp}
                if isinstance(new_schema.index, MultiIndex)
                else {new_schema.index.name: new_schema.index}
            )
            new_schema = new_schema.add_columns(
                {
                    k: Column(
                        pandas_dtype=v.dtype,
                        checks=v.checks,
                        nullable=v.nullable,
                        allow_duplicates=v.allow_duplicates,
                        coerce=v.coerce,
                        name=v.name,
                    )
                    for (k, v) in additional_columns.items()
                }
            )

        new_schema.index = new_index

        return new_schema


class SeriesSchemaBase:
    """Base series validator object."""

    def __init__(
        self,
        pandas_dtype: PandasDtypeInputTypes = None,
        checks: CheckList = None,
        nullable: bool = False,
        allow_duplicates: bool = True,
        coerce: bool = False,
        name: str = None,
    ) -> None:
        """Initialize series schema base object.

        :param pandas_dtype: datatype of the column. If a string is specified,
            then assumes one of the valid pandas string values:
            http://pandas.pydata.org/pandas-docs/stable/basics.html#dtypes
        :param checks: If element_wise is True, then callable signature should
            be:

            ``Callable[Any, bool]`` where the ``Any`` input is a scalar element
            in the column. Otherwise, the input is assumed to be a
            pandas.Series object.
        :type checks: callable
        :param nullable: Whether or not column can contain null values.
        :type nullable: bool
        :param allow_duplicates:
        :type allow_duplicates: bool
        """
        if checks is None:
            checks = []
        if isinstance(checks, (Check, Hypothesis)):
            checks = [checks]

        self._pandas_dtype = pandas_dtype
        self._nullable = nullable
        self._allow_duplicates = allow_duplicates
        self._coerce = coerce
        self._checks = checks
        self._name = name

        for check in self.checks:
            if check.groupby is not None and not self._allow_groupby:
                raise errors.SchemaInitError(
                    f"Cannot use groupby checks with type {type(self)}"
                )

        # make sure pandas dtype is valid
        self.dtype  # pylint: disable=pointless-statement

        # this attribute is not meant to be accessed by users and is explicitly
        # set to True in the case that a schema is created by infer_schema.
        self._IS_INFERRED = False

    # the _is_inferred getter and setter methods are not public
    @property
    def _is_inferred(self):
        return self._IS_INFERRED

    @_is_inferred.setter
    def _is_inferred(self, value: bool):
        self._IS_INFERRED = value

    @property
    def checks(self):
        """Return list of checks or hypotheses."""
        if self._checks is None:
            return []
        if isinstance(self._checks, (Check, Hypothesis)):
            return [self._checks]
        return self._checks

    @checks.setter
    def checks(self, checks):
        self._checks = checks

    @_inferred_schema_guard
    def set_checks(self, checks: CheckList):
        """Create a new SeriesSchema with a new set of Checks

        :param checks: checks to set on the new schema
        :returns: a new SeriesSchema with a new set of checks
        """
        schema_copy = copy.deepcopy(self)
        schema_copy.checks = checks
        return schema_copy

    @property
    def nullable(self) -> bool:
        """Whether the series is nullable."""
        return self._nullable

    @property
    def allow_duplicates(self) -> bool:
        """Whether to allow duplicate values."""
        return self._allow_duplicates

    @property
    def coerce(self) -> bool:
        """Whether to coerce series to specified type."""
        return self._coerce

    @property
    def name(self) -> Union[str, None]:
        """Get SeriesSchema name."""
        return self._name

    @property
    def pandas_dtype(
        self,
    ) -> Union[str, dtypes.PandasDtype, dtypes.PandasExtensionType]:
        """Get the pandas dtype"""
        return self._pandas_dtype

    @pandas_dtype.setter
    def pandas_dtype(
        self, value: Union[str, dtypes.PandasDtype, dtypes.PandasExtensionType]
    ) -> None:
        """Set the pandas dtype"""
        self._pandas_dtype = value
        self.dtype  # pylint: disable=pointless-statement

    @property
    def dtype(self) -> Optional[str]:
        """String representation of the dtype."""
        dtype_ = self._pandas_dtype
        if dtype_ is None:
            return dtype_

        if is_extension_array_dtype(dtype_):
            if isinstance(dtype_, type):
                try:
                    # Convert to str here because some pandas dtypes allow
                    # an empty constructor for compatatibility but fail on
                    # str(). e.g: PeriodDtype
                    return str(dtype_())
                except (TypeError, AttributeError) as err:
                    raise TypeError(
                        f"Pandas dtype {dtype_} cannot be instantiated: "
                        f"{err}\n Usage Tip: Use an instance or a string "
                        "representation."
                    ) from err
            return str(dtype_)

        if dtype_ in dtypes.NUMPY_TYPES:
            dtype_ = PandasDtype.from_numpy_type(dtype_)
        elif isinstance(dtype_, str):
            dtype_ = PandasDtype.from_str_alias(dtype_)
        elif isinstance(dtype_, type):
            dtype_ = PandasDtype.from_python_type(dtype_)

        if isinstance(dtype_, dtypes.PandasDtype):
            return dtype_.str_alias
        raise TypeError(
            "type of `pandas_dtype` argument not recognized: %s "
            "Please specify a pandera PandasDtype enum, legal pandas data "
            "type, pandas data type string alias, or numpy data type "
            "string alias" % type(self._pandas_dtype)
        )

    def coerce_dtype(
        self, series_or_index: Union[pd.Series, pd.Index]
    ) -> pd.Series:
        """Coerce type of a pd.Series by type specified in pandas_dtype.

        :param pd.Series series: One-dimensional ndarray with axis labels
            (including time series).
        :returns: ``Series`` with coerced data type
        """
        if self._pandas_dtype is dtypes.PandasDtype.Str:
            # only coerce non-null elements to string
            return series_or_index.where(
                series_or_index.isna(), series_or_index.astype(str)
            )

        try:
            return series_or_index.astype(self.dtype)
        except TypeError as exc:
            msg = f"Error while coercing '{self.name}' to type {self.dtype}"
            raise TypeError(msg) from exc
        except ValueError as exc:
            msg = f"Error while coercing '{self.name}' to type {self.dtype}: {exc}"
            raise errors.SchemaError(self, None, msg) from exc

    @property
    def _allow_groupby(self):
        """Whether the schema or schema component allows groupby operations."""
        raise NotImplementedError(
            "The _allow_groupby property must be implemented by subclasses "
            "of SeriesSchemaBase"
        )

    def validate(
        self,
        check_obj: Union[pd.DataFrame, pd.Series],
        head: Optional[int] = None,
        tail: Optional[int] = None,
        sample: Optional[int] = None,
        random_state: Optional[int] = None,
        lazy: bool = False,
        inplace: bool = False,
    ) -> Union[pd.DataFrame, pd.Series]:
        # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        """Validate a series or specific column in dataframe.

        :check_obj: pandas DataFrame or Series to validate.
        :param head: validate the first n rows. Rows overlapping with `tail` or
            `sample` are de-duplicated.
        :param tail: validate the last n rows. Rows overlapping with `head` or
            `sample` are de-duplicated.
        :param sample: validate a random sample of n rows. Rows overlapping
            with `head` or `tail` are de-duplicated.
        :param random_state: random seed for the ``sample`` argument.
        :param lazy: if True, lazily evaluates dataframe against all validation
            checks and raises a ``SchemaErrors``. Otherwise, raise
            ``SchemaError`` as soon as one occurs.
        :param inplace: if True, applies coercion to the object of validation,
            otherwise creates a copy of the data.
        :returns: validated DataFrame or Series.

        """

        if self._is_inferred:
            warnings.warn(
                "This %s is an inferred schema that hasn't been "
                "modified. It's recommended that you refine the schema "
                "by calling `set_checks` before using it to validate data."
                % type(self),
                UserWarning,
            )

        error_handler = SchemaErrorHandler(lazy)

        check_obj = _pandas_obj_to_validate(
            check_obj, head, tail, sample, random_state
        )

        series = (
            check_obj
            if isinstance(check_obj, pd.Series)
            else check_obj[self.name]
        )

        if not inplace:
            series = series.copy()

        if self.name is not None and series.name != self._name:
            msg = "Expected %s to have name '%s', found '%s'" % (
                type(self),
                self._name,
                series.name,
            )
            raise errors.SchemaError(
                self,
                check_obj,
                msg,
                failure_cases=scalar_failure_case(series.name),
                check=f"column_name('{self._name}')",
            )

        series_dtype = series.dtype
        if self._nullable:
            series_no_nans = series.dropna()
            if self.dtype in dtypes.NUMPY_NONNULLABLE_INT_DTYPES:
                _series = series_no_nans.astype(self.dtype)
                series_dtype = _series.dtype
                if (_series != series_no_nans).any():
                    # in case where dtype is meant to be int, make sure that
                    # casting to int results in equal values.
                    msg = (
                        "after dropping null values, expected values in "
                        "series '%s' to be int, found: %s"
                        % (series.name, set(series))
                    )
                    error_handler.collect_error(
                        "unexpected_nullable_integer_type",
                        errors.SchemaError(
                            self,
                            check_obj,
                            msg,
                            failure_cases=reshape_failure_cases(
                                series_no_nans
                            ),
                            check="nullable_integer",
                        ),
                    )
        else:
            nulls = series.isna()
            if sum(nulls) > 0:
                msg = "non-nullable series '%s' contains null values: %s" % (
                    series.name,
                    series[nulls].head(constants.N_FAILURE_CASES).to_dict(),
                )
                error_handler.collect_error(
                    "series_contains_nulls",
                    errors.SchemaError(
                        self,
                        check_obj,
                        msg,
                        failure_cases=reshape_failure_cases(
                            series[nulls], ignore_na=False
                        ),
                        check="not_nullable",
                    ),
                )

        # Check if the series contains duplicate values
        if not self._allow_duplicates:
            duplicates = series.duplicated()
            if any(duplicates):
                msg = "series '%s' contains duplicate values: %s" % (
                    series.name,
                    series[duplicates]
                    .head(constants.N_FAILURE_CASES)
                    .to_dict(),
                )
                error_handler.collect_error(
                    "series_contains_duplicates",
                    errors.SchemaError(
                        self,
                        check_obj,
                        msg,
                        failure_cases=reshape_failure_cases(
                            series[duplicates]
                        ),
                        check="no_duplicates",
                    ),
                )

        if self.dtype is not None and str(series_dtype) != self.dtype:
            msg = "expected series '%s' to have type %s, got %s" % (
                series.name,
                self.dtype,
                str(series_dtype),
            )
            error_handler.collect_error(
                "wrong_pandas_dtype",
                errors.SchemaError(
                    self,
                    check_obj,
                    msg,
                    failure_cases=scalar_failure_case(str(series_dtype)),
                    check=f"pandas_dtype('{self.dtype}')",
                ),
            )

        if not self.checks:
            return check_obj

        check_results = []
        if isinstance(check_obj, pd.Series):
            check_obj, check_args = series, [None]
        else:
            check_obj = check_obj.loc[series.index.unique()].copy()
            check_args = [self.name]  # type: ignore

        for check_index, check in enumerate(self.checks):
            try:
                check_results.append(
                    _handle_check_results(
                        self, check_index, check, check_obj, *check_args
                    )
                )
            except errors.SchemaError as err:
                error_handler.collect_error("dataframe_check", err)
            except Exception as err:  # pylint: disable=broad-except
                # catch other exceptions that may occur when executing the
                # Check
                err_str = f'{err.__class__.__name__}("{err.args[0]}")'
                msg = f"Error while executing check function: {err_str}"
                error_handler.collect_error(
                    "check_error",
                    errors.SchemaError(
                        self,
                        check_obj,
                        msg,
                        failure_cases=scalar_failure_case(err_str),
                        check=check,
                        check_index=check_index,
                    ),
                    original_exc=err,
                )

        if lazy and error_handler.collected_errors:
            raise errors.SchemaErrors(
                error_handler.collected_errors, check_obj
            )

        assert all(check_results)
        return check_obj

    def __call__(
        self,
        check_obj: Union[pd.DataFrame, pd.Series],
        head: Optional[int] = None,
        tail: Optional[int] = None,
        sample: Optional[int] = None,
        random_state: Optional[int] = None,
        lazy: bool = False,
        inplace: bool = False,
    ) -> Union[pd.DataFrame, pd.Series]:
        """Alias for ``validate`` method."""
        return self.validate(
            check_obj, head, tail, sample, random_state, lazy, inplace
        )

    def __eq__(self, other):
        return self.__dict__ == other.__dict__


class SeriesSchema(SeriesSchemaBase):
    """Series validator."""

    def __init__(
        self,
        pandas_dtype: PandasDtypeInputTypes = None,
        checks: CheckList = None,
        index=None,
        nullable: bool = False,
        allow_duplicates: bool = True,
        coerce: bool = False,
        name: str = None,
    ) -> None:
        """Initialize series schema base object.

        :param pandas_dtype: datatype of the column. If a string is specified,
            then assumes one of the valid pandas string values:
            http://pandas.pydata.org/pandas-docs/stable/basics.html#dtypes
        :param checks: If element_wise is True, then callable signature should
            be:

            ``Callable[Any, bool]`` where the ``Any`` input is a scalar element
            in the column. Otherwise, the input is assumed to be a
            pandas.Series object.
        :type checks: callable
        :param index: specify the datatypes and properties of the index.
        :param nullable: Whether or not column can contain null values.
        :type nullable: bool
        :param allow_duplicates:
        :type allow_duplicates: bool
        """
        super().__init__(
            pandas_dtype, checks, nullable, allow_duplicates, coerce, name
        )
        self.index = index

    @property
    def _allow_groupby(self) -> bool:
        """Whether the schema or schema component allows groupby operations."""
        return False

    def validate(
        self,
        check_obj: pd.Series,
        head: Optional[int] = None,
        tail: Optional[int] = None,
        sample: Optional[int] = None,
        random_state: Optional[int] = None,
        lazy: bool = False,
        inplace: bool = False,
    ) -> pd.Series:
        """Validate a Series object.

        :param check_obj: One-dimensional ndarray with axis labels
            (including time series).
        :param head: validate the first n rows. Rows overlapping with `tail` or
            `sample` are de-duplicated.
        :param tail: validate the last n rows. Rows overlapping with `head` or
            `sample` are de-duplicated.
        :param sample: validate a random sample of n rows. Rows overlapping
            with `head` or `tail` are de-duplicated.
        :param random_state: random seed for the ``sample`` argument.
        :param lazy: if True, lazily evaluates dataframe against all validation
            checks and raises a ``SchemaErrors``. Otherwise, raise
            ``SchemaError`` as soon as one occurs.
        :param inplace: if True, applies coercion to the object of validation,
            otherwise creates a copy of the data.
        :returns: validated Series.

        :raises SchemaError: when ``DataFrame`` violates built-in or custom
            checks.

        :example:

        >>> import pandas as pd
        >>> import pandera as pa
        >>>
        >>> series_schema = pa.SeriesSchema(
        ...     pa.Float, [
        ...         pa.Check(lambda s: s > 0),
        ...         pa.Check(lambda s: s < 1000),
        ...         pa.Check(lambda s: s.mean() > 300),
        ...     ])
        >>> series = pd.Series([1, 100, 800, 900, 999], dtype=float)
        >>> print(series_schema.validate(series))
        0      1.0
        1    100.0
        2    800.0
        3    900.0
        4    999.0
        dtype: float64

        """
        if not isinstance(check_obj, pd.Series):
            raise TypeError(f"expected {pd.Series}, got {type(check_obj)}")

        if self.coerce:
            check_obj = self.coerce_dtype(check_obj)

        if self.index is not None and (self.index.coerce or self.coerce):
            check_obj.index = self.index.coerce_dtype(check_obj.index)

        # validate index
        if self.index:
            self.index(check_obj)

        return super().validate(
            check_obj, head, tail, sample, random_state, lazy
        )

    def __call__(
        self,
        check_obj: pd.Series,
        head: Optional[int] = None,
        tail: Optional[int] = None,
        sample: Optional[int] = None,
        random_state: Optional[int] = None,
        lazy: bool = False,
        inplace: bool = False,
    ) -> pd.Series:
        """Alias for :func:`SeriesSchema.validate` method."""
        return self.validate(check_obj, head, tail, sample, random_state, lazy)

    def __eq__(self, other):
        return self.__dict__ == other.__dict__


def _pandas_obj_to_validate(
    dataframe_or_series: Union[pd.DataFrame, pd.Series],
    head: Optional[int],
    tail: Optional[int],
    sample: Optional[int],
    random_state: Optional[int],
) -> Union[pd.DataFrame, pd.Series]:
    pandas_obj_subsample = []
    if head is not None:
        pandas_obj_subsample.append(dataframe_or_series.head(head))
    if tail is not None:
        pandas_obj_subsample.append(dataframe_or_series.tail(tail))
    if sample is not None:
        pandas_obj_subsample.append(
            dataframe_or_series.sample(sample, random_state=random_state)
        )
    return (
        dataframe_or_series
        if not pandas_obj_subsample
        else pd.concat(pandas_obj_subsample).drop_duplicates()
    )


def _handle_check_results(
    schema: Union[DataFrameSchema, SeriesSchemaBase],
    check_index: int,
    check: Union[Check, Hypothesis],
    check_obj: Union[pd.DataFrame, pd.Series],
    *check_args,
) -> bool:
    """Handle check results, raising SchemaError on check failure.

    :param check_index: index of check in the schema component check list.
    :param check: Check object used to validate pandas object.
    :param check_args: arguments to pass into check object.
    :returns: True if check results pass or check.raise_warning=True, otherwise
        False.
    """
    check_result = check(check_obj, *check_args)
    if not check_result.check_passed:
        if check_result.failure_cases is None:
            # encode scalar False values explicitly
            failure_cases = scalar_failure_case(check_result.check_passed)
            error_msg = format_generic_error_message(
                schema, check, check_index
            )
        else:
            failure_cases = reshape_failure_cases(
                check_result.failure_cases, check.ignore_na
            )
            error_msg = format_vectorized_error_message(
                schema, check, check_index, failure_cases
            )

        # raise a warning without exiting if the check is specified to do so
        if check.raise_warning:
            warnings.warn(error_msg, UserWarning)
            return True
        raise errors.SchemaError(
            schema,
            check_obj,
            error_msg,
            failure_cases=failure_cases,
            check=check,
            check_index=check_index,
        )
    return check_result.check_passed
