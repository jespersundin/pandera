import pandas as pd

import pandera as pa
from pandera.model_components import Field, FieldInfo
from pandera.typing import DataFrame, Index, Series


class Schema(pa.SchemaModel):
    a: Series[int]  # Blir attribute error, pga inget värde
    b: Series[str] = pa.Field()
    c: Series[str] = pa.Field(alias="_cc")
    idx: Index[str]


class SchemaDatetimeTZDtype(pa.SchemaModel):
    col: Series[pd.DatetimeTZDtype]


# print(Schema.__annotations__)
# print(Schema.__fields__)
# print(Schema.__config__)

print("Acutal schema")
print(Schema.to_schema())

print(Schema.a)
print(Schema.b)
print(Schema.c)
print(Schema.idx)

# class Hej:
#     def __get__(self, obj, t=None):
#         return "hej"

#     def __set__(self, obj, v):
#         pass

# class Normal:
#     a: str = "hejsan"
#     b: Series[str] = Hej()
#     idx: Index[str]

# print([ k for (k,v ) in Normal.__dict__.items() if not k.startswith("__")])
