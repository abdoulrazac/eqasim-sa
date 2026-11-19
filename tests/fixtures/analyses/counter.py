from pathlib import Path

import pandas


def compute(files, parameters):
    counter = Path(parameters["factors"]["counter"])
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    return pandas.DataFrame({"value": [1.5]})
