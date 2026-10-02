# ---------------------------------------------------------
# INTERACTIVE WEB DASHBOARD GENERATOR (EUROPEAN DATES & PEAK SEASONS)
# ---------------------------------------------------------
def generate_html_dashboard(locations_data, weather_db_all):
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Porcini Intelligence & Growth Forecasting Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {{
            --bg-color: #12181b;
            --card-bg: #1e262c;
            --accent-green: #2ecc71;
            --accent-gold: #f1c40f;
            --accent-orange: #e67e22;
            --accent-blue: #3498db;
            --text-main: #ecf0f1;
            --text-muted: #95a5a6;
            --border-color: #2c3e50;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            margin: 0;
            padding: 20px;
        }}
        header {{
            text-align: center;
            margin-bottom: 30px;
        }}
        h1 {{
            color: var(--accent-green);
            margin-bottom: 5px;
        }}
        .season-badge {{
            display: inline-block;
            background: linear-gradient(135deg, #f1c40f, #e67e22);
            color: #12181b;
            font-weight: bold;
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 0.9rem;
            margin-top: 10px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.2);
        }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(450px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .card {{
            background-color: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.3);
            margin-bottom: 20px;
        }}
        .score-gauge {{
            font-size: 2.2rem;
            font-weight: bold;
            color: var(--accent-green);
        }}
        .status-badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 0.85rem;
            font-weight: bold;
            background-color: rgba(46, 204, 113, 0.2);
            color: var(--accent-green);
            margin-top: 10px;
        }}
        .chart-container {{
            position: relative;
            height: 280px;
            margin-top: 15px;
        }}
        .legend-help {{
            font-size: 0.8rem;
            color: var(--text-muted);
            margin-top: 8px;
            background: rgba(0,0,0,0.2);
            padding: 8px;
            border-radius: 4px;
        }}
        .form-group {{
            margin-bottom: 12px;
        }}
        label {{
            display: block;
            margin-bottom: 4px;
            color: var(--text-muted);
            font-size: 0.85rem;
        }}
        input, select {{
            width: 100%;
            padding: 8px;
            background: #12181b;
            border: 1px solid var(--border-color);
            color: var(--text-main);
            border-radius: 4px;
            box-sizing: border-box;
        }}
        button {{
            background-color: var(--accent-green);
            color: #12181b;
            font-weight: bold;
            padding: 10px 15px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            width: 100%;
        }}
        button:hover {{
            opacity: 0.9;
        }}
        textarea {{
            width: 100%;
            height: 80px;
            background: #12181b;
            border: 1px solid var(--border-color);
            color: var(--accent-green);
            font-family: monospace;
            padding: 8px;
            border-radius: 4px;
            box-sizing: border-box;
            margin-top: 8px;
            font-size: 0.8rem;
        }}
        ul.harvest-list {{
            list-style: none;
            padding: 0;
            margin: 10px 0 0 0;
            font-size: 0.85rem;
            color: var(--text-muted);
        }}
        ul.harvest-list li {{
            background: rgba(0,0,0,0.2);
            padding: 6px 10px;
            border-radius: 4px;
            margin-bottom: 5px;
            display: flex;
            justify-content: space-between;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🍄 Porcini Intelligence & Growth Forecasting Dashboard</h1>
            <p>Multi-Year Weather Archival, Growth Suitability Curves & Exact Historical Logging</p>
            <div id="seasonIndicator"></div>
            <small>Last Generated: {datetime.datetime.utcnow().strftime('%d %B %Y - %H:%M:%S')} UTC</small>
        </header>

        <div class="grid">
    """

    for idx, loc in enumerate(locations_data):
        eval_res = loc["evaluation"]
        harvests = loc.get("past_harvests", [])
        
        harvest_items_html = ""
        for h in harvests:
            harvest_items_html += f"<li><span>{h.get('date')} ({h.get('cap_stage')})</span> <strong>{h.get('yield_tier')}</strong></li>"

        html_content += f"""
            <div class="card">
                <h2>{loc['name']}</h2>
                <p><strong>Coordinates:</strong> {loc['latitude']}, {loc['longitude']} ({loc['elevation_m']}m)</p>
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <div class="score-gauge">{eval_res['score']}%</div>
                        <div class="status-badge">{eval_res['status']}</div>
                    </div>
                    <div style="text-align: right; font-size: 0.9rem; color: var(--text-muted);">
                        <p><strong>Quality:</strong> {eval_res['quality']}</p>
                        <p><strong>Best Window:</strong> {eval_res['best_weekend_day']} ({eval_res['best_weekend_score']}%)</p>
                    </div>
                </div>

                <hr style="border-color: var(--border-color); margin: 20px 0;">

                <h3>🌤️ Micro-Climate & Growth Prediction Timeline</h3>
                <div class="chart-container">
                    <canvas id="weatherChart_{idx}"></canvas>
                </div>
                <div class="legend-help">
                    💡 <strong>How to read:</strong> The green filled curve shows the <strong>Growth Suitability Score (0–100%)</strong>. Spikes above 60% indicate prime flush windows or predicted growth periods driven by recent rain and temperature drops.
                </div>
                
                <hr style="border-color: var(--border-color); margin: 20px 0;">
                
                <h3>🍄 Log Historical / Recent Find</h3>
                <div class="form-group">
                    <label>Date Found (Supports past years):</label>
                    <input type="date" id="date_{idx}" value="2024-09-15">
                </div>
                <div class="form-group">
                    <label>Cap Stage:</label>
                    <select id="stage_{idx}">
                        <option value="buttons_young">Buttons / Young (+20 Boost / Flush Trigger)</option>
                        <option value="prime_open">Prime Open Caps</option>
                        <option value="old_overripe">Old / Overripe (-25 Penalty / End)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Yield Tier:</label>
                    <select id="yield_{idx}">
                        <option value="small">Small (Few mushrooms)</option>
                        <option value="medium">Medium (Basket)</option>
                        <option value="large">Large (Full haul)</option>
                    </select>
                </div>
                <button onclick="addHarvest({idx})">Add to Harvest Log</button>
                
                <h4 style="margin-top: 15px; margin-bottom: 5px;">Current Logged Finds:</h4>
                <ul class="harvest-list" id="list_{idx}">
                    {harvest_items_html if harvest_items_html else "<li>No past finds logged yet.</li>"}
                </ul>

                <div style="margin-top: 15px;">
                    <label><strong>Generated config.json snippet:</strong></label>
                    <textarea id="output_{idx}" readonly>Click 'Add to Harvest Log' to generate JSON snippet...</textarea>
                </div>
            </div>
        """

    html_content += f"""
        </div>

        <div class="card">
            <h2>📈 Multi-Year Backtest & Mycelium Probability Timeline</h2>
            <div class="chart-container" style="height: 350px;">
                <canvas id="porciniChart"></canvas>
            </div>
        </div>
    </div>

    <script>
        const rawLocations = {json.dumps(locations_data)};
        const allWeatherData = {json.dumps(weather_db_all)};

        // Check if current month is peak porcini season (September or October in Europe)
        const currentMonthNum = new Date().getMonth(); // 0-indexed: 8 = September, 9 = October
        const seasonIndicatorEl = document.getElementById('seasonIndicator');
        if (currentMonthNum === 8 || currentMonthNum === 9) {{
            seasonIndicatorEl.innerHTML = '<span class="season-badge">🌟 Peak Porcini Season (September & October Active)</span>';
        }} else {{
            seasonIndicatorEl.innerHTML = '<span style="color: var(--text-muted); font-size: 0.85rem;">Off-Peak / Shoulder Season (Prime months: September & October)</span>';
        }}

        // European Date Formatter: Weekday, Day Month Name (e.g., "Mon, 15 October")
        const dateformatter = new Intl.DateTimeFormat('en-GB', {{ 
            weekday: 'short', 
            day: 'numeric', 
            month: 'long' 
        }});

        rawLocations.forEach((loc, idx) => {{
            const wData = allWeatherData || {{}};
            const dailyHistory = loc.evaluation.daily_history || {{}};
            const dates = Object.keys(wData).sort();
            
            const recentDates = dates.slice(-52);
            const formattedLabels = recentDates.map(dStr => {{
                const d = new Date(dStr);
                return isNaN(d) ? dStr : dateformatter.format(d);
            }});

            const temps = recentDates.map(d => wData[d] ? wData[d].temperature_2m_max : 15);
            const rains = recentDates.map(d => wData[d] ? wData[d].precipitation_sum : 0);
            const scores = recentDates.map(d => dailyHistory[d] ? dailyHistory[d].score : 0);

            const ctxW = document.getElementById('weatherChart_' + idx).getContext('2d');
            new Chart(ctxW, {{
                type: 'line',
                data: {{
                    labels: formattedLabels,
                    datasets: [
                        {{
                            label: 'Growth Suitability (%)',
                            data: scores,
                            borderColor: '#2ecc71',
                            backgroundColor: 'rgba(46, 204, 113, 0.15)',
                            fill: true,
                            yAxisID: 'yScore',
                            tension: 0.2,
                            pointRadius: 2
                        }},
                        {{
                            label: 'Max Temp (°C)',
                            data: temps,
                            borderColor: '#e67e22',
                            backgroundColor: 'transparent',
                            yAxisID: 'yTemp',
                            tension: 0.2,
                            pointRadius: 0
                        }},
                        {{
                            label: 'Precipitation (mm)',
                            data: rains,
                            type: 'bar',
                            backgroundColor: 'rgba(52, 152, 219, 0.4)',
                            yAxisID: 'yRain',
                            barPercentage: 0.6
                        }}
                    ]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {{
                        yScore: {{
                            type: 'linear',
                            position: 'left',
                            min: 0,
                            max: 100,
                            grid: {{ color: '#2c3e50' }},
                            ticks: {{ color: '#2ecc71', font: {{ size: 10 }} }}
                        }},
                        yTemp: {{
                            type: 'linear',
                            position: 'right',
                            grid: {{ display: false }},
                            ticks: {{ color: '#e67e22', font: {{ size: 10 }} }}
                        }},
                        yRain: {{
                            type: 'linear',
                            position: 'right',
                            display: false,
                            beginAtZero: true,
                            max: 100
                        }},
                        x: {{
                            grid: {{ color: '#2c3e50' }},
                            ticks: {{ color: '#95a5a6', font: {{ size: 10 }}, maxTicksLimit: 6 }}
                        }}
                    }},
                    plugins: {{
                        legend: {{ labels: {{ color: '#ecf0f1', boxWidth: 12, font: {{ size: 11 }} }} }}
                    }}
                }}
            }});
        }});

        function addHarvest(locIdx) {{
            const date = document.getElementById('date_' + locIdx).value;
            const cap_stage = document.getElementById('stage_' + locIdx).value;
            const yield_tier = document.getElementById('yield_' + locIdx).value;

            if(!rawLocations[locIdx].past_harvests) {{
                rawLocations[locIdx].past_harvests = [];
            }}

            rawLocations[locIdx].past_harvests.push({{ date, cap_stage, yield_tier }});
            
            const listEl = document.getElementById('list_' + locIdx);
            if (listEl.innerHTML.includes('No past finds')) {{
                listEl.innerHTML = '';
            }}
            listEl.innerHTML += `<li><span>${{date}} (${{cap_stage}})</span> <strong>${{yield_tier}}</strong></li>`;
            
            const textarea = document.getElementById('output_' + locIdx);
            textarea.value = JSON.stringify(rawLocations[locIdx].past_harvests, null, 4);
        }}

        const firstLocEval = rawLocations[0].evaluation.daily_history;
        const rawLabels = Object.keys(firstLocEval).sort();
        const formattedMainLabels = rawLabels.map(dStr => {{
            const d = new Date(dStr);
            return isNaN(d) ? dStr : dateformatter.format(d);
        }});
        const scoreData = rawLabels.map(d => firstLocEval[d].score);

        const ctx = document.getElementById('porciniChart').getContext('2d');
        const porciniChart = new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: formattedMainLabels,
                datasets: [{{
                    label: 'Suitability Score (%)',
                    data: scoreData,
                    borderColor: '#2ecc71',
                    backgroundColor: 'rgba(46, 204, 113, 0.1)',
                    fill: true,
                    tension: 0.2,
                    pointRadius: 0,
                    pointHoverRadius: 6
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                scales: {{
                    y: {{ beginAtZero: true, max: 100, grid: {{ color: '#2c3e50' }}, ticks: {{ color: '#95a5a6' }} }},
                    x: {{ grid: {{ color: '#2c3e50' }}, ticks: {{ color: '#95a5a6' }} }}
                }},
                plugins: {{
                    legend: {{ labels: {{ color: '#ecf0f1' }} }}
                }}
            }}
        }});
    </script>
</body>
</html>
    """
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(html_content)
