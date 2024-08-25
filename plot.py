import os
import pymysql
import datetime
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


SERVER = os.environ["SERVER"]
DATABASE = os.environ["DATABASE"]
USERNAME = os.environ["USERNAME"]
PASSWORD = os.environ["PASSWORD"]
DRIVER = os.environ["DRIVER"]


class plotSensor:

    def __init__(self):
        """
        this class request database co2 info
        """

        with pymysql.connect(host=SERVER,
                             port=3306,
                             user=USERNAME,
                             passwd=PASSWORD,
                             database=DATABASE) as conn:
            sql_query = f'SELECT * FROM sys.co2Storage'
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
                                 y=DF['co2'][DF['origin'] == 'sensor03'],
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