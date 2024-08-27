import os
import pymysql
import pandas as pd
import plotly.express as px
from dotenv import load_dotenv
from sshtunnel import SSHTunnelForwarder

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


class plotGps:

    def __init__(self, rows: int = 45):

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
            self.df = pd.read_sql(sql_query, conn)

    def plot(self):
        fig = px.density_mapbox(self.df,
                                lat='lat',
                                lon='lon',
                                z='co2',
                                radius=20,
                                zoom=14,
                                height=320,
                                color_continuous_scale=["#4e2b93", "#6441a9", "#7a57bf", "#3a1f79", "#25145f",
                                                        "#100945", "#0a062d", "#060418", "#02030c", "#000000"],
                                mapbox_style="open-street-map")
        # fig.show()
        fig.update_layout(margin=dict(l=10, r=10, t=10, b=10),
)
        fig_json = fig.to_json()

        template = 'var plotly_data2 = {}'

        # write the JSON to the HTML template
        with open('templates/plots/map_2.txt', 'w', encoding='utf-8') as f:
            f.write(template.format(fig_json))


if __name__ == '__main__':
    plotGps().plot()