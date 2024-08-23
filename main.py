import os
import json
import pymysql
import uvicorn
import pandas as pd
from send_tk import sendTk
from get_balance import GetBalance
from plot import plotSensor
from maps import plotGps
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.encoders import jsonable_encoder
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI()

app.mount("/css", StaticFiles(directory="css"), name="css")
app.mount("/img", StaticFiles(directory="img"), name="img")
templates = Jinja2Templates(directory="templates")

SERVER = os.environ["SERVER"]
DATABASE = os.environ["DATABASE"]
USERNAME = os.environ["USERNAME"]
PASSWORD = os.environ["PASSWORD"]
DRIVER = os.environ["DRIVER"]
TOKEN = os.environ["TOKEN"]
WALLET1 = os.environ["WALLET1"]
WALLET2 = os.environ["WALLET2"]


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """
    this function render front
    :param request:
    :return:
    """

    bal_obj = GetBalance()
    w_1 = bal_obj.fit(wallet=WALLET1)
    w_2 = bal_obj.fit(wallet=WALLET2)

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
    """
    this function get balance a wallet
    :param wallet_balance: wallet to gert balance
    :return: balance_off
    """

    bal_obj = GetBalance()

    balance_off = bal_obj.fit(wallet=wallet_balance)

    return balance_off


@app.get('/send/')
async def send(wallet_send: str, token: str):
    """
    this function send token from main account to any wallet
    :param token:
    :param wallet_send: wallet destination
    :return: if transaction is ok True
    """
    if token == TOKEN:
        amount = 0.1
        tx = sendTk().send(wallet_to_send=wallet_send, amount=amount)
        print(f'Tx is: {tx}')
    else:
        print(f'Not valid token {token}')
        tx = False

    return tx


@app.get('/data_co_send_tokens/')
async def data_co_send_tokens(co2: int, origin: str, token: str, lat: float, lon: float):
    """
    this function send data to database and send token if co2 value up 800 ppm
    :param co2: int, value of ppm co2
    :param origin: str, is origin of data test or sensor
    :param token: uuid for endpoint
    :param lat: float, latitude from sensor
    :param lon: float, longitude from sensor
    :return: None
    """

    with open('data_user.json') as f:
        data_user = json.load(f)

    data_points = int(data_user.get("data"))

    print(data_user)

    print(''.center(60, '='))
    print(f"ppm co2: {co2} , origen: {origin}, lat: {lat}, lon: {lon}")

    if token == TOKEN:
        # insert data in db
        with pymysql.connect(host=SERVER,
                             port=3306,
                             user=USERNAME,
                             passwd=PASSWORD,
                             database=DATABASE) as conn:
            with conn.cursor() as cursor:
                count = cursor.execute(
                    f"INSERT INTO sys.co2Storage (co2, origin, date_c, lat, lon) VALUES ({co2}, '{origin}', CURRENT_TIMESTAMP, {lat}, {lon});")
                conn.commit()
                print(f'Rows inserted: {str(count)}')

        if data_points == 9:
            amount = 0.5
            tx = sendTk().send(wallet_to_send=WALLET2, amount=amount)
            if tx:
                print(f'🤑 send {amount} to {WALLET2} is: {tx}')
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
    """
    this function send data to database and send token if co2 value up 800 ppm
    :param co2: int, value of ppm co2
    :param origin: str, is origin of data test or sensor
    :param lat: float, latitude from sensor
    :param lon: float, longitude from sensor
    :param token: uuid for endpoint
    :return: None
    """

    print(''.center(60, '='))
    print(f"ppm co2: {co2} , origen: {origin}, lat: {lat}, lon: {lon}")

    if token == TOKEN:

        # insert data in db
        with pymysql.connect(host=SERVER,
                             port=3306,
                             user=USERNAME,
                             passwd=PASSWORD,
                             database=DATABASE) as conn:
            with conn.cursor() as cursor:
                count = cursor.execute(
                    f"INSERT INTO sys.co2Storage (co2, origin, date_c, lat, lon) VALUES ({co2}, '{origin}', CURRENT_TIMESTAMP, {lat}, {lon});")
                conn.commit()
                print(f'Rows inserted: {str(count)}')

        print(''.center(60, '='))

    else:
        print(f'Not valid token {token}')


@app.get('/query_co2/')
async def query_co2(rows: int, token: str):
    """
    this function test database
    :param rows: number of rows to query
    :param token: uuid for endpoint
    :return: json with rows in rows param
    """
    if token == TOKEN:

        with pymysql.connect(host=SERVER,
                             port=3306,
                             user=USERNAME,
                             passwd=PASSWORD,
                             database=DATABASE) as conn:
            sql_query = f'SELECT * FROM sys.co2Storage ORDER BY date_c DESC LIMIT {rows}'
            df = pd.read_sql(sql_query, conn)

            json_output = df.to_dict()

            print('OK')

    else:
        print(f'Not valid token {token}')

    json_data = jsonable_encoder(json_output)

    return JSONResponse(content=json_data)


if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=8086)
