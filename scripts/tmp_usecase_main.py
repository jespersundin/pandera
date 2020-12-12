# %%
from typing import List, NamedTuple

import numpy as np
import pandas as pd

import pandera as pa
from pandera.typing import DataFrame, Series

# %%


class InputSchema(pa.SchemaModel):
    year: Series[int]
    year_latest: Series[int] = pa.Field()
    year_most_recent: Series[int] = pa.Field()  # alias
    ...
    month: Series[int] = pa.Field()
    month_latest: Series[int] = pa.Field()
    month_most_recent: Series[int] = pa.Field()
    day: Series[int] = pa.Field()
    day_latest: Series[int] = pa.Field()
    day_most_recent: Series[int] = pa.Field()


class ChildSchema(InputSchema):
    add1: Series[str]
    add2: Series[str]
    add3: Series[str]
    add4: Series[str]


class InputSchema2(pa.SchemaModel):
    year: Series[int]
    comment: Series[str]


class ComposedSchema(pa.SchemaModel):
    # Maybe if this seems like it can be put to good use, it might still be confusing
    # and seem overly magical and "conventiony". If this is desired, maybe the API should
    # be to inherit from "pa.ComposedSchemaModel"
    sc1: InputSchema
    sc2: InputSchema2

    class Config:
        multi_column = True  #


class OnSameIndex(NamedTuple):
    a1: DataFrame[InputSchema]
    a2: DataFrame[ChildSchema]


def _validate_same_index(dfs: List[pd.DataFrame]):
    pass


# %%


ds = InputSchema.to_schema()
ds.columns["year"]
# %%
cols = [c for c in ds.columns.keys()]
vals = np.random.randint(1, 100, size=(5, len(cols)))
df = pd.DataFrame(data=vals, columns=[c for c in ds.columns.keys()])
# %%
df
# %%
df_val = ds.validate(df)
# %%
df_val is df
# %%
InputSchema.year_most_recent
h = InputSchema
h.year_most_recent

# %%
@pa.check_types
def proc(
    d1: DataFrame[InputSchema], d2: DataFrame[InputSchema]
) -> DataFrame[InputSchema]:
    return pd.concat([d1, d2], axis=0)


@pa.check_types
def proc_child(
    d1: DataFrame[InputSchema], d2: DataFrame[InputSchema]
) -> DataFrame[ChildSchema]:
    d1["year_most_recent"]  # also have to remember the alias
    # would like to only have to use the information provided to me from the function signature
    # and avoid "magic strings"
    # a1 = str(d1[InputSchema.day] + d2[InputSchema.day])
    # a2 = str(d1[InputSchema.day] + d2[InputSchema.day])
    # a3 = str(d1[InputSchema.day] + d2[InputSchema.day])
    # a4 = str(d1[InputSchema.day] + d2[InputSchema.day])
    a1 = a2 = a3 = a4 = "hej"
    df = pd.concat([d1, d2], axis=0)
    # df[ChildSchema.add1] = a1
    # df[ChildSchema.add2] = a2
    # df[ChildSchema.add3] = a3
    # df[ChildSchema.add4] = a4
    df["add1"] = a1
    df["add2"] = a2
    df["add3"] = a3
    df["add4"] = a4
    return df


def proc_nested(df: DataFrame[ComposedSchema]):
    ComposedSchema.sc1.year


# %%

proc(df_val, df)
# %%
dfc = proc_child(df_val, df)

proc_child(df_val, dfc)

# %%

schema = InputSchema.to_schema().to_yaml()

# %%
