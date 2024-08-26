import os
import pymysql
import datetime
import pandas as pd
from dotenv import load_dotenv
import plotly.graph_objects as go
from sshtunnel import SSHTunnelForwarder
from plotly.subplots import make_subplots

load_dotenv()

SSH_HOST = os.environ.get('SSH_HOST')
SSH_PORT = int(os.environ.get('SSH_PORT'))
SSH_USER = os.environ.get('SSH_USER')
SSH_KEY_PATH = os.environ.get('SSH_KEY_PATH')
DB_HOST = os.environ.get('DB_HOST')
DB_PORT = int(os.environ.get('DB_PORT'))
DB_USER = os.environ.get('DB_USER')
DB_PASSWORD = os.environ.get('DB_PASSWORD')
DB_NAME = os.environ.get('DB_NAME')


class plotSensor:

    def __init__(self):
        """
        this class request database co2 info
        """

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
            sql_query = f'SELECT * FROM {DB_NAME}.co2Storage'
            df = pd.read_sql(sql_query, conn)

        df['date_c'] = pd.to_datetime(df['date_c'])
        end = max(df['date_c'])
        init = end - datetime.timedelta(hours=1)
        print(init)

        self.DF = df[df['date_c'] > init]

    def plot(self, wallet_1, wallet_2):
        DF = self.DF

        balance_w = [wallet_2, wallet_1]
        names = ['reward', 'pool reward']

        fig = make_subplots(1, 2)

        fig.add_trace(go.Scatter(x=DF['date_c'],
                                 y=DF['co2'][DF['origin'] == 'sensor1'],
                                 name='co2 (Sensor)',
                                 mode='lines',
                                 line_color='rgb(230,0,122)'), 1, 1)

        fig.add_trace(go.Bar(name='Grant',
                             x=balance_w,
                             y=names,
                             text=balance_w,
                             orientation='h',
                             textposition='auto',
                             marker_color='rgb(230,0,122)'), 1, 2)

        template = 'plotly_white'
        fig.update_layout(template=template,
                          autosize=True,
                          height=340,
                          margin=dict(l=10, r=10, t=25, b=10),
                          title="PPM co2 and WALLET STATUS LAST HOUR")
        # fig.show()
        fig.update_layout(showlegend=False)
        # convert it to JSON
        fig_json = fig.to_json()

        # a simple HTML template
        template = 'var plotly_data = {}'

        # write the JSON to the HTML template
        with open('templates/plots/new_plot.txt', 'w', encoding='utf-8') as f:
            f.write(template.format(fig_json))


if __name__ == '__main__':
    plotSensor().plot(12, 5)