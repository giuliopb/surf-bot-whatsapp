from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import requests
import datetime
import os
import threading

app = Flask(__name__)

# Sua API Key da Stormglass (definida em variável de ambiente no Render)
STORMGLASS_API_KEY = os.getenv('STORMGLASS_API_KEY')

# Coordenadas de cada praia suportada
SPOTS = {
    'balneario': (-26.9931, -48.6350),
    'guarda':    (-27.9496, -48.6189),
    'itajai':    (-26.9101, -48.6536),
    'floripa':   (-27.5954, -48.5480)
}

# Prioridade de fontes dentro do próprio Stormglass
SOURCES_PRIORITY = ['noaa', 'sg', 'meteo', 'icon', 'dwd']

# Cache simples em memória: chave = (spot, 'YYYY-MM-DDTHH') → valor = forecast_msg
CACHE = {}
CACHE_LOCK = threading.Lock()
CACHE_TTL_MINUTES = 30  # Tempo de vida de cada entrada do cache em minutos

def degrees_to_direction(degrees):
    """Converte graus numéricos para ponto cardeal."""
    dirs = ['Norte', 'Nordeste', 'Leste', 'Sudeste', 'Sul', 'Sudoeste', 'Oeste', 'Noroeste']
    ix = int((degrees + 22.5) / 45) % 8
    return dirs[ix]

def is_cache_valid(cache_time_str):
    """
    Verifica se o registro em cache ainda está dentro do TTL.
    cache_time_str vem no formato 'YYYY-MM-DDTHH'.
    """
    try:
        cache_time_dt = datetime.datetime.strptime(cache_time_str, '%Y-%m-%dT%H').replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - cache_time_dt).total_seconds() < CACHE_TTL_MINUTES * 60
    except:
        return False

def get_cached_forecast(spot):
    """
    Retorna a previsão em cache para a hora corrente, se existir e estiver válida.
    """
    now = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
    key = (spot, now.strftime('%Y-%m-%dT%H'))
    with CACHE_LOCK:
        entry = CACHE.get(key)
        if entry and is_cache_valid(key[1]):
            print(f"[Cache] Retornando previsão em cache para {spot}")
            return entry
    return None

def set_cached_forecast(spot, forecast_msg):
    """
    Armazena a mensagem de previsão em cache, associada à hora atual (UTC).
    """
    now = datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)
    key = (spot, now.strftime('%Y-%m-%dT%H'))
    with CACHE_LOCK:
        CACHE[key] = forecast_msg

def get_surf_forecast(spot_name):
    """
    Retorna a previsão de surf para um spot em até 3 dias.
    Faz fallback automático entre fontes, aplica cache e trata dados faltantes.
    """
    if spot_name not in SPOTS:
        return "Praia não encontrada. Use por ex.: surf balneario"

    # 1) Verifica cache
    cached = get_cached_forecast(spot_name)
    if cached:
        return cached

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

    print(f"[API] Consulta Stormglass ({spot_name}): {response.status_code} | URL: {url}")
    if response.status_code != 200:
        return 'Não consegui obter a previsão no momento 😞'

    data = response.json()
    forecast_per_day = {}

    # 2) Organizar dados por dia, extraindo valores segundo prioridade de fontes
    for hour_data in data.get('hours', []):
        time_str = hour_data.get('time')
        if not time_str:
            continue
        try:
            # Converte string "YYYY-MM-DDTHH:MM:SSZ" → datetime UTC
            time_obj = datetime.datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        except:
            continue
        date_key = time_obj.date()

        if date_key not in forecast_per_day:
            forecast_per_day[date_key] = []

        def get_param_value(param_name):
            # Retorna o primeiro valor não-nulo da lista de fontes, se existir
            for src in SOURCES_PRIORITY:
                val = hour_data.get(param_name, {}).get(src)
                if val is not None:
                    return val
            return None

        wh = get_param_value('waveHeight')
        wp = get_param_value('wavePeriod')
        ws = get_param_value('windSpeed')
        wd = get_param_value('windDirection')

        # Se qualquer parâmetro for None, ignora esta hora
        if None not in (wh, wp, ws, wd):
            forecast_per_day[date_key].append({
                'wave_height': wh,
                'wave_period': wp,
                'wind_speed': ws,
                'wind_dir': wd
            })

    # 3) Montar a mensagem para até 3 dias
    forecast_msg = f'🌊 Previsão para {spot_name.title()} (3 dias):\n'
    days = list(forecast_per_day.keys())[:3]
    for day in days:
        measures = forecast_per_day.get(day, [])
        if not measures:
            forecast_msg += f"\n📅 {day.strftime('%d/%m/%Y')}: Dados insuficientes.\n"
            continue

        # Calcula média de cada parâmetro
        avg_wave_height = sum(m['wave_height'] for m in measures) / len(measures)
        avg_wave_period = sum(m['wave_period'] for m in measures) / len(measures)
        avg_wind_speed = sum(m['wind_speed'] for m in measures) / len(measures)
        avg_wind_dir = sum(m['wind_dir'] for m in measures) / len(measures)
        wind_dir_str = degrees_to_direction(avg_wind_dir)

        forecast_msg += (
            f"\n📅 {day.strftime('%d/%m/%Y')}:\n"
            f"• Ondas: {avg_wave_height:.1f} m / {avg_wave_period:.1f} s\n"
            f"• Vento: {avg_wind_speed:.1f} m/s ({wind_dir_str})\n"
        )

    # 4) Armazena no cache antes de retornar
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
