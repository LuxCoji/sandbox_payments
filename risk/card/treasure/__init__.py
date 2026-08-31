"""Visa's TREASURE architecture (arXiv:2511.19693), vendored unchanged.

`config.py` and `model.py` are copied verbatim from the MCB reimplementation so
the architecture here is provably the one that was measured offline rather than
a re-derivation of it. `train.py` carries the two objectives and the
pretraining phase.

The schema those files describe is generic - static attributes, dynamic
attributes, network signals. What fills it for FinSim lives in
`risk/card/encoding.py`, which is deliberately the only place the mapping is
written down, so training and serving cannot drift apart.
"""
