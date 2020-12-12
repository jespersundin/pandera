import inspect

import numpy as np

# %%
import pandas as pd

import pandera as pa
from pandera.typing import DataFrame, Series

# %%


def ch(name):
    print(name)

    def inner(f):
        print(f)
        print(inspect.signature(f))

        def inner2(cls, v):
            print("inner")
            print(cls)
            print(v)
            return v

        return f

    return inner


halla = "FEMTON"


class H:
    halla: str = "HALLA"

    @ch(halla)  # detta
    def hej(c, s):
        return "hej"


# %%
