# Keep last-good catalog refreshes

Catalog refreshes are allowed to run repeatedly from scripts or cron, but a failed provider catalog fetch must not shrink or delete the previously deployed APISIX route set. We prefer keeping the last known-good configuration over degrading to a small static model list because route disappearance from transient network/catalog failures looks like gateway routing instability.
