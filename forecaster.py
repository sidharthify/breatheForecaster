#!/usr/bin/env python3

 # breatheForecaster
 # Copyright (c) 2026 The Breathe Open Source Project
 #
 # Permission is hereby granted, free of charge, to any person obtaining a copy
 # of this software and associated documentation files (the "Software"), to deal
 # in the Software without restriction, including without limitation the rights
 # to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 # copies of the Software, and to permit persons to whom the Software is
 # furnished to do so, subject to the following conditions:
 #
 # The above copyright notice and this permission notice shall be included in all
 # copies or substantial portions of the Software.
 #
 # THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 # IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 # FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 # AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 # LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 # OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 # SOFTWARE.
 #

import os
import sys
import csv
import json
import math
import io
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

# ------------------------------------------------------------
# COLORS AND CONSTANTS
# ------------------------------------------------------------

# Colours are switched off when the output is not a terminal, so that piping
# the output into a file or another program does not fill it with escape codes.
if sys.stdout.isatty():
    BLUE = "\033[34m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    GREY = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
else:
    BLUE = ""
    GREEN = ""
    RED = ""
    YELLOW = ""
    GREY = ""
    BOLD = ""
    RESET = ""

INFO = f"{BLUE}forecaster:{RESET}"
SUCCESS = f"{GREEN}SUCCESS:{RESET}"
ERROR = f"{RED}ERROR:{RESET}"
WARN = f"{YELLOW}WARNING:{RESET}"

# The Breathe API. Override with BREATHE_API if you are running one locally.
API_BASE = os.environ.get("BREATHE_API", "https://api.breatheoss.app")

# Open-Meteo needs no key and no account. We only read their forecast, we never
# try to predict the weather ourselves.
WEATHER_API = "https://api.open-meteo.com/v1/forecast"

# India Standard Time. A "day" in this tool always means an IST calendar day,
# because that is the day a person in Jammu or Srinagar actually lives through.
IST = timezone(timedelta(hours=5, minutes=30))

# How many hourly readings a day needs before we trust it. A day built out of
# three night-time readings is not a day, it is a biased sample, because the air
# stops mixing at night and readings run high. We would rather drop it.
MIN_HOURS_IN_A_DAY = 12

# The level is the trailing average of this many days. 14 was measured as the
# best window on the Jammu record. Shorter chases the weather, longer lags the
# season. See the methodology notes in the README.
LEVEL_WINDOW_DAYS = 14

# How far ahead we forecast by default.
DEFAULT_HORIZON_DAYS = 7

# Before we can measure anything we need some history to learn from.
BACKTEST_WARMUP_DAYS = 60

# phi is the fraction of today's wobble that survives into tomorrow. A zone with
# very little data cannot measure its own phi reliably, so we blend whatever it
# measured with this regional value. See shrink_phi() for how the blend works.
POOLED_PHI = 0.60

# How much the regional value counts for, measured in days of local data. With
# 30 days of its own, a zone gets a 50/50 blend. With 208 days it is 87% its own.
POOLED_PHI_STRENGTH = 30

# phi is a correlation, so it cannot sensibly go outside these bounds. A negative
# phi would mean a dirty day predicts a clean one, which we do not believe.
PHI_MINIMUM = 0.0
PHI_MAXIMUM = 0.95

# 1.2816 is the multiplier that turns a standard deviation into an 80% range.
# If you wanted a 95% range instead you would use 1.96.
INTERVAL_MULTIPLIER = 1.2816

# Indian CPCB breakpoints, copied from the Breathe API's aqi_breakpoints.json so
# that this tool and the API always agree. Each row is:
#   [concentration low, concentration high, index low, index high]
CPCB_PM2_5 = [
    [0, 30, 0, 50],
    [31, 60, 51, 100],
    [61, 90, 101, 200],
    [91, 120, 201, 300],
    [121, 250, 301, 400],
    [251, 5000, 401, 500],
]

CPCB_PM10 = [
    [0, 50, 0, 50],
    [51, 100, 51, 100],
    [101, 250, 101, 200],
    [251, 350, 201, 300],
    [351, 430, 301, 400],
    [431, 5000, 401, 500],
]

# The names CPCB gives to each band of the index.
AQI_CATEGORIES = [
    [0, 50, "Good"],
    [51, 100, "Satisfactory"],
    [101, 200, "Moderate"],
    [201, 300, "Poor"],
    [301, 400, "Very Poor"],
    [401, 10000, "Severe"],
]

# Respects XDG_DATA_HOME if it is set, otherwise falls back to ~/.local/share.
# This is where the forecast journal lives, so that `score` has something to
# check yesterday's forecast against.
data_base = os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")
DATA_DIR = Path(data_base) / "breathe-forecaster"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------
# SMALL MATHS HELPERS
# ------------------------------------------------------------

def average(numbers: list) -> float:
    '''
    The ordinary average: add everything up, divide by how many there were.
    Returns None for an empty list rather than crashing, because half the
    functions below can legitimately be handed nothing.
    '''
    if len(numbers) == 0:
        return None
    total = 0.0
    for number in numbers:
        total = total + number
    return total / len(numbers)

def standard_deviation(numbers: list) -> float:
    '''
    Roughly, the typical distance of a value from the average. It is our measure
    of how spread out something is, and it is what turns a single prediction
    into a range.
    Needs at least two numbers, because one number has no spread.
    '''
    if len(numbers) < 2:
        return None
    mean_value = average(numbers)
    total_squared_gap = 0.0
    for number in numbers:
        gap = number - mean_value
        total_squared_gap = total_squared_gap + (gap * gap)
    return math.sqrt(total_squared_gap / (len(numbers) - 1))

def correlation(pairs: list) -> float:
    '''
    Takes a list of (x, y) pairs and returns how strongly they move together,
    as a number between -1 and +1.
      +1 means they move in perfect lockstep
       0 means knowing one tells you nothing about the other
      -1 means they move in perfect opposition
    Returns None if there are too few pairs to mean anything, or if either side
    never varies (you cannot correlate a flat line with anything).
    '''
    if len(pairs) < 10:
        return None

    x_values = []
    y_values = []
    for pair in pairs:
        x_values.append(pair[0])
        y_values.append(pair[1])

    x_mean = average(x_values)
    y_mean = average(y_values)

    top = 0.0
    x_spread = 0.0
    y_spread = 0.0
    for pair in pairs:
        x_gap = pair[0] - x_mean
        y_gap = pair[1] - y_mean
        top = top + (x_gap * y_gap)
        x_spread = x_spread + (x_gap * x_gap)
        y_spread = y_spread + (y_gap * y_gap)

    if x_spread == 0.0:
        return None
    if y_spread == 0.0:
        return None

    return top / math.sqrt(x_spread * y_spread)

def percentile(numbers: list, fraction: float) -> float:
    '''
    Returns the value below which the given fraction of the numbers fall.
    percentile(values, 0.5) is the median.
    '''
    if len(numbers) == 0:
        return None
    ordered = sorted(numbers)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower_index = int(math.floor(position))
    upper_index = lower_index + 1
    if upper_index > len(ordered) - 1:
        upper_index = len(ordered) - 1
    weight = position - lower_index
    lower_value = ordered[lower_index]
    upper_value = ordered[upper_index]
    return lower_value + ((upper_value - lower_value) * weight)

# ------------------------------------------------------------
# TALKING TO THE APIS
# ------------------------------------------------------------

def fetch_url(url: str) -> str:
    '''
    Fetches a URL and returns the body as text.
    Exits with a readable message rather than a stack trace when something goes
    wrong, because the two likely causes (no internet, wrong zone name) are both
    things the person running this can fix.
    '''
    request = urllib.request.Request(url, headers={"User-Agent": "breatheForecaster"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as problem:
        print(f"{ERROR} the server said {problem.code} for {url}")
        if problem.code == 404:
            print(f"{INFO} Is that zone id right? Try: forecaster zones")
        sys.exit(1)
    except urllib.error.URLError as problem:
        print(f"{ERROR} could not reach {url}")
        print(f"{INFO} {problem.reason}")
        sys.exit(1)

def fetch_zones() -> list:
    '''
    Returns the list of zones the API knows about, each one a dict with an id,
    a name, coordinates and which provider it uses.
    '''
    body = fetch_url(f"{API_BASE}/zones")
    payload = json.loads(body)
    return payload.get("zones", [])

def find_zone(zone_id: str) -> dict:
    '''
    Looks up a single zone by its id. Exits with a helpful list if the id is not
    one the API recognises, since a typo here is the most likely mistake.
    '''
    zones = fetch_zones()
    for zone in zones:
        if zone["id"] == zone_id:
            return zone

    print(f"{ERROR} no zone called '{zone_id}'")
    print(f"{INFO} zones with ground sensors:")
    for zone in zones:
        if zone.get("provider") == "airgradient":
            print(f"  {zone['id']}")
    sys.exit(1)

def fetch_history(zone_id: str, time_range: str) -> list:
    '''
    Fetches hourly history for a zone and returns it as a list of readings.
    Each reading is a dict with a unix timestamp and whichever of pm2_5 and
    pm10 were present.

    Note we ask for the zone, not for an individual sensor. The zone series is
    the one the API corrects for sensors joining and leaving; the per sensor
    series are deliberately left raw.
    '''
    path = "/historical-data/{}/{}/1h/pm2.5,pm10".format(
        urllib.parse.quote(zone_id, safe=""),
        urllib.parse.quote(time_range, safe=""),
    )
    body = fetch_url(f"{API_BASE}{path}?format=csv")

    readings = []
    for row in csv.DictReader(io.StringIO(body)):
        timestamp = row.get("ts")
        if timestamp is None:
            continue

        reading = {"ts": int(timestamp)}

        for column in ["pm2_5", "pm10"]:
            raw_value = row.get(column)
            if raw_value is None:
                continue
            if raw_value == "":
                continue
            if raw_value == "None":
                continue
            try:
                value = float(raw_value)
            except ValueError:
                continue
            if value > 0:
                reading[column] = value

        if len(reading) > 1:
            readings.append(reading)

    return readings

def fetch_weather(latitude: float, longitude: float, days: int) -> dict:
    '''
    Fetches the daily weather forecast from Open-Meteo, keyed by date.

    We deliberately do not try to forecast the weather ourselves. Open-Meteo
    serves the output of proper physics models run on supercomputers, it is
    free, and no amount of PM2.5 history would let us compete with it.
    '''
    query = urllib.parse.urlencode({
        "latitude": latitude,
        "longitude": longitude,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,weather_code",
        "timezone": "Asia/Kolkata",
        "forecast_days": days + 1,
    })

    try:
        body = fetch_url(f"{WEATHER_API}?{query}")
        payload = json.loads(body)
    except Exception:
        # The weather is a nice extra, not the point of the tool. If it is
        # unavailable we would rather still print the pollution forecast.
        return {}

    daily = payload.get("daily")
    if daily is None:
        return {}

    weather_by_date = {}
    day_strings = daily.get("time", [])
    for position in range(len(day_strings)):
        day = date.fromisoformat(day_strings[position])
        weather_by_date[day] = {
            "temp_max": daily["temperature_2m_max"][position],
            "temp_min": daily["temperature_2m_min"][position],
            "rain": daily["precipitation_sum"][position],
            "wind": daily["wind_speed_10m_max"][position],
            "code": daily["weather_code"][position],
        }
    return weather_by_date

def describe_weather(code: int) -> str:
    '''
    Turns a WMO weather code into something a person would say.
    The full table is much longer; these are the groups that matter in J&K.
    '''
    if code == 0:
        return "clear"
    if code in [1, 2, 3]:
        return "cloudy"
    if code in [45, 48]:
        return "fog"
    if code in [51, 53, 55, 56, 57]:
        return "drizzle"
    if code in [61, 63, 65, 66, 67]:
        return "rain"
    if code in [71, 73, 75, 77]:
        return "snow"
    if code in [80, 81, 82]:
        return "showers"
    if code in [85, 86]:
        return "snow showers"
    if code in [95, 96, 99]:
        return "thunderstorm"
    return "unknown"

# ------------------------------------------------------------
# TURNING READINGS INTO DAILY NUMBERS
# ------------------------------------------------------------

def ist_day(timestamp: int) -> date:
    '''
    Which IST calendar day a unix timestamp falls on.
    '''
    return datetime.fromtimestamp(timestamp, IST).date()

def daily_averages(readings: list, column: str) -> dict:
    '''
    Collapses hourly readings into one number per day, keyed by date.

    Days with fewer than MIN_HOURS_IN_A_DAY readings are left out entirely
    rather than being filled in with a guess. A missing day is honest; an
    invented one quietly poisons everything downstream.
    '''
    hours_by_day = {}
    for reading in readings:
        if column not in reading:
            continue
        day = ist_day(reading["ts"])
        if day not in hours_by_day:
            hours_by_day[day] = []
        hours_by_day[day].append(reading[column])

    averages = {}
    for day in hours_by_day:
        hours = hours_by_day[day]
        if len(hours) < MIN_HOURS_IN_A_DAY:
            continue
        averages[day] = average(hours)
    return averages

def as_day_list(daily: dict) -> list:
    '''
    Turns the date-keyed dict into a straight list covering every day from the
    first to the last, with None wherever a day is missing.

    Having the gaps present as None matters: it keeps "seven days ago" meaning
    seven actual days, not seven positions in a list that skipped a Tuesday.
    '''
    if len(daily) == 0:
        return []

    first_day = min(daily)
    last_day = max(daily)

    days = []
    current_day = first_day
    while current_day <= last_day:
        if current_day in daily:
            days.append([current_day, daily[current_day]])
        else:
            days.append([current_day, None])
        current_day = current_day + timedelta(days=1)
    return days

def to_logs(days: list) -> list:
    '''
    Replaces each concentration with its natural logarithm, keeping None as None.

    We work in logs because pollution is better described by "twice as bad" than
    by "30 units worse". In logs an error means the same thing whether the day
    was clean or filthy, and a forecast can never come out negative.
    '''
    logged = []
    for entry in days:
        day = entry[0]
        value = entry[1]
        if value is None:
            logged.append([day, None])
        else:
            logged.append([day, math.log(value)])
    return logged

# ------------------------------------------------------------
# THE AQI TABLES
# ------------------------------------------------------------

def sub_index(concentration: float, table: list) -> int:
    '''
    Converts a concentration into its AQI sub-index using the CPCB tables.

    Inside a band the relationship is a straight line, so we work out how far
    along the concentration band we are, and move the same fraction along the
    index band:

        index = index_low + (index_high - index_low) * how_far_along

    Anything above the top of the table is capped at 500.
    '''
    if concentration is None:
        return None

    for band in table:
        concentration_low = band[0]
        concentration_high = band[1]
        index_low = band[2]
        index_high = band[3]

        if concentration >= concentration_low and concentration <= concentration_high:
            concentration_span = concentration_high - concentration_low
            if concentration_span == 0:
                return int(round(index_low))
            how_far_along = (concentration - concentration_low) / concentration_span
            index_span = index_high - index_low
            return int(round(index_low + (index_span * how_far_along)))

    return 500

def indian_aqi(pm2_5: float, pm10: float) -> list:
    '''
    Returns [aqi, main_pollutant] the way CPCB defines it.

    The AQI is NOT an average of the pollutants. It is the worst of them, and
    whichever pollutant produced that worst number is the "main pollutant".
    '''
    pm2_5_index = sub_index(pm2_5, CPCB_PM2_5)
    pm10_index = sub_index(pm10, CPCB_PM10)

    if pm2_5_index is None and pm10_index is None:
        return [None, None]
    if pm10_index is None:
        return [pm2_5_index, "pm2.5"]
    if pm2_5_index is None:
        return [pm10_index, "pm10"]

    if pm2_5_index >= pm10_index:
        return [pm2_5_index, "pm2.5"]
    return [pm10_index, "pm10"]

def aqi_category(aqi: int) -> str:
    '''
    The CPCB name for a given AQI value, for example "Moderate".
    '''
    if aqi is None:
        return "unknown"
    for band in AQI_CATEGORIES:
        if aqi >= band[0] and aqi <= band[1]:
            return band[2]
    return "Severe"

def colour_for_aqi(aqi: int) -> str:
    '''
    Picks a terminal colour for an AQI value so that a bad day is visible at a
    glance. Green for fine, yellow for middling, red for bad.
    '''
    if aqi is None:
        return GREY
    if aqi <= 100:
        return GREEN
    if aqi <= 200:
        return YELLOW
    return RED

# ------------------------------------------------------------
# THE FORECAST MODEL
# ------------------------------------------------------------

def level_at(logs: list, position: int, window: int) -> float:
    '''
    The level is the average of the logged values over the last `window` days,
    counting back from `position`.

    This is the slow, seasonal part of the signal: roughly "what is normal for
    this time of year, around here, lately".

    It only ever looks backwards. That matters more than it sounds: a level that
    peeked at future days would make every test below meaningless.
    '''
    first_position = position - window + 1
    if first_position < 0:
        first_position = 0

    values = []
    for index in range(first_position, position + 1):
        if logs[index][1] is not None:
            values.append(logs[index][1])

    if len(values) < 3:
        return None
    return average(values)

def anomalies_up_to(logs: list, position: int, window: int) -> dict:
    '''
    Works out the wobble for every day up to `position`, keyed by list position.

    The wobble is what is left when you subtract the level from the day:

        wobble = today in logs - the level in logs

    Zero means a completely typical day for the time of year. Positive means
    dirtier than it should be, negative cleaner.
    '''
    wobbles = {}
    for index in range(0, position + 1):
        if logs[index][1] is None:
            continue
        level = level_at(logs, index, window)
        if level is None:
            continue
        wobbles[index] = logs[index][1] - level
    return wobbles

def measure_phi(logs: list, position: int, window: int) -> float:
    '''
    Measures phi: the fraction of today's wobble that is still there tomorrow.

    We pair up each day's wobble with the next day's wobble and correlate them.
    A phi of 0.6 means a day that was 10% dirtier than normal is followed by one
    about 6% dirtier than normal, on average.

    Note the wobbles are measured against the SAME window the forecast uses for
    its level. Estimating phi against one window and applying it to another is a
    real mistake that costs accuracy, and this tool used to make it.
    '''
    wobbles = anomalies_up_to(logs, position, window)

    pairs = []
    for index in wobbles:
        next_index = index + 1
        if next_index in wobbles:
            pairs.append([wobbles[index], wobbles[next_index]])

    measured = correlation(pairs)
    if measured is None:
        return [None, 0]
    return [measured, len(pairs)]

def shrink_phi(measured_phi: float, sample_count: int) -> float:
    '''
    Blends a zone's own measured phi with the regional one, weighted by how much
    data the zone actually has.

        phi = (own_days * own_phi + strength * regional_phi) / (own_days + strength)

    Why bother: measuring a correlation from 30 days is very noisy, with an error
    of roughly 1 / sqrt(30), which is about 0.18. That is wide enough to explain
    the entire spread we see between the short-record zones. So a new zone leans
    on the regional value, and as it gathers its own data the formula hands
    control back to it, a little more every day.

    Nobody has to decide when a zone has "enough" data. The arithmetic decides.
    '''
    if measured_phi is None:
        return POOLED_PHI

    weighted_total = (sample_count * measured_phi) + (POOLED_PHI_STRENGTH * POOLED_PHI)
    total_weight = sample_count + POOLED_PHI_STRENGTH
    blended = weighted_total / total_weight

    if blended < PHI_MINIMUM:
        return PHI_MINIMUM
    if blended > PHI_MAXIMUM:
        return PHI_MAXIMUM
    return blended

def predict_from(logs: list, position: int, horizon: int, window: int) -> list:
    '''
    Produces the forecast in logs for 1..horizon days after `position`.

    The whole model is one line:

        forecast = level + (phi ^ h) * (today - level)

    Read it as: start from today's wobble, shrink it a bit for every day that
    passes, and add it back onto the seasonal level.

    Because phi is below 1, phi^h shrinks fast. With phi = 0.6 you keep 60% of
    the wobble tomorrow but only 3% a week out, so by then the forecast has
    quietly become the level. That is not the model giving up, it is the model
    being honest: after about two days, today's reading genuinely has nothing
    left to tell you.
    '''
    level = level_at(logs, position, window)
    if level is None:
        return None
    if logs[position][1] is None:
        return None

    measurement = measure_phi(logs, position, window)
    phi = shrink_phi(measurement[0], measurement[1])

    todays_wobble = logs[position][1] - level

    predictions = []
    for steps_ahead in range(1, horizon + 1):
        surviving_fraction = math.pow(phi, steps_ahead)
        remaining_wobble = todays_wobble * surviving_fraction
        predictions.append(level + remaining_wobble)
    return predictions

# ------------------------------------------------------------
# BACKTESTING
# ------------------------------------------------------------

def walk_forward(logs: list, horizon: int, window: int) -> dict:
    '''
    Tests the model the only honest way: by covering up the future with a hand.

    We walk through the record one day at a time. At each day we fit everything
    using only what was known by then, forecast the next `horizon` days, and
    write down how wrong we were. Then we step forward and do it all again.

    This is the difference between a model that works and a model that looks
    like it works. Anything that lets the model glimpse a future value, even
    indirectly through an average, will produce beautiful numbers here and fall
    apart the day you actually run it.

    Returns a dict of {steps_ahead: [errors in logs]} plus the same for the two
    baselines we have to beat.
    '''
    results = {
        "model": {},
        "persistence": {},
        "level": {},
    }
    for steps_ahead in range(1, horizon + 1):
        results["model"][steps_ahead] = []
        results["persistence"][steps_ahead] = []
        results["level"][steps_ahead] = []

    for position in range(BACKTEST_WARMUP_DAYS, len(logs)):
        if logs[position][1] is None:
            continue

        predictions = predict_from(logs, position, horizon, window)
        if predictions is None:
            continue

        level = level_at(logs, position, window)

        for steps_ahead in range(1, horizon + 1):
            target_position = position + steps_ahead
            if target_position >= len(logs):
                continue
            truth = logs[target_position][1]
            if truth is None:
                continue

            results["model"][steps_ahead].append(truth - predictions[steps_ahead - 1])
            results["persistence"][steps_ahead].append(truth - logs[position][1])
            results["level"][steps_ahead].append(truth - level)

    return results

def residual_spread(logs: list, horizon: int, window: int) -> dict:
    '''
    Works out how wide the forecast ranges should be, one width per lead time.

    Rather than trusting the textbook formula we simply look at how wrong this
    model has actually been at each lead time in the past. That way the range
    quietly absorbs every flaw in the model, including the ones we do not know
    about yet.

    Falls back to a deliberately generous guess when there is not enough history
    to measure. A too-wide range is honest; a too-narrow one is a lie.
    '''
    outcome = walk_forward(logs, horizon, window)

    spreads = {}
    for steps_ahead in range(1, horizon + 1):
        errors = outcome["model"][steps_ahead]
        if len(errors) >= 10:
            spread = standard_deviation(errors)
            if spread is not None:
                spreads[steps_ahead] = spread
                continue
        # 0.45 in logs is roughly "could be 57% higher or 36% lower".
        spreads[steps_ahead] = 0.45
    return spreads

def mean_absolute_error(errors: list, in_logs: bool) -> float:
    '''
    The average size of our mistakes, ignoring whether we were high or low.

    When in_logs is True the errors are still logged, so we convert each one
    back into a percentage before averaging. That gives "typically 20% out",
    which travels between clean and filthy days far better than "typically
    8 micrograms out".
    '''
    if len(errors) == 0:
        return None

    total = 0.0
    for error in errors:
        if in_logs:
            total = total + abs(math.expm1(error))
        else:
            total = total + abs(error)
    return total / len(errors)

def skill_score(model_error: float, baseline_error: float) -> float:
    '''
    How much better the model is than a baseline, as a percentage of the error
    it removed.

        skill = 1 - (model error / baseline error)

    Positive is good. Zero means the model is no better than the dumb option and
    should be deleted. This single number decides whether any change to this
    tool is an improvement, no matter how clever the change felt.
    '''
    if baseline_error is None:
        return None
    if baseline_error == 0:
        return None
    if model_error is None:
        return None
    return 1.0 - (model_error / baseline_error)

# ------------------------------------------------------------
# THE JOURNAL
# ------------------------------------------------------------

def journal_path(zone_id: str) -> Path:
    '''
    Where a zone's recorded forecasts live. One JSON object per line, appended
    forever, so that it is easy to read back and hard to corrupt.
    '''
    return DATA_DIR / f"{zone_id}.jsonl"

def write_journal(zone_id: str, issued_on: date, rows: list) -> Path:
    '''
    Appends today's forecast to the journal so that `score` can come back later
    and check it.

    This is the part that cannot be backfilled. Every other piece of this tool
    can be rebuilt from history at any time, but a record of what we predicted
    before we knew the answer only accumulates at one day per day. Start it
    early even if the model is bad.
    '''
    path = journal_path(zone_id)
    entry = {
        "zone_id": zone_id,
        "issued_on": issued_on.isoformat(),
        "issued_at": datetime.now(IST).isoformat(),
        "days": [],
    }
    for row in rows:
        entry["days"].append({
            "day": row["day"].isoformat(),
            "steps_ahead": row["steps_ahead"],
            "pm2_5": row["pm2_5"],
            "pm2_5_low": row["pm2_5_low"],
            "pm2_5_high": row["pm2_5_high"],
            "pm10": row["pm10"],
            "aqi": row["aqi"],
            "category": row["category"],
        })

    with open(path, "a") as handle:
        handle.write(json.dumps(entry) + "\n")
    return path

def read_journal(zone_id: str) -> list:
    '''
    Reads every forecast we have recorded for a zone.
    Skips any line that will not parse rather than giving up on the whole file,
    since a half-written line from an interrupted run should not cost you the
    entire history.
    '''
    path = journal_path(zone_id)
    if not path.exists():
        return []

    entries = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if line == "":
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries

# ------------------------------------------------------------
# COMMANDS
# ------------------------------------------------------------

def build_forecast(zone_id: str, horizon: int) -> list:
    '''
    Does the actual work behind both `forecast` and `record`: fetches the
    history, fits the model, and returns one row per forecast day with the
    concentrations, the AQI, the category and the 80% range.
    '''
    readings = fetch_history(zone_id, "1y")
    if len(readings) == 0:
        print(f"{ERROR} no history came back for {zone_id}")
        sys.exit(1)

    pm2_5_daily = daily_averages(readings, "pm2_5")
    pm10_daily = daily_averages(readings, "pm10")

    if len(pm2_5_daily) < 20:
        print(f"{ERROR} only {len(pm2_5_daily)} usable days for {zone_id}")
        print(f"{INFO} a day needs {MIN_HOURS_IN_A_DAY} hourly readings to count")
        sys.exit(1)

    pm2_5_logs = to_logs(as_day_list(pm2_5_daily))
    pm10_logs = to_logs(as_day_list(pm10_daily))

    last_position = len(pm2_5_logs) - 1
    while last_position >= 0 and pm2_5_logs[last_position][1] is None:
        last_position = last_position - 1

    if last_position < 0:
        print(f"{ERROR} every recent day for {zone_id} is missing data")
        sys.exit(1)

    pm2_5_predictions = predict_from(pm2_5_logs, last_position, horizon, LEVEL_WINDOW_DAYS)
    if pm2_5_predictions is None:
        print(f"{ERROR} not enough recent history to forecast {zone_id}")
        sys.exit(1)

    # PM10 is forecast the same way but separately. If its record is too thin we
    # simply leave it out rather than inventing it, and the AQI then comes from
    # PM2.5 alone.
    pm10_predictions = None
    pm10_last = len(pm10_logs) - 1
    while pm10_last >= 0 and pm10_logs[pm10_last][1] is None:
        pm10_last = pm10_last - 1
    if pm10_last >= 0:
        pm10_predictions = predict_from(pm10_logs, pm10_last, horizon, LEVEL_WINDOW_DAYS)

    spreads = residual_spread(pm2_5_logs, horizon, LEVEL_WINDOW_DAYS)
    last_day = pm2_5_logs[last_position][0]

    rows = []
    for steps_ahead in range(1, horizon + 1):
        predicted_log = pm2_5_predictions[steps_ahead - 1]
        spread = spreads[steps_ahead]

        pm2_5_value = math.exp(predicted_log)
        pm2_5_low = math.exp(predicted_log - (INTERVAL_MULTIPLIER * spread))
        pm2_5_high = math.exp(predicted_log + (INTERVAL_MULTIPLIER * spread))

        pm10_value = None
        if pm10_predictions is not None:
            pm10_value = math.exp(pm10_predictions[steps_ahead - 1])

        if pm10_value is None:
            pm10_rounded = None
        else:
            pm10_rounded = round(pm10_value, 1)

        aqi_result = indian_aqi(pm2_5_value, pm10_value)

        rows.append({
            "day": last_day + timedelta(days=steps_ahead),
            "steps_ahead": steps_ahead,
            "pm2_5": round(pm2_5_value, 1),
            "pm2_5_low": round(pm2_5_low, 1),
            "pm2_5_high": round(pm2_5_high, 1),
            "pm10": pm10_rounded,
            "aqi": aqi_result[0],
            "main_pollutant": aqi_result[1],
            "category": aqi_category(aqi_result[0]),
        })

    return rows

def command_forecast(zone_id: str, horizon: int, as_json: bool):
    '''
    Prints the forecast for a zone, alongside the weather that is driving it.
    '''
    zone = find_zone(zone_id)
    rows = build_forecast(zone_id, horizon)
    weather = fetch_weather(zone["lat"], zone["lon"], horizon)

    if as_json:
        payload = {"zone_id": zone_id, "zone_name": zone["name"], "days": []}
        for row in rows:
            day = dict(row)
            day["day"] = row["day"].isoformat()
            payload["days"].append(day)
        print(json.dumps(payload, indent=2))
        return

    print()
    print(f"{BOLD}{zone['name']}{RESET}  {GREY}({zone_id}){RESET}")
    print()
    print(f"  {'DAY':<12} {'PM2.5':>7} {'RANGE':>15} {'PM10':>7} {'AQI':>5}  {'CATEGORY':<14} WEATHER")
    print(f"  {GREY}{'-' * 86}{RESET}")

    for row in rows:
        colour = colour_for_aqi(row["aqi"])
        day_label = row["day"].strftime("%a %d %b")
        span = f"{row['pm2_5_low']} to {row['pm2_5_high']}"

        if row["pm10"] is None:
            pm10_label = "-"
        else:
            pm10_label = str(row["pm10"])

        weather_label = ""
        if row["day"] in weather:
            forecast = weather[row["day"]]
            weather_label = "{}, {:.0f} to {:.0f}C".format(
                describe_weather(forecast["code"]),
                forecast["temp_min"],
                forecast["temp_max"],
            )
            if forecast["rain"] is not None and forecast["rain"] >= 1.0:
                weather_label = weather_label + ", {:.0f}mm".format(forecast["rain"])

        print("  {:<12} {:>7} {:>15} {:>7} {}{:>5}{}  {}{:<14}{} {}".format(
            day_label,
            row["pm2_5"],
            span,
            pm10_label,
            colour, row["aqi"], RESET,
            colour, row["category"], RESET,
            weather_label,
        ))

    print()
    print(f"  {GREY}Ranges are 80% intervals from this zone's own past errors.{RESET}")
    print(f"  {GREY}Past about day 3 the forecast is essentially the seasonal average.{RESET}")
    print()

def command_backtest(zone_id: str, horizon: int):
    '''
    Runs the walk-forward test and prints how the model did against the two
    baselines it has to beat.

    Read the skill row first. If it is not comfortably positive then the model
    is not earning its place and should be changed or removed.
    '''
    find_zone(zone_id)
    readings = fetch_history(zone_id, "1y")
    daily = daily_averages(readings, "pm2_5")

    if len(daily) < BACKTEST_WARMUP_DAYS + 20:
        print(f"{ERROR} {zone_id} has {len(daily)} usable days")
        print(f"{INFO} a backtest needs at least {BACKTEST_WARMUP_DAYS + 20}")
        sys.exit(1)

    logs = to_logs(as_day_list(daily))
    outcome = walk_forward(logs, horizon, LEVEL_WINDOW_DAYS)

    measurement = measure_phi(logs, len(logs) - 1, LEVEL_WINDOW_DAYS)
    phi = shrink_phi(measurement[0], measurement[1])

    print()
    print(f"{BOLD}Backtest: {zone_id}{RESET}")
    print()
    print(f"  usable days       {len(daily)}")
    print(f"  forecasts scored  {len(outcome['model'][1])}")
    if measurement[0] is None:
        print("  measured phi      not enough data")
    else:
        print("  measured phi      {:.3f}".format(measurement[0]))
    print("  phi after blend   {:.3f}".format(phi))
    print()

    header = "  {:<22}".format("")
    for steps_ahead in range(1, horizon + 1):
        header = header + "{:>8}".format(f"d+{steps_ahead}")
    print(header)
    print(f"  {GREY}{'-' * (22 + (8 * horizon))}{RESET}")

    for label, key in [("persistence", "persistence"), ("14-day level", "level"), ("this model", "model")]:
        line = "  {:<22}".format(label)
        for steps_ahead in range(1, horizon + 1):
            error = mean_absolute_error(outcome[key][steps_ahead], True)
            if error is None:
                line = line + "{:>8}".format("-")
            else:
                line = line + "{:>8}".format("{:.1f}%".format(error * 100))
        print(line)

    print()
    line = "  {:<22}".format("skill vs persistence")
    for steps_ahead in range(1, horizon + 1):
        model_error = mean_absolute_error(outcome["model"][steps_ahead], True)
        baseline_error = mean_absolute_error(outcome["persistence"][steps_ahead], True)
        skill = skill_score(model_error, baseline_error)
        if skill is None:
            line = line + "{:>8}".format("-")
        else:
            if skill >= 0:
                colour = GREEN
            else:
                colour = RED
            line = line + "{}{:>8}{}".format(colour, "{:+.0f}%".format(skill * 100), RESET)
    print(line)
    print()
    print(f"  {GREY}Errors are typical percentage misses. Positive skill means better")
    print(f"  than assuming tomorrow looks like today.{RESET}")
    print()

def command_record(zone_id: str, horizon: int):
    '''
    Saves today's forecast to the journal, so it can be scored once the days
    have actually happened.
    '''
    find_zone(zone_id)
    rows = build_forecast(zone_id, horizon)
    today = datetime.now(IST).date()
    path = write_journal(zone_id, today, rows)

    print(f"{SUCCESS} recorded {len(rows)} days for {zone_id}")
    print(f"{INFO} {path}")

def command_score(zone_id: str):
    '''
    Grades every recorded forecast whose day has now passed.

    This is the loop that makes the tool improve rather than just run. A
    backtest tells you how the model would have done; this tells you how it
    actually did, on forecasts made before anyone knew the answer.
    '''
    find_zone(zone_id)
    entries = read_journal(zone_id)
    if len(entries) == 0:
        print(f"{WARN} nothing recorded yet for {zone_id}")
        print(f"{INFO} run: forecaster record {zone_id}")
        return

    readings = fetch_history(zone_id, "1y")
    actual = daily_averages(readings, "pm2_5")

    errors_by_lead = {}
    exact_by_lead = {}
    close_by_lead = {}
    scored_rows = []

    for entry in entries:
        for day_entry in entry["days"]:
            day = date.fromisoformat(day_entry["day"])
            if day not in actual:
                continue

            truth = actual[day]
            predicted = day_entry["pm2_5"]
            lead = day_entry["steps_ahead"]

            relative_error = (predicted - truth) / truth

            if lead not in errors_by_lead:
                errors_by_lead[lead] = []
                exact_by_lead[lead] = []
                close_by_lead[lead] = []
            errors_by_lead[lead].append(relative_error)

            truth_aqi = indian_aqi(truth, None)
            truth_category = aqi_category(truth_aqi[0])
            predicted_category = day_entry["category"]

            if predicted_category == truth_category:
                exact_by_lead[lead].append(1)
            else:
                exact_by_lead[lead].append(0)

            truth_position = category_position(truth_category)
            predicted_position = category_position(predicted_category)
            if abs(truth_position - predicted_position) <= 1:
                close_by_lead[lead].append(1)
            else:
                close_by_lead[lead].append(0)

            # Did the truth land inside the range we published? Over many days
            # this should happen about 80% of the time. Much less and we are
            # overconfident, much more and the ranges are uselessly wide.
            if truth >= day_entry["pm2_5_low"] and truth <= day_entry["pm2_5_high"]:
                inside_range = True
            else:
                inside_range = False

            scored_rows.append({
                "day": day,
                "lead": lead,
                "predicted": predicted,
                "truth": round(truth, 1),
                "error": relative_error,
                "inside": inside_range,
            })

    if len(scored_rows) == 0:
        print(f"{WARN} {len(entries)} forecasts recorded, none have come due yet")
        return

    print()
    print(f"{BOLD}Scorecard: {zone_id}{RESET}")
    print()
    print(f"  forecasts recorded  {len(entries)}")
    print(f"  days scored         {len(scored_rows)}")
    print()
    print("  {:<8}{:>8}{:>12}{:>14}{:>12}".format("LEAD", "DAYS", "TYPICAL", "RIGHT BAND", "WITHIN 1"))
    print(f"  {GREY}{'-' * 54}{RESET}")

    for lead in sorted(errors_by_lead):
        errors = errors_by_lead[lead]
        absolute_errors = []
        for error in errors:
            absolute_errors.append(abs(error))

        typical = percentile(absolute_errors, 0.5)
        exact_rate = average(exact_by_lead[lead])
        close_rate = average(close_by_lead[lead])

        print("  {:<8}{:>8}{:>12}{:>14}{:>12}".format(
            f"d+{lead}",
            len(errors),
            "{:.0f}%".format(typical * 100),
            "{:.0f}%".format(exact_rate * 100),
            "{:.0f}%".format(close_rate * 100),
        ))

    inside_count = 0
    for row in scored_rows:
        if row["inside"]:
            inside_count = inside_count + 1
    coverage = inside_count / len(scored_rows)

    print()
    print("  80% ranges actually contained the truth {:.0f}% of the time".format(coverage * 100))
    if coverage < 0.65:
        print(f"  {WARN} that is well under 80%, the ranges are too narrow")
    if coverage > 0.95:
        print(f"  {WARN} that is well over 80%, the ranges are wider than they need to be")

    print()
    print(f"  {BOLD}Most recent scored days{RESET}")
    print("  {:<14}{:>7}{:>12}{:>10}{:>10}".format("DAY", "LEAD", "PREDICTED", "ACTUAL", "OUT BY"))
    print(f"  {GREY}{'-' * 53}{RESET}")

    def sort_key(row):
        return (row["day"], row["lead"])

    recent = sorted(scored_rows, key=sort_key)
    recent = recent[-10:]
    for row in recent:
        if abs(row["error"]) <= 0.25:
            colour = GREEN
        else:
            colour = YELLOW
        if abs(row["error"]) > 0.60:
            colour = RED
        print("  {:<14}{:>7}{:>12}{:>10}{}{:>10}{}".format(
            row["day"].strftime("%a %d %b"),
            f"d+{row['lead']}",
            row["predicted"],
            row["truth"],
            colour,
            "{:+.0f}%".format(row["error"] * 100),
            RESET,
        ))
    print()

def category_position(name: str) -> int:
    '''
    Where a category sits in the ordered list, so that we can ask whether a
    forecast was one band out rather than only whether it was exactly right.
    '''
    position = 0
    for band in AQI_CATEGORIES:
        if band[2] == name:
            return position
        position = position + 1
    return 0

def command_zones():
    '''
    Lists the zones, marking which ones have ground sensors. Zones on satellite
    estimates are listed too, but they are not what this tool is for.
    '''
    zones = fetch_zones()

    ground = []
    satellite = []
    for zone in zones:
        if zone.get("provider") == "airgradient":
            ground.append(zone)
        else:
            satellite.append(zone)

    print()
    print(f"{BOLD}Ground sensors{RESET}  {GREY}(forecastable){RESET}")
    for zone in ground:
        print("  {:<18} {}".format(zone["id"], zone["name"]))

    print()
    print(f"{BOLD}Satellite only{RESET}  {GREY}(no local sensor){RESET}")
    for zone in satellite:
        print("  {:<18} {}".format(zone["id"], zone["name"]))
    print()

def help():
    '''
    Prints usage info, available commands, and examples.
    Also shows where the journal is kept at the bottom.
    '''
    print(f"{INFO} a seven day air quality forecaster for the Breathe network\n")
    print(f"{GREEN}USAGE:{RESET}")
    print("  forecaster forecast <zone> [--days N] [--json]")
    print("  forecaster backtest <zone> [--days N]")
    print("  forecaster record   <zone> [--days N]")
    print("  forecaster score    <zone>")
    print("  forecaster zones")
    print("  forecaster --help\n")

    print(f"{GREEN}COMMANDS:{RESET}")
    print("  forecast      Print the next few days of PM2.5, PM10, AQI and weather")
    print("  backtest      Measure the model against the baselines it has to beat")
    print("  record        Save today's forecast so it can be scored later")
    print("  score         Grade the recorded forecasts whose days have passed")
    print("  zones         List the zones the API knows about")
    print("  --help        Show this help message and exit\n")

    print(f"{GREEN}OPTIONS:{RESET}")
    print("  --days N      How many days ahead, 1 to 14. Default 7")
    print("  --json        Print machine readable output instead of a table\n")

    print(f"{GREEN}EXAMPLES:{RESET}")
    print("  forecaster forecast jammu_city")
    print("  forecaster forecast srinagar --days 3")
    print("  forecaster backtest jammu_city")
    print("  forecaster record jammu_city && forecaster score jammu_city\n")

    print(f"{INFO} API: {API_BASE}")
    print(f"{INFO} Journal: {DATA_DIR}")

# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def read_days_option(args: list) -> int:
    '''
    Pulls --days N out of the argument list and returns the number.
    Removes it from args so that whatever is left is the zone id.
    '''
    horizon = DEFAULT_HORIZON_DAYS

    if "--days" not in args:
        return horizon

    position = args.index("--days")
    if position + 1 >= len(args):
        print(f"{ERROR} --days needs a number after it")
        sys.exit(1)

    try:
        horizon = int(args[position + 1])
    except ValueError:
        print(f"{ERROR} --days needs a number, not '{args[position + 1]}'")
        sys.exit(1)

    if horizon < 1:
        print(f"{ERROR} --days must be at least 1")
        sys.exit(1)
    if horizon > 14:
        print(f"{ERROR} --days above 14 is not meaningful with this model")
        print(f"{INFO} past about day 3 the forecast is already the seasonal average")
        sys.exit(1)

    del args[position + 1]
    del args[position]
    return horizon

def main():
    '''
    Works out which subcommand was asked for and calls it.
    '''
    argv = sys.argv[1:]

    if len(argv) == 0:
        subcommand = ""
    else:
        subcommand = argv[0]

    args = argv[1:]

    as_json = False
    if "--json" in args:
        as_json = True
        args.remove("--json")

    horizon = read_days_option(args)

    # forecaster forecast
    if subcommand == "forecast":
        if len(args) != 1:
            print(f"{INFO} Usage: forecaster forecast <zone>")
            sys.exit(1)
        command_forecast(args[0], horizon, as_json)

    # forecaster backtest
    elif subcommand == "backtest":
        if len(args) != 1:
            print(f"{INFO} Usage: forecaster backtest <zone>")
            sys.exit(1)
        command_backtest(args[0], horizon)

    # forecaster record
    elif subcommand == "record":
        if len(args) != 1:
            print(f"{INFO} Usage: forecaster record <zone>")
            sys.exit(1)
        command_record(args[0], horizon)

    # forecaster score
    elif subcommand == "score":
        if len(args) != 1:
            print(f"{INFO} Usage: forecaster score <zone>")
            sys.exit(1)
        command_score(args[0])

    # forecaster zones
    elif subcommand == "zones":
        if len(args) != 0:
            print(f"{INFO} Usage: forecaster zones")
            sys.exit(1)
        command_zones()

    # forecaster --help
    elif subcommand in ["--help", "-h", ""]:
        if len(args) != 0:
            print(f"{INFO} Usage: forecaster --help")
            sys.exit(1)
        help()

    else:
        print(f"{INFO} Unknown command: {subcommand}")
        print("Run 'forecaster --help' for usage.")
        sys.exit(1)

if __name__ == "__main__":
    main() # call the main function
