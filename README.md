# Signal Desk

Family dashboard for Oakville, Ontario: weather (today and 7 days) with what to wear, Environment Canada
warnings, the GO Lakeshore West schedule, markets, 3-bed homes for rent with a daily price trend,
upcoming events in Oakville, Mississauga and the GTA, and headlines for Oakville, Ontario, Canada,
Egypt, the Gulf, the USA and the world.

- It refreshes itself about every 10 minutes (`loop.sh`, started by `.github/workflows/update.yml`).
- Add or remove topics from the app: Settings → Add or remove a topic. Or edit `config.json`.
- Change the morning briefing time in `config.json` → `alerts.morning_briefing`.
- Phone alerts: install the free ntfy app and subscribe to the topic in `config.json` → `alerts.ntfy_topic`.
