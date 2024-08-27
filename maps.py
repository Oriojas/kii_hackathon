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
    """
    This class is responsible for fetching and plotting GPS data from a database.

    Attributes:
    rows (int): The number of rows to fetch from the database. Default is 45.

    Methods:
    __init__(self, rows: int = 45): Initializes the class and fetches the data from the database.
    plot(self): Plots the fetched data using Plotly and saves it as a JSON file.
    """

    def __init__(self, rows: int = 45):
        """
        Initializes the class and fetches the data from the database.

        Parameters:
        rows (int): The number of rows to fetch from the database. Default is 45.
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
            sql_query = f'SELECT * FROM {DB_NAME}.co2Storage ORDER BY date_c DESC LIMIT {rows}'
            self.df = pd.read_sql(sql_query, conn)

    def plot(self):
        """
        Plots the fetched data using Plotly and saves it as a JSON file.
        """

        fig = px.density_mapbox(self.df,
                                lat='lat',
                                lon='lon',
                                z='co2',
                                radius=20,
                                zoom=14,
                                height=320,
                                color_continuous_scale=["#e6007a", "#cf006d", "#b80061", "#a10055", "#8a0049",
                                                        "#73003d", "#5c0030", "#450024", "#2e0018", "#17000c",
                                                        "#000000"],
                                mapbox_style="open-street-map")
        # fig.show()
        fig.update_layout(margin=dict(l=10, r=10, t=10, b=10),
)
        fig_json = fig.to_json()

        template = 'var plotly_data2 = {}'

        with open('templates/plots/map_2.txt', 'w', encoding='utf-8') as f:
            f.write(template.format(fig_json))


if __name__ == '__main__':
    plotGps().plot()