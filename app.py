from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import requests
import datetime
import os

app = Flask(__name__)

STORMGLASS_API_KEY = os.getenv('STORMGLASS_API_KEY')

SPOTS = {
    'balneario': (-26.9931, -48.6350),
    'guarda': (-27.9496, -48.6189),
    'itajai': (-26.9101, -48.6536),
    'floripa': (-27.5954, -48.5480)
}

def degrees_to_direction(degrees):
    dirs = ['Norte', 'Nordeste', 'Leste', 'Sudeste', 'Sul', 'Sudoeste', 'Oeste', 'Noroeste']
    ix = int((degrees + 22.5) / 45) % 8
    return dirs[ix]

def get_surf_forecast(spot_name):
    if spot_name not in SPOTS:
        return "Praia não encontrada. Exemplo: surf balneario"

    LATITUDE, LONGITUDE = SPOTS[spot_name]

    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    end_time = now + datetime.timedelta(days=3)
    end = end_time.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    url = (
        f'https://api.stormglass.io/v2/weather/point'
        f'?lat={LATITUDE}&lng={LONGITUDE}'
        f'&params=waveHeight,windSpeed,windDirection,wavePeriod'
        f'&start={start}&end={end}'
    )

    headers = {'Authorization': STORMGLASS_API_KEY}
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        return 'Não consegui obter a previsão no momento 😞'

    data = response.json()
    forecast_per_day = {}

    for hour_data in data['hours']:
        time_str = hour_data['time']
        time_obj = datetime.datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        date_key = time_obj.date()

        if date_key not in forecast_per_day:
            forecast_per_day[date_key] = []

        try:
            forecast_per_day[date_key].append({
                'wave_height': hour_data['waveHeight']['noaa'],
                'wave_period': hour_data['wavePeriod']['noaa'],
                'wind_speed': hour_data['windSpeed']['noaa'],
                'wind_dir': hour_data['windDirection']['noaa']
            })
        except KeyError:
            continue

    forecast_msg = f'🌊 Previsão para {spot_name.title()} (3 dias):\n'

    for day, measures in list(forecast_per_day.items())[:3]:
        if not measures:
            continue

        avg_wave_height = sum([m['wave_height'] for m in measures]) / len(measures)
        avg_wave_period = sum([m['wave_period'] for m in measures]) / len(measures)
        avg_wind_speed = sum([m['wind_speed'] for m in measures]) / len(measures)
        avg_wind_dir = sum([m['wind_dir'] for m in measures]) / len(measures)
        wind_dir_str = degrees_to_direction(avg_wind_dir)

        forecast_msg += f"\n📅 {day.strftime('%d/%m/%Y')}:\n"
        forecast_msg += f"• Ondas: {avg_wave_height:.1f} m / {avg_wave_period:.1f} s\n"
        forecast_msg += f"• Vento: {avg_wind_speed:.1f} m/s ({wind_dir_str})\n"

    return forecast_msg

@app.route("/whatsapp", methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.form.get('Body').lower().strip()
    resp = MessagingResponse()
    msg = resp.message()

    if incoming_msg.startswith('surf'):
        partes = incoming_msg.split()
        if len(partes) >= 2:
            spot = partes[1]
            forecast = get_surf_forecast(spot)
            msg.body(forecast)
        else:
            msg.body("Informe a praia. Exemplo: surf balneario")
    else:
        msg.body("Envie no formato: surf [praia]. Exemplo: surf itajai")

    return str(resp)

if __name__ == "__main__":
    app.run(debug=True)
