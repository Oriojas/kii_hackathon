import os
import time
import requests
import pandas as pd
from dotenv import load_dotenv
from sshtunnel import SSHTunnelForwarder

load_dotenv()

SSH_HOST = os.environ.get('SSH_HOST')
SSH_PORT = int(os.environ.get('SSH_PORT'))
SSH_USER = os.environ.get('SSH_USER')
SSH_KEY_PATH = os.environ.get('SSH_KEY_PATH')
MAIN_DIR = os.environ.get('MAIN_DIR')

REMOTE_HOST = '0.0.0.0'
REMOTE_PORT = 8086

csv_file = f'{MAIN_DIR}/hardware_files/Test_gps/data.csv'
df = pd.read_csv(csv_file)


with SSHTunnelForwarder((SSH_HOST, SSH_PORT),
                        ssh_username=SSH_USER,
                        ssh_password=SSH_KEY_PATH,
                        remote_bind_address=(REMOTE_HOST, REMOTE_PORT),
                        local_bind_address=('127.0.0.1', 10080)) as tunnel:
    local_url = f"http://127.0.0.1:{tunnel.local_bind_port}/data_co_send/"

    for index, row in df.iterrows():
        params = {
            'co2': row['co2'],
            'origin': row['origin'],
            'token': row['token'],
            'lat': row['lat'],
            'lon': row['lon']
        }

        response = requests.get(local_url, params=params, headers={'accept': 'application/json'})

        print(f"Response for row {index}: {response.status_code}, {response.text}")

        time.sleep(1)
