"""Mean trip distance of one run."""

import pandas


def compute(files, parameters):
    trips = pandas.read_csv(files["trips"])
    return pandas.DataFrame(
        {
            "runId": [parameters["runId"]],
            "seed": [parameters["seed"]],
            "mean_distance": [float(trips["distance"].mean())],
        }
    )
