"""Walk-forward backtest against a past season.

Trains only on data available before each predicted game (no
lookahead), tracks accuracy / ROI / calibration weekly, and writes a
row to backtest_runs per run so iterations are comparable over time.

TODO: implement once the model (task #3) is defined.
"""
