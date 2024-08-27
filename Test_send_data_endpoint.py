import os
import time
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

MAIN_DIR = os.environ.get('MAIN_DIR')
TOKEN = os.environ.get('TOKEN')

csv_file = f'{MAIN_DIR}/hardware_files/Test_gps/data.csv'

df = pd.read_csv(csv_file)

url = 'http://0.0.0.0:8086/data_co_send_tokens/'

for index, row in df.iterrows():
    params = {'co2': row['co2'],
              'origin': row['origin'],
              'token': TOKEN,
              'lat': row['lat'],
              'lon': row['lon']}

    response = requests.get(url, params=params, headers={'accept': 'application/json'})

    print(f"Response for row {index}: {response.status_code}, {response.text}")

    time.sleep(120)
