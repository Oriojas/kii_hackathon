import os
import json
import pymysql
import uvicorn
import pandas as pd
from send_tk import SendTk
from get_balance import GetBalance
from plot import plotSensor
from maps import plotGps
from dotenv import load_dotenv
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.encoders import jsonable_encoder
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sshtunnel import SSHTunnelForwarder

load_dotenv()

app = FastAPI()

app.mount("/css", StaticFiles(directory="css"), name="css")
app.mount("/img", StaticFiles(directory="img"), name="img")
templates = Jinja2Templates(directory="templates")

SSH_HOST = os.environ.get('SSH_HOST')
SSH_PORT = int(os.environ.get('SSH_PORT'))
SSH_USER = os.environ.get('SSH_USER')
SSH_KEY_PATH = os.environ.get('SSH_KEY_PATH')
DB_HOST = os.environ.get('DB_HOST')
DB_PORT = int(os.environ.get('DB_PORT'))
DB_USER = os.environ.get('DB_USER')
DB_PASSWORD = os.environ.get('DB_PASSWORD')
DB_NAME = os.environ.get('DB_NAME')
TOKEN = os.environ.get('TOKEN')
MAIN_WALLET = os.environ.get('MAIN_WALLET')
SEND_WALLET = os.environ.get('SEND_WALLET')


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):

    bal_obj = GetBalance()
    w_1 = bal_obj.fit(wallet=MAIN_WALLET)
    w_2 = bal_obj.fit(wallet=SEND_WALLET)

    plotSensor().plot(wallet_1=w_1, wallet_2=w_2)
    print(f'Plot OK!')

    plotGps().plot()
    print(f'Map OK!')

    with open('templates/plots/new_plot.txt', 'r', encoding='utf-8') as file:
        plot = file.readlines()
    with open('templates/plots/map.txt', 'r', encoding='utf-8') as file2:
        plot2 = file2.readlines()

    return templates.TemplateResponse("home_page/index.html", {
        "request": request,
        "plot": str(plot[0]),
        "plot2": str(plot2[0]),
    })


@app.get('/balance/')
async def balance(wallet_balance: str):

    bal_obj = GetBalance()

    balance_off = bal_obj.fit(wallet=wallet_balance)

    return balance_off


@app.get('/send/')
async def send(wallet_send: str, token: str):

    if token == TOKEN:
        amount = 0.1
        tx = SendTk().send(wallet_to_send=wallet_send, amount=amount)
        print(f'Tx is: {tx}')
    else:
        print(f'Not valid token {token}')
        tx = False

    return tx


@app.get('/data_co_send_tokens/')
async def data_co_send_tokens(co2: int, origin: str, token: str, lat: float, lon: float):

    with open('data_user.json') as f:
        data_user = json.load(f)

    data_points = int(data_user.get("data"))

    print(data_user)

    print(''.center(60, '='))
    print(f"ppm co2: {co2} , origen: {origin}, lat: {lat}, lon: {lon}")

    if token == TOKEN:

        with SSHTunnelForwarder((SSH_HOST, SSH_PORT),
                                ssh_username=SSH_USER,
                                ssh_password=SSH_KEY_PATH,
                                remote_bind_address=(DB_HOST, DB_PORT),
                                local_bind_address=('127.0.0.1', 10022)) as tunnel:
            conn = pymysql.connect(host='127.0.0.1',
                                   port=tunnel.local_bind_port,
                                   user=DB_USER,
                                   password=DB_PASSWORD,
                                   db=DB_NAME)
            with conn.cursor() as cursor:
                count = cursor.execute(
                    f"INSERT INTO {DB_NAME}.co2Storage (co2, origin, date_c, lat, lon) VALUES ({co2}, '{origin}', CURRENT_TIMESTAMP, {lat}, {lon});")
                conn.commit()
                print(f'Rows inserted: {str(count)}')

        if data_points == 9:
            amount = 0.5
            tx = SendTk().send(wallet_to_send=SEND_WALLET, amount=amount)
            if tx:
                print(f'🤑 send {amount} to {SEND_WALLET} is: {tx}')
                print(f'👌 data send sensor ok and co2 ok')
            else:
                print(f"😭 Transaction denied by network or insufficient gas")

            data_points = {"user": "sensor03",
                           "data": 0}

            json_object = json.dumps(data_points, indent=4)

            with open('data_user.json', "w") as outfile:
                outfile.write(json_object)

        else:
            data_points = {"user": "sensor03",
                           "data": f"{int(data_points + 1)}"}

            json_object = json.dumps(data_points, indent=4)

            with open('data_user.json', "w") as outfile:
                outfile.write(json_object)

            print("🔥 With 10 points send 0.01 ACA")

        print(''.center(60, '='))

    else:
        print(f'Not valid token {token}')


@app.get('/data_co_send/')
async def data_co_send(co2: int, origin: str, token: str, lat: float, lon: float):

    print(''.center(60, '='))
    print(f"ppm co2: {co2} , origen: {origin}, lat: {lat}, lon: {lon}")

    if token == TOKEN:

        with SSHTunnelForwarder((SSH_HOST, SSH_PORT),
                                ssh_username=SSH_USER,
                                ssh_password=SSH_KEY_PATH,
                                remote_bind_address=(DB_HOST, DB_PORT),
                                local_bind_address=('127.0.0.1', 10022)) as tunnel:
            conn = pymysql.connect(host='127.0.0.1',
                                   port=tunnel.local_bind_port,
                                   user=DB_USER,
                                   password=DB_PASSWORD,
                                   db=DB_NAME)
            with conn.cursor() as cursor:
                count = cursor.execute(
                    f"INSERT INTO {DB_NAME}.co2Storage (co2, origin, date_c, lat, lon) VALUES ({co2}, '{origin}', CURRENT_TIMESTAMP, {lat}, {lon});")
                conn.commit()
                print(f'Rows inserted: {str(count)}')

        print(''.center(60, '='))

    else:
        print(f'Not valid token {token}')


@app.get('/query_co2/')
async def query_co2(rows: int, token: str):
    if token == TOKEN:

        with SSHTunnelForwarder((SSH_HOST, SSH_PORT),
                                ssh_username=SSH_USER,
                                ssh_password=SSH_KEY_PATH,
                                remote_bind_address=(DB_HOST, DB_PORT),
                                local_bind_address=('127.0.0.1', 10022)) as tunnel:
            conn = pymysql.connect(host='127.0.0.1',
                                   port=tunnel.local_bind_port,
                                   user=DB_USER,
                                   password=DB_PASSWORD,
                                   db=DB_NAME)
            sql_query = f'SELECT * FROM {DB_NAME}.co2Storage ORDER BY date_c DESC LIMIT {rows}'
            df = pd.read_sql(sql_query, conn)

            json_output = df.to_dict()

            print('OK')

    else:
        print(f'Not valid token {token}')

    json_data = jsonable_encoder(json_output)

    return JSONResponse(content=json_data)


if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=8086)
