# Local GUI Demo Tasks

These tasks are designed to run on a normal local desktop with visible apps. They are practical demos
for inspecting and trying recorded macOS replay workflows. They require the listed
macOS apps; they are not Windows desktop examples. App versions and screen layout
can affect replay. A fresh live run on the release target is still required.

## Task 1: Stocks Amazon 1-Week History

- ID: `local-demo-stocks-amazon-1w-close`
- App: Stocks on macOS
- Artifact: none
- Minimal prep: make sure Stocks is installed. The replay opens it through Spotlight and quits it at the end.
- Prompt:

```text
Search and open the Stocks app, search for Amazon, click into it, switch the chart to 1 week, then close Stocks.
```

- Success note: Stocks switches to the AMZN `1W` / past-week chart and then quits.
- Direct replay option:

```text
Direct replay: Search and open the Stocks app, search for Amazon, click into it, switch the chart to 1 week, then close Stocks.
```

## Task 2: Weather Location Forecast

- ID: `local-demo-weather-location-forecast-close`
- App: Weather on macOS
- Artifact: none
- Minimal prep: make sure Weather is installed. The replay opens it through Spotlight, searches a location, and quits it at the end.
- Prompt:

```text
Open Weather, search for Seattle, show the 10-day forecast, then close Weather.
```

- Success note: Weather opens the top search result for Seattle, shows the forecast view, and then quits.
- Dynamic input note: the packaged memory can adapt the location in prompts like `Open Weather, search for Boston, show the 10-day forecast, then close Weather.` It does not press Add or accept location permission.
- Direct replay option:

```text
Direct replay: Open Weather, search for Seattle, show the 10-day forecast, then close Weather.
```
