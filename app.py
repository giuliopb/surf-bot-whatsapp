from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import requests
import datetime
import os
import threading

app = Flask(__name__)

# API Key da Stormglass (variável de ambiente no Render)
STORMGLASS_API_KEY = os.getenv('STORMGLASS_API_KEY')

# Coordenadas das praias suportadas
SPOTS = {
    'balneario': (-26.9931, -48.6350),
    'guarda':    (-27.9496, -48.6189),
    'itajai':    (-26.9101, -48.6536),
    'floripa':   (-27.5954, -48.5480)
}

# Somente fontes gratuitas no Stormglass
SOURCES_PRIORITY = ['noaa', 'sg', 'meteo']

# Cache em memória: {(spot, 'YYYY-MM-DDTHH'): forecast_msg}
CACHE = {}
CACHE_LOCK = threading.Lock()
CACHE_TTL_MINUTES = 30  # minutos de validade no cache

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
    Fallback usando Open-Meteo: retorna média de altura de onda e vento nas próximas 24 h.
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
        return None

    if r.status_code != 200:
        return None

    data = r.json().get('hourly', {})
    waves = data.get('wave_height', [])
    winds = data.get('wind_speed', [])

    if not waves or not winds:
        return None

    avg_wave = sum(waves) / len(waves)
    avg_wind = sum(winds) / len(winds)

    return (
        f"🌊 Fallback Open-Meteo (24 h):\n"
        f"• Altura média das ondas: {avg_wave:.1f} m\n"
        f"• Vento médio: {avg_wind:.1f} m/s\n"
        f"ℹ️ Dados de outra fonte gratuita."
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
    end_time = now + datetime.timedelta(hours=24)  # apenas 24 h
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
        # Se der timeout ou falha de rede, vai para fallback
        fb = fallback_open_meteo(LATITUDE, LONGITUDE)
        return fb or 'Não consegui obter a previsão no momento 😞'

    print(f"[API] Consulta Stormglass ({spot_name}): {response.status_code} | URL: {url}")

    # 2) Se retornar 402, tenta fallback
    if response.status_code == 402:
        print(f"[Stormglass] 402 para {spot_name}, ativando fallback Open-Meteo.")
        fb = fallback_open_meteo(LATITUDE, LONGITUDE)
        return fb or 'Não consegui obter a previsão no momento 😞'

    # 3) Se for outro erro, exibe mensagem de problema
    if response.status_code != 200:
        return 'Não consegui obter a previsão no momento 😞'

    data = response.json()
    hours = data.get('hours', [])

    # 4) Se vier sem dados, fallback
    if not hours:
        print(f"[Stormglass] Sem dados válidos para {spot_name}, fallback Open-Meteo.")
        fb = fallback_open_meteo(LATITUDE, LONGITUDE)
        return fb or 'Dados insuficientes para gerar a previsão no momento 😞'

    # 5) Organiza dados por dia, usando apenas fontes gratuitas
    forecast_per_day = {}
    for hour_data in hours:
        time_str = hour_data.get('time')
        if not time_str:
            continue
        try:
            time_obj = datetime.datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        except:
            continue
        date_key = time_obj.date()
        if date_key not in forecast_per_day:
            forecast_per_day[date_key] = []

        def get_param_value(param_name):
            for src in SOURCES_PRIORITY:
                val = hour_data.get(param_name, {}).get(src)
                if val is not None:
                    return val
            return None

        wh = get_param_value('waveHeight')
        wp = get_param_value('wavePeriod')
        ws = get_param_value('windSpeed')
        wd = get_param_value('windDirection')

        if None not in (wh, wp, ws, wd):
            forecast_per_day[date_key].append({
                'wave_height': wh,
                'wave_period': wp,
                'wind_speed': ws,
                'wind_dir': wd
            })

    # 6) Monta mensagem para as próximas 24 h (dia atual)
    forecast_msg = f'🌊 Previsão para {spot_name.title()} (próximas 24 h):\n'
    today = datetime.datetime.now(datetime.timezone.utc).date()
    measures = forecast_per_day.get(today, [])

    if not measures:
        print(f"[Stormglass] Sem dados válidos hoje em {spot_name}, fallback Open-Meteo.")
        fb = fallback_open_meteo(LATITUDE, LONGITUDE)
        return fb or 'Dados insuficientes para gerar a previsão no momento 😞'

    avg_wave_height = sum(m['wave_height'] for m in measures) / len(measures)
    avg_wave_period = sum(m['wave_period'] for m in measures) / len(measures)
    avg_wind_speed = sum(m['wind_speed'] for m in measures) / len(measures)
    avg_wind_dir = sum(m['wind_dir'] for m in measures) / len(measures)
    wind_dir_str = degrees_to_direction(avg_wind_dir)

    forecast_msg += (
        f"\n• Ondas: {avg_wave_height:.1f} m / {avg_wave_period:.1f} s\n"
        f"• Vento: {avg_wind_speed:.1f} m/s ({wind_dir_str})\n"
    )

    # 7) Armazena no cache e retorna
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
            msg.body(forecast)
        else:
            msg.body("Informe a praia. Exemplo: surf balneario")
    else:
        msg.body("Envie no formato: surf [praia]. Exemplo: surf itajai")

    return str(resp)

if __name__ == "__main__":
    app.run(debug=True)
