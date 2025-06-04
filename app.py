from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import requests
import datetime
import os
import threading

app = Flask(__name__)

STORMGLASS_API_KEY = os.getenv('STORMGLASS_API_KEY')

SPOTS = {
    'balneario': (-26.9931, -48.6350),
    'guarda':    (-27.9496, -48.6189),
    'itajai':    (-26.9101, -48.6536),
    'floripa':   (-27.5954, -48.5480)
}

# Só fontes gratuitas
SOURCES_PRIORITY = ['noaa', 'sg', 'meteo']

CACHE = {}
CACHE_LOCK = threading.Lock()
CACHE_TTL_MINUTES = 30

def degrees_to_direction(degrees):
    dirs = ['Norte', 'Nordeste', 'Leste', 'Sudeste', 'Sul', 'Sudoeste', 'Oeste', 'Noroeste']
    ix = int((degrees + 22.5) / 45) % 8
    return dirs[ix]

def is_cache_valid(cache_time_str):
    try:
        cache_time_dt = datetime.datetime.strptime(cache_time_str, '%Y-%m-%dT%H').replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - cache_time_dt).total_seconds() < CACHE_TTL_MINUTES * 60
    except:
        return False

def get_cached_forecast(spot):
    now = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
    key = (spot, now.strftime('%Y-%m-%dT%H'))
    with CACHE_LOCK:
        entry = CACHE.get(key)
        if entry and is_cache_valid(key[1]):
            print(f"[Cache] Retornando previsão em cache para {spot}")
            return entry
    return None

def set_cached_forecast(spot, forecast_msg):
    now = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
    key = (spot, now.strftime('%Y-%m-%dT%H'))
    with CACHE_LOCK:
        CACHE[key] = forecast_msg

def fallback_open_meteo(lat, lng):
    """
    Fallback usando Open-Meteo (24 h). Se não houver dados, retorna mensagem padrão.
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lng}"
        f"&hourly=wave_height,wind_speed"
        f"&timezone=UTC&forecast_days=1"
    )
    try:
        r = requests.get(url, timeout=10)
    except:
        # Se falhar rede/time-out
        return "Fallback: falha ao checar Open-Meteo. Tente novamente mais tarde."

    if r.status_code != 200:
        return "Fallback: Open-Meteo indisponível agora. Tente novamente mais tarde."

    hourly = r.json().get('hourly', {})
    waves = hourly.get('wave_height', [])
    winds = hourly.get('wind_speed', [])

    if not waves or not winds:
        return "Fallback: sem dados válidos do Open-Meteo. Tente novamente mais tarde."

    avg_wave = sum(waves) / len(waves)
    avg_wind = sum(winds) / len(winds)
    return (
        f"🌊 Fallback Open-Meteo (24 h):\n"
        f"• Altura média das ondas: {avg_wave:.1f} m\n"
        f"• Vento médio: {avg_wind:.1f} m/s\n"
        f"ℹ️ Dados via Open-Meteo."
    )

def get_surf_forecast(spot_name):
    if spot_name not in SPOTS:
        return "Praia não encontrada. Exemplo: surf balneario"

    # 1) Verifica cache
    cached = get_cached_forecast(spot_name)
    if cached:
        return cached

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
    try:
        response = requests.get(url, headers=headers, timeout=10)
    except:
        # Falha de rede ou timeout
        return fallback_open_meteo(LATITUDE, LONGITUDE)

    print(f"[API] Consulta Stormglass ({spot_name}): {response.status_code} | URL: {url}")

    if response.status_code == 402:
        # Agora cai direto no fallback Open-Meteo
        print(f"[Stormglass] 402 para {spot_name}, ativando fallback Open-Meteo.")
        return fallback_open_meteo(LATITUDE, LONGITUDE)

    if response.status_code != 200:
        return "Não consegui obter a previsão no momento 😞"

    data = response.json()
    hours = data.get('hours', [])

    if not hours:
        # Mesmo que seja 200, mas sem horas válidas
        print(f"[Stormglass] Sem dados válidos para {spot_name}, fallback Open-Meteo.")
        return fallback_open_meteo(LATITUDE, LONGITUDE)

    # 2) Organiza dados do dia atual
    forecast_per_day = {}
    for hour_data in hours:
        t = hour_data.get('time')
        if not t:
            continue
        try:
            d = datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))
        except:
            continue
        dia = d.date()
        if dia not in forecast_per_day:
            forecast_per_day[dia] = []

        def get_param(p):
            for src in SOURCES_PRIORITY:
                val = hour_data.get(p, {}).get(src)
                if val is not None:
                    return val
            return None

        wh = get_param('waveHeight')
        wp = get_param('wavePeriod')
        ws = get_param('windSpeed')
        wd = get_param('windDirection')

        if None not in (wh, wp, ws, wd):
            forecast_per_day[dia].append({
                'wave_height': wh,
                'wave_period': wp,
                'wind_speed': ws,
                'wind_dir': wd
            })

    # 3) Monta mensagem para as próximas 24 h (dia atual)
    today = now.date()
    measures = forecast_per_day.get(today, [])
    if not measures:
        # Caso sem dados válidos, fallback
        print(f"[Stormglass] Sem dados hoje para {spot_name}, fallback Open-Meteo.")
        return fallback_open_meteo(LATITUDE, LONGITUDE)

    avg_wh = sum(m['wave_height'] for m in measures) / len(measures)
    avg_wp = sum(m['wave_period'] for m in measures) / len(measures)
    avg_ws = sum(m['wind_speed'] for m in measures) / len(measures)
    avg_wd = sum(m['wind_dir'] for m in measures) / len(measures)
    dir_str = degrees_to_direction(avg_wd)

    forecast_msg = (
        f"🌊 Previsão para {spot_name.title()} (próximas 24 h):\n"
        f"• Ondas: {avg_wh:.1f} m / {avg_wp:.1f} s\n"
        f"• Vento: {avg_ws:.1f} m/s ({dir_str})\n"
    )

    # 4) Guarda no cache e retorna
    set_cached_forecast(spot_name, forecast_msg)
    return forecast_msg

@app.route("/whatsapp", methods=['POST'])
def whatsapp_reply():
    incoming_msg = request.form.get('Body', '').lower().strip()
    resp = MessagingResponse()
    msg = resp.message()

    if incoming_msg.startswith('surf'):
        partes = incoming_msg.split()
        if len(partes) >= 2:
            spot = partes[1]
            forecast = get_surf_forecast(spot)
            print(f"[Bot] Resposta gerada: {forecast}")
            msg.body(forecast)
        else:
            msg.body("Informe a praia. Exemplo: surf balneario")
    else:
        msg.body("Envie no formato: surf [praia]. Exemplo: surf itajai")

    twiml = str(resp)
    print(f"[TwiML] {twiml}")
    return twiml

if __name__ == "__main__":
    app.run(debug=True)
