import os

import pandas


def compute(files, parameters):
    path = files["trips"]
    os.chmod(path, 0o600)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("overwritten\n")
    return pandas.DataFrame({"path": [path], "name": [os.path.basename(path)]})
