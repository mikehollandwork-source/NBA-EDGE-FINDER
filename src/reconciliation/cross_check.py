"""Cross-checks the same game/stat across sources instead of trusting one.

Any disagreement (e.g. nba_api and balldontlie report different final
scores for the same game_id) gets written to reconciliation_log rather
than silently auto-resolved, so it stays visible and reviewable.

TODO: implement reconcile_games(), reconcile_team_stats(),
reconcile_player_stats(). Decide (with user) what happens when sources
disagree: prefer one source, average, or block the game from feature
generation until resolved.
"""
