
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import requests
import datetime
import os

app = Flask(__name__)

# API Key da Stormglass puxada da variável de ambiente
STORMGLASS_API_KEY = os.getenv('STORMGLASS_API_KEY')

# Coordenadas de cada praia
SPOTS = {
    'balneario': (-26.9931, -48.6350),
    'guarda': (-27.9496, -48.6189),
    'itajai': (-26.9101, -48.6536),
    'floripa': (-27.5954, -48.5480)
}

def get_surf_forecast(spot_name):
    if spot_name not in SPOTS:
        return "Praia não encontrada. Envie por ex: surf balneario"

    LATITUDE, LONGITUDE = SPOTS[spot_name]

    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    end_time = now + datetime.timedelta(hours=24)
    end = end_time.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    url = (
        f'https://api.stormglass.io/v2/weather/point'
        f'?lat={LATITUDE}&lng={LONGITUDE}'
        f'&params=waveHeight,windSpeed,windDirection,wavePeriod'
        f'&start={start}&end={end}'
    )

    headers = {'Authorization': STORMGLASS_API_KEY}
    response = requests.get(url, headers=headers)

    print(f"Consulta feita para {spot_name}: {url}")
    print(f"Resposta HTTP: {response.status_code}")

    if response.status_code != 200:
        return 'Não consegui obter a previsão no momento 😞'

    data = response.json()

    try:
        first_hour = data['hours'][0]
        wave_height = first_hour['waveHeight']['noaa']
        wave_period = first_hour['wavePeriod']['noaa']
        wind_speed = first_hour['windSpeed']['noaa']
        wind_direction = first_hour['windDirection']['noaa']

        forecast = (
            f'🌊 Previsão para {spot_name.title()}:

'
            f'• Altura: {wave_height:.1f} m
'
            f'• Período: {wave_period:.1f} s
'
            f'• Vento: {wind_speed:.1f} m/s ({wind_direction:.0f}°)
'
            f'📅 Atualizado: {now.strftime("%d/%m/%Y %H:%M")} UTC'
        )

        return forecast

    except (KeyError, IndexError):
        return 'Dados insuficientes para gerar a previsão no momento 😞'

@app.route("/whatsapp", methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.form.get('Body').lower().strip()
    print(f"Mensagem recebida: {incoming_msg}")

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
        msg.body("Envie no formato: surf [praia]\nExemplo: surf itajai")

    return str(resp)

if __name__ == "__main__":
    app.run(debug=True)
