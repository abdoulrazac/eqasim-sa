import sys

import pandas


def compute(files, parameters):
    return pandas.DataFrame({"entry": [str(entry) for entry in sys.path]})
